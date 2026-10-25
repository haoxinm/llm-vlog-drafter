"""`command` backend: run a user-configured local executable that writes an SRT/VTT/JSON transcript.

No shell is used (argument list, `shell=False`). Placeholders inside each argument: {input}, {output_dir}, {output_base}
(= {output_dir}/transcript) and {language} (configured language or "auto").
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from trip_vlog.config import SpeechCommandConfig
from trip_vlog.media.tools import popen_flags
from trip_vlog.paths import long_path
from trip_vlog.speech.parse import parse_transcript_file
from trip_vlog.speech.types import SpeechCommandError, SpeechConfigError, Transcription

OUTPUT_BASE_NAME = "transcript"
MAX_STDERR = 300


def resolve_executable(configured: str) -> str:
    p = Path(configured).expanduser()
    if p.is_file():
        return str(p)
    found = shutil.which(configured)
    if not found:
        raise SpeechConfigError(f"speech.command.executable {configured!r} was not found (give a full path or put it on PATH)")
    return found


def expand_args(args: list[str], *, input_path: Path, output_dir: Path, language: str | None) -> list[str]:
    repl = {"{input}": str(input_path), "{output_dir}": str(output_dir), "{output_base}": str(output_dir / OUTPUT_BASE_NAME),
            "{language}": language or "auto"}
    out = []
    for a in args:
        for k, v in repl.items():
            a = a.replace(k, v)
        out.append(a)
    return out


def find_output(output_dir: Path, fmt: str) -> Path | None:
    """The transcript the tool wrote: transcript.<fmt> if present, else the only (or the first by name) *.<fmt> file."""
    exact = output_dir / f"{OUTPUT_BASE_NAME}.{fmt}"
    if exact.is_file():
        return exact
    found = sorted(p for p in output_dir.rglob(f"*.{fmt}") if p.is_file())
    return found[0] if found else None


class CommandTranscriber:
    name = "command"
    is_local = True

    def __init__(self, cfg: SpeechCommandConfig, *, language: str | None = None):
        self.cfg, self.language = cfg, language
        self.executable = resolve_executable(cfg.executable or "")

    def identity(self) -> dict[str, Any]:
        c = self.cfg
        exe = Path(self.executable).name
        return {"backend": self.name, "executable": exe, "args": list(c.args), "format": c.output_format, "language": self.language}

    def transcribe(self, audio_path: Path, duration: float | None) -> Transcription:
        with tempfile.TemporaryDirectory(prefix="trip-vlog-asr-") as tmp:
            out_dir = Path(tmp)
            cmd = [self.executable, *expand_args(self.cfg.args, input_path=audio_path, output_dir=out_dir, language=self.language)]
            try:
                cp = subprocess.run(cmd, capture_output=True, timeout=self.cfg.timeout_seconds, encoding="utf-8", errors="replace",
                                    stdin=subprocess.DEVNULL, cwd=tmp, **popen_flags())
            except subprocess.TimeoutExpired:
                raise SpeechCommandError(f"the transcription command did not finish within {self.cfg.timeout_seconds:g}s (speech.command.timeout_seconds)") from None
            except OSError as e:
                raise SpeechCommandError(f"cannot run the transcription command: {e.strerror or e}") from None
            if cp.returncode != 0:
                tail = " ".join((cp.stderr or cp.stdout or "no output").split())[-MAX_STDERR:]
                raise SpeechCommandError(f"the transcription command exited with status {cp.returncode}: {tail}")
            fmt = self.cfg.output_format
            out = find_output(out_dir, fmt)
            if out is None:
                raise SpeechCommandError(f"the transcription command finished but wrote no *.{fmt} file into {{output_dir}}; "
                                         "check speech.command.args and output_format")
            try:
                text = long_path(out).read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                raise SpeechCommandError(f"cannot read the transcript file: {e.strerror or e}") from None
            try:
                return parse_transcript_file(text, fmt, duration)
            except Exception as e:  # noqa: BLE001 - parser errors become a typed failure
                raise SpeechCommandError(f"cannot parse the {fmt} transcript: {e}") from None
