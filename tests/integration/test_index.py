"""End-to-end `index` runs against ffmpeg-generated fixtures (skipped cleanly when ffmpeg is missing)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import hash_tree
from trip_vlog.cli import main
from trip_vlog.config import ConfigError
from trip_vlog.logging_utils import read_jsonl
from trip_vlog.media.catalog import Catalog
from trip_vlog.orchestrator import run_index

pytestmark = pytest.mark.integration

OFFLINE = {"echo": lambda *_: None}


def index(media, ws, **kw):
    return run_index(media, ws, None, kw.pop("overrides", None), echo=lambda *_: None, **kw)


def events(ws: Path, run_id: str, stage: str):
    return [e for e in read_jsonl(ws / "logs" / "run.jsonl") if e["run_id"] == run_id and e["stage"] == stage and "asset_id" in e and "cache" in e]


def asset_by_path(ws: Path, rel: str):
    with Catalog(ws / "media_index" / "catalog.sqlite") as c:
        return next(r for r in c.list_assets(present_only=False) if r["relative_path"] == rel)


def test_first_run_catalogs_everything(media_dir, workspace):
    res = index(media_dir, workspace)
    assert res.exit_code == 0 and set(res.stage_status.values()) == {"succeeded"}
    assert res.summary == {"assets": 5, "probe_failed": 0, "stale": 0}
    land = asset_by_path(workspace, "landscape_5s.mp4")
    assert (land["width"], land["height"], land["frame_rate_num"], land["frame_rate_den"]) == (1920, 1080, 30, 1)
    assert land["audio_streams"] == 1 and land["media_type"] == "video" and abs(land["duration_seconds"] - 5.0) < 0.2
    assert land["thumbnail_path"] and (workspace / land["thumbnail_path"]).is_file()
    assert land["contact_sheet_path"] and (workspace / land["contact_sheet_path"]).is_file()
    port = asset_by_path(workspace, "portrait_4s.mp4")
    assert (port["display_width"], port["display_height"]) == (1080, 1920)
    still = asset_by_path(workspace, "still_1080p.png")
    assert still["media_type"] == "image" and still["duration_seconds"] is None and still["thumbnail_path"]
    assert asset_by_path(workspace, "music_6s.wav")["media_type"] == "audio"
    # workspace files
    for rel in ("manifest.json", "project.yaml", "README.md", "logs/run.jsonl", "logs/errors.jsonl", "media_index/catalog.sqlite",
                "media_index/assets.jsonl", "media_index/inventory_report.json"):
        assert (workspace / rel).is_file(), rel
    assert len(list((workspace / "media_index" / "contact_sheets").glob("folder_*.jpg"))) == 1
    man = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    assert man["ffprobe"]["version"] and man["platform"]["system"] and man["config_hash"] and man["tool_version"]
    assert {s: v["status"] for s, v in man["stages"].items()} == {"scan": "succeeded", "probe": "succeeded", "catalog": "succeeded", "thumbnails": "succeeded"}
    rows = [json.loads(l) for l in (workspace / "media_index" / "assets.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 5 and all(r["asset_id"].startswith("asset_") for r in rows)


def test_originals_byte_unchanged_and_nothing_written_to_media(media_dir, workspace):
    before = hash_tree(media_dir)
    index(media_dir, workspace)
    index(media_dir, workspace, overrides={"media.fingerprint": "sha256"})
    assert hash_tree(media_dir) == before  # content and mtime, and no new files


def test_second_run_is_all_cache_hits(media_dir, workspace):
    index(media_dir, workspace)
    res = index(media_dir, workspace)
    assert res.stage_status == {"scan": "succeeded", "probe": "skipped", "catalog": "succeeded", "thumbnails": "skipped"}
    log = read_jsonl(workspace / "logs" / "run.jsonl")
    mine = [e for e in log if e["run_id"] == res.run_id]
    assert not [e for e in mine if e.get("cache") == "miss" and e["stage"] in ("probe", "thumbnails")]
    assert [e["cache"] for e in mine if e["stage"] in ("probe", "thumbnails") and e["status"] == "skipped"] == ["hit", "hit"]
    man = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
    assert man["stages"]["probe"]["decision"] == "cache_hit"


def test_touching_one_file_reprocesses_only_that_one(media_dir, workspace):
    index(media_dir, workspace)
    target = media_dir / "landscape_5s.mp4"
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 7_000_000_000))
    res = index(media_dir, workspace)
    assert res.stage_status["probe"] == "succeeded" and res.stage_status["thumbnails"] == "succeeded"
    probe = events(workspace, res.run_id, "probe")
    assert {e["cache"] for e in probe if e["asset_id"] != asset_by_path(workspace, "landscape_5s.mp4")["asset_id"]} == {"hit"}
    assert [e["asset_id"] for e in probe if e["cache"] == "miss"] == [asset_by_path(workspace, "landscape_5s.mp4")["asset_id"]]
    thumbs = events(workspace, res.run_id, "thumbnails")
    assert [e["asset_id"] for e in thumbs if e["cache"] == "miss"] == [asset_by_path(workspace, "landscape_5s.mp4")["asset_id"]]
    assert sum(1 for e in thumbs if e["cache"] == "hit") == 2  # portrait video + still
    row = asset_by_path(workspace, "landscape_5s.mp4")
    assert row["analysis_status"] == "stale" and row["previous_content_version"] != row["content_version"]
    assert res.summary["stale"] == 1
    assert len(list((workspace / "media_index" / "thumbnails").glob(row["asset_id"] + "_*.jpg"))) == 1  # old thumbnail replaced
    # id unchanged by content change
    assert row["asset_id"] == "asset_" + __import__("hashlib").sha256(b"landscape_5s.mp4").hexdigest()[:16]


def test_corrupt_file_does_not_abort_and_is_reported(tmp_path, media_dir, workspace):
    (media_dir / "broken.mp4").write_bytes(os.urandom(4096))
    (media_dir / "truncated.mov").write_bytes((media_dir / "landscape_5s.mp4").read_bytes()[:3000])
    (media_dir / "fake.jpg").write_bytes(b"\xff\xd8\xff\xe0 not an image")
    res = index(media_dir, workspace)
    assert res.exit_code == 0
    assert res.stage_status["probe"] == "partial" and res.stage_status["catalog"] == "succeeded"
    assert res.summary["assets"] == 8 and res.summary["probe_failed"] == 3
    for rel in ("broken.mp4", "truncated.mov", "fake.jpg"):
        row = asset_by_path(workspace, rel)
        assert row["probe_status"] == "failed" and row["probe_error"] and row["analysis_status"] == "failed", rel
    assert asset_by_path(workspace, "landscape_5s.mp4")["probe_status"] == "ok"
    errs = read_jsonl(workspace / "logs" / "errors.jsonl")
    assert len([e for e in errs if e["stage"] == "probe"]) == 3
    assert all(str(media_dir) not in e["error"] for e in errs)  # media root is redacted in logs
    report = json.loads((workspace / "media_index" / "inventory_report.json").read_text(encoding="utf-8"))
    assert len(report["failed_probes"]) == 3
    # failed files are retried each run; good ones are cache hits
    res2 = index(media_dir, workspace)
    assert res2.stage_status["probe"] == "partial"
    probe = events(workspace, res2.run_id, "probe")
    assert sum(1 for e in probe if e["cache"] == "hit") == 5 and sum(1 for e in probe if e["status"] == "failed") == 3


def test_hidden_unsupported_and_case_handling(media_dir, workspace):
    (media_dir / ".DS_Store").write_bytes(b"x"); (media_dir / "Thumbs.db").write_bytes(b"x"); (media_dir / "._landscape_5s.mp4").write_bytes(b"x")
    (media_dir / "__MACOSX").mkdir(); (media_dir / "__MACOSX" / "x.mp4").write_bytes(b"x")
    (media_dir / "notes.txt").write_text("hi", encoding="utf-8")
    sub = media_dir / "Day 2"
    sub.mkdir()
    shutil.copy2(media_dir / "landscape_5s.mp4", sub / "CLIP.MOV")  # upper-case extension, container is mp4
    res = index(media_dir, workspace)
    assert res.summary["assets"] == 6 and res.summary["probe_failed"] == 0
    report = json.loads((workspace / "media_index" / "inventory_report.json").read_text(encoding="utf-8"))
    assert report["scan"]["skipped_counts"] == {"hidden": 1, "system_file": 3, "unsupported_extension": 1}
    assert asset_by_path(workspace, "Day 2/CLIP.MOV")["media_type"] == "video"


def test_copy_at_other_path_reported_as_duplicate_not_merged(media_dir, workspace):
    shutil.copy2(media_dir / "landscape_5s.mp4", media_dir / "copy_of_landscape.mp4")
    res = index(media_dir, workspace)
    assert res.summary["assets"] == 6
    report = json.loads((workspace / "media_index" / "inventory_report.json").read_text(encoding="utf-8"))
    assert [d["paths"] for d in report["probable_duplicates"]] == [["copy_of_landscape.mp4", "landscape_5s.mp4"]]
    ids = {asset_by_path(workspace, p)["asset_id"] for p in ("copy_of_landscape.mp4", "landscape_5s.mp4")}
    assert len(ids) == 2


def test_move_reported_as_probable_move(media_dir, workspace):
    index(media_dir, workspace)
    (media_dir / "later").mkdir()
    shutil.move(str(media_dir / "portrait_4s.mp4"), str(media_dir / "later" / "portrait_4s.mp4"))
    res = index(media_dir, workspace)
    assert res.summary["assets"] == 5
    report = json.loads((workspace / "media_index" / "inventory_report.json").read_text(encoding="utf-8"))
    assert report["probable_moves"] == [{"from": "portrait_4s.mp4", "to": "later/portrait_4s.mp4",
                                         "content_version": asset_by_path(workspace, "later/portrait_4s.mp4")["content_version"]}]
    assert asset_by_path(workspace, "portrait_4s.mp4")["present"] == 0  # kept in catalog, flagged missing


def test_rotated_phone_clip_display_dims(media_dir, workspace, ffmpeg_tools):
    ffmpeg = ffmpeg_tools[0]
    out = media_dir / "rotated.mp4"
    cp = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-display_rotation", "90", "-i",
                         str(media_dir / "landscape_5s.mp4"), "-c", "copy", str(out)], capture_output=True)
    if cp.returncode != 0 or not out.exists():
        pytest.skip("this ffmpeg cannot write a display matrix")
    index(media_dir, workspace)
    row = asset_by_path(workspace, "rotated.mp4")
    assert row["rotation_degrees"] in (90, 270) and (row["width"], row["height"]) == (1920, 1080)
    assert (row["display_width"], row["display_height"]) == (1080, 1920)


def test_sha256_mode(media_dir, workspace):
    index(media_dir, workspace, overrides={"media.fingerprint": "sha256"})
    row = asset_by_path(workspace, "still_1080p.png")
    assert row["content_version"] == "sha256:" + row["sha256"] and len(row["sha256"]) == 64


def test_switching_fingerprint_mode_does_not_mark_stale(media_dir, workspace):
    index(media_dir, workspace)
    res = index(media_dir, workspace, overrides={"media.fingerprint": "sha256"})
    assert res.summary["stale"] == 0


def test_media_root_must_not_change_for_a_workspace(media_dir, workspace, tmp_path):
    index(media_dir, workspace)
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(ConfigError, match="different media folder"):
        index(other, workspace)


def test_path_safety_via_cli(media_dir, capsys):
    assert main(["index", "--media", str(media_dir), "--output", str(media_dir / "out")]) == 2
    assert "inside the media folder" in capsys.readouterr().err
    assert main(["index", "--media", str(media_dir / "nope"), "--output", str(media_dir.parent / "o")]) == 2


def test_cli_index_status_resume_draft(media_dir, workspace, capsys):
    assert main(["index", "--media", str(media_dir), "--output", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "assets: 5" in out and "thumbnails" in out
    assert main(["status", "--output", str(workspace)]) == 0
    out = capsys.readouterr().out
    assert "scan" in out and "succeeded" in out and "5 present" in out
    assert main(["status", "--output", str(workspace), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["catalog"]["assets_present"] == 5
    assert main(["resume", "--output", str(workspace)]) == 0  # media root comes from the manifest
    assert "probe       skipped" in capsys.readouterr().out
    # draft runs the whole pipeline; 90 s is far more than the fixtures hold, so the plan stage fails and nothing reaches FilmCraft
    assert main(["draft", "--media", str(media_dir), "--output", str(workspace), "--duration", "90", "--aspect", "9:16"]) == 1
    out = capsys.readouterr().out
    assert "plan" in out and "failed" in out and not list(workspace.rglob("*.fcproj"))
    assert "target_duration_seconds: 90" in (workspace / "project.yaml").read_text(encoding="utf-8")
    assert main(["status", "--output", str(workspace / "nowhere")]) == 2


def test_cli_overrides_and_config_file(media_dir, workspace, tmp_path, capsys):
    cfg = tmp_path / "p.yaml"
    cfg.write_text("project:\n  title: Hawaii\nmedia:\n  contact_sheet_frames: 0\n", encoding="utf-8")
    assert main(["index", "--media", str(media_dir), "--output", str(workspace), "--config", str(cfg), "--fingerprint", "sha256",
                 "--set", "media.thumbnail_max_side=200"]) == 0
    text = (workspace / "project.yaml").read_text(encoding="utf-8")
    assert "title: Hawaii" in text and "fingerprint: sha256" in text and "thumbnail_max_side: 200" in text
    assert not list((workspace / "media_index" / "contact_sheets").glob("asset_*.jpg"))  # per-video sheets disabled
    assert main(["index", "--media", str(media_dir), "--output", str(workspace), "--set", "media.fingerprint=md5"]) == 2


def test_status_reports_unfinished_after_interrupted_run(media_dir, workspace, capsys):
    index(media_dir, workspace)
    man_path = workspace / "manifest.json"
    man = json.loads(man_path.read_text(encoding="utf-8"))
    man["stages"]["thumbnails"]["status"] = "running"  # simulate a crash mid-stage
    man_path.write_text(json.dumps(man), encoding="utf-8")
    main(["status", "--output", str(workspace)])
    assert "next: `trip-vlog resume" in capsys.readouterr().out
    res = index(media_dir, workspace)
    assert res.stage_status["thumbnails"] == "succeeded"


def test_missing_ffprobe_degrades_gracefully(media_dir, workspace, monkeypatch, tmp_path):
    import trip_vlog.orchestrator as orch

    monkeypatch.setattr(orch, "find_ffprobe", lambda *_: None)
    res = index(media_dir, workspace)
    assert res.exit_code == 0 and res.stage_status["probe"] == "partial"
    assert asset_by_path(workspace, "still_1080p.png")["probe_status"] == "ok"  # images use Pillow
    assert "ffprobe not found" in asset_by_path(workspace, "landscape_5s.mp4")["probe_error"]
