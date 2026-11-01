"""Handoff compatibility report (Phase 5a): rows, verdicts, recorded Resolve evidence and the resolve-finish override."""
from __future__ import annotations

import json
from fractions import Fraction

from qc_helpers import A1, A2, clip, make_plan
from xml_helpers import xml_for_spec

from trip_vlog.filmcraft.plan_mapping import edit_plan_to_spec
from trip_vlog.handoff import compatibility as K
from trip_vlog.handoff import resolve_evidence as RE
from trip_vlog.planning.catalog_view import AssetInfo, PlanningCatalog

CAPS = {n: True for n in ("create_sequence", "place_clip", "add_transition", "add_title", "add_marker", "audio_gain")}


def world(tmp_path, still_name="IMG_1042.JPG"):
    cat = PlanningCatalog([AssetInfo(A1, "a/clip.mp4", "video", 20.0, Fraction(30), 1920, 1080, 1920, 1080, 1),
                           AssetInfo(A2, f"a/{still_name}", "image", None, None, 1920, 1080, 1920, 1080, 0)], [])
    plan = make_plan(("scene_01", [clip("c1", 0, 90, policy="ambient_low"), clip("c2", 90, 60, asset=A2, media="image", dissolve=15)]),
                     titles=[{"title_id": "t1", "text": "Hi", "role": "opening_title", "scene_id": "scene_01", "text_source": "project_title",
                              "start": {"relative_to": "scene_start", "clip_instance_id": None, "offset_seconds": 0.0}, "start_frame": 0,
                              "duration_frames": 60, "duration_seconds": 2.0, "track": "V2", "style": "simple"}],
                     markers=[{"marker_id": "m1", "name": "Scene 1", "frame": 0, "scene_id": "scene_01"}])
    spec = edit_plan_to_spec(plan, cat, tmp_path).spec
    return plan, cat, spec


def build(tmp_path, xml=None, caps=CAPS, finish=None, **kw):
    plan, cat, spec = world(tmp_path, **kw)
    xp = None
    if xml is not False:
        xp = tmp_path / "timeline.xml"
        xp.write_text(xml or xml_for_spec(spec), encoding="utf-8")
    return K.build_compatibility_report(K.CompatInputs(plan, spec, cat, caps, xp, "0.13.0", True, finish_report=finish, tool_version="t")), spec


def rows(doc):
    return {r["feature"]: r for r in doc["features"]}


def test_rows_for_the_features_the_plan_uses_with_recorded_verdicts(tmp_path):
    doc, _ = build(tmp_path)
    r = rows(doc)
    assert set(r) == {"timeline_settings", "dissolves", "stills", "titles", "markers", "audio_gain"} and "cuts" not in r   # one hard cut-free V1
    assert doc["xml"]["roundtrip"]["ok"] and doc["xml"]["status"] == "ok"
    assert {k: r[k]["verdict"] for k in r} == {"timeline_settings": "expected", "dissolves": "expected", "stills": "manual", "titles": "manual",
                                               "markers": "verified", "audio_gain": "expected"}
    assert r["stills"]["instruction"] and "resolve-finish" in r["stills"]["instruction"] and r["titles"]["instruction"]
    assert r["audio_gain"]["resolve"]["status"] == "unverified" and r["audio_gain"]["plan_count"] == 1
    assert r["titles"]["plan_count"] == 1 and r["dissolves"]["xml"]["matched"] == 1
    assert r["markers"]["filmcraft"]["capability"] == "add_marker" and r["markers"]["resolve"]["source"] == "recorded_evidence"
    assert doc["overall"] == "attention" and doc["counts"] == {"verified": 1, "expected": 3, "manual": 2, "unsupported": 0, "unknown": 0}
    json.dumps(doc)


def test_stills_without_digits_are_not_claimed_broken_or_fine(tmp_path):
    doc, _ = build(tmp_path, still_name="beach.jpg")
    s = rows(doc)["stills"]
    assert s["resolve"]["status"] == "unverified" and s["verdict"] == "expected" and s["instruction"] is None


def test_evidence_table_only_claims_what_is_recorded():
    proven = {f for f, e in RE.EVIDENCE.items() if e.result == RE.PROVEN}
    assert proven == {"overlays_v2", "markers"}      # cuts and dissolves are PARTIAL: no hard cut between two clips / one 15-frame dissolve recorded
    assert RE.EVIDENCE["cuts"].result == RE.PARTIAL and RE.EVIDENCE["dissolves"].result == RE.PARTIAL and RE.EVIDENCE_VERSION == 2
    assert "audio levels) was not reported yet" in RE.EVIDENCE["audio_gain"].reference
    for f in ("audio_gain", "audio_mute", "audio_clips", "mixed_fps", "mixed_resolution", "speed_changes"):
        assert RE.EVIDENCE[f].result == RE.UNVERIFIED
    assert RE.EVIDENCE["titles"].result == RE.ABSENT and RE.EVIDENCE["stills"].result == RE.NEEDS_REPAIR
    assert RE.RESOLVE_TARGET["version"] == "21.1.1.10" and all(e.reference for e in RE.EVIDENCE.values())


def test_xml_mismatch_makes_the_feature_unsupported(tmp_path):
    _, spec = build(tmp_path)
    xml = xml_for_spec(spec, mutate=lambda d: d.update(markers=[]))
    doc, _ = build(tmp_path, xml=xml)
    m = rows(doc)["markers"]
    assert m["verdict"] == "unsupported" and m["xml"]["status"] == "mismatch" and m["xml"]["mismatches"][0]["kind"] == "marker"
    assert doc["overall"] == "problem" and doc["xml"]["roundtrip"]["ok"] is False
    assert K.summary_of(doc)["xml_roundtrip_ok"] is False


def test_missing_filmcraft_capability_and_missing_xml(tmp_path):
    doc, _ = build(tmp_path, caps={**CAPS, "add_transition": False})
    assert rows(doc)["dissolves"]["verdict"] == "unsupported" and rows(doc)["dissolves"]["filmcraft"]["status"] == "unsupported"
    doc, _ = build(tmp_path, xml=False)
    # no XML: the rows are "unknown" (not checked), never "unsupported", and the overall result is not a problem
    assert doc["xml"]["status"] == "not_exported" and doc["overall"] != "problem" and doc["counts"]["unsupported"] == 0
    r = rows(doc)
    assert r["markers"]["verdict"] == "unknown" and "not exported" in r["markers"]["reason"] and r["markers"]["instruction"] is None
    assert all(x["verdict"] in ("unknown", "manual") for x in doc["features"]) and r["dissolves"]["xml"]["status"] == "not_exported"
    assert doc["counts"]["unknown"] == sum(1 for x in doc["features"] if x["verdict"] == "unknown") > 0
    md = K.render_compatibility_markdown(doc)
    assert "## Not checked" in md and "unknown" in md and "## Unsupported or not carried" not in md
    (tmp_path / "timeline.xml").write_text("<nope", encoding="utf-8")
    plan, cat, spec = world(tmp_path)
    doc = K.build_compatibility_report(K.CompatInputs(plan, spec, cat, None, tmp_path / "timeline.xml"))
    assert doc["xml"]["status"] == "unreadable" and rows(doc)["markers"]["filmcraft"]["status"] == "unknown"


def test_recorded_manual_steps_survive_a_missing_xml(tmp_path):
    doc, _ = build(tmp_path, xml=False)
    r = rows(doc)
    assert r["titles"]["verdict"] == "manual" and r["titles"]["instruction"] and r["stills"]["verdict"] == "manual" and "resolve-finish" in r["stills"]["instruction"]
    assert r["markers"]["verdict"] == "unknown" and doc["overall"] == "attention" and doc["counts"]["unknown"] > 0


def finish_doc(sha, **over):
    checks = [{"check": c, "status": "pass", "detail": "ok", "required": True} for c in ("fps", "duration", "media_online", "image_sequence_misdetected",
                                                                                      "transitions", "markers", "titles")]
    return {"status": "pass", "dry_run": False, "resolve": {"product": "DaVinci Resolve Studio", "version": "21.2"}, "finished_at": "2026-10-01T00:00:00Z",
            "inputs": {"timeline_xml_sha256": sha}, "final_checks": checks, **over}


def test_finish_report_overrides_recorded_evidence_for_its_checks(tmp_path):
    doc, _ = build(tmp_path)
    sha = doc["xml"]["sha256"]
    doc, _ = build(tmp_path, finish=finish_doc(sha))
    r = rows(doc)
    assert doc["resolve_finish_report"]["used"] is True
    assert r["stills"]["verdict"] == "verified" and r["stills"]["resolve"]["source"] == "resolve_finish_report" and r["stills"]["instruction"] is None
    assert r["titles"]["verdict"] == "verified" and r["audio_gain"]["resolve"]["source"] == "recorded_evidence"   # gain is not covered by any check
    assert r["timeline_settings"]["resolve"]["checks"] and K.summary_of(doc)["finish_report_used"]


def test_failed_finish_check_needs_manual_work_and_stale_or_dry_runs_are_ignored(tmp_path):
    doc, _ = build(tmp_path)
    sha = doc["xml"]["sha256"]
    bad = finish_doc(sha)
    bad["final_checks"] = [dict(c, status="fail", detail="2 titles missing") if c["check"] == "titles" else c for c in bad["final_checks"]]
    doc, _ = build(tmp_path, finish=bad)
    t = rows(doc)["titles"]
    assert t["verdict"] == "manual" and "2 titles missing" in t["resolve"]["summary"]
    for f in (finish_doc("0" * 64), finish_doc(sha, dry_run=True), finish_doc(sha, status="manual_required")):
        d, _ = build(tmp_path, finish=f)
        assert d["resolve_finish_report"]["used"] is False and d["resolve_finish_report"]["reason"]
        assert rows(d)["stills"]["resolve"]["source"] == "recorded_evidence"


def test_finish_report_without_a_recorded_hash_or_without_timeline_xml_is_not_used(tmp_path):
    doc, _ = build(tmp_path)
    sha = doc["xml"]["sha256"]
    no_hash = finish_doc(sha)
    no_hash["inputs"] = {}
    d, _ = build(tmp_path, finish=no_hash)
    assert d["resolve_finish_report"]["used"] is False and "does not record" in d["resolve_finish_report"]["reason"]
    assert rows(d)["stills"]["resolve"]["source"] == "recorded_evidence"
    d, _ = build(tmp_path, xml=False, finish=finish_doc(sha))
    assert d["resolve_finish_report"]["used"] is False and "timeline.xml" in d["resolve_finish_report"]["reason"]
    ok = K.usable_finish_checks(finish_doc(sha), sha)[1]
    assert ok["used"] is True and K.usable_finish_checks(finish_doc(sha), None)[1]["used"] is False


def test_dissolve_the_engine_placed_as_a_hard_cut_is_never_verified(tmp_path):
    from trip_vlog.filmcraft.timeline_spec import downgrade_dissolves

    plan, cat, spec = world(tmp_path)
    eff = downgrade_dissolves(spec, ["c2"])                      # what the adapter does when the engine cannot place the dissolve
    xp = tmp_path / "timeline.xml"
    xp.write_text(xml_for_spec(eff), encoding="utf-8")           # the XML matches what was sent to FilmCraft: a hard cut
    doc = K.build_compatibility_report(K.CompatInputs(plan, eff, cat, CAPS, xp, "0.13.0", True, tool_version="t"))
    r = rows(doc)["dissolves"]
    assert doc["xml"]["roundtrip"]["ok"] is True                 # nothing is wrong with the export itself ...
    assert r["plan_count"] == 1 and r["xml"]["expected"] == 1 and r["xml"]["matched"] == 0 and r["xml"]["status"] == "downgraded"
    assert r["filmcraft"]["status"] == "downgraded" and "hard cut" in r["filmcraft"]["detail"]
    assert r["verdict"] == "manual" and "add a cross dissolve" in r["instruction"] and "c2" in r["instruction"]   # ... but the plan's dissolve is missing
    assert r["details"]["downgraded"] == 1 and doc["overall"] == "attention"
    md = K.render_compatibility_markdown(doc)
    assert "## Manual steps in Resolve" in md and "downgraded (0/1)" in md
    # the same plan with the dissolve placed is "expected" (the recorded Resolve result for dissolves is only partial), not manual
    ok, _ = build(tmp_path)
    assert rows(ok)["dissolves"]["verdict"] == "expected" and rows(ok)["dissolves"]["xml"]["status"] == "ok"


def test_mixed_resolution_flags_a_different_aspect_ratio_but_not_plain_scaling(tmp_path):
    def usage_for(w, h, disp=None):
        cat = PlanningCatalog([AssetInfo(A1, "a/clip.mp4", "video", 20.0, Fraction(30), w, h, *(disp or (w, h)), 1)], [])
        plan = make_plan(("s", [clip("c1", 0, 90)]))
        spec = edit_plan_to_spec(plan, cat, tmp_path).spec
        return K.plan_usage(plan, spec, cat)["mixed_resolution"]

    assert usage_for(3840, 2160).count == 0 and usage_for(1280, 720).count == 0           # same 16:9, only scaled
    assert usage_for(3840, 2160).data["scaled_same_aspect"] == 1
    assert usage_for(1920, 1080).data["scaled_same_aspect"] == 0
    v = usage_for(1080, 1920)                                                             # portrait in a landscape timeline
    assert v.count == 1 and v.data["orientation_differs"] == 1
    assert usage_for(1440, 1080).count == 1 and usage_for(1920, 800).count == 1           # 4:3 and 2.4:1 differ
    assert usage_for(1920, 1080, (1080, 1920)).count == 1                                 # rotated by metadata: displayed portrait


def test_load_finish_report(tmp_path):
    assert K.load_finish_report(tmp_path / "x.json") == (None, None)
    p = tmp_path / "r.json"
    p.write_text("{bad", encoding="utf-8")
    assert K.load_finish_report(p)[0] is None and K.load_finish_report(p)[1]
    p.write_text('{"status": "pass"}', encoding="utf-8")
    assert K.load_finish_report(p)[0] == {"status": "pass"}


def test_markdown_lists_rows_manual_steps_and_roundtrip(tmp_path):
    doc, _ = build(tmp_path)
    md = K.render_compatibility_markdown(doc)
    assert "# Handoff compatibility report" in md and "| Titles (text on V2) | 1 | supported | ok (1/1) |" in md and "## Manual steps in Resolve" in md
    assert "**identical**" in md and "21.1.1.10" in md and "unverified" in md


def phase7b_world(tmp_path):
    from dataclasses import replace

    from trip_vlog.filmcraft.timeline_spec import GainKey, Placement

    plan, cat, spec = world(tmp_path)
    pl = tuple(replace(p, fit_source_size=(1080, 1920)) if p.clip_instance_id == "c1" else p for p in spec.placements)
    music = Placement("m1", str(tmp_path / "music.wav"), "audio", "A2", 0, 150, 0.0, 5.0, False, None, gain_keys=(GainKey(30, 0.0), GainKey(36, -12.0)))
    return plan, cat, replace(spec, placements=pl + (music,))


def test_framing_and_ducking_rows_and_the_readback_xml(tmp_path):
    plan, cat, spec = phase7b_world(tmp_path)
    caps = {**CAPS, "fit_to_frame": True, "clip_keyframe": True}
    xp = tmp_path / "timeline.xml"
    xp.write_text(xml_for_spec(spec), encoding="utf-8")
    doc = K.build_compatibility_report(K.CompatInputs(plan, spec, cat, caps, xp, "0.13.0", True, tool_version="t"))
    r = rows(doc)
    assert r["framing_fit"]["plan_count"] == 1 and r["framing_fit"]["verdict"] == "expected" and r["framing_fit"]["xml"]["matched"] == 1
    assert r["music_ducking"]["plan_count"] == 1 and r["music_ducking"]["verdict"] == "expected" and r["music_ducking"]["details"]["keyframes"] == 2
    assert doc["xml"]["roundtrip"]["ok"] and doc["xml"]["origin"] == "native"
    # an XML generated from the read-back cannot hold the keyframes: reported as a manual step, not as a mismatch
    nokeys = xml_for_spec(spec, mutate=lambda d: [i.__setitem__("keys", ()) for i in d["items"]])
    xp.write_text(nokeys, encoding="utf-8")
    doc = K.build_compatibility_report(K.CompatInputs(plan, spec, cat, caps, xp, "0.13.0", True, tool_version="t", xml_origin="readback",
                                                      readback={"status": "generated"}))
    d = rows(doc)["music_ducking"]
    assert d["xml"]["status"] == "not_carried" and d["verdict"] == "manual" and "Lower the music" in d["instruction"]
    assert doc["xml"]["roundtrip"]["ok"] and doc["xml"]["origin"] == "readback"
    # the same XML offered as a native export IS a mismatch
    doc = K.build_compatibility_report(K.CompatInputs(plan, spec, cat, caps, xp, "0.13.0", True, tool_version="t"))
    assert rows(doc)["music_ducking"]["verdict"] == "unsupported" and not doc["xml"]["roundtrip"]["ok"]


def test_unfitted_mixed_aspect_clips_stay_a_manual_framing_step(tmp_path):
    from fractions import Fraction

    plan, cat, spec = world(tmp_path)
    cat2 = PlanningCatalog([AssetInfo(A1, "a/clip.mp4", "video", 20.0, Fraction(30), 1080, 1920, 1080, 1920, 1), cat.assets[A2]], [])
    xp = tmp_path / "timeline.xml"
    xp.write_text(xml_for_spec(spec), encoding="utf-8")
    doc = K.build_compatibility_report(K.CompatInputs(plan, spec, cat2, CAPS, xp, "0.13.0", True, tool_version="t"))
    m = rows(doc)["mixed_resolution"]
    assert m["verdict"] == "manual" and m["details"]["unfitted"] == 1 and "cropped" in " ".join(m["notes"])
