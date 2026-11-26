"""Запуск: python -m bot. Long polling, без открытых портов. Без TELEGRAM_BOT_TOKEN завершается сразу."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from bot.api import DocagentClient
from bot.limiter import UserRateLimiter
from bot.service import HELP, BotService
from bot.settings import BotSettings
from bot.transcribe import make_transcriber

log = logging.getLogger("bot")


def build_dispatcher(service: BotService):
    from aiogram import Bot, Dispatcher, F, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.types import Message

    router = Router()

    def from_allowed_chat(message: Message) -> bool:
        user = message.from_user
        return service.chat_allowed(message.chat.type, message.chat.id, bool(user and user.is_bot))

    # Группы, каналы и другие боты отсекаются до любых обработчиков
    router.message.filter(from_allowed_chat)

    def replier(message: Message):
        async def reply(text: str) -> None:
            await message.answer(text)
        return reply

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def start(message: Message) -> None:
        await message.answer(HELP + ("" if service.transcriber else " Голосовые сообщения отключены."))

    @router.message(F.voice | F.audio)
    async def voice(message: Message, bot: Bot) -> None:
        media = message.voice or message.audio
        user_id = message.from_user.id if message.from_user else 0

        async def download(dest) -> None:
            await bot.download(media, destination=dest)

        await service.handle_voice(user_id, media.duration, media.file_size, download, replier(message))

    @router.message(F.text & ~F.text.startswith("/"))
    async def text(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        await service.handle_text(user_id, message.text or "", replier(message))

    dp = Dispatcher()
    dp.include_router(router)
    return dp


async def _idle_watcher(service: BotService) -> None:
    while True:
        await asyncio.sleep(30)
        with contextlib.suppress(Exception):
            await service.unload_if_idle()


async def run(settings: BotSettings) -> None:
    from aiogram import Bot

    api = DocagentClient(settings.docagent_url, settings.request_timeout)
    service = BotService(settings, api, make_transcriber(settings), UserRateLimiter(settings.rate_limit_per_minute))
    if not settings.allowed_users:
        log.warning("TELEGRAM_ALLOWED_USERS не задан: бот отвечает любому пользователю Telegram")
    bot = Bot(settings.token)
    watcher = asyncio.create_task(_idle_watcher(service))
    try:
        log.info("бот запущен, распознавание: %s", settings.transcriber)
        await build_dispatcher(service).start_polling(bot, allowed_updates=["message"])
    finally:
        watcher.cancel()
        await api.close()
        await bot.session.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = BotSettings()
    if not settings.token:
        log.warning("TELEGRAM_BOT_TOKEN не задан, бот не запущен")
        return 0
    asyncio.run(run(settings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
