"""Pydantic-схемы HTTP API: описание полей, примеры и теги OpenAPI."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.boxes import GENERAL_BOX

# --- теги --------------------------------------------------------------------------

OPENAPI_TAGS = [
    {"name": "Служебное", "description": "Живость, готовность и версия сервиса. Секреты не возвращаются."},
    {"name": "Чат и поиск", "description": "Вопрос по загруженным документам и ответ со ссылкой на пункт."},
    {"name": "Документы", "description": "Просмотр пунктов, загрузка файлов, удаление и переиндексация."},
    {"name": "Ящички", "description": "Направления документов: кадры, ИТ, финансы и другие."},
    {"name": "Отзывы", "description": "Оценки «да/нет» и кейсы из практики."},
    {"name": "Админ", "description": "Статистика, операторская и служебные действия. Нужен токен администратора."},
]

API_DESCRIPTION = """
Программный интерфейс поиска по корпоративным документам.

**Документация:** интерактивная схема — [`/docs`](/docs) (Swagger UI), читаемый вид — [`/redoc`](/redoc),
машинное описание — [`/openapi.json`](/openapi.json).

**Авторизация.** Публичные методы (чат, ящички, отзывы, служебные) токена не требуют.
Загрузка документов, удаление, переиндексация, статистика и операторская закрыты, пока не задан `ADMIN_TOKEN`.
Токен передаётся заголовком `Authorization: Bearer <токен>` или `X-Admin-Token: <токен>`.
В Swagger нажмите **Authorize** и введите токен в одно из двух полей.

**Ошибки.** Тело ответа всегда такое: `error.code`, `error.message`, при проверке полей — `error.details`,
плюс `request_id` и поле `detail` (то же сообщение, для веб-интерфейса). Идентификатор запроса приходит
во входящем заголовке `X-Request-ID` или выдаётся сервером и возвращается в том же заголовке.

**Ограничения.** Частота вопросов задаётся `RATE_LIMIT_PER_MINUTE` (по умолчанию 30 в минуту с одного адреса,
считается в памяти процесса, либо в Redis, если задан `REDIS_URL`). При превышении — HTTP 429 и заголовок `Retry-After`.
Размер и расширения загружаемых файлов — `MAX_UPLOAD_MB` и `ALLOWED_UPLOAD_EXTENSIONS`.
Список источников для браузерных клиентов — `CORS_ORIGINS` (пусто — только тот же сайт).
""".strip()

ERROR_EXAMPLES = {
    400: {"error": {"code": "bad_request", "message": "Неизвестный ящичек", "details": None},
          "detail": "Неизвестный ящичек", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    401: {"error": {"code": "unauthorized", "message": "Нужен действующий токен администратора", "details": None},
          "detail": "Нужен действующий токен администратора", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    403: {"error": {"code": "forbidden", "message": "Админка закрыта: не задан ADMIN_TOKEN", "details": None},
          "detail": "Админка закрыта: не задан ADMIN_TOKEN", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    404: {"error": {"code": "not_found", "message": "Документ не найден", "details": None},
          "detail": "Документ не найден", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    409: {"error": {"code": "conflict", "message": "Индексация уже идёт", "details": None},
          "detail": "Индексация уже идёт", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    413: {"error": {"code": "payload_too_large", "message": "Файл больше 50 МБ", "details": None},
          "detail": "Файл больше 50 МБ", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    422: {"error": {"code": "validation_error", "message": "Некорректные данные запроса",
                   "details": [{"loc": ["body", "question"], "msg": "String should have at least 2 characters",
                                "type": "string_too_short"}]},
          "detail": "Некорректные данные запроса", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    429: {"error": {"code": "rate_limited", "message": "Слишком много вопросов подряд, подождите минуту", "details": None},
          "detail": "Слишком много вопросов подряд, подождите минуту", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    500: {"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера", "details": None},
          "detail": "Внутренняя ошибка сервера", "request_id": "a1b2c3d4e5f60718293a4b5c6d7e8f90"},
    503: {"status": "not_ready", "demo_ui_only": False, "checks": {"qdrant": False, "redis": None, "llm": True}},
}

ERROR_DESCRIPTIONS = {
    400: "Некорректный запрос (имя файла, ящичек, пункт не из этого ответа)",
    401: "Нет токена или токен неверный",
    403: "Админка закрыта: не задан ADMIN_TOKEN",
    404: "Объект не найден",
    409: "Конфликт: индексация уже выполняется или файл с таким именем уже загружен",
    413: "Файл больше допустимого размера",
    422: "Некорректные данные запроса",
    429: "Превышено ограничение частоты запросов или временная блокировка после неверных токенов",
    500: "Внутренняя ошибка сервера",
    503: "Сервис не готов принимать запросы",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="ignore")


class ErrorItem(_Model):
    loc: list[str | int] = Field(description="Путь к полю")
    msg: str = Field(description="Текст ошибки")
    type: str = Field(description="Тип ошибки проверки")


class ErrorDetail(_Model):
    code: str = Field(description="Машинный код ошибки", examples=["not_found"])
    message: str = Field(description="Понятное сообщение", examples=["Документ не найден"])
    details: list[Any] | None = Field(default=None, description="Подробности, например ошибки проверки полей")


class ErrorResponse(_Model):
    error: ErrorDetail
    detail: str = Field(description="То же, что error.message; для совместимости с веб-интерфейсом")
    request_id: str = Field(description="Идентификатор запроса", examples=["a1b2c3d4e5f60718293a4b5c6d7e8f90"])


def error_responses(*codes: int) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for code in codes:
        model = ReadyOut if code == 503 else ErrorResponse
        out[code] = {
            "model": model,
            "description": ERROR_DESCRIPTIONS[code],
            "content": {"application/json": {"example": ERROR_EXAMPLES[code]}},
        }
    return out


# --- служебное ---------------------------------------------------------------------

class HealthOut(_Model):
    status: Literal["ok"] = Field(description="Процесс отвечает")
    request_id: str | None = Field(default=None, description="Идентификатор запроса, если уже известен")


class ReadyChecks(_Model):
    qdrant: bool = Field(description="Хранилище векторов доступно")
    redis: bool | None = Field(description="Redis доступен; null — не используется")
    llm: bool = Field(description="Провайдер языковой модели отвечает")
    models: bool = Field(default=True, description="Модели поиска загружены (прогрев при старте завершён)")
    index: bool = Field(default=True, description="Первая сверка индекса с папкой документов при старте завершена")


class ReadyOut(_Model):
    status: Literal["ok", "not_ready"] = Field(description="ok — все обязательные проверки пройдены")
    demo_ui_only: bool = Field(description="true — режим без моделей, ответы-заглушки")
    checks: ReadyChecks


class VersionOut(_Model):
    version: str = Field(description="Версия приложения", examples=["0.1.0"])
    app_title: str = Field(description="Заголовок приложения")
    demo_ui_only: bool = Field(description="true — режим без моделей, ответы-заглушки")
    llm_provider: str = Field(description="Провайдер языковой модели", examples=["ollama"])
    llm_model: str = Field(description="Имя модели", examples=["qwen2.5:3b-instruct"])
    embed_provider: str = Field(description="Провайдер эмбеддингов", examples=["e5"])
    reranker_provider: str = Field(description="Провайдер реранкера", examples=["cross-encoder"])


class StatusOut(_Model):
    """Подробное состояние для веб-интерфейса (совместимо с прежним /api/health)."""

    status: Literal["ok"]
    version: str
    qdrant_ok: bool
    redis_ok: bool | None
    llm_provider: str
    llm_model: str
    llm_ok: bool
    clauses: int = Field(description="Сколько пунктов в индексе")
    indexing: bool = Field(description="Идёт ли сейчас индексация")
    admin_enabled: bool = Field(description="Задан ли токен администратора")
    demo_ui_only: bool
    telegram_url: str | None = Field(default=None, description="Ссылка на Telegram-бота, если задан TELEGRAM_BOT_USERNAME")


# --- ящички, чат, отзывы -----------------------------------------------------------

class BoxRef(_Model):
    box: str = Field(description="Идентификатор ящичка", examples=["hr"])
    name: str = Field(description="Название", examples=["Кадровые вопросы"])


class ChatIn(_Model):
    question: str = Field(min_length=2, max_length=2000, description="Вопрос текстом",
                          examples=["Сколько календарных дней ежегодного оплачиваемого отпуска?"])
    boxes: list[str] | None = Field(default=None, description="Ограничить поиск этими ящичками; иначе выбирает маршрутизатор",
                                    examples=[["hr"]])


class OwnerOut(_Model):
    role: str = Field(description="Роль ответственного", examples=["инспектор по кадрам"])
    full_name: str = Field(description="ФИО")
    contact: str = Field(description="Контакт")
    notes: str = ""
    is_example: bool = False
    box: str | None = Field(default=None, description="Ящичек")
    box_name: str | None = Field(default=None, description="Название ящичка")


class PracticeItem(_Model):
    id: int
    question: str = ""
    text: str = Field(description="Как вопрос решили на практике")
    created_at: str | None = None


class CitationOut(_Model):
    n: int = Field(description="Номер ссылки в тексте ответа", examples=[1])
    point_id: str
    doc_id: str
    doc_title: str = Field(examples=["Положение об отпусках"])
    clause_no: str = Field(examples=["3.1"])
    page: int | None = None
    ref: str = Field(description="Человекочитаемая ссылка", examples=["Положение об отпусках, п. 3.1"])
    quote: str = Field(description="Дословная цитата из пункта")
    verified: bool = Field(description="Цитата подтверждена по тексту пункта")
    auto_quote: bool | None = Field(default=None, description="true, если цитата взята из начала пункта")
    box: str | None = None
    doc_date: str | None = Field(default=None, description="Дата файла документа (ДД.ММ.ГГГГ)")


class VariantOut(_Model):
    point_id: str
    doc_id: str
    doc_title: str
    clause_no: str
    page: int | None = None
    ref: str
    section: str | None = None
    text: str = Field(description="Текст пункта")
    box: str | None = None
    score: float = Field(description="Оценка реранкера от 0 до 1")
    practice: list[PracticeItem] = Field(default_factory=list)
    doc_date: str | None = Field(default=None, description="Дата файла документа (ДД.ММ.ГГГГ)")


class TokensOut(_Model):
    prompt: int = 0
    completion: int = 0


class AgentStepOut(_Model):
    step: str = Field(description="guard, route, retrieve, rerank, assess, rewrite, injection, generate, "
                                  "verify_quote, verify_numbers, decide", examples=["rerank"])
    title: str = Field(examples=["Реранжирование"])
    status: Literal["ok", "warn", "fail", "skip"]
    detail: str = Field(examples=["Проверено пунктов: 12. Лучший: Положение об отпусках, п. 3.1 (0.97)"])
    ms: int | None = Field(default=None, description="Сколько занял шаг, мс")


class ChatOut(_Model):
    answer_id: str = Field(description="Идентификатор ответа, нужен для отзыва и практики")
    question: str
    mode: Literal["confident", "uncertain", "not_found", "refused"] = Field(
        description="confident — короткий ответ и цитата; uncertain — 2–3 варианта; not_found — в документах нет; "
                    "refused — вопрос отклонён фильтром нецензурной лексики, поиск не выполнялся")
    text: str = Field(description="Текст ответа")
    score: float = Field(description="Оценка лучшего пункта от 0 до 1")
    boxes: list[BoxRef] = Field(description="Ящички, в которых искали")
    route_method: str = Field(description="Как выбрали ящички")
    searched_all: bool = Field(description="true, если искали по всем документам")
    citations: list[CitationOut] = Field(default_factory=list)
    variants: list[VariantOut] = Field(default_factory=list)
    practice: list[PracticeItem] = Field(default_factory=list)
    advice: str = ""
    owner: OwnerOut | None = None
    ask_feedback: bool = Field(description="Стоит ли сразу спросить оценку ответа")
    latency_ms: int
    tokens: TokensOut
    llm_error: str | None = Field(default=None, description="Код, если языковую модель вызвать не удалось: "
                                                             "llm_unavailable. Подробности только в журнале сервера")
    quote_check: str | None = Field(default=None, description="Почему не удалось подтвердить цитату")
    answer_check: str | None = Field(default=None, description="numbers_mismatch — в ответе модели были числа, "
                                                                "которых нет в пункте; показана только цитата. "
                                                                "advice_dropped — число было только в совете, совет скрыт")
    answer_kind: Literal["answer", "quote_only"] | None = Field(
        default=None, description="answer — ответ модели с цитатой; quote_only — только дословная цитата")
    warnings: list[str] = Field(default_factory=list, description="Предупреждения: похожий пункт в другом "
                                                                   "документе, подозрительный текст в пункте")
    iterations: int = Field(default=0, description="Сколько раз агент переформулировал вопрос и искал заново")
    steps: list[AgentStepOut] = Field(default_factory=list, description="Ход работы агента по шагам")

    model_config = ConfigDict(extra="ignore", json_schema_extra={"examples": [{
        "answer_id": "c0ffee00c0ffee00c0ffee00c0ffee00",
        "question": "Сколько календарных дней ежегодного оплачиваемого отпуска?",
        "mode": "confident",
        "text": "Ежегодный оплачиваемый отпуск составляет 28 календарных дней.",
        "score": 0.97,
        "boxes": [{"box": "hr", "name": "Кадровые вопросы"}],
        "route_method": "keywords",
        "searched_all": False,
        "citations": [{"n": 1, "point_id": "abc", "doc_id": "def", "doc_title": "Положение об отпусках",
                       "clause_no": "3.1", "page": 2, "ref": "Положение об отпусках, п. 3.1",
                       "quote": "Отпуск составляет 28 календарных дней.", "verified": True, "box": "hr"}],
        "variants": [], "practice": [], "advice": "Оформите заявление в кадры.",
        "owner": None, "ask_feedback": False, "latency_ms": 120, "tokens": {"prompt": 0, "completion": 0},
    }]})


class FeedbackIn(_Model):
    answer_id: str = Field(min_length=8, max_length=64, description="Идентификатор ответа из /api/chat",
                           examples=["c0ffee00c0ffee00c0ffee00c0ffee00"])
    helpful: bool = Field(description="true — ответ помог, false — нет")
    point_id: str | None = Field(default=None, description="Пункт из этого ответа, к которому относится оценка")


class FeedbackOut(_Model):
    ok: bool = True
    ask_practice: bool = Field(description="true, если стоит спросить, как вопрос решили на практике")


class PracticeIn(_Model):
    answer_id: str = Field(min_length=8, max_length=64, description="Идентификатор ответа из /api/chat")
    text: str = Field(min_length=3, max_length=4000, description="Как вопрос решили",
                      examples=["Делили отпуск на две части по 14 дней через кадры"])
    point_id: str | None = Field(default=None, description="Пункт, к которому привязать кейс")


class PracticeCreatedOut(_Model):
    ok: bool = True
    id: int = Field(description="Идентификатор записи практики")


class PracticeStatusIn(_Model):
    status: str = Field(pattern="^(new|approved|hidden)$", description="new — новый, approved — одобрен, hidden — скрыт",
                        examples=["approved"])


class OkOut(_Model):
    ok: bool = True


class ClauseContextOut(_Model):
    point_id: str
    clause_no: str
    text: str
    ord: int


class ClauseOut(_Model):
    point_id: str
    doc_title: str
    box: str
    clause_no: str
    section: str | None = None
    page: int | None = None
    text: str
    ref: str
    context: list[ClauseContextOut] = Field(description="Соседние пункты того же документа")
    practice: list[PracticeItem] = Field(default_factory=list)


# --- документы и админ -------------------------------------------------------------

class DocumentOut(_Model):
    doc_id: str
    rel_path: str = Field(description="Путь относительно папки документов")
    title: str
    box: str
    n_clauses: int
    status: str = Field(description="ok или ошибка разбора")
    error: str | None = None
    indexed_at: str | None = None
    size: int | None = Field(default=None, description="Размер файла в байтах")


class IndexStatsOut(_Model):
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    clauses_added: int = 0
    clauses_removed: int = 0
    clauses_kept: int = 0
    errors: list[str] = Field(default_factory=list)


class IndexStatusOut(_Model):
    running: bool
    last: IndexStatsOut | None = None


class UploadOut(_Model):
    ok: bool = Field(description="true, если файл разобран без ошибок")
    file: str = Field(description="Итоговый путь: ящичек/имя файла")
    stats: IndexStatsOut
    replaced: bool = Field(default=False, description="true, если заменена прежняя редакция файла")
    previous_date: str | None = Field(default=None, description="Дата прежней редакции (ДД.ММ.ГГГГ)")
    warning: str | None = None


class DeleteOut(_Model):
    ok: bool = True
    stats: IndexStatsOut


class TokensStatsOut(_Model):
    prompt: int | float = 0
    completion: int | float = 0
    answers: int | float = 0
    avg_latency_ms: int | float = 0


class StatsOut(_Model):
    documents: int
    clauses: int
    vectors: int
    answers_by_mode: dict[str, int] = Field(default_factory=dict)
    tokens: TokensStatsOut
    feedback: dict[str, int] = Field(default_factory=dict, description="Ключи yes и no")
    index: IndexStatusOut


class InboxAnswerOut(_Model):
    answer_id: str
    created_at: str | None = None
    question: str
    boxes: str | None = None


class InboxFeedbackOut(_Model):
    created_at: str | None = None
    question: str | None = None
    doc_title: str | None = None
    clause_no: str | None = None


class InboxPracticeOut(_Model):
    id: int
    created_at: str | None = None
    question: str | None = None
    doc_title: str | None = None
    clause_no: str | None = None
    box: str | None = None
    text: str | None = None
    status: str = "new"


class InboxOut(_Model):
    not_found: list[InboxAnswerOut] = Field(description="Вопросы, на которые в документах не нашлось ответа")
    negative_feedback: list[InboxFeedbackOut] = Field(description="Отрицательные оценки")
    practice: list[InboxPracticeOut] = Field(description="Кейсы практики, в том числе на модерации")


# чтобы error_responses мог сослаться на ReadyOut, объявление выше уже есть
GENERAL_BOX_DEFAULT = GENERAL_BOX
