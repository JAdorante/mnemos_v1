"""Direct phone -> Sparrow channel — pair any phone, ingest what it sends.

Replaces the Phone Link dependency with a channel Sparrow owns end-to-end:
the phone's own automation app (Shortcuts on iPhone, an HTTP-shortcut app on
Android) POSTs to /phone/ingest and the payload rides the normal event bus like
every other modality. The code is fully general — one pairing flow, one ingest
shape, one registry; everything device-specific (which shortcuts exist, what
they send, when they fire) lives ON the phone as user-authored automation.

Flow:
  desktop: start_pairing()      -> short-lived 6-digit code + setup URL (QR)
  phone:   claim_pairing(code)  -> one-time bearer token (hash stored, never
                                   the token itself), device registered
  phone:   POST /phone/ingest   -> authenticate() by token -> ingest() builds
                                   an Event (source=phone.<kind>) on the bus

Trust model:
  * pairing needs LAN access + eyes on the desktop screen, is single-use,
    expires, and locks after a few wrong attempts.
  * tokens are per-device, stored as SHA-256, revocable from the desktop.
  * ingested content is CONTEXT, never command authority — it lands as memory
    events with the confidence contract attached; anything actionable goes
    through the same offer/approval gates as every other source.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.events import Event, Modality, bus
from app.services import confidence as _conf

# What a phone may send. Anything else is refused (fail closed), so a stolen
# token can only append these bounded, typed observations. "data" is the
# phone ANSWERING a queued query (see OUTBOX_KINDS) — measured device state
# (battery, calendar, location), sent by a shortcut branch the user authored.
KINDS = ("note", "voice", "share", "clipboard", "location", "data", "other")

# Kinds the user authors deliberately (typed/dictated to Sparrow) carry the
# human-said-so tier; captured/forwarded content is merely observed.
_ACCEPTED_KINDS = frozenset({"note", "voice"})

# Client-supplied meta keys worth keeping (whitelist — a phone can't stuff
# arbitrary blobs into event meta). `reply_to` links a "data" answer back to
# the outbox query item it responds to; `name`/`value` carry one measured
# datum ("battery" / 68) alongside the human-readable text.
_META_KEYS = ("url", "title", "app", "lat", "lon", "place", "battery",
              "reply_to", "name", "value")

_lock = threading.Lock()
# The one active pairing offer: {"code", "expires_at", "attempts"}. Starting a
# new pairing replaces it; claiming or too many bad attempts clears it.
_pairing: dict[str, Any] | None = None


# --- device registry (hash-only, outside the agent jail) --------------------
def _registry_path() -> Path:
    return Path(settings.phone.devices_path)


def _load_devices() -> dict:
    try:
        data = json.loads(_registry_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, OSError):
        return {}


def _save_devices(devices: dict) -> None:
    from app.atomic_json import write_json
    write_json(_registry_path(), devices)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --- pairing ----------------------------------------------------------------
def lan_ip() -> str:
    """Best-effort LAN address of this machine (no packets actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def setup_info() -> dict:
    """Where a phone should point, and whether it can reach us at all."""
    ip = lan_ip()
    base = f"http://{ip}:{settings.port}"
    localhost_only = settings.host in ("127.0.0.1", "localhost")
    return {
        "base_url": base,
        "ingest_url": f"{base}/phone/ingest",
        "localhost_only": localhost_only,
        "hint": ("Server is bound to 127.0.0.1 — phones on your Wi-Fi cannot "
                 "reach it. Start with QUILL_HOST=0.0.0.0 (or a tunnel like "
                 "Tailscale) to pair a phone." if localhost_only else
                 "Phone and PC must be on the same network (or a tailnet)."),
    }


def start_pairing() -> dict:
    """Begin (or restart) pairing: one active, short-lived, single-use code."""
    global _pairing
    if not settings.phone.enabled:
        return {"ok": False, "error": "phone channel disabled (QUILL_PHONE_CHANNEL=0)"}
    code = f"{secrets.randbelow(1_000_000):06d}"
    with _lock:
        _pairing = {"code": code,
                    "expires_at": time.time() + settings.phone.pair_ttl_s,
                    "attempts": 0}
    info = setup_info()
    return {"ok": True, "code": code,
            "expires_at": _pairing["expires_at"],
            "ttl_s": settings.phone.pair_ttl_s,
            "setup_url": f"{info['base_url']}/phone/setup?code={code}",
            **info}


def pairing_active() -> bool:
    with _lock:
        return _pairing is not None and _pairing["expires_at"] > time.time()


def active_setup_url() -> str | None:
    with _lock:
        if _pairing is None or _pairing["expires_at"] <= time.time():
            return None
        code = _pairing["code"]
    return f"{setup_info()['base_url']}/phone/setup?code={code}"


def claim_pairing(code: str, name: str, platform: str = "") -> dict:
    """Trade a valid pairing code for a device token (returned exactly once)."""
    global _pairing
    if not settings.phone.enabled:
        return {"ok": False, "error": "phone channel disabled"}
    code = (code or "").strip()
    with _lock:
        if _pairing is None or _pairing["expires_at"] <= time.time():
            _pairing = None
            return {"ok": False, "error": "no active pairing — start one on the desktop"}
        if not code or not hmac.compare_digest(code, _pairing["code"]):
            _pairing["attempts"] += 1
            if _pairing["attempts"] >= settings.phone.max_claim_attempts:
                _pairing = None
                return {"ok": False, "error": "too many wrong codes — pairing cancelled"}
            return {"ok": False, "error": "wrong code"}
        # Check capacity BEFORE consuming the code so a full registry does not
        # burn the offer (and hold the lock for the full registry RMW).
        devices = _load_devices()
        if len(devices) >= settings.phone.max_devices:
            return {"ok": False,
                    "error": f"device limit reached ({settings.phone.max_devices})"}
        _pairing = None  # single-use: claimed
        device_id = uuid.uuid4().hex[:12]
        token = secrets.token_urlsafe(32)
        devices[device_id] = {
            "name": (name or "phone").strip()[:60] or "phone",
            "platform": (platform or "").strip().lower()[:20],
            "token_sha256": _hash(token),
            "created_at": time.time(),
            "last_seen": None,
            "last_kind": "",
            "events": 0,
        }
        _save_devices(devices)
        name_out = devices[device_id]["name"]
    return {"ok": True, "device_id": device_id, "name": name_out, "token": token}


# --- authentication ---------------------------------------------------------
def authenticate(authorization: str | None) -> dict | None:
    """Resolve a `Bearer <token>` header to a device record, or None."""
    if not settings.phone.enabled or not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    digest = _hash(parts[1].strip())
    with _lock:
        devices = _load_devices()
    for device_id, rec in devices.items():
        stored = rec.get("token_sha256", "")
        if stored and hmac.compare_digest(stored, digest):
            return {"device_id": device_id, **rec}
    return None


# --- ingest -----------------------------------------------------------------
def _location_text(meta: dict) -> str:
    place = str(meta.get("place") or "").strip()
    lat, lon = meta.get("lat"), meta.get("lon")
    coords = f"({lat}, {lon})" if lat is not None and lon is not None else ""
    return " ".join(x for x in (place, coords) if x)


def ingest(device: dict, payload: dict) -> dict:
    """Turn one authenticated phone payload into an Event on the bus.

    Shape: {kind: one of KINDS, text: str, meta?: {url/title/app/lat/lon/...}}.
    Returns {ok, kind, source, summary} or {ok: False, error}.
    """
    if not isinstance(payload, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    kind = str(payload.get("kind") or "note").strip().lower()
    if kind not in KINDS:
        return {"ok": False, "error": f"unknown kind {kind!r} (one of {', '.join(KINDS)})"}
    raw_meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
    meta = {k: raw_meta[k] for k in _META_KEYS if k in raw_meta}
    text = str(payload.get("text") or "").strip()
    if kind == "location" and not text:
        text = _location_text(meta)
    if not text:
        return {"ok": False, "error": "empty text"}
    text = text[: settings.phone.max_text_chars]

    now = time.time()
    source = f"phone.{kind}"
    summary = f"[phone:{kind}] {text[:200]}"
    ev = Event(
        time=now, modality=Modality.SYSTEM, raw=text, summary=summary,
        source=source,
        meta={"origin": "phone", "device_id": device.get("device_id", ""),
              "device": device.get("name", ""),
              "platform": device.get("platform", ""), "kind": kind, **meta},
    )
    # note/voice: the user deliberately told Sparrow this — human-said-so tier.
    # share/clipboard/location/other: captured content, observed only. Either
    # way it is memory CONTEXT; nothing here carries action authority.
    _conf.attach(ev, _conf.ACCEPTED if kind in _ACCEPTED_KINDS else _conf.OBSERVED)
    bus.publish_nowait(ev)

    try:
        with _lock:
            devices = _load_devices()
            rec = devices.get(device.get("device_id", ""))
            if rec is not None:
                rec["last_seen"] = now
                rec["last_kind"] = kind
                rec["events"] = int(rec.get("events") or 0) + 1
                _save_devices(devices)
    except OSError:
        pass  # bookkeeping only; never fail the ingest over it
    print(f"[phone] {device.get('name', '?')}: {summary}")
    return {"ok": True, "kind": kind, "source": source, "summary": summary}


# --- photo ingest (phone -> the vision pipeline) ----------------------------
def _downscale_jpeg(data: bytes, max_edge: int = 1568) -> bytes:
    """Re-encode to a VLM-friendly JPEG (Claude's sweet spot is ~1568px long
    edge). Best-effort: without Pillow, or on any decode error, pass through."""
    try:
        import io

        from PIL import Image
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        scale = min(1.0, max_edge / float(max(w, h)))
        if scale < 1.0:
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return data


def _parse_taken_at(value: str | float | None) -> float | None:
    """A phone-supplied capture time -> epoch seconds. Accepts a Unix timestamp
    (s or ms) or an ISO 8601 string; returns None if unusable so the caller
    falls back to now()."""
    if value is None or value == "":
        return None
    try:
        num = float(value)
        return num / 1000.0 if num > 1e11 else num   # ms -> s if it looks like ms
    except (TypeError, ValueError):
        pass
    try:
        import datetime as _dt
        s = str(value).strip().replace("Z", "+00:00")
        return _dt.datetime.fromisoformat(s).timestamp()
    except (ValueError, OSError):
        return None


def ingest_photo(device: dict, image_bytes: bytes, *, caption: str = "",
                 content_type: str = "", taken_at: str | float | None = None,
                 lat: float | None = None, lon: float | None = None,
                 publish=None) -> dict:
    """Save a phone photo, describe it with the VLM, land it as a VISION memory
    event (source=phone.photo). Same path a webcam frame takes, so OCR/notebook/
    whiteboard snapshots become searchable. Opt-in per photo (the phone sends it);
    iOS prompts for photo permission the first time the shortcut runs.

    `taken_at` (when the photo was captured) times the event on the real
    timeline — so sharing an OLD photo lands at its date, not at upload time;
    `lat`/`lon` record where it was taken."""
    if not settings.phone.enabled:
        return {"ok": False, "error": "phone channel disabled"}
    if not image_bytes:
        return {"ok": False, "error": "empty image"}
    if len(image_bytes) > settings.phone.max_photo_bytes:
        return {"ok": False, "error": f"image too large "
                f"({len(image_bytes)} bytes > {settings.phone.max_photo_bytes})"}
    low_ct = (content_type or "").lower()
    if "heic" in low_ct or "heif" in low_ct:
        return {"ok": False, "error": "HEIC/HEIF isn't readable — add a "
                "'Convert Image' (to JPEG) action before uploading"}
    publish = publish or bus.publish_nowait
    upload_at = time.time()
    now = _parse_taken_at(taken_at) or upload_at
    jpeg = _downscale_jpeg(image_bytes)
    photos_dir = Path(settings.phone.photos_dir)
    try:
        photos_dir.mkdir(parents=True, exist_ok=True)
        # Filename uses UPLOAD time so a batch of old shared photos can't collide
        # on a shared capture second; the event time below uses `now` (taken_at).
        path = photos_dir / f"{upload_at:.3f}.jpg"
        path.write_bytes(jpeg)
    except OSError as exc:
        return {"ok": False, "error": f"could not save image ({exc})"}

    description = ocr = ""
    entities: list = []
    model_conf = None
    vlm_meta: dict = {}
    try:
        from app.services.vlm import vlm
        res = vlm.describe(jpeg, context={"frame_path": str(path),
                                          "source": "phone.photo",
                                          "modality": "vision"})
        description = res.get("description", "") or ""
        ocr = res.get("ocr_text", "") or ""
        entities = list(res.get("objects", []) or [])
        model_conf = res.get("confidence")
        vlm_meta = res
    except Exception as exc:
        print(f"[phone] photo VLM unavailable ({exc}); saved without description.")

    caption = (caption or "").strip()[: settings.phone.max_text_chars]
    raw = ocr or description or "[photo]"
    head = caption or description or "photo"
    summary = f"[phone:photo] {head}"[:200]
    meta = {"origin": "phone", "device_id": device.get("device_id", ""),
            "device": device.get("name", ""), "kind": "photo",
            "frame_path": str(path), "caption": caption, "vision": vlm_meta,
            "uploaded_at": upload_at}
    if _parse_taken_at(taken_at) is not None:
        meta["taken_at"] = now
    if lat is not None and lon is not None:
        meta["lat"], meta["lon"] = lat, lon
    ev = Event(
        time=now, modality=Modality.VISION, raw=raw, summary=summary,
        source="phone.photo", entities=entities, meta=meta)
    # A phone photo is model-EXTRACTED from an observed image, exactly like a
    # webcam frame — capture quality unknown here, model confidence from the VLM.
    _conf.attach(ev, _conf.EXTRACTED,
                 model=(float(model_conf) if isinstance(model_conf, (int, float))
                        else None))
    publish(ev)

    try:
        with _lock:
            devices = _load_devices()
            rec = devices.get(device.get("device_id", ""))
            if rec is not None:
                rec["last_seen"] = upload_at   # when they interacted, not the photo's age
                rec["last_kind"] = "photo"
                rec["events"] = int(rec.get("events") or 0) + 1
                _save_devices(devices)
    except OSError:
        pass
    print(f"[phone] {device.get('name', '?')}: photo -> {head[:80]}")
    return {"ok": True, "summary": summary,
            "description": description or ocr, "path": str(path)}


# --- desktop-side management ------------------------------------------------
def devices() -> list[dict]:
    """Registry rows for the UI — no token hashes leave this module."""
    out = []
    with _lock:
        registry = _load_devices()
    for device_id, rec in sorted(registry.items(),
                                 key=lambda kv: kv[1].get("created_at") or 0):
        out.append({
            "device_id": device_id,
            "name": rec.get("name", "?"),
            "platform": rec.get("platform", ""),
            "created_at": rec.get("created_at"),
            "last_seen": rec.get("last_seen"),
            "last_kind": rec.get("last_kind", ""),
            "events": int(rec.get("events") or 0),
        })
    return out


def revoke(device_id: str) -> bool:
    """Forget a device — its token stops working immediately."""
    with _lock:
        registry = _load_devices()
        if device_id not in registry:
            return False
        del registry[device_id]
        _save_devices(registry)
        return True


# --- outbox (Sparrow -> phone, pull-based) -----------------------------------
# The mirror image of ingest, built for a phone with NO extra apps: Sparrow
# queues outbound items here; a native Shortcuts recipe ("Check Sparrow") drains
# them with the same device token — via Siri, a tap, or an iOS automation — and
# executes each with built-in actions (Show Notification, Add Reminder, ...).
# Trust mirror: only Sparrow-side code/UI can ENQUEUE (the desktop is the
# decider); a device token can only READ ITS OWN queue (the phone is the
# executor). Anything consequential must pass the normal approval gates before
# it is ever queued. "query" asks the phone a question — the shortcut's own
# If-branches are the allowlist of what it will answer (kind="data" ingest,
# meta.reply_to = the query item's id), so the PHONE decides what is readable.
OUTBOX_KINDS = ("notify", "reminder", "url", "query", "other")


def _load_outbox() -> list[dict]:
    try:
        data = json.loads(Path(settings.phone.outbox_path).read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, list) else []
    except (FileNotFoundError, ValueError, OSError):
        return []


def _save_outbox(items: list[dict]) -> None:
    # Keep every pending item; prune only delivered history beyond the cap.
    keep = settings.phone.outbox_history
    pending = [i for i in items if i.get("status") == "pending"]
    delivered = [i for i in items if i.get("status") != "pending"][-keep:]
    from app.atomic_json import write_json
    write_json(Path(settings.phone.outbox_path),
               {"items": delivered + pending})


def queue_outbox(kind: str, text: str, *, device_id: str | None = None,
                 meta: dict | None = None) -> dict:
    """Queue one item for the phone(s) to pick up. Desktop-side only.

    `device_id=None` targets whichever paired device drains first (the common
    single-phone case); a specific id pins it to that device.
    """
    if not settings.phone.enabled:
        return {"ok": False, "error": "phone channel disabled"}
    kind = (kind or "notify").strip().lower()
    if kind not in OUTBOX_KINDS:
        return {"ok": False,
                "error": f"unknown kind {kind!r} (one of {', '.join(OUTBOX_KINDS)})"}
    text = (text or "").strip()[: settings.phone.max_text_chars]
    if not text:
        return {"ok": False, "error": "empty text"}
    if device_id and device_id not in _load_devices():
        return {"ok": False, "error": "unknown device"}
    with _lock:
        items = _load_outbox()
        scope = [i for i in items if i.get("status") == "pending"
                 and i.get("device_id") in (None, device_id)]
        if len(scope) >= settings.phone.max_outbox_pending:
            return {"ok": False, "error": "outbox full — drain the phone first"}
        item = {"id": uuid.uuid4().hex[:12], "kind": kind, "text": text,
                "meta": {k: (meta or {})[k] for k in _META_KEYS if k in (meta or {})},
                "device_id": device_id, "created_at": time.time(),
                "status": "pending", "delivered_to": None, "delivered_at": None}
        items.append(item)
        _save_outbox(items)
    return {"ok": True, "item": {k: item[k] for k in
                                 ("id", "kind", "text", "device_id", "created_at")}}


def outbox_pending(device_id: str | None = None) -> list[dict]:
    """Pending items (optionally scoped to one device's view) — for the UI."""
    out = []
    for i in _load_outbox():
        if i.get("status") != "pending":
            continue
        if device_id and i.get("device_id") not in (None, device_id):
            continue
        out.append({k: i.get(k) for k in
                    ("id", "kind", "text", "device_id", "created_at")})
    return out


def drain_outbox(device: dict, *, peek: bool = False) -> dict:
    """Hand an authenticated device its pending items; mark them delivered.

    `peek=True` returns without marking (for testing a shortcut safely).
    Response shape is deliberately Shortcuts-friendly: {count, items:[...]}.
    """
    device_id = device.get("device_id", "")
    now = time.time()
    with _lock:
        items = _load_outbox()
        mine = [i for i in items if i.get("status") == "pending"
                and i.get("device_id") in (None, device_id)]
        if not peek:
            for i in mine:
                i["status"] = "delivered"
                i["delivered_to"] = device_id
                i["delivered_at"] = now
            if mine:
                _save_outbox(items)
    if mine and not peek:
        print(f"[phone] outbox: {len(mine)} item(s) -> {device.get('name', '?')}")
    return {"ok": True, "count": len(mine),
            "items": [{"id": i["id"], "kind": i["kind"], "text": i["text"],
                       "meta": i.get("meta") or {}} for i in mine]}


def sync_exchange(device: dict, payload: dict | None) -> dict:
    """One round-trip for the unified "sparrow" shortcut: optionally INGEST the
    payload (if it carries text), then always DRAIN this device's outbox.

    Lets a single Shortcuts action do both directions — run it empty (from an
    automation) and it just receives; run it with text (Siri / share sheet) and
    it sends and receives in the same call. Same auth + trust rules as the split
    endpoints; this only combines them."""
    payload = payload or {}
    sent = False
    text = str(payload.get("text") or "").strip()
    if text:
        res = ingest(device, payload)     # defaults kind='note'
        sent = bool(res.get("ok"))
    drained = drain_outbox(device)
    return {"ok": True, "sent": sent,
            "count": drained["count"], "items": drained["items"]}


def qr_svg(data: str) -> str | None:
    """Render `data` as an SVG QR (None if the qrcode lib is unavailable)."""
    try:
        import io

        import qrcode
        import qrcode.image.svg
        img = qrcode.make(data, image_factory=qrcode.image.svg.SvgPathImage,
                          box_size=12)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        return svg[svg.index("<svg"):]  # strip the XML prolog for inlining
    except Exception as exc:
        print(f"[phone] QR render unavailable ({exc}); showing the URL instead.")
        return None


def status() -> dict:
    """One snapshot for pages: devices + reachability + pairing state."""
    return {
        "enabled": settings.phone.enabled,
        "devices": devices(),
        "pairing_active": pairing_active(),
        "outbox_pending": outbox_pending(),
        **setup_info(),
    }
