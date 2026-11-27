"""Клиент к API docagent: бот только пересылает вопросы в /api/chat, поиск и модели остаются в приложении."""
from __future__ import annotations

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

    async def close(self) -> None:
        await self._http.aclose()
