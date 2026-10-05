"""The firm relay's publish and forward path (fleet federation, Phase 4).

POST /relay/publish, in order — every refusal is logged, so compliance sees
attempts as well as deliveries:
  1. authenticate the node (auth.require_node)
  2. verify the sender's HMAC (key = the node token's SHA-256, which is what
     the directory already stores; see envelope.link_key)
  3. re-validate the envelope with the egress rules (registered kind and its
     body schema, forbidden fields, internal_ok licences, hop cap, expiry)
  4. check the blocked-subjects list (missing or malformed refuses
     everything); the kind must be in the relay's own registry
  5. check the topic barrier for the sender
  6. append to the chained log, then forward to each permitted recipient

Forwarding POSTs kind="signal" to the recipient Sparrow's /peer/ask with the
inbound token that Sparrow handed us at enrollment, re-signed for that link.
The signal's bytes are otherwise forwarded exactly as received. A failed
delivery is queued per recipient and retried with backoff, mirroring
team_layer's mailbox; one that expires undelivered is logged and dropped.

The relay is trusted for routing, not for content: every recipient verifies
the signature and the envelope again on arrival.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from urllib import error, request

from app.services.fleet import envelope as env
from org_coordinator import relay_log, store, topics

_queue_lock = threading.RLock()
_retry_started = False
MAX_QUEUE = 5000
BACKOFF_S = (5, 15, 60, 300, 900)


def max_hops() -> int:
    return int(os.environ.get("QUILL_RELAY_MAX_HOPS", "2"))


def _timeout() -> float:
    return float(os.environ.get("QUILL_RELAY_TIMEOUT_S", "10"))


# --- enrollment links (kept out of the directory on purpose) ------------------------
# /directory returns every node record to every node, so a forwarding token
# stored there would let any node impersonate the relay to its peers.
def _links() -> dict:
    data = store._load("relay_links.json", {})
    return data if isinstance(data, dict) else {}


def enroll(node_id: str, fleet_url: str, inbound_token: str) -> dict:
    fleet_url = (fleet_url or "").strip().rstrip("/")
    if not fleet_url.startswith(("http://", "https://")):
        raise ValueError("fleet_url must be an http(s) URL")
    if len(inbound_token or "") < 24:
        raise ValueError("inbound_token is too short")
    with store._lock:
        links = _links()
        links[node_id] = {"fleet_url": fleet_url,
                          "inbound_token": inbound_token,
                          "enrolled_at": time.time()}
        store._save("relay_links.json", links)
    return {"node_id": node_id, "fleet_url": fleet_url}


def link(node_id: str) -> dict | None:
    return _links().get(node_id)


# --- blocked subjects + kinds (admin-managed, fail closed) -----------------------------
def _write_json(p: Path, body: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(body, indent=2), encoding="utf-8")
    tmp.replace(p)


def blocked_path() -> Path:
    return Path(os.environ.get("QUILL_RELAY_BLOCKED",
                               str(store.data_dir() / "blocked.json")))


def load_blocked() -> env.Blocklist:
    """Missing or malformed raises, and publish refuses everything (503)."""
    p = blocked_path()
    if not p.is_file():
        raise ValueError("blocked list missing")
    return env.load_blocklist(json.loads(p.read_text(encoding="utf-8")))


def save_blocked(subjects: list[str], patterns: list[str] | None = None, *,
                 updated_by: str = "") -> dict:
    body = {"subjects": sorted({str(i).strip() for i in subjects
                                if str(i).strip()}),
            "patterns": [str(x) for x in patterns or []],
            "updated_at": time.time(), "updated_by": updated_by}
    env.load_blocklist(body)  # validate before writing
    _write_json(blocked_path(), body)
    relay_log.append("blocked_list_updated", sender=updated_by or "admin",
                     recipients=[], count=len(body["subjects"]),
                     patterns=len(body["patterns"]))
    return body


def kinds_path() -> Path:
    return Path(os.environ.get("QUILL_RELAY_KINDS",
                               str(store.data_dir() / "fleet_kinds.json")))


def load_kinds() -> dict:
    """The relay's registry. A malformed file means built-ins only, so a
    custom kind is refused rather than checked loosely."""
    p = kinds_path()
    if not p.is_file():
        return env.load_kinds(None)
    try:
        return env.load_kinds(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"[relay] fleet_kinds.json ignored ({exc}); built-ins only.")
        return env.load_kinds(None)


def save_kinds(kinds: dict, *, updated_by: str = "") -> dict:
    body = {"kinds": kinds}
    env.load_kinds(body)
    _write_json(kinds_path(), {**body, "updated_at": time.time(),
                               "updated_by": updated_by})
    relay_log.append("kinds_updated", sender=updated_by or "admin",
                     recipients=[], kinds=sorted(kinds))
    return load_kinds()


# --- relay-side replay guard ---------------------------------------------------------------
def _seen_first(origin_id: str, expires_at: float) -> bool:
    now = time.time()
    with store._lock:
        seen = store._load("relay_seen.json", {})
        seen = {k: v for k, v in seen.items() if float(v) > now}
        if origin_id in seen:
            return False
        seen[origin_id] = float(expires_at) + 600
        store._save("relay_seen.json", seen)
    return True


# --- publish --------------------------------------------------------------------------------
def publish(node: dict, body) -> tuple[int, dict]:
    sender = node["node_id"]
    signal = body.get("signal") if isinstance(body, dict) else None
    if not isinstance(signal, dict):
        return 422, {"ok": False, "error": "body.signal must be an object"}

    def refuse(status: int, code: str, reason: str = "") -> tuple[int, dict]:
        relay_log.append("refused", sender=sender, recipients=[],
                         signal=signal, reason=code, detail=reason[:300])
        return status, {"ok": False, "error": code, "reason": reason}

    if not env.verify(signal, node.get("token_sha256") or ""):
        return refuse(403, "bad_signature")
    try:
        sig = env.validate(signal, kinds=load_kinds(), max_hops=max_hops(),
                           outbound=True)
    except env.SignalError as exc:
        return refuse(422, exc.code, exc.reason)
    try:
        blocked = load_blocked()
    except Exception as exc:
        return refuse(503, "blocked_list_unavailable", str(exc))
    if blocked.blocks(sig.subject):
        return refuse(403, "blocked_subject", sig.subject or "")
    if not topics.allowed(sender, sig.topic):
        return refuse(403, "barrier", f"{sender} may not publish {sig.topic}")
    if not _seen_first(sig.origin_id, sig.expires_at):
        return 200, {"ok": True, "duplicate": True}

    targets = [r for r in topics.recipients(sig.topic, sender) if link(r)]
    row = relay_log.append("forward", sender=sender, recipients=targets,
                           signal=signal)
    for r in targets:
        _dispatch(r, signal, sender)
    return 200, {"ok": True, "seq": row["seq"], "recipients": targets}


def _dispatch(recipient: str, signal: dict, sender: str) -> None:
    if os.environ.get("QUILL_RELAY_FORWARD_SYNC") in ("1", "true", "True"):
        _deliver_or_queue(recipient, signal, sender)
        return
    threading.Thread(target=_deliver_or_queue,
                     args=(recipient, signal, sender),
                     name="relay-forward", daemon=True).start()


def _post_signal(recipient: str, signal: dict, sender: str) -> int:
    """HTTP status of one delivery attempt (0 = network failure)."""
    ln = link(recipient)
    if not ln:
        return 410
    wire = env.sign({k: v for k, v in signal.items() if k != "sig"},
                    env.link_key(ln["inbound_token"]))
    body = {"kind": "signal", "ask_id": signal.get("origin_id"),
            "signal": wire, "sender_node": sender}
    req = request.Request(
        f"{ln['fleet_url']}/peer/ask", data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {ln['inbound_token']}"},
        method="POST")
    try:
        with request.urlopen(req, timeout=_timeout()) as resp:
            return resp.status
    except error.HTTPError as exc:
        return exc.code
    except Exception:
        return 0


def _deliver_or_queue(recipient: str, signal: dict, sender: str) -> None:
    code = _post_signal(recipient, signal, sender)
    if 200 <= code < 300:
        return
    if code == 0 or code >= 500 or code == 404:
        # 404 covers a Sparrow restarting with the fleet flag still coming up.
        _enqueue(recipient, signal, sender, f"HTTP {code}")
        ensure_retry_loop()
        return
    relay_log.append("delivery_refused", sender=sender,
                     recipients=[recipient], signal=signal,
                     reason=f"HTTP {code}")


# --- per-recipient retry queue ------------------------------------------------------------
def _load_queue() -> list:
    data = store._load("relay_queue.json", [])
    return data if isinstance(data, list) else []


def _enqueue(recipient: str, signal: dict, sender: str, why: str) -> None:
    with _queue_lock:
        q = _load_queue()
        key = (recipient, signal.get("origin_id"))
        if any((r["recipient"], r["signal"].get("origin_id")) == key for r in q):
            return
        q.append({"recipient": recipient, "sender": sender, "signal": signal,
                  "attempts": 0, "next_at": time.time() + BACKOFF_S[0],
                  "last_error": why, "queued_at": time.time()})
        store._save("relay_queue.json", q[-MAX_QUEUE:])


def queue() -> list[dict]:
    with _queue_lock:
        return [{k: v for k, v in r.items() if k != "signal"}
                | {"origin_id": r["signal"].get("origin_id")}
                for r in _load_queue()]


def drain(*, force: bool = False) -> dict:
    """One pass over the queue. `force` ignores backoff (admin / tests)."""
    now = time.time()
    with _queue_lock:
        q = _load_queue()
        store._save("relay_queue.json", [])
    delivered = expired = 0
    keep = []
    for r in q:
        sig = r["signal"]
        if float(sig.get("expires_at") or 0) <= now:
            relay_log.append("delivery_failed", sender=r["sender"],
                             recipients=[r["recipient"]], signal=sig,
                             reason=f"expired undelivered ({r['last_error']})")
            expired += 1
            continue
        if not force and r["next_at"] > now:
            keep.append(r)
            continue
        code = _post_signal(r["recipient"], sig, r["sender"])
        if 200 <= code < 300:
            delivered += 1
            continue
        if code == 0 or code >= 500 or code == 404:
            r["attempts"] += 1
            r["last_error"] = f"HTTP {code}"
            r["next_at"] = now + BACKOFF_S[min(r["attempts"],
                                               len(BACKOFF_S) - 1)]
            keep.append(r)
        else:
            relay_log.append("delivery_refused", sender=r["sender"],
                             recipients=[r["recipient"]], signal=sig,
                             reason=f"HTTP {code}")
    if keep:
        with _queue_lock:
            store._save("relay_queue.json", (keep + _load_queue())[-MAX_QUEUE:])
    return {"delivered": delivered, "expired": expired, "pending": len(keep)}


def ensure_retry_loop(interval_s: float = 5.0) -> None:
    global _retry_started
    with _queue_lock:
        if _retry_started:
            return
        _retry_started = True

    def _loop() -> None:
        while True:
            time.sleep(interval_s)
            try:
                if _load_queue():
                    drain()
            except Exception as exc:
                print(f"[relay] retry pass skipped ({exc}).")

    threading.Thread(target=_loop, name="relay-retry", daemon=True).start()
