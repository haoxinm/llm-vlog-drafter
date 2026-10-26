"""Command line interface: index / analyze / transcribe / outline / llm-check / draft / critic / revise / approve / status / resume.

Exit codes
  0    success (pipeline finished; QC findings are reported, not errors)
  1    a stage failed (details in `status`, logs/errors.jsonl)
  2    usage or configuration error (bad flag, unusable paths)
  3    stopped at the approval gate: review plans/story_outline.md (`outline`) or plans/plan_summary.md (`draft`), or the critic revision under
       plans/revisions/ (qc.critic: apply with workflow.plan_review), then `approve` and `resume` (or `--auto-approve`); `revise` also stops
       here with a proposal (plans/revisions/revise_rNN.md) when a request is ambiguous or destructive: `approve --revision`, then `resume`
  4    `approve` refused: the outline/plan is missing, failed validation, or its stage did not succeed
       (`resolve-finish`: 0 finished, 1 required checks failed, 2 preconditions/arguments, 3 Resolve scripting unavailable)
  130  interrupted (Ctrl-C); `resume` continues
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

import yaml

from trip_vlog import __version__
from trip_vlog.config import ConfigError
from trip_vlog.draft_stages import DRAFT_STAGES
from trip_vlog.orchestrator import (ApprovalRefused, IndexResult, approve_pending, read_status, run_analyze, run_critic, run_draft, run_index,
                                    run_outline, run_revise, run_transcribe)


def _common(p: argparse.ArgumentParser, *, media_required: bool) -> None:
    p.add_argument("--media", type=Path, required=media_required, help="folder with the original photos/videos (read-only)")
    p.add_argument("--output", type=Path, required=True, help="output workspace folder (must be outside --media)")
    p.add_argument("--config", type=Path, help="project.yaml (default: <output>/project.yaml if present, else defaults)")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE",
                   help="override any config field, e.g. --set media.fingerprint=sha256 (repeatable)")
    p.add_argument("--fingerprint", choices=["fast", "sha256"], help="content fingerprint mode (media.fingerprint)")
    p.add_argument("--recursive", action=argparse.BooleanOptionalAction, default=None, help="scan sub-folders (media.recursive)")
    p.add_argument("--follow-symlinks", action=argparse.BooleanOptionalAction, default=None, help="follow symlinks (media.follow_symlinks)")
    p.add_argument("--ffprobe", help="path to ffprobe (media.ffprobe_path)")
    p.add_argument("--ffmpeg", help="path to ffmpeg (media.ffmpeg_path)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="trip-vlog", description="Draft a travel vlog from photos and videos.")
    ap.add_argument("--version", action="version", version=f"trip-vlog {__version__}")
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("index", help="inventory media: scan, probe, catalog, thumbnails")
    _common(p, media_required=True)

    p = sub.add_parser("analyze", help="label video segments and photos with the vision model -> candidate moments (runs index + segment first)")
    _common(p, media_required=False)
    p.add_argument("--limit", type=int, metavar="N", help="analyze only the first N assets (sorted by path)")
    p.add_argument("--sampling-fps", type=float, metavar="FPS", help="frames sampled per second of video, > 0 and <= 30 (media.vision_sampling_fps)")
    p.add_argument("--dry-run", action="store_true",
                   help="print how many segments, frames, images and requests would be sent, and to which endpoint; sends nothing")

    p = sub.add_parser("transcribe", help="measure audio features and transcribe speech with the configured backend (runs index + segment first; "
                                          "speech.backend defaults to none = skipped)")
    _common(p, media_required=False)
    p.add_argument("--limit", type=int, metavar="N", help="cover only the first N assets (sorted by path)")
    p.add_argument("--dry-run", action="store_true",
                   help="print how many assets / minutes of audio would be transcribed, by which backend, local or remote; extracts and sends nothing")

    p = sub.add_parser("outline", help="LLM story outline from the labeled moments, then stop for your approval "
                                       "(runs index, segment, audio, transcribe and analyze first)")
    _common(p, media_required=False)
    p.add_argument("--duration", type=float, help="target duration in seconds (project.target_duration_seconds)")
    p.add_argument("--dry-run", action="store_true",
                   help="print how many moments go into the prompt, the estimated input size and the endpoint (local/remote); sends nothing")
    p.add_argument("--auto-approve", action="store_true", default=None,
                   help="skip the approval stop; approval is recorded as automatic (workflow.approval=auto_approve)")

    p = sub.add_parser("llm-check", help="send one tiny live request per configured endpoint (text, then image) and print a result table")
    p.add_argument("--output", type=Path, help="workspace whose project.yaml configures the endpoints")
    p.add_argument("--config", type=Path, help="project.yaml (default: <output>/project.yaml if present, else defaults)")
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE", help="override any config field (repeatable)")
    p.add_argument("--endpoint", metavar="NAME", help="check this endpoint profile only (default: those used by llm.text and llm.vision)")
    p.add_argument("--no-image", action="store_true", help="skip the image request")
    p.add_argument("--speech", action="store_true",
                   help="also send a generated 1-second tone to the speech.* transcription backend and report reachability (empty text is fine)")
    p.add_argument("--no-llm", action="store_true", help="with --speech: check only the transcription backend, not the LLM endpoints")

    p = sub.add_parser("status", help="show the last run's stage status")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--json", action="store_true", help="machine-readable output")

    p = sub.add_parser("resume", help="continue from the last valid stage (cached work is reused)")
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--media", type=Path, help="media folder (default: the one recorded in manifest.json)")
    p.add_argument("--config", type=Path)
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE")
    p.add_argument("--auto-approve", action="store_true", default=None, help="approve the plan automatically (recorded as automatic)")
    p.add_argument("--filmcraft", help="path to filmcraft-cli (filmcraft.executable; else $FILMCRAFT_CLI, PATH)")

    p = sub.add_parser("draft", help="media folder -> edited FilmCraft draft, preview, QC and Resolve handoff")
    _common(p, media_required=True)
    p.add_argument("--duration", type=float, help="target duration in seconds (project.target_duration_seconds)")
    p.add_argument("--aspect", choices=["16:9", "9:16"], help="aspect ratio (project.aspect_ratio)")
    p.add_argument("--style", help="style (project.style)")
    p.add_argument("--sampling-fps", type=float, metavar="FPS", help="frame sampling rate used when analysis runs, > 0 and <= 30 (media.vision_sampling_fps)")
    p.add_argument("--planner", choices=["baseline", "llm"],
                   help="planner (workflow.planner): 'baseline' (default, no LLM) or 'llm' (labels, outline, your approval, then LLM clip selection)")
    p.add_argument("--filmcraft", help="path to filmcraft-cli (filmcraft.executable; else $FILMCRAFT_CLI, PATH)")
    p.add_argument("--filmcraft-mode", choices=["mcp", "cli", "bridge"], help="filmcraft.mode")
    p.add_argument("--auto-approve", action="store_true", default=None,
                   help="skip the approval stop; approval is recorded as automatic (workflow.approval=auto_approve)")

    p = sub.add_parser("critic", help="run the optional LLM critic on a finished draft: report (default) writes qc/critic_report.md; apply also applies its "
                                      "validated, local fixes as a new plan version and FilmCraft project (this run only; project.yaml is not changed)")
    p.add_argument("--output", type=Path, required=True, help="the draft workspace")
    p.add_argument("--config", type=Path)
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE")
    p.add_argument("--mode", choices=["report", "apply"], help="default: qc.critic from the config, or report when that is off")
    p.add_argument("--filmcraft", help="path to filmcraft-cli (filmcraft.executable; else $FILMCRAFT_CLI, PATH)")

    p = sub.add_parser("revise", help="change a finished draft with a request in your own words (e.g. \"make the beach scene shorter and drop the blurry "
                                      "sunset clip\"): a model maps it onto a closed set of plan operations, code validates them, and the result is a new plan "
                                      "version, a new FilmCraft project version, a new preview and a QC re-run. Ambiguous or destructive requests are only "
                                      "proposed (exit 3): `approve --revision`, then `resume`")
    p.add_argument("request", nargs="?", help="what to change, in your own words (or use --file)")
    p.add_argument("--file", type=Path, help="read the request from this text file (UTF-8)")
    p.add_argument("--output", type=Path, required=True, help="the draft workspace")
    p.add_argument("--config", type=Path)
    p.add_argument("--set", dest="sets", action="append", default=[], metavar="SECTION.FIELD=VALUE")
    p.add_argument("--dry-run", action="store_true", help="ask the model and show what would change; the plan, project and preview stay as they are")
    p.add_argument("--apply", action="store_true", help="apply the proposal you approved with `approve --revision` (the same as `resume`); takes no request")
    p.add_argument("--again", action="store_true", help="apply a request again even though it produced the current plan (a repeated command is otherwise skipped)")
    p.add_argument("--filmcraft", help="path to filmcraft-cli (filmcraft.executable; else $FILMCRAFT_CLI, PATH)")

    p = sub.add_parser("approve", help="approve what is waiting at the approval gate: the story outline (plans/story_outline.json), "
                                       "the edit plan (plans/edit_plan.json), a critic revision or a proposed revision; it says which")
    p.add_argument("--output", type=Path, required=True)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--outline", dest="which", action="store_const", const="outline", help="approve the story outline explicitly")
    g.add_argument("--plan", dest="which", action="store_const", const="plan", help="approve the edit plan explicitly")
    g.add_argument("--critic", dest="which", action="store_const", const="critic",
                   help="approve the critic revision plans/revisions/critic_rNN.* (workflow.plan_review: true); `resume` then applies it")
    g.add_argument("--revision", dest="which", action="store_const", const="revision",
                   help="approve the proposed revision plans/revisions/revise_rNN.* (`trip-vlog revise` stopped for it); `resume` or `revise --apply` then applies exactly that")

    p = sub.add_parser("resolve-finish", help="optional: finish the handoff inside DaVinci Resolve Studio "
                                              "(new project, import, relink stills, markers, experimental titles, report)")
    p.add_argument("--output", type=Path, required=True, help="the draft workspace (needs a completed handoff)")
    p.add_argument("--project-name", help="name of the NEW Resolve project (default: '<title> (trip-vlog vNNN)'); "
                                          "an existing project is never reused")
    p.add_argument("--dry-run", action="store_true", help="print the planned steps; nothing is created or changed in Resolve")
    p.add_argument("--keep-on-failure", action="store_true",
                   help="leave a title that could not be verified in the timeline instead of rolling it back")
    return ap


def _parse_sets(sets: Sequence[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in sets:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            raise ConfigError(f"--set expects SECTION.FIELD=VALUE, got {item!r}")
        try:
            out[key.strip()] = yaml.safe_load(value)
        except yaml.YAMLError:
            out[key.strip()] = value
    return out


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    ov: dict[str, Any] = {}
    mapping = {
        "fingerprint": "media.fingerprint", "recursive": "media.recursive", "follow_symlinks": "media.follow_symlinks",
        "ffprobe": "media.ffprobe_path", "ffmpeg": "media.ffmpeg_path", "duration": "project.target_duration_seconds",
        "aspect": "project.aspect_ratio", "style": "project.style", "filmcraft_mode": "filmcraft.mode",
        "filmcraft": "filmcraft.executable", "planner": "workflow.planner", "sampling_fps": "media.vision_sampling_fps",
    }
    for attr, key in mapping.items():
        if getattr(args, attr, None) is not None:
            ov[key] = getattr(args, attr)
    if getattr(args, "auto_approve", None):
        ov["workflow.approval"] = "auto_approve"
    ov.update(_parse_sets(getattr(args, "sets", [])))  # --set wins over convenience flags
    return ov


def _print_index_result(res: IndexResult) -> None:
    print(f"run {res.run_id} -> {res.workspace}")
    for name, status in res.stage_status.items():
        print(f"  {name:<11} {status}")
    s = res.summary
    print(f"assets: {s['assets']}  probe failures: {s['probe_failed']}  stale: {s['stale']}")
    if s["probe_failed"]:
        print("  some files could not be probed; see logs/errors.jsonl and media_index/inventory_report.json")


def _cmd_index(args: argparse.Namespace, command: str = "index") -> int:
    res = run_index(args.media, args.output, args.config, _overrides(args), command=command)
    _print_index_result(res)
    return res.exit_code


def _print_draft_result(res: IndexResult) -> None:
    from trip_vlog.state import Manifest, Workspace

    m = Manifest.load(Workspace(res.workspace).manifest_path)
    print(f"run {res.run_id} -> {res.workspace}")
    for name, status in res.stage_status.items():
        st = m.stage(name)
        if status == "skipped" and st.get("decision") == "cache_hit":
            status = "cached"
        print(f"  {name:<10} {status}")
        if status == "failed":
            print(f"      {str(st.get('error') or '').splitlines()[0] if st.get('error') else ''}")
    s = res.summary
    print(f"assets: {s['assets']}  probe failures: {s['probe_failed']}")
    ws = res.workspace
    if res.awaiting_approval and m.stage("outline_approval").get("status") == "awaiting_approval":
        ap = m.stage("outline_approval").get("summary", {})
        print("\nAWAITING APPROVAL of the story outline: no clips have been selected and nothing was created in FilmCraft yet.")
        if ap.get("invalidated"):
            print(f"  {ap['invalidated']}")
        print(f"  review: {ws / ap.get('outline_summary', 'plans/story_outline.md')}")
        print(f"  edit  : {ws / ap.get('outline', 'plans/story_outline.json')} (optional)")
        print(f"  then  : trip-vlog approve --output {ws}  &&  trip-vlog resume --output {ws}")
        return
    if res.awaiting_approval and m.stage("revise").get("status") == "awaiting_approval":
        _print_revise_block(m, ws)
        return
    if res.awaiting_approval and m.stage("critic").get("status") == "awaiting_approval":
        cs = m.stage("critic").get("summary", {})
        print(f"\nAWAITING APPROVAL of a critic revision: the draft and its preview are unchanged; {cs.get('proposals_valid', 0)} proposed change(s) wait for you.")
        print(f"  review: {ws / cs.get('revision_summary', 'plans/revisions')}")
        print(f"  report: {ws / str(cs.get('report', 'qc/critic_report.json'))[:-5]}.md")
        print(f"  then  : trip-vlog approve --critic --output {ws}  &&  trip-vlog resume --output {ws}")
    elif res.awaiting_approval:
        ap = m.stage("approval").get("summary", {})
        print("\nAWAITING APPROVAL: nothing has been created in FilmCraft yet.")
        if ap.get("invalidated"):
            print(f"  {ap['invalidated']}")
        print(f"  review: {ws / ap.get('plan_summary', 'plans/plan_summary.md')}")
        print(f"  then:   trip-vlog approve --output {ws}  &&  trip-vlog resume --output {ws}")
        return
    fc = m.get("filmcraft") or {}
    if fc.get("project"):
        print(f"\nFilmCraft {fc.get('version')}: {ws / fc['project']}")
    pl = m.stage("plan").get("summary", {})
    if pl.get("planner") == "llm" and pl.get("clips"):
        print(f"plan: {pl['clips']} clip(s) in {pl['scenes']} scene(s), {pl['titles']} title(s), {pl['duration_seconds']:g} s (target {pl['target_seconds']:g} s); "
              f"{pl.get('requests', 0)} request(s) to '{pl.get('endpoint')}' ({'local' if pl.get('local') else 'remote'}), "
              f"{pl.get('input_tokens', 0)} input / {pl.get('output_tokens', 0)} output tokens"
              + (f"; re-asked {pl['reasked']}x" if pl.get("reasked") else "") + (f"; {pl['repairs']} automatic repair(s)" if pl.get("repairs") else ""))
        print(f"  summary: {ws / 'plans' / 'plan_summary.md'}")
    ap_rec = m.get("approval") or {}
    if ap_rec.get("mode") == "derived_from_outline":
        print(f"edit plan approved from the {ap_rec.get('outline_approval_mode')} outline approval (no separate review; set workflow.plan_review: true to stop here)")
    qc = m.stage("qc").get("summary", {})
    if qc:
        print(f"QC: {qc.get('status')} ({qc.get('pass', 0)} pass, {qc.get('warn', 0)} warn, {qc.get('fail', 0)} fail"
              + (f", {qc['skipped']} skipped" if qc.get("skipped") else "") + ")"
              + (f"; warnings: {', '.join(qc['warned_checks'])}" if qc.get("warned_checks") else "")
              + (f"; FAILED: {', '.join(qc['failed_checks'])}" if qc.get("failed_checks") else ""))
    _print_revise_block(m, ws)
    cs = m.stage("critic").get("summary", {})
    if cs.get("mode"):
        sev = cs.get("by_severity") or {}
        print(f"critic ({cs['mode']}): {cs.get('status')}; {cs.get('findings', 0)} finding(s)"
              + (f" ({', '.join(f'{n} {k}' for k, n in sev.items() if n)})" if cs.get("findings") else "")
              + (f", {cs['changes_applied']} change(s) applied" if cs.get("changes_applied") else "")
              + f"; {cs.get('requests', 0)} request(s) to '{cs.get('endpoint')}' ({'local' if cs.get('local') else 'remote'}), "
              f"{cs.get('input_tokens', 0)} input / {cs.get('output_tokens', 0)} output tokens; "
              + ("preview sheets sent as images" if cs.get("images_sent") else f"TEXT ONLY, no images ({cs.get('images_reason')})"))
        if cs.get("rounds") or cs.get("changes_applied"):
            print(f"  the critic changed the plan in {cs.get('rounds', 0)} round(s); FilmCraft project now {cs.get('project')} (v{cs.get('project_version')}), "
                  "earlier versions are kept")
        if cs.get("halt_note"):
            print(f"  WARNING: {cs['halt_note']}")
        if cs.get("error") and cs.get("error") != cs.get("halt_note"):
            print(f"  critic problem (the draft is unaffected): {str(cs['error']).splitlines()[0]}")
        if cs.get("report"):
            print(f"  report: {ws / str(cs['report'])[:-5]}.md")
    pv = m.stage("preview").get("summary", {})
    if pv.get("preview"):
        print(f"preview: {ws / pv['preview']} ({pv.get('duration_seconds')} s)")
    if m.stage("handoff").get("status") in ("succeeded", "partial"):
        print(f"handoff: {ws / 'resolve_handoff' / 'handoff_report.md'}")
        line = _compat_line(m.stage("handoff").get("summary", {}), ws)
        if line:
            print(line)


def _print_revise_block(m: Any, ws: Path) -> None:
    """What `revise` did or wants (the stage summary): the change summary lines, then what to do next."""
    st = m.stage("revise")
    s = st.get("summary") or {}
    out = s.get("outcome")
    pend = (m.get("revise") or {}).get("pending") or {}
    ran_now = str(st.get("finished_at") or st.get("started_at") or "") >= str(m.get("run_started_at") or "~")      # the stage's record is from this run
    if not ran_now:
        if pend.get("status") == "pending":
            print(f"\na proposed revision waits for you ({pend.get('revision')}): trip-vlog approve --revision --output {ws}  &&  trip-vlog resume --output {ws}")
        return
    if not out or st.get("status") in (None, "pending") or (st.get("status") == "skipped" and not s.get("reason")):
        return
    if out == "already_applied":
        print(f"\nrevise: {s.get('reason')}")
        return
    print()
    for line in s.get("lines") or []:
        print(line)
    mod = f"; {s.get('requests', 0)} request(s) to '{s.get('endpoint')}' ({'local' if s.get('local') else 'remote'}), {s.get('input_tokens', 0)} input / {s.get('output_tokens', 0)} output tokens" if s.get("endpoint") else ""
    if out == "proposed":
        print(f"\nAWAITING CONFIRMATION of a revision: the plan, project and preview are unchanged{mod}.")
        for r in s.get("confirm_reasons") or []:
            print(f"  - {r}")
        print(f"  review: {ws / str(s.get('revision_summary'))}")
        print(f"  then  : trip-vlog approve --revision --output {ws}  &&  trip-vlog resume --output {ws}")
    elif out == "needs_clarification":
        print(f"\nThe request needs clarification; nothing was changed{mod}. Ask again with the answers in the request.")
    elif out == "dry_run":
        print(f"\ndry run: nothing was changed{mod}" + ("; applying it would ask for your confirmation first" if s.get("would_need_confirmation") else ""))
    elif out == "applied":
        d = s.get("duration") or {}
        print(f"\nrevision applied ({s.get('authorization')}): {s.get('changes_applied', 0)} change(s)" + (f", {s.get('changes_refused')} could not be made" if s.get("changes_refused") else "")
              + f"; length {d.get('before_seconds', 0):.1f} s -> {d.get('after_seconds', 0):.1f} s; FilmCraft project now {s.get('project')} (v{s.get('project_version')}), "
              f"earlier versions are kept; QC {s.get('qc_before')} -> {s.get('qc_after')}{mod}")
        if s.get("halt_note"):
            print(f"  WARNING: {s['halt_note']}")
        if s.get("revision_summary"):
            print(f"  summary: {ws / str(s['revision_summary'])}")
    elif out in ("failed", "no_change", "stale", "not_approved", "nothing_pending") and st.get("error"):
        print(f"\nnothing was changed: {str(st['error']).splitlines()[0]}")


def _compat_line(summary: dict, ws: Path) -> str:
    cp = summary.get("compatibility")
    if not cp:
        return ""
    return (f"compatibility: {cp['overall']} ({cp['verified']} verified, {cp['expected']} expected, {cp['manual']} manual, {cp['unsupported']} unsupported"
            + (f", {cp['unknown']} unknown" if cp.get("unknown") else "") + "); "
            f"XML round trip {'identical' if cp.get('xml_roundtrip_ok') else 'NOT checked' if cp.get('xml_roundtrip_ok') is None else 'has mismatches'}"
            f"\n  report: {ws / 'resolve_handoff' / 'compatibility_report.md'}")


def _cmd_draft(args: argparse.Namespace, command: str = "draft") -> int:
    res = run_draft(args.media, args.output, args.config, _overrides(args), command=command)
    _print_draft_result(res)
    return res.exit_code


def _cmd_revise(args: argparse.Namespace) -> int:
    request = args.request
    if args.file is not None:
        if request:
            raise ConfigError("give the request either as an argument or with --file, not both")
        try:
            request = Path(args.file).expanduser().read_text(encoding="utf-8-sig")
        except (OSError, UnicodeDecodeError) as e:
            raise ConfigError(f"cannot read the request file {args.file}: {getattr(e, 'strerror', None) or e}") from None
    res = run_revise(args.output, args.config, _overrides(args), request=request, dry_run=args.dry_run, apply=args.apply, again=args.again, command="revise")
    _print_draft_result(res)
    return res.exit_code


def _cmd_critic(args: argparse.Namespace) -> int:
    res = run_critic(args.output, args.config, _overrides(args), mode=args.mode, command="critic")
    _print_draft_result(res)
    return res.exit_code


def _print_analyze_result(res: IndexResult) -> None:
    from trip_vlog.state import Manifest, Workspace

    print(f"run {res.run_id} -> {res.workspace}")
    if res.dry_run is not None:
        from trip_vlog.analysis.stage import format_dry_run

        print(format_dry_run(res.dry_run))
        return
    m = Manifest.load(Workspace(res.workspace).manifest_path)
    for name, status in res.stage_status.items():
        st = m.stage(name)
        if status == "skipped" and st.get("decision") == "cache_hit":
            status = "cached"
        print(f"  {name:<11} {status}")
        if status == "failed" and st.get("error"):
            print(f"      {str(st['error']).splitlines()[0]}")
    s = m.stage("analyze").get("summary", {})
    if s:
        print(f"labeled {s.get('labeled', 0)} new, {s.get('already_labeled', 0)} already labeled, {s.get('fallback_unlabeled', 0)} fell back to the "
              f"baseline; {s.get('requests', 0)} request(s) to '{s.get('endpoint')}' ({'local' if s.get('local') else 'remote'}), "
              f"{s.get('input_tokens', 0)} input / {s.get('output_tokens', 0)} output tokens")
        for e in s.get("errors", [])[:5]:
            print(f"  - {e.get('path') or e.get('segment_id')}: {e.get('error')}")
        for w in s.get("warnings", []):
            print(f"  warning: {w}")
        if s.get("fallback_unlabeled"):
            print("  unlabeled segments keep their baseline moment and are retried by the next `analyze`; details in logs/errors.jsonl")
        print(f"moments: {res.workspace / 'media_index' / 'moments.jsonl'}")


def _cmd_analyze(args: argparse.Namespace, command: str = "analyze") -> int:
    if args.limit is not None and args.limit < 1:
        raise ConfigError("--limit must be at least 1")
    res = run_analyze(args.media, args.output, args.config, _overrides(args), limit=args.limit, dry_run=args.dry_run, command=command)
    _print_analyze_result(res)
    return res.exit_code


def _print_transcribe_result(res: IndexResult) -> None:
    from trip_vlog.state import Manifest, Workspace

    print(f"run {res.run_id} -> {res.workspace}")
    if res.dry_run is not None:
        from trip_vlog.speech.stage import format_dry_run

        print(format_dry_run(res.dry_run))
        return
    m = Manifest.load(Workspace(res.workspace).manifest_path)
    for name, status in res.stage_status.items():
        st = m.stage(name)
        if status == "skipped" and st.get("decision") == "cache_hit":
            status = "cached"
        print(f"  {name:<11} {status}")
        if status == "failed" and st.get("error"):
            print(f"      {str(st['error']).splitlines()[0]}")
    a = m.stage("audio").get("summary", {})
    if a and not a.get("skipped"):
        print(f"audio features: {a.get('assets_measured', 0)} measured, {a.get('cache_hits', 0)} cached, {a.get('no_audio_stream', 0)} without audio, {a.get('failed', 0)} failed")
    s = m.stage("transcribe").get("summary", {})
    if s.get("skipped"):
        print(f"transcription skipped: {s.get('reason')}")
    elif s:
        sk = ", ".join(f"{n} {why}" for why, n in (s.get("skipped_assets") or {}).items())
        print(f"transcribed {s.get('transcribed', 0)} new, {s.get('cached', 0)} already done, {s.get('failed', 0)} failed"
              + (f"; skipped: {sk}" if sk else "") + f"; backend {s.get('backend')} ({'local' if s.get('local') else 'remote'}), "
              f"{s.get('segments', 0)} transcript segment(s), {s.get('moments_with_transcript', 0)} moment(s) with speech")
        for e in s.get("errors", [])[:5]:
            print(f"  - {e.get('path')}: {e.get('error')}")
        print(f"transcripts: {res.workspace / 'media_index' / 'transcripts.jsonl'}")


def _cmd_transcribe(args: argparse.Namespace, command: str = "transcribe") -> int:
    if args.limit is not None and args.limit < 1:
        raise ConfigError("--limit must be at least 1")
    res = run_transcribe(args.media, args.output, args.config, _overrides(args), limit=args.limit, dry_run=args.dry_run, command=command)
    _print_transcribe_result(res)
    return res.exit_code


def _print_outline_result(res: IndexResult) -> None:
    from trip_vlog.state import Manifest, Workspace

    print(f"run {res.run_id} -> {res.workspace}")
    if res.dry_run is not None:
        from trip_vlog.outline_stages import format_dry_run

        print(format_dry_run(res.dry_run))
        return
    m = Manifest.load(Workspace(res.workspace).manifest_path)
    for name, status in res.stage_status.items():
        st = m.stage(name)
        if status == "skipped" and st.get("decision") == "cache_hit":
            status = "cached"
        print(f"  {name:<16} {status}")
        if status == "failed" and st.get("error"):
            for line in str(st["error"]).splitlines()[:8]:
                print(f"      {line}")
    s = m.stage("outline").get("summary", {})
    if s.get("scenes"):
        print(f"outline: {s['scenes']} scene(s), {s.get('planned_seconds', 0):g} s planned (target {s.get('target_seconds', 0):g} s); "
              f"{s.get('moments_in_prompt', 0)} of {s.get('eligible_moments', 0)} moments in the prompt; {s.get('requests', 0)} request(s) to "
              f"'{s.get('endpoint')}' ({'local' if s.get('local') else 'remote'}), {s.get('input_tokens', 0)} input / {s.get('output_tokens', 0)} output tokens")
        if s.get("repairs") or s.get("warnings"):
            print(f"  {s.get('repairs', 0)} automatic repair(s), {s.get('warnings', 0)} warning(s): see plans/story_outline.md")
        if s.get("edited_outline_backup"):
            print(f"  your edited outline was replaced because the inputs changed; a copy is at {s['edited_outline_backup']}")
    ws = res.workspace
    if res.awaiting_approval:
        ap = m.stage("outline_approval").get("summary", {})
        print("\nAWAITING APPROVAL: no clips have been selected and nothing was created in FilmCraft.")
        if ap.get("invalidated"):
            print(f"  {ap['invalidated']}")
        print(f"  review: {ws / ap.get('outline_summary', 'plans/story_outline.md')}")
        print(f"  edit  : {ws / ap.get('outline', 'plans/story_outline.json')} (optional)")
        print(f"  then  : trip-vlog approve --output {ws}  &&  trip-vlog resume --output {ws}")
    elif m.stage("outline_approval").get("status") == "succeeded":
        print(f"outline approved ({(m.get('outline_approval') or {}).get('mode')}): {ws / 'plans' / 'story_outline.json'}")


def _cmd_outline(args: argparse.Namespace, command: str = "outline") -> int:
    res = run_outline(args.media, args.output, args.config, _overrides(args), dry_run=args.dry_run, command=command)
    _print_outline_result(res)
    return res.exit_code


def _cmd_llm_check(args: argparse.Namespace) -> int:
    from trip_vlog.config import load_config
    from trip_vlog.llm.check import format_table, run_llm_check

    cfg_path = args.config
    if cfg_path is None and args.output is not None:
        cand = Path(args.output).expanduser() / "project.yaml"
        cfg_path = cand if cand.is_file() else None
    config = load_config(cfg_path, _parse_sets(args.sets))
    if args.no_llm and not args.speech:
        raise ConfigError("--no-llm only makes sense together with --speech")
    rows = [] if args.no_llm else run_llm_check(config, endpoint=args.endpoint, include_image=not args.no_image)
    if args.speech:
        from trip_vlog.speech.check import check_speech

        rows.extend(check_speech(config))
    print(format_table(rows))
    return 0 if all(r.ok for r in rows) else 1


def _cmd_resume(args: argparse.Namespace) -> int:
    from trip_vlog.state import Manifest, Workspace

    m = Manifest.load(Workspace(Path(args.output).expanduser().resolve()).manifest_path)
    if m.get("pipeline") == "draft":
        return _cmd_draft(args, "resume")
    if m.get("pipeline") == "analyze":
        args.limit, args.dry_run = None, False
        return _cmd_analyze(args, "resume")
    if m.get("pipeline") == "transcribe":
        args.limit, args.dry_run = None, False
        return _cmd_transcribe(args, "resume")
    if m.get("pipeline") == "outline":
        args.dry_run = False
        return _cmd_outline(args, "resume")
    res = run_index(args.media, args.output, args.config, _overrides(args), command="resume")
    _print_index_result(res)
    return res.exit_code


def _cmd_approve(args: argparse.Namespace) -> int:
    try:
        kind, ap = approve_pending(args.output, args.which)
    except ApprovalRefused as e:
        print(f"error: {e}", file=sys.stderr)
        return 4
    if kind == "revision":
        print(f"approved revision {ap.get('revision')} (recorded in manifest.json); it will be applied exactly as proposed, to the plan it was proposed for. "
              f"Continue with: trip-vlog resume --output {args.output}")
    elif kind == "critic":
        print(f"approved critic revision {ap.get('revision')} (recorded in manifest.json). Continue with: trip-vlog resume --output {args.output}")
    elif kind == "outline":
        print(f"approved story outline {ap['outline_hash'][:12]} (recorded in manifest.json). Continue with: trip-vlog resume --output {args.output}")
    else:
        print(f"approved plan {ap['plan_hash'][:12]} (recorded in manifest.json). Continue with: trip-vlog resume --output {args.output}")
    return 0


def _cmd_resolve_finish(args: argparse.Namespace) -> int:
    from trip_vlog.resolve.finish import run_finish

    return run_finish(args.output, project_name=args.project_name, dry_run=args.dry_run,
                      keep_on_failure=args.keep_on_failure).exit_code


def _cmd_status(args: argparse.Namespace) -> int:
    info = read_status(args.output)
    if args.json:
        print(json.dumps(info, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    m = info["manifest"]
    print(f"workspace : {info['workspace']}")
    print(f"tool      : trip-vlog {m.get('tool_version')}  platform: {m.get('platform', {}).get('system')} {m.get('platform', {}).get('machine')}")
    print(f"ffprobe   : {m.get('ffprobe', {}).get('version') or 'not found'}")
    print(f"last run  : {m.get('run_id')} ({m.get('command')}) started {m.get('run_started_at')} finished {m.get('run_finished_at') or '-'}")
    print("stages:")
    stages = m.get("stages", {})
    canonical = ("scan", "probe", "catalog", "thumbnails", "segment", "audio", "transcribe", "analyze", "outline", "outline_approval", *DRAFT_STAGES[5:])
    llm_draft = m.get("pipeline") == "draft" and m.get("planner") == "llm"
    order = ([n for n in canonical if n in stages or (m.get("pipeline") == "draft" and n not in ("critic", "revise")
                                                      and (llm_draft or n not in ("analyze", "audio", "transcribe", "outline", "outline_approval")))]
             + [n for n in stages if n not in canonical])
    for name in order:
        st = stages.get(name, {})
        extra = f" [{st['decision']}]" if st.get("decision") else ""
        print(f"  {name:<11} {st.get('status', 'pending'):<18}{extra} {json.dumps(st.get('summary', {}), sort_keys=True)[:300]}")
        if st.get("error"):
            print(f"      error: {st['error']}")
    ap = m.get("approval")
    if ap:
        print(f"approval  : {ap.get('status')}" + (f" ({ap.get('mode')})" if ap.get("mode") else "") + f" plan {str(ap.get('plan_hash', ''))[:12]}"
              + (f" (critic fix of {str(ap.get('parent_plan_hash', ''))[:12]}, {ap.get('revision')})" if ap.get("mode") == "critic_fix" else "")
              + (f" (revision of {str(ap.get('parent_plan_hash', ''))[:12]}, {ap.get('revision')})" if ap.get("mode") == "revision" else ""))
    cst = m.get("critic")
    if cst and (cst.get("rounds") or cst.get("pending")):
        pend = cst.get("pending") or {}
        print(f"critic    : {len(cst.get('rounds', []))} round(s) applied" + (f"; latest {cst['rounds'][-1].get('revision')}" if cst.get("rounds") else "")
              + (f"; revision {pend.get('revision')} {pend.get('status')}" if pend else "") + "; report qc/critic_report.md")
        if cst.get("rounds") and cst["rounds"][-1].get("halt_note"):
            print(f"            WARNING: {cst['rounds'][-1]['halt_note']}")
    from trip_vlog.revise_stages import history

    hist = history(m)
    if hist:
        print(f"revisions : {sum(1 for h in hist if h['status'] == 'applied')} applied" + (f", {sum(1 for h in hist if h['status'] in ('pending', 'approved'))} waiting" if any(h['status'] in ('pending', 'approved') for h in hist) else ""))
        for h in hist:
            q = json.dumps(str(h.get("request") or ""), ensure_ascii=False)
            q = q if len(q) <= 70 else q[:67] + '..."'
            tail = (f" -> {Path(str(h['project'])).name}" if h.get("project") else "") + (f", QC {h.get('qc_before')} -> {h.get('qc_after')}" if h.get("qc_after") else "")
            print(f"  {Path(str(h.get('revision'))).stem:<10} {h['status']:<9} {q}{tail}")
            if h.get("halt_note"):
                print(f"            WARNING: {h['halt_note']}")
    oap = m.get("outline_approval")
    if oap:
        print(f"outline   : {oap.get('status')}" + (f" ({oap.get('mode')})" if oap.get("mode") else "") + f" outline {str(oap.get('outline_hash', ''))[:12]}")
    fc = m.get("filmcraft")
    if fc:
        print(f"filmcraft : {fc.get('version')}  project {fc.get('project')} (v{fc.get('project_version')})  snapshot {fc.get('capability_snapshot')}")
    if m.get("planner"):
        print(f"planner   : {m['planner']}")
    cat = info.get("catalog")
    if cat:
        print(f"catalog   : {cat['assets_present']} present, {cat['assets_missing']} missing, schema v{cat['schema_version']}")
        for label, key in (("probe failures", "probe_failed"), ("stale (content changed)", "stale"),
                           ("thumbnail errors", "thumbnail_errors")):
            if cat[key]:
                print(f"  {label}: {len(cat[key])}")
                for p in cat[key][:10]:
                    print(f"    - {p}")
        for d in cat["probable_duplicates"]:
            print(f"  probable duplicates: {', '.join(d['paths'])}")
        for mv in cat["probable_moves"]:
            print(f"  probable move: {mv['from']} -> {mv['to']}")
    cl = _compat_line(stages.get("handoff", {}).get("summary", {}), Path(info["workspace"]))
    if cl:
        print(cl.replace("compatibility:", "compat    :", 1))
    rf = stages.get("resolve_finish")
    if rf:
        rs = rf.get("summary", {})
        print(f"resolve   : {rf.get('status')} - Resolve project '{rs.get('project')}' ({rs.get('titles_failed', 0)} title(s) need manual work); "
              f"report {rs.get('report')}")
    unfinished = [n for n in order if n != "resolve_finish" and stages.get(n, {}).get("status", "pending") not in ("succeeded", "partial")
                  and not (stages.get(n, {}).get("decision") == "cache_hit")
                  and not (stages.get(n, {}).get("status") == "skipped" and stages[n].get("summary", {}).get("skipped"))]  # e.g. no speech backend
    if stages.get("outline_approval", {}).get("status") == "awaiting_approval":
        print(f"next: review plans/story_outline.md (edit plans/story_outline.json if needed), then `trip-vlog approve --output {info['workspace']}` "
              f"and `trip-vlog resume --output {info['workspace']}`")
    elif stages.get("revise", {}).get("status") == "awaiting_approval" and (m.get("revise") or {}).get("pending", {}).get("status") == "approved":
        print(f"next: the revision is approved; `trip-vlog resume --output {info['workspace']}` applies it")
    elif stages.get("revise", {}).get("status") == "awaiting_approval":
        print(f"next: review plans/revisions/ (the revise_rNN.md of the proposal), then `trip-vlog approve --revision --output {info['workspace']}` and "
              f"`trip-vlog resume --output {info['workspace']}`; or ask again with `trip-vlog revise`")
    elif stages.get("critic", {}).get("status") == "awaiting_approval" and (m.get("critic") or {}).get("pending", {}).get("status") == "approved":
        print(f"next: the critic revision is approved; `trip-vlog resume --output {info['workspace']}` applies it")
    elif stages.get("critic", {}).get("status") == "awaiting_approval":
        print(f"next: review plans/revisions/ and qc/critic_report.md, then `trip-vlog approve --critic --output {info['workspace']}` and "
              f"`trip-vlog resume --output {info['workspace']}`")
    elif stages.get("approval", {}).get("status") == "awaiting_approval":
        print(f"next: review plans/plan_summary.md, then `trip-vlog approve --output {info['workspace']}` and `trip-vlog resume --output {info['workspace']}`")
    elif unfinished:
        print(f"next: `trip-vlog resume --output {info['workspace']}` (unfinished: {', '.join(unfinished)})")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # robust output on legacy Windows code pages
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    try:
        return {"index": _cmd_index, "status": _cmd_status, "resume": _cmd_resume, "draft": _cmd_draft,
                "approve": _cmd_approve, "critic": _cmd_critic, "revise": _cmd_revise, "outline": _cmd_outline, "resolve-finish": _cmd_resolve_finish, "analyze": _cmd_analyze, "transcribe": _cmd_transcribe,
                "llm-check": _cmd_llm_check}[args.command](args)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted; run `trip-vlog resume` to continue", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
