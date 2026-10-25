"""Provider-neutral LLM gateway (DESIGN §9, §22). The Anthropic SDK is an optional, lazily imported dependency."""

from trip_vlog.llm.factory import ENV_MODE, ENV_MODEL, make_gateway, resolve_mode, resolve_model
from trip_vlog.llm.gateway import Gateway, Provider, fingerprint_of, request_summary
from trip_vlog.llm.replay import ReplayProvider
from trip_vlog.llm.store import ResponseStore
from trip_vlog.llm.types import *  # noqa: F401,F403
from trip_vlog.llm.untrusted import untrusted_block, untrusted_section

__all__ = [
    "ENV_MODE", "ENV_MODEL", "Gateway", "Provider", "ReplayProvider", "ResponseStore", "fingerprint_of", "make_gateway",
    "request_summary", "resolve_mode", "resolve_model", "untrusted_block", "untrusted_section",
]
