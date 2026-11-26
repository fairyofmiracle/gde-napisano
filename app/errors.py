"""Единый формат ошибок, идентификатор запроса и структурные логи с временем ответа."""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas import ErrorResponse

log = logging.getLogger("docagent")

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_STATUS_CODE = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
}

_SAFE_ID = re.compile(r"[^\w.\-:]")
_factory_installed = False


def install_request_id_log_factory() -> None:
    """Добавляет request_id в каждую запись лога. Ставится один раз на процесс."""
    global _factory_installed
    if _factory_installed:
        return
    old = logging.getLogRecordFactory()

    def factory(*args, **kwargs):
        record = old(*args, **kwargs)
        if not hasattr(record, "request_id"):
            record.request_id = request_id_var.get("-")
        return record

    logging.setLogRecordFactory(factory)
    _factory_installed = True


def resolve_request_id(header: str | None) -> str:
    if header:
        value = _SAFE_ID.sub("", header.strip())[:128]
        if value:
            return value
    return uuid.uuid4().hex


def current_request_id(request: Request | None = None) -> str:
    if request is not None:
        rid = getattr(request.state, "request_id", None)
        if rid:
            return rid
    return request_id_var.get("-")


def error_body(status: int, message: str, request_id: str, details: list | None = None) -> dict:
    code = _STATUS_CODE.get(status, f"http_{status}")
    return ErrorResponse(
        error={"code": code, "message": message, "details": details},
        detail=message,
        request_id=request_id,
    ).model_dump()


def _message_from_detail(detail: object) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        return "Некорректные данные запроса"
    if isinstance(detail, dict):
        return str(detail.get("message") or detail.get("detail") or "Ошибка запроса")
    return "Ошибка запроса"


def _json(status: int, body: dict, request_id: str, extra_headers: dict | None = None) -> JSONResponse:
    headers = {"X-Request-ID": request_id}
    if extra_headers:
        headers.update(extra_headers)
    return JSONResponse(status_code=status, content=body, headers=headers)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    request_id = current_request_id(request)
    body = error_body(exc.status_code, _message_from_detail(exc.detail), request_id)
    return _json(exc.status_code, body, request_id, dict(exc.headers or {}))


async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    request_id = current_request_id(request)
    details = []
    for err in exc.errors():
        details.append({"loc": list(err.get("loc", [])), "msg": err.get("msg", ""), "type": err.get("type", "")})
    body = error_body(422, "Некорректные данные запроса", request_id, details)
    return _json(422, body, request_id)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = current_request_id(request)
    log.exception("необработанная ошибка request_id=%s: %s", request_id, exc)
    body = error_body(500, "Внутренняя ошибка сервера", request_id)
    return _json(500, body, request_id)


def install_exception_handlers(app) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)


def install_request_middleware(app) -> None:
    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = resolve_request_id(request.headers.get("x-request-id"))
        request.state.request_id = request_id
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["X-Request-ID"] = request_id
            return response
        except Exception:
            status = 500
            raise
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            client = request.client.host if request.client else "-"
            log.info(json.dumps({
                "event": "request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": status,
                "duration_ms": duration_ms,
                "client": client,
            }, ensure_ascii=False))
            request_id_var.reset(token)
