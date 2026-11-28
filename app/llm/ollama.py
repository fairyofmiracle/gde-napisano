"""Локальная модель через Ollama."""
from __future__ import annotations

import json
import re

import httpx

from .base import LLM, LLMError, LLMResult

_THINK_OPEN = "<think>"
_THINK_CLOSE = "</think>"
_THINK_BLOCK = re.compile(r"<think>.*?(</think>|$)", re.S)
# Подсказка для моделей семейства Qwen3, если сервер Ollama не понимает параметр think
_NO_THINK_HINT = "/no_think"


def strip_think(text: str) -> str:
    """Убирает блоки размышлений <think>…</think>; незакрытый блок отбрасывается целиком."""
    return _THINK_BLOCK.sub("", text or "").strip()


class ThinkFilter:
    """Вырезает <think>…</think> из потока кусочков, не теряя текст на стыках."""

    def __init__(self) -> None:
        self.inside = False
        self.buf = ""

    def feed(self, piece: str) -> str:
        self.buf += piece
        out: list[str] = []
        while True:
            if self.inside:
                i = self.buf.find(_THINK_CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(_THINK_CLOSE) - 1):]
                    break
                self.buf = self.buf[i + len(_THINK_CLOSE):].lstrip()
                self.inside = False
                continue
            i = self.buf.find(_THINK_OPEN)
            if i >= 0:
                out.append(self.buf[:i])
                self.buf = self.buf[i + len(_THINK_OPEN):]
                self.inside = True
                continue
            keep = 0
            for k in range(min(len(self.buf), len(_THINK_OPEN) - 1), 0, -1):
                if _THINK_OPEN.startswith(self.buf[-k:]):
                    keep = k
                    break
            out.append(self.buf[:len(self.buf) - keep])
            self.buf = self.buf[len(self.buf) - keep:]
            break
        return "".join(out)

    def flush(self) -> str:
        rest, self.buf = ("" if self.inside else self.buf), ""
        return rest


class OllamaLLM(LLM):
    provider = "ollama"

    def __init__(self, settings) -> None:
        self.base = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model
        self.num_ctx = settings.ollama_num_ctx
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self.timeout = settings.ollama_timeout
        # None — параметр не передаётся (поведение по умолчанию модели);
        # False — размышления отключены (think=false), True — включены
        self.think = getattr(settings, "ollama_think", None)
        self._think_param_ok = True

    def _payload(self, system: str, user: str, json_mode: bool, stream: bool) -> dict:
        if self.think is False and not self._think_param_ok:
            system = f"{system}\n{_NO_THINK_HINT}"
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": stream,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx, "num_predict": self.max_tokens},
        }
        if self.think is not None and self._think_param_ok:
            payload["think"] = self.think
        if json_mode:
            payload["format"] = "json"
        return payload

    def _think_rejected(self, status: int, body: str) -> bool:
        """Старый сервер или модель не принимают параметр think — переходим на подсказку в промпте."""
        if "think" in body.lower() and status in (400, 422, 500) and self._think_param_ok and self.think is not None:
            self._think_param_ok = False
            return True
        return False

    def _error(self, status: int, body: str) -> LLMError:
        if status == 404 and "not found" in body:
            return LLMError(f"Модель {self.model} не скачана в Ollama: выполните ollama pull {self.model}")
        return LLMError(f"Ollama ответила ошибкой {status}: {body}")

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        for _ in range(2):
            try:
                r = httpx.post(f"{self.base}/api/chat", json=self._payload(system, user, json_mode, False),
                               timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                break
            except httpx.HTTPStatusError as e:
                body = e.response.text[:300]
                if self._think_rejected(e.response.status_code, body):
                    continue
                raise self._error(e.response.status_code, body) from e
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama недоступна по адресу {self.base}: {e}") from e
        return self.log_usage(LLMResult(
            text=strip_think((data.get("message") or {}).get("content", "")),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            provider=self.provider, model=self.model,
        ))

    def chat_stream(self, system: str, user: str, json_mode: bool = False, on_delta=None) -> LLMResult:
        for _ in range(2):
            parts: list[str] = []
            last: dict = {}
            flt = ThinkFilter()
            retry = False

            def push(text: str) -> None:
                if text:
                    parts.append(text)
                    if on_delta is not None:
                        on_delta(text)

            try:
                with httpx.stream("POST", f"{self.base}/api/chat",
                                  json=self._payload(system, user, json_mode, True), timeout=self.timeout) as r:
                    if r.status_code >= 400:
                        body = r.read().decode("utf-8", "replace")[:300]
                        if self._think_rejected(r.status_code, body):
                            retry = True
                        else:
                            raise self._error(r.status_code, body)
                    else:
                        for line in r.iter_lines():
                            if not line.strip():
                                continue
                            try:
                                chunk = json.loads(line)
                            except ValueError:
                                continue
                            if chunk.get("error"):
                                raise LLMError(f"Ollama: {str(chunk['error'])[:200]}")
                            # поле message.thinking (размышления) намеренно не показываем
                            push(flt.feed((chunk.get("message") or {}).get("content") or ""))
                            if chunk.get("done"):
                                last = chunk
                        push(flt.flush())
            except httpx.HTTPError as e:
                raise LLMError(f"Ollama недоступна по адресу {self.base}: {e}") from e
            if not retry:
                break
        return self.log_usage(LLMResult(
            text=strip_think("".join(parts)),
            prompt_tokens=int(last.get("prompt_eval_count") or 0),
            completion_tokens=int(last.get("eval_count") or 0),
            provider=self.provider, model=self.model,
        ))

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base}/api/tags", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False
