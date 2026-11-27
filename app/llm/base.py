"""Общий интерфейс LLM и выбор провайдера одной настройкой LLM_PROVIDER."""
from __future__ import annotations

import logging
import threading
import time
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

    def chat_stream(self, system: str, user: str, json_mode: bool = False, on_delta=None) -> LLMResult:
        """Тот же ответ, что chat, но куски текста по мере генерации отдаются в on_delta.
        Провайдеры без потока отдают весь текст одним куском."""
        res = self.chat(system, user, json_mode=json_mode)
        if on_delta is not None and res.text:
            on_delta(res.text)
        return res

    def healthy(self) -> bool:
        return True

    @staticmethod
    def log_usage(res: LLMResult) -> LLMResult:
        log.info("tokens provider=%s model=%s prompt=%d completion=%d total=%d", res.provider, res.model,
                 res.prompt_tokens, res.completion_tokens, res.prompt_tokens + res.completion_tokens)
        return res


class GuardedLLM(LLM):
    """Предохранитель вокруг облачной или локальной модели.

    После нескольких сбоев подряд модель на время считается недоступной: запросы сразу получают LLMError,
    и сотрудник не ждёт повторов и таймаутов на каждый вопрос. По истечении паузы пробуется один запрос;
    успех закрывает предохранитель. Ошибки настройки (нет ключа) пробрасываются как есть."""

    def __init__(self, inner: LLM, failures: int = 2, cooldown: float = 60.0, clock=time.monotonic) -> None:
        self.inner = inner
        self.provider = inner.provider
        self.model = inner.model
        self.failures = max(1, int(failures))
        self.cooldown = float(cooldown)
        self._clock = clock
        self._fails = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    @property
    def is_open(self) -> bool:
        return self._clock() < self._open_until

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        return self._guarded(lambda: self.inner.chat(system, user, json_mode=json_mode))

    def chat_stream(self, system: str, user: str, json_mode: bool = False, on_delta=None) -> LLMResult:
        return self._guarded(lambda: self.inner.chat_stream(system, user, json_mode=json_mode, on_delta=on_delta))

    def _guarded(self, call) -> LLMResult:
        with self._lock:
            if self._clock() < self._open_until:
                raise LLMError("модель временно недоступна (предохранитель открыт)")
        try:
            res = call()
        except LLMConfigError:
            raise
        except LLMError:
            with self._lock:
                self._fails += 1
                if self._fails >= self.failures:
                    self._open_until = self._clock() + self.cooldown
                    log.warning("LLM %s: %d сбоев подряд, пауза %.0f с", self.provider, self._fails, self.cooldown)
            raise
        with self._lock:
            self._fails = 0
            self._open_until = 0.0
        return res

    def healthy(self) -> bool:
        return (not self.is_open) and self.inner.healthy()

    def close(self) -> None:
        close = getattr(self.inner, "close", None)
        if close:
            close()


def make_llm(settings) -> LLM:
    llm = _make_llm(settings)
    n = int(getattr(settings, "llm_breaker_failures", 0) or 0)
    if n > 0 and llm.provider != "fake":
        return GuardedLLM(llm, n, float(getattr(settings, "llm_breaker_cooldown", 60.0)))
    return llm


def _make_llm(settings) -> LLM:
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
