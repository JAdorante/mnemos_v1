"""Sparrow -> firm relay: registration, signed sends, and the outbox.

Registration reuses the coordinator's RegisterIn flow (POST /register) to get
a node token, then enrolls for fleet traffic (POST /relay/enroll), handing the
relay an inbound token it will present when forwarding signals to us. If this
Sparrow is already an org-network node on the same coordinator, that node's
token is reused instead of minting a second identity.

Every send: hops + 1, egress validation, HMAC with envelope.link_key(node
token), POST /relay/publish. A network failure or a 5xx queues the signal in
the outbox for retry; a 4xx is the relay refusing it, so it is not retried.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from urllib import error, request

from app.config import settings
from app.services.fleet import _files, kinds, state
from app.services.fleet import envelope as env
from app.services.fleet import router as fleet_router

_retry_started = False
_retry_lock = threading.Lock()
RETRY_INTERVAL_S = 30.0
MAX_OUTBOX = 1000


def _post(url: str, body: dict, token: str = "") -> tuple[int, dict]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = request.Request(url, data=json.dumps(body).encode("utf-8"),
                          headers=headers, method="POST")
    try:
        with request.urlopen(req, timeout=settings.fleet.http_timeout_s) as r:
            raw = r.read().decode("utf-8")
            return r.status, (json.loads(raw) if raw else {})
    except error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:
            detail = {}
        return exc.code, detail if isinstance(detail, dict) else {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _default_fleet_url() -> str:
    try:
        from app.services import peer_channel
        return peer_channel.my_internal_url() or peer_channel.my_base_url()
    except Exception:
        return f"http://{settings.host}:{settings.port}"


def register(relay_url: str, node_id: str, *, display_name: str = "",
             fleet_url: str = "") -> dict:
    relay_url = (relay_url or settings.fleet.relay_url).rstrip("/")
    if not relay_url or not node_id:
        return {"ok": False, "error": "relay_url and node_id are required"}
    token = ""
    try:
        from app.services import org_client
        st = org_client._state()
        if (st.get("token") and st.get("node_id") == node_id and
                (st.get("coordinator_url") or "").rstrip("/") == relay_url):
            token = st["token"]
    except Exception:
        token = ""
    if not token:
        prev = state.relay()
        if prev.get("node_id") == node_id and prev.get("url") == relay_url:
            token = prev.get("token") or ""
    if not token:
        code, res = _post(f"{relay_url}/register", {
            "node_id": node_id, "display_name": display_name or node_id,
            "role": "ic", "base_url": fleet_url or _default_fleet_url()})
        if code != 200 or not res.get("token"):
            return {"ok": False, "error": f"register failed ({code})",
                    "detail": res}
        token = res["token"]
    inbound = "fi_" + secrets.token_urlsafe(32)
    code, res = _post(f"{relay_url}/relay/enroll", {
        "fleet_url": fleet_url or _default_fleet_url(),
        "inbound_token": inbound}, token=token)
    if code != 200:
        return {"ok": False, "error": f"enroll failed ({code})", "detail": res}
    state.set_relay(url=relay_url, node_id=node_id, token=token,
                    inbound_token_sha256=env.link_key(inbound),
                    registered_at=time.time())
    return {"ok": True, "node_id": node_id, "relay_url": relay_url,
            "topics": res.get("topics", [])}


def prepare(signal: dict) -> dict:
    """The exact bytes that go on the wire: hop + 1, validated, signed."""
    out = fleet_router.outbound_copy(signal)
    env.validate(out, kinds=kinds.registry(),
                 max_hops=settings.fleet.max_hops, outbound=True)
    return env.sign(out, env.link_key(state.relay().get("token") or ""))


def send(signal: dict) -> dict:
    relay = state.relay()
    url = state.relay_url()
    if not relay.get("token") or not url:
        return {"ok": False, "error": "no relay registered"}
    try:
        wire = prepare(signal)
    except env.SignalError as exc:
        return {"ok": False, "error": exc.code, "reason": exc.reason}
    code, res = _post(f"{url}/relay/publish", {"signal": wire},
                      token=relay["token"])
    if code == 200 and res.get("ok"):
        return {"ok": True, "relay": res}
    if code == 0 or code >= 500:
        _enqueue(signal, f"{code}: {res.get('error') or res.get('detail')}")
        ensure_retry_loop()
        return {"ok": False, "queued": True, "error": f"relay unreachable ({code})"}
    return {"ok": False, "error": f"relay refused ({code})",
            "detail": res.get("detail") or res}


def send_async(signal: dict) -> None:
    if os.environ.get("QUILL_FLEET_SEND_SYNC") in ("1", "true", "True"):
        send(signal)
        return
    threading.Thread(target=send, args=(signal,), name="fleet-send",
                     daemon=True).start()


# --- outbox ------------------------------------------------------------------------
def _outbox() -> list:
    data = _files.load(settings.fleet.outbox_path, [])
    return data if isinstance(data, list) else []


def _enqueue(signal: dict, why: str) -> None:
    with _files.lock:
        box = _outbox()
        if any(r.get("signal", {}).get("origin_id") == signal.get("origin_id")
               for r in box):
            return
        box.append({"signal": signal, "queued_at": time.time(),
                    "attempts": 0, "last_error": why})
        _files.save(settings.fleet.outbox_path, box[-MAX_OUTBOX:])


def outbox() -> list[dict]:
    return _outbox()


def drain_outbox() -> dict:
    """Retry queued sends once each. Expired signals are dropped, not sent."""
    with _files.lock:
        box = _outbox()
        _files.save(settings.fleet.outbox_path, [])
    sent = dropped = 0
    keep = []
    now = time.time()
    for row in box:
        sig = row.get("signal") or {}
        if float(sig.get("expires_at") or 0) <= now:
            dropped += 1
            continue
        relay = state.relay()
        url = state.relay_url()
        if not relay.get("token") or not url:
            keep.append(row)
            continue
        try:
            wire = prepare(sig)
        except env.SignalError:
            dropped += 1
            continue
        code, res = _post(f"{url}/relay/publish", {"signal": wire},
                          token=relay["token"])
        if code == 200 and res.get("ok"):
            sent += 1
        elif code == 0 or code >= 500:
            row["attempts"] = int(row.get("attempts") or 0) + 1
            row["last_error"] = f"{code}"
            keep.append(row)
        else:
            dropped += 1
    if keep:
        with _files.lock:
            box = _outbox()
            _files.save(settings.fleet.outbox_path, (keep + box)[-MAX_OUTBOX:])
    return {"sent": sent, "dropped": dropped, "pending": len(keep)}


def ensure_retry_loop() -> None:
    global _retry_started
    with _retry_lock:
        if _retry_started:
            return
        _retry_started = True

    def _loop() -> None:
        while True:
            time.sleep(RETRY_INTERVAL_S)
            try:
                if _outbox():
                    drain_outbox()
            except Exception as exc:
                print(f"[fleet] outbox drain skipped ({exc}).")

    threading.Thread(target=_loop, name="fleet-outbox", daemon=True).start()
