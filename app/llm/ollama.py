"""Локальная модель через Ollama."""
from __future__ import annotations

import json

import httpx

from .base import LLM, LLMError, LLMResult


class OllamaLLM(LLM):
    provider = "ollama"

    def __init__(self, settings) -> None:
        self.base = settings.ollama_base_url.rstrip("/")
        self.model = settings.ollama_model
        self.num_ctx = settings.ollama_num_ctx
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self.timeout = settings.ollama_timeout

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx, "num_predict": self.max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = httpx.post(f"{self.base}/api/chat", json=payload, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:300]
            if e.response.status_code == 404 and "not found" in body:
                raise LLMError(f"Модель {self.model} не скачана в Ollama: выполните ollama pull {self.model}") from e
            raise LLMError(f"Ollama ответила ошибкой {e.response.status_code}: {body}") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama недоступна по адресу {self.base}: {e}") from e
        return self.log_usage(LLMResult(
            text=(data.get("message") or {}).get("content", "").strip(),
            prompt_tokens=int(data.get("prompt_eval_count") or 0),
            completion_tokens=int(data.get("eval_count") or 0),
            provider=self.provider, model=self.model,
        ))

    def chat_stream(self, system: str, user: str, json_mode: bool = False, on_delta=None) -> LLMResult:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": True,
            "options": {"temperature": self.temperature, "num_ctx": self.num_ctx, "num_predict": self.max_tokens},
        }
        if json_mode:
            payload["format"] = "json"
        parts: list[str] = []
        last: dict = {}
        try:
            with httpx.stream("POST", f"{self.base}/api/chat", json=payload, timeout=self.timeout) as r:
                if r.status_code >= 400:
                    body = r.read().decode("utf-8", "replace")[:300]
                    if r.status_code == 404 and "not found" in body:
                        raise LLMError(f"Модель {self.model} не скачана в Ollama: выполните ollama pull {self.model}")
                    raise LLMError(f"Ollama ответила ошибкой {r.status_code}: {body}")
                for line in r.iter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except ValueError:
                        continue
                    if chunk.get("error"):
                        raise LLMError(f"Ollama: {str(chunk['error'])[:200]}")
                    piece = (chunk.get("message") or {}).get("content") or ""
                    if piece:
                        parts.append(piece)
                        if on_delta is not None:
                            on_delta(piece)
                    if chunk.get("done"):
                        last = chunk
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama недоступна по адресу {self.base}: {e}") from e
        return self.log_usage(LLMResult(
            text="".join(parts).strip(),
            prompt_tokens=int(last.get("prompt_eval_count") or 0),
            completion_tokens=int(last.get("eval_count") or 0),
            provider=self.provider, model=self.model,
        ))

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.base}/api/tags", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False
