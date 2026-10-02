"""The one signal envelope the fleet, Sparrow, and the relay all validate.

Stdlib only and free of app imports: org_coordinator/ imports this module
directly so the relay checks exactly the shape Sparrow checks.

The schema deliberately has no field for quantity, price, order id, or
position. A payload carrying those is malformed, not just unwise — it fails
`validate` with code "order_like_field" before anything else looks at it.

Signing is HMAC-SHA256 over `canonical(signal)`: sorted keys, no whitespace,
UTF-8, with `sig` excluded. Any byte change after signing fails `verify`.
"""
from __future__ import annotations

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
    "instrument", "direction", "horizon", "confidence", "thesis", "sources",
    "hops", "thread_id", "derived_from", "sig",
)
# What an agent may send to POST /fleet/publish. Identity, timing, hop count,
# and signature are Sparrow's to stamp, so an agent supplying them is refused.
AGENT_FIELDS = (
    "signal_id", "topic", "instrument", "direction", "horizon", "confidence",
    "thesis", "sources", "thread_id", "derived_from", "expires_at",
)
SOURCE_FIELDS = ("name", "url", "license", "as_of")
OPTIONAL = frozenset({"thread_id", "derived_from", "sig"})

DIRECTIONS = ("bullish", "bearish", "neutral")
HORIZONS = ("intraday", "days", "weeks", "months", "long_term")
SHAREABLE_LICENSE = "internal_ok"

# Named so the refusal says why. They are already unknown fields; this only
# sharpens the error so an agent author learns the boundary, not a typo.
ORDER_LIKE = frozenset({
    "quantity", "qty", "size", "shares", "lots", "notional", "price",
    "limit_price", "stop_price", "target_price", "order", "order_id",
    "order_type", "side", "position", "positions", "exposure", "pnl",
    "p_and_l", "p&l", "account", "account_id", "fill", "fills",
})

MAX_THESIS_CHARS = 4000
MAX_SOURCES = 20
MAX_TTL_S = 7 * 24 * 3600
MAX_CLOCK_SKEW_S = 300
MAX_BYTES = 16 * 1024

_TOPIC_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_INSTRUMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:/_-]{0,31}$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,95}$")
_PRODUCER_RE = re.compile(r"^agent:[a-z0-9][a-z0-9_-]{0,47}$")
_LICENSE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


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
    instrument: str
    direction: str
    horizon: str
    confidence: float
    thesis: str
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


# Published at GET /fleet/schema so agent authors can validate client-side.
# `validate` below is the enforcement; this mirrors it for other tooling.
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
        "instrument": {"type": "string", "pattern": _INSTRUMENT_RE.pattern},
        "direction": {"enum": list(DIRECTIONS)},
        "horizon": {"enum": list(HORIZONS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "thesis": {"type": "string", "minLength": 1,
                   "maxLength": MAX_THESIS_CHARS},
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


def _check_keys(obj: dict, allowed: Iterable[str], where: str) -> None:
    allowed = set(allowed)
    extra = [k for k in obj if k not in allowed]
    if not extra:
        return
    order_like = sorted(k for k in extra if str(k).lower() in ORDER_LIKE)
    if order_like:
        raise _bad("order_like_field",
                   f"{where} carries {', '.join(order_like)}; signals hold a "
                   "view, never positions, orders, or P&L")
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


def check_agent_input(obj: Any) -> dict:
    """The agent-facing gate: only AGENT_FIELDS, nothing Sparrow stamps."""
    if not isinstance(obj, dict):
        raise _bad("not_object", "signal must be a JSON object")
    stamped = sorted(k for k in obj if k in FIELDS and k not in AGENT_FIELDS)
    if stamped:
        raise _bad("stamped_field",
                   f"{', '.join(stamped)} are set by Sparrow, not the agent")
    _check_keys(obj, AGENT_FIELDS, "signal")
    return obj


def validate(obj: Any, *, now: float | None = None, max_hops: int = 2,
             outbound: bool = False) -> Signal:
    """Full envelope check. Raises SignalError; returns the typed Signal.

    `outbound=True` adds the egress rules: every source must be licensed
    internal_ok. Expiry and the hop cap apply in both directions.
    """
    if not isinstance(obj, dict):
        raise _bad("not_object", "signal must be a JSON object")
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

    confidence = _num(obj["confidence"], "confidence")
    if not 0.0 <= confidence <= 1.0:
        raise _bad("bad_value", "confidence must be within [0, 1]")
    direction = _str(obj["direction"], "direction")
    if direction not in DIRECTIONS:
        raise _bad("bad_value", f"direction must be one of {DIRECTIONS}")
    horizon = _str(obj["horizon"], "horizon")
    if horizon not in HORIZONS:
        raise _bad("bad_value", f"horizon must be one of {HORIZONS}")
    thesis = _str(obj["thesis"], "thesis", max_len=MAX_THESIS_CHARS)
    if not thesis.strip():
        raise _bad("bad_value", "thesis is empty")
    sig = obj.get("sig", "")
    if not isinstance(sig, str):
        raise _bad("bad_type", "sig must be a string")

    return Signal(
        signal_id=_str(obj["signal_id"], "signal_id", _ID_RE),
        origin_id=_str(obj["origin_id"], "origin_id", _ID_RE),
        producer=_str(obj["producer"], "producer", _PRODUCER_RE),
        ts=ts, expires_at=expires_at,
        topic=_str(obj["topic"], "topic", _TOPIC_RE),
        instrument=_str(obj["instrument"], "instrument", _INSTRUMENT_RE),
        direction=direction, horizon=horizon, confidence=confidence,
        thesis=thesis,
        sources=_sources(obj["sources"], outbound=outbound),
        hops=hops,
        thread_id=_opt_id(obj.get("thread_id"), "thread_id"),
        derived_from=_opt_id(obj.get("derived_from"), "derived_from"),
        sig=sig,
    )


# --- restricted list ------------------------------------------------------------
def normalize_instrument(s: str) -> str:
    """Case- and venue-insensitive key: "aapl", "AAPL.O", "NASDAQ:AAPL" all
    collapse to "AAPL", so a suffix cannot walk a name past the list."""
    s = (s or "").strip().upper()
    if ":" in s:
        s = s.rsplit(":", 1)[1]
    if "." in s:
        s = s.split(".", 1)[0]
    return s


def load_restricted(raw: Any) -> frozenset[str]:
    """Parse a restricted_list.json body. Raises ValueError on a malformed
    list so callers fail closed rather than treating garbage as "empty"."""
    if not isinstance(raw, dict) or not isinstance(raw.get("instruments"),
                                                   list):
        raise ValueError("restricted list must be {\"instruments\": [...]}")
    return frozenset(normalize_instrument(str(x)) for x in raw["instruments"]
                     if str(x).strip())


def is_restricted(instrument: str, restricted: frozenset[str]) -> bool:
    return normalize_instrument(instrument) in restricted


# --- helpers for stampers -------------------------------------------------------
def new_id(prefix: str = "") -> str:
    u = uuid.uuid4().hex
    return f"{prefix}:{u}" if prefix else u
