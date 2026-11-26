"""Конвертация голосовых в wav 16 кГц моно через ffmpeg и нарезка длинной записи по паузам."""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000


class AudioError(Exception):
    """Файл не удалось прочитать или он не подходит."""


async def convert_to_wav(src: Path, dst: Path, *, max_seconds: int, ffmpeg: str = "ffmpeg", timeout: float = 60.0) -> float:
    """Пишет dst (wav, 16 кГц, моно, 16 бит) и возвращает длительность в секундах.

    ffmpeg читает не больше max_seconds + 1 секунды, поэтому слишком длинная запись не раздувает диск и время.
    """
    cmd = [ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE),
           "-t", str(max_seconds + 1), "-c:a", "pcm_s16le", str(dst)]
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError as e:
        raise AudioError("ffmpeg не установлен") from e
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError as e:
        proc.kill()
        await proc.wait()
        raise AudioError("Конвертация заняла слишком много времени") from e
    if proc.returncode != 0 or not dst.exists():
        raise AudioError("Не удалось прочитать аудио: " + err.decode("utf-8", "replace").strip()[:200])
    return wav_duration(dst)


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def read_wav(path: Path) -> np.ndarray:
    """Отсчёты int16 из wav 16 кГц моно."""
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise AudioError("Ожидался wav 16 кГц, моно, 16 бит")
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def write_wav(path: Path, samples: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(samples.astype(np.int16).tobytes())


def split_on_pauses(samples: np.ndarray, *, max_chunk_s: float = 20.0, search_s: float = 5.0,
                    frame_ms: int = 50) -> list[np.ndarray]:
    """Режет запись на куски не длиннее max_chunk_s, по возможности в самом тихом месте последних search_s секунд.

    GigaAM принимает до 25 секунд за раз, а внешний детектор пауз (pyannote) требует отдельный токен Hugging Face.
    """
    n_max = int(max_chunk_s * SAMPLE_RATE)
    if len(samples) <= n_max:
        return [samples]
    frame = int(SAMPLE_RATE * frame_ms / 1000)
    out: list[np.ndarray] = []
    pos = 0
    while len(samples) - pos > n_max:
        lo = pos + n_max - int(search_s * SAMPLE_RATE)
        hi = pos + n_max
        seg = samples[lo:hi].astype(np.float64)
        n_frames = len(seg) // frame
        energy = (seg[: n_frames * frame].reshape(n_frames, frame) ** 2).mean(axis=1)
        cut = lo + int(np.argmin(energy)) * frame + frame // 2
        out.append(samples[pos:cut])
        pos = cut
    out.append(samples[pos:])
    return out
