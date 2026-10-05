"""Deterministic predicate -> external field mapping (connector_mappings rows).

A mapping row: (kind, predicate) -> op, object_type, field, value_path,
transform. `predicate` may be "*" to match any predicate of that kind;
`field` may be "$field" for field_update claims, which name their own target
field (the spec's "field_update must name the target field").

Transforms are a closed list — no expressions, no templates beyond the fixed
renderings below — so the same record and the same row always yield the same
bytes:

  identity   value as-is
  string     str(value); integral floats lose the ".0"
  lower / upper
  number     float -> shortest decimal string
  date_ms    YYYY-MM-DD -> epoch ms at UTC midnight (HubSpot date properties)
  enum:<json object>   lookup; a value not in the table is a mapping error
"""
from __future__ import annotations

import calendar
import json
import time
from typing import Any

from org_coordinator.connectors.base import PermanentError

TRANSFORMS = ("identity", "string", "lower", "upper", "number", "date_ms")


class MappingError(PermanentError):
    pass


def find_mapping(mappings: list[dict], kind: str,
                 predicate: str) -> dict | None:
    exact = [m for m in mappings
             if m["kind"] == kind and m["predicate"] == predicate]
    if exact:
        return exact[0]
    wild = [m for m in mappings if m["kind"] == kind and m["predicate"] == "*"]
    return wild[0] if wild else None


def validate_row(row: dict) -> None:
    t = row.get("transform") or "identity"
    if t.startswith("enum:"):
        try:
            table = json.loads(t[5:])
        except ValueError as exc:
            raise MappingError("enum transform must be a JSON object") from exc
        if not isinstance(table, dict):
            raise MappingError("enum transform must be a JSON object")
    elif t not in TRANSFORMS:
        raise MappingError(f"unknown transform {t!r}")
    path = row.get("value_path") or "value"
    if path != "value" and not path.startswith("value."):
        raise MappingError("value_path must be 'value' or 'value.<key>'")


def extract(value: Any, path: str) -> Any:
    if path == "value":
        return value
    cur = value
    for part in path.split(".")[1:]:
        if not isinstance(cur, dict) or part not in cur:
            raise MappingError(f"value has no {path!r}")
        cur = cur[part]
    return cur


def _num_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return repr(v) if isinstance(v, float) else str(v)


def apply_transform(transform: str, v: Any) -> Any:
    if transform == "identity":
        return v
    if transform == "string":
        return "" if v is None else _num_str(v)
    if transform == "lower":
        return str(v).lower()
    if transform == "upper":
        return str(v).upper()
    if transform == "number":
        try:
            return _num_str(float(v))
        except (TypeError, ValueError) as exc:
            raise MappingError(f"not a number: {v!r}") from exc
    if transform == "date_ms":
        try:
            t = time.strptime(str(v)[:10], "%Y-%m-%d")
        except ValueError as exc:
            raise MappingError(f"not a date: {v!r}") from exc
        return str(calendar.timegm(t) * 1000)
    if transform.startswith("enum:"):
        table = json.loads(transform[5:])
        if str(v) not in table:
            raise MappingError(f"{v!r} not in enum mapping")
        return table[str(v)]
    raise MappingError(f"unknown transform {transform!r}")


def render_text(version: dict) -> str:
    """One fixed human rendering of a record value (notes, log entries)."""
    v = version.get("value") or {}
    kind = version.get("kind")
    label = version.get("subject_label") or version.get("subject_ref") or ""
    if kind == "commitment":
        s = f"{v.get('owner') or label} owes: {v.get('text', '')}"
        if v.get("counterparty"):
            s += f" → {v['counterparty']}"
        if v.get("due"):
            s += f" (due {v['due']})"
        return s
    if kind == "decision":
        return f"Decided: {v.get('text', '')}"
    if kind == "status":
        s = f"{label} {version.get('predicate', '')}: {v.get('state', '')}"
        return s + (f" — {v['text']}" if v.get("text") else "")
    if kind == "field_update":
        return f"{label} {v.get('field', '')} = {_num_str(v.get('value'))}"
    if kind == "fact":
        s = f"{label} {version.get('predicate', '')} {v.get('value', '')}".strip()
        return s + (f" — {v['text']}" if v.get("text") else "")
    return json.dumps(v, sort_keys=True, ensure_ascii=False)


def field_and_value(version: dict, mapping: dict) -> tuple[str, Any]:
    """(external field name, transformed value) for a set_property mapping."""
    field = mapping.get("field") or ""
    value = version.get("value") or {}
    if field == "$field":
        field = str(value.get("field") or "")
        raw = value.get("value")
    else:
        raw = extract(value, mapping.get("value_path") or "value")
    if not field:
        raise MappingError("mapping names no field")
    return field, apply_transform(mapping.get("transform") or "identity", raw)


def marker(version: dict) -> str:
    """Idempotency marker carried in created text, known at preview time (the
    payload hash is not — the preview is part of what gets hashed)."""
    return f"sparrow-{version.get('packet_id') or version.get('record_version_id')}"
