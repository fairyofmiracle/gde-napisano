"""События хода ответа для потоковой выдачи: найденные пункты, текущий шаг, черновик текста.

Обработчик ставится на время одного вопроса (contextvar), поэтому обычный непотоковый ответ,
проверки и бот через /api/chat работают как раньше: без обработчика события просто не отправляются."""
from __future__ import annotations

import contextvars
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager

log = logging.getLogger(__name__)

_sink: contextvars.ContextVar[Callable[[dict], None] | None] = contextvars.ContextVar("progress_sink", default=None)
_scope: contextvars.ContextVar[str] = contextvars.ContextVar("progress_scope", default="")


def active() -> bool:
    return _sink.get() is not None


def emit(kind: str, **data) -> None:
    sink = _sink.get()
    if sink is None:
        return
    event = {"type": kind, **data}
    scope = _scope.get()
    if scope and "scope" not in event:
        event["scope"] = scope
    try:
        sink(event)
    except Exception:  # поток не должен ломать сам ответ
        log.debug("событие не отправлено", exc_info=True)


@contextmanager
def listening(sink: Callable[[dict], None]):
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


@contextmanager
def scoped(name: str):
    """Подпись событий (например, подразделение), чтобы интерфейс показывал их в своём блоке."""
    token = _scope.set(name)
    try:
        yield
    finally:
        _scope.reset(token)


def status(text: str, step: str = "") -> None:
    emit("status", text=text, step=step)


class AnswerDraft:
    """Достаёт значение поля "answer" из JSON, который модель ещё пишет, и отдаёт черновик не чаще interval."""

    _START = re.compile(r'"answer"\s*:\s*"')

    def __init__(self, interval: float = 0.25, clock=time.monotonic) -> None:
        self.buf = ""
        self.text = ""
        self.sent = ""
        self.interval = interval
        self._clock = clock
        self._last = float("-inf")  # первый кусок текста уходит сразу
        self._lock = threading.Lock()

    def feed(self, delta: str) -> None:
        with self._lock:
            self.buf += delta or ""
            text = self.extract(self.buf)
            if text is None or text == self.text:
                return
            self.text = text
            now = self._clock()
            if now - self._last < self.interval:
                return
            self._last = now
            self.sent = text
        emit("draft", text=text)

    def flush(self) -> None:
        if self.text and self.text != self.sent:
            self.sent = self.text
            emit("draft", text=self.text)

    @classmethod
    def extract(cls, buf: str) -> str | None:
        m = cls._START.search(buf)
        if not m:
            return None
        out, i, n = [], m.end(), len(buf)
        while i < n:
            ch = buf[i]
            if ch == '"':
                break
            if ch == "\\":
                if i + 1 >= n:
                    break
                nxt = buf[i + 1]
                if nxt == "u":
                    if i + 6 > n:
                        break
                    try:
                        out.append(chr(int(buf[i + 2:i + 6], 16)))
                    except ValueError:
                        pass
                    i += 6
                    continue
                out.append({"n": "\n", "t": " ", "r": "", '"': '"', "\\": "\\", "/": "/"}.get(nxt, nxt))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)


def ndjson(event: dict) -> bytes:
    return (json.dumps(event, ensure_ascii=False, default=str) + "\n").encode("utf-8")
