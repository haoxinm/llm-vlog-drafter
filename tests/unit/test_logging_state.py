from __future__ import annotations

import json
import os
import threading

import pytest

from trip_vlog.logging_utils import RunLogger, Status, read_jsonl, sanitize
from trip_vlog.state import Manifest, StageResult, StageRunner, Workspace, atomic_write_text


def test_sanitize_masks_env_values_and_patterns():
    env = {"ANTHROPIC_API_KEY": "sk-ant-abcdef123456", "HOME": "/home/me", "FILMCRAFT_CONTROL_TOKEN": "tok_9f8e7d6c", "SHORT_KEY": "abc"}
    s = sanitize("failed with key sk-ant-abcdef123456 and token tok_9f8e7d6c at /home/me", env=env)
    assert "sk-ant" not in s and "tok_9f8e7d6c" not in s and "/home/me" in s
    assert sanitize("Authorization: Bearer abcdefghijkl", env={}) == "Authorization: Bearer ***"
    assert sanitize("api_key=hunter2hunter2 next", env={}) == "api_key=*** next"
    assert sanitize("password: 'x'", env={}).count("***") == 1
    assert sanitize("use sk-abcdefghijk1234 now", env={}) == "use *** now"
    assert sanitize("nothing secret here", env={}) == "nothing secret here"


def test_sanitize_reads_real_environment(monkeypatch):
    monkeypatch.setenv("MY_SERVICE_SECRET", "s3cr3t-value-123")
    assert "s3cr3t-value-123" not in sanitize("boom s3cr3t-value-123 boom")


def test_sanitize_redactions():
    assert sanitize("cannot read /media/trip/a.mp4", env={}, redactions=[("/media/trip", "<media_root>")]) == "cannot read <media_root>/a.mp4"


def test_logger_writes_structured_jsonl_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_TOKEN", "supersecretvalue")
    lg = RunLogger(tmp_path / "logs" / "run.jsonl", tmp_path / "logs" / "errors.jsonl", "run1", [("/m", "<m>")])
    lg.event("probe", Status.SUCCEEDED, asset_id="asset_1", cache="hit", duration_ms=1.234)
    lg.event("probe", Status.FAILED, asset_id="asset_2", cache="miss", error="bad file /m/x.mp4 supersecretvalue")
    run = read_jsonl(tmp_path / "logs" / "run.jsonl")
    err = read_jsonl(tmp_path / "logs" / "errors.jsonl")
    assert [r["status"] for r in run] == ["succeeded", "failed"] and len(err) == 1
    assert run[0] == {**run[0], "run_id": "run1", "stage": "probe", "asset_id": "asset_1", "cache": "hit", "duration_ms": 1.23}
    assert err[0]["error"] == "bad file <m>/x.mp4 ***"
    assert "supersecretvalue" not in (tmp_path / "logs" / "run.jsonl").read_text(encoding="utf-8")
    assert b"\r\n" not in (tmp_path / "logs" / "run.jsonl").read_bytes()


def test_logger_thread_safe(tmp_path):
    lg = RunLogger(tmp_path / "r.jsonl", tmp_path / "e.jsonl", "r")
    ts = [threading.Thread(target=lambda i=i: [lg.event("s", Status.RUNNING, n=i) for _ in range(20)]) for i in range(5)]
    [t.start() for t in ts]; [t.join() for t in ts]
    assert len(read_jsonl(tmp_path / "r.jsonl")) == 100


def test_atomic_write_utf8_lf_and_no_leftovers(tmp_path):
    p = tmp_path / "sub" / "f.txt"
    atomic_write_text(p, "café\nline2\n")
    assert p.read_bytes() == "café\nline2\n".encode("utf-8")
    atomic_write_text(p, "new")
    assert p.read_text(encoding="utf-8") == "new"
    assert [x.name for x in p.parent.iterdir()] == ["f.txt"]


def test_manifest_roundtrip_and_corrupt_recovery(tmp_path):
    p = tmp_path / "manifest.json"
    m = Manifest.load(p)
    m.set(config_hash="abc")
    m.stages["scan"] = {"status": "succeeded"}
    m.save()
    assert b"\r" not in p.read_bytes()
    m2 = Manifest.load(p)
    assert m2["config_hash"] == "abc" and m2.stage("scan")["status"] == "succeeded"
    p.write_text("{not json", encoding="utf-8")
    m3 = Manifest.load(p)
    assert m3.stages == {} and (tmp_path / "manifest.json.corrupt").exists()


def test_workspace_layout(tmp_path):
    ws = Workspace(tmp_path / "w")
    ws.ensure_dirs()
    assert ws.catalog_path == tmp_path / "w" / "media_index" / "catalog.sqlite"
    assert ws.run_log.parent.is_dir() and ws.thumbnails_dir.is_dir()


class Ctx:
    def __init__(self): self.calls = []; self.value = 1


def make_runner(tmp_path, ctx=None):
    ctx = ctx or Ctx()
    m = Manifest.load(tmp_path / "manifest.json")
    lg = RunLogger(tmp_path / "run.jsonl", tmp_path / "err.jsonl", "r1")
    return StageRunner(m, lg, ctx), m, ctx


def test_runner_skips_succeeded_unchanged_stage(tmp_path):
    runner, m, ctx = make_runner(tmp_path)
    runner.register("a", lambda c: (c.calls.append("a"), StageResult(Status.SUCCEEDED, {"n": 1}))[1], inputs=lambda c: c.value)
    runner.register("b", lambda c: (c.calls.append("b"), StageResult())[1], deps=("a",), inputs=lambda c: c.value)
    assert runner.run() == {"a": "succeeded", "b": "succeeded"} and ctx.calls == ["a", "b"]
    runner2 = StageRunner(Manifest.load(tmp_path / "manifest.json"), runner.logger, ctx)
    runner2.register("a", lambda c: (c.calls.append("a"), StageResult())[1], inputs=lambda c: c.value)
    assert runner2.run() == {"a": "skipped"} and ctx.calls == ["a", "b"]
    assert runner2.manifest.stage("a")["decision"] == "cache_hit" and runner2.manifest.stage("a")["status"] == "succeeded"
    ctx.value = 2  # inputs changed -> reruns
    assert runner2.run() == {"a": "succeeded"} and ctx.calls == ["a", "b", "a"]
    assert runner2.run(force=True) == {"a": "succeeded"}


def test_runner_reruns_partial_failed_and_always_run(tmp_path):
    runner, m, ctx = make_runner(tmp_path)
    runner.register("p", lambda c: (c.calls.append("p"), StageResult(Status.PARTIAL))[1], inputs=lambda c: 1)
    runner.register("s", lambda c: (c.calls.append("s"), StageResult())[1], inputs=lambda c: 1, always_run=True)
    runner.run(); runner.run()
    assert ctx.calls == ["p", "s", "p", "s"]


def test_runner_exception_becomes_failed_and_blocks_dependents(tmp_path):
    runner, m, ctx = make_runner(tmp_path)

    def boom(c): raise RuntimeError("kaboom")

    runner.register("a", boom)
    runner.register("b", lambda c: (c.calls.append("b"), StageResult())[1], deps=("a",))
    runner.register("c", lambda c: (c.calls.append("c"), StageResult())[1], deps=("b",))
    res = runner.run()
    assert res == {"a": "failed", "b": "skipped", "c": "skipped"} and ctx.calls == []
    assert "kaboom" in m.stage("a")["error"] and m.stage("b")["decision"] == "dependency_failed"
    assert any("kaboom" in e["error"] for e in read_jsonl(tmp_path / "err.jsonl"))
    assert Manifest.load(tmp_path / "manifest.json").stage("a")["status"] == "failed"  # persisted


def test_runner_rejects_bad_registration(tmp_path):
    runner, _, _ = make_runner(tmp_path)
    runner.register("a", lambda c: StageResult())
    with pytest.raises(ValueError):
        runner.register("a", lambda c: StageResult())
    with pytest.raises(ValueError):
        runner.register("x", lambda c: StageResult(), deps=("missing",))
