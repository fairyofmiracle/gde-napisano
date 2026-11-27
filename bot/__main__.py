"""Запуск: python -m bot. Long polling, без открытых портов. Без TELEGRAM_BOT_TOKEN завершается сразу."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys

from bot.api import DocagentClient
from bot.limiter import UserRateLimiter
from bot.service import MSG_DENIED, BotService
from bot.settings import BotSettings
from bot.transcribe import make_transcriber

log = logging.getLogger("bot")


def build_dispatcher(service: BotService):
    from aiogram import Bot, Dispatcher, F, Router
    from aiogram.filters import Command, CommandStart
    from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

    router = Router()

    def from_allowed_chat(message: Message) -> bool:
        user = message.from_user
        return service.chat_allowed(message.chat.type, message.chat.id, bool(user and user.is_bot))

    # Группы, каналы и другие боты отсекаются до любых обработчиков
    router.message.filter(from_allowed_chat)

    def replier(message: Message):
        async def reply(text: str, buttons: list[tuple[str, str]] | None = None) -> None:
            markup = None
            if buttons:
                markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)]
                                                               for t, d in buttons])
            await message.answer(text, reply_markup=markup)

        def _markup(buttons):
            if not buttons:
                return None
            return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=d)]
                                                         for t, d in buttons])

        async def send(text: str):
            return await message.answer(text)

        async def edit(sent, text: str, buttons=None) -> bool:
            try:
                await sent.edit_text(text, reply_markup=_markup(buttons))
                return True
            except Exception as e:  # «message is not modified», лимит правок и т. п.: итог отправится новым сообщением
                log.info("правка сообщения не удалась: %s", type(e).__name__)
                return False

        async def typing() -> None:
            await message.bot.send_chat_action(message.chat.id, "typing")

        reply.send, reply.edit, reply.typing = send, edit, typing
        return reply

    @router.callback_query(F.data.startswith("div:"))
    async def pick_division(cb: CallbackQuery) -> None:
        msg = cb.message
        user_id = cb.from_user.id if cb.from_user else 0
        await cb.answer()
        if msg is None or not service.chat_allowed(msg.chat.type, msg.chat.id, bool(cb.from_user and cb.from_user.is_bot)):
            return
        with contextlib.suppress(Exception):
            await msg.edit_reply_markup(reply_markup=None)
        await service.handle_division(user_id, (cb.data or "")[4:], replier(msg))

    @router.callback_query(F.data.startswith("simp:"))
    async def simplify_answer(cb: CallbackQuery) -> None:
        msg = cb.message
        user_id = cb.from_user.id if cb.from_user else 0
        await cb.answer()
        if msg is None or not service.chat_allowed(msg.chat.type, msg.chat.id, bool(cb.from_user and cb.from_user.is_bot)):
            return
        await service.handle_simplify(user_id, (cb.data or "")[5:], replier(msg))

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def start(message: Message) -> None:
        user_id = message.from_user.id if message.from_user else 0
        if not service.allowed(user_id):
            await message.answer(MSG_DENIED)
            return
        await message.answer(service.welcome())

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
    if not settings.allowed_users and not settings.telegram_open:
        log.warning("TELEGRAM_ALLOWED_USERS пуст и TELEGRAM_OPEN=false: бот никому не отвечает")
    elif not settings.allowed_users and settings.telegram_open:
        log.warning("TELEGRAM_OPEN=true: бот отвечает любому пользователю Telegram")
    bot = Bot(settings.token)
    watcher = asyncio.create_task(_idle_watcher(service))
    try:
        log.info("бот запущен, распознавание: %s", settings.transcriber)
        await build_dispatcher(service).start_polling(bot, allowed_updates=["message", "callback_query"])
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
