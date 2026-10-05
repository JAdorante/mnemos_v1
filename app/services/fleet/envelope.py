"""The one signal envelope the fleet, Sparrow, and the relay all validate.

Stdlib only and free of app imports: org_coordinator/ imports this module
directly so the relay checks exactly the shape Sparrow checks.

A signal is domain-neutral: a `kind`, an optional `subject` it is about, a
human-readable `summary`, an optional `confidence`, and a structured `body`
whose shape the kind declares. Kinds live in a registry (fleet_kinds.json)
that Sparrow and the relay both load; a signal of an unknown kind is refused.
The built-in `note` kind (no body) is always available.

A kind definition:

    {"description": "A project's status, as one agent sees it",
     "subject": "required" | "optional" | "forbidden",
     "subject_pattern": "^[A-Za-z0-9 ._-]{1,80}$",
     "fields": {"status":   {"enum": ["on_track", "at_risk", "blocked"]},
                "due":      {"type": "string", "maxLength": 32},
                "progress": {"type": "number", "minimum": 0, "maximum": 1},
                "tags":     {"type": "array", "items": {"type": "string"},
                             "maxItems": 10}},
     "required": ["status"],
     "forbidden": ["salary", "customer_email", "api_key"]}

`forbidden` names fields that must never ride in a signal of that kind,
anywhere in the payload. They are already unknown fields; naming them makes
the refusal say why ("forbidden_field") so an author learns the boundary.

Signing is HMAC-SHA256 over `canonical(signal)`: sorted keys, no whitespace,
UTF-8, with `sig` excluded. Any byte change after signing fails `verify`.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import math
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

# Every field a signal may carry. Anything else is rejected.
FIELDS = (
    "signal_id", "origin_id", "producer", "ts", "expires_at", "topic",
    "kind", "subject", "summary", "confidence", "body", "sources", "hops",
    "thread_id", "derived_from", "sig",
)
# What an agent may send to POST /fleet/publish. Identity, timing, hop count,
# and signature are Sparrow's to stamp, so an agent supplying them is refused.
AGENT_FIELDS = (
    "signal_id", "topic", "kind", "subject", "summary", "confidence", "body",
    "sources", "thread_id", "derived_from", "expires_at",
)
SOURCE_FIELDS = ("name", "url", "license", "as_of")
OPTIONAL = frozenset({"subject", "confidence", "thread_id", "derived_from",
                      "sig"})
SHAREABLE_LICENSE = "internal_ok"

MAX_SUMMARY_CHARS = 4000
MAX_SOURCES = 20
MAX_TTL_S = 7 * 24 * 3600
MAX_CLOCK_SKEW_S = 300
MAX_BYTES = 16 * 1024
MAX_BODY_FIELDS = 32

_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_SUBJECT_MAX = 200
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,95}$")
_PRODUCER_RE = re.compile(r"^agent:[a-z0-9][a-z0-9_-]{0,47}$")
_LICENSE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")

# Always available, needs no registry: a summary about an optional subject.
BUILTIN_KINDS: dict = {
    "note": {"description": "Free-form information: a summary about an "
                            "optional subject, no structured body.",
             "subject": "optional", "fields": {}, "required": [],
             "forbidden": []},
}


class SignalError(ValueError):
    """A signal failed validation. `code` is stable; `reason` is for humans."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason

    def as_dict(self) -> dict:
        return {"ok": False, "error": self.code, "reason": self.reason}


@dataclass(frozen=True)
class Source:
    name: str
    license: str
    url: str = ""
    as_of: float | None = None


@dataclass(frozen=True)
class Signal:
    signal_id: str
    origin_id: str
    producer: str
    ts: float
    expires_at: float
    topic: str
    kind: str
    summary: str
    body: dict = field(default_factory=dict)
    subject: str | None = None
    confidence: float | None = None
    sources: tuple[Source, ...] = field(default_factory=tuple)
    hops: int = 0
    thread_id: str | None = None
    derived_from: str | None = None
    sig: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["sources"] = [_source_dict(s) for s in self.sources]
        return d


def _source_dict(s: Source) -> dict:
    out: dict[str, Any] = {"name": s.name, "license": s.license}
    if s.url:
        out["url"] = s.url
    if s.as_of is not None:
        out["as_of"] = s.as_of
    return out


# --- kinds registry ---------------------------------------------------------------
_FIELD_SPEC_KEYS = {"type", "enum", "maxLength", "minimum", "maximum",
                    "pattern", "items", "maxItems", "description"}
_SCALAR_TYPES = ("string", "number", "integer", "boolean")


def _check_field_spec(name: str, spec: Any, where: str) -> dict:
    if not isinstance(spec, dict):
        raise ValueError(f"{where}.{name} must be an object")
    extra = set(spec) - _FIELD_SPEC_KEYS
    if extra:
        raise ValueError(f"{where}.{name} has unknown keys {sorted(extra)}")
    if "enum" in spec:
        if not isinstance(spec["enum"], list) or not spec["enum"] or not all(
                isinstance(v, (str, int, float, bool)) for v in spec["enum"]):
            raise ValueError(f"{where}.{name}.enum must be a list of scalars")
        return dict(spec)
    t = spec.get("type")
    if t == "array":
        items = spec.get("items") or {"type": "string"}
        _check_field_spec("items", items, f"{where}.{name}")
        if items.get("type") == "array":
            raise ValueError(f"{where}.{name}: nested arrays are not allowed")
        return {**spec, "items": items}
    if t not in _SCALAR_TYPES:
        raise ValueError(f"{where}.{name}.type must be one of "
                         f"{_SCALAR_TYPES + ('array',)} (or give an enum)")
    if "pattern" in spec:
        re.compile(spec["pattern"])
    return dict(spec)


def check_kind(name: str, spec: Any) -> dict:
    """Validate one kind definition; returns it normalized. ValueError if bad."""
    if not _KIND_RE.match(name or ""):
        raise ValueError(f"bad kind name {name!r}")
    if not isinstance(spec, dict):
        raise ValueError(f"kind {name!r} must be an object")
    extra = set(spec) - {"description", "subject", "subject_pattern",
                         "fields", "required", "forbidden"}
    if extra:
        raise ValueError(f"kind {name!r} has unknown keys {sorted(extra)}")
    subject = spec.get("subject", "optional")
    if subject not in ("required", "optional", "forbidden"):
        raise ValueError(f"kind {name!r}: subject must be required, "
                         "optional or forbidden")
    if spec.get("subject_pattern"):
        re.compile(spec["subject_pattern"])
    fields = spec.get("fields") or {}
    if not isinstance(fields, dict) or len(fields) > MAX_BODY_FIELDS:
        raise ValueError(f"kind {name!r}: fields must be an object "
                         f"(at most {MAX_BODY_FIELDS})")
    clean = {}
    for fname, fspec in fields.items():
        if not _FIELD_RE.match(fname):
            raise ValueError(f"kind {name!r}: bad field name {fname!r}")
        clean[fname] = _check_field_spec(fname, fspec, f"kind {name!r}")
    required = list(spec.get("required") or [])
    missing = [r for r in required if r not in clean]
    if missing:
        raise ValueError(f"kind {name!r}: required names undeclared "
                         f"field(s) {missing}")
    forbidden = sorted({str(f).strip().lower()
                        for f in spec.get("forbidden") or [] if str(f).strip()})
    clash = [f for f in forbidden if f in clean]
    if clash:
        raise ValueError(f"kind {name!r}: {clash} are both fields and "
                         "forbidden")
    return {"description": str(spec.get("description") or "")[:300],
            "subject": subject,
            "subject_pattern": spec.get("subject_pattern") or "",
            "fields": clean, "required": required, "forbidden": forbidden}


def load_kinds(raw: Any) -> dict:
    """Parse a fleet_kinds.json body ({"kinds": {...}}) into a registry that
    always includes the built-ins. Raises ValueError on any bad definition
    so a malformed registry fails closed rather than loosening checks."""
    out = {k: check_kind(k, v) for k, v in BUILTIN_KINDS.items()}
    if raw is None:
        return out
    if not isinstance(raw, dict) or not isinstance(raw.get("kinds"), dict):
        raise ValueError('kinds registry must be {"kinds": {...}}')
    for name, spec in raw["kinds"].items():
        if name in BUILTIN_KINDS:
            raise ValueError(f"kind {name!r} is built in")
        out[name] = check_kind(name, spec)
    return out


# Published at GET /fleet/schema so agent authors can validate client-side.
# `validate` below is the enforcement; this mirrors the core envelope.
SIGNAL_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Sparrow fleet signal",
    "type": "object",
    "additionalProperties": False,
    "required": [f for f in FIELDS if f not in OPTIONAL],
    "properties": {
        "signal_id": {"type": "string", "pattern": _ID_RE.pattern},
        "origin_id": {"type": "string", "pattern": _ID_RE.pattern},
        "producer": {"type": "string", "pattern": _PRODUCER_RE.pattern},
        "ts": {"type": "number"},
        "expires_at": {"type": "number"},
        "topic": {"type": "string", "pattern": _TOPIC_RE.pattern},
        "kind": {"type": "string", "pattern": _KIND_RE.pattern},
        "subject": {"type": ["string", "null"], "maxLength": _SUBJECT_MAX},
        "summary": {"type": "string", "minLength": 1,
                    "maxLength": MAX_SUMMARY_CHARS},
        "confidence": {"type": ["number", "null"], "minimum": 0,
                       "maximum": 1},
        "body": {"type": "object",
                 "description": "Shape declared by the signal's kind"},
        "sources": {
            "type": "array", "maxItems": MAX_SOURCES,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["name", "license"],
                "properties": {
                    "name": {"type": "string", "minLength": 1,
                             "maxLength": 200},
                    "url": {"type": "string", "maxLength": 500},
                    "license": {"type": "string",
                                "pattern": _LICENSE_RE.pattern},
                    "as_of": {"type": "number"},
                },
            },
        },
        "hops": {"type": "integer", "minimum": 0},
        "thread_id": {"type": ["string", "null"], "pattern": _ID_RE.pattern},
        "derived_from": {"type": ["string", "null"],
                         "pattern": _ID_RE.pattern},
        "sig": {"type": "string"},
    },
}


def kind_schema(name: str, spec: dict) -> dict:
    """JSON Schema for one kind's body (for GET /fleet/kinds)."""
    props = {}
    for f, s in spec["fields"].items():
        props[f] = {k: v for k, v in s.items()}
    return {"title": name, "description": spec["description"],
            "type": "object", "additionalProperties": False,
            "required": spec["required"], "properties": props,
            "x-subject": spec["subject"],
            "x-forbidden": spec["forbidden"]}


# --- canonical bytes + HMAC ---------------------------------------------------
def canonical(signal: dict | Signal) -> bytes:
    """Sorted keys, no whitespace, UTF-8, `sig` excluded."""
    d = signal.to_dict() if isinstance(signal, Signal) else dict(signal)
    d.pop("sig", None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def digest(signal: dict | Signal) -> str:
    """SHA-256 of the canonical bytes — what an approval binds to."""
    return hashlib.sha256(canonical(signal)).hexdigest()


def _key_bytes(key: str | bytes) -> bytes:
    if isinstance(key, bytes):
        return key
    return (key or "").encode("utf-8")


def compute_sig(signal: dict | Signal, key: str | bytes) -> str:
    if not key:
        raise SignalError("no_key", "refusing to sign with an empty key")
    return hmac.new(_key_bytes(key), canonical(signal),
                    hashlib.sha256).hexdigest()


def sign(signal: dict, key: str | bytes) -> dict:
    """Return a copy of `signal` with `sig` set. Never mutates the input."""
    out = dict(signal)
    out["sig"] = compute_sig(out, key)
    return out


def verify(signal: dict, key: str | bytes) -> bool:
    sig = signal.get("sig") if isinstance(signal, dict) else None
    if not isinstance(sig, str) or not sig or not key:
        return False
    try:
        want = compute_sig(signal, key)
    except (SignalError, TypeError, ValueError):
        return False
    return hmac.compare_digest(want, sig)


def link_key(token: str) -> str:
    """HMAC key for one hop, derived from that hop's bearer token.

    Both ends store bearer tokens hash-only, so neither could verify an HMAC
    keyed on the plaintext. Keying on SHA-256(token) lets the holder of the
    plaintext and the holder of the hash compute the same key.
    """
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


# --- validation ---------------------------------------------------------------
def _bad(code: str, reason: str) -> SignalError:
    return SignalError(code, reason)


def _find_forbidden(obj: Any, forbidden: frozenset[str],
                    where: str) -> str | None:
    """Path of the first forbidden key anywhere in `obj`, or None."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in forbidden:
                return f"{where}.{k}"
            hit = _find_forbidden(v, forbidden, f"{where}.{k}")
            if hit:
                return hit
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hit = _find_forbidden(v, forbidden, f"{where}[{i}]")
            if hit:
                return hit
    return None


def _check_keys(obj: dict, allowed: Iterable[str], where: str) -> None:
    extra = [k for k in obj if k not in set(allowed)]
    if extra:
        raise _bad("unknown_field", f"{where} has unknown field(s): "
                                    f"{', '.join(sorted(map(str, extra)))}")


def _num(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise _bad("bad_type", f"{name} must be a number")
    if not math.isfinite(float(v)):
        raise _bad("bad_value", f"{name} must be finite")
    return float(v)


def _str(v: Any, name: str, pattern: re.Pattern | None = None,
         max_len: int | None = None) -> str:
    if not isinstance(v, str):
        raise _bad("bad_type", f"{name} must be a string")
    if max_len is not None and len(v) > max_len:
        raise _bad("bad_value", f"{name} exceeds {max_len} characters")
    if pattern is not None and not pattern.match(v):
        raise _bad("bad_value", f"{name} {v!r} is not well formed")
    return v


def _opt_id(v: Any, name: str) -> str | None:
    if v is None or v == "":
        return None
    return _str(v, name, _ID_RE)


def _sources(raw: Any, *, outbound: bool) -> tuple[Source, ...]:
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise _bad("bad_type", "sources must be a list")
    if len(raw) > MAX_SOURCES:
        raise _bad("bad_value", f"at most {MAX_SOURCES} sources")
    out = []
    for i, s in enumerate(raw):
        where = f"sources[{i}]"
        if not isinstance(s, dict):
            raise _bad("bad_type", f"{where} must be an object")
        _check_keys(s, SOURCE_FIELDS, where)
        if "license" not in s:
            raise _bad("missing_field", f"{where}.license is required")
        name = _str(s.get("name"), f"{where}.name", max_len=200).strip()
        if not name:
            raise _bad("bad_value", f"{where}.name is empty")
        lic = _str(s["license"], f"{where}.license", _LICENSE_RE)
        if outbound and lic != SHAREABLE_LICENSE:
            raise _bad("license_not_shareable",
                       f"{where} is licensed {lic!r}; only "
                       f"{SHAREABLE_LICENSE!r} sources may leave this Sparrow")
        url = _str(s.get("url", ""), f"{where}.url", max_len=500)
        as_of = s.get("as_of")
        out.append(Source(name=name, license=lic, url=url,
                          as_of=None if as_of is None
                          else _num(as_of, f"{where}.as_of")))
    return tuple(out)


def _field_value(v: Any, spec: dict, name: str) -> Any:
    if "enum" in spec:
        if isinstance(v, bool) != any(isinstance(e, bool) for e in spec["enum"]) \
                or v not in spec["enum"]:
            raise _bad("bad_value", f"{name} must be one of {spec['enum']}")
        return v
    t = spec.get("type")
    if t == "array":
        if not isinstance(v, list):
            raise _bad("bad_type", f"{name} must be a list")
        if len(v) > int(spec.get("maxItems", 50)):
            raise _bad("bad_value", f"{name} has too many items")
        return [_field_value(x, spec["items"], f"{name}[{i}]")
                for i, x in enumerate(v)]
    if t == "string":
        _str(v, name, max_len=int(spec.get("maxLength", 1000)))
        if spec.get("pattern") and not re.search(spec["pattern"], v):
            raise _bad("bad_value", f"{name} {v!r} is not well formed")
        return v
    if t == "boolean":
        if not isinstance(v, bool):
            raise _bad("bad_type", f"{name} must be true or false")
        return v
    if t == "integer" and (isinstance(v, bool) or not isinstance(v, int)):
        raise _bad("bad_type", f"{name} must be an integer")
    n = _num(v, name)
    if "minimum" in spec and n < spec["minimum"]:
        raise _bad("bad_value", f"{name} is below {spec['minimum']}")
    if "maximum" in spec and n > spec["maximum"]:
        raise _bad("bad_value", f"{name} is above {spec['maximum']}")
    return v


def _body(raw: Any, kind: str, spec: dict) -> dict:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _bad("bad_type", "body must be an object")
    _check_keys(raw, spec["fields"], f"body (kind {kind!r})")
    missing = [f for f in spec["required"] if f not in raw]
    if missing:
        raise _bad("missing_field",
                   f"kind {kind!r} requires body.{', body.'.join(missing)}")
    return {f: _field_value(v, spec["fields"][f], f"body.{f}")
            for f, v in raw.items()}


def _forbidden_gate(obj: dict, kinds: dict) -> None:
    """Runs before any other check so the refusal names the boundary."""
    kind = obj.get("kind") if isinstance(obj.get("kind"), str) else None
    spec = kinds.get(kind) if kind else None
    if not spec or not spec["forbidden"]:
        return
    hit = _find_forbidden(obj, frozenset(spec["forbidden"]), "signal")
    if hit:
        raise _bad("forbidden_field",
                   f"{hit} is forbidden in a {kind!r} signal")


def check_agent_input(obj: Any, kinds: dict | None = None) -> dict:
    """The agent-facing gate: only AGENT_FIELDS, nothing Sparrow stamps."""
    if not isinstance(obj, dict):
        raise _bad("not_object", "signal must be a JSON object")
    kinds = kinds if kinds is not None else load_kinds(None)
    _forbidden_gate(obj, kinds)
    stamped = sorted(k for k in obj if k in FIELDS and k not in AGENT_FIELDS)
    if stamped:
        raise _bad("stamped_field",
                   f"{', '.join(stamped)} are set by Sparrow, not the agent")
    _check_keys(obj, AGENT_FIELDS, "signal")
    return obj


def validate(obj: Any, *, kinds: dict | None = None, now: float | None = None,
             max_hops: int = 2, outbound: bool = False) -> Signal:
    """Full envelope check. Raises SignalError; returns the typed Signal.

    `kinds` is the loaded registry (load_kinds); None means built-ins only.
    `outbound=True` adds the egress rule that every source must be licensed
    internal_ok. Expiry and the hop cap apply in both directions.
    """
    if not isinstance(obj, dict):
        raise _bad("not_object", "signal must be a JSON object")
    kinds = kinds if kinds is not None else load_kinds(None)
    _forbidden_gate(obj, kinds)
    _check_keys(obj, FIELDS, "signal")
    missing = [f for f in FIELDS if f not in OPTIONAL and f not in obj]
    if missing:
        raise _bad("missing_field", f"missing: {', '.join(missing)}")
    try:
        size = len(canonical(obj))
    except (TypeError, ValueError) as exc:
        raise _bad("bad_type", f"not canonical JSON ({exc})") from None
    if size > MAX_BYTES:
        raise _bad("too_large", f"signal is {size} bytes (max {MAX_BYTES})")

    kind = _str(obj["kind"], "kind", _KIND_RE)
    spec = kinds.get(kind)
    if spec is None:
        raise _bad("unknown_kind", f"kind {kind!r} is not in the registry")

    now = time.time() if now is None else now
    ts = _num(obj["ts"], "ts")
    expires_at = _num(obj["expires_at"], "expires_at")
    if ts > now + MAX_CLOCK_SKEW_S:
        raise _bad("bad_value", "ts is in the future")
    if expires_at <= ts:
        raise _bad("bad_value", "expires_at must be after ts")
    if expires_at - ts > MAX_TTL_S:
        raise _bad("bad_value", f"ttl exceeds {MAX_TTL_S}s")
    if expires_at <= now:
        raise _bad("expired", "signal has expired")

    hops = obj["hops"]
    if isinstance(hops, bool) or not isinstance(hops, int) or hops < 0:
        raise _bad("bad_type", "hops must be a non-negative integer")
    if hops > max_hops:
        raise _bad("too_many_hops", f"hops {hops} exceeds max {max_hops}")

    confidence = obj.get("confidence")
    if confidence is not None:
        confidence = _num(confidence, "confidence")
        if not 0.0 <= confidence <= 1.0:
            raise _bad("bad_value", "confidence must be within [0, 1]")

    subject = obj.get("subject")
    if subject in ("", None):
        subject = None
    if subject is None and spec["subject"] == "required":
        raise _bad("missing_field", f"kind {kind!r} requires a subject")
    if subject is not None:
        if spec["subject"] == "forbidden":
            raise _bad("bad_value", f"kind {kind!r} takes no subject")
        subject = _str(subject, "subject", max_len=_SUBJECT_MAX).strip()
        if not subject:
            raise _bad("bad_value", "subject is empty")
        if spec["subject_pattern"] and not re.search(spec["subject_pattern"],
                                                     subject):
            raise _bad("bad_value", f"subject {subject!r} is not well formed "
                                    f"for kind {kind!r}")

    summary = _str(obj["summary"], "summary", max_len=MAX_SUMMARY_CHARS)
    if not summary.strip():
        raise _bad("bad_value", "summary is empty")
    sig = obj.get("sig", "")
    if not isinstance(sig, str):
        raise _bad("bad_type", "sig must be a string")

    return Signal(
        signal_id=_str(obj["signal_id"], "signal_id", _ID_RE),
        origin_id=_str(obj["origin_id"], "origin_id", _ID_RE),
        producer=_str(obj["producer"], "producer", _PRODUCER_RE),
        ts=ts, expires_at=expires_at,
        topic=_str(obj["topic"], "topic", _TOPIC_RE),
        kind=kind, summary=summary,
        body=_body(obj["body"], kind, spec),
        subject=subject, confidence=confidence,
        sources=_sources(obj["sources"], outbound=outbound),
        hops=hops,
        thread_id=_opt_id(obj.get("thread_id"), "thread_id"),
        derived_from=_opt_id(obj.get("derived_from"), "derived_from"),
        sig=sig,
    )


# --- blocked subjects (compliance's list) ---------------------------------------
def normalize_subject(s: str) -> str:
    """Case- and whitespace-insensitive key."""
    return " ".join((s or "").split()).casefold()


@dataclass(frozen=True)
class Blocklist:
    subjects: frozenset[str] = frozenset()
    patterns: tuple[str, ...] = ()

    def blocks(self, subject: str | None) -> bool:
        if not subject:
            return False
        key = normalize_subject(subject)
        if key in self.subjects:
            return True
        return any(fnmatch.fnmatchcase(key, p) for p in self.patterns)


def load_blocklist(raw: Any) -> Blocklist:
    """Parse a blocked-subjects file:

        {"subjects": ["Project Falcon", "ACME Corp"],
         "patterns": ["xyz*"]}

    Exact entries match case- and whitespace-insensitively; patterns are
    shell globs over the same normalized form. Raises ValueError on a
    malformed list so callers fail closed rather than treating garbage as
    "empty"."""
    if not isinstance(raw, dict):
        raise ValueError('blocked list must be {"subjects": [...]}')
    subs = raw.get("subjects", [])
    pats = raw.get("patterns", [])
    if not isinstance(subs, list) or not isinstance(pats, list):
        raise ValueError("subjects and patterns must be lists")
    if "subjects" not in raw and "patterns" not in raw:
        raise ValueError('blocked list must be {"subjects": [...]}')
    return Blocklist(
        subjects=frozenset(normalize_subject(str(x)) for x in subs
                           if str(x).strip()),
        patterns=tuple(normalize_subject(str(p)) for p in pats
                       if str(p).strip()))


# --- helpers for stampers -------------------------------------------------------
def new_id(prefix: str = "") -> str:
    u = uuid.uuid4().hex
    return f"{prefix}:{u}" if prefix else u
