"""Workspace layout, atomic writes, manifest.json and a generic stage runner."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from trip_vlog import __version__
from trip_vlog.logging_utils import RunLogger, Status, utc_now_iso
from trip_vlog.paths import long_path

MANIFEST_SCHEMA = 1


def atomic_write_text(path: Path, text: str) -> None:
    """Write UTF-8 with '\\n' newlines via temp file + os.replace (retries briefly on Windows locks)."""
    path = Path(path)
    long_path(path.parent).mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(long_path(tmp), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(8):
            try:
                os.replace(long_path(tmp), long_path(path))
                return
            except PermissionError:
                if attempt == 7:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if long_path(tmp).exists():
            try:
                long_path(tmp).unlink()
            except OSError:
                pass


def atomic_write_json(path: Path, obj: Any) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def stable_hash(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def manifest_path(self) -> Path: return self.root / "manifest.json"
    @property
    def project_yaml(self) -> Path: return self.root / "project.yaml"
    @property
    def readme(self) -> Path: return self.root / "README.md"
    @property
    def media_index(self) -> Path: return self.root / "media_index"
    @property
    def catalog_path(self) -> Path: return self.media_index / "catalog.sqlite"
    @property
    def assets_jsonl(self) -> Path: return self.media_index / "assets.jsonl"
    @property
    def inventory_report(self) -> Path: return self.media_index / "inventory_report.json"
    @property
    def thumbnails_dir(self) -> Path: return self.media_index / "thumbnails"
    @property
    def contact_sheets_dir(self) -> Path: return self.media_index / "contact_sheets"
    @property
    def tmp_dir(self) -> Path: return self.media_index / "tmp"
    @property
    def analysis_dir(self) -> Path: return self.media_index / "analysis"
    @property
    def moments_jsonl(self) -> Path: return self.media_index / "moments.jsonl"
    @property
    def transcripts_jsonl(self) -> Path: return self.media_index / "transcripts.jsonl"
    @property
    def audio_dir(self) -> Path: return self.media_index / "audio"
    @property
    def logs_dir(self) -> Path: return self.root / "logs"
    @property
    def run_log(self) -> Path: return self.logs_dir / "run.jsonl"
    @property
    def errors_log(self) -> Path: return self.logs_dir / "errors.jsonl"

    def ensure_dirs(self) -> None:
        for d in (self.root, self.media_index, self.thumbnails_dir, self.contact_sheets_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)


def platform_info() -> dict[str, str]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
    }


class Manifest:
    """manifest.json: tool/platform/tool versions, config hash and per-stage status."""

    def __init__(self, path: Path, data: dict[str, Any] | None = None):
        self.path = Path(path)
        self.data: dict[str, Any] = data or {
            "schema_version": MANIFEST_SCHEMA,
            "tool": "trip-vlog",
            "tool_version": __version__,
            "platform": platform_info(),
            "created_at": utc_now_iso(),
            "stages": {},
        }

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        path = Path(path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict) and isinstance(data.get("stages"), dict):
                    return cls(path, data)
            except (OSError, ValueError):
                pass
            try:  # unreadable manifest: keep it for inspection, start fresh
                os.replace(path, path.with_name(path.name + ".corrupt"))
            except OSError:
                pass
        return cls(path)

    def save(self) -> None:
        self.data["updated_at"] = utc_now_iso()
        atomic_write_json(self.path, self.data)

    def __getitem__(self, key: str) -> Any: return self.data[key]
    def get(self, key: str, default: Any = None) -> Any: return self.data.get(key, default)
    def set(self, **kw: Any) -> None: self.data.update(kw)

    @property
    def stages(self) -> dict[str, dict[str, Any]]:
        return self.data["stages"]

    def stage(self, name: str) -> dict[str, Any]:
        return self.stages.get(name, {})


@dataclass
class StageResult:
    status: Status = Status.SUCCEEDED
    summary: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass
class Stage:
    name: str
    fn: Callable[[Any], StageResult]
    deps: tuple[str, ...] = ()
    inputs: Callable[[Any], Any] | None = None  # JSON-able description of inputs; hashed for skip decisions
    always_run: bool = False  # cheap detection stages (e.g. scan) that must see the current disk state
    verify: Callable[[Any], bool] | None = None  # extra cache-hit condition (e.g. the artifact still exists on disk)


class StageRunner:
    """Runs registered stages in registration order.

    A stage is skipped (cache hit) when the manifest says it succeeded with the same input hash.
    A stage whose dependency failed or was skipped-due-to-failure is skipped with an explanation.
    Phase 2 registers more stages with `register(...)`; nothing here is index-specific.
    """

    def __init__(self, manifest: Manifest, logger: RunLogger, ctx: Any):
        self.manifest, self.logger, self.ctx = manifest, logger, ctx
        self._stages: dict[str, Stage] = {}
        self._blocked: set[str] = set()
        self._waiting: set[str] = set()

    def register(self, name: str, fn: Callable[[Any], StageResult], *, deps: tuple[str, ...] = (),
                 inputs: Callable[[Any], Any] | None = None, always_run: bool = False,
                 verify: Callable[[Any], bool] | None = None) -> None:
        if name in self._stages:
            raise ValueError(f"stage already registered: {name}")
        for d in deps:
            if d not in self._stages:
                raise ValueError(f"stage {name!r} depends on unregistered stage {d!r}")
        self._stages[name] = Stage(name, fn, tuple(deps), inputs, always_run, verify)

    @property
    def names(self) -> list[str]:
        return list(self._stages)

    def run(self, only: list[str] | None = None, force: bool = False) -> dict[str, Status]:
        results: dict[str, Status] = {}
        for stage in self._stages.values():
            if only is not None and stage.name not in only:
                continue
            results[stage.name] = self._run_one(stage, results, force)
        return results

    def _verified(self, stage: Stage) -> bool:
        try:
            return bool(stage.verify(self.ctx))  # type: ignore[misc]
        except Exception:  # noqa: BLE001 - cannot confirm the artifact: run the stage
            return False

    def _record(self, name: str, **kw: Any) -> None:
        entry = self.manifest.stages.setdefault(name, {})
        entry.update(kw)
        self.manifest.save()

    def _run_one(self, stage: Stage, results: dict[str, Status], force: bool) -> Status:
        name = stage.name
        blocked = [d for d in stage.deps if results.get(d) in (Status.FAILED, Status.AWAITING_APPROVAL) or d in self._blocked]
        if blocked:
            self._blocked.add(name)
            waiting = any(results.get(d) == Status.AWAITING_APPROVAL or d in self._waiting for d in blocked)
            if waiting:
                self._waiting.add(name)
                self._record(name, status=str(Status.SKIPPED), decision="awaiting_approval", error=None)
                self.logger.event(name, Status.SKIPPED, message="waiting for approval")
                return Status.SKIPPED
            self._record(name, status=str(Status.SKIPPED), decision="dependency_failed",
                         error=f"dependency failed: {', '.join(blocked)}")
            self.logger.event(name, Status.SKIPPED, message=f"dependency failed: {', '.join(blocked)}")
            return Status.SKIPPED
        input_hash = None
        if stage.inputs is not None:
            try:
                input_hash = stable_hash(stage.inputs(self.ctx))
            except Exception as e:  # inputs unavailable -> must run
                self.logger.event(name, Status.RUNNING, message=f"input hash unavailable: {e}")
        prev = self.manifest.stage(name)
        if (not force and not stage.always_run and input_hash is not None and prev.get("status") == Status.SUCCEEDED
                and prev.get("input_hash") == input_hash
                and (stage.verify is None or self._verified(stage))):
            self._record(name, decision="cache_hit", last_checked_at=utc_now_iso())
            self.logger.event(name, Status.SKIPPED, cache="hit", message="inputs unchanged")
            return Status.SKIPPED
        started = time.perf_counter()
        self._record(name, status=str(Status.RUNNING), decision="ran", started_at=utc_now_iso(),
                     input_hash=input_hash, error=None)
        self.logger.event(name, Status.RUNNING, cache="miss")
        try:
            res = stage.fn(self.ctx)
        except Exception as e:  # noqa: BLE001 - stage boundary
            res = StageResult(Status.FAILED, {}, f"{type(e).__name__}: {e}")
            self.logger.event(name, Status.FAILED, error=res.error, traceback=traceback.format_exc(limit=6))
        dur = time.perf_counter() - started
        status = Status(res.status)
        self._record(name, status=str(status), finished_at=utc_now_iso(), duration_seconds=round(dur, 3),
                     summary=res.summary, error=self.logger.clean(res.error) if res.error else None)
        self.logger.event(name, status, duration_ms=dur * 1000, summary=None, error=res.error if status == Status.FAILED else None)
        return status
