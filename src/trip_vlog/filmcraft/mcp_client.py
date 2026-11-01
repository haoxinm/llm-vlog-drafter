"""Robust stdio JSON-RPC (MCP) client for ``filmcraft-cli mcp``.

* argument-list ``Popen`` (never a shell); binary pipes; UTF-8; messages framed by ``\\n`` (a trailing
  ``\\r`` is tolerated),
* one reader thread demultiplexes responses so every request has its own timeout,
* stderr is drained continuously by a second thread (a full pipe would otherwise stall the server) and the
  tail is kept for diagnostics,
* clean shutdown on Windows and POSIX: close stdin, wait, then terminate (process group on POSIX,
  ``CREATE_NEW_PROCESS_GROUP`` + ``terminate`` on Windows), then kill.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from trip_vlog.filmcraft.command_log import CommandLog
from trip_vlog.filmcraft.errors import McpClosed, McpProtocolError, McpTimeout

PROTOCOL_VERSION = "2025-06-18"
_IS_WINDOWS = sys.platform == "win32"


@dataclass
class ToolResult:
    """Outcome of ``tools/call``. ``ok`` is False when the server flagged ``isError``."""

    ok: bool
    payload: Any  # parsed JSON of the text content when possible, else the text
    text: str
    content: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def images(self) -> list[dict[str, Any]]:
        return [c for c in self.content if c.get("type") == "image"]


def parse_tool_result(result: Mapping[str, Any]) -> ToolResult:
    content = [c for c in result.get("content", []) if isinstance(c, dict)]
    text = "".join(c.get("text", "") for c in content if c.get("type", "text") == "text")
    payload: Any = text
    if text.strip():
        try:
            payload = json.loads(text)
        except ValueError:
            payload = text
    return ToolResult(ok=not result.get("isError", False), payload=payload, text=text, content=content, raw=dict(result))


class McpClient:
    def __init__(
        self,
        argv: Sequence[str | os.PathLike[str]],
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        default_timeout: float = 60.0,
        log: CommandLog | None = None,
        label: str = "session",
        client_name: str = "trip-vlog-filmcraft-adapter",
        client_version: str = "0.1",
        handshake: bool = True,
    ):
        self.argv = [os.fspath(a) for a in argv]
        self.label = label
        self.default_timeout = default_timeout
        self.log = log
        self._client_info = {"name": client_name, "version": client_version}
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[int, "queue.Queue[dict[str, Any] | None]"] = {}
        self._closed = threading.Event()
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=200)
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.instructions: str | None = None
        kwargs: dict[str, Any] = {}
        if _IS_WINDOWS:
            kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            kwargs["start_new_session"] = True
        try:
            self.proc = subprocess.Popen(
                self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=os.fspath(cwd) if cwd else None, env=dict(env) if env is not None else None, shell=False, **kwargs)
        except OSError as e:
            raise McpClosed(f"cannot start {self.argv[0]!r}: {e.strerror or e}") from None
        self._reader = threading.Thread(target=self._read_loop, name=f"mcp-stdout-{label}", daemon=True)
        self._err_reader = threading.Thread(target=self._drain_stderr, name=f"mcp-stderr-{label}", daemon=True)
        self._reader.start()
        self._err_reader.start()
        if handshake:
            try:
                self.initialize()
            except BaseException:
                self.close(graceful=False)
                raise

    # ------------------------------------------------------------------ threads
    def _read_loop(self) -> None:
        out = self.proc.stdout
        assert out is not None
        try:
            for raw in iter(out.readline, b""):
                line = raw.rstrip(b"\r\n")
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._stderr_tail.append("<non-JSON line on stdout: %r>" % line[:200])
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg and "id" in msg:  # server -> client request: we support none
                    self._reply_unsupported(msg)
                elif "id" in msg and ("result" in msg or "error" in msg):
                    q = self._pending.get(msg["id"])
                    if q is not None:
                        q.put(msg)
                # notifications (progress, logging) are ignored
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            for q in list(self._pending.values()):
                q.put(None)

    def _drain_stderr(self) -> None:
        err = self.proc.stderr
        assert err is not None
        try:
            for raw in iter(err.readline, b""):
                self._stderr_tail.append(raw.decode("utf-8", "replace").rstrip("\r\n"))
        except (OSError, ValueError):
            pass

    def _reply_unsupported(self, msg: Mapping[str, Any]) -> None:
        try:
            self._send({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "method not supported"}})
        except McpClosed:
            pass

    # ------------------------------------------------------------------ io
    def _send(self, msg: Mapping[str, Any]) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        stdin = self.proc.stdin
        try:
            with self._write_lock:
                assert stdin is not None
                stdin.write(data)
                stdin.flush()
        except (OSError, ValueError, AssertionError):
            raise McpClosed(f"FilmCraft server is not accepting input. {self._diagnostics()}") from None

    def _diagnostics(self) -> str:
        rc = self.proc.poll()
        tail = "\n".join(list(self._stderr_tail)[-8:])
        return f"(exit code {rc}; stderr tail: {tail!r})" if rc is not None or tail else ""

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None and not self._closed.is_set()

    def stderr_tail(self) -> list[str]:
        return list(self._stderr_tail)

    # ------------------------------------------------------------------ protocol
    def request(self, method: str, params: Mapping[str, Any] | None = None, timeout: float | None = None,
                *, _log_extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
        timeout = self.default_timeout if timeout is None else timeout
        with self._id_lock:
            self._next_id += 1
            rid = self._next_id
        q: "queue.Queue[dict[str, Any] | None]" = queue.Queue(maxsize=1)
        self._pending[rid] = q
        extra = dict(_log_extra or {})
        t0 = time.monotonic()
        if self.log:
            self.log.write(dir="request", session=self.label, id=rid, method=method, params=params or {}, **extra)
        try:
            if self._closed.is_set():
                raise McpClosed(f"FilmCraft server has exited. {self._diagnostics()}")
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": dict(params or {})})
            try:
                msg = q.get(timeout=timeout)
            except queue.Empty:
                if self.log:
                    self.log.write(dir="response", session=self.label, id=rid, method=method, error="timeout",
                                   ms=round((time.monotonic() - t0) * 1000), **extra)
                raise McpTimeout(f"{method} timed out after {timeout:g}s; the session state is unknown, "
                                 "close and reopen the project") from None
            if msg is None:
                raise McpClosed(f"FilmCraft server closed the connection during {method}. {self._diagnostics()}")
        finally:
            self._pending.pop(rid, None)
        ms = round((time.monotonic() - t0) * 1000)
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            if self.log:
                self.log.write(dir="response", session=self.label, id=rid, method=method, error=err, ms=ms, **extra)
            raise McpProtocolError(f"{method}: {err.get('message', err)}", err.get("code"), err.get("data"))
        result = msg.get("result", {})
        if self.log:
            self.log.write(dir="response", session=self.label, id=rid, method=method, result=result, ms=ms, **extra)
        return result

    def notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params:
            msg["params"] = dict(params)
        self._send(msg)

    def initialize(self, timeout: float | None = None) -> dict[str, Any]:
        res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": self._client_info}, timeout)
        self.server_info = dict(res.get("serverInfo", {}))
        self.protocol_version = res.get("protocolVersion")
        self.instructions = res.get("instructions")
        self.notify("notifications/initialized")
        return res

    def list_tools(self, timeout: float | None = None) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        while True:
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            tools += res.get("tools", [])
            cursor = res.get("nextCursor")
            if not cursor:
                return tools

    def tool_call(self, name: str, arguments: Mapping[str, Any] | None = None, timeout: float | None = None) -> ToolResult:
        args = dict(arguments or {})
        extra: dict[str, Any] = {"tool": name}
        if name == "command_run":
            extra["command"] = args.get("id")
        res = self.request("tools/call", {"name": name, "arguments": args}, timeout, _log_extra=extra)
        return parse_tool_result(res)

    # ------------------------------------------------------------------ shutdown
    def close(self, graceful: bool = True, grace: float = 5.0) -> int | None:
        """Stop the server and release threads/pipes. Idempotent. Returns the exit code."""
        proc = self.proc
        if graceful and proc.poll() is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                pass
        if proc.poll() is None:
            self._terminate(force=False)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self._terminate(force=True)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:
                pass
        for t in (self._reader, self._err_reader):
            t.join(timeout=2)
        self._closed.set()
        return proc.returncode

    def kill(self) -> int | None:
        """Hard-stop the server without letting it flush anything (crash simulation)."""
        return self.close(graceful=False)

    def _terminate(self, force: bool) -> None:
        proc = self.proc
        try:
            if _IS_WINDOWS:
                proc.kill() if force else proc.terminate()
            else:
                os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill() if force else proc.terminate()
            except OSError:
                pass

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
