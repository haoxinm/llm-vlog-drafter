from __future__ import annotations

import json

import pytest
from outline_helpers import OutlineTransport, _scene, default_draft, draft, make_outline_ctx, outline_config

from trip_vlog import outline_stages as os_
from trip_vlog.logging_utils import Status
from trip_vlog.orchestrator import ApprovalRefused, approve_outline, approve_pending
from trip_vlog.planning import outline as ol
from trip_vlog.state import Manifest, StageResult, StageRunner


@pytest.fixture(autouse=True)
def _reset_hook():
    yield
    os_.gateway_factory = None


def runner_for(ctx):
    r = StageRunner(ctx.manifest, ctx.logger, ctx)
    r.register("analyze", lambda c: StageResult(Status.SUCCEEDED, {}), always_run=True)
    os_.register_outline_stages(r, after="analyze")
    return r


def plans(ctx):
    return ctx.workspace.root / "plans"


def load(ctx):
    return json.loads((plans(ctx) / "story_outline.json").read_text(encoding="utf-8"))


def save(ctx, doc):
    (plans(ctx) / "story_outline.json").write_text(json.dumps(doc, indent=2), encoding="utf-8")


def test_outline_stage_writes_outline_summary_report_and_schemas(tmp_path):
    ctx, tr, ids = make_outline_ctx(tmp_path)
    res = os_.outline_stage(ctx)
    assert res.status == Status.SUCCEEDED and len(tr.calls) == 1
    p = plans(ctx)
    doc = load(ctx)
    assert doc["schema_version"] == "1" and [s["scene_id"] for s in doc["scenes"]] == ["scene_01", "scene_02", "scene_03"]
    ol.StoryOutline.model_validate(doc)
    assert {"story_outline.md", "story_outline_report.json", "story_outline.schema.json", "story_outline_response.schema.json"} <= {f.name for f in p.iterdir()}
    rep = json.loads((p / "story_outline_report.json").read_text(encoding="utf-8"))
    assert rep["ok"] and rep["model"] == "test-model-resolved" and rep["prompt"]["system"]["id"] == "story_editor" and rep["prompt"]["system"]["version"] == 1
    assert rep["usage"]["input_tokens"] == 1000 and rep["selection"]["kept"] == 9 and rep["attempts"][0]["errors"] == []
    s = res.summary
    assert s["scenes"] == 3 and s["moments_in_prompt"] == 9 and s["requests"] == 1 and s["local"] is False and s["endpoint"] == "main"
    assert ctx.manifest.get("outline")["outline_hash"] == s["outline_hash"] == ol.outline_file_hash(p / "story_outline.json")
    sent = tr.calls[0].body
    assert sent["system"].startswith("You are an experienced personal travel-vlog editor") and "test-model" == sent["model"]
    assert "sk-ant" not in json.dumps(rep)                                                         # no credentials in artifacts
    assert (p / "story_outline.md").read_text(encoding="utf-8").startswith("# Trip Vlog: story outline")


def test_markdown_summary_is_readable_and_explains_approval(tmp_path):
    def respond(call, d):
        ids = call.ids
        d["scenes"][0].update(title="Arrival", role="arrival_context", chronology_note=None)
        d["scenes"][1]["chronology_note"] = "Told after lunch for contrast."
        d["missing_coverage"] = ["No shot of the hotel room."]
        d["uncertainties"] = ["Is the market the one in the old town?"]
        return d

    ctx, tr, ids = make_outline_ctx(tmp_path, transport=OutlineTransport(respond))
    os_.outline_stage(ctx)
    md = (plans(ctx) / "story_outline.md").read_text(encoding="utf-8")
    for needle in ("Premise: A short test trip.", "## 1. Arrival", "role: arrival_context", "Walking out of the station #0", "Look at all this fruit",
                   "Out of chronological order: Told after lunch", "## Missing coverage", "No shot of the hotel room.", "## Please confirm",
                   "Is the market the one in the old town?", "Opening hook: no", "against a target of 60 s", "interest 0.4",
                   "edit `plans/story_outline.json`", f"trip-vlog approve --output {ctx.workspace.root}", "trip-vlog resume", "approved again"):
        assert needle in md, needle
    assert ids["day1/arrival.mp4"][0] in md and "Checks" not in md


def test_stage_caches_on_inputs_and_target_duration_changes_the_outline(tmp_path):
    ctx, tr, _ = make_outline_ctx(tmp_path)
    r = runner_for(ctx)
    st = r.run()
    assert st["outline"] == Status.SUCCEEDED and st["outline_approval"] == Status.AWAITING_APPROVAL and len(tr.calls) == 1
    st = r.run()                                                                                  # re-run: no LLM call, nothing rewritten
    assert st["outline"] == Status.SKIPPED and ctx.manifest.stage("outline")["decision"] == "cache_hit" and len(tr.calls) == 1
    ctx.config.project.target_duration_seconds = 90
    st = r.run()
    assert st["outline"] == Status.SUCCEEDED and len(tr.calls) == 2 and tr.calls[1].target == 90
    assert ol.StoryOutline.model_validate(load(ctx)).total_seconds() == pytest.approx(90, abs=0.05)
    assert ctx.manifest.stage("outline_approval")["status"] == "awaiting_approval"
    # other inputs that shape the request also invalidate it: editorial settings, the catalog, the model
    ctx.config.editorial.preferred_topics = ["food"]
    assert r.run()["outline"] == Status.SUCCEEDED and len(tr.calls) == 3
    ctx.config.llm.endpoints["main"].model = "other-test-model"
    assert r.run()["outline"] == Status.SUCCEEDED and len(tr.calls) == 4
    row = ctx.catalog.get_moment(tr.calls[0].ids[0])
    m = json.loads(row["data_json"])
    m["description"] = "New label"
    ctx.catalog.upsert_moments(row["asset_id"], row["content_version"], [m])
    assert r.run()["outline"] == Status.SUCCEEDED and len(tr.calls) == 5
    assert r.run()["outline"] == Status.SKIPPED and len(tr.calls) == 5


def test_deleted_outline_is_rebuilt_from_the_response_cache_without_a_new_request(tmp_path):
    ctx, tr, _ = make_outline_ctx(tmp_path)
    r = runner_for(ctx)
    r.run()
    (plans(ctx) / "story_outline.json").unlink()
    st = r.run()
    assert st["outline"] == Status.SUCCEEDED and len(tr.calls) == 1 and ctx.manifest.stage("outline")["summary"]["response_cache_hits"] == 1
    assert (plans(ctx) / "story_outline.json").is_file()


def test_approval_stop_then_approve_then_auto(tmp_path):
    ctx, tr, _ = make_outline_ctx(tmp_path)
    r = runner_for(ctx)
    assert r.run()["outline_approval"] == Status.AWAITING_APPROVAL
    ap = ctx.manifest.get("outline_approval")
    assert ap["status"] == "pending" and ap["outline_hash"] == ol.outline_file_hash(plans(ctx) / "story_outline.json")
    h = ap["outline_hash"]
    os_.approve_current_outline(ctx.manifest, h, "manual")
    assert r.run()["outline_approval"] == Status.SUCCEEDED
    assert ctx.manifest.get("outline_approval")["mode"] == "manual" and len(tr.calls) == 1
    # automatic approval is recorded as such
    ctx2, tr2, _ = make_outline_ctx(tmp_path / "auto", cfg=outline_config())
    ctx2.config.workflow.approval = "auto_approve"
    assert runner_for(ctx2).run()["outline_approval"] == Status.SUCCEEDED
    assert ctx2.manifest.get("outline_approval")["mode"] == "automatic" and "auto" in (plans(ctx2) / "story_outline.md").read_text(encoding="utf-8").lower()


def test_edited_outline_invalidates_approval_and_is_revalidated_without_the_llm(tmp_path):
    ctx, tr, ids = make_outline_ctx(tmp_path)
    r = runner_for(ctx)
    r.run()
    os_.approve_current_outline(ctx.manifest, ol.outline_file_hash(plans(ctx) / "story_outline.json"), "manual")
    assert r.run()["outline_approval"] == Status.SUCCEEDED
    doc = load(ctx)
    doc["scenes"][0]["title"] = "My own title"
    doc["scenes"][0]["target_duration_seconds"] += 1
    doc["scenes"][1]["target_duration_seconds"] -= 1
    save(ctx, doc)
    st = r.run()
    assert st["outline"] == Status.SKIPPED and st["outline_approval"] == Status.AWAITING_APPROVAL and len(tr.calls) == 1   # no LLM call
    ap = ctx.manifest.get("outline_approval")
    assert ap["status"] == "invalidated" and ap["previous_outline_hash"] != ap["outline_hash"]
    assert "outline changed since approval" in ctx.manifest.stage("outline_approval")["summary"]["invalidated"]
    assert "My own title" in (plans(ctx) / "story_outline.md").read_text(encoding="utf-8")        # the readable summary follows the edit
    # whitespace/key order alone does not change the approval hash
    os_.approve_current_outline(ctx.manifest, ol.outline_file_hash(plans(ctx) / "story_outline.json"), "manual")
    (plans(ctx) / "story_outline.json").write_text(json.dumps(load(ctx), indent=4, sort_keys=True), encoding="utf-8")
    assert r.run()["outline_approval"] == Status.SUCCEEDED


def test_invalid_edit_fails_the_approval_stage_with_findings_and_never_rewrites_the_file(tmp_path):
    ctx, tr, ids = make_outline_ctx(tmp_path)
    r = runner_for(ctx)
    r.run()
    doc = load(ctx)
    doc["scenes"][0]["candidate_moment_ids"].append("moment_ffffffffffffffff")
    save(ctx, doc)
    raw = (plans(ctx) / "story_outline.json").read_text(encoding="utf-8")
    st = r.run()
    assert st["outline_approval"] == Status.FAILED and (plans(ctx) / "story_outline.json").read_text(encoding="utf-8") == raw
    err = ctx.manifest.stage("outline_approval")["error"]
    assert "unknown_moment" in err and "moment_ffffffffffffffff" in err
    assert json.loads((plans(ctx) / "story_outline_validation.json").read_text(encoding="utf-8"))["ok"] is False
    (plans(ctx) / "story_outline.json").write_text("{not json", encoding="utf-8")
    assert r.run()["outline_approval"] == Status.FAILED and "not readable JSON" in ctx.manifest.stage("outline_approval")["error"]
    (plans(ctx) / "story_outline.json").write_text("{}", encoding="utf-8")
    assert r.run()["outline_approval"] == Status.FAILED and "premise" in ctx.manifest.stage("outline_approval")["error"]


def test_regenerating_keeps_a_copy_of_an_outline_the_user_edited(tmp_path):
    ctx, tr, _ = make_outline_ctx(tmp_path)
    r = runner_for(ctx)
    r.run()
    doc = load(ctx)
    doc["premise"] = "MY HAND-WRITTEN PREMISE"
    save(ctx, doc)
    ctx.config.project.target_duration_seconds = 90          # an input change forces a new outline
    r.run()
    baks = list(plans(ctx).glob("story_outline.*.bak.json"))
    assert len(baks) == 1 and json.loads(baks[0].read_text(encoding="utf-8"))["premise"] == "MY HAND-WRITTEN PREMISE"
    assert ctx.manifest.stage("outline")["summary"]["edited_outline_backup"].endswith(baks[0].name)
    ctx.config.project.target_duration_seconds = 120         # an unedited outline is simply replaced
    r.run()
    assert len(list(plans(ctx).glob("story_outline.*.bak.json"))) == 1


def test_failed_outline_blocks_approval_and_writes_a_report(tmp_path):
    def respond(call, d):
        d["scenes"][1]["candidate_moment_ids"] = ["moment_0000000000000000"]
        return d

    ctx, tr, _ = make_outline_ctx(tmp_path, transport=OutlineTransport(respond))
    st = runner_for(ctx).run()
    assert st["outline"] == Status.FAILED and st["outline_approval"] == Status.SKIPPED and len(tr.calls) == 2
    assert "empty_scene" in ctx.manifest.stage("outline")["error"] and not (plans(ctx) / "story_outline.json").exists()
    rep = json.loads((plans(ctx) / "story_outline_report.json").read_text(encoding="utf-8"))
    assert rep["ok"] is False and len(rep["attempts"]) == 2


def test_llm_errors_fail_the_stage_with_a_clear_message(tmp_path):
    from trip_vlog.llm.transport import HttpResponse

    err = HttpResponse(401, {}, json.dumps({"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}).encode())
    ctx, tr, _ = make_outline_ctx(tmp_path, transport=OutlineTransport(lambda c, d: err))
    st = runner_for(ctx).run()
    assert st["outline"] == Status.FAILED and "LLMAuthError" in ctx.manifest.stage("outline")["error"]


def test_dry_run_reports_prompt_size_endpoint_and_cache_state(tmp_path):
    cfg = outline_config(planning={"max_moments_in_prompt": 5})
    ctx, tr, _ = make_outline_ctx(tmp_path, cfg=cfg)
    rep = os_.dry_run_report(ctx)
    assert rep["moments_in_prompt"] == 5 and rep["eligible"] == 9 and rep["left_out"] == 4 and rep["where"] == "remote" and rep["endpoint"] == "main"
    assert rep["model"] == "test-model" and rep["with_speech"] in (0, 1) and rep["estimated_input_tokens"] > 100 and rep["already_outlined"] is False
    text = os_.format_dry_run(rep)
    assert "nothing is sent" in text and "5 (limit planning.max_moments_in_prompt = 5)" in text and "remote" in text and "leave this machine" in text
    assert tr.calls == []
    runner_for(ctx).run()
    assert os_.dry_run_report(ctx)["already_outlined"] is True
    ep = {"api": "openai_chat", "base_url": "http://127.0.0.1:8080", "api_key_env": None, "model": "test-model"}
    local = outline_config(llm={"endpoints": {"main": ep}})
    assert os_.dry_run_report(type(ctx)(**{**ctx.__dict__, "config": local}))["where"] == "local"


# ---- approve (workspace on disk) ---------------------------------------------------------------------------------
def workspace_on_disk(tmp_path, **kw):
    ctx, tr, ids = make_outline_ctx(tmp_path, **kw)
    from trip_vlog.state import atomic_write_text

    atomic_write_text(ctx.workspace.project_yaml, ctx.config.to_yaml())
    ctx.manifest.set(media_root=str(ctx.paths.media_root), pipeline="outline")
    ctx.manifest.save()
    return ctx, tr


def test_approve_outline_records_manual_approval_and_resume_continues(tmp_path):
    ctx, tr = workspace_on_disk(tmp_path)
    r = runner_for(ctx)
    assert r.run()["outline_approval"] == Status.AWAITING_APPROVAL
    kind, ap = approve_pending(ctx.workspace.root)                                                 # outline is what is waiting
    assert kind == "outline" and ap["mode"] == "manual" and ap["outline_hash"] == ol.outline_file_hash(plans(ctx) / "story_outline.json")
    fresh = Manifest.load(ctx.workspace.manifest_path)
    assert fresh.get("outline_approval")["status"] == "approved"
    ctx.manifest = Manifest.load(ctx.workspace.manifest_path)
    r2 = runner_for(ctx)
    assert r2.run()["outline_approval"] == Status.SUCCEEDED and len(tr.calls) == 1


def test_approve_refuses_an_invalid_or_missing_outline(tmp_path):
    ctx, tr = workspace_on_disk(tmp_path)
    with pytest.raises(ApprovalRefused, match="no successful story outline"):
        approve_outline(ctx.workspace.root)
    runner_for(ctx).run()
    doc = load(ctx)
    doc["scenes"][0]["candidate_moment_ids"] = ["moment_ffffffffffffffff"]
    save(ctx, doc)
    with pytest.raises(ApprovalRefused, match="invalid story outline"):
        approve_outline(ctx.workspace.root)
    assert (Manifest.load(ctx.workspace.manifest_path).get("outline_approval") or {}).get("status") != "approved"


def test_approve_picks_what_is_waiting_and_honors_explicit_choice(tmp_path):
    ctx, tr = workspace_on_disk(tmp_path)
    runner_for(ctx).run()
    ctx.manifest.stages["approval"] = {"status": "awaiting_approval"}                             # an edit plan is waiting too (4b)
    ctx.manifest.save()
    assert approve_pending(ctx.workspace.root)[0] == "outline"                                    # outline comes first
    ctx.manifest.stages["outline_approval"]["status"] = "succeeded"
    ctx.manifest.save()
    with pytest.raises(ApprovalRefused, match="no successful plan yet"):
        approve_pending(ctx.workspace.root)                                                       # now the plan is next, and there is none
    with pytest.raises(ApprovalRefused, match="no successful plan yet"):
        approve_pending(ctx.workspace.root, "plan")
    with pytest.raises(ValueError):
        approve_pending(ctx.workspace.root, "everything")
