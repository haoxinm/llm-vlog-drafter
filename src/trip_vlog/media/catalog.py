"""SQLite catalog: the single source of truth (DESIGN §5.1, §6). JSONL files are exports only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from trip_vlog.logging_utils import utc_now_iso
from trip_vlog.media.ids import fingerprint_algorithm
from trip_vlog.state import atomic_write_text

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
    CREATE TABLE assets (
        asset_id TEXT PRIMARY KEY,
        relative_path TEXT NOT NULL UNIQUE,
        media_type TEXT,
        extension TEXT,
        size_bytes INTEGER,
        mtime_ns INTEGER,
        content_version TEXT,
        sha256 TEXT,
        duration_seconds REAL,
        width INTEGER, height INTEGER,
        display_width INTEGER, display_height INTEGER,
        frame_rate_num INTEGER, frame_rate_den INTEGER,
        is_vfr INTEGER NOT NULL DEFAULT 0,
        rotation_degrees INTEGER NOT NULL DEFAULT 0,
        codec TEXT, pix_fmt TEXT, hdr TEXT,
        audio_streams INTEGER,
        capture_time TEXT, capture_time_source TEXT,
        gps_json TEXT,                      -- private
        format_name TEXT,
        technical_notes_json TEXT NOT NULL DEFAULT '[]',
        probe_status TEXT NOT NULL DEFAULT 'pending',   -- ok | failed | pending
        probe_error TEXT,
        analysis_status TEXT NOT NULL DEFAULT 'pending', -- pending | complete | stale | failed
        present INTEGER NOT NULL DEFAULT 1,
        first_seen TEXT, last_seen TEXT,
        content_changed_at TEXT, previous_content_version TEXT,
        thumbnail_path TEXT, thumbnail_error TEXT, contact_sheet_path TEXT
    );
    CREATE INDEX idx_assets_content_version ON assets(content_version);
    CREATE TABLE analysis_cache (
        asset_id TEXT NOT NULL,
        analyzer TEXT NOT NULL,
        cache_key TEXT NOT NULL,            -- analyzer/model version + relevant settings
        content_version TEXT,
        status TEXT NOT NULL,               -- ok | stale | failed
        result_json TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        PRIMARY KEY (asset_id, analyzer, cache_key)
    );
    CREATE TABLE segments (
        segment_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, start_seconds REAL, end_seconds REAL,
        content_version TEXT, data_json TEXT
    );
    CREATE TABLE moments (
        moment_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, segment_id TEXT,
        source_in_seconds REAL, source_out_seconds REAL, content_version TEXT, data_json TEXT
    );
    CREATE TABLE transcripts (
        transcript_id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, start_seconds REAL, end_seconds REAL,
        text TEXT, confidence REAL, content_version TEXT, data_json TEXT
    );
    """),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]

_PROBE_COLS = (
    "media_type", "duration_seconds", "width", "height", "display_width", "display_height", "frame_rate_num",
    "frame_rate_den", "is_vfr", "rotation_degrees", "codec", "pix_fmt", "hdr", "audio_streams", "capture_time",
    "capture_time_source", "gps_json", "format_name", "technical_notes_json", "probe_status", "probe_error",
)


class Catalog:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Catalog": return self
    def __exit__(self, *exc: Any) -> None: self.close()

    # -- schema ----------------------------------------------------------------------------
    def migrate(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
        done = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations")}
        for version, script in MIGRATIONS:
            if version in done:
                continue
            self.conn.executescript(script)
            with self.conn:
                self.conn.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)", (version, utc_now_iso()))

    def schema_version(self) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()[0]

    # -- analysis cache --------------------------------------------------------------------
    def cache_get(self, asset_id: str, analyzer: str, cache_key: str, content_version: str | None) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM analysis_cache WHERE asset_id=? AND analyzer=? AND cache_key=?", (asset_id, analyzer, cache_key)
        ).fetchone()
        if row and row["status"] == "ok" and row["content_version"] == content_version and row["result_json"]:
            return json.loads(row["result_json"])
        return None

    def cache_put(self, asset_id: str, analyzer: str, cache_key: str, content_version: str | None,
                  result: dict[str, Any], status: str = "ok", error: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO analysis_cache(asset_id, analyzer, cache_key, content_version, status, result_json, error, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (asset_id, analyzer, cache_key, content_version, status, json.dumps(result, sort_keys=True), error, utc_now_iso()),
            )

    # -- assets ----------------------------------------------------------------------------
    def get_asset(self, asset_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM assets WHERE asset_id=?", (asset_id,)).fetchone()

    def list_assets(self, present_only: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM assets" + (" WHERE present=1" if present_only else "") + " ORDER BY relative_path"
        return self.conn.execute(q).fetchall()

    def upsert_asset(self, rec: dict[str, Any]) -> str:
        """Insert/update an asset. Returns 'new' | 'unchanged' | 'changed' | 'reappeared' | 'algorithm_changed'.

        A changed content_version marks analysis_status 'stale' (the asset_id never changes).
        """
        now = utc_now_iso()
        old = self.get_asset(rec["asset_id"])
        probe_ok = rec.get("probe_status") == "ok"
        fields = {c: rec.get(c) for c in _PROBE_COLS}
        fields.update(relative_path=rec["relative_path"], extension=rec.get("extension"), size_bytes=rec.get("size_bytes"),
                      mtime_ns=rec.get("mtime_ns"), content_version=rec.get("content_version"), sha256=rec.get("sha256"),
                      present=1, last_seen=now)
        if old is None:
            fields.update(first_seen=now, analysis_status="complete" if probe_ok else "failed")
            kind = "new"
        else:
            old_cv, new_cv = old["content_version"], rec.get("content_version")
            status = old["analysis_status"]
            kind = "unchanged" if old["present"] else "reappeared"
            if old_cv and new_cv and old_cv != new_cv:
                if fingerprint_algorithm(old_cv) != fingerprint_algorithm(new_cv):
                    kind = "algorithm_changed"  # fingerprint mode switched: cannot tell if content changed
                else:
                    kind = "changed"
                    status = "stale"
                    fields.update(content_changed_at=now, previous_content_version=old_cv)
                    self.conn.execute("UPDATE analysis_cache SET status='stale' WHERE asset_id=? AND content_version IS NOT ?",
                                      (rec["asset_id"], new_cv))
            if not probe_ok:
                status = "failed"
            elif status == "failed":
                status = "complete"
            fields["analysis_status"] = status
        cols = ["asset_id", *fields]
        with self.conn:
            if old is None:
                self.conn.execute(f"INSERT INTO assets({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                                  [rec["asset_id"], *fields.values()])
            else:
                sets = ",".join(f"{k}=?" for k in fields)
                self.conn.execute(f"UPDATE assets SET {sets} WHERE asset_id=?", [*fields.values(), rec["asset_id"]])
        return kind

    def mark_missing(self, present_ids: Iterable[str]) -> list[str]:
        """Flag assets no longer on disk (never deleted: plans may still reference them)."""
        present = set(present_ids)
        gone = [r["asset_id"] for r in self.conn.execute("SELECT asset_id FROM assets WHERE present=1") if r["asset_id"] not in present]
        with self.conn:
            for aid in gone:
                self.conn.execute("UPDATE assets SET present=0 WHERE asset_id=?", (aid,))
        return gone

    def set_artifacts(self, asset_id: str, *, thumbnail_path: str | None = None, thumbnail_error: str | None = None,
                      contact_sheet_path: str | None = None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE assets SET thumbnail_path=?, thumbnail_error=?, contact_sheet_path=COALESCE(?, contact_sheet_path) WHERE asset_id=?",
                (thumbnail_path, thumbnail_error, contact_sheet_path, asset_id))

    def mark_analysis_complete(self, asset_id: str) -> None:
        """For later phases: clears 'stale' once dependent analysis was recomputed."""
        with self.conn:
            self.conn.execute("UPDATE assets SET analysis_status='complete' WHERE asset_id=? AND probe_status='ok'", (asset_id,))

    def probable_duplicates(self) -> list[dict[str, Any]]:
        """Present assets at different paths sharing a content_version (reported, never merged)."""
        out = []
        for r in self.conn.execute(
            "SELECT content_version, group_concat(relative_path, char(10)) AS paths, COUNT(*) AS n FROM assets "
            "WHERE present=1 AND content_version IS NOT NULL GROUP BY content_version HAVING n > 1 ORDER BY content_version"
        ):
            out.append({"content_version": r["content_version"], "paths": sorted(r["paths"].split("\n"))})
        return out

    def probable_moves(self) -> list[dict[str, Any]]:
        """A present asset whose content_version equals that of an asset no longer present at its old path."""
        rows = self.conn.execute(
            "SELECT a.relative_path AS new_path, b.relative_path AS old_path, a.content_version AS cv "
            "FROM assets a JOIN assets b ON a.content_version = b.content_version AND a.asset_id != b.asset_id "
            "WHERE a.present=1 AND b.present=0 AND a.content_version IS NOT NULL ORDER BY a.relative_path").fetchall()
        return [{"from": r["old_path"], "to": r["new_path"], "content_version": r["cv"]} for r in rows]

    def stale_assets(self) -> list[str]:
        return [r["relative_path"] for r in self.conn.execute(
            "SELECT relative_path FROM assets WHERE present=1 AND analysis_status='stale' ORDER BY relative_path")]

    # -- segments and moments (Phase 2) ------------------------------------------------------
    def replace_segments(self, asset_id: str, content_version: str | None, segments: list[dict[str, Any]]) -> None:
        """Replace all segments of an asset. Each dict needs segment_id, start_seconds, end_seconds (None for stills)
        and may carry any other JSON-able fields (stored in data_json)."""
        with self.conn:
            self.conn.execute("DELETE FROM segments WHERE asset_id=?", (asset_id,))
            for s in segments:
                self.conn.execute(
                    "INSERT INTO segments(segment_id, asset_id, start_seconds, end_seconds, content_version, data_json) VALUES (?,?,?,?,?,?)",
                    (s["segment_id"], asset_id, s.get("start_seconds"), s.get("end_seconds"), content_version,
                     json.dumps(s, sort_keys=True, ensure_ascii=False)))

    def list_segments(self, asset_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM segments" + (" WHERE asset_id=?" if asset_id else "")
        q += " ORDER BY asset_id, start_seconds, segment_id"
        return self.conn.execute(q, (asset_id,) if asset_id else ()).fetchall()

    def replace_moments(self, asset_id: str, content_version: str | None, moments: list[dict[str, Any]]) -> None:
        """Make `moments` the baseline moments of an asset. Moments enriched by later phases (data 'source' other than
        'baseline') are never overwritten or removed here."""
        keep = {m["moment_id"] for m in moments}
        with self.conn:
            for r in self.conn.execute("SELECT moment_id, data_json FROM moments WHERE asset_id=?", (asset_id,)).fetchall():
                data = json.loads(r["data_json"] or "{}")
                if data.get("source", "baseline") == "baseline" and r["moment_id"] not in keep:
                    self.conn.execute("DELETE FROM moments WHERE moment_id=?", (r["moment_id"],))
            for m in moments:
                old = self.conn.execute("SELECT data_json FROM moments WHERE moment_id=?", (m["moment_id"],)).fetchone()
                if old is not None and json.loads(old["data_json"] or "{}").get("source", "baseline") != "baseline":
                    continue
                self.conn.execute(
                    "INSERT OR REPLACE INTO moments(moment_id, asset_id, segment_id, source_in_seconds, source_out_seconds, content_version, data_json)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (m["moment_id"], asset_id, m.get("segment_id"), m.get("source_in_seconds"), m.get("source_out_seconds"),
                     content_version, json.dumps(m, sort_keys=True, ensure_ascii=False)))

    def list_moments(self, asset_id: str | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM moments" + (" WHERE asset_id=?" if asset_id else "")
        q += " ORDER BY asset_id, source_in_seconds, moment_id"
        return self.conn.execute(q, (asset_id,) if asset_id else ()).fetchall()

    def get_moment(self, moment_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM moments WHERE moment_id=?", (moment_id,)).fetchone()

    def export_moments_jsonl(self, path: Path) -> int:
        """moments.jsonl: read-only export (DESIGN §6.3), ordered by relative path then source_in."""
        rows = self.conn.execute(
            "SELECT m.data_json FROM moments m JOIN assets a ON a.asset_id=m.asset_id "
            "ORDER BY a.relative_path, m.source_in_seconds, m.moment_id").fetchall()
        text = "".join(json.dumps(json.loads(r["data_json"]), ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
        atomic_write_text(path, text)
        return len(rows)

    # -- export ----------------------------------------------------------------------------
    @staticmethod
    def asset_export(row: sqlite3.Row, include_gps: bool = False) -> dict[str, Any]:
        """Asset record per DESIGN §6.2 plus content_version, display dims, technical_notes."""
        gps = json.loads(row["gps_json"]) if row["gps_json"] else None
        return {
            "asset_id": row["asset_id"],
            "relative_path": row["relative_path"],
            "media_type": row["media_type"],
            "content_version": row["content_version"],
            "sha256": row["sha256"],
            "size_bytes": row["size_bytes"],
            "duration_seconds": row["duration_seconds"],
            "width": row["width"],
            "height": row["height"],
            "display_width": row["display_width"],
            "display_height": row["display_height"],
            "frame_rate_num": row["frame_rate_num"],
            "frame_rate_den": row["frame_rate_den"],
            "is_vfr": bool(row["is_vfr"]),
            "rotation_degrees": row["rotation_degrees"],
            "codec": row["codec"],
            "pix_fmt": row["pix_fmt"],
            "hdr": row["hdr"],
            "capture_time": row["capture_time"],
            "gps": gps if include_gps else None,
            "has_gps": gps is not None,
            "audio_streams": row["audio_streams"],
            "technical_notes": json.loads(row["technical_notes_json"] or "[]"),
            "probe_status": row["probe_status"],
            "probe_error": row["probe_error"],
            "analysis_status": row["analysis_status"],
            "present": bool(row["present"]),
            "thumbnail": row["thumbnail_path"],
            "contact_sheet": row["contact_sheet_path"],
        }

    def export_assets_jsonl(self, path: Path, include_gps: bool = False) -> int:
        rows = self.list_assets(present_only=False)
        text = "".join(json.dumps(self.asset_export(r, include_gps), ensure_ascii=False, sort_keys=True) + "\n" for r in rows)
        atomic_write_text(path, text)
        return len(rows)
