"""Typed claim kinds — load app/schemas/claims/<kind>.json and validate values.

The validator covers the JSON Schema subset those files use (type, required,
properties, additionalProperties, enum, minLength, maxLength, pattern). It is
deliberately small: nodes take no new dependencies, and a schema that needs a
keyword this does not know fails loudly at load time instead of validating
nothing.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

KINDS = ("decision", "commitment", "status", "field_update", "fact")

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas" / "claims"
_KNOWN_KEYWORDS = {
    "$id", "version", "description", "type", "properties", "required",
    "additionalProperties", "enum", "minLength", "maxLength", "pattern",
}
_TYPES = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "null": lambda v: v is None,
}


class SchemaError(ValueError):
    pass


def _check_keywords(schema: dict, where: str) -> None:
    unknown = set(schema) - _KNOWN_KEYWORDS
    if unknown:
        raise SchemaError(f"{where}: unsupported keywords {sorted(unknown)}")
    for name, sub in (schema.get("properties") or {}).items():
        _check_keywords(sub, f"{where}.{name}")


@lru_cache(maxsize=None)
def schema_for(kind: str) -> dict:
    if kind not in KINDS:
        raise SchemaError(f"unknown claim kind {kind!r}")
    schema = json.loads((_SCHEMA_DIR / f"{kind}.json").read_text("utf-8"))
    _check_keywords(schema, kind)
    return schema


def schema_version(kind: str) -> str:
    return str(schema_for(kind).get("version") or f"{kind}-v1")


def _errors(value: Any, schema: dict, path: str) -> list[str]:
    out: list[str] = []
    types = schema.get("type")
    if types is not None:
        allowed = types if isinstance(types, list) else [types]
        if not any(_TYPES[t](value) for t in allowed):
            return [f"{path}: expected {'|'.join(allowed)}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: not one of {schema['enum']}")
    if isinstance(value, str):
        if len(value) < int(schema.get("minLength", 0)):
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > int(schema["maxLength"]):
            out.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: does not match {schema['pattern']}")
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for req in schema.get("required") or []:
            if req not in value:
                out.append(f"{path}.{req}: required")
        if schema.get("additionalProperties") is False:
            for extra in sorted(set(value) - set(props)):
                out.append(f"{path}.{extra}: not allowed")
        for name, sub in props.items():
            if name in value:
                out.extend(_errors(value[name], sub, f"{path}.{name}"))
    return out


def validate(kind: str, value: Any) -> list[str]:
    """Empty list when `value` is a valid payload for `kind`."""
    return _errors(value, schema_for(kind), "value")
