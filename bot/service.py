"""Логика бота без привязки к библиотеке Telegram: проверки, распознавание голоса, вопрос к docagent."""
from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

from bot.api import ApiError, DocagentClient, RateLimited
from bot.audio import AudioError, convert_to_wav
from bot.formatting import format_answer, split_message
from bot.limiter import UserRateLimiter
from bot.settings import BotSettings
from bot.transcribe import Transcriber

log = logging.getLogger(__name__)

Reply = Callable[[str], Awaitable[None]]
Download = Callable[[Path], Awaitable[None]]

HELP = ("Я бот «Где написано?». Задайте вопрос по регламентам текстом или голосовым сообщением. Отвечу по "
        "загруженным документам и покажу, из какого пункта взят ответ.")
MSG_BUSY = "Слишком много вопросов подряд, подождите минуту."
MSG_DENIED = ("Этот бот только для сотрудников из списка доступа. "
              "Если вам нужен доступ — напишите администратору.")
MSG_APP_DOWN = "Сервис поиска сейчас недоступен. Попробуйте позже."
MSG_SHORT = "Вопрос слишком короткий. Напишите хотя бы пару слов."
MSG_LONG = "Вопрос слишком длинный, максимум 2000 символов."
MSG_VOICE_BUSY = "Сейчас распознаётся много голосовых. Подождите минуту или напишите вопрос текстом."


class BotService:
    def __init__(self, settings: BotSettings, api: DocagentClient, transcriber: Transcriber | None,
                 limiter: UserRateLimiter | None = None) -> None:
        self.settings = settings
        self.api = api
        self.transcriber = transcriber
        self.limiter = limiter or UserRateLimiter(settings.rate_limit_per_minute)
        self.voice_limiter = UserRateLimiter(settings.voice_per_minute)
        self._stt_lock = asyncio.Lock()  # распознавание по одному: процессор общий с поиском
        self._voice_pending = 0
        self.last_used = time.monotonic()

    def welcome(self) -> str:
        text = HELP
        if not self.transcriber:
            text += " Голосовые сообщения отключены."
        url = (self.settings.web_public_url or "").strip().rstrip("/")
        if url:
            text += f"\n\nТот же поиск в браузере: {url}"
        return text

    def allowed(self, user_id: int) -> bool:
        allow = self.settings.allowed_users
        if allow:
            return user_id in allow
        # Пустой список: либо открытый демо (TELEGRAM_OPEN=true), либо никому
        return bool(self.settings.telegram_open)

    def chat_allowed(self, chat_type: str, chat_id: int, from_bot: bool = False) -> bool:
        """Личные сообщения от людей — да; группы и каналы — только из TELEGRAM_ALLOWED_CHATS; другие боты — нет."""
        if from_bot:
            return False
        if chat_type == "private":
            return True
        return chat_id in self.settings.allowed_chats

    async def handle_text(self, user_id: int, text: str, reply: Reply) -> None:
        if not self.allowed(user_id):
            return await reply(MSG_DENIED)
        if not self.limiter.allow(user_id):
            return await reply(MSG_BUSY)
        await self._answer(text, reply)

    async def handle_voice(self, user_id: int, duration: int | None, file_size: int | None,
                           download: Download, reply: Reply) -> None:
        s = self.settings
        if not self.allowed(user_id):
            return await reply(MSG_DENIED)
        if self.transcriber is None:
            return await reply("Голосовые сообщения отключены. Напишите вопрос текстом.")
        # Дешёвые проверки до скачивания файла
        if duration is not None and duration > s.voice_max_seconds:
            return await reply(f"Голосовое длиннее {s.voice_max_seconds} с. Запишите покороче или напишите текстом.")
        if file_size is not None and file_size > s.max_file_bytes:
            return await reply(f"Файл больше {s.max_file_bytes // (1024 * 1024)} МБ.")
        if not self.voice_limiter.allow(user_id):
            return await reply(f"Не больше {s.voice_per_minute} голосовых в минуту. Подождите или напишите текстом.")
        if not self.limiter.allow(user_id):
            return await reply(MSG_BUSY)
        if self._voice_pending >= max(1, s.voice_queue_max):
            return await reply(MSG_VOICE_BUSY)
        self._voice_pending += 1
        try:
            await self._handle_voice(download, reply)
        finally:
            self._voice_pending -= 1

    async def _handle_voice(self, download: Download, reply: Reply) -> None:
        s = self.settings
        with tempfile.TemporaryDirectory(prefix="voice_") as tmp:
            src, wav = Path(tmp) / "in.bin", Path(tmp) / "out.wav"
            try:
                await download(src)
                if src.stat().st_size > s.max_file_bytes:
                    return await reply(f"Файл больше {s.max_file_bytes // (1024 * 1024)} МБ.")
                real = await convert_to_wav(src, wav, max_seconds=s.voice_max_seconds, ffmpeg=s.ffmpeg_cmd)
            except AudioError as e:
                log.warning("аудио не обработано: %s", e)
                return await reply("Не получилось прочитать аудио. Отправьте голосовое ещё раз или напишите текстом.")
            except OSError as e:
                log.warning("файл не скачан: %s", e)
                return await reply("Не получилось скачать голосовое. Попробуйте ещё раз.")
            if real > s.voice_max_seconds:
                return await reply(f"Голосовое длиннее {s.voice_max_seconds} с. Запишите покороче или напишите текстом.")

            try:
                async with self._stt_lock:
                    text = await asyncio.to_thread(self.transcriber.transcribe, wav)
                self.last_used = time.monotonic()
            except Exception:  # модель не скачалась, не хватило памяти и т. п.
                log.exception("ошибка распознавания")
                return await reply("Распознавание речи сейчас не работает. Напишите вопрос текстом.")

        text = (text or "").strip()
        if not text:
            return await reply("Речь не разобрана. Скажите ещё раз поближе к микрофону или напишите текстом.")
        await reply(f"Распознано: «{text}»")
        await self._answer(text, reply)

    async def _answer(self, question: str, reply: Reply) -> None:
        question = question.strip()
        if len(question) < 2:
            return await reply(MSG_SHORT)
        if len(question) > 2000:
            return await reply(MSG_LONG)
        try:
            data = await self.api.ask(question)
        except RateLimited as e:
            return await reply(f"Слишком много вопросов подряд, подождите {e.retry_after} с.")
        except ApiError as e:
            log.error("docagent: %s", e)
            return await reply(MSG_APP_DOWN)
        for part in split_message(format_answer(data)):
            await reply(part)

    async def unload_if_idle(self, now: float | None = None) -> bool:
        """Выгружает модель, если голосовых давно не было. Возвращает True, если выгрузила."""
        idle = self.settings.transcriber_idle_seconds
        now = time.monotonic() if now is None else now
        t = self.transcriber
        if t is None or not t.loaded or idle <= 0 or now - self.last_used < idle or self._stt_lock.locked():
            return False
        await asyncio.to_thread(t.unload)
        return True
