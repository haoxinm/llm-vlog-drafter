"""`trip-vlog llm-check`: one tiny live request per endpoint (text, then image) to verify an adapter against a real server.

Everything here bypasses the response cache and never records into fixtures. The only data sent is a fixed prompt and a
generated solid-colour image; no media from the user's library is involved.
"""

from __future__ import annotations

import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from trip_vlog.config import Config, ConfigError
from trip_vlog.llm.factory import resolve_model
from trip_vlog.llm.gateway import Gateway
from trip_vlog.llm.http_provider import HttpProvider
from trip_vlog.llm.transport import Transport
from trip_vlog.llm.types import ImagePart, LLMError, LLMRequest, TextPart
from trip_vlog.logging_utils import sanitize

TEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"word": {"type": "string"}, "number": {"type": "integer"}},
    "required": ["word", "number"],
    "additionalProperties": False,
}
IMAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"dominant_colour": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["dominant_colour", "confidence"],
    "additionalProperties": False,
}
SYSTEM = "You are a connectivity test. Answer only with the requested JSON."
TEXT_PROMPT = 'Reply with a JSON object with the string field "word" set to "pong" and the integer field "number" set to 7.'
IMAGE_PROMPT = ('The image is a single flat colour. Reply with a JSON object: "dominant_colour" is the English name of that colour '
                'and "confidence" is a number between 0 and 1.')
RED_WORDS = ("red", "crimson", "scarlet", "vermilion", "rouge")
MAX_MESSAGE = 80


@dataclass
class CheckRow:
    endpoint: str
    test: str                      # "text" | "image"
    api: str = ""
    url: str = ""
    where: str = ""                # "local" | "remote"
    model: str = ""
    structured: str = ""
    status: str = "ok"             # "ok" | "fail" | "skipped"
    latency_ms: float | None = None
    tokens: str = ""
    error: str = ""                # "ClassName: short message"
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status != "fail"


def public_url(url: str) -> str:
    """scheme://host[:port]/path with no userinfo, query or fragment (secrets can hide there)."""
    p = urlsplit(url)
    host = p.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    return f"{p.scheme}://{host}{f':{p.port}' if p.port else ''}{p.path}"


def endpoints_to_check(config: Config, name: str | None) -> list[str]:
    if name is not None:
        if name not in config.llm.endpoints:
            raise ConfigError(f"unknown endpoint {name!r} (defined: {', '.join(sorted(config.llm.endpoints))})")
        return [name]
    out: list[str] = []
    for purpose in ("text", "vision"):
        n = getattr(config.llm, purpose)
        if n not in out:
            out.append(n)
    if config.qc.critic != "off":
        n = config.llm.endpoint_for("critic", images=config.qc.critic_images)[0]
        if n not in out:
            out.append(n)
    if config.llm.revise is not None:     # `trip-vlog revise` reads text only; without llm.revise it uses the text profile, already checked
        n = config.llm.endpoint_for("revise")[0]
        if n not in out:
            out.append(n)
    return out


def _short(text: object, env: Mapping[str, str]) -> str:
    one = " ".join(sanitize(text, env=env).split())
    return one if len(one) <= MAX_MESSAGE else one[:MAX_MESSAGE - 3] + "..."


def _test_image(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (128, 128), (220, 20, 30)).save(path, format="PNG")
    return path


def check_endpoint(config: Config, name: str, *, include_image: bool = True, transport: Transport | None = None,
                   env: Mapping[str, str] | None = None, sleep: Any = time.sleep) -> list[CheckRow]:
    env = os.environ if env is None else env
    ep = config.llm.endpoints[name]
    base = dict(endpoint=name, api=ep.api, url=public_url((ep.base_url or "").rstrip("/") + (ep.path or "")),
                where="local" if ep.is_local else "remote", structured=ep.structured_output)
    text_row = CheckRow(test="text", model=resolve_model(ep, env) or "", **base)
    rows = [text_row]
    image_row = CheckRow(test="image", model=text_row.model, **base) if include_image else None
    try:
        model = resolve_model(ep, env)
        if not model:
            raise ConfigError(f"no model configured: set llm.endpoints.{name}.model" + (f" or ${ep.model_env}" if ep.model_env else ""))
        provider = HttpProvider(name, ep, model=model, env=env, transport=transport, sleep=sleep)
        text_row.url = public_url(provider.url)
        # no cache, no recorder: a check must always reach the server and must never write fixtures
        gateway = Gateway(provider, schema_retries=config.llm.schema_retries, cache=None, recorder=None)
    except (LLMError, ConfigError) as e:
        text_row.status, text_row.error = "fail", f"{type(e).__name__}: {_short(e, env)}"
        if image_row is not None:
            image_row.status, image_row.note = "skipped", "endpoint could not be set up"
            rows.append(image_row)
        return rows

    def run(row: CheckRow, req: LLMRequest, expect: Any = None) -> None:
        t0 = time.perf_counter()
        try:
            resp = gateway.complete(req)
        except LLMError as e:
            row.status, row.latency_ms = "fail", (time.perf_counter() - t0) * 1000
            row.error = f"{type(e).__name__}: {_short(e, env)}"
            return
        row.latency_ms = resp.latency_ms
        row.tokens = f"{resp.usage.input_tokens}/{resp.usage.output_tokens}"
        if row.model and resp.model and resp.model != row.model:
            row.note = f"served by {resp.model}"
        if expect is not None:
            problem = expect(resp.parsed)
            if problem:
                row.status, row.error = "fail", f"UnexpectedAnswer: {problem}"

    run(text_row, LLMRequest("llm_check_text", "1", SYSTEM, (TextPart(TEXT_PROMPT),), TEXT_SCHEMA, 200))
    if image_row is not None:
        rows.append(image_row)
        if not ep.supports_images:
            image_row.status, image_row.note = "skipped", "supports_images: false"
        else:
            with tempfile.TemporaryDirectory(prefix="trip_vlog_check_") as td:
                img = _test_image(Path(td) / "red.png")
                req = LLMRequest("llm_check_image", "1", SYSTEM, (ImagePart(img, "image/png"), TextPart(IMAGE_PROMPT)), IMAGE_SCHEMA, 200)
                run(image_row, req, lambda p: None if any(w in str(p.get("dominant_colour", "")).lower() for w in RED_WORDS)
                    else f"the image is red but the model answered {str(p.get('dominant_colour'))[:30]!r} (does it receive the image?)")
    return rows


def run_llm_check(config: Config, *, endpoint: str | None = None, include_image: bool = True, transport: Transport | None = None,
                  env: Mapping[str, str] | None = None, sleep: Any = time.sleep) -> list[CheckRow]:
    rows: list[CheckRow] = []
    for name in endpoints_to_check(config, endpoint):
        rows.extend(check_endpoint(config, name, include_image=include_image, transport=transport, env=env, sleep=sleep))
    return rows


HEADERS = ("endpoint", "test", "api", "url", "where", "model", "structured", "result", "latency", "tokens (in/out)", "error")


def format_table(rows: list[CheckRow]) -> str:
    def cells(r: CheckRow) -> list[str]:
        result = {"ok": "ok", "fail": "FAIL", "skipped": "skipped"}[r.status]
        err = r.error or r.note
        return [r.endpoint, r.test, r.api, r.url, r.where, r.model or "-", r.structured, result,
                f"{r.latency_ms:.0f} ms" if r.latency_ms is not None else "-", r.tokens or "-", err or "-"]

    table = [list(HEADERS), *(cells(r) for r in rows)]
    widths = [max(len(row[i]) for row in table) for i in range(len(HEADERS))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in table]
    lines.insert(1, "  ".join("-" * w for w in widths))
    bad = sum(1 for r in rows if r.status == "fail")
    lines.append("")
    lines.append("all checks passed" if not bad else f"{bad} check(s) failed")
    return "\n".join(lines)
