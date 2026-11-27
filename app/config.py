"""Настройки приложения. Все значения читаются из переменных окружения или файла .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_title: str = "Где написано?"
    # Username бота без @ (для ссылки в веб-интерфейсе). Пусто = ссылку не показываем.
    telegram_bot_username: str = ""

    # Пути
    data_dir: Path = Path("./data/runtime")
    docs_dir: Path = Path("./data/runtime/docs")
    boxes_csv: Path = Path("./data/boxes.csv")

    # Хранилище векторов: либо сервер (QDRANT_URL), либо локальная папка (QDRANT_PATH)
    qdrant_url: str = ""
    qdrant_path: str = "./data/runtime/qdrant"
    qdrant_api_key: str = ""
    qdrant_collection: str = "clauses"

    # Redis необязателен (ограничение частоты запросов). Пусто = в памяти.
    redis_url: str = ""
    rate_limit_per_minute: int = 30
    # Отдельные лимиты (в минуту с одного адреса). 0 — без ограничения.
    feedback_rate_limit_per_minute: int = 20
    practice_rate_limit_per_minute: int = 5
    upload_rate_limit_per_minute: int = 10
    admin_rate_limit_per_minute: int = 60
    # Адреса внутренних клиентов (например, контейнер Telegram-бота), у которых свои лимиты на пользователя.
    # Для них действует RATE_LIMIT_TRUSTED_PER_MINUTE вместо RATE_LIMIT_PER_MINUTE.
    trusted_client_ips: str = ""
    rate_limit_trusted_per_minute: int = 300

    # Модели
    embed_provider: Literal["e5", "fake"] = "e5"
    embed_model: str = "intfloat/multilingual-e5-base"
    reranker_provider: Literal["cross-encoder", "fake"] = "cross-encoder"
    reranker_model: str = "BAAI/bge-reranker-v2-m3"
    reranker_max_length: int = 512
    model_device: str = "cpu"

    # Поиск
    vector_top_k: int = 30
    bm25_top_k: int = 30
    rerank_top_n: int = 12
    conflict_check: bool = True  # отдельная проверка противоречий между пунктами одного документа
    answer_contexts: int = 3  # сколько пунктов отдавать модели для ответа
    answer_ctx_chars: int = 900  # длина одного пункта в запросе к модели, 0 = без ограничения
    rrf_k: int = 60
    max_clause_chars: int = 1200

    # Режимы ответа (оценка реранкера, строго 0..1)
    # Подобраны на 53 тестовых вопросах (eval/summary.json) с запасом под реальные документы
    confident_threshold: float = 0.50
    uncertain_threshold: float = 0.03
    # Если второй кандидат почти так же хорош, а документы разные, это не уверенный ответ
    ambiguity_margin: float = 0.05
    max_variants: int = 3
    # Если в выбранных ящичках оценка ниже этого значения, ищем по всем документам
    box_fallback_threshold: float = 0.5

    # Память отзывов
    memory_similarity: float = 0.90
    memory_boost: float = 0.5
    memory_penalty: float = 0.3
    # Потолок сдвига оценки памятью отзывов (абсолютный, 0..1): отзывы не могут вытащить нерелевантный пункт
    memory_max_boost: float = 0.15
    feedback_sample_rate: float = 0.02  # примерно 1 из 50 уверенных ответов
    practice_requires_approval: bool = True  # True = кейсы практики видны только после одобрения оператором
    strict_quotes: bool = True

    # Маршрутизатор ящичков
    router_use_llm: bool = False
    router_min_similarity: float = 0.80

    # Агент: переформулировка вопроса при низкой уверенности и повторный поиск
    agent_rewrite: bool = True
    agent_max_rewrites: int = 1  # 0..2
    agent_rewrite_use_llm: bool = True  # False = только правила (без обращения к модели)
    agent_rewrite_penalty: float = 0.05  # оценка, полученная по переформулировке, чуть ниже исходной
    # Числа и даты в ответе модели должны встречаться в процитированных пунктах, иначе — только цитата
    check_numbers: bool = True

    # LLM
    llm_provider: Literal["gigachat", "ollama", "fake"] = "ollama"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 600
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b-instruct"
    ollama_num_ctx: int = 8192
    ollama_timeout: float = 180.0
    gigachat_credentials: str = Field(default="", repr=False)
    gigachat_scope: str = "GIGACHAT_API_PERS"
    gigachat_model: str = "GigaChat"
    gigachat_ca_bundle: str = ""  # путь к корневому сертификату Минцифры (russian_trusted_root_ca.pem)
    gigachat_timeout: float = 60.0
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    llm_retries: int = 2  # повторы при сетевых ошибках, 429 и 5xx
    llm_retry_backoff: float = 1.0  # секунды, удваивается с каждой попыткой
    # Предохранитель: после стольких сбоев подряд модель считается недоступной на llm_breaker_cooldown секунд
    # (0 = выключен). Пока он открыт, запросы не ждут таймаутов, а отвечают цитатой пункта.
    llm_breaker_failures: int = 2
    llm_breaker_cooldown: float = 60.0
    # Если лучший пункт оценён реранкером не ниже этого значения, а модель ответила «ответа нет»,
    # показываем пункт дословно со ссылкой, а не уходим в «нет в документах» (1.01 = выключено)
    quote_fallback_score: float = 0.95
    llm_max_concurrency: int = 2  # одновременных запросов к модели на процесс

    # OCR и форматы
    ocr_lang: str = "rus+eng"
    ocr_min_chars_per_page: int = 30
    ocr_dpi: int = 300
    tesseract_cmd: str = ""  # путь к tesseract.exe в Windows, если его нет в PATH
    soffice_cmd: str = "soffice"
    max_upload_mb: int = 50
    # Пусто = все форматы, которые умеет разбирать приложение. Иначе список через запятую, например .pdf,.docx,.txt
    allowed_upload_extensions: str = ""

    # Фильтр нецензурной лексики во входящих вопросах. Слова-исключения через запятую.
    profanity_filter: bool = True
    profanity_allowlist: str = ""

    # Пусто = CORS выключен (только тот же сайт). Список источников через запятую или * — любой источник.
    cors_origins: str = ""

    # Доступ. Пустой токен = админка, загрузка и операторская закрыты.
    admin_token: str = Field(default="", repr=False)
    admin_token_min_length: int = 16
    # Пусто = с любого адреса. Иначе адреса или подсети через запятую: 1.2.3.4,10.0.0.0/8
    admin_allowed_ips: str = ""
    # После стольких неверных токенов подряд адрес блокируется; блокировка удваивается до часа
    admin_max_failures: int = 5
    admin_lockout_seconds: int = 300

    # Глоссарий сокращений (TSV: сокращение<TAB>расшифровка). Нет файла = без расширения запроса.
    glossary_tsv: Path = Path("./data/glossary.tsv")

    # Служба качества и имитация профиля сотрудника (для письма-эскалации: сотрудник в копии)
    quality_email: str = "quality-test@example.local"
    demo_user_email: str = "employee-test@example.local"
    demo_user_name: str = "Тестовый сотрудник"
    # Ссылка на веб-интерфейс для писем (пусто = без ссылки)
    web_public_url: str = ""
    # SMTP для писем в службу качества. Пусто = письма складываются в DATA_DIR/outbox как .eml
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = Field(default="", repr=False)
    smtp_from: str = "gde-napisano@example.local"
    smtp_starttls: bool = True
    smtp_ssl: bool = False
    smtp_timeout: float = 15.0
    # IMAP для чтения ответов экспертов (пусто = только импорт .eml и форма в операторской)
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = Field(default="", repr=False)
    imap_folder: str = "INBOX"
    # Эскалация при недостаточной уверенности (варианты без подтверждённого ответа)
    escalate_on_uncertain: bool = True

    # Разъяснения службы качества (FAQ): порог похожести вопроса и минимальная доля общих слов
    faq_similarity: float = 0.93
    faq_similarity_soft: float = 0.85
    faq_min_overlap: float = 0.4
    # Сколько повторов одного вопроса — сигнал «пересмотреть регламент»
    review_repeat_threshold: int = 3

    # Уточнение подразделения: максимум уточнений за сессию и порог «ответы расходятся»
    clarify_max: int = 2
    clarify_ratio: float = 0.6

    # Стоимость запроса (для аналитики): цена 1000 токенов облачной модели, руб.,
    # и стоимость часа стенда с локальной моделью, руб.
    llm_price_per_1k_tokens: float = 0.0
    stand_cost_per_hour: float = 0.0

    index_on_start: bool = True
    # Прогрев моделей при старте (в фоне); /ready отвечает 503, пока прогрев не закончен
    warmup_on_start: bool = True
    log_level: str = "INFO"

    # Режим для работы над интерфейсом: модели не скачиваются и не загружаются,
    # поиск идёт по словам, ответы собираются заглушкой из найденного пункта.
    demo_ui_only: bool = False

    @model_validator(mode="after")
    def _demo_mode(self):
        if self.demo_ui_only:
            self.embed_provider = "fake"
            self.reranker_provider = "fake"
            self.llm_provider = "fake"
        self.allowed_upload_ext_set  # проверяем список расширений при старте
        self.admin_allowed_networks  # и список адресов админки
        self.agent_max_rewrites = max(0, min(2, self.agent_max_rewrites))
        return self

    @property
    def db_path(self) -> Path:
        return self.data_dir / "docagent.sqlite3"

    @property
    def outbox_dir(self) -> Path:
        return self.data_dir / "outbox"

    @property
    def allowed_upload_ext_set(self) -> set[str]:
        from app.parsing import SUPPORTED_EXTENSIONS

        raw = {e.strip().lower() for e in self.allowed_upload_extensions.split(",") if e.strip()}
        if not raw:
            return set(SUPPORTED_EXTENSIONS)
        normalized = {e if e.startswith(".") else f".{e}" for e in raw}
        unknown = normalized - set(SUPPORTED_EXTENSIONS)
        if unknown:
            raise ValueError("В ALLOWED_UPLOAD_EXTENSIONS есть форматы, которые приложение не разбирает: "
                             + ", ".join(sorted(unknown)))
        return normalized

    @property
    def trusted_client_ip_set(self) -> set[str]:
        return {x.strip() for x in self.trusted_client_ips.split(",") if x.strip()}

    @property
    def admin_allowed_networks(self) -> list:
        import ipaddress

        return [ipaddress.ip_network(x.strip(), strict=False) for x in self.admin_allowed_ips.split(",") if x.strip()]

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
