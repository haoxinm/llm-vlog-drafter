from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401
from trip_vlog.filmcraft.command_log import CommandLog, Sanitizer, default_replacements


def test_paths_masked_in_every_spelling(tmp_path):
    media, ws, home = tmp_path / "Trips" / "Italy 2026", tmp_path / "vlog", Path.home()
    s = Sanitizer([(media, "$MEDIA_ROOT"), (ws, "$WORKSPACE"), (home, "~")])
    clip = media / "day 1" / "IMG_1.MOV"
    obj = {
        "path": str(clip), "posix": clip.as_posix(), "url": clip.as_uri(), "escaped": str(clip).replace("\\", "\\\\"),
        "list": [str(ws / "filmcraft" / "x.fcproj")], "nested": {"deep": f"opened {home / 'secret' / 'f.txt'} ok"},
        "quoted": "file://" + __import__("urllib.parse", fromlist=["quote"]).quote(clip.as_posix()),
    }
    out = json.dumps(s.obj(obj))
    for needle in (str(media), media.as_posix(), str(ws), str(home) if str(home) != "/" else "\0"):
        assert needle.replace("\\", "\\\\") not in out and needle not in out
    assert "$MEDIA_ROOT" in out and "$WORKSPACE" in out and "~" in out
    assert s.obj({"path": str(clip)})["path"].startswith("$MEDIA_ROOT")


def test_media_root_inside_home_prefers_the_longer_match(tmp_path, monkeypatch):
    home = tmp_path / "home" / "me"
    media = home / "Videos" / "trip"
    s = Sanitizer([(home, "~"), (media, "$MEDIA_ROOT")])
    assert s.text(str(media / "a.mp4")).startswith("$MEDIA_ROOT")
    assert s.text(str(home / "Documents")).startswith("~")


def test_secret_keys_and_values_are_masked(monkeypatch):
    monkeypatch.setenv("FILMCRAFT_CONTROL_TOKEN", "tok-1234567890abcdef")
    s = Sanitizer()
    out = s.obj({"token": "abc", "Authorization": "Bearer xyz", "api_key": "k", "nested": [{"password": "p"}],
                 "msg": "control token is tok-1234567890abcdef today", "note": "api_key=SUPERSECRET123 rest",
                 "bearer": "Bearer abcdefghijklmnop", "ok": "fine", "n": 3})
    blob = json.dumps(out)
    assert out["token"] == out["Authorization"] == out["api_key"] == "***"
    assert out["nested"][0]["password"] == "***"
    for secret in ("tok-1234567890abcdef", "SUPERSECRET123", "abcdefghijklmnop"):
        assert secret not in blob
    assert out["ok"] == "fine" and out["n"] == 3


def test_huge_strings_are_truncated_with_a_digest():
    s = Sanitizer()
    out = s.obj({"image": "A" * 200_000})
    assert len(out["image"]) < 3000 and "truncated 200000 chars sha256=" in out["image"]


def test_log_file_is_jsonl_utf8_with_lf_newlines(tmp_path):
    log = CommandLog(tmp_path / "sub" / "log.jsonl", Sanitizer([(tmp_path, "$WORKSPACE")]))
    log.write(dir="request", method="x", params={"text": "café \U0001f3ac", "p": str(tmp_path / "a")})
    log.write(dir="response", method="x", result={"ok": True})
    raw = (tmp_path / "sub" / "log.jsonl").read_bytes()
    assert b"\r" not in raw and raw.endswith(b"\n")
    recs = [json.loads(l) for l in raw.decode("utf-8").splitlines()]
    assert [r["seq"] for r in recs] == [1, 2] and "café" in recs[0]["params"]["text"]
    assert recs[0]["params"]["p"].startswith("$WORKSPACE")


def test_full_build_log_contains_no_media_root_workspace_or_home(fc):
    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    adapter.export_interchange()
    adapter.close()
    text = (ws / "filmcraft" / "command_log.jsonl").read_text(encoding="utf-8")
    assert len(text.splitlines()) > 40
    for real in (media, ws):
        for spelling in (str(real), real.as_posix(), str(real).replace("\\", "\\\\"), real.as_uri()):
            assert spelling not in text, spelling
    assert "$MEDIA_ROOT" in text and "$WORKSPACE" in text
    home = str(Path.home())
    if home not in ("/", "") and not str(ws).startswith(home):
        assert home not in text
    for line in text.splitlines():
        json.loads(line)


def test_default_replacements_order(tmp_path):
    reps = default_replacements(tmp_path / "ws", tmp_path / "media")
    assert [t for _, t in reps][:2] == ["$MEDIA_ROOT", "$WORKSPACE"]
