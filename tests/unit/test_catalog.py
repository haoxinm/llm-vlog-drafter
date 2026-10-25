from __future__ import annotations

import json
import sqlite3

from trip_vlog.media.catalog import SCHEMA_VERSION, Catalog


def rec(path="a/x.mp4", cv="fast:aaa", **kw):
    from trip_vlog.media.ids import asset_id_for

    base = {"asset_id": asset_id_for(path), "relative_path": path, "extension": ".mp4", "size_bytes": 10, "mtime_ns": 1,
            "content_version": cv, "sha256": None, "media_type": "video", "duration_seconds": 5.0, "width": 1920, "height": 1080,
            "display_width": 1920, "display_height": 1080, "frame_rate_num": 30000, "frame_rate_den": 1001, "is_vfr": 0,
            "rotation_degrees": 0, "codec": "h264", "audio_streams": 1, "technical_notes_json": "[]", "probe_status": "ok",
            "gps_json": json.dumps({"latitude": 1.0, "longitude": 2.0})}
    base.update(kw)
    return base


def test_schema_and_migration_idempotent(tmp_path):
    p = tmp_path / "media_index" / "catalog.sqlite"
    with Catalog(p) as c:
        assert c.schema_version() == SCHEMA_VERSION == 1
        tables = {r[0] for r in c.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"assets", "analysis_cache", "segments", "moments", "transcripts", "schema_migrations"} <= tables
    with Catalog(p) as c:  # reopen: no re-migration
        assert c.conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1


def test_upsert_new_unchanged_changed_stale(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        r = rec()
        assert c.upsert_asset(r) == "new"
        assert c.get_asset(r["asset_id"])["analysis_status"] == "complete"
        assert c.upsert_asset(r) == "unchanged"
        assert c.upsert_asset({**r, "content_version": "fast:bbb"}) == "changed"
        row = c.get_asset(r["asset_id"])
        assert row["analysis_status"] == "stale" and row["previous_content_version"] == "fast:aaa" and row["asset_id"] == r["asset_id"]
        assert c.stale_assets() == ["a/x.mp4"]
        assert c.upsert_asset({**r, "content_version": "fast:bbb"}) == "unchanged"
        assert c.get_asset(r["asset_id"])["analysis_status"] == "stale"  # stays stale until later phases re-analyse
        c.mark_analysis_complete(r["asset_id"])
        assert c.stale_assets() == []


def test_changed_content_invalidates_only_dependent_cache(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        a, b = rec("a.mp4", "fast:1"), rec("b.mp4", "fast:2")
        c.upsert_asset(a); c.upsert_asset(b)
        for r in (a, b):
            c.cache_put(r["asset_id"], "vision", "k", r["content_version"], {"x": 1})
        assert c.cache_get(a["asset_id"], "vision", "k", "fast:1") == {"x": 1}
        c.upsert_asset({**a, "content_version": "fast:9"})
        assert c.cache_get(a["asset_id"], "vision", "k", "fast:9") is None
        assert c.cache_get(a["asset_id"], "vision", "k", "fast:1") is None  # marked stale
        assert c.cache_get(b["asset_id"], "vision", "k", "fast:2") == {"x": 1}


def test_fingerprint_algorithm_switch_is_not_staleness(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        r = rec()
        c.upsert_asset(r)
        assert c.upsert_asset({**r, "content_version": "sha256:abc"}) == "algorithm_changed"
        assert c.get_asset(r["asset_id"])["analysis_status"] == "complete"


def test_failed_probe_then_recovery(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        r = rec(probe_status="failed", probe_error="corrupt")
        c.upsert_asset(r)
        assert c.get_asset(r["asset_id"])["analysis_status"] == "failed"
        c.upsert_asset({**r, "probe_status": "ok", "probe_error": None})
        assert c.get_asset(r["asset_id"])["analysis_status"] == "complete"


def test_missing_assets_kept_not_deleted(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        a, b = rec("a.mp4", "fast:1"), rec("b.mp4", "fast:2")
        c.upsert_asset(a); c.upsert_asset(b)
        assert c.mark_missing([a["asset_id"]]) == [b["asset_id"]]
        assert [r["relative_path"] for r in c.list_assets()] == ["a.mp4"]
        assert len(c.list_assets(present_only=False)) == 2
        assert c.upsert_asset(b) == "reappeared"


def test_duplicates_and_moves_reported_never_merged(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        c.upsert_asset(rec("a/x.mp4", "fast:same")); c.upsert_asset(rec("b/copy.mp4", "fast:same")); c.upsert_asset(rec("c/y.mp4", "fast:other"))
        assert c.probable_duplicates() == [{"content_version": "fast:same", "paths": ["a/x.mp4", "b/copy.mp4"]}]
        assert len(c.list_assets()) == 3 and len({r["asset_id"] for r in c.list_assets()}) == 3
        # move: old path vanishes, same fingerprint appears elsewhere
        c.upsert_asset(rec("moved/y.mp4", "fast:other"))
        c.mark_missing([r["asset_id"] for r in c.list_assets() if r["relative_path"] != "c/y.mp4"])
        assert c.probable_moves() == [{"from": "c/y.mp4", "to": "moved/y.mp4", "content_version": "fast:other"}]


def test_jsonl_export_hides_gps_by_default_and_is_regenerated(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        c.upsert_asset(rec())
        out = tmp_path / "assets.jsonl"
        assert c.export_assets_jsonl(out) == 1
        row = json.loads(out.read_text(encoding="utf-8"))
        assert row["gps"] is None and row["has_gps"] is True and row["content_version"] == "fast:aaa"
        assert {"asset_id", "relative_path", "media_type", "duration_seconds", "width", "height", "frame_rate_num", "frame_rate_den",
                "rotation_degrees", "capture_time", "audio_streams", "technical_notes", "analysis_status", "display_width"} <= row.keys()
        c.export_assets_jsonl(out, include_gps=True)
        assert json.loads(out.read_text(encoding="utf-8"))["gps"] == {"latitude": 1.0, "longitude": 2.0}
        out.write_text("garbage", encoding="utf-8")  # exports are never read back
        assert c.export_assets_jsonl(out) == 1 and json.loads(out.read_text(encoding="utf-8"))["asset_id"]


def test_cache_only_hits_ok_and_matching_version(tmp_path):
    with Catalog(tmp_path / "c.sqlite") as c:
        c.cache_put("asset_1", "probe", "k", "fast:1", {"v": 1})
        assert c.cache_get("asset_1", "probe", "k", "fast:1") == {"v": 1}
        assert c.cache_get("asset_1", "probe", "k", "fast:2") is None
        assert c.cache_get("asset_1", "probe", "other-key", "fast:1") is None
        c.cache_put("asset_1", "probe", "k", "fast:1", {}, status="failed", error="x")
        assert c.cache_get("asset_1", "probe", "k", "fast:1") is None
