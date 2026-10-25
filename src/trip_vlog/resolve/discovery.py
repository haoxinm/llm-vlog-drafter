"""Locate and connect to DaVinci Resolve's scripting API (DESIGN 15.4, 22.1)."""
from __future__ import annotations

import os
import platform
import struct
import sys
from typing import Any


def default_script_paths(plat: str | None = None, env: dict | None = None) -> tuple[str, str]:
    """Documented default (API dir, fusionscript library) for a platform (sys.platform style)."""
    plat = plat or sys.platform
    env = os.environ if env is None else env
    if plat.startswith("win"):
        pd = env.get("PROGRAMDATA", r"C:\ProgramData")
        return (pd + r"\Blackmagic Design\DaVinci Resolve\Support\Developer\Scripting",
                r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll")
    if plat == "darwin":
        return ("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting",
                "/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so")
    return ("/opt/resolve/Developer/Scripting", "/opt/resolve/libs/Fusion/fusionscript.so")


def discover_resolve(plat: str | None = None, env: dict | None = None) -> dict:
    """Resolve RESOLVE_SCRIPT_API / RESOLVE_SCRIPT_LIB (env wins over defaults) and check existence."""
    env = os.environ if env is None else env
    d_api, d_lib = default_script_paths(plat, env)
    api = env.get("RESOLVE_SCRIPT_API") or d_api
    lib = env.get("RESOLVE_SCRIPT_LIB") or d_lib
    modules = os.path.join(api, "Modules")
    return {
        "api": api, "lib": lib, "modules": modules,
        "api_source": "env" if env.get("RESOLVE_SCRIPT_API") else "default",
        "lib_source": "env" if env.get("RESOLVE_SCRIPT_LIB") else "default",
        "modules_exists": os.path.isdir(modules),
        "lib_exists": os.path.isfile(lib),
    }


PROVEN_PYTHON = "3.14 (Windows 11, Resolve Studio 21.1.1.10)"
LOWEST_RISK_PYTHON = "3.10-3.12"


def python_info() -> dict:
    """The interpreter that is about to load Resolve's fusionscript library (it must match in bitness and ABI)."""
    return {"version": platform.python_version(), "bits": struct.calcsize("P") * 8, "implementation": platform.python_implementation(),
            "executable": sys.executable}


def python_compat_hint(py: dict | None = None) -> str:
    """Advice when fusionscript cannot be loaded or Resolve cannot be reached from this interpreter."""
    py = py or python_info()
    return (f"This is Python {py['version']} ({py['bits']}-bit {py['implementation']}). Resolve's fusionscript library is loaded "
            f"into the interpreter and only works with a 64-bit CPython of a version it supports. Proven on {PROVEN_PYTHON}; "
            f"{LOWEST_RISK_PYTHON} is the documented lowest-risk range. If the import failed with a DLL/shared-library or "
            "version error, run trip-vlog with one of those Python versions (python.org build, not the Microsoft Store "
            "stub), or point RESOLVE_SCRIPT_LIB at the fusionscript library of your Resolve installation.")


def connect_resolve(plat: str | None = None, env: dict | None = None) -> tuple[Any, dict]:
    """Return (resolve_or_None, info). info['diagnosis'] lists reasons when None."""
    info = discover_resolve(plat, env)
    diag: list[str] = []
    if not info["modules_exists"]:
        diag.append(f"Scripting modules folder not found: {info['modules']} "
                    "(is DaVinci Resolve installed? set RESOLVE_SCRIPT_API to the 'Scripting' folder).")
    if not info["lib_exists"]:
        diag.append(f"fusionscript library not found: {info['lib']} (set RESOLVE_SCRIPT_LIB to its full path).")
    # Resolve's own helper module reads these when it loads fusionscript.
    os.environ.setdefault("RESOLVE_SCRIPT_API", info["api"])
    os.environ.setdefault("RESOLVE_SCRIPT_LIB", info["lib"])
    if info["modules_exists"] and info["modules"] not in sys.path:
        sys.path.insert(0, info["modules"])
    mod = None
    if info["modules_exists"]:
        try:
            import DaVinciResolveScript as mod  # type: ignore  # noqa: N813
        except Exception as e:  # ImportError, OSError loading the dll, ...
            diag.append(f"import DaVinciResolveScript failed: {type(e).__name__}: {e}")
            diag.append(python_compat_hint())
    resolve = None
    if mod is not None:
        try:
            resolve = mod.scriptapp("Resolve")
        except Exception as e:
            diag.append(f"scriptapp('Resolve') raised {type(e).__name__}: {e}")
            diag.append(python_compat_hint())
        if resolve is None and not any("scriptapp" in x for x in diag):
            diag.append("scriptapp('Resolve') returned None. Either Resolve is not running, or external scripting is "
                        "disabled: set Preferences > System > General > 'External scripting using' = Local "
                        "(the free edition of Resolve may not offer external scripting at all; Studio does).")
    info["diagnosis"] = diag
    info["python"] = python_info()
    return resolve, info

