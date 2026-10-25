"""Tests for ``trip-vlog resolve-finish`` against a stateful fake of Resolve's scripting API (no Resolve needed)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "fixtures" / "resolve"))
import fake_resolve  # noqa: E402
from fake_resolve import FakeResolve  # noqa: E402
from workspace_builder import build_workspace, title  # noqa: E402

from trip_vlog import cli
from trip_vlog.resolve import finish
from trip_vlog.resolve.guard import ALLOWED, MUTATING, AllowlistViolation, ApiSession, media_keys
from trip_vlog.state import Manifest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def inject(monkeypatch):
    """Install a fake Resolve as the connection used by resolve-finish; returns the fake."""
    def _install(fake=None, **kw):
        fake = fake or FakeResolve(**kw)
        monkeypatch.setattr(finish, "resolve_factory", lambda: (fake, {"diagnosis": []}))
        return fake
    return _install


def run(ws: Path, **kw):
    lines: list[str] = []
    res = finish.run_finish(ws, echo=lines.append, **kw)
    return res, lines


def checks(res) -> dict:
    return {c["check"]: c for c in res.report["final_checks"]}


def rj(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def mutations(res) -> list[str]:
    return [c["call"] for c in res.api.log if c.get("mutating") and not c.get("blocked")]


# ---------------------------------------------------------------- success, project naming
def test_success_creates_new_project_and_leaves_existing_alone(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject(misread_stills=False)  # a clean import: nothing to repair
    res, lines = run(w["ws"])
    assert res.exit_code == 0 and res.status == "pass", res.report["final_checks"]
    pm = fake.pm
    assert pm.created == ["Iceland Trip (trip-vlog v001)"]
    assert "My Trip" in pm.projects and pm.projects["My Trip"].timelines == [] and pm.projects["My Trip"].set_setting_calls == []
    assert res.report["project"]["name"] == "Iceland Trip (trip-vlog v001)"
    proj = pm.projects["Iceland Trip (trip-vlog v001)"]
    assert proj.settings == {"timelineFrameRate": "30", "timelineResolutionWidth": "1920", "timelineResolutionHeight": "1080"}
    assert [t.name for t in proj.timelines] == [res.report["timeline"]["name"]]
    assert pm.saved  # SaveProject called
    c = checks(res)
    for k in ("fps", "resolution", "duration", "clip_order", "record_inout", "source_in", "media_online", "media_path",
              "markers", "image_sequence_misdetected", "track_structure"):
        assert c[k]["status"] == "pass", (k, c[k])
    assert res.report["repairs"] == [] and "Result: PASS" in "\n".join(lines)


def test_settings_are_applied_before_import(tmp_path, inject):
    fake = inject()
    run(build_workspace(tmp_path)["ws"])
    proj = fake.pm.projects["Iceland Trip (trip-vlog v001)"]
    assert proj.set_setting_calls and all(had_timelines is False for _, _, had_timelines in proj.set_setting_calls)
    assert len(fake.imports) == 1
    path, opts = fake.imports[0]
    assert Path(path).is_absolute() and path.endswith("timeline.xml")
    assert opts["importSourceClips"] is True and opts["timelineName"].startswith("Main Draft (trip-vlog ")
    assert Path(opts["sourceClipsPath"]) == tmp_path / "media"


def test_name_collision_goes_to_next_version_and_manifest_records_runs(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject(existing_projects=("My Trip", "Iceland Trip (trip-vlog v001)", "Iceland Trip (trip-vlog v002)"))
    res, _ = run(w["ws"])
    assert res.report["project"]["name"] == "Iceland Trip (trip-vlog v003)" and res.exit_code == 0
    assert fake.pm.projects["Iceland Trip (trip-vlog v001)"].timelines == []  # untouched
    # a second run in a fresh Resolve (the earlier project is not listed there) still increments, from the manifest
    inject()
    res2, _ = run(w["ws"])
    assert res2.report["project"]["name"] == "Iceland Trip (trip-vlog v004)"
    runs = Manifest.load(w["ws"] / "manifest.json").get("resolve_finish")["runs"]
    assert [r["project_name"] for r in runs] == ["Iceland Trip (trip-vlog v003)", "Iceland Trip (trip-vlog v004)"]


def test_explicit_project_name_never_reuses_existing(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject()
    res, lines = run(w["ws"], project_name="My Trip")
    assert res.exit_code == 2 and res.status == "name_collision"
    assert fake.pm.created == [] and mutations(res) == []
    assert any("never reuses" in l for l in lines)
    res, _ = run(w["ws"], project_name="Custom Name")
    assert res.exit_code == 0 and fake.pm.created == ["Custom Name"]


def test_project_never_deleted_loaded_or_closed(tmp_path, inject):
    inject()  # FakePM raises AssertionError if DeleteProject / LoadProject / CloseProject / Quit were ever reached
    res, _ = run(build_workspace(tmp_path)["ws"])
    assert res.exit_code == 0
    assert not {"DeleteProject", "LoadProject", "CloseProject"} & {c["call"].split(".")[1] for c in res.api.log}


# ---------------------------------------------------------------- stills misread as image sequences
@pytest.mark.parametrize("still,pattern", [("IMG_1042.JPG", "IMG_[1042-1191].JPG"), ("still_1080p.png", "still_[1080-1229]p.png")])
def test_misread_still_is_repaired_with_replace_clip(tmp_path, inject, still, pattern):
    w = build_workspace(tmp_path, still=still)
    inject()
    res, _ = run(w["ws"])
    init = {c["check"]: c for c in res.report["initial_checks"]}
    assert init["image_sequence_misdetected"]["status"] == "fail" and pattern in init["image_sequence_misdetected"]["detail"]
    assert init["media_online"]["status"] == "fail"
    (rep,) = res.report["repairs"]
    assert rep["result"] == "online" and rep["reason"] == "image sequence misread" and rep["was"]["name"] == pattern
    assert rep["expected_path"] == str(w["media"] / still)
    replace = [c for c in res.api.log if c["call"] == "mpi.ReplaceClip"]
    assert len(replace) == 1 and replace[0]["args"][0].endswith(still)
    c = checks(res)
    assert res.status == "pass"
    assert c["media_online"]["status"] == c["image_sequence_misdetected"]["status"] == c["clip_order"]["status"] == "pass"
    assert c["record_inout"]["status"] == "pass"  # record range unchanged by the repair


def test_missing_source_file_is_not_repaired_and_reported(tmp_path, inject):
    w = build_workspace(tmp_path, still="IMG_1042.JPG")
    (w["media"] / "IMG_1042.JPG").unlink()
    inject()
    res, _ = run(w["ws"])
    assert res.status == "fail" and res.exit_code == 1
    (rep,) = res.report["repairs"]
    assert rep["result"] == "skipped" and "does not exist" in rep["detail"]
    assert "mpi.ReplaceClip" not in {c["call"] for c in res.api.log}
    assert any("missing on disk" in n for n in res.report["notes"])


def test_offline_media_that_is_not_a_sequence_is_relinked_by_replace_clip(tmp_path, inject):
    w = build_workspace(tmp_path)
    inject(offline_all=True, misread_stills=False)
    res, _ = run(w["ws"])
    assert res.status == "pass" and {r["reason"] for r in res.report["repairs"]} == {"offline"}
    assert {c["args"][0].rsplit("/", 1)[-1].rsplit("\\", 1)[-1] for c in res.api.log if c["call"] == "mpi.ReplaceClip"} \
        == {"landscape_5s.mp4", "still_1080p.png", "music_6s.wav"}


def test_replace_clip_refused_outside_manifest(tmp_path):
    inside, outside = tmp_path / "a.png", tmp_path / "evil.png"
    for p in (inside, outside):
        p.write_bytes(b"x")
    s = ApiSession(allowed_media=media_keys([str(inside)]))
    mpi = fake_resolve.FakeMPI(str(tmp_path / "x_[1-9].png"))
    g = s.wrap(mpi, "mpi")
    with pytest.raises(AllowlistViolation, match="not listed in media_manifest.csv"):
        g.ReplaceClip(str(outside))
    assert mpi.replace_calls == []
    assert g.ReplaceClip(str(inside)) is True and mpi.replace_calls == [str(inside)]
    assert [c.get("blocked") for c in s.log] == [True, None]


def test_repair_refuses_expected_path_not_in_manifest(tmp_path, inject):
    w = build_workspace(tmp_path, still="IMG_1042.JPG")
    inject()
    inp = finish.load_inputs(w["ws"])
    inp.rows = [r for r in inp.rows if not r["absolute_path"].endswith("IMG_1042.JPG")]  # simulate a manifest without the still
    session = ApiSession(allowed_media=media_keys(inp.manifest_paths))
    fake = FakeResolve()
    proj = fake.pm.CreateProject("p")
    tl = proj.mp.ImportTimelineFromFile(str(inp.xml_path), {"timelineName": "t"})
    g = session.wrap(tl, "timeline")
    from trip_vlog.resolve.dump import dump_timeline
    from trip_vlog.resolve.expected import expected_from_spec
    exp = expected_from_spec(inp.spec, title_names={})
    r = finish.repair_media(finish.FinishRun(inp, session, "S", False, echo=lambda *_: None), g, exp, dump_timeline(g))
    assert r[0]["result"] == "refused" and "not listed in media_manifest.csv" in r[0]["detail"]
    assert "mpi.ReplaceClip" not in {c["call"] for c in session.log}


# ---------------------------------------------------------------- stills with numbered siblings: isolated copy fallback
def _siblings_ws(tmp_path, still="261010_stars_-318.jpg"):
    w = build_workspace(tmp_path, still=still)
    (w["media"] / "261010_stars_-319.jpg").write_bytes(b"y" * 64)  # numbered sibling in the same folder
    return w


def _snapshot(p: Path):
    return p.read_bytes(), p.stat().st_mtime_ns


def test_still_with_numbered_siblings_falls_back_to_isolated_copy(tmp_path, inject):
    w = _siblings_ws(tmp_path)
    orig = w["media"] / "261010_stars_-318.jpg"
    before = {p.name: _snapshot(p) for p in w["media"].iterdir()}
    fake = inject()
    res, lines = run(w["ws"])
    (rep,) = res.report["repairs"]
    staged = w["handoff"] / "stills" / finish.stills_key(str(orig)) / orig.name
    assert rep["result"] == "online" and rep["replace_returned"] is True and rep["staged_copy"] == str(staged)
    assert [a["returned"] for a in rep["attempts"]] == [False, True]
    assert rep["attempts"][0]["path"] == str(orig) and rep["attempts"][1]["path"] == str(staged)
    assert "isolated copy" in rep["detail"] and "resolve_handoff/stills" in rep["detail"]
    assert staged.read_bytes() == orig.read_bytes() and not staged.is_symlink() and staged.stat().st_nlink == 1
    assert sorted(p.name for p in staged.parent.iterdir()) == [orig.name]  # no numbered siblings
    assert {p.name: _snapshot(p) for p in w["media"].iterdir()} == before  # originals untouched
    assert res.status == "pass", res.report["final_checks"]
    c = checks(res)
    assert c["media_online"]["status"] == c["media_path"]["status"] == c["image_sequence_misdetected"]["status"] == "pass"
    assert "1 via isolated copies in resolve_handoff/stills" in c["media_path"]["detail"]
    assert res.report["staged_copies"] == [{"copy": str(staged), "source": str(orig)}]
    md = (w["handoff"] / "resolve_finish_report.md").read_text(encoding="utf-8")
    assert "Isolated copies (1)" in md and "resolve_handoff/stills/" in md and str(staged) in md
    mpis = [c for c in res.api.log if c["call"] == "mpi.ReplaceClip"]
    assert len(mpis) == 2 and not any(c.get("blocked") for c in mpis)


def test_rerun_reuses_the_isolated_copy(tmp_path, inject):
    w = _siblings_ws(tmp_path)
    inject()
    res, _ = run(w["ws"])
    staged = Path(res.report["repairs"][0]["staged_copy"])
    snap = _snapshot(staged)
    inject()
    res2, _ = run(w["ws"])
    assert res2.status == "pass" and Path(res2.report["repairs"][0]["staged_copy"]) == staged
    assert _snapshot(staged) == snap  # not recopied
    assert len(list((w["handoff"] / "stills").iterdir())) == 1
    staged.write_bytes(b"short")  # different size: recopied from the original
    inject()
    res3, _ = run(w["ws"])
    assert res3.status == "pass" and staged.read_bytes() == (w["media"] / "261010_stars_-318.jpg").read_bytes()


def test_same_source_staged_once(tmp_path):
    w = _siblings_ws(tmp_path)
    inp = finish.load_inputs(w["ws"])
    r = finish.FinishRun(inp, ApiSession(), "S", False, echo=lambda *_: None)
    src = str(w["media"] / "261010_stars_-318.jpg")
    a = finish.stage_still(r, src)
    m = a.stat().st_mtime_ns
    assert finish.stage_still(r, src) == a and a.stat().st_mtime_ns == m
    assert len(list((w["handoff"] / "stills").iterdir())) == 1


def test_still_without_siblings_repairs_on_first_attempt_without_copy(tmp_path, inject):
    w = build_workspace(tmp_path, still="261010_stars_final.jpg")
    inject()
    res, _ = run(w["ws"])
    (rep,) = res.report["repairs"]
    assert rep["result"] == "online" and len(rep["attempts"]) == 1 and "staged_copy" not in rep
    assert not (w["handoff"] / "stills").exists() and "staged_copies" not in res.report
    assert "via isolated" not in checks(res)["media_path"]["detail"]


def test_offline_video_never_gets_the_fallback(tmp_path, inject):
    w = build_workspace(tmp_path)
    (w["media"] / "landscape_6s.mp4").write_bytes(b"z" * 64)  # numbered sibling of landscape_5s.mp4
    inject(offline_all=True, misread_stills=False)
    res, _ = run(w["ws"])
    vid = next(r for r in res.report["repairs"] if r["expected_path"].endswith("landscape_5s.mp4"))
    assert vid["result"] == "still_offline" and len(vid["attempts"]) == 1 and "staged_copy" not in vid
    assert vid["replace_returned"] is False
    assert res.status == "fail" and not (w["handoff"] / "stills" / finish.stills_key(vid["expected_path"])).exists()


def test_failed_fallback_is_reported_still_offline(tmp_path, inject, monkeypatch):
    w = _siblings_ws(tmp_path)
    fake = inject()
    monkeypatch.setattr(fake_resolve, "has_numbered_sibling", lambda p: True)  # even the copy is refused
    res, _ = run(w["ws"])
    (rep,) = res.report["repairs"]
    assert rep["result"] == "still_offline" and [a["returned"] for a in rep["attempts"]] == [False, False]
    assert res.status == "fail" and "detail" not in rep


def test_allow_staged_guard(tmp_path):
    src = tmp_path / "m" / "a_-1.jpg"
    other = tmp_path / "m" / "b_-1.jpg"
    src.parent.mkdir()
    for p in (src, other):
        p.write_bytes(b"x")
    root = tmp_path / "ws" / "stills"
    key = finish.stills_key(str(src))

    def mk(d, name="a_-1.jpg"):
        (root / d).mkdir(parents=True, exist_ok=True)
        (root / d / name).write_bytes(b"x")
        return root / d / name

    s = ApiSession(allowed_media=media_keys([str(src)]), stills_root=root)
    good = mk(key)
    mpi = fake_resolve.FakeMPI(str(tmp_path / "x_[1-9].jpg"))
    g = s.wrap(mpi, "mpi")
    with pytest.raises(AllowlistViolation, match="not listed in media_manifest.csv"):
        g.ReplaceClip(str(good))  # not registered yet
    s.allow_staged(str(src), str(good))
    assert g.ReplaceClip(str(good)) is True
    for bad_source, bad_staged, why in [
        (str(tmp_path / "evil.jpg"), str(good), "not listed in media_manifest.csv"),  # non-manifest source
        (str(src), str(other), "isolated copy"),  # outside the stills root
        (str(src), str(mk("0123456789ab")), "isolated copy"),  # wrong key folder
        (str(src), str(mk(key, "other.jpg")), "isolated copy"),  # basename differs
        (str(src), str(root / key / "missing.jpg"), "isolated copy"),
        (str(src), str(root / key / ".." / key / "a_-1.jpg" / ".." / ".." / "a_-1.jpg"), "isolated copy"),
    ]:
        s2 = ApiSession(allowed_media=media_keys([str(src)]), stills_root=root)
        with pytest.raises(AllowlistViolation, match=why):
            s2.allow_staged(bad_source, bad_staged)
        assert s2.staged_media == set()
        with pytest.raises(AllowlistViolation):
            s2.wrap(fake_resolve.FakeMPI(None), "mpi").ReplaceClip(bad_staged)
    with pytest.raises(AllowlistViolation, match="no stills root"):
        ApiSession(allowed_media=media_keys([str(src)])).allow_staged(str(src), str(good))


def test_compare_path_aliases_only_for_the_expected_source():
    from trip_vlog.resolve.compare import compare_timeline
    exp = {"fps": 30, "tracks": [{"kind": "video", "clips": [{"name": "a.jpg", "source_path": "/m/a.jpg", "record_start_frame": 0,
                                                              "record_end_frame": 10}]}]}
    act = {"fps": 30, "tracks": [{"kind": "video", "index": 1, "items": [
        {"name": "a.jpg", "media_path": "/ws/stills/abc/a.jpg", "online": True, "start": 0, "end": 10, "source_in": 0, "index": 0}]}]}
    d = {c["check"]: c for c in compare_timeline(exp, act)}
    assert d["media_path"]["status"] == "fail"
    d = {c["check"]: c for c in compare_timeline(exp, act, path_aliases={"/ws/stills/abc/a.jpg": "/m/a.jpg"})}
    assert d["media_path"]["status"] == "pass" and "1 via isolated copies" in d["media_path"]["detail"]
    d = {c["check"]: c for c in compare_timeline(exp, act, path_aliases={"/ws/stills/abc/a.jpg": "/m/other.jpg"})}
    assert d["media_path"]["status"] == "fail"


def test_dry_run_plan_mentions_the_fallback(tmp_path, inject):
    inject()
    res, lines = run(build_workspace(tmp_path)["ws"], dry_run=True)
    assert any("resolve_handoff/stills/" in l for l in lines)


# ---------------------------------------------------------------- markers
def test_markers_present_are_not_duplicated(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject()
    res, _ = run(w["ws"])
    assert {m["result"] for m in res.report["markers"]} == {"present"}
    tl = fake.pm.projects[res.report["project"]["name"]].timelines[0]
    assert tl.add_marker_calls == [] and sorted(tl.markers) == [0, 90]


def test_missing_markers_are_added_once(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject(drop_markers=True)
    res, _ = run(w["ws"])
    assert [m["result"] for m in res.report["markers"]] == ["added", "added"] and res.status == "pass"
    tl = fake.pm.projects[res.report["project"]["name"]].timelines[0]
    assert [(a[0], a[2]) for a in tl.add_marker_calls] == [(0, "Scene 1"), (90, "Scene 2")]
    assert tl.markers[90]["name"] == "Scene 2" and checks(res)["markers"]["status"] == "pass"


def test_only_the_missing_marker_is_added(tmp_path, inject):
    w = build_workspace(tmp_path, xml_markers=[{"name": "Scene 1", "frame": 0}])
    fake = inject()
    res, _ = run(w["ws"])
    assert [m["result"] for m in res.report["markers"]] == ["present", "added"]
    assert [a[0] for a in fake.pm.projects[res.report["project"]["name"]].timelines[0].add_marker_calls] == [90]


def test_marker_at_same_frame_with_other_name_is_a_conflict_not_overwritten(tmp_path, inject):
    w = build_workspace(tmp_path, xml_markers=[{"name": "Something else", "frame": 0}, {"name": "Scene 2", "frame": 90}])
    fake = inject()
    res, _ = run(w["ws"])
    assert res.report["markers"][0]["result"] == "conflict"
    assert fake.pm.projects[res.report["project"]["name"]].timelines[0].add_marker_calls == []


# ---------------------------------------------------------------- titles (experimental)
def test_title_created_and_verified(tmp_path, inject):
    w = build_workspace(tmp_path, titles=[title("Iceland", 0, 60)])
    fake = inject()
    res, _ = run(w["ws"])
    (t,) = res.report["titles"]
    assert t["status"] == "created" and t["experimental"] and t["applied"] == ["text", "track and range"], t
    tl = fake.pm.projects[res.report["project"]["name"]].timelines[0]
    v2 = [i for i in tl.tracks["video"][1]]
    assert len(v2) == 1 and (v2[0].s, v2[0].e) == (0, 60) and v2[0].name == t["resolve_name"]
    assert checks(res)["titles"]["status"] == "pass" and res.status == "pass"
    helper = [x for x in fake.pm.projects[res.report["project"]["name"]].timelines if x.name == t["helper_timeline"]]
    assert helper and "Iceland" in [i.text for i in helper[0].tracks["video"][0]]
    call = next(c for c in res.api.log if c["call"] == "mp.AppendToTimeline")
    info = call["args"][0][0]
    assert info["trackIndex"] == 2 and info["recordFrame"] == fake_resolve.TL_START and info["mediaType"] == 1
    assert (info["startFrame"], info["endFrame"]) == (0, 59)  # 60-frame title trimmed from the 150-frame template
    assert not res.report["titles_need_manual_recreation"]


@pytest.mark.parametrize("mode,reason", [("no_insert", "neither InsertFusionTitleIntoTimeline"), ("no_comp", "text could not be set"),
                                         ("no_tool", "text could not be set"), ("reject_text", "text could not be set")])
def test_title_failure_is_rolled_back_and_listed(tmp_path, inject, mode, reason):
    w = build_workspace(tmp_path, titles=[title("Iceland", 30, 60)])
    fake = inject(titles=mode)
    res, lines = run(w["ws"])
    (t,) = res.report["titles"]
    assert t["status"] == "failed" and reason in t["reason"]
    proj = fake.pm.projects[res.report["project"]["name"]]
    assert [x.name for x in proj.timelines] == [res.report["timeline"]["name"]]  # helper timeline removed again
    tl = proj.timelines[0]
    assert all(not tr for tr in tl.tracks["video"][1:])  # nothing left on V2
    assert res.report["titles_need_manual_recreation"] == [{"text": "Iceland", "track": "V2", "start_frame": 30, "duration_frames": 60, "reason": t["reason"]}]
    assert checks(res)["titles"]["status"] == "fail" and checks(res)["titles"]["required"] is False
    assert res.status == "partial" and res.exit_code == 0
    assert any("title needs manual recreation: 'Iceland'" in l for l in lines)


def test_title_with_wrong_placement_is_deleted_from_the_timeline(tmp_path, inject):
    w = build_workspace(tmp_path, titles=[title("Iceland", 30, 60)])
    fake = inject(append_record_relative=True)  # Resolve treats recordFrame differently than assumed -> read-back mismatch
    res, _ = run(w["ws"])
    (t,) = res.report["titles"]
    assert t["status"] == "failed" and "after AppendToTimeline" in t["reason"]
    assert t["rollback"]["delete_clips"] is True and t["rollback"]["delete_helper_timeline"] is True
    proj = fake.pm.projects[res.report["project"]["name"]]
    assert [x.name for x in proj.timelines] == [res.report["timeline"]["name"]]
    assert all(not tr for tr in proj.timelines[0].tracks["video"][1:])
    deletes = [c for c in res.api.log if c["call"] in ("timeline.DeleteClips", "mp.DeleteTimelines")]
    assert len(deletes) == 2 and not any(c.get("blocked") for c in deletes)


def test_keep_on_failure_leaves_unverified_title_in_place(tmp_path, inject):
    w = build_workspace(tmp_path, titles=[title("Iceland", 30, 60)])
    fake = inject(append_record_relative=True)
    res, _ = run(w["ws"], keep_on_failure=True)
    (t,) = res.report["titles"]
    assert t["status"] == "failed" and t["rollback"]["kept"] is True
    assert "timeline.DeleteClips" not in {c["call"] for c in res.api.log}
    proj = fake.pm.projects[res.report["project"]["name"]]
    assert len(proj.timelines) == 2


def test_title_as_long_as_the_template_appends_the_whole_clip(tmp_path, inject):
    w = build_workspace(tmp_path, titles=[title("Iceland", 0, 150)])
    inject()
    res, _ = run(w["ws"])
    assert res.report["titles"][0]["status"] == "created"
    info = next(c for c in res.api.log if c["call"] == "mp.AppendToTimeline")["args"][0][0]
    assert "startFrame" not in info and "endFrame" not in info


def test_title_too_long_for_template_fails_cleanly(tmp_path, inject):
    w = build_workspace(tmp_path, titles=[title("Iceland", 0, 120)])
    inject(title_default_frames=60)
    res, _ = run(w["ws"])
    assert res.report["titles"][0]["status"] == "failed" and "only 60 frames long" in res.report["titles"][0]["reason"]


def test_delete_clips_only_accepts_items_created_by_this_run():
    s = ApiSession()
    fake = FakeResolve()
    proj = fake.pm.CreateProject("p")
    proj.mp.ImportTimelineFromFile  # noqa: B018
    tl = fake_resolve.FakeTimeline("t", proj)
    foreign = fake_resolve.FakeItem("x", 0, 10, 0, None)
    tl.tracks["video"][0].append(foreign)
    g = s.wrap(tl, "timeline")
    with pytest.raises(AllowlistViolation, match="created by this run"):
        g.DeleteClips([foreign], False)
    assert foreign in tl.tracks["video"][0]
    mine = g.InsertFusionTitleIntoTimeline("Text+")
    assert g.DeleteClips([mine], False) is True


# ---------------------------------------------------------------- availability, dry run, preconditions
def test_scripting_unavailable_exit_3_writes_report_and_changes_nothing(tmp_path, monkeypatch):
    w = build_workspace(tmp_path)
    monkeypatch.setattr(finish, "resolve_factory", lambda: (None, {"diagnosis": ["scriptapp('Resolve') returned None"]}))
    res, lines = run(w["ws"])
    assert res.exit_code == 3 and res.status == "manual_required"
    text = "\n".join(lines)
    assert "returned None" in text and "External scripting using = Local" in text and "Python" in text
    rep = rj(w["handoff"] / "resolve_finish_report.json")
    assert rep["status"] == "manual_required" and rep["python"]["version"] and rep["api_calls"] == []
    assert (w["handoff"] / "resolve_finish_report.md").is_file()
    st = Manifest.load(w["ws"] / "manifest.json").stage("resolve_finish")
    assert st["status"] == "failed" and "unavailable" in st["error"]


def test_free_edition_is_refused_with_exit_3(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject(product="DaVinci Resolve")
    res, lines = run(w["ws"])
    assert res.exit_code == 3 and "needs DaVinci Resolve Studio" in "\n".join(lines) and fake.pm.created == []


def test_real_connect_failure_reports_python_and_compat_hint(tmp_path, monkeypatch):
    from trip_vlog.resolve import discovery

    monkeypatch.setattr(finish, "resolve_factory", None)
    monkeypatch.setenv("RESOLVE_SCRIPT_API", str(tmp_path / "nope"))
    monkeypatch.setenv("RESOLVE_SCRIPT_LIB", str(tmp_path / "nope.dll"))
    res, lines = run(build_workspace(tmp_path)["ws"])
    assert res.exit_code == 3
    text = "\n".join(lines)
    assert "Scripting modules folder not found" in text and "fusionscript library not found" in text
    assert f"Python {discovery.python_info()['version']}" in text and "3.10-3.12" in text and "3.14" in text


def test_dry_run_makes_no_mutating_calls(tmp_path, inject):
    w = build_workspace(tmp_path, titles=[title()])
    fake = inject()
    res, lines = run(w["ws"], dry_run=True)
    assert res.exit_code == 0 and res.status == "dry_run"
    assert mutations(res) == [] and all(c["call"] in {f"{r}.{m}" for r, ms in ALLOWED.items() for m in ms if m not in MUTATING}
                                        for c in res.api.log)
    assert fake.pm.created == [] and fake.imports == [] and fake.pm.saved == []
    text = "\n".join(lines)
    assert "DRY RUN" in text and "Iceland Trip (trip-vlog v001)" in text and "ReplaceClip" in text and "1 title" in text
    assert not (w["handoff"] / "resolve_finish_report.json").exists()
    assert "resolve_finish" not in Manifest.load(w["ws"] / "manifest.json").stages


def test_dry_run_without_resolve_still_prints_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(finish, "resolve_factory", lambda: (None, {"diagnosis": ["no"]}))
    res, lines = run(build_workspace(tmp_path)["ws"], dry_run=True)
    assert res.exit_code == 0 and any("create a NEW Resolve project" in l for l in lines)


def test_import_failure_keeps_new_project_and_reports(tmp_path, inject):
    w = build_workspace(tmp_path)
    fake = inject(import_returns_none=True)
    res, _ = run(w["ws"])
    assert res.exit_code == 1 and res.status == "fail" and "could not import" in res.report["error"]
    assert fake.pm.created and fake.pm.created[0] in fake.pm.projects  # nothing is ever deleted
    assert Manifest.load(w["ws"] / "manifest.json").stage("resolve_finish")["status"] == "failed"


@pytest.mark.parametrize("breakage,msg", [
    ("no_manifest", "not a trip-vlog workspace"), ("handoff_pending", "handoff stage has not completed"),
    ("no_xml", "missing: timeline.xml"), ("stale_ref", "exported from FilmCraft project v0"),
    ("tampered_spec", "does not match the hash"), ("plan_mismatch", "disagrees"), ("spec_outside_manifest", "not in media_manifest.csv"),
])
def test_preconditions_fail_clearly_before_connecting(tmp_path, monkeypatch, breakage, msg):
    called = []
    monkeypatch.setattr(finish, "resolve_factory", lambda: called.append(1) or (None, {}))
    w = build_workspace(tmp_path, write_xml=breakage != "no_xml", handoff_status="pending" if breakage == "handoff_pending" else "succeeded")
    ws, hd = w["ws"], w["handoff"]
    if breakage == "no_manifest":
        (ws / "manifest.json").unlink()
    elif breakage == "stale_ref":
        (hd / "project_reference.json").write_text(json.dumps({"project_version": 0}))
    elif breakage == "tampered_spec":
        p = ws / w["run"]["spec_file"]
        d = rj(p)
        d["placements"][0]["duration_frames"] += 1
        p.write_text(json.dumps(d))
    elif breakage == "plan_mismatch":
        d = rj(hd / "edit_plan.json")
        d["sequence"]["fps"] = "25"
        (hd / "edit_plan.json").write_text(json.dumps(d))
    elif breakage == "spec_outside_manifest":
        lines = (hd / "media_manifest.csv").read_text().splitlines()
        (hd / "media_manifest.csv").write_text("\n".join(l for l in lines if "landscape" not in l) + "\n")
    res, out = run(ws)
    assert res.exit_code == 2 and called == [] and any(msg in l for l in out), out


# ---------------------------------------------------------------- report and CLI
def test_report_contents_json_and_markdown(tmp_path, inject):
    w = build_workspace(tmp_path, still="IMG_1042.JPG", titles=[title("Iceland")], dissolve=True)
    inject(drop_markers=True)
    res, _ = run(w["ws"])
    rep = rj(w["handoff"] / "resolve_finish_report.json")
    assert rep["status"] == res.status and rep["resolve"]["product"] == "DaVinci Resolve Studio" and rep["resolve"]["version"] == "21.1.1.10"
    assert rep["project"]["name"] == "Iceland Trip (trip-vlog v001)" and rep["python"]["version"]
    assert rep["inputs"]["timeline_xml_sha256"] and rep["inputs"]["filmcraft_project_version"] == 1
    calls = {c["call"] for c in rep["api_calls"]}
    assert {"pm.CreateProject", "project.SetSetting", "mp.ImportTimelineFromFile", "mpi.ReplaceClip", "timeline.AddMarker",
            "mp.AppendToTimeline", "pm.SaveProject"} <= calls
    imp = next(c for c in rep["api_calls"] if c["call"] == "mp.ImportTimelineFromFile")
    assert imp["args"][1]["importSourceClips"] is True and imp["mutating"] is True and imp["result"] == "<timeline>"
    assert rep["read_call_counts"]["timeline.GetItemListInTrack"] > 0
    names = {c["check"] for c in rep["final_checks"]}
    assert {"fps", "duration", "clip_order", "record_inout", "media_online", "markers", "transitions", "titles"} <= names
    assert [c["status"] for c in rep["final_checks"] if c["check"] == "transitions"] == ["pass"]
    md = (w["handoff"] / "resolve_finish_report.md").read_text(encoding="utf-8")
    assert "# Resolve finish report" in md and "| fps |" in md and "UNPROVEN on real Resolve" in md and "`mpi.ReplaceClip`" in md
    assert "## Titles (EXPERIMENTAL)" in md and "IMG_[1042-1191].JPG" in md
    st = Manifest.load(w["ws"] / "manifest.json").stage("resolve_finish")
    assert st["status"] == "succeeded" and st["summary"]["repairs"] == 1 and st["summary"]["project"] == rep["project"]["name"]
    assert "resolve_finish" not in __import__("trip_vlog.draft_stages", fromlist=["DRAFT_STAGES"]).DRAFT_STAGES


def test_cli_dry_run_and_status_shows_the_stage(tmp_path, inject, capsys):
    w = build_workspace(tmp_path)
    inject()
    assert cli.main(["resolve-finish", "--output", str(w["ws"]), "--dry-run"]) == 0
    assert "DRY RUN" in capsys.readouterr().out
    assert cli.main(["resolve-finish", "--output", str(w["ws"]), "--project-name", "Cli Project"]) == 0
    capsys.readouterr()
    assert cli.main(["status", "--output", str(w["ws"])]) == 0
    out = capsys.readouterr().out
    assert "resolve   : succeeded - Resolve project 'Cli Project'" in out
    assert "resolve_finish" not in out.split("unfinished:")[-1]  # an optional stage never makes the draft look unfinished
    assert cli.main(["resolve-finish", "--output", str(tmp_path / "nowhere")]) == 2


def test_verify_script_runs_from_a_checkout_without_the_package_installed():
    p = subprocess.run([sys.executable, "-S", str(ROOT / "tools" / "resolve_verify.py"), "--help"], capture_output=True, text=True)
    assert p.returncode == 0 and "--handoff" in p.stdout, p.stderr
