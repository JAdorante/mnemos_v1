"""Origin-id dedup and the own-origin ledger.

echo_dedup.py suppresses near-duplicate TEXT inside a time window; signals
need the same windowing idea keyed on ids instead, because two different
agents may legitimately say similar things and one signal may arrive twice.

Two ledgers, one file:
  own  — origin_ids this Sparrow's fleet minted. An inbound signal carrying
         one is our own view coming back around, and is dropped so it can
         never look like independent confirmation.
  seen — origin_ids already accepted from the relay. A replay is deduped.

Entries live until the signal's own expiry (plus a grace), so the ledger is
bounded by the TTL cap in envelope.py and survives a restart.
"""
from __future__ import annotations

import time

from app.config import settings
from app.services.fleet import _files

GRACE_S = 600.0
MAX_ENTRIES = 50_000


def _load() -> dict:
    data = _files.load(settings.fleet.origins_path, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("own", {})
    data.setdefault("seen", {})
    return data


def _prune(book: dict, now: float) -> dict:
    live = {k: v for k, v in book.items() if float(v) > now}
    if len(live) > MAX_ENTRIES:
        keep = sorted(live.items(), key=lambda kv: kv[1])[-MAX_ENTRIES:]
        live = dict(keep)
    return live


def _put(kind: str, origin_id: str, expires_at: float) -> None:
    now = time.time()
    with _files.lock:
        data = _load()
        book = _prune(data[kind], now)
        book[origin_id] = float(expires_at) + GRACE_S
        data[kind] = book
        _files.save(settings.fleet.origins_path, data)


def _has(kind: str, origin_id: str) -> bool:
    exp = _load()[kind].get(origin_id)
    return exp is not None and float(exp) > time.time()


def record_own(origin_id: str, expires_at: float) -> None:
    _put("own", origin_id, expires_at)


def is_own(origin_id: str) -> bool:
    return _has("own", origin_id)


def check_and_mark_seen(origin_id: str, expires_at: float) -> bool:
    """True when this origin_id is new (and marks it); False on a replay."""
    now = time.time()
    with _files.lock:
        data = _load()
        book = _prune(data["seen"], now)
        if origin_id in book:
            return False
        book[origin_id] = float(expires_at) + GRACE_S
        data["seen"] = book
        _files.save(settings.fleet.origins_path, data)
    return True
