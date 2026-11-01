"""DaVinci Resolve scripting support (DESIGN 15.4-15.6).

Everything here except ``finish`` is standard-library only, so ``tools/resolve_verify.py`` can import it from a plain
repo checkout. ``finish`` (the ``trip-vlog resolve-finish`` step) needs the rest of the package.
"""
