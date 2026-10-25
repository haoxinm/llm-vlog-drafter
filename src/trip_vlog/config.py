"""Project configuration (DESIGN §4.1): pydantic models, loading, overrides, path safety."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trip_vlog.paths import check_portable_component, is_within

CLOUD_PROVIDERS = {"anthropic"}
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
_PROVIDER_RE = r"^[A-Za-z0-9_.-]+$"


class ConfigError(ValueError):
    """Invalid configuration or unsafe paths. Message is user-facing."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ProjectConfig(_Model):
    title: str = Field("Trip Vlog", min_length=1)
    target_duration_seconds: float = Field(180, gt=0, le=4 * 3600)
    aspect_ratio: str = "16:9"
    frame_rate: str = "30"
    language: str = Field("en", pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")
    story_mode: Literal["chronological", "mostly_chronological", "thematic"] = "mostly_chronological"
    style: str = "personal, cinematic, authentic"
    audience: str = "friends and family"
    include_narration: bool = False
    include_subtitles: bool = False
    music_policy: Literal["none", "user_supplied_only"] = "user_supplied_only"
    preserve_natural_audio: bool = True
    target_loudness_lufs: float | None = Field(None, ge=-70, le=0)
    # total duration may differ from the target by max(pct% of target, seconds); used by plan validation and QC
    duration_tolerance_pct: float = Field(5.0, ge=0, le=100)
    duration_tolerance_seconds: float = Field(2.0, ge=0, le=600)

    @field_validator("aspect_ratio")
    @classmethod
    def _aspect(cls, v: str) -> str:
        m = re.fullmatch(r"(\d{1,3}):(\d{1,3})", v.strip())
        if not m or int(m[1]) == 0 or int(m[2]) == 0:
            raise ValueError("aspect_ratio must look like '16:9' with positive integers")
        return f"{int(m[1])}:{int(m[2])}"

    @field_validator("frame_rate", mode="before")
    @classmethod
    def _frame_rate(cls, v: Any) -> str:
        if isinstance(v, bool):
            raise ValueError("frame_rate must be a number or a rational like '30000/1001'")
        s = str(v).strip()
        try:
            fr = Fraction(s)
        except (ValueError, ZeroDivisionError):
            raise ValueError("frame_rate must be a number or a rational like '30000/1001'") from None
        if not (0 < fr <= 240):
            raise ValueError("frame_rate must be in (0, 240]")
        return s


class EditorialConfig(_Model):
    must_include: list[str] = []
    must_exclude: list[str] = []
    preferred_topics: list[str] = []
    max_similar_shots_per_scene: int = Field(2, ge=1, le=20)
    opening_hook_seconds: float = Field(8, ge=0, le=60)
    closing_seconds: float = Field(10, ge=0, le=120)
    allow_nonlinear_hook: bool = True
    avoid_fabricated_context: bool = True
    avoid_excessive_transitions: bool = True


class MediaConfig(_Model):
    recursive: bool = True
    follow_symlinks: bool = False
    fingerprint: Literal["fast", "sha256"] = "fast"
    proxy_policy: Literal["on_demand", "always", "never"] = "on_demand"
    vision_sampling_fps: float = Field(1.0, gt=0, le=30)
    fine_sampling_fps: float = Field(4.0, gt=0, le=60)
    fine_sampling_enabled: bool = False
    segment_window_seconds: float = Field(6.0, gt=0, le=120)
    transcribe_speech: bool = True
    vision_provider: str = Field("anthropic", pattern=_PROVIDER_RE)
    llm_provider: str = Field("anthropic", pattern=_PROVIDER_RE)
    llm_model: str | None = None
    local_only: bool = False
    # Phase 1 additions
    ffprobe_path: str | None = None
    ffmpeg_path: str | None = None
    probe_timeout_seconds: float = Field(60.0, gt=0, le=3600)
    thumbnail_max_side: int = Field(480, ge=64, le=2048)
    contact_sheet_frames: int = Field(6, ge=0, le=16)  # 0 disables per-video sheets
    export_gps: bool = False  # GPS is private: kept in SQLite, omitted from JSONL exports unless true
    # Phase 2 deterministic segmentation / baseline planning
    segment_min_seconds: float = Field(1.5, gt=0, le=60)       # shorter candidate segments are merged with a neighbour
    scene_threshold: float = Field(0.30, gt=0, lt=1)           # ffmpeg scene-cut score
    silence_threshold_db: float = Field(-35.0, ge=-120, le=0)  # audio below this level counts as silence
    still_duration_seconds: float = Field(3.0, gt=0, le=600)   # default on-screen time of a photo

    @model_validator(mode="after")
    def _check(self) -> "MediaConfig":
        if self.fine_sampling_enabled and self.fine_sampling_fps < self.vision_sampling_fps:
            raise ValueError("fine_sampling_fps must be >= vision_sampling_fps when fine sampling is enabled")
        if self.local_only:
            for name in ("vision_provider", "llm_provider"):
                if getattr(self, name) in CLOUD_PROVIDERS:
                    raise ValueError(f"local_only is true but {name}={getattr(self, name)!r} is a cloud provider")
        return self


class WorkflowConfig(_Model):
    approval: Literal["stop_after_outline", "auto_approve"] = "stop_after_outline"
    planner: Literal["baseline", "llm"] = "baseline"  # "llm" arrives in Phase 3/4 and is rejected until then


class FilmCraftConfig(_Model):
    mode: Literal["mcp", "cli", "bridge"] = "mcp"
    executable: str | None = None
    control_host: str = "127.0.0.1"
    control_port: int | None = Field(None, ge=1, le=65535)
    project_name: str = "Trip Vlog Draft"
    sequence_name: str = "Main Draft"
    prefer_native_project: bool = True
    # FCP7 XML is the primary handoff format (imported cleanly in Resolve 21.1 with the Phase 0 fixture), OTIO secondary.
    # fcpxml/edl/aaf are accepted only when named explicitly: they crashed, froze or could not be opened in Resolve 21.1.
    interchange_formats: list[Literal["fcp7_xml", "otio", "fcpxml", "edl", "aaf"]] = ["fcp7_xml", "otio"]
    request_timeout_seconds: float = Field(120.0, gt=0, le=3600)  # per FilmCraft command (renders get a longer budget)
    preview_scale: float = Field(1.0, gt=0, le=1)                  # 1.0 = full sequence size; <1 renders a smaller draft
    handle_policy: Literal["error", "cut"] = "error"               # dissolve without source handles: fail, or downgrade to a recorded cut
    ambient_low_gain_db: float = Field(-12.0, ge=-60, le=0)        # gain of clips whose audio_policy is "ambient_low"

    @field_validator("control_host")
    @classmethod
    def _loopback(cls, v: str) -> str:
        if v.lower() not in LOOPBACK_HOSTS:
            raise ValueError("control_host must be a loopback address (127.0.0.1, ::1, localhost)")
        return v


class QcConfig(_Model):
    min_still_seconds: float = Field(1.5, gt=0, le=60)       # stills shorter than this fail QC
    black_min_seconds: float = Field(0.5, gt=0, le=60)       # blackdetect: minimum black run to report
    black_picture_threshold: float = Field(0.98, gt=0, le=1)  # blackdetect pic_th
    black_pixel_threshold: float = Field(0.10, ge=0, lt=1)    # blackdetect pix_th
    contact_sheet_columns: int = Field(4, ge=1, le=12)
    contact_sheet_max_frames: int = Field(48, ge=2, le=200)
    contact_sheet_tile_width: int = Field(320, ge=64, le=1280)


class OutputsConfig(_Model):
    project_file: str = "filmcraft/TripVlog.fcproj"
    preview_file: str = "preview/draft.mp4"
    edit_plan_file: str = "plans/edit_plan.json"
    interchange_dir: str = "resolve_handoff"

    @field_validator("*")
    @classmethod
    def _relative_and_portable(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("path must not be empty")
        norm = v.replace("\\", "/")
        p = PurePosixPath(norm)
        if p.is_absolute() or re.match(r"^[A-Za-z]:", norm) or norm.startswith("//"):
            raise ValueError(f"output path must be relative to the workspace: {v!r}")
        parts = [x for x in p.parts if x != "."]
        if not parts or ".." in parts:
            raise ValueError(f"output path must stay inside the workspace (no '..'): {v!r}")
        for part in parts:
            problem = check_portable_component(part)
            if problem:
                raise ValueError(f"output path {v!r} {problem}")
        return "/".join(parts)


class Config(_Model):
    project: ProjectConfig = ProjectConfig()
    editorial: EditorialConfig = EditorialConfig()
    media: MediaConfig = MediaConfig()
    workflow: WorkflowConfig = WorkflowConfig()
    filmcraft: FilmCraftConfig = FilmCraftConfig()
    qc: QcConfig = QcConfig()
    outputs: OutputsConfig = OutputsConfig()

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")

    def config_hash(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_yaml(self) -> str:
        return yaml.safe_dump(self.to_dict(), sort_keys=False, allow_unicode=True)


def _format_errors(err: ValidationError) -> str:
    lines = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e["loc"]) or "<root>"
        lines.append(f"  {loc}: {e['msg']}")
    return "invalid configuration:\n" + "\n".join(lines)


def apply_overrides(raw: dict[str, Any], overrides: Mapping[str, Any] | None) -> dict[str, Any]:
    """Apply dotted-key overrides ('media.fingerprint': 'sha256'). None values are ignored."""
    out = json.loads(json.dumps(raw))  # deep copy of plain data
    for key, value in (overrides or {}).items():
        if value is None:
            continue
        parts = key.split(".")
        if len(parts) != 2:
            raise ConfigError(f"override key must look like 'section.field': {key!r}")
        section = out.setdefault(parts[0], {})
        if not isinstance(section, dict):
            raise ConfigError(f"config section {parts[0]!r} must be a mapping")
        section[parts[1]] = value
    return out


def parse_config(raw: Mapping[str, Any] | None, overrides: Mapping[str, Any] | None = None) -> Config:
    data = apply_overrides(dict(raw or {}), overrides)
    try:
        return Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(_format_errors(e)) from None


def load_config(path: Path | None, overrides: Mapping[str, Any] | None = None) -> Config:
    """Load project.yaml (or defaults when path is None) and apply overrides."""
    raw: Any = {}
    if path is not None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"cannot read config file {path}: {e.strerror or e}") from None
        try:
            raw = yaml.safe_load(text) or {}
        except yaml.YAMLError as e:
            raise ConfigError(f"config file {path} is not valid YAML: {e}") from None
        if not isinstance(raw, dict):
            raise ConfigError(f"config file {path} must contain a mapping at top level")
    return parse_config(raw, overrides)


@dataclass(frozen=True)
class ResolvedPaths:
    media_root: Path
    workspace: Path
    outputs: dict[str, Path]


def resolve_paths(config: Config, media_root: Path | str, workspace: Path | str) -> ResolvedPaths:
    """Validate media root / workspace / outputs and return resolved absolute paths."""
    media = Path(media_root).expanduser()
    if not media.exists():
        raise ConfigError(f"media folder does not exist: {media}")
    if not media.is_dir():
        raise ConfigError(f"media path is not a directory: {media}")
    media = media.resolve()
    ws_in = Path(workspace).expanduser()
    if ws_in.exists() and not ws_in.is_dir():
        raise ConfigError(f"output workspace is not a directory: {ws_in}")
    ws = ws_in.resolve()
    if is_within(ws, media):
        raise ConfigError(f"output workspace {ws} must not be inside the media folder {media} (originals stay untouched)")
    if is_within(media, ws):
        raise ConfigError(f"media folder {media} must not be inside the output workspace {ws}")
    outs: dict[str, Path] = {}
    for name, rel in config.outputs.model_dump().items():
        target = (ws / rel).resolve()
        if not is_within(target, ws) or target == ws:
            raise ConfigError(f"outputs.{name}={rel!r} resolves outside the workspace")
        if is_within(target, media):
            raise ConfigError(f"outputs.{name}={rel!r} resolves inside the media folder")
        outs[name] = target
    return ResolvedPaths(media_root=media, workspace=ws, outputs=outs)
