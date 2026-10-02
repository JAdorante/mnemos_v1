"""Canonical JSON + hashing shared by the node and the Org Record Service.

stdlib only — the Org Record Service imports this module, so it must never
pull in app config, the store, or anything heavy.

The canonical form is a property of the VALUE, not of the text it arrived in:

  * keys sorted, compact separators, UTF-8 (no ASCII escaping)
  * strings NFC-normalized, so a composed and a decomposed "é" hash alike
  * numbers: an integral float is written as an int (1.0 == 1 == 1.00), any
    other float as its shortest round-trip repr; NaN and +-Infinity are
    refused, because JSON has no spelling for them and a hash over one would
    not survive a round trip through another language
  * bool stays bool (True is not 1)

`canonical_json` of `json.loads(canonical_json(x))` is a fixed point; the
property tests in tests/test_records_canonical.py pin that.
"""
from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from typing import Any


class CanonicalError(ValueError):
    """The value has no canonical JSON form (NaN, Infinity, a non-JSON type)."""


def _norm(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalError("NaN/Infinity has no canonical JSON form")
        if value.is_integer() and abs(value) < 2 ** 53:
            return int(value)
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalError(f"non-string key {k!r}")
            nk = unicodedata.normalize("NFC", k)
            if nk in out:
                raise CanonicalError(f"duplicate key after NFC: {nk!r}")
            out[nk] = _norm(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_norm(v) for v in value]
    raise CanonicalError(f"type {type(value).__name__} is not JSON")


def canonical_json(value: Any) -> str:
    """The one canonical serialization. Raises CanonicalError when none exists."""
    return json.dumps(_norm(value), sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def canonical_hash(value: Any) -> str:
    """sha256 hex of canonical_json(value)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def quote_hash(quote: str) -> str:
    """Hash of a verbatim evidence quote — what travels when the quote doesn't.

    Whitespace runs collapse and the text is NFC-normalized first, so the same
    words re-transcribed with different spacing still match."""
    text = " ".join(unicodedata.normalize("NFC", quote or "").split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def claim_identity_hash(kind: str, subject_ref: str, predicate: str,
                        value: Any) -> str:
    """claims.canonical_hash: sha256 over (kind, subject, predicate, value)."""
    return canonical_hash({"kind": kind, "subject_ref": subject_ref,
                           "predicate": predicate, "value": value})


GENESIS_HASH = "0" * 64


def audit_entry_hash(prev_hash: str, seq: int, actor: str, action: str,
                     object_ref: str, payload_hash: str, at: float) -> str:
    """entry_hash = sha256(prev_hash || seq || actor || action || object_ref ||
    payload_hash || at), fields separated by 0x1f so no concatenation of two
    fields can collide with another split. `at` is written as repr(float) so
    the node (SQLite REAL) and the service (Postgres double) agree exactly."""
    h = hashlib.sha256()
    for part in (prev_hash, str(int(seq)), actor, action, object_ref,
                 payload_hash, repr(float(at))):
        h.update(part.encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()
