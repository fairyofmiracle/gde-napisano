"""Клиент к API docagent: бот только пересылает вопросы в /api/chat, поиск и модели остаются в приложении."""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import httpx


class ApiError(Exception):
    """Приложение недоступно или вернуло ошибку."""


class RateLimited(ApiError):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__("rate_limited")
        self.retry_after = retry_after


class DocagentClient:
    def __init__(self, base_url: str, timeout: float = 180.0, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport)

    async def ask(self, question: str, session_id: str | None = None, division: str | None = None) -> dict:
        body: dict = {"question": question}
        if session_id:
            body["session_id"] = session_id
        if division:
            body["division"] = division
        try:
            r = await self._http.post("/api/chat", json=body)
        except httpx.HTTPError as e:
            raise ApiError(f"Приложение недоступно: {type(e).__name__}") from e
        if r.status_code == 429:
            try:
                retry = int(r.headers.get("retry-after", "60"))
            except ValueError:
                retry = 60
            raise RateLimited(retry)
        if r.status_code != 200:
            raise ApiError(f"Приложение ответило кодом {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise ApiError("Приложение вернуло не JSON") from e

    async def ask_stream(self, question: str, on_event: Callable[[dict], Awaitable[None]],
                         session_id: str | None = None, division: str | None = None) -> dict:
        """Вопрос через /api/chat/stream: события хода ответа уходят в on_event, возвращается итоговый ответ.
        Если у приложения нет потокового адреса, вопрос задаётся обычным /api/chat."""
        body: dict = {"question": question}
        if session_id:
            body["session_id"] = session_id
        if division:
            body["division"] = division
        try:
            async with self._http.stream("POST", "/api/chat/stream", json=body) as r:
                if r.status_code in (404, 405):
                    return await self.ask(question, session_id=session_id, division=division)
                if r.status_code == 429:
                    try:
                        retry = int(r.headers.get("retry-after", "60"))
                    except ValueError:
                        retry = 60
                    raise RateLimited(retry)
                if r.status_code != 200:
                    raise ApiError(f"Приложение ответило кодом {r.status_code}")
                if "ndjson" not in r.headers.get("content-type", ""):
                    # приложение ответило обычным JSON (без потока): это и есть итоговый ответ
                    await r.aread()
                    try:
                        return r.json()
                    except ValueError as e:
                        raise ApiError("Приложение вернуло не JSON") from e
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    kind = ev.get("type")
                    if kind == "final":
                        return ev.get("answer") or {}
                    if kind == "error":
                        raise ApiError("stream_error")
                    if kind != "ping":
                        try:
                            await on_event(ev)
                        except Exception:  # сбой показа хода ответа не должен терять сам ответ
                            pass
        except httpx.HTTPError as e:
            raise ApiError(f"Приложение недоступно: {type(e).__name__}") from e
        raise ApiError("Ответ прервался")

    async def simplify(self, answer_id: str) -> dict:
        try:
            r = await self._http.post("/api/simplify", json={"answer_id": answer_id})
        except httpx.HTTPError as e:
            raise ApiError(f"Приложение недоступно: {type(e).__name__}") from e
        if r.status_code == 429:
            try:
                retry = int(r.headers.get("retry-after", "60"))
            except ValueError:
                retry = 60
            raise RateLimited(retry)
        if r.status_code == 400:
            raise ApiError("simplify_unavailable")
        if r.status_code == 404:
            raise ApiError("answer_not_found")
        if r.status_code != 200:
            raise ApiError(f"Приложение ответило кодом {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise ApiError("Приложение вернуло не JSON") from e

    async def close(self) -> None:
        await self._http.aclose()
