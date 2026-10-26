"""Execution journal and the versioned, idempotent project build (DESIGN §12.2).

Policy: *versioned rebuilds*. ``build_project`` writes ``filmcraft/<ProjectName>_vNNN.fcproj``.
  * Same spec hash, same media, and the newest COMPLETED version's journal says it completed (and the file on disk is
    still the one that was verified) -> no-op (a failed attempt after it does not matter): FilmCraft is not even started, nothing is appended anywhere.
  * Different spec, changed media, or no completed run -> a NEW version; earlier
    versions are never touched. A plan is therefore never appended to an existing timeline.
The journal (``filmcraft/execution_journal.json``) is rewritten atomically after every checkpoint. Each run records the pid, host and start time of the
process building it; every read-modify-write of the journal happens under a short lock file (journal_lock.py) and reloads the journal first, so two
processes never overwrite each other's runs, and a build still running in another live process is never marked abandoned (a second build is refused).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from trip_vlog.filmcraft.errors import CommandError, FilmCraftError, VerificationFailed
from trip_vlog.filmcraft.journal_lock import journal_lock, owner_is_live, this_host
from trip_vlog.filmcraft.inspect import VerificationReport, verify_against_spec
from trip_vlog.filmcraft.mappings import ClipMap, MediaMap
from trip_vlog.filmcraft.timeline_spec import TimelineSpec, ensure_valid
from trip_vlog.logging_utils import utc_now_iso
from trip_vlog.paths import safe_filename
from trip_vlog.state import atomic_write_json

if TYPE_CHECKING:  # pragma: no cover
    from trip_vlog.filmcraft.adapter import FilmCraftAdapter

JOURNAL_SCHEMA = 1
_VERSION_RE = re.compile(r"_v(\d{3,})\.(?:fcproj|timeline_spec\.json)$")   # the project and its spec copy: either one reserves the number


@dataclass
class BuildResult:
    noop: bool
    version: int
    project_path: Path
    spec_hash: str
    run: dict[str, Any]
    verification: VerificationReport | None = None
    media_map: MediaMap | None = None
    clip_map: ClipMap | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def sequence_id(self) -> int | None:
        return self.run.get("sequence_id")


# ----------------------------------------------------------------------------
# journal file
# ----------------------------------------------------------------------------


def load_journal(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": JOURNAL_SCHEMA, "runs": []}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict) or not isinstance(doc.get("runs"), list):
            raise ValueError("not a journal")
        return doc
    except (ValueError, OSError):
        aside = path.with_name(f"{path.stem}.corrupt-{int(time.time())}{path.suffix}")
        try:
            os.replace(path, aside)
        except OSError:
            pass
        return {"schema_version": JOURNAL_SCHEMA, "runs": [], "recovered_from": aside.name}


def save_journal(path: Path, doc: dict[str, Any]) -> None:
    atomic_write_json(path, doc)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def media_signature(paths: list[str]) -> dict[str, list[int]]:
    """size + mtime_ns of each asset: a changed original must trigger a new version."""
    sig = {}
    for p in paths:
        st = os.stat(p)
        sig[p] = [st.st_size, st.st_mtime_ns]
    return sig


def project_filename(project_name: str, version: int) -> str:
    return f"{safe_filename(project_name, 60)}_v{version:03d}.fcproj"


def _runs_for(doc: dict[str, Any], project_name: str) -> list[dict[str, Any]]:
    return sorted((r for r in doc["runs"] if r.get("project_name") == project_name), key=lambda r: r["version"])


def next_version(doc: dict[str, Any], fc_dir: Path, project_name: str) -> int:
    seen = [r["version"] for r in _runs_for(doc, project_name)]
    prefix = safe_filename(project_name, 60) + "_v"
    if fc_dir.is_dir():
        for p in fc_dir.iterdir():
            m = _VERSION_RE.search(p.name)
            if m and p.name.startswith(prefix):
                seen.append(int(m[1]))
    return max(seen, default=0) + 1


def mark_abandoned(doc: dict[str, Any]) -> list[int]:
    """A run still marked ``running`` whose process is gone (same host, pid not alive; another host: older than the timeout; no pid recorded: an older
    version's run) was cut off (killed, power loss): mark it ``failed`` (``abandoned``) so it is never taken for the current project. Its files are not
    touched. A run whose process is alive is left alone. Returns the versions marked."""
    out: list[int] = []
    for r in doc["runs"]:
        if r.get("status") == "running" and not owner_is_live(r.get("pid"), r.get("host"), r.get("started_at")):
            r["status"] = "failed"
            r["abandoned"] = True
            r["finished_at"] = r.get("finished_at") or utc_now_iso()
            r.setdefault("errors", []).append({"at": utc_now_iso(), "op": (r.get("completed_ops") or ["start"])[-1], "type": "Interrupted",
                                               "message": "the build did not finish (the process stopped before it could record a result)"})
            out.append(r["version"])
    return out


def live_builds(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Runs still ``running`` (after mark_abandoned): a build of another live process."""
    return [r for r in doc["runs"] if r.get("status") == "running"]


def failed_builds(doc: dict[str, Any], project_name: str) -> list[dict[str, Any]]:
    """The builds of ``project_name`` that did not complete, oldest first, as small records for the manifest, ``status`` and the handoff report.
    A run still marked ``running`` is listed as ``interrupted`` (it is only mutated by the next build). Nothing here deletes or renames a file."""
    out = []
    for r in _runs_for(doc, project_name):
        if r.get("status") == "complete":
            continue
        err = (r.get("errors") or [{}])[-1]
        out.append({"version": r["version"], "project": r.get("project_path"), "status": "interrupted" if (r.get("status") == "running" or r.get("abandoned")) else "failed",
                    "failed_at_op": err.get("op"), "error": str(err.get("message") or "")[:300] or None, "at": r.get("finished_at") or r.get("started_at")})
    return out


def latest_complete_project(adapter: "FilmCraftAdapter") -> Path | None:
    doc = load_journal(adapter.journal_path)
    for r in reversed(_runs_for(doc, adapter.config.project_name)):
        if r.get("status") == "complete":
            p = adapter.workspace / r["project_path"]
            if p.is_file():
                return p
    return None


def _newest_complete(doc: dict[str, Any], project_name: str) -> dict[str, Any] | None:
    for r in reversed(_runs_for(doc, project_name)):
        if r.get("status") == "complete":
            return r
    return None


def _is_noop(adapter: "FilmCraftAdapter", latest: dict[str, Any] | None, spec_hash: str, sig: dict[str, list[int]]) -> bool:
    if not latest or latest.get("status") != "complete" or latest.get("spec_hash") != spec_hash:
        return False
    proj = adapter.workspace / latest["project_path"]
    if not proj.is_file():
        return False
    if latest.get("media_signature") != sig:
        return False
    return latest.get("project_sha256") == sha256_file(proj)


# ----------------------------------------------------------------------------
# the build
# ----------------------------------------------------------------------------


def _new_run(adapter: "FilmCraftAdapter", spec: TimelineSpec, cmap: Any, project_name: str, version: int, project_path: Path, spec_copy: Path,
             spec_hash: str, sig: dict[str, list[int]]) -> dict[str, Any]:
    return {
        "version": version, "project_name": project_name, "status": "running", "pid": os.getpid(), "host": this_host(),
        "project_path": project_path.relative_to(adapter.workspace).as_posix(),
        "spec_hash": spec_hash, "spec_file": spec_copy.relative_to(adapter.workspace).as_posix(),
        "spec_summary": {"placements": len(spec.placements), "titles": len(spec.titles), "markers": len(spec.markers),
                         "tracks": list(spec.tracks), "end_frame": spec.end_frame, "fps": str(spec.fps)},
        "filmcraft_version": cmap.version, "started_at": utc_now_iso(), "finished_at": None,
        "media_signature": sig, "media_mapping": {}, "clip_mapping": {}, "sequence_id": None,
        "completed_ops": [], "last_checkpoint": None, "errors": [], "warnings": [],
        "project_sha256": None, "verified_after_reopen": False,
    }


def run_build(adapter: "FilmCraftAdapter", spec: TimelineSpec, project_name: str, *, force_new_version: bool = False) -> BuildResult:
    from trip_vlog.filmcraft.adapter import SequenceSettings

    ensure_valid(spec)  # structural errors never create a version
    for p in spec.asset_paths:
        rp = adapter.policy.check_read(p, "asset")
        if not rp.is_file():
            raise FilmCraftError(f"asset not found: {rp}")
    cmap = adapter.discover_capabilities()
    fc_dir = adapter.fc_dir
    fc_dir.mkdir(parents=True, exist_ok=True)
    jpath = adapter.journal_path
    spec_hash = spec.spec_hash()
    sig = media_signature(spec.asset_paths)
    noop_run: dict[str, Any] | None = None
    with journal_lock(jpath):
        doc = load_journal(jpath)
        if mark_abandoned(doc):
            save_journal(jpath, doc)
        latest = _newest_complete(doc, project_name)
        if not force_new_version and _is_noop(adapter, latest, spec_hash, sig):
            noop_run = latest
        elif (other := live_builds(doc)):
            o = other[0]
            raise FilmCraftError(f"another build is running on this workspace (version {o.get('version')}, pid {o.get('pid')} on {o.get('host')}, started {o.get('started_at')}); "
                                 f"wait for it to finish, then run the command again. If that process is gone, its run is marked interrupted automatically")
        else:
            version = next_version(doc, fc_dir, project_name)
            project_path = fc_dir / project_filename(project_name, version)
            spec_copy = fc_dir / f"{project_path.stem}.timeline_spec.json"
            run = _new_run(adapter, spec, cmap, project_name, version, project_path, spec_copy, spec_hash, sig)
            doc["runs"].append(run)
            save_journal(jpath, doc)       # the version number is reserved from here on

    if noop_run is not None:
        latest = noop_run
        adapter.project_path = adapter.workspace / latest["project_path"]
        adapter.sequence_id = latest.get("sequence_id")
        adapter.spec = spec
        return BuildResult(True, latest["version"], adapter.project_path, spec_hash, latest, None,
                           MediaMap.from_dict(latest.get("media_mapping", {})), ClipMap.from_dict(latest.get("clip_mapping", {})),
                           list(latest.get("warnings", [])))

    current = {"op": "start"}

    def flush() -> None:
        with journal_lock(jpath):
            cur = load_journal(jpath)          # reload: another process may have written its own runs since
            for i, r in enumerate(cur["runs"]):
                if r.get("version") == version and r.get("project_name") == project_name:
                    cur["runs"][i] = run
                    break
            else:
                cur["runs"].append(run)
            save_journal(jpath, cur)

    def done(op: str, checkpoint: str | None = None) -> None:
        run["completed_ops"].append(op)
        if checkpoint:
            run["last_checkpoint"] = checkpoint
        flush()

    try:
        atomic_write_json(spec_copy, spec.to_dict())
        current["op"] = "create_project"
        adapter.create_project(project_path, project_name)
        done("create_project", "created")

        current["op"] = "import_media"
        imp = adapter.import_media(spec.asset_paths)
        run["media_mapping"] = imp.media_map.to_dict()
        adapter.save_project()
        done(f"import_media:{imp.imported}", "imported")

        current["op"] = "create_sequence"
        seq = adapter.create_sequence(SequenceSettings.from_spec(spec))
        run["sequence_id"] = seq.sequence_id
        done("create_sequence")

        current["op"] = "apply_timeline"
        applied = adapter.apply_timeline(spec, imp.media_map, imp.durations)
        eff = applied.effective_spec or spec
        run["warnings"] += applied.warnings
        run["downgraded_dissolves"] = applied.downgraded_dissolves
        run["clip_mapping"] = applied.clip_map.to_dict()
        done(f"apply_timeline:{applied.placed}_placements")

        current["op"] = "verify_before_save"
        pre = adapter.verify(eff, imp.media_map)
        if not pre.ok:
            raise VerificationFailed("timeline read back before saving does not match the spec: " + pre.summary(), pre.mismatches)
        done("verify_live")

        current["op"] = "save_project"
        adapter.save_project()
        run["clip_mapping"] = pre.clip_map.to_dict()
        done("save_project", "assembled")

        # persistence check: hard-kill the server, then reopen the file in a NEW process and verify again
        current["op"] = "reopen_verify"
        adapter.kill_server()
        adapter.open_project(project_path)
        post = adapter.verify(eff, imp.media_map)
        if not post.ok:
            raise VerificationFailed("project reopened in a new process does not match the spec: " + post.summary(), post.mismatches)
        run["clip_mapping"] = post.clip_map.to_dict()
        run["sequence_id"] = adapter.sequence_id if adapter.sequence_id is not None else run["sequence_id"]
        run["project_sha256"] = sha256_file(project_path)
        run["verified_after_reopen"] = True
        run["status"] = "complete"
        run["finished_at"] = utc_now_iso()
        done("reopen_verify", "verified")
        adapter.spec = eff
        return BuildResult(False, version, project_path, spec_hash, run, post, imp.media_map, post.clip_map, list(run["warnings"]))
    except BaseException as e:
        err: dict[str, Any] = {"at": utc_now_iso(), "op": current["op"], "type": type(e).__name__,
                               "message": adapter.log.sanitizer.text(str(e))}
        if isinstance(e, CommandError):
            err["command"] = e.command
            err["params"] = adapter.log.sanitizer.obj(e.params)
        if isinstance(e, VerificationFailed):
            err["mismatches"] = adapter.log.sanitizer.obj([m.to_dict() for m in e.mismatches[:50]])
        run["errors"].append(err)
        run["status"] = "failed"
        run["finished_at"] = utc_now_iso()
        try:
            flush()
        finally:
            adapter.close()
        raise
