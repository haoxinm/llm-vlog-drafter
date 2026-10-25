"""Exception hierarchy for the FilmCraft adapter. Messages are user-facing and actionable."""

from __future__ import annotations

from typing import Any


class FilmCraftError(Exception):
    """Base class for every adapter error."""


class FilmCraftNotFound(FilmCraftError):
    """No usable filmcraft-cli executable could be located."""


class CapabilityError(FilmCraftError):
    """The installed FilmCraft lacks a capability the pipeline requires."""

    def __init__(self, message: str, missing: list[str] | None = None):
        super().__init__(message)
        self.missing = list(missing or [])


class McpError(FilmCraftError):
    """Transport / protocol level failure talking to the MCP server."""


class McpTimeout(McpError):
    """A request did not complete within its timeout."""


class McpClosed(McpError):
    """The server process exited or closed its pipes."""


class McpProtocolError(McpError):
    """The server answered with a JSON-RPC error object."""

    def __init__(self, message: str, code: int | None = None, data: Any = None):
        super().__init__(message)
        self.code = code
        self.data = data


class CommandError(FilmCraftError):
    """FilmCraft executed a tool or command and reported failure (``isError``)."""

    def __init__(self, command: str, message: str, params: Any = None):
        super().__init__(f"FilmCraft command {command} failed: {message}")
        self.command = command
        self.detail = message
        self.params = params


class ParameterError(FilmCraftError):
    """A command or tool call was rejected locally, before reaching FilmCraft."""


class PathPolicyError(ParameterError):
    """A path is not absolute or lies outside the allowed read/write roots."""


class SpecValidationError(FilmCraftError):
    """The TimelineSpec is invalid. ``issues`` lists every problem found."""

    def __init__(self, issues: list[str]):
        super().__init__("invalid timeline spec:\n  - " + "\n  - ".join(issues))
        self.issues = list(issues)


class SequenceNotEmpty(FilmCraftError):
    """apply_timeline refuses to append to a sequence that already holds clips (would duplicate)."""


class VerificationFailed(FilmCraftError):
    """The timeline read back from FilmCraft does not match the spec."""

    def __init__(self, message: str, mismatches: list[Any] | None = None):
        super().__init__(message)
        self.mismatches = list(mismatches or [])


class BuildFailed(FilmCraftError):
    """A project build stopped; the journal records where and why."""

    def __init__(self, message: str, version: int | None = None, project_path: Any = None):
        super().__init__(message)
        self.version = version
        self.project_path = project_path


class ToolMissing(FilmCraftError):
    """A required helper tool (ffprobe) is not installed."""
