"""Seat side of the Mac helper: how a hosted seat gets texts it can't read.

A hosted seat is a Linux container, so the helper on the user's Mac
(imessage_helper.py) reads Messages and pushes what passes its filters here.
Off unless QUILL_IMESSAGE_RELAY=1.

One Mac per seat. The webapp starts a pairing; the single-use code in the
command it shows buys the helper a token that can do exactly one thing, push
texts, and Disconnect revokes it. Only hashes are stored.

The helper is not trusted to have filtered anything: this end keeps only known
fields, repeats the secret and one-time-code checks, and forces the stamps. A
stolen token can still plant believable fake texts, which is why these land
observed-tier and can never authorize an action.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import secrets
import threading
import time
import zipfile
from pathlib import Path
from typing import Any

from app.config import settings
from app.services import privacy_class as pc
from app.services.connectors import imessage_reader as reader

PAIR_TTL_S = 15 * 60
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 500
MAX_TEXT = 20000
STALE_AFTER_S = 3 * 3600
# The only problems a helper may report; anything else is dropped, not stored.
DEVICE_ERRORS = {"no_access": reader.ACCESS_HINT}

_lock = threading.RLock()
_ROOT = Path(__file__).resolve().parents[3]
_HELPER_FILES = ("app/services/redact.py", "app/services/privacy_class.py",
                 "app/services/connectors/imessage_reader.py",
                 "app/services/connectors/imessage_helper.py")
_EMPTY_INITS = ("app/__init__.py", "app/services/__init__.py",
                "app/services/connectors/__init__.py")


def enabled() -> bool:
    return os.environ.get("QUILL_IMESSAGE_RELAY", "0") in ("1", "true", "True")


def _state_path() -> Path:
    return Path(settings.storage.data_dir) / "connectors" / "imessage" / "relay.json"


def _load() -> dict:
    try:
        data = json.loads(_state_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(data: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _h(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _matches(secret: str | None, stored: str | None) -> bool:
    return bool(secret and stored) and hmac.compare_digest(_h(secret), stored)


def start_pairing(*, now: float | None = None) -> str:
    """A fresh single-use code. An already paired Mac keeps working until a new
    one claims this, so an abandoned pairing costs nothing."""
    now = float(now if now is not None else time.time())
    code = secrets.token_urlsafe(24)
    with _lock:
        data = _load()
        data["pair"] = {"hash": _h(code), "expires": now + PAIR_TTL_S}
        _save(data)
    return code


def claim(code: str | None, device: str | None,
          *, now: float | None = None) -> str | None:
    """Trade a live code for a push token. A new Mac replaces the old one."""
    now = float(now if now is not None else time.time())
    with _lock:
        data = _load()
        pair = data.get("pair") or {}
        if float(pair.get("expires") or 0) <= now or not _matches(code, pair.get("hash")):
            return None
        token = "spi_" + secrets.token_urlsafe(32)
        data.pop("pair", None)
        data["device"] = {"token": _h(token), "name": str(device or "Mac")[:80],
                          "paired_at": now, "last_seen": now, "error": None}
        _save(data)
    return token


def device() -> dict | None:
    """The paired Mac, without its token hash."""
    d = _load().get("device")
    if not isinstance(d, dict):
        return None
    return {k: d.get(k) for k in ("name", "paired_at", "last_seen", "error")}


def pair_pending(*, now: float | None = None) -> bool:
    now = float(now if now is not None else time.time())
    return float((_load().get("pair") or {}).get("expires") or 0) > now


def revoke() -> None:
    with _lock:
        data = _load()
        data.pop("device", None)
        data.pop("pair", None)
        _save(data)


def verify(token: str | None) -> bool:
    return _matches(token, (_load().get("device") or {}).get("token"))


def clean_item(raw: Any, *, now: float) -> dict | None:
    """What may land from one pushed item, or None. Fields are rebuilt rather
    than passed through, so a helper can't set meta like never_authorizes."""
    if not isinstance(raw, dict):
        return None
    ext = raw.get("external_id")
    body = raw.get("body")
    if not isinstance(ext, str) or not ext or len(ext) > 200:
        return None
    if not isinstance(body, str) or not body.strip():
        return None
    body = body.strip()[:MAX_TEXT]
    if reader.keep_out(body):
        return None
    try:
        ts = float(raw.get("ts"))
    except (TypeError, ValueError):
        ts = now
    from_self = raw.get("from_self") is True

    def short(key: str) -> str:
        v = raw.get(key)
        return v[:500] if isinstance(v, str) else ""

    people = raw.get("people") if isinstance(raw.get("people"), list) else []
    meta = raw.get("meta") if isinstance(raw.get("meta"), dict) else {}
    service = meta.get("service")
    return {
        "kind": "message", "external_id": ext, "thread_id": short("thread_id") or None,
        "ts": min(ts, now), "title": body[:reader.TITLE_MAX], "body": body,
        "people": [p[:120] for p in people if isinstance(p, str)][:50],
        "direction": "sent" if from_self else "received", "from_self": from_self,
        "from": short("from"), "to": short("to"),
        "meta": {"privacy_class": pc.SENSITIVE, "relayed": True,
                 "service": service[:40] if isinstance(service, str) else "iMessage",
                 "group": meta.get("group") is True},
    }


def accept_push(connector, payload: Any, *, store=None,
                now: float | None = None) -> dict[str, Any]:
    """Land one authenticated batch. Safe to repeat: the helper resends when it
    never saw the answer, and the scheduler's ledger drops what already landed."""
    from app.services.connectors import scheduler
    now = float(now if now is not None else time.time())
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return {"ok": False, "error": "items must be a list"}
    raw_items = payload["items"][:MAX_ITEMS]
    items = [i for i in (clean_item(r, now=now) for r in raw_items) if i]
    with _lock:
        data = _load()
        dev = data.get("device")
        if isinstance(dev, dict):
            dev["last_seen"] = now
            dev["error"] = (payload.get("error")
                            if payload.get("error") in DEVICE_ERRORS else None)
            _save(data)
        landed = scheduler.land_unseen(connector, items, store=store, now=now)
    return {"ok": True, "received": len(raw_items), "landed": landed,
            "dropped": len(raw_items) - len(items)}


def status_line(store_landed: int, *, now: float | None = None) -> str:
    now = float(now if now is not None else time.time())
    dev = device()
    if not dev:
        return ""
    age = max(0.0, now - float(dev.get("last_seen") or 0))
    if age < 120:
        ago = "just now"
    elif age < 7200:
        ago = f"{int(age // 60)} minutes ago"
    else:
        ago = f"{int(age // 3600)} hours ago"
    kept = f"{store_landed} text{'' if store_landed == 1 else 's'} kept."
    if age > STALE_AFTER_S:
        return (f"{dev.get('name')} last checked in {ago}, so new texts are "
                f"waiting. Is that Mac awake? {kept}")
    return f"{dev.get('name')} checked in {ago}. {kept}"


def build_helper() -> bytes:
    """The helper as a runnable zipapp, built from the same reader and filters
    this install uses, so the two can't drift apart. The package __init__ files
    are empty so nothing else of Sparrow is imported or shipped."""
    buf = io.BytesIO()
    buf.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in _EMPTY_INITS:
            z.writestr(name, "")
        for name in _HELPER_FILES:
            z.write(_ROOT / name, name)
        z.writestr("__main__.py",
                   "from app.services.connectors.imessage_helper import main\n"
                   "raise SystemExit(main())\n")
    return buf.getvalue()


def install_command(base: str, code: str) -> str:
    """The one command the webapp shows. /usr/bin/python3 is what every Mac has
    (it offers to install Apple's command line tools the first time)."""
    pyz = "~/.sparrow/sparrow-imessage.pyz"
    return (f"mkdir -p ~/.sparrow && curl -fsSL '{base}/imessage-relay/helper.pyz' "
            f"-o {pyz} && /usr/bin/python3 {pyz} pair '{base}' '{code}'")
