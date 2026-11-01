"""JSON-schema helpers: client-side validation messages and the provider-safe schema copy."""

from __future__ import annotations

import copy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from trip_vlog.llm.types import LLMInputError

# Keywords structured-output backends reject or ignore: numeric/string/array size constraints, patterns,
# composition keywords they do not support. The gateway still validates the FULL schema client-side.
UNSUPPORTED_KEYWORDS = frozenset({
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
    "minLength", "maxLength", "pattern",
    "maxItems", "uniqueItems", "contains", "minContains", "maxContains",
    "minProperties", "maxProperties", "propertyNames", "patternProperties", "unevaluatedProperties", "unevaluatedItems",
    "not", "if", "then", "else", "dependentSchemas", "dependentRequired",
    "$schema", "$id", "$comment",
})
SUPPORTED_FORMATS = frozenset({"date-time", "time", "date", "duration", "email", "hostname", "uri", "ipv4", "ipv6", "uuid"})
_SCHEMA_VALUED = ("items", "contains")
_SCHEMA_MAPS = ("properties", "$defs", "definitions")
_SCHEMA_LISTS = ("anyOf", "allOf", "oneOf", "prefixItems")


def check_schema(schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as e:
        raise LLMInputError(f"output schema is not a valid JSON schema: {e.message}") from None


def validation_errors(instance: Any, schema: dict[str, Any], limit: int = 12) -> list[str]:
    """Human-readable, deterministic list of schema violations ('$.a.b: message'); empty when valid."""
    out = []
    for e in sorted(Draft202012Validator(schema).iter_errors(instance), key=lambda e: [str(p) for p in e.absolute_path]):
        path = "$" + "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in e.absolute_path)
        msg = e.message if len(e.message) <= 300 else e.message[:300] + "..."
        out.append(f"{path}: {msg}")
        if len(out) >= limit:
            break
    return out


def sanitize_schema_for_provider(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `schema` safe for provider-side structured output.

    Strips UNSUPPORTED_KEYWORDS and `minItems` other than 0/1, keeps only supported string formats, rewrites
    `oneOf` as `anyOf`, and forces `additionalProperties: false` on every object schema. The input is not mutated.
    """
    return _walk(copy.deepcopy(schema))


def _walk(node: Any) -> Any:
    if not isinstance(node, dict):
        return node
    for k in list(node):
        if k in UNSUPPORTED_KEYWORDS:
            del node[k]
    if "minItems" in node and node["minItems"] not in (0, 1):
        del node["minItems"]
    if "format" in node and node["format"] not in SUPPORTED_FORMATS:
        del node["format"]
    if "oneOf" in node:
        node.setdefault("anyOf", []).extend(node.pop("oneOf"))
    for k in _SCHEMA_VALUED:
        if isinstance(node.get(k), dict):
            node[k] = _walk(node[k])
    for k in _SCHEMA_MAPS:
        if isinstance(node.get(k), dict):
            node[k] = {name: _walk(sub) for name, sub in node[k].items()}
    for k in _SCHEMA_LISTS:
        if isinstance(node.get(k), list):
            node[k] = [_walk(sub) for sub in node[k]]
    t = node.get("type")
    if t == "object" or (isinstance(t, list) and "object" in t) or "properties" in node:
        node["additionalProperties"] = False
    return node
