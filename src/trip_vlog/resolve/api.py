"""Defensive helpers for calling Resolve's scripting API (every call may raise or return None)."""
from __future__ import annotations

from typing import Any


def method(obj: Any, name: str):
    """getattr that tolerates API methods missing in this Resolve version."""
    def missing(*a, **k):
        raise AttributeError(f"{type(obj).__name__} has no method {name}")
    return getattr(obj, name, missing)


def safe_call(fn, *a, default=None, errors: list | None = None, label: str = ""):
    """Call a Resolve API method defensively; every call may raise or return None."""
    try:
        return fn(*a)
    except Exception as e:
        if errors is not None:
            errors.append(f"{label or getattr(fn, '__name__', 'call')}: {type(e).__name__}: {e}")
        return default


def to_number(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None



def type_name(v: Any) -> str:
    return type(v).__name__


def jsonable(r: Any) -> Any:
    if isinstance(r, (bool, int, float, str, type(None))):
        return r
    if isinstance(r, (list, tuple)):
        return [jsonable(x) for x in r]
    if isinstance(r, dict):
        return {str(k): jsonable(v) for k, v in r.items()}
    return type_name(r)

