"""Ограничение числа вопросов на одного пользователя Telegram (окно в минуту, счётчик в памяти)."""
from __future__ import annotations

import time
from collections import defaultdict, deque


class UserRateLimiter:
    def __init__(self, per_minute: int, clock=time.monotonic) -> None:
        self.per_minute = per_minute
        self._clock = clock
        self._hits: dict[int, deque] = defaultdict(deque)

    def allow(self, user_id: int) -> bool:
        if self.per_minute <= 0:
            return True
        now = self._clock()
        q = self._hits[user_id]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= self.per_minute:
            return False
        q.append(now)
        return True
