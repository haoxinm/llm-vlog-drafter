"""Project configuration (DESIGN §4.1): pydantic models, loading, overrides, path safety."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from trip_vlog.paths import check_portable_component, is_within

LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


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


def _workspace_relative(v: str, what: str) -> str:
    norm = v.replace("\\", "/")
    p = PurePosixPath(norm)
    if not norm.strip() or p.is_absolute() or re.match(r"^[A-Za-z]:", norm) or norm.startswith("//"):
        raise ValueError(f"{what} must be a non-empty path relative to the workspace: {v!r}")
    parts = [x for x in p.parts if x != "."]
    if not parts or ".." in parts:
        raise ValueError(f"{what} must stay inside the workspace (no '..'): {v!r}")
    for part in parts:
        problem = check_portable_component(part)
        if problem:
            raise ValueError(f"{what} {v!r} {problem}")
    return "/".join(parts)


class MediaConfig(_Model):
    recursive: bool = True
    follow_symlinks: bool = False
    fingerprint: Literal["fast", "sha256"] = "fast"
    proxy_policy: Literal["on_demand", "always", "never"] = "on_demand"
    vision_sampling_fps: float = Field(1.0, gt=0, le=30)
    fine_sampling_fps: float = Field(4.0, gt=0, le=60)
    fine_sampling_enabled: bool = False
    segment_window_seconds: float = Field(6.0, gt=0, le=120)
    transcribe_speech: bool | None = None  # DEPRECATED alias of speech.enabled; folded into it at load time (see Config._speech_alias)
    local_only: bool = False       # true: every LLM endpoint used for text and vision must be loopback (see `llm.endpoints`)
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
    # Phase 3b vision labeling (the sampling fps itself is `vision_sampling_fps`)
    vision_max_frames_per_segment: int = Field(8, ge=1, le=64)  # frames beyond this are thinned evenly
    vision_input: Literal["frames", "contact_sheet"] = "frames"  # what the model receives per segment
    vision_batch_max_images: int = Field(16, ge=1, le=100)       # images per request (segments are never split across requests)
    vision_send_filenames: bool = True                          # send the file's base name (wrapped as untrusted data), never its folders
    photo_duplicate_distance: int = Field(6, ge=0, le=32)        # max Hamming distance of 64-bit dHashes to call two photos near-duplicates
    # Phase 3c audio features (loudness, clipping, wind-likelihood per asset/segment; see README "Speech transcription")
    audio_features: bool = True

    @model_validator(mode="after")
    def _check(self) -> "MediaConfig":
        if self.fine_sampling_enabled and self.fine_sampling_fps < self.vision_sampling_fps:
            raise ValueError("fine_sampling_fps must be >= vision_sampling_fps when fine sampling is enabled")
        return self


API_KINDS = ("anthropic_messages", "openai_chat", "openai_responses")
RESERVED_PARAMS = frozenset({"model", "messages", "input", "system", "instructions"})
_API_DEFAULTS = {  # api -> (base_url, api_key_env)
    "anthropic_messages": ("https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    "openai_chat": ("https://api.openai.com", "OPENAI_API_KEY"),
    "openai_responses": ("https://api.openai.com", "OPENAI_API_KEY"),
}
_ENV_NAME_RE = r"^[A-Za-z_][A-Za-z0-9_]*$"


def is_loopback_host(host: str | None) -> bool:
    """127.0.0.0/8, ::1 and 'localhost' (any case, optional trailing dot)."""
    if not host:
        return False
    h = host.strip("[]").rstrip(".").lower()
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


class EndpointConfig(_Model):
    """One LLM endpoint profile. Credentials are referenced by environment-variable NAME, never stored."""

    api: Literal["anthropic_messages", "openai_chat", "openai_responses"] = "anthropic_messages"
    base_url: str | None = None          # default depends on `api`
    path: str | None = None              # null -> the adapter's default path
    model: str | None = None
    model_env: str | None = Field("TRIP_VLOG_LLM_MODEL", pattern=_ENV_NAME_RE)
    api_key_env: str | None = Field(None, pattern=_ENV_NAME_RE)   # unset -> default per api; null -> no auth (local servers)
    headers: dict[str, str] = {}         # values may reference env vars as ${VAR}; values are never logged
    timeout_seconds: float = Field(120.0, gt=0, le=3600)
    max_retries: int = Field(2, ge=0, le=10)                      # transport retries (429/5xx/connection)
    structured_output: Literal["native", "json_prompt", "none"] = "native"
    max_tokens: int = Field(4096, ge=1)                           # upper bound on a request's max_tokens
    max_tokens_field: str | None = None
    supports_images: bool = True
    params: dict[str, Any] = {}          # merged into the request body (gateway-controlled fields win)
    allow_insecure_http: bool = False

    @model_validator(mode="before")
    @classmethod
    def _no_literal_keys(cls, data: Any) -> Any:
        if isinstance(data, dict):
            for k in data:
                if str(k).lower() in ("api_key", "apikey", "key", "token", "authorization"):
                    raise ValueError(f"never put a credential in the config: remove {k!r} and set api_key_env to the NAME of an environment variable")
        return data

    @field_validator("model")
    @classmethod
    def _model_not_blank(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("model must not be blank (use null to fall back to model_env)")
        return v.strip() if v is not None else v

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip() or "://" in v or any(c in v for c in " \t\r\n"):
            raise ValueError("path must be a URL path such as '/v1/chat/completions', not a full URL")
        return v if v.startswith("/") else "/" + v

    @field_validator("headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        for k in v:
            if not re.fullmatch(r"[A-Za-z0-9-]+", k):
                raise ValueError(f"invalid header name {k!r}")
        return v

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = sorted(RESERVED_PARAMS & set(v))
        if bad:
            raise ValueError(f"params must not set reserved request fields: {', '.join(bad)}")
        return v

    @model_validator(mode="after")
    def _finish(self) -> "EndpointConfig":
        default_url, default_key = _API_DEFAULTS[self.api]
        if "base_url" not in self.model_fields_set or self.base_url is None:
            self.__dict__["base_url"] = default_url
        if "api_key_env" not in self.model_fields_set:
            self.__dict__["api_key_env"] = default_key
        parts = urlsplit(self.base_url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"base_url must be an http(s) URL with a host, got {self.base_url!r}")
        if parts.username or parts.password:
            raise ValueError("base_url must not contain credentials")
        if parts.scheme == "http" and not self.is_local and not self.allow_insecure_http:
            raise ValueError(f"base_url {self.base_url!r} uses plain http to a non-loopback host; use https or set allow_insecure_http: true")
        return self

    @property
    def host(self) -> str:
        return urlsplit(self.base_url or "").hostname or ""

    @property
    def is_local(self) -> bool:
        return is_loopback_host(self.host)


class LLMConfig(_Model):
    mode: Literal["live", "record", "replay"] = "live"   # env TRIP_VLOG_LLM_MODE overrides
    text: str = "main"      # endpoint profile for text/planning calls
    vision: str = "main"    # endpoint profile for vision calls
    endpoints: dict[str, EndpointConfig] = Field(default_factory=lambda: {"main": EndpointConfig()})
    schema_retries: int = Field(2, ge=0, le=5)           # re-asks with validation errors after a schema failure
    image_max_side: int = Field(1568, ge=256, le=8000)   # longest side of frames sent to a model
    image_quality: int = Field(85, ge=30, le=100)
    cache_dir: str = "cache/llm"                         # response cache, relative to the workspace, must stay inside it
    fixture_dir: str | None = None                       # record/replay store; default cache_dir (env TRIP_VLOG_LLM_FIXTURE_DIR)

    @field_validator("cache_dir")
    @classmethod
    def _cache_dir_inside_workspace(cls, v: str) -> str:
        return _workspace_relative(v, "cache_dir")

    @model_validator(mode="after")
    def _profiles_exist(self) -> "LLMConfig":
        for purpose in ("text", "vision"):
            name = getattr(self, purpose)
            if name not in self.endpoints:
                raise ValueError(f"llm.{purpose} refers to unknown endpoint profile {name!r} (defined: {', '.join(sorted(self.endpoints)) or 'none'})")
        return self

    def endpoint_for(self, purpose: Literal["text", "vision"]) -> tuple[str, EndpointConfig]:
        name = self.text if purpose == "text" else self.vision
        return name, self.endpoints[name]


SPEECH_BACKENDS = ("none", "openai_transcriptions", "command")
SPEECH_RESPONSE_FORMATS = ("verbose_json", "json", "text", "srt", "vtt")
SPEECH_RESERVED_PARAMS = frozenset({"file", "model", "language", "response_format", "timestamp_granularities[]", "timestamp_granularities"})
DEFAULT_SPEECH_PATH = "/v1/audio/transcriptions"


class SpeechEndpointConfig(_Model):
    """HTTP transcription endpoint (multipart upload, OpenAI-style `/v1/audio/transcriptions`). Credentials by env-var NAME only.

    The request shape follows the public OpenAI documentation and is what OpenAI-compatible servers (faster-whisper-server,
    whisper.cpp server, LocalAI, ...) implement; verify it against your server with `trip-vlog llm-check --speech`.
    """

    base_url: str = "https://api.openai.com"
    path: str | None = None              # null -> /v1/audio/transcriptions
    model: str | None = None
    model_env: str | None = Field("TRIP_VLOG_SPEECH_MODEL", pattern=_ENV_NAME_RE)
    api_key_env: str | None = Field("OPENAI_API_KEY", pattern=_ENV_NAME_RE)   # null -> no auth (local servers)
    headers: dict[str, str] = {}         # values may reference env vars as ${VAR}; never logged
    timeout_seconds: float = Field(300.0, gt=0, le=7200)
    max_retries: int = Field(2, ge=0, le=10)
    response_format: Literal["verbose_json", "json", "text", "srt", "vtt"] = "verbose_json"
    timestamp_granularities: list[Literal["segment", "word"]] = ["segment"]  # sent only with verbose_json; [] omits the field
    params: dict[str, Any] = {}          # extra multipart fields (e.g. temperature, prompt); scalars or lists
    allow_insecure_http: bool = False

    @model_validator(mode="before")
    @classmethod
    def _no_literal_keys(cls, data: Any) -> Any:
        return EndpointConfig._no_literal_keys.__func__(cls, data)  # type: ignore[attr-defined]

    @field_validator("model")
    @classmethod
    def _model_not_blank(cls, v: str | None) -> str | None:
        return EndpointConfig._model_not_blank.__func__(cls, v)  # type: ignore[attr-defined]

    @field_validator("path")
    @classmethod
    def _path(cls, v: str | None) -> str | None:
        return EndpointConfig._path.__func__(cls, v)  # type: ignore[attr-defined]

    @field_validator("headers")
    @classmethod
    def _headers(cls, v: dict[str, str]) -> dict[str, str]:
        return EndpointConfig._headers.__func__(cls, v)  # type: ignore[attr-defined]

    @field_validator("params")
    @classmethod
    def _params(cls, v: dict[str, Any]) -> dict[str, Any]:
        bad = sorted(SPEECH_RESERVED_PARAMS & set(v))
        if bad:
            raise ValueError(f"params must not set fields that have their own option: {', '.join(bad)}")
        for k, x in v.items():
            if not isinstance(k, str) or not k or any(c in k for c in '"\r\n'):
                raise ValueError(f"invalid parameter name {k!r}")
            if x is None or isinstance(x, (dict, set)):
                raise ValueError(f"params.{k} must be a scalar or a list of scalars")
        return v

    @model_validator(mode="after")
    def _finish(self) -> "SpeechEndpointConfig":
        parts = urlsplit(self.base_url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"base_url must be an http(s) URL with a host, got {self.base_url!r}")
        if parts.username or parts.password:
            raise ValueError("base_url must not contain credentials")
        if parts.scheme == "http" and not self.is_local and not self.allow_insecure_http:
            raise ValueError(f"base_url {self.base_url!r} uses plain http to a non-loopback host; use https or set allow_insecure_http: true")
        return self

    @property
    def host(self) -> str:
        return urlsplit(self.base_url or "").hostname or ""

    @property
    def is_local(self) -> bool:
        return is_loopback_host(self.host)

    @property
    def url(self) -> str:
        return self.base_url.rstrip("/") + (self.path or DEFAULT_SPEECH_PATH)


class SpeechCommandConfig(_Model):
    """A local executable that writes a transcript file. No shell is involved: `args` is a list, placeholders are substituted
    inside each element: {input} (audio file), {output_dir} (empty temp dir), {output_base} (= {output_dir}/transcript, for tools
    that take a prefix without extension) and {language} (the configured language or "auto")."""

    executable: str | None = None
    args: list[str] = []
    output_format: Literal["srt", "vtt", "json"] = "srt"
    timeout_seconds: float = Field(3600.0, gt=0, le=86400)

    @model_validator(mode="after")
    def _placeholders(self) -> "SpeechCommandConfig":
        if self.args:
            joined = "\n".join(self.args)
            if "{input}" not in joined:
                raise ValueError("args must contain the {input} placeholder")
            if "{output_dir}" not in joined and "{output_base}" not in joined:
                raise ValueError("args must contain {output_dir} (or {output_base}) so the transcript file can be found")
        return self


class SpeechConfig(_Model):
    """Optional speech transcription (DESIGN §7.3, §22). Off in effect until a backend is chosen: the default backend is `none`."""

    enabled: bool = True                 # single source of truth; media.transcribe_speech is a deprecated alias
    backend: Literal["none", "openai_transcriptions", "command"] = "none"
    language: str | None = Field(None, pattern=r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")   # null -> let the backend detect it
    endpoint: SpeechEndpointConfig = Field(default_factory=SpeechEndpointConfig)
    command: SpeechCommandConfig = Field(default_factory=SpeechCommandConfig)
    min_speech_seconds: float = Field(1.0, ge=0, le=3600)   # skip assets with less non-silent audio than this
    audio_format: Literal["wav", "flac"] = "wav"            # temporary 16 kHz mono file handed to the backend
    keep_audio: bool = False                                # keep the extracted files under media_index/audio/ (else deleted after use)
    chunk_seconds: float = Field(600.0, ge=30, le=3600)     # HTTP backend: longer audio is uploaded in chunks of this length
    context_max_chars: int = Field(1200, ge=0, le=20000)    # transcript text per segment shown to the vision model (0 disables)

    @model_validator(mode="after")
    def _backend_ready(self) -> "SpeechConfig":
        if self.backend == "command" and not (self.command.executable or "").strip():
            raise ValueError("speech.backend is 'command' but speech.command.executable is not set")
        if self.backend == "command" and not self.command.args:
            raise ValueError("speech.backend is 'command' but speech.command.args is empty (it must contain {input} and {output_dir})")
        return self

    @property
    def active(self) -> bool:
        return self.enabled and self.backend != "none"

    @property
    def is_local(self) -> bool:
        return self.backend != "openai_transcriptions" or self.endpoint.is_local


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
    llm: LLMConfig = Field(default_factory=LLMConfig)
    speech: SpeechConfig = Field(default_factory=SpeechConfig)
    workflow: WorkflowConfig = WorkflowConfig()
    filmcraft: FilmCraftConfig = FilmCraftConfig()
    qc: QcConfig = QcConfig()
    outputs: OutputsConfig = OutputsConfig()

    @model_validator(mode="before")
    @classmethod
    def _legacy_media_keys(cls, data: Any) -> Any:
        media = data.get("media") if isinstance(data, dict) else None
        if isinstance(media, dict):
            old = sorted(k for k in ("llm_provider", "vision_provider", "llm_model") if k in media)
            if old:
                raise ValueError(
                    f"media.{', media.'.join(old)} no longer exist: configure an endpoint profile under `llm:` "
                    "(llm.endpoints.<name>.api/base_url/model, selected by llm.text and llm.vision). See README \"LLM provider\".")
        return data

    @model_validator(mode="after")
    def _speech_alias(self) -> "Config":
        """`media.transcribe_speech` (older name) is folded into `speech.enabled`, which is the single source of truth.
        Both set to different values is an error; afterwards the alias is cleared so the effective config shows one value."""
        alias = self.media.transcribe_speech
        if alias is not None:
            if "enabled" in self.speech.model_fields_set and self.speech.enabled != alias:
                raise ValueError(
                    f"media.transcribe_speech ({str(alias).lower()}) conflicts with speech.enabled ({str(self.speech.enabled).lower()}); "
                    "media.transcribe_speech is deprecated: remove it and use speech.enabled only")
            self.speech.__dict__["enabled"] = alias
            self.speech.__dict__["__pydantic_fields_set__"] = self.speech.model_fields_set | {"enabled"}
            self.media.__dict__["transcribe_speech"] = None
        return self

    @model_validator(mode="after")
    def _local_only(self) -> "Config":
        if self.media.local_only and self.speech.active and not self.speech.is_local:
            raise ValueError(
                f"media.local_only is true but speech.endpoint host {self.speech.endpoint.host!r} is not a loopback address "
                "(127.x, ::1, localhost); point speech.endpoint at a local server, use speech.backend: command, or turn local_only off")
        if self.media.local_only:
            for purpose in ("text", "vision"):
                name, ep = self.llm.endpoint_for(purpose)
                if not ep.is_local:
                    raise ValueError(
                        f"media.local_only is true but llm.{purpose} uses endpoint {name!r} whose host {ep.host!r} is not a loopback address "
                        "(127.x, ::1, localhost); point it at a local server or turn local_only off")
        return self

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
