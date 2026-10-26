"""Doubles for the critic tests: a fake critic endpoint (Anthropic-shaped HTTP) that reads ids out of the request text, reply builders and a config
builder. No network, no ffmpeg."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from analysis_helpers import ENV

from trip_vlog.config import parse_config
from trip_vlog.llm.factory import make_gateway
from trip_vlog.llm.transport import HttpResponse

CLIP_RE = re.compile(r"^([A-Za-z0-9_.-]+) \| (video|photo|image|audio) \| (moment_[0-9a-f]+)", re.M)
SCENE_RE = re.compile(r"^### (scene_\d+) \|", re.M)
TILE_RE = re.compile(r"^(tile_\d+) \| sheet (\d+) \| record ([0-9.]+) s \| ([^|]+) \| (scene_\d+) \| ([A-Za-z0-9_.-]+) \| (image supplied|image NOT supplied|frame not extracted)", re.M)
CHECK_RE = re.compile(r"^([a-z0-9_]+) \| (pass|warn|fail|skipped) \| ", re.M)
ALT_RE = re.compile(r"^(moment_[0-9a-f]+) \| (?:photo|video, usable [0-9.]+ s) \|", re.M)


def critic_config(qc: dict | None = None, workflow: dict | None = None, project: dict | None = None, llm: dict | None = None, **top: Any):
    raw: dict[str, Any] = {"llm": {"endpoints": {"main": {"api": "anthropic_messages", "model": "test-model"}}, **(llm or {})},
                           "qc": {"critic": "report", **(qc or {})}, "project": {"target_duration_seconds": 18.5, **(project or {})}}
    if workflow:
        raw["workflow"] = workflow
    raw.update(top)
    return parse_config(raw)


def gateway_for(cfg, ws_root: Path, transport, **kw):
    return make_gateway(cfg, ws_root, "critic", images=cfg.qc.critic_images, transport=transport, env=ENV, sleep=lambda s: None, mode="live", **kw)


def reply(obj: Any, *, stop: str = "end_turn", model: str = "test-model-resolved") -> HttpResponse:
    body = {"id": "msg_1", "type": "message", "model": model, "stop_reason": stop, "content": [{"type": "text", "text": json.dumps(obj)}],
            "usage": {"input_tokens": 2000, "output_tokens": 400}}
    return HttpResponse(200, {"request-id": "req_c"}, json.dumps(body).encode("utf-8"))


def change(op: str, **fields: Any) -> dict[str, Any]:
    c: dict[str, Any] = {"operation": op, "clip_instance_id": None, "scene_id": None, "duration_seconds": None, "anchor": None, "moment_id": None,
                         "to_position": None, "transition_type": None, "transition_seconds": None, "audio": None, "shorten_by_seconds": None,
                         "reason": "Tighter."}
    c.update(fields)
    return c


def finding(*, severity: str = "minor", category: str = "pacing", summary: str = "The clip drags.", scenes: list[str] | None = None, clips: list[str] | None = None,
            evidence: list[dict[str, str]] | None = None, proposed: dict[str, Any] | None = None) -> dict[str, Any]:
    clips = clips or []
    ref = clips[0] if clips else scenes[0] if scenes else None
    ev = evidence if evidence is not None else [{"kind": "plan", "ref": ref, "note": "It is long."}] if ref else [{"kind": "qc_check", "ref": "total_duration", "note": "On target."}]
    return {"severity": severity, "category": category, "summary": summary, "scene_ids": scenes or [], "clip_instance_ids": clips, "evidence": ev,
            "proposed_change": proposed}


@dataclass
class CriticCall:
    texts: list[str]
    images: int
    clips: list[str]
    scenes: list[str]
    tiles: list[tuple[str, int, str]]       # tile id, sheet number, state
    checks: list[str]
    alternatives: list[str]
    body: dict[str, Any]
    retry: bool = False


@dataclass
class CriticTransport:
    """Fake critic endpoint. `respond(call)` returns the reply object (a dict) or an HttpResponse; default: one info finding citing the first clip."""

    respond: Callable[[CriticCall], Any] | None = None
    calls: list[CriticCall] = field(default_factory=list)

    def send(self, request) -> HttpResponse:
        body = json.loads(request.body.decode("utf-8"))
        msgs = body["messages"]
        blocks = msgs[0]["content"]
        texts = [b["text"] for b in blocks if b["type"] == "text"]
        joined = "\n".join(texts)
        call = CriticCall(texts, sum(1 for b in blocks if b["type"] == "image"), list(dict.fromkeys(m[0] for m in CLIP_RE.findall(joined))),
                          list(dict.fromkeys(SCENE_RE.findall(joined))), [(m[0], int(m[1]), m[6]) for m in TILE_RE.findall(joined)],
                          list(dict.fromkeys(m[0] for m in CHECK_RE.findall(joined))),
                          list(dict.fromkeys(ALT_RE.findall(joined))), body, len(msgs) > 1)
        self.calls.append(call)
        if self.respond is not None:
            out = self.respond(call)
        else:
            out = {"summary": "A decent draft.", "findings": [finding(severity="info", category="other", summary="Fine.", clips=call.clips[:1])]}
        return out if isinstance(out, HttpResponse) else reply(out)


# ---- preview sheet index + critic inputs (no ffmpeg) ---------------------------------------------------------
def make_index(plan, ws: Path, per_sheet: int = 4):
    """A PreviewSheetIndex for `plan` with real (tiny) JPEG sheets under ws/qc/. Returns (index, {sheet number: path})."""
    from fractions import Fraction

    from PIL import Image

    from trip_vlog.qc.preview import CutClip, PreviewSheetIndex, SheetEntry, TileEntry, plan_tiles, split_sheets

    fps = Fraction(plan.sequence.fps)
    clips = [CutClip(c.clip_instance_id, s.scene_id, c.timeline_start_frame, c.timeline_start_frame + c.duration_frames)
             for s in plan.scenes for c in s.clips if c.track_role == "primary_video"]
    tp = plan_tiles(clips, fps, plan.total_duration_frames, max_per_sheet=per_sheet)
    idx = PreviewSheetIndex("preview/draft.mp4", plan.plan_hash, str(fps), plan.total_duration_frames, 4, 320, 0.15, cuts=list(tp.cuts),
                            cuts_total=tp.cuts_total, cuts_sampled=tp.cuts_sampled)
    paths: dict[int, Path] = {}
    (ws / "qc").mkdir(parents=True, exist_ok=True)
    for n, tiles in enumerate(split_sheets(tp.groups, per_sheet), 1):
        p = ws / "qc" / f"sheet_{n:02d}.jpg"
        Image.new("RGB", (64, 48), (30 * n % 255, 80, 120)).save(p, format="JPEG")
        paths[n] = p
        idx.sheets.append(SheetEntry(n, p.relative_to(ws).as_posix(), [t.tile_id for t in tiles]))
        for pos, t in enumerate(tiles):
            idx.tiles[t.tile_id] = TileEntry(t.tile_id, n, pos, pos // 4, pos % 4, t.record_seconds, t.record_frame, t.clip_instance_id, t.scene_id, t.side, t.cut_index)
    return idx, paths


QC_DOC = {"status": "warn", "counts": {"pass": 2, "warn": 1, "fail": 0, "skipped": 0}, "plan_hash": "x",
          "checks": [{"name": "total_duration", "status": "pass", "detail": "18.50s vs target 18.5s"},
                     {"name": "orientation", "status": "warn", "detail": "1 portrait clip(s) in a landscape sequence", "data": {"clips": ["c4"]}},
                     {"name": "black_frames", "status": "pass", "detail": "no black intervals detected"}]}


# ---- a workspace with a finished draft, without FilmCraft or ffmpeg --------------------------------------------
from types import SimpleNamespace  # noqa: E402

from planning_helpers import make_catalog, mid, sel_clip, sel_plan  # noqa: E402

from trip_vlog.logging_utils import RunLogger  # noqa: E402
from trip_vlog.planning.models import to_json_dict  # noqa: E402
from trip_vlog.planning.resolve_plan import PlanSettings, resolve_selection_plan  # noqa: E402
from trip_vlog.state import Manifest, Workspace, atomic_write_json  # noqa: E402

CAT = make_catalog()
A0, A1, A2 = (mid(CAT, "day1/a.mp4", i) for i in range(3))
B0 = mid(CAT, "day1/b.mp4", 0)
P0, P1 = (mid(CAT, "day2/p.mp4", i) for i in range(2))
STILL = mid(CAT, "day1/c.jpg")


class _FakeCatalog:
    def list_assets(self, *a, **k):
        return []

    def list_transcripts(self, *a, **k):
        return []


def draft_selection():
    sel = sel_plan([sel_clip("c1", A0, 5), sel_clip("c2", A1, 4), sel_clip("c3", B0, 3)], [sel_clip("c4", P0, 3.5, audio="muted"), sel_clip("c5", STILL, 3)])
    return sel.model_copy(update={"schema_version": "1.1"})


def write_qc(ws: Path, plan, status: str = "warn") -> None:
    doc = {**QC_DOC, "status": status, "plan_hash": plan.plan_hash, "preview": "draft.mp4", "contact_sheet": {"index": "qc/preview_sheet.json"},
           "critic": {"status": "not_run", "reason": "deterministic checks only"}, "generated_at": "t"}
    atomic_write_json(ws / "preview" / "qc_report.json", doc)


def make_draft_ctx(tmp_path: Path, monkeypatch, cfg, *, approval_mode: str = "manual"):
    """RunContext stand-in over a workspace that holds a finished baseline-style draft: plan files, QC report, preview sheet index and images."""
    from trip_vlog.planning.catalog_view import PlanningCatalog

    monkeypatch.setattr(PlanningCatalog, "from_catalog", classmethod(lambda cls, c: CAT))
    ws = Workspace(tmp_path / "ws")
    ws.ensure_dirs()
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    sel = draft_selection()
    plan = resolve_selection_plan(sel, CAT, PlanSettings.from_config(cfg))
    (ws.root / "plans").mkdir(exist_ok=True)
    atomic_write_json(ws.root / "plans" / "selection_plan.json", to_json_dict(sel))
    atomic_write_json(ws.root / "plans" / "edit_plan.json", to_json_dict(plan))
    idx, _ = make_index(plan, ws.root)
    atomic_write_json(ws.root / "qc" / "preview_sheet.json", idx.to_dict())
    write_qc(ws.root, plan)
    (ws.root / "preview" / "draft.mp4").write_bytes(b"not a real video")
    manifest = Manifest(ws.manifest_path)
    manifest.set(approval={"status": "approved", "mode": approval_mode, "plan_hash": plan.plan_hash}, pipeline="draft", planner="baseline", media_root=str(media))
    manifest.save()
    outputs = {"edit_plan_file": ws.root / "plans" / "edit_plan.json", "qc_dir": ws.root / "qc", "preview_file": ws.root / "preview" / "draft.mp4",
               "project_file": ws.root / "filmcraft" / "TripVlog.fcproj", "interchange_dir": ws.root / "resolve_handoff"}
    ctx = SimpleNamespace(config=cfg, workspace=ws, catalog=_FakeCatalog(), logger=RunLogger(ws.run_log, ws.errors_log, "run-test"), manifest=manifest, ffmpeg=None,
                          ffprobe=None, paths=SimpleNamespace(media_root=media, workspace=ws.root, outputs=outputs), data={}, echo=lambda *_: None)
    return ctx, sel, plan


def stub_rebuild(monkeypatch, *, fail_assemble: str | None = None, reject_plan: bool = False, preview_status=None, qc_failed: list[str] | None = None,
                 qc_raises: bool = False, preview_raises: BaseException | None = None):
    """Replace the FilmCraft-facing stages the critic calls after a fix. Returns the list of calls (plan hash on disk at the time, in order)."""
    from trip_vlog import draft_stages as ds
    from trip_vlog.logging_utils import Status
    from trip_vlog.state import StageResult

    calls: list[tuple[str, str]] = []

    def assemble(ctx):
        calls.append(("assemble", ds.plan_file_hash(ds._pp(ctx).edit_plan)))
        mp = ctx.workspace.root / "filmcraft" / "mapping_report.json"      # the real stage writes it before it builds
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps({"plan_hash": ds.plan_file_hash(ds._pp(ctx).edit_plan)}), encoding="utf-8")
        if fail_assemble:     # a plan rejection says why in `errors`, like the real stage; a failure of the environment does not
            return StageResult(Status.FAILED, {"errors": [{"code": "x"}], "plan_rejected": True} if reject_plan else {}, fail_assemble)
        return StageResult(Status.SUCCEEDED, {"project": f"filmcraft/Trip Vlog Draft_v{len([c for c in calls if c[0] == 'assemble']) + 1:03d}.fcproj",
                                              "version": len([c for c in calls if c[0] == "assemble"]) + 1})

    def preview(ctx):
        calls.append(("preview", ds.plan_file_hash(ds._pp(ctx).edit_plan)))
        if preview_raises is not None:
            raise preview_raises
        ds._preview_path(ctx).write_bytes(b"new preview")
        return StageResult(preview_status or Status.SUCCEEDED, {"preview": "preview/draft.mp4"})

    def qc(ctx):
        h = ds.plan_file_hash(ds._pp(ctx).edit_plan)
        calls.append(("qc", h))
        if qc_raises:
            raise RuntimeError("qc crashed")
        plan = ds._prepare(ctx).plan
        write_qc(ctx.workspace.root, plan, "fail" if qc_failed else "warn")
        if qc_failed:      # the report names the failing checks too, as the real stage's does (the critic's next review reads it)
            qp = ctx.workspace.root / "preview" / "qc_report.json"
            doc = json.loads(qp.read_text(encoding="utf-8"))
            doc["checks"] = [{**c, "status": "fail" if c["name"] in qc_failed else c["status"]} for c in doc["checks"]]
            doc["checks"] += [{"name": n, "status": "fail", "detail": "x"} for n in qc_failed if n not in {c["name"] for c in doc["checks"]}]
            atomic_write_json(qp, doc)
        return StageResult(Status.SUCCEEDED, {"status": "fail" if qc_failed else "warn", "failed_checks": list(qc_failed or []), "warned_checks": ["orientation"]})

    monkeypatch.setattr(ds, "assemble_stage", assemble)
    monkeypatch.setattr(ds, "preview_stage", preview)
    monkeypatch.setattr(ds, "qc_stage", qc)
    return calls
