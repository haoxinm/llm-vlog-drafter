"""Titles in the plans (DESIGN §13.1, §13.4): simple, editable text on V2.

Rules (all deterministic; the LLM only ever *proposes* scene titles):
  * Text comes from the user or from the outline, never from the model's imagination: the opening title is ``project.title``,
    a closing title is ``editorial.closing_title``, a scene title is the *outline's* scene title. A title that is neither is
    rejected, so no location or date can be invented. Text that equals a folder name of a clip in its scene is accepted but
    flagged ("from folder name") in the plan's uncertainties, because a folder name is weak evidence of a place or date.
  * At most ``editorial.max_titles`` titles; one line, 60 characters (two lines read back wrongly from FilmCraft: see models); 1-10 seconds each; inside the sequence; no two titles
    overlap and none overlaps an overlay clip on V2.
  * Nothing appears unless enabled: ``editorial.opening_title`` (default off), ``editorial.closing_title`` (set to enable),
    ``editorial.scene_titles`` (default off, llm planner only).

Validation functions take a report object with ``error`` / ``warn`` / ``repaired`` (``planning.validate.ValidationReport``) and
settings with the title fields of ``ValidationSettings``; this module does not import ``validate`` (it imports this one).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Sequence

from trip_vlog.planning.duration import frames_to_float, seconds_to_frames
from trip_vlog.planning.models import (TITLE_MAX_CHARS, TITLE_MAX_LINES, TITLE_MAX_SECONDS, TITLE_MIN_SECONDS, EditScene, EditTitle,
                                       SelectionPlan, SelectionScene, SelectionTitle, TitleStart)

_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")   # newline (\x0a) is allowed: two-line titles
FOLDER_FLAG = "from folder name"


@dataclass(frozen=True)
class TitleSettings:
    opening_title: bool = False
    scene_titles: bool = False
    closing_title: str | None = None
    max_titles: int = 6
    title_seconds: float = 3.0
    project_title: str = "Trip Vlog"

    @classmethod
    def from_config(cls, config: Any) -> "TitleSettings":
        e = config.editorial
        return cls(opening_title=e.opening_title, scene_titles=e.scene_titles, closing_title=e.closing_title, max_titles=e.max_titles,
                   title_seconds=float(e.title_seconds), project_title=config.project.title)

    def key(self) -> dict[str, Any]:
        return dict(self.__dict__)


def normalize_text(text: str) -> str:
    """NFC, CRLF -> LF, runs of blanks collapsed per line, blank edges trimmed. Line breaks are kept."""
    t = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(" ".join(line.split()) for line in t.strip().split("\n"))


def same_text(a: str, b: str) -> bool:
    return normalize_text(a).casefold() == normalize_text(b).casefold()


def text_problems(text: str) -> list[str]:
    """Why ``text`` cannot be a title (empty list = fine): one non-empty line of at most 60 characters."""
    if not isinstance(text, str) or not text.strip():
        return ["the text is empty"]
    out = []
    if _CONTROL.search(text):
        out.append("the text contains control characters")
    if len(text) > TITLE_MAX_CHARS:
        out.append(f"the text has {len(text)} characters, the limit is {TITLE_MAX_CHARS}")
    lines = text.split("\n")
    if len(lines) > TITLE_MAX_LINES:
        out.append(f"the text has {len(lines)} lines, the limit is {TITLE_MAX_LINES} (FilmCraft does not read multi-line titles back)")
    if any(not ln.strip() for ln in lines):
        out.append("the text has an empty line")
    return out


def title_id_for(n: int) -> str:
    return f"title_{n:02d}"


def scene_primary_seconds(scene: SelectionScene) -> float:
    return sum(c.target_duration_seconds for c in scene.clips if c.track_role == "primary_video")


# ---- deterministic titles (opening / closing) ---------------------------------------------------------------------

def default_titles(first_scene_id: str, last_scene_id: str, s: TitleSettings, notes: list[str], first_id: int = 1) -> list[SelectionTitle]:
    """The opening title (``project.title``, on the first scene) and the closing title (``editorial.closing_title``, on the last)
    when enabled. ``notes`` collects why a requested title was not created."""
    out: list[SelectionTitle] = []
    if s.max_titles <= 0:
        return out
    dur = min(max(float(s.title_seconds), TITLE_MIN_SECONDS), TITLE_MAX_SECONDS)
    if s.opening_title:
        text = normalize_text(s.project_title)
        why = text_problems(text)
        if why:
            notes.append(f"the opening title was skipped: project.title is not usable as a title ({'; '.join(why)})")
        else:
            out.append(SelectionTitle(
                title_id=title_id_for(first_id + len(out)), text=text, role="opening_title", scene_id=first_scene_id,
                text_source="project_title", start=TitleStart(relative_to="scene_start", clip_instance_id=None, offset_seconds=0.0),
                duration_seconds=dur, rationale="Opening title with project.title (editorial.opening_title)"))
    if s.closing_title and len(out) < s.max_titles:
        text = normalize_text(s.closing_title)
        why = text_problems(text)
        if why:
            notes.append(f"the closing title was skipped: editorial.closing_title is not usable as a title ({'; '.join(why)})")
        else:
            out.append(SelectionTitle(
                title_id=title_id_for(first_id + len(out)), text=text, role="closing", scene_id=last_scene_id,
                text_source="user_config", start=TitleStart(relative_to="sequence_end", clip_instance_id=None, offset_seconds=0.0),
                duration_seconds=dur, rationale="Closing title with editorial.closing_title"))
    return out


def with_default_titles(plan: SelectionPlan, s: TitleSettings) -> SelectionPlan:
    """Add the opening/closing titles to a selection plan that has none of those roles (baseline planner). Idempotent."""
    have = {t.role for t in plan.titles}
    notes: list[str] = []
    new = [t for t in default_titles(plan.scenes[0].scene_id, plan.scenes[-1].scene_id, s, notes) if t.role not in have]
    if not new and not notes:
        return plan
    titles = list(plan.titles)
    for t in new:
        titles.append(t.model_copy(update={"title_id": title_id_for(len(titles) + 1)}))
    return plan.model_copy(update={"schema_version": "1.1", "titles": titles, "uncertainties": [*plan.uncertainties, *notes]})


# ---- classification + selection-time validation ---------------------------------------------------------------------

def folder_names(scene: SelectionScene, folders_by_moment: dict[str, str]) -> set[str]:
    """Every folder component of the clips of ``scene`` (``folders_by_moment``: moment_id -> asset folder path)."""
    names: set[str] = set()
    for c in scene.clips:
        for part in (folders_by_moment.get(c.moment_id) or "").split("/"):
            if part.strip():
                names.add(normalize_text(part))
    return names


def classify_text(text: str, role: str, scene: SelectionScene, s: Any, folders: set[str]) -> str | None:
    """Which allowed source ``text`` comes from for this role, or None when it comes from none of them."""
    if role == "opening_title":
        return "project_title" if same_text(text, s.project_title) else None
    if role == "closing":
        return "user_config" if s.closing_title and same_text(text, s.closing_title) else None
    if same_text(text, scene.title):
        return "outline_scene_title"
    if any(same_text(text, f) for f in folders):
        return "folder_name"
    return None


def validate_selection_titles(report: Any, plan: SelectionPlan, s: Any, folders_by_moment: dict[str, str] | None = None) -> None:
    """Errors (``report.error``) for titles a selection plan may not contain; see the module docstring."""
    titles = plan.titles
    if len(titles) > s.max_titles:
        report.error("too_many_titles", f"the plan has {len(titles)} titles, editorial.max_titles is {s.max_titles}", "titles")
    scenes = {sc.scene_id: sc for sc in plan.scenes}
    clip_scene = {c.clip_instance_id: sc.scene_id for sc in plan.scenes for c in sc.clips}
    enabled = {"opening_title": s.opening_title, "scene_title": s.scene_titles, "closing": bool(s.closing_title)}
    flag_option = {"opening_title": "editorial.opening_title", "scene_title": "editorial.scene_titles", "closing": "editorial.closing_title"}
    seen: set[str] = set()
    for i, t in enumerate(titles):
        path = f"titles[{i}]"
        if t.title_id in seen:
            report.error("duplicate_title_id", f"title_id {t.title_id!r} is used more than once", path)
        seen.add(t.title_id)
        for why in text_problems(t.text):
            report.error("title_text", f"title {t.title_id}: {why}", path)
        if not (TITLE_MIN_SECONDS - 1e-9 <= t.duration_seconds <= TITLE_MAX_SECONDS + 1e-9):
            report.error("title_duration", f"title {t.title_id}: {t.duration_seconds:g} s is outside {TITLE_MIN_SECONDS:g}-{TITLE_MAX_SECONDS:g} s", path)
        sc = scenes.get(t.scene_id)
        if sc is None:
            report.error("title_scene_unknown", f"title {t.title_id}: unknown scene_id {t.scene_id!r}", path)
        if t.start.relative_to == "clip_start":
            if t.start.clip_instance_id is None or t.start.clip_instance_id not in clip_scene:
                report.error("title_clip_unknown", f"title {t.title_id}: clip_start needs a clip of the plan, got {t.start.clip_instance_id!r}", path)
            elif clip_scene[t.start.clip_instance_id] != t.scene_id:
                report.error("title_clip_unknown", f"title {t.title_id}: clip {t.start.clip_instance_id} belongs to scene "
                             f"{clip_scene[t.start.clip_instance_id]}, not {t.scene_id}", path)
        elif t.start.clip_instance_id is not None:
            report.error("title_clip_unknown", f"title {t.title_id}: {t.start.relative_to} takes no clip_instance_id", path)
        if t.text_source == "user":
            report.error("title_text_not_allowed", f"title {t.title_id}: text_source 'user' is only for titles typed into the edit plan", path)
            continue
        if not enabled.get(t.role, False):
            report.error("title_not_enabled", f"title {t.title_id}: a {t.role} was proposed but {flag_option[t.role]} is not enabled", path)
        if sc is None:
            continue
        found = classify_text(t.text, t.role, sc, s, folder_names(sc, folders_by_moment or {}))
        if found is None:
            report.error("title_text_not_allowed",
                         f"title {t.title_id}: {t.text!r} is not the project title, the closing text from the config, the outline's title of "
                         f"scene {sc.scene_id} ({sc.title!r}) or a folder name of its clips; titles never carry invented places, dates or facts",
                         path)
        elif found != t.text_source:
            report.error("title_text_source", f"title {t.title_id}: text_source is {t.text_source!r} but the text is the {found}", path)
        elif found == "folder_name" and not any(FOLDER_FLAG in u and t.title_id in u for u in plan.uncertainties):
            report.error("title_unflagged", f"title {t.title_id}: text comes from a folder name, which must be flagged in uncertainties "
                         f"(\"{t.title_id}: ... {FOLDER_FLAG} ...\")", path)


def folder_flag(t: SelectionTitle) -> str:
    return (f"{t.title_id}: the text {t.text!r} is {FOLDER_FLAG}; it has not been checked as a place or date, confirm it or remove the title")


# ---- resolution -----------------------------------------------------------------------------------------------------

def resolve_titles(plan: SelectionPlan, scenes: Sequence[EditScene], fps: Fraction, total_frames: int) -> tuple[list[EditTitle], list[str]]:
    """Titles of ``plan`` as frames on the resolved timeline. Titles that would end past the end of the sequence are moved
    earlier (and shortened if longer than the sequence); every such change is a note on the title. Returns (titles, issues);
    issues are unresolvable references (the selection validation reports those first)."""
    by_scene = {sc.scene_id: sc for sc in scenes}
    clip_start = {c.clip_instance_id: c.timeline_start_frame for sc in scenes for c in sc.clips}
    out: list[EditTitle] = []
    issues: list[str] = []
    for t in plan.titles:
        sc = by_scene.get(t.scene_id)
        if sc is None:
            issues.append(f"{t.title_id}: unknown scene_id {t.scene_id!r}")
            continue
        dur = max(1, seconds_to_frames(t.duration_seconds, fps))
        off = seconds_to_frames(t.start.offset_seconds, fps)
        notes: list[str] = []
        if t.start.relative_to == "scene_start":
            start = sc.start_frame + off
        elif t.start.relative_to == "clip_start":
            cid = t.start.clip_instance_id
            if cid not in clip_start:
                issues.append(f"{t.title_id}: unknown clip_instance_id {cid!r}")
                continue
            start = clip_start[cid] + off
        else:  # sequence_end: ends `offset` before the end of the sequence
            start = total_frames - dur - off
        if dur > total_frames:
            notes.append(f"title shortened from {dur}f to the sequence length {total_frames}f")
            dur = total_frames
        if start + dur > total_frames or start < 0:
            moved = min(max(0, start), max(0, total_frames - dur))
            if moved != start:
                notes.append(f"title moved from frame {start} to {moved} so that it lies inside the sequence ({total_frames}f)")
            start = moved
        out.append(EditTitle(
            title_id=t.title_id, text=t.text, role=t.role, scene_id=t.scene_id, text_source=t.text_source, start=t.start,
            start_frame=start, duration_frames=dur, duration_seconds=frames_to_float(dur, fps), track=t.track, style=t.style,
            resolver_notes=notes, rationale=t.rationale))
    return out, issues


# ---- edit-plan validation -------------------------------------------------------------------------------------------

def validate_edit_titles(report: Any, plan: Any, fps: Fraction, s: Any) -> None:
    """Structure of the resolved titles. A person may edit or add titles in ``edit_plan.json``, so their text is not checked
    against the sources the planners are held to; only what would break the timeline or the typography is."""
    titles = plan.titles
    if not titles:
        return
    total = plan.total_duration_frames
    scene_ids = {sc.scene_id for sc in plan.scenes}
    if len(titles) > s.max_titles:
        report.warn("too_many_titles", f"the plan has {len(titles)} titles, editorial.max_titles is {s.max_titles}", "titles")
    seen: set[str] = set()
    one_frame = float(1 / fps)
    for i, t in enumerate(titles):
        path = f"titles[{i}]"
        if t.title_id in seen:
            report.error("duplicate_title_id", f"title_id {t.title_id!r} is used more than once", path)
        seen.add(t.title_id)
        for why in text_problems(t.text):
            report.error("title_text", f"title {t.title_id}: {why}", path)
        if t.scene_id not in scene_ids:
            report.error("title_scene_unknown", f"title {t.title_id}: unknown scene_id {t.scene_id!r}", path)
        if t.start_frame + t.duration_frames > total:
            report.error("title_outside_sequence", f"title {t.title_id}: frames {t.start_frame}-{t.start_frame + t.duration_frames} end after the "
                         f"sequence ({total}f)", path)
        secs = frames_to_float(t.duration_frames, fps)
        if abs(secs - t.duration_seconds) > one_frame:
            report.repaired("title_seconds", f"title {t.title_id}: duration_seconds recomputed from duration_frames", t.duration_seconds, secs,
                            f"{path}.duration_seconds")
            t.duration_seconds = secs
        if secs < TITLE_MIN_SECONDS - one_frame or secs > TITLE_MAX_SECONDS + one_frame:
            report.error("title_duration", f"title {t.title_id}: {secs:g} s is outside {TITLE_MIN_SECONDS:g}-{TITLE_MAX_SECONDS:g} s", path)
    ordered = sorted(titles, key=lambda t: (t.start_frame, t.title_id))
    for a, b in zip(ordered, ordered[1:]):
        if b.start_frame < a.start_frame + a.duration_frames:
            report.error("title_overlap", f"titles {a.title_id} and {b.title_id} overlap on V2 "
                         f"(frames {a.start_frame}-{a.start_frame + a.duration_frames} and {b.start_frame}-{b.start_frame + b.duration_frames})")
    overlays = [c for sc in plan.scenes for c in sc.clips if c.track_role == "overlay"]
    for t in titles:
        for c in overlays:
            if t.start_frame < c.timeline_start_frame + c.duration_frames and c.timeline_start_frame < t.start_frame + t.duration_frames:
                report.error("title_overlaps_overlay", f"title {t.title_id} overlaps overlay clip {c.clip_instance_id} on V2; "
                             "FilmCraft would overwrite one of them")
