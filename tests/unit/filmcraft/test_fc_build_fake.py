"""Adapter + execution journal against the stateful fake server: translation, idempotency, versioning, failures."""
from __future__ import annotations

import json
import os
import time
from dataclasses import replace
from fractions import Fraction

import pytest

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401
from trip_vlog.filmcraft.errors import (
    CommandError, FilmCraftError, ParameterError, PathPolicyError, SequenceNotEmpty, SpecValidationError, VerificationFailed)
from trip_vlog.filmcraft.execution import load_journal
from trip_vlog.filmcraft.mappings import MediaMap
from trip_vlog.filmcraft.timeline_spec import Placement, TimelineSpec, TransitionIn

ticks = lambda fps, frames: round(frames * 254016000000 / fps)


def journal(ws):
    return json.loads((ws / "filmcraft" / "execution_journal.json").read_text(encoding="utf-8"))


def starts(count_file) -> int:
    return len(count_file.read_text().splitlines()) if count_file.exists() else 0


def test_first_build_creates_v001_and_verifies_after_reopen(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    res = adapter.build_project(spec)
    assert not res.noop and res.version == 1
    assert res.project_path == ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj" and res.project_path.is_file()
    assert res.verification.ok and res.sequence_id
    run = journal(ws)["runs"][0]
    assert run["status"] == "complete" and run["verified_after_reopen"] and run["last_checkpoint"] == "verified"
    assert run["spec_hash"] == spec.spec_hash() and run["filmcraft_version"] == "0.13.0-fake"
    assert run["completed_ops"][:3] == ["create_project", "import_media:4", "create_sequence"]
    assert run["project_path"] == "filmcraft/Trip Vlog Draft_v001.fcproj"
    assert set(run["media_mapping"].values()) == {1, 2, 3, 4} or len(run["media_mapping"]) == 4
    assert set(run["clip_mapping"]["clips"]) == {"c1", "c2", "c3", "c4"}
    assert run["clip_mapping"]["clips"]["c1"]["audio_clip"] is not None and run["clip_mapping"]["clips"]["c3"]["audio_clip"] is None
    assert len(run["clip_mapping"]["titles"]) == 1 and len(run["clip_mapping"]["markers"]) == 1 and run["clip_mapping"]["transitions"]["c2"]
    assert (ws / "filmcraft" / "Trip Vlog Draft_v001.timeline_spec.json").is_file()
    assert (ws / "filmcraft" / "capability_snapshot.json").is_file()


def test_rerun_with_same_spec_is_a_noop_and_never_duplicates(fc, tmp_path, monkeypatch):
    adapter, media, ws = fc
    counter = tmp_path / "starts.txt"
    monkeypatch.setenv("FAKE_FC_COUNT_FILE", str(counter))
    spec = fcsupport.sample_spec(media)
    first = adapter.build_project(spec)
    adapter.close()
    n_starts, mtime = starts(counter), first.project_path.stat().st_mtime_ns
    journal_bytes = (ws / "filmcraft" / "execution_journal.json").read_bytes()
    second = adapter.build_project(spec)
    assert second.noop and second.version == 1 and second.project_path == first.project_path
    assert starts(counter) == n_starts, "no FilmCraft process may be started for a no-op"
    assert first.project_path.stat().st_mtime_ns == mtime
    assert (ws / "filmcraft" / "execution_journal.json").read_bytes() == journal_bytes
    assert sorted(p.name for p in (ws / "filmcraft").glob("*.fcproj")) == ["Trip Vlog Draft_v001.fcproj"]
    # and the stored project holds exactly the spec's clips
    snap = adapter.open_project(second.project_path) and adapter.inspect_sequence()
    assert snap.clip_count == 6 and len(snap.markers) == 1  # V1:2 V2:2(title) A1:1 A2:1 (portrait has no audio)
    assert second.media_map.item_for(spec.placements[0].source_path) >= 1 and "c2" in second.clip_map


def test_changed_spec_creates_a_new_version_and_leaves_the_old_one_alone(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    v1 = adapter.build_project(spec)
    sha1 = (v1.project_path.read_bytes())
    v2 = adapter.build_project(fcsupport.sample_spec(media, end_marker=90))
    assert not v2.noop and v2.version == 2 and v2.project_path.name == "Trip Vlog Draft_v002.fcproj"
    assert v1.project_path.read_bytes() == sha1
    assert [r["status"] for r in journal(ws)["runs"]] == ["complete", "complete"]
    # re-running v1's spec after v2 is NOT a no-op (latest is v2) -> v3, never an append to an old project
    v3 = adapter.build_project(spec)
    assert not v3.noop and v3.version == 3
    assert adapter.build_project(spec).noop


def test_changed_media_forces_a_new_version(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    assert adapter.build_project(spec).version == 1
    (media / "landscape_5s.mp4").write_bytes(b"y" * 99)
    res = adapter.build_project(spec)
    assert not res.noop and res.version == 2


def test_modified_project_file_forces_a_new_version(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    first = adapter.build_project(spec)
    adapter.close()
    first.project_path.write_bytes(first.project_path.read_bytes() + b" ")
    assert adapter.build_project(spec).version == 2


def test_force_new_version(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    res = adapter.build_project(spec, force_new_version=True)
    assert not res.noop and res.version == 2


def test_failure_stops_at_first_failed_command_and_is_journaled(fc, monkeypatch):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    monkeypatch.setenv("FAKE_FC_FAIL_ON", "timeline.place")
    monkeypatch.setenv("FAKE_FC_FAIL_AFTER", "2")
    with pytest.raises(CommandError, match="timeline.place"):
        adapter.build_project(spec)
    places = fcsupport.log_requests(ws, "timeline.place")
    assert len(places) == 3, "2 succeeded, the 3rd failed, nothing was attempted afterwards"
    assert not fcsupport.log_requests(ws, "markers.add") and not fcsupport.log_requests(ws, "graphics.newText")
    run = journal(ws)["runs"][0]
    assert run["status"] == "failed" and run["finished_at"]
    err = run["errors"][0]
    assert err["op"] == "apply_timeline" and err["command"] == "timeline.place" and "injected failure" in err["message"]
    assert run["last_checkpoint"] == "imported" and "import_media:4" in run["completed_ops"]
    assert str(media) not in json.dumps(run["errors"])
    # retry without the fault -> new version, complete
    monkeypatch.delenv("FAKE_FC_FAIL_ON")
    res = adapter.build_project(spec)
    assert not res.noop and res.version == 2
    assert [r["status"] for r in journal(ws)["runs"]] == ["failed", "complete"]


def test_interrupted_run_is_never_resumed_in_place(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    j = journal(ws)
    j["runs"][0]["status"] = "running"  # simulate a crash before completion was recorded
    (ws / "filmcraft" / "execution_journal.json").write_text(json.dumps(j), encoding="utf-8")
    res = adapter.build_project(spec)
    assert not res.noop and res.version == 2


def test_persistence_failure_is_detected_by_reopen_in_a_new_process(fc, monkeypatch):
    adapter, media, ws = fc
    monkeypatch.setenv("FAKE_FC_LOSE_CLIPS_ON_SAVE", "1")
    with pytest.raises(VerificationFailed, match="reopened in a new process"):
        adapter.build_project(fcsupport.sample_spec(media))
    run = journal(ws)["runs"][0]
    assert run["status"] == "failed" and run["errors"][0]["op"] == "reopen_verify"
    kinds = {m["kind"] for m in run["errors"][0]["mismatches"]}
    assert "missing_clip" in kinds and run["last_checkpoint"] == "assembled"


def test_invalid_spec_is_rejected_before_any_side_effect(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    bad = replace(spec, placements=spec.placements + (replace(spec.placements[0], clip_instance_id="dup"),))
    with pytest.raises(SpecValidationError, match="overlap"):
        adapter.build_project(bad)
    assert not (ws / "filmcraft" / "execution_journal.json").exists() and not list(ws.rglob("*.fcproj"))


def test_asset_outside_media_root_is_refused_before_touching_filmcraft(fc, tmp_path):
    adapter, media, ws = fc
    outside = tmp_path / "private" / "secret.mp4"
    outside.parent.mkdir()
    outside.write_bytes(b"x")
    spec = fcsupport.sample_spec(media)
    evil = replace(spec, placements=(replace(spec.placements[0], source_path=str(outside)),) + spec.placements[1:])
    with pytest.raises(PathPolicyError, match="outside the allowed read roots"):
        adapter.build_project(evil)
    assert not (ws / "filmcraft" / "execution_journal.json").exists() and not list(ws.rglob("*.fcproj"))
    rel = replace(spec, placements=(replace(spec.placements[0], source_path="media/landscape_5s.mp4"),) + spec.placements[1:])
    with pytest.raises(PathPolicyError, match="absolute"):
        adapter.build_project(rel)
    missing = replace(spec, placements=(replace(spec.placements[0], source_path=str(media / "gone.mp4")),) + spec.placements[1:])
    with pytest.raises(FilmCraftError, match="not found"):
        adapter.build_project(missing)


def test_source_range_past_end_of_media_is_caught_before_the_first_placement(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)  # landscape is 5 s in the fake
    c1 = spec.placements[0]
    bad = replace(spec, placements=(replace(c1, source_in_seconds=4.0, source_out_seconds=4.0 + 105 / 30),) + spec.placements[1:])
    with pytest.raises(SpecValidationError, match="past the end of the media"):
        adapter.build_project(bad)
    assert not fcsupport.log_requests(ws, "timeline.place")
    assert journal(ws)["runs"][0]["status"] == "failed"


def test_dissolve_without_handles_errors_or_downgrades_to_cut(tmp_path):
    adapter, media, ws = fcsupport.make_adapter(tmp_path)
    try:
        spec = fcsupport.sample_spec(media)
        c1 = spec.placements[0]  # make c1 use the final frames of the 5 s source: no handle after the cut
        tight = replace(spec, placements=(replace(c1, source_in_seconds=5.0 - 105 / 30, source_out_seconds=5.0),) + spec.placements[1:])
        with pytest.raises(SpecValidationError, match="lacks source handle"):
            adapter.build_project(tight)
        assert not fcsupport.log_requests(ws, "timeline.place")
    finally:
        adapter.close()
    ws2 = tmp_path / "ws_cut"
    adapter2 = fcsupport.FilmCraftAdapter(ws2, media, argv_prefix=fcsupport.FAKE_ARGV, request_timeout=30, handle_policy="cut")
    try:
        res = adapter2.build_project(tight)
        assert res.verification.ok and not fcsupport.log_requests(ws2, "sequence.applyVideoTransition")
        assert journal(ws2)["runs"][0]["downgraded_dissolves"] == ["c2"] and "downgraded to a cut" in res.warnings[0]
    finally:
        adapter2.close()


def test_apply_timeline_refuses_a_non_empty_sequence(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    res = adapter.build_project(spec)
    with pytest.raises(SequenceNotEmpty, match="duplicate"):
        adapter.apply_timeline(spec, res.media_map)
    assert adapter.inspect_sequence().clip_count == 6  # nothing was added


def test_existing_project_is_never_overwritten(fc):
    adapter, media, ws = fc
    res = adapter.build_project(fcsupport.sample_spec(media))
    with pytest.raises(ParameterError, match="already exists"):
        adapter.create_project(res.project_path)
    with pytest.raises(PathPolicyError):
        adapter.create_project(media / "oops.fcproj")


# ---- parameter translation, read from the sanitized command log --------------------------------------------------

def params_of(ws, cmd):
    return [r["params"]["arguments"]["params"] for r in fcsupport.log_requests(ws, cmd)]


@pytest.mark.parametrize("fps", [Fraction(30), Fraction(30000, 1001), Fraction(25)])
def test_placement_parameters_use_exact_ticks(tmp_path, fps):
    adapter, media, ws = fcsupport.make_adapter(tmp_path)
    try:
        spec = fcsupport.sample_spec(media, fps)
        res = adapter.build_project(spec)
        mm = res.media_map
        c1, c2, c3, c4 = spec.placements
        T = lambda frames: ticks(fps, frames)
        by_item = {p["item"]: p for p in params_of(ws, "timeline.place")}
        p1 = by_item[mm.item_for(c1.source_path)]
        assert (p1["track"], p1["frame"], p1["duration"], p1["audio"], p1["audioTrack"]) == ("V1", 0, T(c1.duration_frames), True, "A1")
        assert p1["sourceIn"] == round(0.5 * 254016000000)
        p2 = by_item[mm.item_for(c2.source_path)]
        assert p2 == {"item": p2["item"], "track": "V1", "frame": c2.timeline_start_frame, "duration": T(c2.duration_frames)}
        p3 = by_item[mm.item_for(c3.source_path)]
        assert p3["audio"] is False and "audioTrack" not in p3 and p3["track"] == "V2" and p3["frame"] == c3.timeline_start_frame
        assert p3["sourceIn"] == 254016000000
        p4 = by_item[mm.item_for(c4.source_path)]
        assert (p4["track"], p4["duration"]) == ("A2", T(c4.duration_frames))
        assert not {"audio", "video", "sourceIn", "audioTrack"} & set(p4)
        (gain,) = params_of(ws, "clip.audioGain")
        assert gain["db"] == -6.0 and gain["mode"] == "set" and len(gain["clips"]) == 1
        (tr,) = params_of(ws, "sequence.applyVideoTransition")
        assert tr == {"clip": tr["clip"], "edge": "in", "effect": "cross_dissolve", "frames": 15}
        (mk,) = params_of(ws, "markers.add")
        assert mk["time"] == T(spec.markers[0].frame) and mk["name"] == "Scene A" and mk["comment"] == "first scene"
        (title,) = params_of(ws, "graphics.newText")
        assert title["time"] == 0 and title["track"] == 1 and title["text"] == "Hello Title"
        assert title["seconds"] == pytest.approx(spec.titles[0].duration_frames / float(fps))
        seq = params_of(ws, "file.newSequence")[0]
        assert (seq["width"], seq["height"], seq["video"], seq["audio"]) == (1920, 1080, 2, 2)
        assert seq["fps"] == (int(fps) if fps.denominator == 1 else pytest.approx(float(fps)))
        assert res.verification.ok
    finally:
        adapter.close()


def test_gain_goes_to_the_linked_audio_clip_of_a_video_not_the_video_clip(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    c1 = replace(spec.placements[0], audio_gain_db=-3.5)
    spec = replace(spec, placements=(c1,) + spec.placements[1:])
    res = adapter.build_project(spec)
    ref = res.clip_map["c1"]
    gains = params_of(ws, "clip.audioGain")
    assert {"clips": [ref.audio_clip], "mode": "set", "db": -3.5} in gains and ref.clip not in [c for g in gains for c in g["clips"]]
    assert next(c for c in adapter.inspect_sequence().track("A1").clips).gain_db == -3.5


def test_silent_source_with_linked_audio_warns_but_a_gain_request_fails(tmp_path, monkeypatch):
    adapter, media, ws = fcsupport.make_adapter(tmp_path)
    try:
        monkeypatch.setenv("FAKE_FC_NO_AUDIO", "landscape_5s.mp4")
        spec = fcsupport.sample_spec(media)
        with pytest.raises(VerificationFailed, match="linked audio"):  # spec said audio, FilmCraft made none
            adapter.build_project(spec)
        assert journal(ws)["runs"][0]["warnings"]
        c1 = replace(spec.placements[0], include_linked_audio=False)
        ok = adapter.build_project(replace(spec, placements=(c1,) + spec.placements[1:]))
        assert ok.verification.ok
    finally:
        adapter.close()


def test_commands_are_only_ever_sent_from_the_allowlist(fc):
    from trip_vlog.filmcraft.safety import ALLOWED_TOOLS, COMMAND_RULES

    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    adapter.export_interchange()
    for r in fcsupport.log_requests(ws):
        assert r["tool"] in ALLOWED_TOOLS
        if r["tool"] == "command_run":
            assert r["command"] in COMMAND_RULES


def test_mixed_case_and_unicode_paths_round_trip(tmp_path):
    media = tmp_path / "Media été 日本"
    adapter, _, ws = fcsupport.make_adapter(tmp_path / "w")
    adapter.close()
    adapter = fcsupport.FilmCraftAdapter(tmp_path / "ws ü", fcsupport.make_fake_media(media), argv_prefix=fcsupport.FAKE_ARGV, request_timeout=30)
    try:
        res = adapter.build_project(fcsupport.sample_spec(media))
        assert res.verification.ok
    finally:
        adapter.close()


# ---- failed-assembly bookkeeping (Phase 7): failed versions are marked, listed, never reused, never deleted --------------------

def test_failed_build_is_listed_kept_on_disk_and_its_number_is_never_reused(fc, monkeypatch):
    from trip_vlog.filmcraft.execution import failed_builds, next_version

    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    monkeypatch.setenv("FAKE_FC_FAIL_ON", "timeline.place")
    monkeypatch.setenv("FAKE_FC_FAIL_AFTER", "1")
    with pytest.raises(CommandError):
        adapter.build_project(spec)
    partial = ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj"
    partial_bytes = partial.read_bytes()
    doc = journal(ws)
    (rec,) = failed_builds(doc, "Trip Vlog Draft")
    assert rec["version"] == 1 and rec["status"] == "failed" and rec["failed_at_op"] == "apply_timeline" and rec["project"].endswith("_v001.fcproj")
    monkeypatch.delenv("FAKE_FC_FAIL_ON")
    ok = adapter.build_project(spec)
    assert ok.version == 2 and not ok.noop
    assert partial.read_bytes() == partial_bytes, "the failed project is neither deleted nor overwritten"
    # even when the journal is lost, the files on disk keep the failed number reserved
    (ws / "filmcraft" / "execution_journal.json").unlink()
    assert next_version(load_journal(ws / "filmcraft" / "execution_journal.json"), ws / "filmcraft", "Trip Vlog Draft") == 3
    # a failure before the project file exists still reserves its number through the journal and the spec copy
    (ws / "filmcraft" / "Trip Vlog Draft_v003.timeline_spec.json").write_text("{}", encoding="utf-8")
    assert next_version({"schema_version": 1, "runs": []}, ws / "filmcraft", "Trip Vlog Draft") == 4


def test_a_build_that_was_cut_off_is_marked_failed_by_the_next_build(fc):
    from trip_vlog.filmcraft.execution import failed_builds

    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    j = journal(ws)
    j["runs"][0].update(status="running", finished_at=None, project_sha256=None)
    (ws / "filmcraft" / "execution_journal.json").write_text(json.dumps(j), encoding="utf-8")
    assert [f["status"] for f in failed_builds(journal(ws), "Trip Vlog Draft")] == ["interrupted"]   # shown as such even before the next build
    res = adapter.build_project(spec)
    assert res.version == 2
    runs = journal(ws)["runs"]
    assert runs[0]["status"] == "failed" and runs[0]["abandoned"] and runs[0]["errors"][-1]["type"] == "Interrupted" and runs[1]["status"] == "complete"
    assert (ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj").is_file()


# ---- concurrency: pid/host in the journal, abandon only dead builds, journal lock ------------------------------------------

def _dead_pid() -> int:
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def _set_running(ws, **fields):
    jp = ws / "filmcraft" / "execution_journal.json"
    j = json.loads(jp.read_text(encoding="utf-8"))
    j["runs"][0].update(status="running", finished_at=None, project_sha256=None, **fields)
    jp.write_text(json.dumps(j), encoding="utf-8")


def test_runs_record_pid_host_and_start_time(fc):
    from trip_vlog.filmcraft.journal_lock import this_host

    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    run = journal(ws)["runs"][0]
    assert run["pid"] == os.getpid() and run["host"] == this_host() and run["started_at"]
    from trip_vlog.filmcraft.journal_lock import journal_lock

    with journal_lock(ws / "filmcraft" / "execution_journal.json", wait=0.2):       # released after every journal update
        pass


def test_a_build_running_in_a_live_process_is_not_abandoned_and_blocks_a_second_build(fc):
    from trip_vlog.filmcraft.journal_lock import this_host
    from trip_vlog.logging_utils import utc_now_iso

    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    _set_running(ws, pid=os.getppid(), host=this_host(), started_at=utc_now_iso())     # the parent process is alive and is not us
    with pytest.raises(FilmCraftError, match="another build is running"):
        adapter.build_project(fcsupport.sample_spec(media, end_marker=90))
    run = journal(ws)["runs"][0]
    assert run["status"] == "running" and not run.get("abandoned") and len(journal(ws)["runs"]) == 1


def test_a_build_of_a_dead_process_or_a_long_gone_other_host_is_abandoned(fc):
    from trip_vlog.filmcraft.journal_lock import this_host
    from trip_vlog.logging_utils import utc_now_iso

    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    _set_running(ws, pid=_dead_pid(), host=this_host(), started_at=utc_now_iso())
    assert adapter.build_project(spec).version == 2
    assert journal(ws)["runs"][0]["abandoned"]
    _set_running(ws, pid=4242, host="some-other-host", started_at="2001-01-01T00:00:00Z")      # older than the timeout: taken for dead
    assert adapter.build_project(spec, force_new_version=True).version == 3
    assert journal(ws)["runs"][0]["abandoned"]


def test_a_recent_build_of_another_host_is_taken_for_live(fc):
    from trip_vlog.logging_utils import utc_now_iso

    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    _set_running(ws, pid=4242, host="some-other-host", started_at=utc_now_iso())
    with pytest.raises(FilmCraftError, match="another build is running"):
        adapter.build_project(fcsupport.sample_spec(media, end_marker=90))
    assert journal(ws)["runs"][0]["status"] == "running"


_HOLDER = """
import sys, time
from pathlib import Path
from trip_vlog.filmcraft.journal_lock import journal_lock
jp, ready, secs = Path(sys.argv[1]), Path(sys.argv[2]), float(sys.argv[3])
with journal_lock(jp, wait=5):
    ready.write_text("held")
    time.sleep(secs)
"""


def _spawn_holder(jp, ready, secs):
    import subprocess
    import sys

    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(jp), str(ready), str(secs)])
    t0 = time.monotonic()
    while not ready.exists():
        assert p.poll() is None and time.monotonic() - t0 < 20, "holder did not start"
        time.sleep(0.02)
    return p


def test_journal_lock_is_exclusive_across_processes_and_released_when_the_holder_dies(tmp_path):
    from trip_vlog.filmcraft.journal_lock import journal_lock, lock_path_for

    jp = tmp_path / "execution_journal.json"
    p = _spawn_holder(jp, tmp_path / "ready1", 30)
    try:
        t0 = time.monotonic()
        with pytest.raises(FilmCraftError, match="is being updated by pid"):
            with journal_lock(jp, wait=0.3):
                raise AssertionError("two processes held the lock at once")
        assert time.monotonic() - t0 < 5, "waits no longer than asked"
    finally:
        p.kill()
        p.wait()
    with journal_lock(jp, wait=2):          # the OS dropped the dead holder's lock: no stale handling needed
        pass
    assert lock_path_for(jp).exists(), "the lock file is persistent and never deleted"


def test_journal_lock_waits_for_a_short_holder_then_proceeds(tmp_path):
    from trip_vlog.filmcraft.journal_lock import journal_lock

    jp = tmp_path / "execution_journal.json"
    p = _spawn_holder(jp, tmp_path / "ready2", 0.5)
    with journal_lock(jp, wait=10):
        pass
    assert p.wait(timeout=10) == 0


def test_journal_lock_reacquires_after_release_error_or_interrupt_and_nested_use_does_not_deadlock(tmp_path):
    from trip_vlog.filmcraft.journal_lock import journal_lock

    jp = tmp_path / "execution_journal.json"
    with pytest.raises(KeyboardInterrupt):
        with journal_lock(jp, wait=0.3):
            raise KeyboardInterrupt
    for _ in range(3):
        with journal_lock(jp, wait=0.3):
            with journal_lock(jp, wait=0.3):        # nested use by the same thread is a no-op, not a deadlock
                pass


def test_abandon_running_marks_foreign_running_builds_and_keeps_files(fc):
    from trip_vlog.filmcraft.execution import abandon_running, running_builds
    from trip_vlog.logging_utils import utc_now_iso

    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    adapter.build_project(spec)
    _set_running(ws, pid=4242, host="renamed-host", started_at=utc_now_iso())
    jp = ws / "filmcraft" / "execution_journal.json"
    with pytest.raises(FilmCraftError) as e:
        adapter.build_project(fcsupport.sample_spec(media, end_marker=90))
    assert str(jp) in str(e.value) and "--abandon-running" in str(e.value)
    assert [r["version"] for r in running_builds(jp)] == [1]
    assert [r["version"] for r in abandon_running(jp)] == [1]
    assert journal(ws)["runs"][0]["abandoned"] and (ws / "filmcraft" / "Trip Vlog Draft_v001.fcproj").is_file()
    assert adapter.build_project(fcsupport.sample_spec(media, end_marker=90)).version == 2


def test_cli_abandon_running_asks_first(fc, monkeypatch, capsys):
    from trip_vlog import cli
    from trip_vlog.logging_utils import utc_now_iso

    adapter, media, ws = fc
    adapter.build_project(fcsupport.sample_spec(media))
    _set_running(ws, pid=4242, host="renamed-host", started_at=utc_now_iso())
    monkeypatch.setattr("builtins.input", lambda *_: "n")
    assert cli._abandon_running(ws) is False and journal(ws)["runs"][0]["status"] == "running"
    monkeypatch.setattr("builtins.input", lambda *_: "y")
    assert cli._abandon_running(ws) is True and journal(ws)["runs"][0]["status"] == "failed"


def test_a_failing_journal_write_while_recording_a_failure_does_not_hide_the_build_error(fc, monkeypatch):
    from trip_vlog.filmcraft import execution

    adapter, media, ws = fc
    monkeypatch.setenv("FAKE_FC_FAIL_ON", "timeline.place")
    real = execution.save_journal
    calls = {"n": 0}

    def flaky(path, doc):
        calls["n"] += 1
        if doc["runs"] and doc["runs"][-1]["status"] == "failed":
            raise OSError("disk full")
        real(path, doc)

    monkeypatch.setattr(execution, "save_journal", flaky)
    with pytest.raises(CommandError, match="timeline.place"):
        adapter.build_project(fcsupport.sample_spec(media))


def test_each_checkpoint_reloads_the_journal_so_another_process_runs_are_kept(fc, monkeypatch):
    adapter, media, ws = fc
    jp = ws / "filmcraft" / "execution_journal.json"
    orig = adapter.import_media

    def import_and_meanwhile_another_project_records_a_run(paths):
        j = json.loads(jp.read_text(encoding="utf-8"))
        j["runs"].append({"version": 7, "project_name": "Other", "status": "complete", "spec_hash": "x"})
        jp.write_text(json.dumps(j), encoding="utf-8")
        return orig(paths)

    monkeypatch.setattr(adapter, "import_media", import_and_meanwhile_another_project_records_a_run)
    res = adapter.build_project(fcsupport.sample_spec(media))
    runs = journal(ws)["runs"]
    assert res.version == 1 and [(r["project_name"], r["version"]) for r in runs] == [("Trip Vlog Draft", 1), ("Other", 7)]
    assert runs[0]["status"] == "complete"


def test_a_failed_later_attempt_does_not_hide_the_newest_complete_build(fc, monkeypatch):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    assert adapter.build_project(spec).version == 1
    monkeypatch.setenv("FAKE_FC_FAIL_ON", "timeline.place")
    with pytest.raises(CommandError):
        adapter.build_project(spec, force_new_version=True)
    monkeypatch.delenv("FAKE_FC_FAIL_ON")
    res = adapter.build_project(spec)
    assert res.noop and res.version == 1, "the verified v001 still matches; the failed v002 is not rebuilt over"
    assert [r["status"] for r in journal(ws)["runs"]] == ["complete", "failed"]


def test_a_build_that_uses_fit_or_ducking_is_rebuilt_after_a_filmcraft_upgrade(fc):
    adapter, media, ws = fc
    spec = fcsupport.sample_spec(media)
    i = next(n for n, p in enumerate(spec.placements) if p.media_kind == "video")
    fitted = replace(spec, placements=tuple(replace(p, fit_source_size=(3840, 2160)) if n == i else p for n, p in enumerate(spec.placements)))
    jp = ws / "filmcraft" / "execution_journal.json"
    for s, rebuilt in ((spec, False), (fitted, True)):
        first = adapter.build_project(s, force_new_version=True)
        j = json.loads(jp.read_text(encoding="utf-8"))
        j["runs"][-1]["filmcraft_version"] = "0.12.0-older"
        jp.write_text(json.dumps(j), encoding="utf-8")
        again = adapter.build_project(s)
        assert again.noop is (not rebuilt) and (again.version != first.version) is rebuilt


def test_a_build_marked_abandoned_meanwhile_keeps_that_record(fc, monkeypatch):
    adapter, media, ws = fc
    jp = ws / "filmcraft" / "execution_journal.json"
    orig = adapter.import_media

    def import_after_someone_abandoned_this_build(paths):
        j = json.loads(jp.read_text(encoding="utf-8"))
        j["runs"][-1].update(status="failed", abandoned=True)
        jp.write_text(json.dumps(j), encoding="utf-8")
        return orig(paths)

    monkeypatch.setattr(adapter, "import_media", import_after_someone_abandoned_this_build)
    adapter.build_project(fcsupport.sample_spec(media))
    run = journal(ws)["runs"][-1]
    assert run["abandoned"] and run["status"] == "failed"


@pytest.mark.skipif(os.name == "nt", reason="POSIX flock path")
def test_a_lock_error_that_is_not_contention_is_reported_not_waited_out(tmp_path, monkeypatch):
    import errno
    import fcntl

    from trip_vlog.filmcraft import journal_lock as jl

    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    t0 = time.monotonic()
    with pytest.raises(FilmCraftError, match="without file locking"):
        with jl.journal_lock(tmp_path / "execution_journal.json"):
            pass
    assert time.monotonic() - t0 < 1.0
