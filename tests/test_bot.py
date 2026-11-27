"""Тесты Telegram-бота. Модели не скачиваются: распознавание подменено, ffmpeg нужен только для настоящей конвертации."""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import wave
from pathlib import Path

import httpx
import numpy as np
import pytest

from bot.api import ApiError, DocagentClient, RateLimited
from bot.audio import SAMPLE_RATE, AudioError, convert_to_wav, read_wav, split_on_pauses, wav_duration, write_wav
from bot.formatting import format_answer, split_message
from bot.limiter import UserRateLimiter
from bot.service import BotService
from bot.settings import BotSettings
from bot.transcribe import GigaAMTranscriber, WhisperTranscriber, join_chunks, make_transcriber

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нет ffmpeg")

CONFIDENT = {
    "mode": "confident", "text": "Отпуск составляет 28 календарных дней.", "advice": "Подайте заявление в кадры.",
    "citations": [{"n": 1, "ref": "Положение об отпусках, п. 3.1", "quote": "Отпуск составляет 28 календарных дней."}],
    "variants": [], "owner": {"role": "инспектор", "full_name": "И. И. Иванова", "contact": "доб. 101"},
}
UNCERTAIN = {
    "mode": "uncertain", "text": "Точного ответа не нашлось.", "citations": [],
    "variants": [{"ref": "Положение о премиях, п. 2.4", "text": "Премия выплачивается ежеквартально."},
                 {"ref": "Положение о премиях, п. 2.5", "text": "Размер премии определяет приказ."}],
    "owner": {"role": "бухгалтер", "full_name": "П. П. Петрова", "contact": "доб. 202"},
}
NOT_FOUND = {"mode": "not_found", "text": "В загруженных документах ответа на этот вопрос нет.", "owner": None,
             "citations": [], "variants": []}


def make_settings(**kw) -> BotSettings:
    # telegram_open=True в тестах: без списка id бот ведёт себя как раньше (отвечает всем)
    base = dict(telegram_bot_token="123:test", transcriber="gigaam", voice_max_seconds=120,
                rate_limit_per_minute=0, telegram_open=True)
    base.update(kw)
    return BotSettings(_env_file=None, **base)


class FakeTranscriber:
    name = "fake"

    def __init__(self, text="сколько дней отпуска", fail=False):
        self.text, self.fail, self.calls, self.loaded, self.unloaded = text, fail, [], False, 0

    def transcribe(self, wav: Path) -> str:
        if self.fail:
            raise RuntimeError("нет памяти")
        self.loaded = True
        self.calls.append((wav.name, wav_duration(wav)))
        return self.text

    def unload(self) -> None:
        self.loaded = False
        self.unloaded += 1


def make_api(payload=CONFIDENT, status=200, headers=None, seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append((str(request.url), request.content))
        return httpx.Response(status, json=payload, headers=headers or {})

    return DocagentClient("http://app:8000", transport=httpx.MockTransport(handler))


def collector():
    out: list[str] = []

    async def reply(text: str, buttons=None) -> None:
        out.append(text)

    return out, reply


def tone_ogg(path: Path, seconds: float) -> None:
    """Короткий настоящий ogg/opus для проверки ffmpeg."""
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    "-c:a", "libopus", "-f", "ogg", str(path)], check=True)


def downloader(seconds: float, size_hint: int | None = None):
    async def download(dest: Path) -> None:
        tone_ogg(dest, seconds)

    return download


# ---------- настройки ----------

def test_settings_token_hidden_and_allowed_users():
    s = make_settings(telegram_allowed_users="1, 22,333")
    assert "123:test" not in repr(s) and "123:test" not in str(s.model_dump())
    assert s.token == "123:test"
    assert s.allowed_users == {1, 22, 333}
    assert make_settings(telegram_bot_token="").token == ""


def test_settings_file_limit_never_above_telegram_cap():
    assert make_settings(max_upload_mb=50).max_file_bytes == 20 * 1024 * 1024
    assert make_settings(max_upload_mb=5).max_file_bytes == 5 * 1024 * 1024


def test_rate_limit_env_aliases(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_PER_MINUTE", "7")
    assert BotSettings(_env_file=None).rate_limit_per_minute == 7
    monkeypatch.setenv("BOT_RATE_LIMIT_PER_MINUTE", "3")
    assert BotSettings(_env_file=None).rate_limit_per_minute == 3


def test_make_transcriber_choice():
    assert make_transcriber(make_settings(transcriber="off")) is None
    g = make_transcriber(make_settings(transcriber="gigaam"))
    assert isinstance(g, GigaAMTranscriber) and not g.loaded  # модель не грузится при создании
    w = make_transcriber(make_settings(transcriber="whisper"))
    assert isinstance(w, WhisperTranscriber) and not w.loaded
    with pytest.raises(ValueError):
        BotSettings(_env_file=None, transcriber="vosk")


# ---------- аудио ----------

def test_split_short_audio_is_single_chunk():
    x = np.zeros(SAMPLE_RATE * 10, dtype=np.int16)
    assert len(split_on_pauses(x)) == 1


def test_split_cuts_in_the_quietest_place_and_keeps_all_samples():
    rng = np.random.default_rng(0)
    x = (rng.normal(0, 3000, SAMPLE_RATE * 45)).astype(np.int16)
    quiet = int(18.5 * SAMPLE_RATE)
    x[quiet:quiet + SAMPLE_RATE // 2] = 0  # пауза в окне поиска первого куска (15..20 с)
    chunks = split_on_pauses(x, max_chunk_s=20, search_s=5)
    assert all(len(c) <= 20 * SAMPLE_RATE for c in chunks)
    assert sum(len(c) for c in chunks) == len(x)
    first_end = len(chunks[0])
    assert quiet <= first_end <= quiet + SAMPLE_RATE // 2
    assert np.array_equal(np.concatenate(chunks), x)


def test_wav_roundtrip(tmp_path):
    x = (np.sin(np.arange(SAMPLE_RATE) / 10) * 1000).astype(np.int16)
    write_wav(tmp_path / "a.wav", x)
    assert np.array_equal(read_wav(tmp_path / "a.wav"), x)
    assert wav_duration(tmp_path / "a.wav") == pytest.approx(1.0)


@needs_ffmpeg
def test_convert_to_wav_16k_mono(tmp_path):
    tone_ogg(tmp_path / "a.ogg", 2.0)
    d = asyncio.run(convert_to_wav(tmp_path / "a.ogg", tmp_path / "a.wav", max_seconds=120))
    assert d == pytest.approx(2.0, abs=0.1)
    with wave.open(str(tmp_path / "a.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)


@needs_ffmpeg
def test_convert_caps_length_and_rejects_garbage(tmp_path):
    tone_ogg(tmp_path / "long.ogg", 12.0)
    d = asyncio.run(convert_to_wav(tmp_path / "long.ogg", tmp_path / "long.wav", max_seconds=5))
    assert 5.5 < d < 6.5  # обрезано до max_seconds + 1
    (tmp_path / "bad.ogg").write_bytes(b"not audio at all")
    with pytest.raises(AudioError):
        asyncio.run(convert_to_wav(tmp_path / "bad.ogg", tmp_path / "bad.wav", max_seconds=5))


def test_convert_without_ffmpeg(tmp_path):
    (tmp_path / "a.ogg").write_bytes(b"x")
    with pytest.raises(AudioError, match="ffmpeg"):
        asyncio.run(convert_to_wav(tmp_path / "a.ogg", tmp_path / "a.wav", max_seconds=5, ffmpeg="/нет/такого/ffmpeg"))


# ---------- форматирование ----------

def test_format_confident_has_sources_and_no_owner():
    text = format_answer(CONFIDENT)
    assert text.startswith("Отпуск составляет 28")
    assert "Источники:" in text and "[1] Положение об отпусках, п. 3.1" in text and "«Отпуск составляет" in text
    assert "Что делать: Подайте заявление" in text
    assert "Ответственный" not in text


def test_format_uncertain_lists_variants_and_owner():
    text = format_answer(UNCERTAIN)
    assert "1. Положение о премиях, п. 2.4" in text and "2. Положение о премиях, п. 2.5" in text
    assert "Ответственный: бухгалтер, П. П. Петрова, доб. 202" in text


def test_format_not_found():
    assert format_answer(NOT_FOUND) == NOT_FOUND["text"]


def test_format_refused_is_plain_refusal():
    from app.profanity import REFUSAL_TEXT
    text = format_answer({
        "mode": "refused", "text": REFUSAL_TEXT,
        "citations": [{"n": 1, "ref": "Положение об отпусках, п. 3.1", "quote": "не должно попасть"}],
        "variants": [{"ref": "Положение о премиях, п. 2.4", "text": "тоже нет"}],
        "owner": {"role": "инспектор", "full_name": "И. И. Иванова", "contact": "доб. 101"},
        "advice": "не показывать",
    })
    assert text == REFUSAL_TEXT
    assert "Источники:" not in text
    assert "Возможные пункты:" not in text
    assert "Ответственный" not in text
    assert "Что делать" not in text
    assert all(ord(ch) < 0x2190 or ch in "«»…" for ch in text)


def test_split_message_respects_limit_and_loses_nothing():
    para = "слово " * 200
    text = "\n\n".join([para.strip()] * 8)
    parts = split_message(text, limit=1000)
    assert all(len(p) <= 1000 for p in parts) and len(parts) > 1
    assert "".join(p.replace("\n", "").replace(" ", "") for p in parts) == text.replace("\n", "").replace(" ", "")
    assert split_message("   ") == []


def test_no_emoji_in_bot_texts():
    import bot.service as svc
    texts = [v for k, v in vars(svc).items() if k.startswith("MSG_") or k == "HELP"]
    assert texts
    for t in texts:
        assert all(ord(ch) < 0x2190 or ch in "«»…" for ch in t), t


# ---------- клиент API ----------

def test_api_ask_posts_question_to_chat():
    seen = []
    data = asyncio.run(make_api(seen=seen).ask("сколько дней отпуска?"))
    assert data["mode"] == "confident"
    assert seen[0][0] == "http://app:8000/api/chat" and "сколько дней отпуска?" in seen[0][1].decode()


def test_api_429_and_errors():
    with pytest.raises(RateLimited) as e:
        asyncio.run(make_api(status=429, headers={"Retry-After": "42"}).ask("вопрос"))
    assert e.value.retry_after == 42
    with pytest.raises(ApiError):
        asyncio.run(make_api(status=503).ask("вопрос"))

    def boom(request):
        raise httpx.ConnectError("нет связи")

    with pytest.raises(ApiError):
        asyncio.run(DocagentClient("http://app:8000", transport=httpx.MockTransport(boom)).ask("вопрос"))


# ---------- сервис: текст ----------

def test_text_question_gets_answer_with_sources():
    out, reply = collector()
    svc = BotService(make_settings(), make_api(), None)
    asyncio.run(svc.handle_text(1, "Сколько дней отпуска?", reply))
    assert len(out) == 1 and "Источники:" in out[0]


def test_text_short_and_long_not_sent_to_api():
    seen = []
    svc = BotService(make_settings(), make_api(seen=seen), None)
    out, reply = collector()
    asyncio.run(svc.handle_text(1, " а ", reply))
    asyncio.run(svc.handle_text(1, "я" * 2001, reply))
    assert not seen and len(out) == 2


def test_user_rate_limit_and_api_429():
    out, reply = collector()
    svc = BotService(make_settings(rate_limit_per_minute=2), make_api(), None)
    for _ in range(3):
        asyncio.run(svc.handle_text(1, "вопрос про отпуск", reply))
    assert "подождите минуту" in out[-1] and "Источники" in out[0]
    asyncio.run(svc.handle_text(2, "вопрос про отпуск", reply))  # другой пользователь не заблокирован
    assert "Источники" in out[-1]

    out2, reply2 = collector()
    svc2 = BotService(make_settings(), make_api(status=429, headers={"Retry-After": "30"}), None)
    asyncio.run(svc2.handle_text(1, "вопрос про отпуск", reply2))
    assert "30" in out2[0]


def test_limiter_window_expires():
    t = [0.0]
    lim = UserRateLimiter(1, clock=lambda: t[0])
    assert lim.allow(1) and not lim.allow(1)
    t[0] = 61
    assert lim.allow(1)
    assert UserRateLimiter(0).allow(1)


def test_app_down_message():
    out, reply = collector()
    svc = BotService(make_settings(), make_api(status=500), None)
    asyncio.run(svc.handle_text(1, "вопрос про отпуск", reply))
    assert "недоступен" in out[0]


def test_allowed_users_whitelist():
    seen = []
    svc = BotService(make_settings(telegram_allowed_users="5", telegram_open=False), make_api(seen=seen), FakeTranscriber())
    out, reply = collector()
    asyncio.run(svc.handle_text(6, "вопрос про отпуск", reply))
    asyncio.run(svc.handle_voice(6, 3, 1000, downloader(1), reply))
    assert not seen and all("списка доступа" in m or "закрыт" in m for m in out)
    asyncio.run(svc.handle_text(5, "вопрос про отпуск", reply))
    assert seen


def test_closed_by_default_without_allowlist():
    svc = BotService(make_settings(telegram_open=False, telegram_allowed_users=""), make_api(), None)
    assert not svc.allowed(1)
    out, reply = collector()
    asyncio.run(svc.handle_text(1, "вопрос про отпуск", reply))
    assert "списка доступа" in out[0]


def test_welcome_includes_web_link():
    svc = BotService(make_settings(web_public_url="https://docs.example.ru/"), make_api(), None)
    text = svc.welcome()
    assert text.startswith("Я бот «Где написано?»")
    assert "https://docs.example.ru" in text
    assert "Голосовые" in text  # transcriber=None → подсказка про голос


# ---------- сервис: голос ----------

@needs_ffmpeg
def test_voice_transcript_goes_through_profanity_filter(services_factory):
    """Распознанный текст уходит в /api/chat и проходит тот же фильтр, что и обычный вопрос."""
    import json

    from fastapi.testclient import TestClient

    from app.llm.fake import FakeLLM
    from app.main import create_app
    from app.profanity import REFUSAL_TEXT

    class NoCallLLM(FakeLLM):
        def chat(self, system, user, json_mode=False):
            raise AssertionError("языковая модель не должна вызываться")

    recognized = "это пиздец, где регламент отпуска"
    posted: list[str] = []
    with TestClient(create_app(services=services_factory(llm=NoCallLLM()))) as client:
        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(request.content.decode())
            r = client.post("/api/chat", content=request.content, headers={"content-type": "application/json"})
            return httpx.Response(r.status_code, json=r.json(), headers=dict(r.headers))

        svc = BotService(make_settings(), DocagentClient("http://app", transport=httpx.MockTransport(handler)),
                         FakeTranscriber(recognized))
        out, reply = collector()
        asyncio.run(svc.handle_voice(1, 2, 5000, downloader(1.0), reply))
    assert json.loads(posted[0])["question"] == recognized
    assert out[0] == f"Распознано: «{recognized}»"
    assert out[1] == REFUSAL_TEXT
    assert "Источники:" not in out[1] and "Возможные пункты:" not in out[1] and "Ответственный" not in out[1]


@needs_ffmpeg
def test_voice_flow_shows_recognized_text_then_answer():
    tr, seen = FakeTranscriber("Сколько дней отпуска?"), []
    svc = BotService(make_settings(), make_api(seen=seen), tr)
    out, reply = collector()
    asyncio.run(svc.handle_voice(1, 2, 5000, downloader(2.0), reply))
    assert out[0] == "Распознано: «Сколько дней отпуска?»"
    assert "Источники:" in out[1]
    assert tr.calls and tr.calls[0][1] == pytest.approx(2.0, abs=0.1)
    import json
    assert json.loads(seen[0][1])["question"] == "Сколько дней отпуска?"


def test_voice_rejected_before_download_when_too_long_or_big():
    called = []

    async def download(dest):
        called.append(1)

    tr = FakeTranscriber()
    svc = BotService(make_settings(voice_max_seconds=60, max_upload_mb=1), make_api(), tr)
    out, reply = collector()
    asyncio.run(svc.handle_voice(1, 61, 1000, download, reply))
    asyncio.run(svc.handle_voice(1, 10, 2 * 1024 * 1024, download, reply))
    assert not called and not tr.calls
    assert "60 с" in out[0] and "1 МБ" in out[1]


@needs_ffmpeg
def test_voice_real_length_checked_after_conversion():
    tr = FakeTranscriber()
    svc = BotService(make_settings(voice_max_seconds=3), make_api(), tr)
    out, reply = collector()
    # Telegram сообщил 2 секунды, а в файле 8
    asyncio.run(svc.handle_voice(1, 2, 1000, downloader(8.0), reply))
    assert not tr.calls and "длиннее 3 с" in out[0]


def test_voice_off_and_garbage_file():
    out, reply = collector()
    asyncio.run(BotService(make_settings(transcriber="off"), make_api(), None).handle_voice(1, 3, 100, downloader(1), reply))
    assert "отключены" in out[0]

    async def junk(dest: Path):
        dest.write_bytes(b"junk")

    out2, reply2 = collector()
    tr = FakeTranscriber()
    asyncio.run(BotService(make_settings(), make_api(), tr).handle_voice(1, 3, 4, junk, reply2))
    assert not tr.calls and "прочитать аудио" in out2[0]


@needs_ffmpeg
def test_voice_empty_transcript_and_engine_failure():
    out, reply = collector()
    asyncio.run(BotService(make_settings(), make_api(), FakeTranscriber(text="  ")).handle_voice(1, 2, 100, downloader(1.0), reply))
    assert len(out) == 1 and "не разобрана" in out[0]
    out2, reply2 = collector()
    asyncio.run(BotService(make_settings(), make_api(), FakeTranscriber(fail=True)).handle_voice(1, 2, 100, downloader(1.0), reply2))
    assert len(out2) == 1 and "не работает" in out2[0]


@needs_ffmpeg
def test_voice_counts_towards_rate_limit():
    tr = FakeTranscriber()
    svc = BotService(make_settings(rate_limit_per_minute=1), make_api(), tr)
    out, reply = collector()
    asyncio.run(svc.handle_voice(1, 2, 100, downloader(1.0), reply))
    asyncio.run(svc.handle_voice(1, 2, 100, downloader(1.0), reply))
    assert len(tr.calls) == 1 and "подождите минуту" in out[-1]


def test_idle_unload():
    async def run():
        tr = FakeTranscriber()
        tr.loaded = True
        svc = BotService(make_settings(transcriber_idle_seconds=100), make_api(), tr)
        svc.last_used = 0.0
        assert not await svc.unload_if_idle(now=50)
        assert await svc.unload_if_idle(now=150)
        assert tr.unloaded == 1 and not await svc.unload_if_idle(now=300)
        svc2 = BotService(make_settings(transcriber_idle_seconds=0), make_api(), tr)
        tr.loaded = True
        assert not await svc2.unload_if_idle(now=10**9)

    asyncio.run(run())


# ---------- движки: без загрузки моделей ----------

def test_gigaam_chunks_long_audio_and_joins_text(tmp_path, monkeypatch):
    import sys
    import types

    texts = iter(["первая часть", "вторая часть", "третья часть"])
    calls = []

    class FakeModel:
        def parameters(self):
            return iter([])

        buffers = parameters

        def transcribe(self, path):
            calls.append(path)
            assert wave.open(path).getnframes() <= 20 * SAMPLE_RATE
            return types.SimpleNamespace(text=next(texts))

    fake_gigaam = types.ModuleType("gigaam")
    loaded = []
    fake_gigaam.load_model = lambda name, **kw: (loaded.append((name, kw)), FakeModel())[1]
    monkeypatch.setitem(sys.modules, "gigaam", fake_gigaam)
    pytest.importorskip("torch")

    x = (np.random.default_rng(1).normal(0, 2000, SAMPLE_RATE * 45)).astype(np.int16)
    write_wav(tmp_path / "long.wav", x)
    t = GigaAMTranscriber("v3_e2e_ctc", "/models/gigaam", 2)
    assert not t.loaded and not loaded
    assert t.transcribe(tmp_path / "long.wav") == "первая часть вторая часть третья часть"
    assert t.loaded and len(calls) == 3 and loaded == [("v3_e2e_ctc", {"fp16_encoder": False, "device": "cpu",
                                                                       "download_root": "/models/gigaam"})]
    t.unload()
    assert not t.loaded


def test_join_chunks_removes_false_sentence_end():
    assert join_chunks(["Как возместить расходы на проезд?", "...и проживание, если билеты куплены."]) == \
        "Как возместить расходы на проезд и проживание, если билеты куплены."
    assert join_chunks(["Расходы на проезд?", "и проживание."]) == "Расходы на проезд и проживание."
    assert join_chunks(["Первое предложение.", "Второе предложение."]) == "Первое предложение. Второе предложение."
    assert join_chunks(["", "  ", "один"]) == "один"
    assert join_chunks([]) == ""


def test_whisper_joins_segments(tmp_path, monkeypatch):
    import sys
    import types

    class Seg:
        def __init__(self, text):
            self.text = text

    class FakeWhisper:
        def __init__(self, name, **kw):
            self.kw = kw

        def transcribe(self, path, **kw):
            assert kw["language"] == "ru"
            return iter([Seg(" привет"), Seg("мир ")]), None

    mod = types.ModuleType("faster_whisper")
    mod.WhisperModel = FakeWhisper
    monkeypatch.setitem(sys.modules, "faster_whisper", mod)
    write_wav(tmp_path / "a.wav", np.zeros(SAMPLE_RATE, dtype=np.int16))
    t = WhisperTranscriber("small", "int8", 2)
    assert t.transcribe(tmp_path / "a.wav") == "привет мир"
    t.unload()
    assert not t.loaded


# ---------- aiogram ----------

def test_dispatcher_has_handlers_for_text_voice_and_start():
    pytest.importorskip("aiogram")
    from bot.__main__ import build_dispatcher

    svc = BotService(make_settings(), make_api(), None)
    dp = build_dispatcher(svc)
    router = dp.sub_routers[0]
    assert len(router.message.handlers) == 4  # start, help, голос/аудио, текст


def test_main_without_token_exits_cleanly(monkeypatch):
    import bot.__main__ as m

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    monkeypatch.chdir(Path(__file__).parent)  # чтобы не подхватить настоящий .env
    assert m.main() == 0


def test_dispatcher_routes_text_and_voice_updates():
    """Прогон через настоящий Dispatcher aiogram: запросы к Telegram перехвачены, сети нет."""
    pytest.importorskip("aiogram")
    from datetime import datetime

    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.methods import SendMessage
    from aiogram.types import Chat, Message, Update, User, Voice

    from bot.__main__ import build_dispatcher

    sent: list[str] = []

    class Session(BaseSession):
        async def make_request(self, bot, method, timeout=None):
            assert isinstance(method, SendMessage)
            sent.append(method.text)
            return Message(message_id=99, date=datetime.now(), chat=Chat(id=1, type="private"), text=method.text)

        async def close(self):
            pass

        async def stream_content(self, *a, **kw):
            raise NotImplementedError

    class Svc(BotService):
        async def handle_voice(self, user_id, duration, file_size, download, reply):
            await reply(f"voice user={user_id} dur={duration} size={file_size}")

    chat, user = Chat(id=1, type="private"), User(id=5, is_bot=False, first_name="А")
    base = dict(date=datetime.now(), chat=chat, from_user=user)
    updates = [
        Update(update_id=1, message=Message(message_id=1, text="Сколько дней отпуска?", **base)),
        Update(update_id=2, message=Message(message_id=2, voice=Voice(file_id="f", file_unique_id="u", duration=7,
                                                                      file_size=1234), **base)),
        Update(update_id=3, message=Message(message_id=3, text="/start", **base)),
    ]

    async def run():
        bot = Bot("123456:TEST", session=Session())
        dp = build_dispatcher(Svc(make_settings(), make_api(), FakeTranscriber()))
        for u in updates:
            await dp.feed_update(bot, u)

    asyncio.run(run())
    assert "Источники:" in sent[0]
    assert sent[1] == "voice user=5 dur=7 size=1234"
    assert sent[2].startswith("Я бот «Где написано?»")


# ---------- защита от потока и чужих чатов ----------

def test_groups_and_bots_are_ignored_unless_allowed():
    svc = BotService(make_settings(telegram_allowed_chats="-100500"), make_api(), FakeTranscriber())
    assert svc.chat_allowed("private", 5)
    assert not svc.chat_allowed("group", -42)
    assert not svc.chat_allowed("supergroup", -42)
    assert not svc.chat_allowed("channel", -42)
    assert svc.chat_allowed("supergroup", -100500)
    assert not svc.chat_allowed("private", 5, from_bot=True)


def test_dispatcher_drops_group_messages():
    pytest.importorskip("aiogram")
    from datetime import datetime

    from aiogram import Bot
    from aiogram.client.session.base import BaseSession
    from aiogram.types import Chat, Message, Update, User

    from bot.__main__ import build_dispatcher

    sent: list[str] = []

    class Session(BaseSession):
        async def make_request(self, bot, method, timeout=None):
            sent.append(method.text)
            return Message(message_id=99, date=datetime.now(), chat=Chat(id=1, type="private"), text=method.text)

        async def close(self):
            pass

        async def stream_content(self, *a, **kw):
            raise NotImplementedError

    user = User(id=5, is_bot=False, first_name="А")
    other_bot = User(id=6, is_bot=True, first_name="Б")
    updates = [
        Update(update_id=1, message=Message(message_id=1, date=datetime.now(), chat=Chat(id=-42, type="group"),
                                            from_user=user, text="Сколько дней отпуска?")),
        Update(update_id=2, message=Message(message_id=2, date=datetime.now(), chat=Chat(id=6, type="private"),
                                            from_user=other_bot, text="Сколько дней отпуска?")),
    ]

    async def run():
        bot = Bot("123456:TEST", session=Session())
        dp = build_dispatcher(BotService(make_settings(), make_api(), FakeTranscriber()))
        for u in updates:
            await dp.feed_update(bot, u)

    asyncio.run(run())
    assert sent == []


def test_voice_flood_is_limited_per_user():
    out, reply = collector()
    svc = BotService(make_settings(voice_per_minute=2), make_api(), FakeTranscriber())

    async def broken_download(dest):
        raise OSError("сеть")  # ffmpeg не нужен: до распознавания не доходит

    async def run():
        for _ in range(3):
            await svc.handle_voice(7, 5, 1000, broken_download, reply)

    asyncio.run(run())
    assert "Не больше 2 голосовых в минуту" in out[-1]


def test_voice_queue_is_bounded():
    out, reply = collector()
    svc = BotService(make_settings(voice_queue_max=1), make_api(), FakeTranscriber())
    svc._voice_pending = 1  # одно голосовое уже распознаётся
    asyncio.run(svc.handle_voice(8, 5, 1000, downloader(1), reply))
    assert out == ["Сейчас распознаётся много голосовых. Подождите минуту или напишите вопрос текстом."]


# ---------- уточнение подразделения кнопками ----------

class _ClarifyApi:
    def __init__(self):
        self.calls = []

    async def ask(self, question, session_id=None, division=None):
        self.calls.append((question, session_id, division))
        if division is None:
            return {"mode": "clarify", "text": "Ответ зависит от подразделения. В каком вы подразделении?",
                    "clarify": {"pending_question": question,
                                "options": [{"id": "dbrct", "label": "ДБРЦТ"}, {"id": "unknown", "label": "Не знаю"}]}}
        return {"mode": "confident", "text": "Не менее чем за 3 рабочих дня [1].",
                "citations": [{"n": 1, "ref": "Проектная деятельность ДБРЦТ, п. 1.5", "quote": "не менее чем за 3",
                               "link": "/doc/x?p=y"}],
                "escalation": None, "glossary": [{"abbr": "РП", "full": "Руководитель проекта"}]}


def test_bot_clarify_buttons_and_division_answer():
    from bot.service import BotService
    from bot.settings import BotSettings

    api = _ClarifyApi()
    svc = BotService(BotSettings(_env_file=None, telegram_open=True, web_public_url="https://bot.example.local"), api, None)
    out = []

    async def reply(text, buttons=None):
        out.append((text, buttons))

    asyncio.run(svc.handle_text(7, "За сколько инициировать заявку?", reply))
    assert out[-1][1] == [("ДБРЦТ", "div:dbrct"), ("Не знаю", "div:unknown")]
    assert api.calls[-1][1] == "tg-7"
    asyncio.run(svc.handle_division(7, "dbrct", reply))
    assert api.calls[-1] == ("За сколько инициировать заявку?", "tg-7", "dbrct")
    text = out[-1][0]
    assert "3 рабочих дня" in text and "https://bot.example.local/doc/x?p=y" in text and "РП — Руководитель проекта" in text
    asyncio.run(svc.handle_division(7, "dbrct", reply))
    assert "неактуален" in out[-1][0]


def test_bot_formats_escalation_and_faq():
    from bot.formatting import format_answer

    t = format_answer({"mode": "not_found", "text": "Не смог ответить по регламентам, отправил запрос.",
                       "escalation": {"id": 3, "to": "q@example.local", "cc": "u@example.local", "delivery": "outbox"}})
    assert "ГН-3" in t and "копия u@example.local" in t
    f = format_answer({"mode": "faq", "text": "700 рублей.", "faq": {"label": "Разъяснение службы качества",
                                                                     "created_at": "2026-11-26 20:00:00"}})
    assert "Разъяснение службы качества" in f
    assert format_answer({"mode": "offtopic", "text": "Обратитесь к руководителю."}) == "Обратитесь к руководителю."


def test_bot_simplify_button_only_for_regulation_answer():
    from bot.formatting import can_simplify
    from bot.service import BotService

    assert not can_simplify(CONFIDENT)  # нет answer_id
    confident = dict(CONFIDENT, answer_id="abc12345abc12345abc12345abc12345", can_simplify=True)
    assert can_simplify(confident)
    assert not can_simplify(dict(confident, escalation={"id": 1}))
    assert not can_simplify(NOT_FOUND)
    assert not can_simplify(UNCERTAIN)


class _SimplifyApi:
    def __init__(self):
        self.asked = []
        self.simplified = []

    async def ask(self, question, session_id=None, division=None):
        self.asked.append((question, session_id, division))
        return {"mode": "confident", "text": "ПА должен принять заявку за 4 рабочих часа [1].",
                "answer_id": "abcd1234abcd1234abcd1234abcd1234", "can_simplify": True,
                "citations": [{"n": 1, "ref": "Пресейловая деятельность Дирекции ИБ, п. 5.1",
                               "quote": "принять заявку за 4 рабочих часа"}],
                "glossary": [{"abbr": "ПА", "full": "пресейл-архитектор"}],
                "escalation": None}

    async def simplify(self, answer_id):
        self.simplified.append(answer_id)
        return {"text": "ПА (пресейл-архитектор) должен принять заявку за 4 рабочих часа [1].",
                "citations": [{"n": 1, "ref": "Пресейловая деятельность Дирекции ИБ, п. 5.1",
                               "quote": "принять заявку за 4 рабочих часа"}],
                "glossary": [{"abbr": "ПА", "full": "пресейл-архитектор"}],
                "advice": "", "simplified": True, "source": "model"}


def test_bot_simplify_button_and_reply():
    from bot.service import BotService
    from bot.settings import BotSettings

    api = _SimplifyApi()
    svc = BotService(BotSettings(_env_file=None, telegram_open=True, web_public_url="http://example.local"), api, None)
    out = []

    async def reply(text, buttons=None):
        out.append((text, buttons))

    asyncio.run(svc.handle_text(5, "За какой срок ПА должен принять заявку?", reply))
    assert out[-1][1] == [("Не понял", "simp:abcd1234abcd1234abcd1234abcd1234")]
    asyncio.run(svc.handle_simplify(5, "abcd1234abcd1234abcd1234abcd1234", reply))
    assert api.simplified == ["abcd1234abcd1234abcd1234abcd1234"]
    text = out[-1][0]
    assert "простыми словами" in text
    assert "пресейл-архитектор" in text
    assert "4 рабочих часа" in text
    assert "п. 5.1" in text
