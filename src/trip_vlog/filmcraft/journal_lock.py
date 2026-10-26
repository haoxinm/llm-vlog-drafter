"""Who is building: process liveness and the short exclusive lock around the execution journal's read-modify-write.

The lock is a file (``filmcraft/execution_journal.lock``) created with O_CREAT|O_EXCL, holding pid, host and time. It is held only while the journal
is read, changed and written, never across a whole build. A lock left by a process that is gone (same host, pid not alive) or that is older than
``STALE_AFTER_SECONDS`` (another host: the pid cannot be checked) is taken over. A live holder is waited for briefly, then refused with a clear message.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from trip_vlog.filmcraft.errors import FilmCraftError

STALE_AFTER_SECONDS = 24 * 3600     # a run or lock of another host (or with a reused pid) older than this is taken for dead
LOCK_WAIT_SECONDS = 5.0


def this_host() -> str:
    return socket.gethostname()


def pid_alive(pid: Any) -> bool:
    """Is a process with this pid running on THIS machine? Portable: os.kill(pid, 0) on POSIX, OpenProcess/GetExitCodeProcess on Windows."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32                     # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)     # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.GetLastError() == 5                 # ERROR_ACCESS_DENIED: it exists, we may not look at it
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                          # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def age_seconds(iso: Any) -> float | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds()
    except ValueError:
        return None


def owner_is_live(pid: Any, host: Any, started_at: Any, *, self_is_live: bool = False) -> bool:
    """A holder/runner recorded as (pid, host, started_at) still counts as alive: same host -> pid check; another host -> only while younger than the timeout.
    A record without a pid (written by an older version) is never live."""
    if pid is None:
        return False
    if host == this_host():
        if pid == os.getpid():
            return self_is_live     # a build record of this very process is stale (it is not building when it looks); a lock of this process is held
        age = age_seconds(started_at)
        return pid_alive(pid) and (age is None or age < STALE_AFTER_SECONDS)
    age = age_seconds(started_at)
    return age is not None and age < STALE_AFTER_SECONDS


def lock_path_for(journal_path: Path) -> Path:
    return journal_path.with_suffix(".lock")


def _read_holder(p: Path) -> dict[str, Any]:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def journal_lock(journal_path: Path, wait: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    lock = lock_path_for(journal_path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    payload = json.dumps({"pid": os.getpid(), "host": this_host(), "at": datetime.now(timezone.utc).isoformat()}).encode("utf-8")
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            h = _read_holder(lock)
            if not h:
                # empty/unreadable: a holder that is just writing it, or debris; give a fresh file a moment, then treat it as stale
                try:
                    if time.time() - lock.stat().st_mtime > 2:
                        lock.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
            elif not owner_is_live(h.get("pid"), h.get("host"), h.get("at"), self_is_live=True):
                try:
                    lock.unlink(missing_ok=True)       # stale: its process is gone
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise FilmCraftError(f"another trip-vlog process (pid {h.get('pid')} on {h.get('host')}) is updating {journal_path.name} right now; "
                                     f"wait for it to finish and run the command again (a lock left by a dead process is cleared automatically)")
            time.sleep(0.05)
            continue
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        break
    try:
        yield
    finally:
        try:
            lock.unlink()
        except OSError:
            pass
