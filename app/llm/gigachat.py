"""GigaChat API. Без ключа — явная ошибка, проверка SSL всегда включена.

Один клиент на процесс (SDK сам обновляет токен доступа), не больше LLM_MAX_CONCURRENCY запросов
одновременно, повторы с паузой при сетевых сбоях, 429 и 5xx. Текст ошибок наружу не отдаётся."""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .base import LLM, LLMConfigError, LLMError, LLMResult

log = logging.getLogger(__name__)

# Куда docker-compose монтирует папку ./certs; если сертификат там лежит, он подхватывается сам
DEFAULT_CA_PATHS = ("/certs/russian_trusted_root_ca.pem", "./certs/russian_trusted_root_ca.pem")


def find_ca_bundle(configured: str) -> str | None:
    ca = (configured or "").strip()
    if ca:
        if not Path(ca).is_file():
            raise LLMConfigError(f"Файл сертификата GIGACHAT_CA_BUNDLE не найден: {ca}")
        return ca
    for p in DEFAULT_CA_PATHS:
        if Path(p).is_file():
            return p
    return None


def _retryable(e: Exception) -> bool:
    try:
        from gigachat.exceptions import RateLimitError, ResponseError, ServerError
    except ImportError:  # pragma: no cover
        return False
    if isinstance(e, (RateLimitError, ServerError)):
        return True
    if isinstance(e, ResponseError):
        return False  # 400/401/403/404/413/422 повтор не исправит
    try:
        import httpx

        return isinstance(e, (httpx.TransportError, httpx.TimeoutException))
    except ImportError:  # pragma: no cover
        return False


def _describe(e: Exception) -> str:
    """Короткое описание для журнала: без тела ответа и заголовков."""
    code = getattr(e, "status_code", None)
    text = f"{type(e).__name__}" + (f" HTTP {code}" if code else "")
    if "CERTIFICATE_VERIFY_FAILED" in str(e):
        text += (" (нет корневого сертификата НУЦ Минцифры: положите его в ./certs/russian_trusted_root_ca.pem "
                 "или укажите путь в GIGACHAT_CA_BUNDLE)")
    return text


class GigaChatLLM(LLM):
    provider = "gigachat"

    def __init__(self, settings, client_factory=None, sleep=time.sleep) -> None:
        if not settings.gigachat_credentials.strip():
            raise LLMConfigError(
                "LLM_PROVIDER=gigachat, но GIGACHAT_CREDENTIALS не задан. "
                "Укажите ключ авторизации или переключите LLM_PROVIDER=ollama."
            )
        self.ca = find_ca_bundle(settings.gigachat_ca_bundle)
        if self.ca is None:
            log.warning("Корневой сертификат Минцифры не найден: запросы к GigaChat из контейнера, скорее всего, "
                        "не пройдут проверку SSL. См. README, раздел про сертификат")
        self.credentials = settings.gigachat_credentials.strip()
        self.scope = settings.gigachat_scope
        self.model = settings.gigachat_model
        self.base_url = settings.gigachat_base_url
        self.timeout = settings.gigachat_timeout
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self.retries = max(0, int(getattr(settings, "llm_retries", 2)))
        self.backoff = max(0.0, float(getattr(settings, "llm_retry_backoff", 1.0)))
        self._slots = threading.BoundedSemaphore(max(1, int(getattr(settings, "llm_max_concurrency", 2))))
        self._factory = client_factory or self._make_client
        self._client = None
        self._client_lock = threading.Lock()
        self._sleep = sleep

    def _make_client(self):
        from gigachat import GigaChat

        # Адрес задан явно: у библиотеки по умолчанию api.giga.chat, который из ряда сетей отвечает 404
        return GigaChat(credentials=self.credentials, scope=self.scope, model=self.model, verify_ssl_certs=True,
                        ca_bundle_file=self.ca, timeout=self.timeout, base_url=self.base_url)

    @property
    def client(self):
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._factory()
        return self._client

    def _reset_client(self) -> None:
        with self._client_lock:
            old, self._client = self._client, None
        close = getattr(old, "close", None)
        if close:
            try:
                close()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        self._reset_client()

    def chat(self, system: str, user: str, json_mode: bool = False) -> LLMResult:
        from gigachat.models import Chat, Messages, MessagesRole

        payload = Chat(
            messages=[Messages(role=MessagesRole.SYSTEM, content=system), Messages(role=MessagesRole.USER, content=user)],
            temperature=max(self.temperature, 0.01),
            max_tokens=self.max_tokens,
        )
        attempt = 0
        while True:
            try:
                with self._slots:
                    resp = self.client.chat(payload)
                break
            except Exception as e:
                if attempt < self.retries and _retryable(e):
                    attempt += 1
                    delay = self.backoff * (2 ** (attempt - 1))
                    log.warning("GigaChat: %s, повтор %d из %d через %.1f с", _describe(e), attempt, self.retries,
                                delay)
                    if "Transport" in type(e).__name__ or "Timeout" in type(e).__name__:
                        self._reset_client()
                    self._sleep(delay)
                    continue
                raise LLMError(f"GigaChat: {_describe(e)}") from e
        usage = getattr(resp, "usage", None)
        return self.log_usage(LLMResult(
            text=(resp.choices[0].message.content or "").strip(),
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            provider=self.provider, model=self.model,
        ))

    def chat_stream(self, system: str, user: str, json_mode: bool = False, on_delta=None) -> LLMResult:
        """Потоковый ответ. Повтор возможен только до первого куска текста, дальше ошибка уходит наверх."""
        from gigachat.models import Chat, Messages, MessagesRole

        payload = Chat(
            messages=[Messages(role=MessagesRole.SYSTEM, content=system), Messages(role=MessagesRole.USER, content=user)],
            temperature=max(self.temperature, 0.01),
            max_tokens=self.max_tokens,
        )
        attempt = 0
        while True:
            parts: list[str] = []
            usage = None
            try:
                with self._slots:
                    for chunk in self.client.stream(payload):
                        if getattr(chunk, "usage", None) is not None:
                            usage = chunk.usage
                        for ch in chunk.choices or []:
                            piece = getattr(ch.delta, "content", None) or ""
                            if piece:
                                parts.append(piece)
                                if on_delta is not None:
                                    on_delta(piece)
                break
            except Exception as e:
                if not parts and attempt < self.retries and _retryable(e):
                    attempt += 1
                    delay = self.backoff * (2 ** (attempt - 1))
                    log.warning("GigaChat: %s, повтор %d из %d через %.1f с", _describe(e), attempt, self.retries,
                                delay)
                    if "Transport" in type(e).__name__ or "Timeout" in type(e).__name__:
                        self._reset_client()
                    self._sleep(delay)
                    continue
                raise LLMError(f"GigaChat: {_describe(e)}") from e
        return self.log_usage(LLMResult(
            text="".join(parts).strip(),
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            provider=self.provider, model=self.model,
        ))
