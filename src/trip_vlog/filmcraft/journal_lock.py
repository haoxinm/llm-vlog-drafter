"""Who is building: process liveness and the short exclusive lock around the execution journal's read-modify-write.

The lock is an OS advisory lock on byte 0 of a persistent file (``filmcraft/execution_journal.lock``, never deleted): ``fcntl.flock`` on POSIX,
``msvcrt.locking`` on Windows. The operating system drops it when the holding process dies, so there is no stale-lock logic and no takeover race. It
is held only while the journal is read, changed and written, never across a whole build. A busy lock is polled until ``LOCK_WAIT_SECONDS``, then
refused with a message. pid / host / time are written into the file (from offset 8, past the lock byte) as diagnostics only. Re-entrant use by the
same thread is allowed (it does not lock twice).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import socket
import threading
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


_DIAG_OFFSET = 8
_held = threading.local()            # paths this thread holds: nested use does not lock (and so cannot deadlock) a second time


_BUSY = {errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK}   # held by someone else (Windows reports contention as EACCES)


def _try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)       # type: ignore[attr-defined]
        except OSError as e:
            if e.errno in _BUSY:
                return False
            raise FilmCraftError(f"cannot lock the FilmCraft journal ({e}); is the workspace on a file system without file locking?") from e
        return True
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        if e.errno in _BUSY:
            return False
        raise FilmCraftError(f"cannot lock the FilmCraft journal ({e}); is the workspace on a file system without file locking?") from e
    return True


def _unlock(fd: int) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)       # type: ignore[attr-defined]
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass                                            # closing the handle releases it anyway


def _read_holder(p: Path) -> dict[str, Any]:
    try:
        with open(p, "rb") as f:
            f.seek(_DIAG_OFFSET)
            d = json.loads(f.read().decode("utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


@contextlib.contextmanager
def journal_lock(journal_path: Path, wait: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    lock = lock_path_for(journal_path)
    key = str(lock)
    mine: set[str] = getattr(_held, "paths", None) or set()
    _held.paths = mine
    if key in mine:
        yield
        return
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
    try:
        deadline = time.monotonic() + wait
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                h = _read_holder(lock)
                who = f"pid {h.get('pid')} on {h.get('host')}" if h else "another process"
                raise FilmCraftError(f"{journal_path.name} is being updated by {who}; this only takes a moment, so run the command again in a few seconds. "
                                     f"If it keeps happening, check that no other trip-vlog command is running on this workspace")
            time.sleep(0.05)
        mine.add(key)
        try:
            try:
                os.ftruncate(fd, _DIAG_OFFSET)
                os.lseek(fd, _DIAG_OFFSET, os.SEEK_SET)
                os.write(fd, json.dumps({"pid": os.getpid(), "host": this_host(), "at": datetime.now(timezone.utc).isoformat()}).encode("utf-8"))
            except OSError:
                pass                                    # diagnostics only
            yield
        finally:
            mine.discard(key)
            _unlock(fd)
    finally:
        os.close(fd)
