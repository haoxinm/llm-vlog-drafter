"""Versioned prompt templates (DESIGN §9). The version is part of the file name: `<id>.v<N>.md`.

Each file starts with front matter declaring `id` and `version`; a mismatch with the file name is an error,
so a template can never silently change meaning under an old version number.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

import yaml

_NAME_RE = re.compile(r"^(?P<id>[a-z][a-z0-9_]*)\.v(?P<version>[1-9][0-9]*)\.md$")
_FRONT_RE = re.compile(r"\A---\n(?P<front>.*?)\n---\n(?P<body>.*)\Z", re.S)


class PromptError(ValueError):
    """Template missing, malformed, or inconsistent with its file name."""


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    version: int
    text: str
    sha256: str


def parse_template(filename: str, raw: str) -> PromptTemplate:
    m = _NAME_RE.match(filename)
    if not m:
        raise PromptError(f"prompt file name must look like '<id>.v<N>.md', got {filename!r}")
    raw = raw.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
    fm = _FRONT_RE.match(raw)
    if not fm:
        raise PromptError(f"{filename}: missing front matter ('---' block with id and version)")
    try:
        meta = yaml.safe_load(fm["front"]) or {}
    except yaml.YAMLError as e:
        raise PromptError(f"{filename}: front matter is not valid YAML: {e}") from None
    if not isinstance(meta, dict):
        raise PromptError(f"{filename}: front matter must be a mapping")
    if meta.get("id") != m["id"]:
        raise PromptError(f"{filename}: front-matter id {meta.get('id')!r} does not match the file name id {m['id']!r}")
    if meta.get("version") != int(m["version"]) or isinstance(meta.get("version"), bool):
        raise PromptError(f"{filename}: front-matter version {meta.get('version')!r} does not match the file name version {m['version']}")
    text = fm["body"].strip("\n")
    if not text.strip():
        raise PromptError(f"{filename}: template body is empty")
    return PromptTemplate(m["id"], int(m["version"]), text, hashlib.sha256(text.encode("utf-8")).hexdigest())


def _read(directory: Path | None, filename: str) -> str:
    try:
        if directory is not None:
            return (directory / filename).read_text(encoding="utf-8")
        return (resources.files(__package__) / filename).read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        raise PromptError(f"prompt template {filename} not found") from None


def available_versions(template_id: str, directory: Path | None = None) -> list[int]:
    names = (p.name for p in directory.iterdir()) if directory is not None else (r.name for r in resources.files(__package__).iterdir())
    out = []
    for n in names:
        m = _NAME_RE.match(n)
        if m and m["id"] == template_id:
            out.append(int(m["version"]))
    return sorted(out)


def load_template(template_id: str, version: int | None = None, *, directory: Path | None = None) -> PromptTemplate:
    """Load `<template_id>.v<version>.md` (latest version when omitted) from the package, or from `directory`."""
    if version is None:
        versions = available_versions(template_id, directory)
        if not versions:
            raise PromptError(f"no prompt template named {template_id!r}")
        version = versions[-1]
    return parse_template(f"{template_id}.v{version}.md", _read(directory, f"{template_id}.v{version}.md"))
