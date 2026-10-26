"""Gateway construction from the `llm:` config section: `make_gateway(config, workspace, purpose)` (DESIGN §4.1, §22)."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Callable, Literal, Mapping

from trip_vlog.config import Config, EndpointConfig
from trip_vlog.llm.adapters import fingerprint_extra
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.http_provider import HttpProvider
from trip_vlog.llm.replay import LLM_MODES, ReplayProvider
from trip_vlog.llm.store import ResponseStore
from trip_vlog.llm.transport import Transport
from trip_vlog.llm.types import LLMConfigError
from trip_vlog.logging_utils import RunLogger

ENV_MODE = "TRIP_VLOG_LLM_MODE"
ENV_FIXTURE_DIR = "TRIP_VLOG_LLM_FIXTURE_DIR"


def resolve_model(endpoint: EndpointConfig, env: Mapping[str, str] | None = None) -> str | None:
    """Model id: `model` in the profile if set, else the env var named by `model_env`, else None. No built-in default."""
    if endpoint.model:
        return endpoint.model
    env = os.environ if env is None else env
    return ((env.get(endpoint.model_env) or "").strip() or None) if endpoint.model_env else None


def resolve_mode(config: Config, env: Mapping[str, str] | None = None) -> str:
    """env TRIP_VLOG_LLM_MODE (live|record|replay) overrides `llm.mode`."""
    env = os.environ if env is None else env
    mode = (env.get(ENV_MODE) or "").strip() or config.llm.mode
    if mode not in LLM_MODES:
        raise LLMConfigError(f"{ENV_MODE} / llm.mode must be one of {', '.join(LLM_MODES)}, got {mode!r}")
    return mode


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
    transport: Transport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Gateway:
    """Gateway for `purpose`, using the endpoint profile named by llm.vision / llm.text.

    live: real calls (+ workspace response cache). record: like live, and every validated response is also saved to the
    fixture dir. replay: stored responses only, no network, no key needed. `transport`/`sleep` are test injection points.
    """
    if purpose not in ("vision", "text"):
        raise ValueError("purpose must be 'vision' or 'text'")
    env = os.environ if env is None else env
    llm = config.llm
    workspace = Path(workspace)
    resolved = mode or resolve_mode(config, env)
    if resolved not in LLM_MODES:
        raise LLMConfigError(f"mode must be one of {', '.join(LLM_MODES)}, got {resolved!r}")
    profile, ep = llm.endpoint_for(purpose)
    if config.media.local_only and not ep.is_local:  # defence in depth: Config already validates this at load time
        raise LLMConfigError(f"media.local_only is true but endpoint {profile!r} ({ep.host}) is not local")

    cache_dir = _dir(llm.cache_dir, workspace)
    fixture_value = llm.fixture_dir or env.get(ENV_FIXTURE_DIR) or None
    fixture_dir = _dir(fixture_value, workspace) if fixture_value else cache_dir
    model = resolve_model(ep, env)
    common = dict(schema_retries=llm.schema_retries, image_max_side=llm.image_max_side, image_quality=llm.image_quality,
                  image_cache_dir=cache_dir / "images", logger=logger)
    if resolved == "replay":
        provider = ReplayProvider(ResponseStore(fixture_dir), model=model or "", endpoint=profile,
                                  fingerprint_extra=fingerprint_extra(ep))
        return Gateway(provider, **common)  # type: ignore[arg-type]
    if not model:
        raise LLMConfigError(
            f"no model configured for endpoint {profile!r}: set llm.endpoints.{profile}.model"
            + (f" or the {ep.model_env} environment variable" if ep.model_env else "") + " (there is no built-in default)")
    provider = HttpProvider(profile, ep, model=model, env=env, transport=transport, sleep=sleep)
    recorder = ResponseStore(fixture_dir) if resolved == "record" else None
    return Gateway(provider, cache=ResponseStore(cache_dir), recorder=recorder, **common)  # type: ignore[arg-type]
