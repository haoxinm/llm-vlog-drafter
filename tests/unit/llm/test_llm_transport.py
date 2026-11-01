"""UrllibTransport against a throwaway loopback HTTP server (no external network)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from trip_vlog.llm.transport import HttpRequest, TransportConnectionError, TransportTimeout, UrllibTransport


@pytest.fixture
def server():
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): ...

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if self.path == "/slow":
                time.sleep(1.5)
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:1/steal")
                self.end_headers()
                return
            code = 503 if self.path == "/err" else 200
            out = json.dumps({"ok": True}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "3")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()
    srv.server_close()


def req(url, timeout=5.0):
    return HttpRequest("POST", url, {"content-type": "application/json", "x-api-key": "abc"}, b'{"a":1}', timeout)


def test_round_trip_and_non_2xx_returned_not_raised(server):
    base, seen = server
    r = UrllibTransport().send(req(base + "/v1/x"))
    assert r.status == 200 and json.loads(r.body) == {"ok": True} and r.headers["retry-after"] == "3"
    assert seen[0][0] == "/v1/x" and seen[0][2] == b'{"a":1}' and seen[0][1]["x-api-key"] == "abc"
    e = UrllibTransport().send(req(base + "/err"))
    assert e.status == 503 and e.headers["retry-after"] == "3"


def test_redirects_are_not_followed(server):
    base, seen = server
    r = UrllibTransport().send(req(base + "/redirect"))
    assert r.status == 302 and len(seen) == 1


def test_timeout_and_connection_refused(server):
    base, _ = server
    with pytest.raises(TransportTimeout):
        UrllibTransport().send(req(base + "/slow", timeout=0.3))
    with pytest.raises(TransportConnectionError):
        UrllibTransport().send(req("http://127.0.0.1:1/x"))
