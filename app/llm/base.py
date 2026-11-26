"""Общий интерфейс LLM и выбор провайдера одной настройкой LLM_PROVIDER."""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger("docagent.llm")


class LLMError(Exception):
    """Ошибка вызова модели."""


class LLMConfigError(LLMError):
    """Модель не настроена (например, нет ключа GigaChat)."""


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = ""
    model: str = ""


class LLM:
    provider = "base"
    model = ""

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:  # pragma: no cover
        raise NotImplementedError

    def healthy(self) -> bool:
        return True

    @staticmethod
    def log_usage(res: LLMResult) -> LLMResult:
        log.info("tokens provider=%s model=%s prompt=%d completion=%d total=%d", res.provider, res.model,
                 res.prompt_tokens, res.completion_tokens, res.prompt_tokens + res.completion_tokens)
        return res


def make_llm(settings) -> LLM:
    provider = settings.llm_provider
    if provider == "gigachat":
        from .gigachat import GigaChatLLM

        return GigaChatLLM(settings)
    if provider == "ollama":
        from .ollama import OllamaLLM

        return OllamaLLM(settings)
    if provider == "fake":
        from .fake import FakeLLM

        return FakeLLM()
    raise LLMConfigError(f"Неизвестный LLM_PROVIDER: {provider}")
