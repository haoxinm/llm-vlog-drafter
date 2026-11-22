"""`trip-vlog llm-check --speech`: one generated one-second WAV through the configured transcription backend.

Only a fixed, generated tone is sent; no media from the user's library is involved. Empty text is a success: the point is
that the backend is reachable, authorised, accepts the request shape and returns a parsable reply.
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
import time
import wave
from pathlib import Path
from typing import Any, Mapping

from trip_vlog.config import Config
from trip_vlog.llm.check import CheckRow, _short, public_url
from trip_vlog.llm.transport import Transport
from trip_vlog.speech.stage import make_backend
from trip_vlog.speech.types import SpeechError


def write_test_wav(path: Path, seconds: float = 1.0, rate: int = 16000, freq: float = 440.0) -> Path:
    """A quiet mono 16-bit sine tone."""
    n = int(seconds * rate)
    frames = b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / rate))) for i in range(n))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return path


def check_speech(config: Config, *, transport: Transport | None = None, env: Mapping[str, str] | None = None,
                 sleep: Any = time.sleep) -> list[CheckRow]:
    env = os.environ if env is None else env
    sp = config.speech
    row = CheckRow(endpoint="speech", test="transcribe", api=sp.backend, where="local" if sp.is_local else "remote", structured="-")
    if sp.backend == "openai_transcriptions":
        row.url = public_url(sp.endpoint.url)
    elif sp.backend == "command":
        row.url = Path(sp.command.executable or "").name
    if not sp.active:
        row.status = "skipped"
        row.note = "speech.enabled is false" if not sp.enabled else "speech.backend is none"
        return [row]
    try:
        backend = _http(sp, env, transport, sleep) if sp.backend == "openai_transcriptions" else make_backend(sp)
        row.model = getattr(backend, "model", None) or ""
        with tempfile.TemporaryDirectory(prefix="trip_vlog_check_") as td:
            wav = write_test_wav(Path(td) / "tone.wav")
            t0 = time.perf_counter()
            tr = backend.transcribe(wav, 1.0)
            row.latency_ms = (time.perf_counter() - t0) * 1000
        row.note = f"reply parsed: {len(tr.segments)} segment(s), timestamps {tr.timestamps}" + (" (empty text is fine for a tone)" if not tr.segments else "")
    except SpeechError as e:
        row.status, row.error = "fail", f"{type(e).__name__}: {_short(e, env)}"
    return [row]


def _http(sp: Any, env: Mapping[str, str], transport: Transport, sleep: Any) -> Any:
    from trip_vlog.speech.http_backend import OpenAITranscriptions

    return OpenAITranscriptions(sp.endpoint, language=sp.language, env=env, transport=transport, sleep=sleep)
