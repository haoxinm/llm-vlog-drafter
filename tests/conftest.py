from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures"


def ffprobe_json(name: str) -> dict:
    import json

    return json.loads((FIXTURES / "ffprobe" / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def ffmpeg_tools() -> tuple[str, str]:
    from trip_vlog.media.tools import find_ffmpeg, find_ffprobe

    ffmpeg, ffprobe = find_ffmpeg(), find_ffprobe()
    if not ffmpeg or not ffprobe:
        pytest.skip("ffmpeg/ffprobe not installed")
    return str(ffmpeg), str(ffprobe)


@pytest.fixture(scope="session")
def generated_media(tmp_path_factory, ffmpeg_tools) -> Path:
    """Fixture media made by tests/fixtures/make_fixtures.py (into a temp dir, so the repo stays clean)."""
    out = tmp_path_factory.mktemp("generated")
    subprocess.run([sys.executable, str(FIXTURES / "make_fixtures.py"), str(out)], check=True, capture_output=True)
    return out


@pytest.fixture
def media_dir(tmp_path, generated_media) -> Path:
    """A fresh copy of the fixture media (mtime preserved) that tests may add files to."""
    dst = tmp_path / "media"
    shutil.copytree(generated_media, dst)
    return dst


@pytest.fixture
def workspace(tmp_path) -> Path:
    return tmp_path / "workspace"


def hash_tree(root: Path) -> dict[str, tuple[str, int]]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
    return out
