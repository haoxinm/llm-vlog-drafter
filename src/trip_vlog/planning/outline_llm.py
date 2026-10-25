"""Story-editor request building and the generate -> validate -> (one re-ask) loop (DESIGN §9.2, §9.5, §11.4)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.types import LLMRequest, TextPart, Usage
from trip_vlog.llm.untrusted import PREAMBLE
from trip_vlog.planning.outline import (OutlineSettings, StoryOutline, document_from_response, findings_text, response_schema,
                                        validate_outline)
from trip_vlog.planning.outline_catalog import OutlineCatalog
from trip_vlog.planning.validate import ValidationReport
from trip_vlog.prompts import PromptTemplate, load_template

SYSTEM_TEMPLATE = "story_editor"
INPUT_TEMPLATE = "story_editor_input"
MAX_IDS_PER_REF = 8


class OutlineError(RuntimeError):
    """The story editor could not produce a valid outline. `report` holds the last findings, `history` every attempt's."""

    def __init__(self, message: str, report: ValidationReport | None = None, history: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.report = report
        self.history = history or []


def templates() -> tuple[PromptTemplate, PromptTemplate]:
    return load_template(SYSTEM_TEMPLATE), load_template(INPUT_TEMPLATE)


def prompt_info() -> dict[str, Any]:
    s, i = templates()
    return {"system": {"id": s.id, "version": s.version, "sha256": s.sha256}, "input": {"id": i.id, "version": i.version, "sha256": i.sha256}}


def _q(text: str) -> str:
    return json.dumps(text, ensure_ascii=False)


def settings_text(config: Any) -> str:
    p = config.project
    return "\n".join([
        f"- title: {_q(p.title)}", f"- target duration: {p.target_duration_seconds:g} seconds",
        f"- story mode: {p.story_mode} (chronological = strictly in order; mostly_chronological = in order, a brief hook allowed; "
        "thematic = grouped by theme, say so in chronology_note)",
        f"- style: {_q(p.style)}", f"- audience: {_q(p.audience)}", f"- language of titles and notes: {p.language}",
        f"- photos stay on screen for about {config.media.still_duration_seconds:g} s"])


def constraints_text(config: Any, oc: OutlineCatalog) -> str:
    e = config.editorial
    lines = []
    for ref, ids in oc.must_include.items():
        if ids:
            shown = ", ".join(ids[:MAX_IDS_PER_REF]) + (f" (+{len(ids) - MAX_IDS_PER_REF} more)" if len(ids) > MAX_IDS_PER_REF else "")
            lines.append(f"- must include {_q(ref)}: use at least one of these moments: {shown}")
        else:
            lines.append(f"- must include {_q(ref)}: matches nothing available; add a line to missing_coverage")
    if e.must_exclude:
        lines.append("- must exclude (already removed from the catalog): " + ", ".join(_q(r) for r in e.must_exclude))
    if e.preferred_topics:
        lines.append("- preferred topics: " + ", ".join(_q(t) for t in e.preferred_topics))
    lines.append(f"- at most {e.max_similar_shots_per_scene} visually similar shots will be used in one scene, so offer varied candidates")
    if e.allow_nonlinear_hook and e.opening_hook_seconds > 0:
        lines.append(f"- an opening hook is allowed: at most {e.opening_hook_seconds:g} seconds, first scene, role opening_hook")
    else:
        lines.append("- no opening hook: hook.used must be false and no scene may have the role opening_hook")
    if e.closing_seconds > 0:
        lines.append(f"- the final scene is a closing of about {e.closing_seconds:g} seconds")
    if e.avoid_fabricated_context:
        lines.append("- do not add context (places, names, reasons) the catalog does not show; put open questions in uncertainties")
    s = oc.selection
    if s.over_limit:
        lines.append(f"- the catalog was reduced from {s.eligible} to {s.kept} moments (best of each duplicate group and time cluster kept)")
    return "\n".join(lines)


def user_text(config: Any, oc: OutlineCatalog) -> str:
    _, tpl = templates()
    s = OutlineSettings.from_config(config)
    values = {"settings": settings_text(config), "constraints": constraints_text(config, oc), "moment_count": str(len(oc.moments)),
              "untrusted_preamble": PREAMBLE, "catalog": oc.render() or "(no usable moments)",
              "target_seconds": f"{s.target_duration_seconds:g}", "tolerance_seconds": f"{s.tolerance_seconds:g}"}
    # one pass, so text taken from the media can never be mistaken for a placeholder
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m[1], m[0]), tpl.text)


def max_tokens(config: Any) -> int:
    _, ep = config.llm.endpoint_for("text")
    return max(256, min(config.planning.outline_max_tokens, ep.max_tokens))


def build_request(config: Any, oc: OutlineCatalog, extra: tuple[str, ...] = ()) -> LLMRequest:
    system, _ = templates()
    parts = [TextPart(user_text(config, oc)), *(TextPart(t) for t in extra)]
    return LLMRequest.from_template(system, parts, schema=response_schema(), max_tokens=max_tokens(config))


def estimate_input_tokens(config: Any, oc: OutlineCatalog) -> int:
    """Rough size (4 characters per token); the real count comes back from the endpoint."""
    system, _ = templates()
    return (len(system.text) + len(user_text(config, oc)) + 3) // 4


@dataclass
class OutlineResult:
    outline: StoryOutline
    report: ValidationReport
    model: str = ""
    usage: Usage = field(default_factory=Usage)
    requests: int = 0
    cache_hits: int = 0
    attempts: list[dict[str, Any]] = field(default_factory=list)   # per ask: findings (errors) and repairs


def generate_outline(gateway: Gateway, config: Any, oc: OutlineCatalog) -> OutlineResult:
    """Ask the story editor, validate and repair deterministically; on errors re-ask with the findings (up to
    `planning.outline_retries` times, default once), then fail rather than silently changing the intent."""
    if not oc.moments:
        raise OutlineError("there are no usable candidate moments to build an outline from (check must_exclude and that the media was indexed)")
    settings = OutlineSettings.from_config(config)
    retries = config.planning.outline_retries
    extra: tuple[str, ...] = ()
    res_usage, model, requests, hits = Usage(), "", 0, 0
    history: list[dict[str, Any]] = []
    report: ValidationReport | None = None
    for attempt in range(retries + 1):
        resp = gateway.complete(build_request(config, oc, extra))
        requests += 0 if resp.cached else 1
        hits += int(resp.cached)
        res_usage = res_usage + (Usage() if resp.cached else resp.usage)
        model = resp.model or model
        report = validate_outline(document_from_response(resp.parsed), oc, settings, repair=True)
        history.append({"attempt": attempt + 1, "fingerprint": resp.fingerprint, "cached": resp.cached, "errors": [i.to_dict() for i in report.errors],
                        "warnings": [i.to_dict() for i in report.warnings], "repairs": [r.to_dict() for r in report.repairs]})
        if report.ok and report.plan is not None:
            return OutlineResult(report.plan, report, model, res_usage, requests, hits, history)
        extra = ("Your previous reply was:\n" + json.dumps(resp.parsed, ensure_ascii=False, indent=1),
                 "It has these problems that cannot be fixed automatically:\n" + findings_text(report) +
                 "\nReply again with the complete corrected JSON object. Keep everything that was fine.")
    assert report is not None
    raise OutlineError("the story editor's outline failed validation" + (" after a corrected reply was requested" if retries else "") + ":\n"
                       + findings_text(report), report, history)
