"""Настройки бота. Читаются из переменных окружения и файла .env (общего с приложением)."""
from __future__ import annotations

from typing import Literal

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class BotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      populate_by_name=True)

    # Пустой токен = бот не запускается
    telegram_bot_token: SecretStr = Field(default=SecretStr(""), repr=False)
    # Telegram id пользователей через запятую. Без списка бот никому не отвечает (см. TELEGRAM_OPEN).
    telegram_allowed_users: str = ""
    # true = пустой TELEGRAM_ALLOWED_USERS означает «всем» (только для открытого демо).
    # false (по умолчанию) = без списка вопросов не принимает — защита от случайных людей.
    telegram_open: bool = False
    # В группах бот молчит. Чтобы он отвечал в конкретной группе, перечислите её id через запятую (обычно с минусом).
    telegram_allowed_chats: str = ""
    # Публичная ссылка на веб-чат для приветствия (/start). Пример: https://docs.example.ru
    web_public_url: str = ""

    # Адрес приложения docagent (поиск и ответ делает оно, модели в бот не грузятся)
    docagent_url: str = "http://localhost:8000"
    request_timeout: float = 180.0

    # Те же ограничения, что у веб-чата: вопросов в минуту (на одного пользователя) и размер файла
    rate_limit_per_minute: int = Field(default=30, validation_alias=AliasChoices("BOT_RATE_LIMIT_PER_MINUTE",
                                                                                 "RATE_LIMIT_PER_MINUTE"))
    max_upload_mb: int = 50

    # Распознавание голоса
    transcriber: Literal["gigaam", "whisper", "off"] = "gigaam"
    voice_max_seconds: int = 120
    # Защита от потока голосовых: не больше N голосовых в минуту на пользователя
    # и не больше M голосовых в очереди на распознавание на весь бот
    voice_per_minute: int = 3
    voice_queue_max: int = 3
    gigaam_model: str = "v3_e2e_ctc"
    gigaam_dir: str = ""  # папка для весов, пусто = ~/.cache/gigaam
    whisper_model: str = "small"
    whisper_compute_type: str = "int8"
    transcriber_threads: int = 3
    transcriber_idle_seconds: int = 600  # через сколько секунд простоя выгружать модель из памяти (0 = не выгружать)
    ffmpeg_cmd: str = "ffmpeg"

    @property
    def token(self) -> str:
        return self.telegram_bot_token.get_secret_value().strip()

    @property
    def allowed_users(self) -> set[int]:
        out: set[int] = set()
        for part in self.telegram_allowed_users.split(","):
            part = part.strip()
            if part:
                out.add(int(part))
        return out

    @property
    def allowed_chats(self) -> set[int]:
        return {int(x.strip()) for x in self.telegram_allowed_chats.split(",") if x.strip()}

    @property
    def max_file_bytes(self) -> int:
        # Бот Telegram не может скачать файл больше 20 МБ
        return min(self.max_upload_mb, 20) * 1024 * 1024
