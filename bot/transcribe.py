"""Распознавание речи. Движок выбирается переменной TRANSCRIBER: gigaam | whisper | off.

Модели грузятся при первом голосовом сообщении, а не при старте бота, и по таймеру выгружаются из памяти.
Оба движка работают на CPU.
"""
from __future__ import annotations

import ctypes
import gc
import logging
import tempfile
import threading
from pathlib import Path
from typing import Protocol

import numpy as np

from bot.audio import SAMPLE_RATE, read_wav, split_on_pauses, write_wav
from bot.settings import BotSettings

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    name: str

    def transcribe(self, wav: Path) -> str:
        """Текст из wav 16 кГц моно. Блокирующий вызов: запускать в потоке."""

    def unload(self) -> None: ...

    @property
    def loaded(self) -> bool: ...


def join_chunks(parts: list[str]) -> str:
    """Склеивает куски. Если запись резали посреди фразы, модель ставит в конце куска точку или вопрос, а следующий
    начинается с маленькой буквы или многоточия: такой знак убираем."""
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if out:
            cont = part.lstrip(". …")
            if cont != part or cont[:1].islower():
                out = out.rstrip().rstrip(".?!…")
                part = cont
            out += " "
        out += part
    return out.strip()


def _release_memory() -> None:
    gc.collect()
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)  # вернуть освобождённую память системе (glibc)
    except Exception:
        pass


class GigaAMTranscriber:
    """GigaAM v3 (Сбер, MIT). Для e2e-моделей текст приходит с пунктуацией и нормализованными числами."""

    name = "gigaam"
    chunk_seconds = 20.0  # модель принимает до 25 секунд

    def __init__(self, model_name: str = "v3_e2e_ctc", download_root: str = "", threads: int = 3) -> None:
        self.model_name = model_name
        self.download_root = download_root or None
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        if self._model is None:
            import gigaam  # тяжёлый импорт (torch) откладываем до первого использования
            import torch

            torch.set_num_threads(self.threads)
            log.info("загрузка GigaAM %s", self.model_name)
            self._model = gigaam.load_model(self.model_name, fp16_encoder=False, device="cpu",
                                            download_root=self.download_root)
        return self._model

    def transcribe(self, wav: Path) -> str:
        import torch

        with self._lock:
            model = self._load()
            parts: list[str] = []
            chunks = split_on_pauses(read_wav(wav), max_chunk_s=self.chunk_seconds)
            with tempfile.TemporaryDirectory(prefix="gigaam_") as tmp:
                for i, chunk in enumerate(chunks):
                    if len(chunk) < SAMPLE_RATE // 5:  # короче 0,2 с — это не речь
                        continue
                    p = Path(tmp) / f"{i}.wav"
                    write_wav(p, chunk)
                    with torch.inference_mode():
                        res = model.transcribe(str(p))
                    text = str(getattr(res, "text", res)).strip()
                    if text:
                        parts.append(text)
            return join_chunks(parts)

    def unload(self) -> None:
        with self._lock:
            if self._model is not None:
                model, self._model = self._model, None
                # Объект модели остаётся жить в ссылках внутри hydra, поэтому веса освобождаем явно
                import torch

                with torch.no_grad():
                    for t in (*model.parameters(), *model.buffers()):
                        t.data = torch.empty(0)
                del model
                _release_memory()
                log.info("GigaAM выгружена из памяти")


class WhisperTranscriber:
    """faster-whisper (CTranslate2, int8). Запасной вариант: модель small, около 0,5 ГБ памяти."""

    name = "whisper"

    def __init__(self, model_name: str = "small", compute_type: str = "int8", threads: int = 3) -> None:
        self.model_name = model_name
        self.compute_type = compute_type
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            log.info("загрузка faster-whisper %s", self.model_name)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type=self.compute_type,
                                       cpu_threads=self.threads)
        return self._model

    def transcribe(self, wav: Path) -> str:
        with self._lock:
            model = self._load()
            # Отсчёты отдаём массивом: так faster-whisper не декодирует файл сам (ffmpeg мы уже вызвали)
            audio = read_wav(wav).astype(np.float32) / 32768.0
            segments, _info = model.transcribe(audio, language="ru", beam_size=3, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()

    def unload(self) -> None:
        with self._lock:
            if self._model is not None:
                self._model = None
                _release_memory()
                log.info("faster-whisper выгружена из памяти")


def make_transcriber(settings: BotSettings) -> Transcriber | None:
    if settings.transcriber == "off":
        return None
    if settings.transcriber == "gigaam":
        return GigaAMTranscriber(settings.gigaam_model, settings.gigaam_dir, settings.transcriber_threads)
    if settings.transcriber == "whisper":
        return WhisperTranscriber(settings.whisper_model, settings.whisper_compute_type, settings.transcriber_threads)
    raise ValueError(f"Неизвестный TRANSCRIBER: {settings.transcriber}")
