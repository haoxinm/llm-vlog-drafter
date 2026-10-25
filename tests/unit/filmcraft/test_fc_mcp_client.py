from __future__ import annotations

import json
import sys
import textwrap
import time

import pytest

import sys as _sys
from pathlib import Path as _P

_sys.path.insert(0, str(_P(__file__).resolve().parents[2] / "fixtures" / "filmcraft"))
import fcsupport  # noqa: E402
from fcsupport import fc  # noqa: E402,F401
from trip_vlog.filmcraft.command_log import CommandLog
from trip_vlog.filmcraft.errors import McpClosed, McpProtocolError, McpTimeout
from trip_vlog.filmcraft.mcp_client import McpClient, parse_tool_result


def fake_client(**kw) -> McpClient:
    return McpClient([*fcsupport.FAKE_ARGV, "mcp"], default_timeout=15, **kw)


def script_client(tmp_path, body: str, **kw) -> McpClient:
    p = tmp_path / "srv.py"
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return McpClient([sys.executable, str(p)], default_timeout=kw.pop("default_timeout", 10), **kw)


PRELUDE = '''
import sys, json
def send(m): sys.stdout.buffer.write((json.dumps(m)+"\\n").encode()); sys.stdout.buffer.flush()
def reply(rid, result): send({"jsonrpc":"2.0","id":rid,"result":result})
INIT = {"protocolVersion":"2025-06-18","capabilities":{},"serverInfo":{"name":"s","version":"1"}}
'''


def test_handshake_tools_and_tool_call():
    with fake_client() as c:
        assert c.server_info["name"] == "filmcraft" and c.protocol_version == "2025-06-18"
        names = [t["name"] for t in c.list_tools()]
        assert "command_run" in names and "sequence_inspect" in names
        res = c.tool_call("command_run", {"id": "file.newProject", "params": {"name": "x"}})
        assert res.ok
        bad = c.tool_call("command_run", {"id": "no.such", "params": {}})
        assert not bad.ok and "unknown command" in bad.text


def test_protocol_error_is_raised(tmp_path):
    with fake_client() as c:
        with pytest.raises(McpProtocolError, match="method not found"):
            c.request("nonsense/method")


def test_timeout_then_close_does_not_hang(monkeypatch):
    monkeypatch.setenv("FAKE_FC_SLEEP_ON", "file.newProject")
    c = fake_client()
    t0 = time.monotonic()
    with pytest.raises(McpTimeout, match="timed out"):
        c.tool_call("command_run", {"id": "file.newProject", "params": {"name": "x"}}, timeout=0.5)
    c.close(grace=0.5)
    assert time.monotonic() - t0 < 15
    assert not c.alive
    c.close()  # idempotent


def test_server_dies_mid_request(tmp_path):
    c = script_client(tmp_path, PRELUDE + '''
for line in sys.stdin.buffer:
    m = json.loads(line)
    if m.get("method") == "initialize": reply(m["id"], INIT)
    elif m.get("id") is not None:
        sys.stderr.write("boom: out of cheese\\n"); sys.stderr.flush(); sys.exit(3)
''')
    with pytest.raises(McpClosed, match="boom: out of cheese|exit code"):
        c.request("tools/list")
    with pytest.raises(McpClosed):
        c.request("tools/list")
    c.close()
    assert not c.alive


def test_unstartable_binary(tmp_path):
    with pytest.raises(McpClosed, match="cannot start"):
        McpClient([str(tmp_path / "does-not-exist")])


def test_stderr_is_drained_so_a_chatty_server_cannot_deadlock(tmp_path):
    c = script_client(tmp_path, PRELUDE + '''
sys.stderr.write("x" * 100 + "\\n" ) 
for line in sys.stdin.buffer:
    m = json.loads(line)
    if m.get("method") == "initialize": reply(m["id"], INIT)
    elif m.get("id") is not None:
        for _ in range(5000): sys.stderr.write("noise " * 40 + "\\n")   # ~1.2 MB: far more than a pipe buffer
        sys.stderr.flush()
        reply(m["id"], {"tools": []})
''')
    with c:
        assert c.request("tools/list") == {"tools": []}
        assert 0 < len(c.stderr_tail()) <= 200


def test_crlf_blank_lines_junk_and_unicode_framing(tmp_path):
    c = script_client(tmp_path, PRELUDE + '''
def send_crlf(m): sys.stdout.buffer.write((json.dumps(m, ensure_ascii=False)+"\\r\\n").encode("utf-8")); sys.stdout.buffer.flush()
for line in sys.stdin.buffer:
    m = json.loads(line.decode("utf-8"))
    if m.get("method") == "initialize": send_crlf({"jsonrpc":"2.0","id":m["id"],"result":INIT})
    elif m.get("id") is not None:
        sys.stdout.buffer.write(b"\\n   \\nthis is not json\\n"); sys.stdout.buffer.flush()
        send_crlf({"jsonrpc":"2.0","method":"notifications/progress","params":{"p":1}})
        send_crlf({"jsonrpc":"2.0","id":m["id"],"result":{"echo": m["params"]}})
''')
    with c:
        payload = {"text": "café 日本語 \U0001f3ac", "n": 1}
        assert c.request("echo", payload) == {"echo": payload}


def test_server_to_client_requests_get_method_not_found(tmp_path):
    c = script_client(tmp_path, PRELUDE + '''
seen = None
for line in sys.stdin.buffer:
    m = json.loads(line)
    if m.get("method") == "initialize": reply(m["id"], INIT)
    elif m.get("method") == "ping-me":
        send({"jsonrpc":"2.0","id":99,"method":"roots/list"})
    elif "result" in m or "error" in m:
        seen = m
    elif m.get("id") is not None:
        reply(m["id"], {"client_answered": seen})
''')
    with c:
        c.notify("ping-me")
        time.sleep(0.5)
        res = c.request("check")
        assert res["client_answered"]["error"]["code"] == -32601


def test_large_message_roundtrip(tmp_path):
    c = script_client(tmp_path, PRELUDE + '''
for line in sys.stdin.buffer:
    m = json.loads(line)
    if m.get("method") == "initialize": reply(m["id"], INIT)
    elif m.get("id") is not None: reply(m["id"], {"len": len(m["params"]["blob"]), "blob": m["params"]["blob"]})
''')
    with c:
        blob = "A" * 3_000_000
        res = c.request("big", {"blob": blob}, timeout=30)
        assert res["len"] == 3_000_000 and res["blob"] == blob


def test_concurrent_requests_are_demultiplexed(tmp_path):
    import threading

    c = script_client(tmp_path, PRELUDE + '''
import time
for line in sys.stdin.buffer:
    m = json.loads(line)
    if m.get("method") == "initialize": reply(m["id"], INIT)
    elif m.get("id") is not None:
        time.sleep(m["params"]["delay"]); reply(m["id"], {"who": m["params"]["who"]})
''')
    results = {}
    with c:
        def call(who, delay):
            results[who] = c.request("x", {"who": who, "delay": delay})["who"]
        ts = [threading.Thread(target=call, args=(w, d)) for w, d in (("slow", 0.4), ("fast", 0.0))]
        [t.start() for t in ts]
        [t.join(10) for t in ts]
    assert results == {"slow": "slow", "fast": "fast"}


def test_parse_tool_result_variants():
    r = parse_tool_result({"content": [{"type": "text", "text": '{"a": 1}'}]})
    assert r.ok and r.payload == {"a": 1}
    r = parse_tool_result({"content": [{"type": "text", "text": "plain words"}], "isError": True})
    assert not r.ok and r.payload == "plain words"
    r = parse_tool_result({"content": [{"type": "image", "data": "AAAA", "mimeType": "image/png"}, {"type": "text", "text": "rendered frame"}]})
    assert r.ok and len(r.images) == 1 and r.text == "rendered frame"
    assert parse_tool_result({}).payload == ""


def test_no_shell_interpretation_of_arguments(tmp_path):
    marker = tmp_path / "pwned"
    # a hostile "path" is just one argv element: nothing is expanded or executed
    with pytest.raises(McpClosed):
        McpClient([f"{sys.executable}; touch {marker}"])
    assert not marker.exists()


def test_requests_and_responses_are_logged(tmp_path):
    log = CommandLog(tmp_path / "cmd.jsonl")
    with fake_client(log=log, label="t") as c:
        c.tool_call("command_run", {"id": "file.newProject", "params": {"name": "x"}})
    recs = [json.loads(l) for l in (tmp_path / "cmd.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [(r["dir"], r["method"]) for r in recs]
    assert ("request", "initialize") in kinds and ("response", "tools/call") in kinds
    call = next(r for r in recs if r["dir"] == "request" and r["method"] == "tools/call")
    assert call["command"] == "file.newProject" and call["session"] == "t"
