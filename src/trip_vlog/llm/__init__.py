"""Provider-neutral LLM gateway (DESIGN §9, §22). The stdlib HTTP transport speaks Anthropic- and OpenAI-style APIs; no SDK dependency."""

from trip_vlog.llm.factory import ENV_FIXTURE_DIR, ENV_MODE, make_gateway, resolve_mode, resolve_model
from trip_vlog.llm.http_provider import HttpProvider
from trip_vlog.llm.gateway import Gateway, Provider, fingerprint_of, request_summary
from trip_vlog.llm.replay import ReplayProvider
from trip_vlog.llm.store import ResponseStore
from trip_vlog.llm.types import *  # noqa: F401,F403
from trip_vlog.llm.untrusted import untrusted_block, untrusted_section

__all__ = [
    "ENV_FIXTURE_DIR", "ENV_MODE", "HttpProvider", "Gateway", "Provider", "ReplayProvider", "ResponseStore", "fingerprint_of", "make_gateway",
    "request_summary", "resolve_mode", "resolve_model", "untrusted_block", "untrusted_section",
]
