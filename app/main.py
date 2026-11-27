"""HTTP API и веб-чат."""
from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.boxes import GENERAL_BOX
from app.config import Settings, get_settings
from app.errors import (
    current_request_id,
    install_exception_handlers,
    install_request_id_log_factory,
    install_request_middleware,
)
from app.schemas import (
    API_DESCRIPTION,
    OPENAPI_TAGS,
    BoxRef,
    ChatIn,
    ChatOut,
    ClauseOut,
    DeleteOut,
    DocumentOut,
    ErrorResponse,
    EscalateIn,
    ExpertAnswerIn,
    FeedbackIn,
    FeedbackOut,
    HealthOut,
    InboxOut,
    IndexStatsOut,
    OkOut,
    PracticeCreatedOut,
    PracticeIn,
    PracticeStatusIn,
    ReadyOut,
    StatusOut,
    StatsOut,
    UploadOut,
    VersionOut,
    WikiOut,
    SimplifyIn,
    SimplifyOut,
    error_responses,
)
from app.security import AdminGuard, client_ip, client_key, require_admin, safe_filename, safe_join
from app.services import Services, build_services

log = logging.getLogger("docagent")
STATIC = Path(__file__).resolve().parent.parent / "static"

ADMIN_ERRORS = error_responses(401, 403, 429)
ADMIN_DOC_ERRORS = error_responses(400, 401, 403, 404, 409, 413, 429)
CHAT_ERRORS = error_responses(422, 429)
_ADMIN_SECURITY = [{"BearerAuth": []}, {"AdminToken": []}]


def _apply_admin_security(schema: dict) -> None:
    schema.setdefault("components", {}).setdefault("securitySchemes", {}).update({
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "token",
            "description": "Токен администратора в заголовке Authorization: Bearer <токен>",
        },
        "AdminToken": {
            "type": "apiKey",
            "in": "header",
            "name": "X-Admin-Token",
            "description": "Тот же токен заголовком X-Admin-Token",
        },
    })
    for path, item in schema.get("paths", {}).items():
        if not (path.startswith("/api/admin") or path.startswith("/api/operator")):
            continue
        for method, op in item.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            op["security"] = _ADMIN_SECURITY


def _configure_cors(app: FastAPI, settings: Settings) -> None:
    origins = settings.cors_origin_list
    if not origins:
        return
    allow_all = origins == ["*"]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"] if allow_all else origins,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )


# Строгая политика для своих страниц: только свои скрипты и стили, без встраивания в чужие сайты.
CSP_APP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
           "font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'")
# Swagger и ReDoc грузят скрипты с CDN и используют встроенный скрипт
CSP_DOCS = ("default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
            "img-src 'self' data: https://fastapi.tiangolo.com https://cdn.redoc.ly; "
            "font-src 'self' https://fonts.gstatic.com; worker-src 'self' blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")


def _install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        path = request.url.path
        h = response.headers
        h.setdefault("Content-Security-Policy", CSP_DOCS if path in ("/docs", "/redoc") else CSP_APP)
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("Referrer-Policy", "no-referrer")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        h.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if path.startswith(("/api/admin", "/api/operator", "/admin")):
            h["Cache-Control"] = "no-store"
        elif path.startswith("/static/") and path.rsplit(".", 1)[-1].lower() in ("js", "css"):
            h["Cache-Control"] = "no-cache, must-revalidate"
        return response


def _warmup(app_: FastAPI) -> None:
    """Загружает модели заранее, чтобы первый вопрос после старта не ждал 10+ секунд."""
    st = app_.state.warm
    t0 = __import__("time").perf_counter()
    try:
        s = app_.state.services
        s.embedder.embed_queries(["прогрев"])
        s.reranker.score("прогрев", ["прогрев моделей"])
        st["error"] = None
    except Exception as e:  # стенд не должен падать из-за прогрева: модели догрузятся при первом вопросе
        log.exception("прогрев моделей не удался")
        st["error"] = type(e).__name__
    finally:
        st["done"] = True
        st["seconds"] = round(__import__("time").perf_counter() - t0, 1)
        log.info("прогрев моделей завершён за %.1f с", st["seconds"])


def create_app(settings: Settings | None = None, services: Services | None = None) -> FastAPI:
    settings = settings or (services.settings if services else get_settings())
    install_request_id_log_factory()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s request_id=%(request_id)s: %(message)s",
        force=False,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        svc = services or build_services(settings)
        app.state.services = svc
        app.state.index_status = {"running": False, "last": None, "startup_done": not settings.index_on_start}
        app.state.warm = {"done": not settings.warmup_on_start, "error": None, "seconds": None}
        if settings.warmup_on_start:
            fake = settings.embed_provider == "fake" and settings.reranker_provider == "fake"
            if fake:
                _warmup(app)  # заглушки грузятся мгновенно
            else:
                threading.Thread(target=_warmup, args=(app,), daemon=True).start()
        if settings.index_on_start:
            threading.Thread(target=_sync, args=(app, True), daemon=True).start()
        yield
        if services is None:
            svc.close()

    app = FastAPI(
        title=settings.app_title,
        version=__version__,
        description=API_DESCRIPTION,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
        openapi_tags=OPENAPI_TAGS,
        license_info={"name": "MIT"},
        servers=[{"url": "/", "description": "Текущий сервер"}],
        responses={422: {"model": ErrorResponse, "description": "Некорректные данные запроса"}},
    )
    app.state.settings = settings
    app.state.admin_guard = AdminGuard(settings.admin_max_failures, settings.admin_lockout_seconds)
    if settings.admin_token and len(settings.admin_token) < settings.admin_token_min_length:
        log.error("ADMIN_TOKEN короче %d символов: админка будет закрыта", settings.admin_token_min_length)
    install_exception_handlers(app)
    install_request_middleware(app)
    _configure_cors(app, settings)
    _install_security_headers(app)

    def custom_openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
            tags=app.openapi_tags,
            servers=app.servers,
            license_info=app.license_info,
        )
        _apply_admin_security(schema)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi

    def svc(request: Request) -> Services:
        return request.app.state.services

    def _sync(app_: FastAPI, startup: bool = False) -> dict:
        st = app_.state.index_status
        st["running"] = True
        try:
            res = app_.state.services.indexer.sync().as_dict()
            st["last"] = res
            return res
        finally:
            st["running"] = False
            if startup:
                st["startup_done"] = True

    # --- страницы ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index_page():
        return FileResponse(STATIC / "index.html")

    @app.get("/doc/{doc_id}", include_in_schema=False)
    def doc_page(doc_id: str):
        return FileResponse(STATIC / "doc.html")

    @app.get("/wiki", include_in_schema=False)
    def wiki_page():
        return FileResponse(STATIC / "wiki.html")

    @app.get("/mail", include_in_schema=False)
    def mail_page():
        return FileResponse(STATIC / "mail.html")

    @app.get("/gaps", include_in_schema=False)
    def gaps_page():
        return FileResponse(STATIC / "gaps.html")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        return FileResponse(STATIC / "admin.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "img" / "favicon.ico", media_type="image/x-icon")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # --- служебное -----------------------------------------------------------------
    @app.get("/health", response_model=HealthOut, tags=["Служебное"], summary="Проверка живости",
             description="Возвращает 200, пока процесс принимает запросы. Зависимости не проверяет — для этого есть `/ready`.")
    def liveness(request: Request):
        return HealthOut(status="ok", request_id=current_request_id(request))

    @app.get("/ready", response_model=ReadyOut, tags=["Служебное"], summary="Проверка готовности",
             description="Проверяет хранилище векторов, Redis (если задан REDIS_URL) и провайдер языковой модели. "
                         "В режиме DEMO_UI_ONLY поле demo_ui_only=true, ответы при этом — заглушки. "
                         "Если обязательная проверка не пройдена, возвращается HTTP 503.",
             responses=error_responses(503))
    def readiness(request: Request):
        s: Services = svc(request)
        checks = {
            "qdrant": s.store.healthy(),
            "redis": s.limiter.redis_ok(),
            "llm": s.llm.healthy(),
            "models": bool(getattr(request.app.state, "warm", {"done": True})["done"]),
            # после перезапуска или восстановления из копии индекс пересобирается: до конца отвечать нечем
            "index": bool(request.app.state.index_status["startup_done"]),
        }
        required = [checks["qdrant"], checks["llm"], checks["models"], checks["index"]]
        if checks["redis"] is not None:
            required.append(checks["redis"])
        ok = all(required)
        body = ReadyOut(status="ok" if ok else "not_ready", demo_ui_only=settings.demo_ui_only, checks=checks)
        if ok:
            return body
        return JSONResponse(status_code=503, content=body.model_dump(),
                            headers={"X-Request-ID": current_request_id(request)})

    @app.get("/version", response_model=VersionOut, tags=["Служебное"], summary="Версия и режим",
             description="Версия приложения, режим (в том числе демо) и провайдеры моделей. Секреты и токены не включаются.")
    def version():
        return VersionOut(
            version=__version__,
            app_title=settings.app_title,
            demo_ui_only=settings.demo_ui_only,
            llm_provider=settings.llm_provider,
            llm_model=settings.ollama_model if settings.llm_provider == "ollama" else (
                settings.gigachat_model if settings.llm_provider == "gigachat" else "fake"),
            embed_provider=settings.embed_provider,
            reranker_provider=settings.reranker_provider,
        )

    @app.get("/api/health", response_model=StatusOut, tags=["Служебное"],
             summary="Подробное состояние",
             description="Состояние хранилища, модели и индекса. Им пользуется веб-интерфейс. "
                         "Для оркестраторов предпочтительнее `/health` и `/ready`.")
    def health(request: Request):
        s: Services = svc(request)
        n = s.db.query("SELECT COUNT(*) AS n FROM clauses")[0]["n"]
        tg = (settings.telegram_bot_username or "").strip().lstrip("@")
        return {
            "status": "ok", "version": __version__, "qdrant_ok": s.store.healthy(),
            "redis_ok": s.limiter.redis_ok(), "llm_provider": s.llm.provider, "llm_model": s.llm.model,
            "llm_ok": s.llm.healthy(), "clauses": n, "indexing": request.app.state.index_status["running"],
            "admin_enabled": bool(settings.admin_token), "demo_ui_only": settings.demo_ui_only,
            "telegram_url": f"https://t.me/{tg}" if tg else None,
            "quality_email": settings.quality_email, "demo_user_email": settings.demo_user_email,
        }

    # --- публичное API -------------------------------------------------------------
    @app.get("/api/boxes", response_model=list[BoxRef], tags=["Ящички"], summary="Список ящичков",
             description="Направления документов (кадры, ИТ, финансы и другие) из справочника.")
    def list_boxes(request: Request):
        s: Services = svc(request)
        return [{"box": b, "name": s.boxes.name(b)} for b in s.boxes.ids()]

    @app.post("/api/chat", response_model=ChatOut, tags=["Чат и поиск"], summary="Задать вопрос",
              description="Ищет ответ в загруженных документах и возвращает режим (уверенный ответ с цитатой, "
                          "варианты пунктов или «в документах нет»). Вопрос с нецензурной лексикой не ищется: режим `refused` "
                          "с вежливым отказом (отключается `PROFANITY_FILTER=off`). Частота запросов ограничена "
                          f"`RATE_LIMIT_PER_MINUTE` (сейчас {settings.rate_limit_per_minute} в минуту с одного адреса; "
                          "0 — без ограничения).",
              responses=CHAT_ERRORS)
    async def chat(body: ChatIn, request: Request):
        s: Services = svc(request)
        client = client_ip(request)
        trusted = client in settings.trusted_client_ip_set
        limit = settings.rate_limit_trusted_per_minute if trusted else None
        if not s.limiter.allow(client if not trusted else f"trusted:{client}", limit):
            raise HTTPException(429, "Слишком много вопросов подряд, подождите минуту",
                                headers={"Retry-After": "60"})
        return await run_in_threadpool(
            lambda: s.assistant.ask(body.question, session_id=body.session_id or "", division=body.division,
                                    boxes=body.boxes, user_email=body.user_email or "",
                                    user_name=body.user_name or ""))

    @app.post("/api/escalate", tags=["Чат и поиск"], summary="Передать вопрос в службу качества",
              description="Сотрудник сам просит передать вопрос эксперту (например, после вариантов по "
                          "подразделениям). Письмо уходит на QUALITY_EMAIL, сотрудник в копии.",
              responses=error_responses(404, 422))
    def escalate(body: EscalateIn, request: Request):
        s: Services = svc(request)
        _limit(s, request, "escalate", settings.practice_rate_limit_per_minute, "Слишком много запросов подряд")
        ans = s.memory.get_answer(body.answer_id)
        if ans is None:
            raise HTTPException(404, "Ответ не найден")
        sess = s.assistant.session(body.session_id or "")
        cands = json.loads(ans["candidates"] or "[]")
        found = []
        for c in cands[:3]:
            rows = s.db.query("SELECT text, label FROM clauses WHERE point_id=?", (c["point_id"],))
            if rows:
                from app.parsing import format_ref

                found.append({"ref": format_ref(c["doc_title"], rows[0]["label"] or c["clause_no"]), "text": rows[0]["text"],
                              "doc_id": c["doc_id"], "doc_title": c["doc_title"], "clause_no": c["clause_no"]})
        div = sess.get("division") or ""
        esc = s.escalations.create(ans["question"], "manual", found,
                                   user_email=body.user_email or sess.get("user_email") or "",
                                   user_name=sess.get("user_name") or "",
                                   division=div if div in s.boxes.boxes else "",
                                   division_name=s.boxes.name(div) if div in s.boxes.boxes else "",
                                   answer_id=body.answer_id, session_id=body.session_id or "")
        s.db.execute("UPDATE answers SET escalated=1 WHERE answer_id=?", (body.answer_id,))
        return {"ok": True, "escalation": esc,
                "text": f"Отправил запрос в службу качества ({esc['to']}), вы в копии ({esc['cc']})."}

    @app.post("/api/simplify", response_model=SimplifyOut, tags=["Чат и поиск"],
              summary="Пересказать ответ простыми словами",
              description="Тот же ответ по уже найденным пунктам, без нового поиска. Цитаты и номера пунктов "
                          "не меняются. Если в пересказе появляются числа, которых нет в исходном ответе и цитатах, "
                          "возвращается исходный текст с раскрытыми сокращениями. Для ответов без цитат и для "
                          "передач в службу качества упрощение недоступно.",
              responses=error_responses(400, 404, 422, 429))
    async def simplify_answer(body: SimplifyIn, request: Request):
        s: Services = svc(request)
        _limit(s, request, "simplify", settings.practice_rate_limit_per_minute,
               "Слишком много запросов подряд")
        from app.simplify import can_simplify, payload_from_answer, simplify

        stored_id = body.answer_id
        payload = None
        if stored_id:
            ans = s.memory.get_answer(stored_id)
            if ans is None:
                raise HTTPException(404, "Ответ не найден")
            raw = ans["payload"] if "payload" in ans.keys() else ""
            if raw and raw not in ("", "{}"):
                try:
                    payload = json.loads(raw)
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = None
            if not payload:
                # ответ записан до появления поля payload: собираем из тела запроса, иначе отказываем
                if not (body.text and body.citations):
                    raise HTTPException(400, "Для этого ответа нет сохранённого текста, пришлите сам ответ")
        if payload is None:
            payload = payload_from_answer({
                "question": body.question or "",
                "mode": body.mode or "confident",
                "text": body.text or "",
                "citations": [c.model_dump() if hasattr(c, "model_dump") else c for c in (body.citations or [])],
                "glossary": body.glossary or [],
                "advice": body.advice or "",
                "answer_kind": "answer",
                "escalated": bool(body.escalated),
            })
        else:
            # если клиент прислал поля, ими не подменяем сохранённый ответ: источник правды — запись в базе
            pass
        if not can_simplify(payload):
            raise HTTPException(400, "Для этого ответа упрощение недоступно")
        result = await run_in_threadpool(lambda: simplify(s.answers.llm, payload, db=s.db))
        result["answer_id"] = stored_id
        result["can_simplify"] = True
        return result

    @app.get("/api/wiki", response_model=WikiOut, tags=["Документы"],
             summary="Список регламентов и разъяснений",
             description="Проиндексированные регламенты и опубликованные разъяснения службы качества "
                         "для страницы хранилища. Токен не нужен.")
    def wiki_index(request: Request):
        s: Services = svc(request)
        from app.answer import doc_date

        docs = []
        for r in s.db.query(
            "SELECT doc_id, title, box, mtime, n_clauses FROM files "
            "WHERE status='ok' AND n_clauses > 0 ORDER BY box, title"):
            docs.append({
                "doc_id": r["doc_id"], "title": r["title"], "box": r["box"] or "",
                "box_name": s.boxes.name(r["box"]) if r["box"] else "",
                "doc_date": doc_date(r["mtime"]) if r["mtime"] else None,
                "n_clauses": int(r["n_clauses"]),
            })
        return {"documents": docs, "faq": s.faq.public_list()}

    @app.get("/api/wiki/export", tags=["Документы"], summary="Выгрузка хранилища для Confluence",
             description="Zip: регламенты по пунктам с якорями и разъяснения службы качества в формате хранения "
                         "Confluence (storage/*.xml) и обычным HTML (html/*.html), manifest.json с иерархией страниц. "
                         "Загрузка в пространство: scripts/confluence_upload.py, инструкция в docs/confluence.md.")
    def wiki_export(request: Request):
        from fastapi.responses import Response

        from app import confluence

        s: Services = svc(request)
        body = confluence.build_zip(s.db, s.boxes)
        return Response(body, media_type="application/zip",
                        headers={"Content-Disposition": 'attachment; filename="gde-napisano-confluence.zip"'})

    @app.get("/api/gaps", tags=["Документы"], summary="Отчёт «Где не написано?»",
             description="Пробелы в регламентах: вопросы без ответа, найденные противоречия, пункты, "
                         "из-за которых чаще всего создают заявки, и ссылки на документы, которых нет в базе. "
                         "Токен не нужен, в отчёте нет почты и имён сотрудников. format=md или format=csv — скачать отчёт.")
    def gaps_report(request: Request, format: str | None = Query(default=None, pattern="^(md|csv)?$")):
        from app import gaps as gaps_mod

        data = gaps_mod.build(svc(request).db)
        if format == "md":
            body = gaps_mod.to_markdown(data)
            return PlainTextResponse(body, media_type="text/markdown; charset=utf-8",
                                     headers={"Content-Disposition": 'attachment; filename="gde-ne-napisano.md"'})
        if format == "csv":
            body = gaps_mod.to_csv(data)
            return PlainTextResponse(body, media_type="text/csv; charset=utf-8",
                                     headers={"Content-Disposition": 'attachment; filename="gde-ne-napisano.csv"'})
        return data

    @app.get("/api/documents/{doc_id}/clauses", tags=["Документы"], summary="Документ целиком по пунктам",
             description="Все пункты документа по порядку: для перехода к месту в документе с подсветкой.",
             responses=error_responses(404))
    def document_clauses(doc_id: str, request: Request):
        s: Services = svc(request)
        f = s.db.query("SELECT doc_id, title, box, mtime FROM files WHERE doc_id=?", (doc_id,))
        if not f:
            raise HTTPException(404, "Документ не найден")
        rows = s.db.query("SELECT point_id, clause_no, label, section, page, kind, text FROM clauses WHERE doc_id=? "
                          "ORDER BY ord", (doc_id,))
        from app.answer import doc_date

        return {"doc_id": doc_id, "title": f[0]["title"], "box": f[0]["box"],
                "box_name": s.boxes.name(f[0]["box"]) if f[0]["box"] else "",
                "doc_date": doc_date(f[0]["mtime"]) if f[0]["mtime"] else None,
                "clauses": [dict(r) for r in rows]}

    def _limit(s: Services, request: Request, bucket: str, per_minute: int, message: str) -> None:
        ip = client_ip(request)
        if ip in settings.trusted_client_ip_set:
            per_minute = max(per_minute, settings.rate_limit_trusted_per_minute) if per_minute > 0 else 0
        if not s.limiter.allow(f"{bucket}:{ip}", per_minute):
            raise HTTPException(429, f"{message}, подождите минуту", headers={"Retry-After": "60"})

    def _candidate(s: Services, answer_id: str, point_id: str | None) -> tuple[object, dict | None]:
        ans = s.memory.get_answer(answer_id)
        if ans is None:
            raise HTTPException(404, "Ответ не найден")
        cands = json.loads(ans["candidates"] or "[]")
        if point_id is None:
            return ans, (cands[0] if cands and ans["mode"] == "confident" else None)
        for c in cands:
            if c["point_id"] == point_id:
                return ans, c
        raise HTTPException(400, "Этот пункт не показывался в ответе")

    @app.post("/api/feedback", response_model=FeedbackOut, tags=["Отзывы"], summary="Оценить ответ",
              description="Сохраняет оценку «да/нет». Оценивать можно только пункт, который был показан в этом ответе.",
              responses=error_responses(400, 404, 422))
    def feedback(body: FeedbackIn, request: Request):
        s: Services = svc(request)
        _limit(s, request, "feedback", settings.feedback_rate_limit_per_minute, "Слишком много оценок подряд")
        ans, c = _candidate(s, body.answer_id, body.point_id)
        who = client_key(request)
        if c is None:
            s.memory.add_feedback(body.answer_id, body.helpful, client=who)
        else:
            s.memory.add_feedback(body.answer_id, body.helpful, c["doc_id"], c["doc_title"], c["clause_no"], client=who)
        return {"ok": True, "ask_practice": not body.helpful}

    @app.post("/api/practice", response_model=PracticeCreatedOut, tags=["Отзывы"],
              summary="Сохранить кейс практики",
              description="Как вопрос решили на практике. Привязывается к пункту из ответа, если он указан.",
              responses=error_responses(400, 404, 422))
    def practice(body: PracticeIn, request: Request):
        s: Services = svc(request)
        _limit(s, request, "practice", settings.practice_rate_limit_per_minute, "Слишком много записей подряд")
        if s.answers.profanity.check(body.text):
            raise HTTPException(400, "Текст содержит нецензурную лексику. Опишите решение без грубых выражений.")
        ans, c = _candidate(s, body.answer_id, body.point_id)
        box = (c or {}).get("box") or (ans["boxes"].split(",")[0] if ans["boxes"] else "")
        pid = s.memory.add_practice(body.answer_id, body.text, (c or {}).get("doc_id", ""),
                                    (c or {}).get("doc_title", ""), (c or {}).get("clause_no", ""), box)
        return {"ok": True, "id": pid}

    @app.get("/api/clauses/{point_id}", response_model=ClauseOut, tags=["Документы"],
             summary="Пункт документа",
             description="Текст пункта, соседние пункты того же документа и связанные кейсы практики.",
             responses=error_responses(404))
    def clause(point_id: str, request: Request):
        s: Services = svc(request)
        rows = s.db.query(
            "SELECT c.*, f.title, f.box FROM clauses c JOIN files f ON f.doc_id=c.doc_id WHERE c.point_id=?",
            (point_id,))
        if not rows:
            raise HTTPException(404, "Пункт не найден (возможно, документ обновили)")
        r = rows[0]
        near = s.db.query("SELECT point_id, clause_no, label, text, ord FROM clauses WHERE doc_id=? AND ord BETWEEN ? AND ?"
                          " ORDER BY ord", (r["doc_id"], r["ord"] - 2, r["ord"] + 2))
        from app.parsing import format_ref

        return {"point_id": point_id, "doc_title": r["title"], "box": r["box"], "clause_no": r["clause_no"],
                "label": r["label"] or "",
                "section": r["section"], "page": r["page"], "text": r["text"],
                "ref": format_ref(r["title"], r["label"] or r["clause_no"], r["page"], r["kind"]),
                "context": [dict(x) for x in near],
                "practice": s.memory.practice_for(r["doc_id"], r["clause_no"], 5)}

    # --- админка -------------------------------------------------------------------
    admin = [Depends(require_admin)]

    @app.get("/api/admin/documents", response_model=list[DocumentOut], tags=["Документы", "Админ"],
             summary="Список документов", description="Все проиндексированные файлы и ошибки разбора.",
             dependencies=admin, responses=ADMIN_ERRORS)
    def documents(request: Request):
        s: Services = svc(request)
        return [dict(r) for r in s.db.query(
            "SELECT doc_id, rel_path, title, box, n_clauses, status, error, indexed_at, size FROM files ORDER BY rel_path")]

    @app.post("/api/admin/upload", response_model=UploadOut, tags=["Документы", "Админ"],
              summary="Загрузить документ",
              description="Принимает один файл (multipart: поле `file` и необязательное `box`). "
                          f"Размер не больше {settings.max_upload_mb} МБ. "
                          "Допустимые расширения задаются `ALLOWED_UPLOAD_EXTENSIONS` "
                          f"(сейчас: {', '.join(sorted(settings.allowed_upload_ext_set))}). "
                          "Имя очищается, путь обязан остаться внутри папки документов. После записи файл индексируется.",
              dependencies=admin, responses=ADMIN_DOC_ERRORS)
    async def upload(request: Request, file: UploadFile = File(..., description="Файл документа"),
                     box: str = Form(GENERAL_BOX, description="Ящичек, в который положить файл", examples=[GENERAL_BOX]),
                     replace: bool = Form(False, description="Заменить файл с тем же именем (старая редакция "
                                                             "уйдёт из индекса)")):
        s: Services = svc(request)
        _limit(s, request, "upload", settings.upload_rate_limit_per_minute, "Слишком много загрузок подряд")
        try:
            name = safe_filename(file.filename or "")
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        ext = Path(name).suffix.lower()
        if ext not in settings.allowed_upload_ext_set:
            raise HTTPException(400, f"Формат {ext} не допускается настройками")
        if box != GENERAL_BOX and box not in s.boxes.boxes:
            raise HTTPException(400, "Неизвестный ящичек")
        try:
            target = safe_join(s.indexer.docs_dir, box, name)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        previous = None
        if target.exists():
            old = s.db.query("SELECT indexed_at, mtime FROM files WHERE rel_path=?", (f"{box}/{name}",))
            from app.answer import doc_date

            previous = doc_date(old[0]["mtime"]) if old and old[0]["mtime"] else doc_date(target.stat().st_mtime)
            if not replace:
                raise HTTPException(409, f"Файл {box}/{name} уже загружен (редакция от {previous}). "
                                         "Чтобы заменить его новой редакцией, отправьте replace=true")
        limit = settings.max_upload_mb * 1024 * 1024
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with tempfile.NamedTemporaryFile(delete=False, dir=target.parent, prefix=".upload_") as tmp:
            try:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > limit:
                        raise HTTPException(413, f"Файл больше {settings.max_upload_mb} МБ")
                    tmp.write(chunk)
            except BaseException:
                tmp.close()
                Path(tmp.name).unlink(missing_ok=True)
                raise
        shutil.move(tmp.name, target)
        stats = await run_in_threadpool(s.indexer.index_path, target)
        if previous:
            log.warning("документ %s/%s заменён новой редакцией (прежняя от %s)", box, name, previous)
        return {"ok": not stats.errors, "file": f"{box}/{name}", "stats": stats.as_dict(),
                "replaced": bool(previous), "previous_date": previous,
                "warning": (f"Заменена прежняя редакция от {previous}. Ответы теперь строятся по новой." if previous
                            else None)}

    @app.delete("/api/admin/documents/{doc_id}", response_model=DeleteOut, tags=["Документы", "Админ"],
                summary="Удалить документ",
                description="Удаляет файл с диска и убирает его пункты из индекса.",
                dependencies=admin, responses=error_responses(400, 401, 403, 404))
    def delete_document(doc_id: str, request: Request):
        s: Services = svc(request)
        rows = s.db.query("SELECT rel_path FROM files WHERE doc_id=?", (doc_id,))
        if not rows:
            raise HTTPException(404, "Документ не найден")
        try:
            safe_join(s.indexer.docs_dir, rows[0]["rel_path"]).unlink(missing_ok=True)
        except ValueError as e:
            raise HTTPException(400, str(e)) from e
        return {"ok": True, "stats": s.indexer.remove_doc(doc_id).as_dict()}

    @app.post("/api/admin/reindex", response_model=IndexStatsOut, tags=["Документы", "Админ"],
              summary="Переиндексировать документы",
              description="Проходит папку документов: новые и изменённые файлы индексируются, удалённые убираются. "
                          "Повторный запуск, пока идёт индексация, возвращает 409.",
              dependencies=admin, responses=error_responses(401, 403, 409))
    async def reindex(request: Request):
        if request.app.state.index_status["running"]:
            raise HTTPException(409, "Индексация уже идёт")
        return await run_in_threadpool(_sync, request.app)

    @app.get("/api/admin/stats", response_model=StatsOut, tags=["Админ"], summary="Сводка по индексу и ответам",
             description="Число документов и пунктов, режимы ответов, токены, отзывы и состояние индексации.",
             dependencies=admin, responses=ADMIN_ERRORS)
    def stats(request: Request):
        s: Services = svc(request)
        q = s.db.query
        return {
            "documents": q("SELECT COUNT(*) n FROM files")[0]["n"],
            "clauses": q("SELECT COUNT(*) n FROM clauses")[0]["n"],
            "vectors": s.store.count(),
            "answers_by_mode": {r["mode"]: r["n"] for r in q("SELECT mode, COUNT(*) n FROM answers GROUP BY mode")},
            "tokens": dict(q("SELECT COALESCE(SUM(prompt_tokens),0) prompt, COALESCE(SUM(completion_tokens),0) "
                             "completion, COUNT(*) answers, COALESCE(AVG(latency_ms),0) avg_latency_ms FROM answers")[0]),
            "feedback": {("yes" if r["helpful"] else "no"): r["n"]
                         for r in q("SELECT helpful, COUNT(*) n FROM feedback GROUP BY helpful")},
            "index": request.app.state.index_status,
        }

    @app.get("/api/operator/inbox", response_model=InboxOut, tags=["Отзывы", "Админ"],
             summary="Очередь оператора",
             description="Вопросы без ответа в документах, отрицательные оценки и кейсы практики.",
             dependencies=admin, responses=ADMIN_ERRORS)
    def operator_inbox(request: Request, limit: int = Query(100, ge=1, le=500,
                                                            description="Максимум записей в каждой категории")):
        s: Services = svc(request)
        q = s.db.query
        return {
            "not_found": [dict(r) for r in q("SELECT answer_id, created_at, question, boxes FROM answers "
                                             "WHERE mode='not_found' ORDER BY created_at DESC LIMIT ?", (limit,))],
            "negative_feedback": [dict(r) for r in q(
                "SELECT created_at, question, doc_title, clause_no FROM feedback WHERE helpful=0 "
                "ORDER BY id DESC LIMIT ?", (limit,))],
            "practice": [dict(r) for r in q("SELECT id, created_at, question, doc_title, clause_no, box, text, status "
                                            "FROM practice ORDER BY id DESC LIMIT ?", (limit,))],
        }

    @app.post("/api/operator/practice/{pid}", response_model=OkOut, tags=["Отзывы", "Админ"],
              summary="Сменить статус практики",
              description="new — новый, approved — одобрен и виден в ответах, hidden — скрыт.",
              dependencies=admin, responses=error_responses(401, 403, 404, 422))
    def practice_status(pid: int, body: PracticeStatusIn, request: Request):
        s: Services = svc(request)
        cur = s.db.execute("UPDATE practice SET status=? WHERE id=?", (body.status, pid))
        if cur.rowcount == 0:
            raise HTTPException(404, "Запись не найдена")
        return {"ok": True}

    # --- служба качества: эскалации, ответы экспертов, разъяснения, аналитика ---------
    @app.get("/api/operator/escalations", tags=["Служба качества", "Админ"], summary="Запросы в службу качества",
             dependencies=admin, responses=ADMIN_ERRORS)
    def escalations_list(request: Request, limit: int = Query(100, ge=1, le=500)):
        return svc(request).escalations.list(limit)

    @app.post("/api/operator/escalations/{esc_id}/answer", tags=["Служба качества", "Админ"],
              summary="Ответ эксперта (имитация письма)",
              description="Финальный ответ попадает в базу разъяснений и используется для таких же вопросов. "
                          "Ответ со встречным вопросом (или final=false) только сохраняется.",
              dependencies=admin, responses=error_responses(400, 401, 403, 404, 422))
    def escalation_answer(esc_id: int, body: ExpertAnswerIn, request: Request):
        from app.escalation import EscalationError

        try:
            return svc(request).escalations.answer(esc_id, body.text, expert=body.expert, final=body.final)
        except KeyError:
            raise HTTPException(404, "Запрос не найден") from None
        except EscalationError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/operator/escalations/import-eml", tags=["Служба качества", "Админ"],
              summary="Импорт письма-ответа эксперта (.eml)", dependencies=admin,
              responses=error_responses(400, 401, 403, 404, 413))
    async def escalation_import(request: Request, file: UploadFile = File(..., description="Письмо .eml")):
        from app.escalation import EscalationError

        raw = await file.read(2 * 1024 * 1024 + 1)
        if len(raw) > 2 * 1024 * 1024:
            raise HTTPException(413, "Письмо больше 2 МБ")
        try:
            return svc(request).escalations.import_eml(raw)
        except KeyError:
            raise HTTPException(404, "Запрос из письма не найден") from None
        except EscalationError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/api/operator/mail/poll", tags=["Служба качества", "Админ"], summary="Проверить почту (IMAP)",
              dependencies=admin, responses=error_responses(400, 401, 403))
    def mail_poll(request: Request):
        from app.escalation import EscalationError

        try:
            return svc(request).escalations.poll_imap()
        except EscalationError as e:
            raise HTTPException(400, str(e)) from None
        except OSError:
            raise HTTPException(400, "Почтовый сервер недоступен") from None

    @app.get("/api/operator/outbox/{name}", tags=["Служба качества", "Админ"], summary="Письмо из исходящих (.eml)",
             dependencies=admin, responses=error_responses(401, 403, 404))
    def outbox_file(name: str, request: Request):
        if not re.fullmatch(r"escalation-\d{5}-\d+\.eml", name):
            raise HTTPException(404, "Письмо не найдено")
        path = settings.outbox_dir / name
        if not path.is_file():
            raise HTTPException(404, "Письмо не найдено")
        return FileResponse(path, media_type="message/rfc822", filename=name)

    @app.get("/api/operator/mail", tags=["Служба качества", "Админ"], summary="Демо-ящик: список писем",
             description="Письма службе качества из outbox: тема, заявка ГН-N, дата, прочитано ли. "
                         "Наружу письма не отправляются.", dependencies=admin, responses=ADMIN_ERRORS)
    def mail_list(request: Request):
        from app import mailbox

        return mailbox.list_mail(svc(request).db, settings)

    @app.get("/api/operator/mail/{esc_id}", tags=["Служба качества", "Админ"], summary="Демо-ящик: письмо",
             description="Текст письма, вопрос, найденные пункты со ссылками. Открытое письмо отмечается прочитанным.",
             dependencies=admin, responses=error_responses(401, 403, 404))
    def mail_get(esc_id: int, request: Request):
        from app import mailbox

        m = mailbox.get_mail(svc(request).db, settings, esc_id)
        if m is None:
            raise HTTPException(404, "Письмо не найдено")
        return m

    @app.post("/api/operator/mail/{esc_id}/read", response_model=OkOut, tags=["Служба качества", "Админ"],
              summary="Демо-ящик: отметить прочитанным или непрочитанным", dependencies=admin,
              responses=error_responses(401, 403, 404))
    def mail_read(esc_id: int, request: Request, read: bool = Query(True)):
        from app import mailbox

        s = svc(request)
        if not s.db.query("SELECT 1 FROM escalations WHERE id=?", (esc_id,)):
            raise HTTPException(404, "Письмо не найдено")
        mailbox.set_read(s.db, esc_id, read)
        return {"ok": True}

    @app.get("/api/operator/faq", tags=["Служба качества", "Админ"], summary="База разъяснений",
             dependencies=admin, responses=ADMIN_ERRORS)
    def faq_list(request: Request):
        return svc(request).faq.list()

    @app.get("/api/admin/analytics", tags=["Служба качества", "Админ"], summary="Аналитика",
             description="Неудачные поиски, эскалации, повторы, популярные документы и пункты, время ответа, "
                         "стоимость запроса.", dependencies=admin, responses=ADMIN_ERRORS)
    def analytics(request: Request):
        from app import analytics as an

        return an.build(svc(request).db, settings)

    return app


def app_factory() -> FastAPI:
    return create_app()
