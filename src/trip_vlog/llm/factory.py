"""Provider selection: `make_gateway(config, workspace, purpose)` (DESIGN §4.1, §22)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, Mapping

from trip_vlog.config import CLOUD_PROVIDERS, Config
from trip_vlog.llm.anthropic_provider import AnthropicProvider
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.replay import LLM_MODES, ReplayProvider
from trip_vlog.llm.store import ResponseStore
from trip_vlog.llm.types import LLMConfigError
from trip_vlog.logging_utils import RunLogger

ENV_MODEL = "TRIP_VLOG_LLM_MODEL"
ENV_MODE = "TRIP_VLOG_LLM_MODE"
ENV_FIXTURE_DIR = "TRIP_VLOG_LLM_FIXTURE_DIR"


def resolve_model(config: Config, env: Mapping[str, str] | None = None) -> str | None:
    """Model id precedence: `media.llm_model` if set, else env TRIP_VLOG_LLM_MODEL, else None. No built-in default."""
    if config.media.llm_model:
        return config.media.llm_model
    env = os.environ if env is None else env
    return (env.get(ENV_MODEL) or "").strip() or None


def resolve_mode(provider_name: str, mode: str | None, env: Mapping[str, str] | None = None) -> str:
    """`replay` provider -> replay. Otherwise explicit `mode`, else env TRIP_VLOG_LLM_MODE, else `off`."""
    env = os.environ if env is None else env
    if provider_name == "replay":
        if mode == "record" or (mode is None and env.get(ENV_MODE, "").strip() == "record"):
            raise LLMConfigError("record mode needs a real provider; set media.llm_provider/vision_provider to a real one, not 'replay'")
        return "replay"
    chosen = (mode or env.get(ENV_MODE, "") or "off").strip()
    if chosen not in LLM_MODES:
        raise LLMConfigError(f"{ENV_MODE} must be one of {', '.join(LLM_MODES)}, got {chosen!r}")
    return chosen


def _dir(value: str, workspace: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else workspace / p


def make_gateway(
    config: Config,
    workspace: Path | str,
    purpose: Literal["vision", "text"] = "vision",
    *,
    mode: str | None = None,
    env: Mapping[str, str] | None = None,
    logger: RunLogger | None = None,
    sdk: Any = None,
    client: Any = None,
) -> Gateway:
    """Build the gateway for `purpose`: media.vision_provider for "vision", media.llm_provider for "text".

    `sdk`/`client` are injection points for tests. The anthropic SDK is imported lazily.
    """
    if purpose not in ("vision", "text"):
        raise ValueError("purpose must be 'vision' or 'text'")
    env = os.environ if env is None else env
    media = config.media
    workspace = Path(workspace)
    name = media.vision_provider if purpose == "vision" else media.llm_provider
    resolved_mode = resolve_mode(name, mode, env)
    if name not in CLOUD_PROVIDERS | {"replay"}:
        raise LLMConfigError(f"unknown LLM provider {name!r} (available: anthropic, replay)")
    if media.local_only and name in CLOUD_PROVIDERS:
        raise LLMConfigError(f"media.local_only is true but the {purpose} provider {name!r} is a cloud provider")

    cache_dir = _dir(media.llm_cache_dir, workspace)
    fixture_value = media.llm_fixture_dir or env.get(ENV_FIXTURE_DIR) or None
    fixture_dir = _dir(fixture_value, workspace) if fixture_value else cache_dir
    model = resolve_model(config, env)
    common: dict[str, Any] = dict(
        schema_retries=media.llm_schema_retries, image_max_side=media.llm_image_max_side,
        image_quality=media.llm_image_quality, image_cache_dir=cache_dir / "images", logger=logger,
    )
    if resolved_mode == "replay":
        return Gateway(ReplayProvider(ResponseStore(fixture_dir), model=model or ""), **common)
    if name != "anthropic":  # pragma: no cover - guarded above
        raise LLMConfigError(f"provider {name!r} cannot run in mode {resolved_mode!r}")
    if not model:
        raise LLMConfigError(
            f"no LLM model configured: set media.llm_model in project.yaml or the {ENV_MODEL} environment variable "
            "(there is no built-in default)")
    provider = AnthropicProvider(model, client=client, sdk=sdk, max_retries=media.llm_max_retries,
                                 timeout_seconds=media.llm_timeout_seconds)
    recorder = ResponseStore(fixture_dir) if resolved_mode == "record" else None
    return Gateway(provider, cache=ResponseStore(cache_dir), recorder=recorder, **common)
