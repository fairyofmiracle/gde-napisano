"""Доступ по токену, очистка имён загружаемых файлов, ограничение частоты запросов."""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import re
import threading
import time
import unicodedata
from collections import defaultdict, deque
from pathlib import Path

from fastapi import HTTPException, Request

from app.parsing import SUPPORTED_EXTENSIONS

log = logging.getLogger(__name__)

_BAD_CHARS = re.compile(r"[^\w\-. ()\[\]№«»,]+", re.UNICODE)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(name: str) -> str:
    """Только имя файла, без каталогов и опасных символов. Бросает ValueError, если имя непригодно."""
    name = unicodedata.normalize("NFC", name or "")
    name = name.replace("\\", "/").split("/")[-1]  # отрезаем любые каталоги, в том числе ../ и C:\
    name = name.replace("\x00", "")
    name = _BAD_CHARS.sub("_", name).strip(" .")
    name = re.sub(r"_+", "_", name)
    if not name or name in (".", ".."):
        raise ValueError("Пустое имя файла")
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        raise ValueError("У файла нет расширения")
    ext = "." + ext.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise ValueError(f"Формат {ext} не поддерживается")
    if stem.lower() in _RESERVED:
        stem = f"_{stem}"
    stem = stem[:150]
    return f"{stem}{ext}"


def safe_join(base: Path, *parts: str) -> Path:
    """Путь внутри base; всё, что выходит за его пределы, — ошибка."""
    base = base.resolve()
    target = base.joinpath(*parts).resolve()
    if not target.is_relative_to(base):
        raise ValueError("Путь за пределами папки документов")
    return target


def client_ip(request: Request) -> str:
    """Адрес клиента. За обратным прокси uvicorn подставляет его из X-Forwarded-For,
    но только если запрос пришёл с адреса из FORWARDED_ALLOW_IPS (см. Dockerfile и docker-compose.yml)."""
    return request.client.host if request.client else "?"


def client_key(request: Request) -> str:
    """Обезличенный идентификатор клиента для хранения рядом с отзывами (сам адрес не сохраняется)."""
    return hashlib.sha256(f"docagent:{client_ip(request)}".encode()).hexdigest()[:16]


def ip_allowed(ip: str, networks: list) -> bool:
    if not networks:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in networks)


class AdminGuard:
    """Защита от подбора токена: после N неверных попыток адрес блокируется,
    каждая следующая блокировка вдвое длиннее (до часа). Счётчик в памяти процесса."""

    MAX_LOCK = 3600

    def __init__(self, max_failures: int = 5, lockout_seconds: int = 300, clock=time.monotonic) -> None:
        self.max_failures = max(1, max_failures)
        self.lockout_seconds = max(1, lockout_seconds)
        self._clock = clock
        self._fails: dict[str, int] = defaultdict(int)
        self._locks: dict[str, tuple[float, int]] = {}  # ip -> (до какого времени, сколько раз блокировали)
        self._lock = threading.Lock()

    def locked_for(self, ip: str) -> int:
        with self._lock:
            until, _ = self._locks.get(ip, (0.0, 0))
            left = until - self._clock()
            return int(left) + 1 if left > 0 else 0

    def failure(self, ip: str) -> int:
        """Отмечает неудачу. Возвращает длительность новой блокировки (0 — пока без блокировки)."""
        with self._lock:
            self._fails[ip] += 1
            if self._fails[ip] < self.max_failures:
                return 0
            self._fails[ip] = 0
            _, times = self._locks.get(ip, (0.0, 0))
            seconds = min(self.MAX_LOCK, self.lockout_seconds * (2 ** times))
            self._locks[ip] = (self._clock() + seconds, times + 1)
            return seconds

    def success(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


def require_admin(request: Request) -> None:
    """Токен из Authorization: Bearer … или из X-Admin-Token. Схемы для Swagger задаются в OpenAPI.
    Порядок: список разрешённых адресов → блокировка после неудач → лимит частоты → сравнение токена."""
    settings = request.app.state.settings
    token = settings.admin_token
    if not token:
        raise HTTPException(403, "Админка закрыта: не задан ADMIN_TOKEN")
    if len(token) < settings.admin_token_min_length:
        log.error("ADMIN_TOKEN короче %d символов, админка закрыта", settings.admin_token_min_length)
        raise HTTPException(403, f"Админка закрыта: ADMIN_TOKEN короче {settings.admin_token_min_length} символов")
    ip = client_ip(request)
    if not ip_allowed(ip, settings.admin_allowed_networks):
        raise HTTPException(403, "Админка недоступна с этого адреса")
    guard: AdminGuard | None = getattr(request.app.state, "admin_guard", None)
    if guard is not None:
        left = guard.locked_for(ip)
        if left:
            raise HTTPException(429, "Слишком много неверных попыток входа. Попробуйте позже",
                                headers={"Retry-After": str(left)})
    services = getattr(request.app.state, "services", None)
    limiter = getattr(services, "limiter", None)
    if limiter is not None and not limiter.allow(f"admin:{ip}", settings.admin_rate_limit_per_minute):
        raise HTTPException(429, "Слишком много запросов к админке, подождите минуту", headers={"Retry-After": "60"})
    supplied = request.headers.get("x-admin-token") or ""
    auth = request.headers.get("authorization") or ""
    if not supplied and auth.lower().startswith("bearer "):
        supplied = auth[7:].strip()
    # сравнение за постоянное время; длина токена из сравнения не утекает
    ok = bool(supplied) and hmac.compare_digest(hashlib.sha256(supplied.encode()).digest(),
                                                hashlib.sha256(token.encode()).digest())
    if not ok:
        if guard is not None and supplied:  # пустой токен — не попытка подбора
            locked = guard.failure(ip)
            if locked:
                log.warning("неверный токен администратора: адрес заблокирован на %d с", locked)
        raise HTTPException(401, "Нужен действующий токен администратора")
    if guard is not None:
        guard.success(ip)


class RateLimiter:
    """Не больше N запросов в минуту с одного адреса. Redis, если задан REDIS_URL, иначе память процесса."""

    def __init__(self, per_minute: int, redis_url: str = "") -> None:
        self.per_minute = per_minute
        self._redis = None
        if redis_url:
            import redis

            self._redis = redis.Redis.from_url(redis_url, socket_timeout=1)
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, per_minute: int | None = None) -> bool:
        """per_minute — свой лимит для этой группы ключей (например, «feedback:адрес»); None — общий."""
        limit = self.per_minute if per_minute is None else per_minute
        if limit <= 0:
            return True
        if self._redis is not None:
            try:
                k = f"rl:{key}:{int(time.time() // 60)}"
                n = self._redis.incr(k)
                if n == 1:
                    self._redis.expire(k, 70)
                return n <= limit
            except Exception:
                pass  # Redis недоступен — считаем в памяти
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 10000:  # не копим пустые очереди от разовых адресов
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] > 60]:
                    del self._hits[k]
            q = self._hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def redis_ok(self) -> bool | None:
        if self._redis is None:
            return None
        try:
            return bool(self._redis.ping())
        except Exception:
            return False
