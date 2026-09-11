"""Peer channel — Sparrow <-> Sparrow, for teams (Phase 1 walking skeleton).

Everyone on a team runs their own Sparrow. This channel lets two instances pair
and exchange bounded, typed messages so one user's assistant can ask another's
a question — answered from the OTHER user's memory, by THEIR models, behind
THEIR consent. Raw memory never crosses the wire; only composed, redacted
answers do.

Deliberately modeled on phone_channel.py (same trust primitives, hardened the
same way), with one structural difference: pairing is MUTUAL. The claimer
sends a token it minted for us alongside the claim, and receives ours back —
one round trip leaves both sides able to authenticate the other.

Flow (A = answerer/desktop that started pairing, B = joiner):
  A: start_pairing()            -> short-lived 6-digit code, plus the same code
                                   and address packed into ONE pasteable invite
                                   (`sparrow://pair/...`, see make_invite) so B
                                   never retypes a hostname.
  B: join(a_url, code)          -> POST /peer/pair/claim on A with B's name,
                                   base_url, and a token B minted for A.
                                   A returns a token for B. Both store records.
  B: ask(peer_id, question)     -> POST /peer/ask on A (Bearer B's token)
  A: handle_ask()               -> disclosure gate:
       * default ("offer"): queue for A's human; approve composes the answer
         (llm.answer -> the same grounded, no-action-authority path chat uses
         with the agent off), redacts it, and POSTs it back to B /peer/answer.
       * QUILL_PEER_AUTO_ANSWER=1 ("auto", dev/sim only): compose + redact +
         return the answer synchronously.

Trust model:
  * pairing needs the code (spoken/messaged human-to-human), is single-use,
    expires, and locks after a few wrong attempts.
  * tokens are per-peer: what we ACCEPT is stored SHA-256 only; what we
    PRESENT is stored plaintext (it is a credential to someone else's server,
    like any stored OAuth token — the known plaintext-at-rest posture applies).
  * inbound asks/answers are CONTEXT, never command authority — they land as
    observed-tier events; nothing here reaches an execution surface.
  * every outbound answer passes redact.py; the default posture is that a
    human approves each disclosure ("offer" mode). Fact-class allow policies
    arrive in Phase 2 — this module only ever narrows from "ask the human".
  * an inbound /peer/answer must match an ask WE sent to THAT peer, else it
    is refused — a peer cannot inject unsolicited "answers" into memory.

Phase 2 (here): the disclosure policy. Each peer carries a per-class action
map (availability / work / contact / personal / other -> auto | offer | deny).
Default is all-"offer" — exactly Phase 1's posture. "auto" is an explicit,
per-peer, per-class grant the user makes in the /peer UI; "deny" declines
without interrupting the human. A question is classified by the LOCAL model
(schema-enforced, via the ModelRouter); any classifier failure or doubt falls
back to "offer" — the gate never widens on uncertainty, and the `personal`
class can never be set to auto (enforced at write AND at enforcement time).

Phase 3 (partial): peer <-> Person linking is USER-ASSERTED only (no auto-mint
on pair — junk-people risk). Disclosure stays keyed by peer_id. Chat "ask Name:"
may match linked Person aliases only when that person maps to a still-paired peer.

Addressing: a peer record can hold TWO addresses. `base_url` is the public one
(tunnel / tailnet); `internal_url` is optional and only advertised by instances
that share a private network with us (containers on one box, a LAN). Outbound
calls go through _post_peer, which tries the internal address first and falls
back to the public one — so on-box pairs survive a tunnel hostname change and
their traffic never leaves the machine.

Team layer (services/team_layer.py): presence pings + offline mailbox, relationship
policy packs, named peer groups (`ask #platform:`), shared loop IDs on handoffs,
and meeting-attendee pairing offers. Still no shared memory.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from app.config import settings
from app.events import Event, Modality, bus
from app.services import confidence as _conf

_lock = threading.Lock()
# The one active pairing offer: {"code", "expires_at", "attempts"}.
_pairing: dict[str, Any] | None = None


# --- registry (mirrors phone_channel's device registry) ----------------------
def _peers_path() -> Path:
    return Path(settings.peer.peers_path)


def _asks_path() -> Path:
    return Path(settings.peer.asks_path)


def _sent_path() -> Path:
    return Path(settings.peer.sent_path)


def _load(path: Path, default):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, type(default)) else default
    except (FileNotFoundError, ValueError, OSError):
        return default


def _save(path: Path, data) -> None:
    from app.atomic_json import write_json
    write_json(path, data)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def instance_name() -> str:
    """How this instance introduces itself to peers. QUILL_PEER_NAME wins;
    else the onboarding profile's user name; else the hostname."""
    import os
    env = (os.environ.get("QUILL_PEER_NAME") or "").strip()
    if env:
        return env[:60]
    try:
        from app.services.identity import user_identity
        name = str(user_identity().get("name") or "").strip()
        if name:
            return name[:60]
    except Exception:
        pass
    return socket.gethostname()[:60]


def lan_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def my_base_url() -> str:
    """The URL a peer should call us back on. QUILL_PEER_BASE_URL overrides
    (tailnet / reverse-proxy); else LAN ip + our port."""
    import os
    env = (os.environ.get("QUILL_PEER_BASE_URL") or "").strip().rstrip("/")
    return env or f"http://{lan_ip()}:{settings.port}"


def my_internal_url() -> str:
    """A second address for peers that share our private network — the compose
    network on a hosted box, a LAN. Peers try it BEFORE the public URL, so an
    on-box pair keeps working when a quick-tunnel hostname churns and never
    leaves the machine. Empty unless QUILL_PEER_INTERNAL_URL is set."""
    import os
    url = (os.environ.get("QUILL_PEER_INTERNAL_URL") or "").strip().rstrip("/")
    return url if url.startswith(("http://", "https://")) else ""


def _accept_internal(url: str | None) -> str:
    """Validate an internal address a peer advertised. Private-network hops
    still carry a bearer token, so QUILL_PEER_REQUIRE_TLS applies here too —
    a refused internal URL just means we keep using the public one."""
    u = (url or "").strip().rstrip("/")
    if not u.startswith(("http://", "https://")):
        return ""
    try:
        from app.services.team_layer import url_transport
        if not url_transport(u).get("ok"):
            return ""
    except Exception:
        return ""
    return u


# --- pairing -----------------------------------------------------------------
def start_pairing() -> dict:
    """Begin (or restart) pairing: one active, short-lived, single-use code.
    The desktop user tells the code to the teammate (say it, message it)."""
    global _pairing
    if not settings.peer.enabled:
        return {"ok": False, "error": "peer channel disabled (QUILL_PEER_CHANNEL=0)"}
    code = f"{secrets.randbelow(1_000_000):06d}"
    with _lock:
        _pairing = {"code": code,
                    "expires_at": time.time() + settings.peer.pair_ttl_s,
                    "attempts": 0}
    return {"ok": True, "code": code, "expires_at": _pairing["expires_at"],
            "ttl_s": settings.peer.pair_ttl_s, "base_url": my_base_url(),
            "internal_url": my_internal_url(),
            "invite": make_invite(code, _pairing["expires_at"]),
            "name": instance_name(), "tls": _start_tls_note()}


# --- invites (one paste instead of address + code) ---------------------------
_INVITE_PREFIX = "sparrow://pair/"


def make_invite(code: str, expires_at: float) -> str:
    """Pack everything the teammate would otherwise retype — our address, our
    name, the code — into one token they paste in a single field.

    Packaging only: the code inside is the same single-use, expiring,
    lockout-guarded secret claim_pairing() checks, so an invite is exactly as
    sensitive as the code and no more powerful."""
    body = {"v": 1, "u": my_base_url(), "c": code,
            "n": instance_name(), "e": int(expires_at)}
    internal = my_internal_url()
    if internal:
        body["i"] = internal
    blob = base64.urlsafe_b64encode(
        json.dumps(body, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return _INVITE_PREFIX + blob.rstrip("=")


def parse_invite(text: str) -> dict:
    """Decode an invite token. Whitespace anywhere is ignored — these get
    pasted out of chat clients that wrap lines."""
    raw = "".join((text or "").split())
    if not raw.lower().startswith(_INVITE_PREFIX):
        return {"ok": False,
                "error": "that is not an invite — paste the whole "
                         "sparrow://pair/… line they sent you"}
    blob = raw[len(_INVITE_PREFIX):]
    try:
        decoded = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        body = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {"ok": False, "error": "invite is damaged — ask them to copy it again"}
    if not isinstance(body, dict):
        return {"ok": False, "error": "invite is damaged — ask them to copy it again"}
    url = str(body.get("u") or "").strip().rstrip("/")
    code = str(body.get("c") or "").strip()[:32]
    if not url.startswith(("http://", "https://")) or not code:
        return {"ok": False, "error": "invite is missing an address or a code"}
    expires_at = float(body.get("e") or 0)
    if expires_at and expires_at <= time.time():
        return {"ok": False, "error": "invite expired — ask them for a fresh one"}
    return {"ok": True, "url": url, "code": code,
            "name": str(body.get("n") or "").strip()[:60],
            "internal_url": _accept_internal(body.get("i")),
            "expires_at": expires_at}


def _start_tls_note() -> dict:
    try:
        from app.services.team_layer import my_transport
        return my_transport()
    except Exception:
        return {"ok": True, "tls": False, "local": True, "warning": None}


def pairing_active() -> bool:
    with _lock:
        return _pairing is not None and _pairing["expires_at"] > time.time()


def apply_initial_pack(peer_id: str, pack: str | None) -> str | None:
    """Set a new pair's disclosure pack: the human's choice, else the
    deployment default, else nothing (leaving all-offer).

    Applied at PAIRING because that is the only moment both people are
    thinking about this relationship. The packs UI lives on a settings table
    nobody opens during a two-week trial, so a pack that can only be chosen
    there is, in practice, never chosen.
    """
    want = (pack or "").strip().lower() or settings.peer.default_pack
    if not want:
        return None
    try:
        from app.services import team_layer
        res = team_layer.apply_pack(peer_id, want)
        if res.get("ok"):
            print(f"[peer] new pair {peer_id} set to the '{want}' pack.")
            return want
        print(f"[peer] pack {want!r} not applied ({res.get('error')}).")
    except Exception as exc:
        print(f"[peer] pack {want!r} not applied ({exc}).")
    return None


def claim_pairing(code: str, name: str, base_url: str,
                  token_for_caller: str, internal_url: str = "",
                  pack: str | None = None) -> dict:
    """A joining peer trades a valid code for OUR token (returned exactly once)
    and hands us THEIRS — after this one call both sides can authenticate.

    `token_for_caller` is what WE will present when calling THEM (stored
    plaintext — it is our credential to their server); the token we mint is
    what THEY present to us (stored hash-only). `internal_url` is optional: a
    private-network address we prefer over `base_url` when reaching them."""
    global _pairing
    if not settings.peer.enabled:
        return {"ok": False, "error": "peer channel disabled"}
    code = (code or "").strip()
    base_url = (base_url or "").strip().rstrip("/")
    if not base_url.startswith(("http://", "https://")):
        return {"ok": False, "error": "base_url must be http(s)://host:port"}
    if len((token_for_caller or "").strip()) < 16:
        return {"ok": False, "error": "token_for_caller too short"}
    from app.services.team_layer import url_transport
    transport = url_transport(base_url)
    if not transport.get("ok"):
        return {"ok": False, "error": transport.get("error", "callback url refused")}
    with _lock:
        if _pairing is None or _pairing["expires_at"] <= time.time():
            _pairing = None
            return {"ok": False, "error": "no active pairing — start one on the desktop"}
        if not code or not hmac.compare_digest(code, _pairing["code"]):
            _pairing["attempts"] += 1
            if _pairing["attempts"] >= settings.peer.max_claim_attempts:
                _pairing = None
                return {"ok": False, "error": "too many wrong codes — pairing cancelled"}
            return {"ok": False, "error": "wrong code"}
        peers = _load(_peers_path(), {})
        if len(peers) >= settings.peer.max_peers:
            return {"ok": False, "error": f"peer limit reached ({settings.peer.max_peers})"}
        _pairing = None  # single-use: claimed
        peer_id = uuid.uuid4().hex[:12]
        token = secrets.token_urlsafe(32)
        peers[peer_id] = {
            "name": (name or "peer").strip()[:60] or "peer",
            "base_url": base_url,
            "internal_url": _accept_internal(internal_url),
            "token_sha256": _hash(token),          # what they present to us
            "outbound_token": token_for_caller.strip(),  # what we present to them
            "created_at": time.time(),
            "last_seen": None,
            "asks": 0,
            "answers": 0,
            # Never auto-mint a Person on pair (junk-people risk).
            "person_id": None,
        }
        _save(_peers_path(), peers)
        name_out = peers[peer_id]["name"]
    print(f"[peer] paired with {name_out} ({base_url}).")
    applied = apply_initial_pack(peer_id, pack)
    return {"ok": True, "peer_id": peer_id, "name": instance_name(),
            "base_url": my_base_url(), "internal_url": my_internal_url(),
            "token": token, "pack": applied}


def join(url: str, code: str, internal_url: str | None = None,
         pack: str | None = None) -> dict:
    """Driver side of pairing: claim `code` on the remote instance at `url`.

    Mints the token the remote will use to call US (stored hash-only), sends
    it with the claim, stores the token the remote returns (plaintext — our
    credential to them) plus their name/url as a peer record.

    `pack` is THIS side's disclosure choice about the peer being added. It is
    deliberately not sent to them: what we will disclose is ours to decide,
    and each side picks its own.
    """
    if not settings.peer.enabled:
        return {"ok": False, "error": "peer channel disabled"}
    url = (url or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "url must be http(s)://host:port"}
    from app.services.team_layer import url_transport
    transport = url_transport(url)
    if not transport.get("ok"):
        return {"ok": False, "error": transport.get("error", "url refused")}
    with _lock:
        peers = _load(_peers_path(), {})
        if len(peers) >= settings.peer.max_peers:
            return {"ok": False, "error": f"peer limit reached ({settings.peer.max_peers})"}
    inbound_token = secrets.token_urlsafe(32)
    try:
        res = _post_json(f"{url}/peer/pair/claim", {
            "code": (code or "").strip(),
            "name": instance_name(),
            "base_url": my_base_url(),
            "internal_url": my_internal_url(),
            "token_for_caller": inbound_token,
        }, token=None)
    except Exception as exc:
        return {"ok": False, "error": f"could not reach peer ({exc})"}
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error", "claim refused")}
    with _lock:
        peers = _load(_peers_path(), {})
        peer_id = uuid.uuid4().hex[:12]
        peers[peer_id] = {
            "name": str(res.get("name") or "peer").strip()[:60] or "peer",
            "base_url": url,
            # The claim response is fresher than anything the invite carried.
            "internal_url": _accept_internal(res.get("internal_url")
                                             or internal_url),
            "token_sha256": _hash(inbound_token),
            "outbound_token": str(res.get("token") or ""),
            "created_at": time.time(),
            "last_seen": None,
            "asks": 0,
            "answers": 0,
            "person_id": None,
        }
        _save(_peers_path(), peers)
        name = peers[peer_id]["name"]
    print(f"[peer] joined {name} ({url}).")
    applied = apply_initial_pack(peer_id, pack)
    return {"ok": True, "peer_id": peer_id, "name": name, "pack": applied}


def join_invite(invite: str, pack: str | None = None) -> dict:
    """Join from a pasted invite — same claim, one field instead of two."""
    parsed = parse_invite(invite)
    if not parsed.get("ok"):
        return {"ok": False, "error": parsed.get("error", "invalid invite")}
    return join(parsed["url"], parsed["code"],
                internal_url=parsed.get("internal_url"), pack=pack)


# --- authentication ----------------------------------------------------------
def authenticate(authorization: str | None) -> dict | None:
    """Resolve a `Bearer <token>` header to a peer record, or None."""
    if not settings.peer.enabled or not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    digest = _hash(parts[1].strip())
    with _lock:
        peers = _load(_peers_path(), {})
    for peer_id, rec in peers.items():
        stored = rec.get("token_sha256", "")
        if stored and hmac.compare_digest(stored, digest):
            return {"peer_id": peer_id, **rec}
    return None


def revoke(peer_id: str) -> bool:
    """Forget a peer — every direction dies: their token stops authenticating,
    ours to them is discarded, and any clip grant they still hold is killed.

    That last one matters: a clip grant is a separate credential with its own
    expiry, so unpairing without revoking it would leave a live key to this
    tenant's recordings in the hands of someone who is no longer a peer."""
    with _lock:
        peers = _load(_peers_path(), {})
        if peer_id not in peers:
            return False
        del peers[peer_id]
        _save(_peers_path(), peers)
    try:
        from app.services import peer_clip
        peer_clip.revoke_for_peer(peer_id)
    except Exception as exc:
        print(f"[peer] clip grant revoke skipped ({exc}).")
    return True


def _person_display(person_id: int | None) -> str | None:
    if person_id is None:
        return None
    try:
        from app.services.memory import memory
        p = memory._ensure_store().get_person(int(person_id))
        if p:
            return str(p.get("name") or "").strip() or None
    except Exception:
        pass
    return None


def peers() -> list[dict]:
    """Registry rows for the UI — no tokens or hashes leave this module."""
    out = []
    with _lock:
        registry = _load(_peers_path(), {})
    for peer_id, rec in sorted(registry.items(),
                               key=lambda kv: kv[1].get("created_at") or 0):
        try:
            policy = _sanitize_policy(rec.get("policy") or {})
        except ValueError:
            policy = default_policy()
        pid = rec.get("person_id")
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        out.append({"peer_id": peer_id, "name": rec.get("name", "?"),
                    "base_url": rec.get("base_url", ""),
                    "internal_url": rec.get("internal_url", ""),
                    "created_at": rec.get("created_at"),
                    "last_seen": rec.get("last_seen"),
                    "asks": int(rec.get("asks") or 0),
                    "answers": int(rec.get("answers") or 0),
                    "person_id": pid,
                    "person_name": _person_display(pid),
                    "policy": policy,
                    "policy_pack": rec.get("policy_pack") or "custom",
                    "presence": _presence(rec.get("last_seen")),
                    "tls": _url_tls_flag(rec.get("base_url") or "")})
    return out


def _presence(last_seen) -> str:
    try:
        from app.services.team_layer import presence_of
        return presence_of(last_seen)
    except Exception:
        return "unknown"


def _url_tls_flag(url: str) -> bool:
    return (url or "").lower().startswith("https://")


def link_person(peer_id: str, person_id: int) -> dict:
    """User-asserted link from a paired peer to an existing Person row.

    Never creates a Person here — caller may create first (with name_quality
    gate) then pass the id. Disclosure remains keyed by peer_id.
    """
    try:
        person_id = int(person_id)
    except (TypeError, ValueError):
        return {"ok": False, "error": "person_id required"}
    try:
        from app.services.memory import memory
        person = memory._ensure_store().get_person(person_id)
    except Exception as exc:
        return {"ok": False, "error": f"store unavailable ({exc})"}
    if not person:
        return {"ok": False, "error": "unknown person"}
    if person.get("canonical_person_id") or person.get("hide_from_people"):
        return {"ok": False, "error": "person is hidden or merged"}
    with _lock:
        peers_reg = _load(_peers_path(), {})
        rec = peers_reg.get(peer_id)
        if not rec:
            return {"ok": False, "error": "unknown peer"}
        # One peer per person (and one person per peer).
        for other_id, other in peers_reg.items():
            if other_id == peer_id:
                continue
            try:
                if int(other.get("person_id") or 0) == person_id:
                    return {"ok": False,
                            "error": f"person already linked to peer {other_id}"}
            except (TypeError, ValueError):
                continue
        rec["person_id"] = person_id
        peers_reg[peer_id] = rec
        _save(_peers_path(), peers_reg)
    return {"ok": True, "peer_id": peer_id, "person_id": person_id,
            "person_name": str(person.get("name") or "")}


def unlink_person(peer_id: str) -> dict:
    """Clear the optional Person link on a peer (pairing unchanged)."""
    with _lock:
        peers_reg = _load(_peers_path(), {})
        rec = peers_reg.get(peer_id)
        if not rec:
            return {"ok": False, "error": "unknown peer"}
        rec["person_id"] = None
        peers_reg[peer_id] = rec
        _save(_peers_path(), peers_reg)
    return {"ok": True, "peer_id": peer_id, "person_id": None}


def create_and_link_person(peer_id: str, name: str | None = None) -> dict:
    """Create a Person (name_quality gated) then link — only on user confirm."""
    with _lock:
        peers_reg = _load(_peers_path(), {})
        rec = peers_reg.get(peer_id)
        if not rec:
            return {"ok": False, "error": "unknown peer"}
        display = (name or rec.get("name") or "").strip()
    from app.services.name_quality import is_plausible_person, normalize_person_name
    if not is_plausible_person(display):
        return {"ok": False, "error": "name fails person-quality gate"}
    try:
        from app.services.memory import memory
        store = memory._ensure_store()
        canon = normalize_person_name(display) or display
        pid = int(store.insert_person(canon))
    except Exception as exc:
        return {"ok": False, "error": f"could not create person ({exc})"}
    return link_person(peer_id, pid)


def _touch(peer_id: str, counter: str | None = None) -> None:
    try:
        with _lock:
            registry = _load(_peers_path(), {})
            rec = registry.get(peer_id)
            if rec is not None:
                rec["last_seen"] = time.time()
                if counter:
                    rec[counter] = int(rec.get(counter) or 0) + 1
                _save(_peers_path(), registry)
    except OSError:
        pass  # bookkeeping only


def refresh_peer_base_url(peer_id: str, base_url: str | None,
                          internal_url: str | None = None) -> bool:
    """Update a peer's addresses when they advertise new ones.

    Docker bridge IPs change on every recreate and quick-tunnel hostnames
    change on every tunnel restart; peers send `base_url` (or `callback_url`)
    and optionally `internal_url` on ask/ping so delivery keeps working after
    a rebuild without anyone editing peers.json.
    """
    url = (base_url or "").strip().rstrip("/")
    if not peer_id:
        return False
    if url.startswith(("http://", "https://")):
        try:
            from app.services.team_layer import url_transport
            if not url_transport(url).get("ok"):
                url = ""
        except Exception:
            url = ""
    else:
        url = ""
    internal = _accept_internal(internal_url) if internal_url is not None else None
    if not url and not internal:
        return False
    try:
        with _lock:
            registry = _load(_peers_path(), {})
            rec = registry.get(peer_id)
            if rec is None:
                return False
            changed = False
            if url and (rec.get("base_url") or "").rstrip("/") != url:
                old = rec.get("base_url")
                rec["base_url"] = url
                changed = True
                print(f"[peer] refreshed callback for {peer_id}: {old} → {url}")
            if internal and (rec.get("internal_url") or "").rstrip("/") != internal:
                rec["internal_url"] = internal
                changed = True
                print(f"[peer] refreshed private address for {peer_id}: {internal}")
            if not changed:
                return False
            _save(_peers_path(), registry)
        return True
    except OSError:
        return False


# --- transport ---------------------------------------------------------------
def _post_json(url: str, payload: dict, token: str | None,
               timeout: float | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers)
    wait = settings.peer.http_timeout_s if timeout is None else float(timeout)
    with urllib.request.urlopen(req, timeout=wait) as r:
        return json.loads(r.read().decode("utf-8"))


def peer_urls(rec: dict) -> list[str]:
    """Where to call a peer, best first: the private-network address they
    advertised (same box / same LAN, immune to tunnel churn), then the public
    one. Deduped, so a peer with one address costs one attempt."""
    out: list[str] = []
    for candidate in (rec.get("internal_url"), rec.get("base_url")):
        url = (candidate or "").strip().rstrip("/")
        if url and url not in out:
            out.append(url)
    return out


# Addresses that just failed at the transport layer, url -> retry after ts.
# A peer advertises its private address to everyone, but only teammates on the
# same network can use it — without this, an off-box peer would burn a failed
# connection on every single call. Process-local: a restart retries everything.
_addr_cooldown: dict[str, float] = {}
_ADDR_COOLDOWN_S = 300.0


def _post_peer(rec: dict, path: str, payload: dict,
               timeout: float | None = None) -> dict:
    """POST to a peer over the first address that answers.

    A transport failure on the private address falls through to the public one
    (and parks that address for a few minutes, so a peer who can never reach it
    stops paying for the attempt); an HTTP status means the peer DID answer
    (auth, 404, 5xx), so it is raised as-is rather than retried elsewhere."""
    token = rec.get("outbound_token")
    urls = peer_urls(rec)
    now = time.time()
    live = [u for u in urls if _addr_cooldown.get(u, 0.0) <= now] or urls
    last: Exception | None = None
    for i, base in enumerate(live):
        try:
            if timeout is None:
                res = _post_json(f"{base}{path}", payload, token=token)
            else:
                res = _post_json(f"{base}{path}", payload, token=token,
                                 timeout=timeout)
            _addr_cooldown.pop(base, None)
            return res
        except urllib.error.HTTPError:
            raise
        except Exception as exc:
            last = exc
            _addr_cooldown[base] = time.time() + _ADDR_COOLDOWN_S
            if i + 1 < len(live):
                print(f"[peer] {base} unreachable ({exc}); trying the next address.")
    raise last if last is not None else ValueError("peer has no address")


def _publish_event(source: str, text: str, meta: dict, tier=None) -> None:
    """Land peer traffic as memory context (observed tier unless overridden).
    Best-effort: the channel must work without a bound event loop."""
    try:
        ev = Event(time=time.time(), modality=Modality.SYSTEM, raw=text,
                   summary=f"[{source}] {text[:200]}", source=source,
                   meta={"origin": "peer", **meta})
        _conf.attach(ev, tier if tier is not None else _conf.OBSERVED)
        bus.publish_nowait(ev)
    except Exception as exc:  # pragma: no cover
        print(f"[peer] event publish skipped ({exc}).")


# --- disclosure policy (Phase 2) --------------------------------------------
# What a peer's question can be ABOUT, as the user reasons about sharing:
#   availability — schedule, whereabouts, free/busy, deadlines, dates
#   work         — projects, tasks, documents, status, tools
#   contact      — phone numbers, emails, addresses of people
#   personal     — health, family, money, feelings, anything private
#   other        — everything else / unclear
CLASSES = ("availability", "work", "contact", "personal", "other")
ACTIONS = ("auto", "offer", "deny")

_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "topic": {"type": "string", "enum": list(CLASSES),
                  "description": "What the question is about. Use `personal` "
                                 "for anything private (health, family, money, "
                                 "salary, feelings); `other` when unclear."},
    },
    "required": ["topic"],
}


def default_policy() -> dict:
    """All-offer: every ask waits for the human. Phase 1's posture, verbatim."""
    return {c: "offer" for c in CLASSES}


def _sanitize_policy(policy: dict) -> dict:
    """Full, valid action map or ValueError. `personal` can never be auto —
    automatic disclosure of private material is not grantable, period."""
    if not isinstance(policy, dict):
        raise ValueError("policy must be an object of {class: action}")
    out = default_policy()
    for cls, action in policy.items():
        if cls not in CLASSES:
            raise ValueError(f"unknown class {cls!r} (one of {', '.join(CLASSES)})")
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r} (one of {', '.join(ACTIONS)})")
        out[cls] = action
    if out["personal"] == "auto":
        raise ValueError("the `personal` class can never be auto-answered")
    return out


def get_policy(peer_id: str) -> dict:
    with _lock:
        rec = _load(_peers_path(), {}).get(peer_id)
    if rec is None:
        return default_policy()
    try:
        return _sanitize_policy(rec.get("policy") or {})
    except ValueError:
        return default_policy()   # a corrupt stored policy fails closed


def set_policy(peer_id: str, policy: dict, pack: str | None = None) -> dict:
    try:
        clean = _sanitize_policy(policy)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    with _lock:
        peers = _load(_peers_path(), {})
        rec = peers.get(peer_id)
        if rec is None:
            return {"ok": False, "error": "unknown peer"}
        rec["policy"] = clean
        rec["policy_pack"] = (pack or "custom").strip().lower() or "custom"
        _save(_peers_path(), peers)
    return {"ok": True, "peer_id": peer_id, "policy": clean,
            "policy_pack": rec["policy_pack"]}


def classify_question(question: str) -> str | None:
    """The LOCAL model buckets the question (schema-enforced). None on any
    failure or hesitation — the caller treats None as 'ask the human'."""
    try:
        from app.services.model_router import router
        res = router.complete_json(
            "peer_classify",
            system=("Classify what a question is about so a privacy policy can "
                    "be applied. Categories: availability (schedule, "
                    "whereabouts, free/busy, deadlines, dates), work (projects, "
                    "tasks, documents, status, tools), contact (phone numbers, "
                    "emails, addresses), personal (health, family, money, "
                    "salary, feelings — anything private), other (unclear). "
                    "When in doubt between personal and anything else, answer "
                    "personal."),
            messages=[{"role": "user", "content": question}],
            schema=_CLASSIFY_SCHEMA, max_tokens=64)
        topic = str((res or {}).get("topic") or "").strip().lower()
        return topic if topic in CLASSES else None
    except Exception as exc:
        print(f"[peer] classify failed ({exc}); treating as offer.")
        return None


def _decide_action(peer: dict, question: str) -> tuple[str, str | None]:
    """(action, topic) for one inbound ask. Classification only runs when the
    peer's policy could change the outcome; every failure path lands on
    'offer'. `personal` never autos, even if a stored policy says so."""
    policy = get_policy(peer.get("peer_id", ""))
    if all(a == "offer" for a in policy.values()):
        return "offer", None
    topic = classify_question(question)
    if topic is None:
        return "offer", None
    action = policy.get(topic, "offer")
    if action == "auto" and topic == "personal":
        action = "offer"
    return action, topic


def _notify_chat(text: str) -> None:
    """Surface a line in the chat pane (same seam data_watch uses). Best-effort:
    the channel must work headless and under QUILL_AGENT=0."""
    try:
        from app.services import agent_bridge
        agent_bridge.worker._emit("system", text)
    except Exception:
        pass


# --- answering side (inbound asks) ------------------------------------------
def compose_answer(question: str) -> dict:
    """Answer a teammate's question from OUR memory for peer egress.

    Phase 1: this is `compose_peer_claims` rendered for the wire. The answer
    carries the claims it rests on and the date of its newest supporting
    memory, so the asker can tell Tuesday from April. `text` stays for peers
    running an older build.

    The old path composed free prose from rendered grounding blobs, which is
    why identity blocks and assistant hedges kept crossing and had to be
    chased with substring filters. Untyped text is no longer a candidate.
    """
    out = compose_peer_claims(question)
    if out["claims"]:
        return {"text": out["prose"], "claims": out["claims"],
                "as_of": out["as_of"], "near_miss": out["near_miss"],
                "redacted": out["redacted"]}
    return {"text": "I don't have anything in my memory on that.",
            "claims": [], "as_of": None, "near_miss": False, "redacted": []}


def compose_peer_update(topic: str) -> dict:
    """Compose a teammate push-update from OUR memory.

    Same typed egress path as an answer — a push update is egress too, and
    splitting the two is how the sender's own tasks/commitments used to ride
    along on one path after being filtered out of the other. EGRESS_KINDS
    excludes work items structurally now, on both.
    """
    out = compose_peer_claims(topic)
    if not out["claims"] or out["near_miss"]:
        # Nothing typed on the topic: send nothing rather than filler. A push
        # update is unsolicited, so a near-miss is not worth a teammate's
        # attention the way it is when they actually asked.
        return {"text": "", "claims": [], "as_of": None, "redacted": []}
    return {"text": out["prose"], "claims": out["claims"],
            "as_of": out["as_of"], "redacted": out["redacted"]}


def compose_peer_claims(question: str, *, store=None,
                        now: float | None = None) -> dict:
    """Phase 1.2 — the single peer-egress composer.

    Returns {"claims": [...], "as_of": float|None, "prose": str,
             "near_miss": bool, "redacted": [...]}. Each claim carries its own
    text, fact id, source event id, speaker and timestamp, so the asker can
    see WHEN something was true and WHERE it came from instead of receiving an
    undated paragraph.

    `prose` is rendered FROM the claims, never composed separately. That is
    what makes this one egress path rather than two: there is no second route
    by which untyped text can reach the wire, so the substring blocklists that
    used to guard it have nothing left to guard.
    """
    from app.services import peer_retrieval as pr
    from app.services import redact

    question = (question or "").strip()
    if not question:
        return {"claims": [], "as_of": None, "prose": "", "near_miss": False,
                "redacted": []}

    # Ground on the bare ask when the asker attached a context brief — their
    # brief is background, never something we echo back as our own knowledge.
    topic = question
    marker = "(Context from my side"
    if marker in topic:
        topic = topic.split(marker, 1)[0].strip() or question

    now = time.time() if now is None else now
    claims = pr.facts_for_topic(topic, store=store, now=now)
    near_miss = False
    if not claims:
        # 1.6: a dated near-miss beats a refusal — it tells the asker whether
        # to go find the human, which is itself a real answer.
        claims = pr.near_miss(topic, store=store)
        near_miss = bool(claims)

    kinds: list[str] = []
    out_claims: list[dict] = []
    for c in claims:
        clean = redact.redact_text(c["text"])
        kinds.extend(redact.scan(c["text"]))
        out_claims.append({**c, "text": clean,
                           "when": pr.describe_age(c["ts"], now)})

    return {
        "claims": out_claims,
        "as_of": pr.as_of(out_claims),
        "prose": _render_claims(out_claims, near_miss=near_miss, now=now),
        "near_miss": near_miss,
        "redacted": sorted(set(kinds)),
    }


def _render_claims(claims: list[dict], *, near_miss: bool = False,
                   now: float | None = None) -> str:
    """Claims -> the text an unupgraded peer reads. Every line carries its own
    date, so staleness is visible even without the structured payload."""
    if not claims:
        return ""
    lines = []
    if near_miss:
        from app.services import peer_retrieval as pr
        newest = pr.describe_age(claims[0].get("ts"), now)
        lines.append(
            f"I don't have anything on that specifically. The most recent "
            f"thing I do have (from {newest}):" if newest else
            "I don't have anything on that specifically. The closest I have:")
    for c in claims:
        who = c.get("speaker") or ""
        when = c.get("when") or ""
        stamp = " · ".join([p for p in (who, when) if p])
        lines.append(f"- {c['text']}" + (f" ({stamp})" if stamp else ""))
    return "\n".join(lines)


def enrich_peer_question(question: str) -> str:
    """Attach a short factual brief from OUR memory so the teammate isn't cold.

    This is egress too — the brief is our memory, sent to them — so it runs the
    same typed path as an answer. Dated, attributed claims only; the brief used
    to be rendered grounding lines, which is how "I can make a note of that for
    you" once crossed as if it were a project fact.
    """
    q = (question or "").strip()
    if not q or "Context from my side" in q:
        return q
    out = compose_peer_claims(q)
    if not out["claims"] or out["near_miss"]:
        return q
    text = _render_claims(out["claims"])[:600]
    if not text:
        return q
    enriched = (
        f"{q}\n\n"
        f"(Context from my side — background only; add your own knowledge or "
        f"say you have none):\n{text}"
    )
    return enriched[: settings.peer.max_text_chars]


def handle_ask(peer: dict, payload: dict) -> dict:
    """One authenticated inbound ask, through the disclosure gate.

    Resolution order: the QUILL_PEER_AUTO_ANSWER dev/sim flag autos everything;
    else the peer's per-class policy decides (auto / offer / deny) with the
    local classifier, failing to "offer" on any doubt. Default policy = all
    offer = queue for the human."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    ask_id = str(payload.get("ask_id") or "").strip()[:64]
    question = str(payload.get("question") or "").strip()
    if not ask_id or not question:
        return {"ok": False, "error": "ask_id and question are required"}
    question = question[: settings.peer.max_text_chars]
    peer_id = peer.get("peer_id", "")
    _touch(peer_id, "asks")
    refresh_peer_base_url(
        peer_id, payload.get("base_url") or payload.get("callback_url"),
        payload.get("internal_url"))
    _publish_event("peer.ask", question,
                   {"peer_id": peer_id, "peer": peer.get("name", ""),
                    "ask_id": ask_id})

    kind = str(payload.get("kind") or "question").strip().lower()
    # Org network packets ride the peer transport as structured text; they are
    # never raw memory. org_escalate always human-offers; org_digest/priority
    # use work-class policy (default offer). notify is a push update composed
    # on the sender — surfaces for the human (offer) unless the sim flag.
    if kind not in _PEER_KINDS:
        return {"ok": False, "error": f"unknown kind {kind!r}"}

    # A handoff is a request for THIS user to do something — action-adjacent,
    # so it ALWAYS waits for the human. No policy grant and no dev flag can
    # auto-accept work on someone's behalf. Org escalations are likewise
    # always offer (Phase 1: exec must see them). Notify is an inbound update
    # from their teammate's Sparrow — also offer unless the sim flag.
    if kind in ("handoff", "org_escalate", "notify"):
        action, topic = "offer", "work"
        if kind == "notify" and settings.peer.auto_answer:
            action = "auto"
    elif kind in ("org_digest", "org_priority"):
        # Treat as work-class; respect per-peer policy for "work".
        policy = peer.get("policy") or default_policy()
        action = policy.get("work") or "offer"
        if action == "auto" and kind == "org_priority":
            # Still queue a soft ack path via auto ingest below
            pass
        topic = "work"
        if settings.peer.auto_answer and action == "offer":
            action = "auto"
    elif settings.peer.auto_answer:
        action, topic = "auto", None
    else:
        action, topic = _decide_action(peer, question)

    try:
        from app.services import peer_telemetry
        peer_telemetry.record(
            "gate", ask_id=ask_id, peer_id=peer_id,
            peer_name=peer.get("name", "?"), kind=kind,
            action=action, topic=topic or "",
            question_chars=len(question or ""))
    except Exception as exc:
        print(f"[peer] telemetry skipped ({exc}).")

    if action == "auto" and kind in ("org_digest", "org_priority"):
        _accept_org_packet(peer.get("name") or "a teammate", peer_id, ask_id,
                           question, kind)
        print(f"[peer] auto-accepted {kind} from {peer.get('name', '?')}")
        return {"ok": True, "status": "answered", "ask_id": ask_id,
                "topic": topic, "answer": f"accepted {kind}",
                "redacted": []}
    if action == "auto" and kind == "notify":
        _accept_notify(peer.get("name") or "a teammate", peer_id, ask_id,
                       question)
        print(f"[peer] auto-accepted notify from {peer.get('name', '?')}")
        return {"ok": True, "status": "answered", "ask_id": ask_id,
                "topic": topic, "answer": "accepted notify",
                "redacted": []}
    if action == "auto":
        composed = compose_answer(question)
        print(f"[peer] auto-answered {peer.get('name', '?')} "
              f"({topic or 'dev flag'}): {question[:80]}")
        return {"ok": True, "status": "answered", "ask_id": ask_id,
                "topic": topic, "answer": composed["text"],
                "claims": composed.get("claims") or [],
                "as_of": composed.get("as_of"),
                "near_miss": composed.get("near_miss"),
                "redacted": composed["redacted"]}
    if action == "deny":
        print(f"[peer] auto-declined {peer.get('name', '?')} "
              f"({topic}): {question[:80]}")
        return {"ok": True, "status": "declined", "ask_id": ask_id,
                "topic": topic}

    with _lock:
        asks = _load(_asks_path(), [])
        # delivery_pending still occupies a slot until the outbound lands.
        open_asks = [a for a in asks if a.get("status") in _OPEN_ASK_STATUSES]
        if len(open_asks) >= settings.peer.max_pending_asks:
            return {"ok": False, "error": "ask queue full"}
        item = {"id": uuid.uuid4().hex[:12], "peer_id": peer_id,
                "peer_name": peer.get("name", "?"), "ask_id": ask_id,
                "question": question, "topic": topic, "kind": kind,
                "loop_id": str(payload.get("loop_id") or "").strip() or None,
                "created_at": time.time(),
                "status": "pending", "answer": None, "decided_at": None}
        asks.append(item)
        _save(_asks_path(), asks)
    print(f"[peer] {kind} queued from {peer.get('name', '?')}: {question[:80]}")
    _announce_pending(item)
    return {"ok": True, "status": "pending", "ask_id": ask_id}


# Asks arriving within this window are announced together. A teammate working
# through a few questions produces a burst, and one interruption per ask is
# how a disclosure queue trains someone to ignore it.
_BATCH_WINDOW_S = 45.0


def _ask_line(item: dict) -> str:
    who = item.get("peer_name") or "A teammate"
    q = str(item.get("question") or "")[:200]
    kind = item.get("kind")
    if kind == "handoff":
        return f"{who} wants to hand you a task: “{q}”"
    if kind == "notify":
        return f"{who}'s Sparrow sent an update: “{q}”"
    if kind == "org_digest":
        return f"{who} sent an org digest"
    if kind == "org_priority":
        return f"{who} sent company priority guidance"
    if kind == "org_escalate":
        return f"{who} escalated a strategic issue"
    return f"{who}'s Sparrow asks: “{q}”"


def _announce_pending(item: dict) -> None:
    """One prompt for everything waiting, not one per ask.

    Re-announcing the whole queue (rather than only the newest) means the
    latest line is always a complete picture of what is waiting, so the human
    can act once instead of reconstructing it from a scroll of interruptions.
    """
    now = float(item.get("created_at") or time.time())
    try:
        with _lock:
            asks = _load(_asks_path(), [])
        waiting = [a for a in asks if a.get("status") == "pending"]
    except Exception:
        waiting = [item]
    recent = [a for a in waiting
              if now - float(a.get("created_at") or 0) <= _BATCH_WINDOW_S]
    # The triggering ask is always included, even if the read raced.
    if not any(a.get("id") == item.get("id") for a in recent):
        recent.append(item)
    recent.sort(key=lambda a: float(a.get("created_at") or 0))

    if len(recent) == 1:
        _notify_chat(f"{_ask_line(recent[0])} — approve or decline on the "
                     "Team page (/peer).")
        return
    names = list(dict.fromkeys(a.get("peer_name") or "a teammate"
                               for a in recent))
    whose = names[0] if len(names) == 1 else f"{len(names)} teammates"
    lines = "\n".join(f"• {_ask_line(a)}" for a in recent)
    _notify_chat(
        f"{len(recent)} things from {whose} are waiting on you:\n{lines}\n"
        "Approve or decline them together on the Team page (/peer).")


def pending_asks() -> list[dict]:
    """Inbound asks awaiting the human's disclosure decision — for the UI."""
    return [{k: a.get(k) for k in ("id", "peer_name", "question", "topic",
                                   "kind", "loop_id", "created_at")}
            for a in _load(_asks_path(), []) if a.get("status") == "pending"]


def _accept_org_packet(name: str, peer_id: str, ask_id: str,
                       text: str, kind: str) -> None:
    """Ingest an org_digest / org_priority / org_escalate packet as observed
    context. Priorities also land in org_priority store for grounding."""
    try:
        _publish_event(f"peer.{kind}", f"[{kind} from {name}] {text}",
                       {"peer_id": peer_id, "peer": name, "ask_id": ask_id,
                        "kind": kind})
        if kind == "org_priority":
            from app.services import org_priority
            org_priority.ingest_packet({
                "guidance": text,
                "items": [],
                "goals": [],
                "target_role": None,
            }, source=f"peer.{name}")
        elif kind == "org_escalate":
            from app.services import org_escalate
            org_escalate.append_local({
                "via": "peer",
                "from": name,
                "text": text[:2000],
                "ask_id": ask_id,
            })
    except Exception as exc:
        print(f"[peer] org packet ingest skipped ({exc}).")


def _accept_notify(name: str, peer_id: str, ask_id: str, text: str) -> None:
    """Inbound teammate update (kind=notify) — observed context in chat/memory."""
    try:
        _publish_event("peer.notify", f"[update from {name}] {text}",
                       {"peer_id": peer_id, "peer": name, "ask_id": ask_id,
                        "kind": "notify"})
        _notify_chat(f"Update from {name}'s Sparrow:\n{text[:1200]}")
    except Exception as exc:
        print(f"[peer] notify ingest skipped ({exc}).")


def _accept_handoff(name: str, peer_id: str, ask_id: str, task: str,
                    loop_id: str | None = None) -> None:
    """One accepted handoff -> this user's memory as ACCEPTED-tier material,
    mined for commitments/tasks by the same extraction chain. Best-effort:
    a storage hiccup must not block telling the sender it was accepted."""
    if loop_id:
        try:
            from app.services import team_layer
            team_layer.upsert_loop(loop_id=loop_id, peer_id=peer_id,
                                   peer_name=name, task=task, status="open",
                                   ask_id=ask_id, side="receiver")
        except Exception as exc:
            print(f"[peer] loop persist skipped ({exc}).")
    try:
        if not ingest_enabled():
            _publish_event("peer.handoff", f"[handoff from {name}] {task}",
                           {"peer_id": peer_id, "peer": name,
                            "ask_id": ask_id, "loop_id": loop_id},
                           tier=_conf.ACCEPTED)
            return
        from app.events import Event, Modality
        from app.services.attachments import _index_event
        from app.storage import get_store

        text = f"[handoff from {name}] {task}"
        ev = Event(time=time.time(), modality=Modality.TEXT, raw=text,
                   summary=f"[peer.handoff] {text[:120]}",
                   source="peer.handoff",
                   meta={"origin": "peer", "peer_id": peer_id, "peer": name,
                         "ask_id": ask_id, "loop_id": loop_id})
        _conf.attach(ev, _conf.ACCEPTED)
        anchor = get_store().insert(ev)
        _index_event(anchor, ev)
        from app.services.worker import worker
        worker.enqueue("peer_ingest",
                       payload={"event_id": anchor, "text": task,
                                "peer": name, "source": "peer.handoff"})
    except Exception as exc:
        print(f"[peer] handoff ingest skipped ({exc}).")


def _deliver(peer_rec: dict, payload: dict) -> bool:
    try:
        res = _post_peer(peer_rec, "/peer/answer", payload)
        return bool(res.get("ok"))
    except Exception as exc:
        print(f"[peer] answer delivery failed ({exc}).")
        return False


# Human still owes a verdict. delivery_pending means the human already decided
# but the outbound /peer/answer POST failed — keep the row (and payload) until
# retry_delivery_pending lands it. Never truncate these the way decided history
# is truncated.
_OPEN_ASK_STATUSES = frozenset({"pending", "delivery_pending"})
_OUTSTANDING_SENT = frozenset({"sent", "pending", "queued"})


def _mint_clip_grant(peer_id: str, ask_id: str, claims: list) -> dict | None:
    """A scoped, expiring key to exactly the recordings behind `claims`.

    Only claims that actually name a source event can be played, so the grant's
    scope is the answer's own provenance — which is what Phase 1 put there.
    """
    try:
        from app.services import peer_clip
        if not peer_clip.enabled():
            return None
        # Each granted event carries the verbatim span its claim rests on, so
        # the grant authorises the part of the recording that backs what was
        # approved — not the whole captured moment around it.
        scope = []
        for c in claims or []:
            eid = c.get("source_event_id")
            # Only events that really have audio: a grant naming an unplayable
            # event would tell the asker a clip exists when it does not.
            if eid and peer_clip.playable_path(eid):
                scope.append({"event_id": eid,
                              "span": c.get("source_span") or c.get("text") or ""})
        return peer_clip.mint(peer_id, ask_id, scope)
    except Exception as exc:
        print(f"[peer] clip grant skipped ({exc}).")
        return None


def decide_ask(local_id: str, approve: bool) -> dict:
    """The human's disclosure verdict on one queued ask. Approve composes the
    answer NOW (so the human's yes is to the question, and composition uses
    current memory), redacts it, and delivers it to the asking peer."""
    with _lock:
        asks = _load(_asks_path(), [])
        item = next((a for a in asks if a.get("id") == local_id
                     and a.get("status") == "pending"), None)
        if item is None:
            return {"ok": False, "error": "no such pending ask"}
        registry = _load(_peers_path(), {})
        peer_rec = registry.get(item.get("peer_id", ""))
    if peer_rec is None:
        return {"ok": False, "error": "asking peer no longer paired"}

    # How long the asker sat in dead air waiting on this human. On a hosted box
    # where everyone is always online, this is the number that distinguishes a
    # consent-latency problem from a retrieval one.
    try:
        from app.services import peer_telemetry
        peer_telemetry.record(
            "verdict", ask_id=item.get("ask_id", ""),
            peer_id=item.get("peer_id", ""),
            peer_name=peer_rec.get("name", "?"), kind=item.get("kind", ""),
            verdict="approved" if approve else "declined",
            topic=item.get("topic") or "",
            waited_s=round(time.time() - float(item.get("created_at") or 0), 3))
    except Exception as exc:
        print(f"[peer] telemetry skipped ({exc}).")

    if not approve:
        outbound = {"ask_id": item["ask_id"], "declined": True}
        delivered = _deliver(peer_rec, outbound)
        if delivered:
            _finish_ask(local_id, "denied", None)
        else:
            _stash_delivery_pending(local_id, None, outbound,
                                    success_status="denied")
        if item.get("loop_id"):
            try:
                from app.services import team_layer
                team_layer.mark_loop(item["loop_id"], "declined")
            except Exception:
                pass
        return {"ok": True, "status": "denied", "delivered": delivered}

    if item.get("kind") == "handoff":
        # Accepting a handoff doesn't compose an answer — it takes the task:
        # ACCEPTED tier (this human just said yes to it), mined by the same
        # extractor so it becomes a commitment/task in THIS user's memory.
        _accept_handoff(peer_rec.get("name") or "a teammate",
                        item.get("peer_id", ""), item["ask_id"],
                        item["question"], loop_id=item.get("loop_id"))
        reply = "Accepted — added to my list."
        outbound = {"ask_id": item["ask_id"], "answer": reply}
        delivered = _deliver(peer_rec, outbound)
        return _complete_or_retry_delivery(
            local_id, delivered, reply, outbound, "accepted")

    if item.get("kind") == "notify":
        _accept_notify(peer_rec.get("name") or "a teammate",
                       item.get("peer_id", ""), item["ask_id"],
                       item["question"])
        reply = "Accepted update."
        outbound = {"ask_id": item["ask_id"], "answer": reply}
        delivered = _deliver(peer_rec, outbound)
        return _complete_or_retry_delivery(
            local_id, delivered, reply, outbound, "accepted")

    if item.get("kind") in ("org_digest", "org_priority", "org_escalate"):
        _accept_org_packet(peer_rec.get("name") or "a teammate",
                           item.get("peer_id", ""), item["ask_id"],
                           item["question"], item["kind"])
        reply = f"Accepted {item['kind']}."
        outbound = {"ask_id": item["ask_id"], "answer": reply}
        delivered = _deliver(peer_rec, outbound)
        return _complete_or_retry_delivery(
            local_id, delivered, reply, outbound, "accepted")

    composed = compose_answer(item["question"])
    outbound = {"ask_id": item["ask_id"],
                "answer": composed["text"],
                "claims": composed.get("claims") or [],
                "as_of": composed.get("as_of"),
                "near_miss": composed.get("near_miss")}
    # Phase 4.1: THIS is the moment a clip grant may be minted — a human just
    # said yes to this specific answer. The grant covers only the events behind
    # the claims they approved, and only for the peer who asked. The auto-
    # answer path deliberately mints nothing: a policy pack is consent to
    # answer questions, not consent to hand over recordings.
    grant = _mint_clip_grant(item.get("peer_id", ""), item["ask_id"],
                             composed.get("claims") or [])
    if grant:
        outbound["clip_grant"] = grant
    delivered = _deliver(peer_rec, outbound)
    out = _complete_or_retry_delivery(
        local_id, delivered, composed["text"], outbound, "answered")
    out["redacted"] = composed["redacted"]
    return out


def _complete_or_retry_delivery(local_id: str, delivered: bool,
                                answer: str | None, outbound: dict,
                                success_status: str) -> dict:
    """Land a decided ask, or keep it open with a retryable outbound payload.

    A failed POST used to call `_finish_ask(..., "delivery_failed")`, which
    moved the row into decided history with no retry path anywhere in the
    tree. The composed/accepted payload is already a human verdict — stash it
    as delivery_pending and let presence flush re-POST.
    """
    if delivered:
        _finish_ask(local_id, success_status, answer)
        return {"ok": True, "status": success_status, "answer": answer}
    _stash_delivery_pending(local_id, answer, outbound,
                            success_status=success_status)
    return {"ok": False, "status": "delivery_pending", "answer": answer}


def _stash_delivery_pending(local_id: str, answer: str | None,
                            outbound: dict, *,
                            success_status: str) -> None:
    with _lock:
        asks = _load(_asks_path(), [])
        for a in asks:
            if a.get("id") == local_id:
                a["status"] = "delivery_pending"
                a["answer"] = answer
                a["outbound"] = outbound
                a["success_status"] = success_status
                a["delivery_attempts"] = int(a.get("delivery_attempts") or 0) + 1
                a["last_delivery_at"] = time.time()
                a["decided_at"] = a.get("decided_at") or time.time()
        _save_asks_partitioned(asks)
    print(f"[peer] delivery pending for ask {local_id}; will retry on presence.")


def _save_asks_partitioned(asks: list) -> None:
    """Bound decided history; never drop pending or delivery_pending rows."""
    open_asks = [a for a in asks if a.get("status") in _OPEN_ASK_STATUSES]
    decided = [a for a in asks if a.get("status") not in _OPEN_ASK_STATUSES]
    decided = decided[-settings.peer.history:]
    _save(_asks_path(), decided + open_asks)


def _finish_ask(local_id: str, status: str, answer: str | None) -> None:
    with _lock:
        asks = _load(_asks_path(), [])
        for a in asks:
            if a.get("id") == local_id:
                a["status"] = status
                a["answer"] = answer
                a["decided_at"] = time.time()
                a.pop("outbound", None)
                a.pop("success_status", None)
        _save_asks_partitioned(asks)


def retry_delivery_pending(peer_id: str | None = None) -> dict:
    """Re-POST stashed outbound answers whose first delivery failed.

    Called from the presence/mailbox flush path. Does not re-compose and does
    not re-ask the human — the verdict and payload are already on the row.
    """
    with _lock:
        asks = _load(_asks_path(), [])
        items = [dict(a) for a in asks
                 if a.get("status") == "delivery_pending"
                 and (peer_id is None or a.get("peer_id") == peer_id)]
        registry = _load(_peers_path(), {})
    flushed = 0
    still = 0
    for item in items:
        local_id = item.get("id")
        peer_rec = registry.get(item.get("peer_id", ""))
        outbound = item.get("outbound")
        if not local_id or not isinstance(outbound, dict) or peer_rec is None:
            still += 1
            continue
        if _deliver(peer_rec, outbound):
            _finish_ask(local_id, item.get("success_status") or "answered",
                        item.get("answer"))
            flushed += 1
        else:
            with _lock:
                asks = _load(_asks_path(), [])
                for a in asks:
                    if a.get("id") == local_id:
                        a["delivery_attempts"] = (
                            int(a.get("delivery_attempts") or 0) + 1)
                        a["last_delivery_at"] = time.time()
                _save_asks_partitioned(asks)
            still += 1
    return {"ok": True, "flushed": flushed, "remaining": still}


# --- asking side (outbound) --------------------------------------------------
def ask(peer_id: str, question: str, kind: str = "question",
        *, loop_id: str | None = None, team_slug: str | None = None,
        team_ask_id: str | None = None) -> dict:
    """Send one question or task handoff to a paired peer. Synchronous when
    their side auto-answers (questions only — handoffs always wait for their
    human); otherwise pending until they decide, delivered to /peer/answer.

    Unreachable peers are queued (status=queued) and retried on presence ping.
    """
    if not settings.peer.enabled:
        return {"ok": False, "error": "peer channel disabled"}
    if kind not in _PEER_KINDS:
        return {"ok": False, "error": f"unknown kind {kind!r}"}
    question = (question or "").strip()[: settings.peer.max_text_chars]
    if not question:
        return {"ok": False, "error": "empty question"}
    # Questions carry a brief from OUR memory so the teammate can build on it
    # instead of inventing (Venture Pulse → fake CRM). Handoffs/notifies stay
    # as the human wrote them.
    if kind == "question":
        try:
            question = enrich_peer_question(question)
        except Exception as exc:
            print(f"[peer] question enrich skipped ({exc}).")
    with _lock:
        registry = _load(_peers_path(), {})
        peer_rec = registry.get(peer_id)
    if peer_rec is None:
        return {"ok": False, "error": "unknown peer"}
    ask_id = uuid.uuid4().hex[:12]
    if kind == "handoff" and not loop_id:
        loop_id = uuid.uuid4().hex[:12]
        try:
            from app.services import team_layer
            team_layer.upsert_loop(loop_id=loop_id, peer_id=peer_id,
                                   peer_name=peer_rec.get("name") or "",
                                   task=question, status="offered",
                                   ask_id=ask_id, side="sender")
        except Exception:
            pass
    with _lock:
        sent = _load(_sent_path(), [])
        sent.append({"ask_id": ask_id, "peer_id": peer_id,
                     "peer_name": peer_rec.get("name", "?"),
                     "question": question, "kind": kind,
                     "loop_id": loop_id, "team_slug": team_slug,
                     "team_ask_id": team_ask_id,
                     "created_at": time.time(),
                     "status": "sent", "answer": None, "answered_at": None})
        _save(_sent_path(), sent)
    return _dispatch_ask(peer_rec, peer_id, ask_id, question, kind, loop_id)


def retry_queued(item: dict) -> dict:
    """Re-send a mailbox row without minting a new ask_id/sent row."""
    ask_id = str(item.get("ask_id") or "")
    peer_id = str(item.get("peer_id") or "")
    question = str(item.get("question") or "")
    kind = str(item.get("kind") or "question")
    loop_id = item.get("loop_id")
    if not ask_id or not peer_id or not question:
        return {"ok": False, "error": "bad mailbox row"}
    with _lock:
        registry = _load(_peers_path(), {})
        peer_rec = registry.get(peer_id)
    if peer_rec is None:
        return {"ok": False, "error": "unknown peer"}
    return _dispatch_ask(peer_rec, peer_id, ask_id, question, kind, loop_id,
                         from_mailbox=True)


def _dispatch_ask(peer_rec: dict, peer_id: str, ask_id: str, question: str,
                  kind: str, loop_id: str | None,
                  from_mailbox: bool = False) -> dict:
    """Send one ask and record the attempt. The telemetry wrapper sits here,
    not in ask(), so mailbox retries are counted too."""
    t0 = time.time()
    res = _dispatch_ask_inner(peer_rec, peer_id, ask_id, question, kind,
                              loop_id, from_mailbox=from_mailbox)
    try:
        from app.services import peer_telemetry
        peer_telemetry.record(
            "ask_sent", ask_id=ask_id, peer_id=peer_id,
            peer_name=peer_rec.get("name", "?"), kind=kind,
            status=res.get("status") or ("error" if not res.get("ok") else "?"),
            question_chars=len(question or ""),
            dispatch_s=round(time.time() - t0, 3),
            from_mailbox=bool(from_mailbox),
            followup=_is_followup(peer_id, ask_id))
    except Exception as exc:
        print(f"[peer] telemetry skipped ({exc}).")
    return res


def _is_followup(peer_id: str, ask_id: str) -> bool:
    """True when this pair already had an answered ask before this one — the
    repeat-use signal, read from the sent log we already keep."""
    try:
        for s in _load(_sent_path(), []):
            if (s.get("peer_id") == peer_id and s.get("ask_id") != ask_id
                    and s.get("status") == "answered"):
                return True
    except Exception:
        pass
    return False


def _dispatch_ask_inner(peer_rec: dict, peer_id: str, ask_id: str,
                        question: str, kind: str, loop_id: str | None,
                        from_mailbox: bool = False) -> dict:
    payload = {"ask_id": ask_id, "question": question, "kind": kind,
               "base_url": my_base_url(), "internal_url": my_internal_url()}
    if loop_id:
        payload["loop_id"] = loop_id
    try:
        res = _post_peer(peer_rec, "/peer/ask", payload)
    except Exception as exc:
        queued = _queue_offline(peer_id, ask_id, question, kind, loop_id, exc)
        if queued:
            return {"ok": True, "status": "queued", "ask_id": ask_id,
                    "peer": peer_rec.get("name", "?"),
                    "error": (f"queued — {peer_rec.get('name', 'peer')} "
                              "unreachable")}
        return {"ok": False, "status": "sent", "ask_id": ask_id,
                "peer": peer_rec.get("name", "?"),
                "error": (f"unreachable and mailbox enqueue failed — "
                          f"{peer_rec.get('name', 'peer')}")}
    if not res.get("ok"):
        _update_sent(ask_id, "refused", None)
        return {"ok": False, "ask_id": ask_id,
                "error": res.get("error", "peer refused"),
                "peer": peer_rec.get("name", "?")}
    if res.get("status") == "answered":
        answer_text = str(res.get("answer") or "")[: settings.peer.max_text_chars]
        claims = _sanitize_claims(res.get("claims"))
        # A synchronous answer can carry a grant too — today only the human
        # approval path mints one, but the asker must not silently drop it if
        # a peer ever answers in-line with one attached.
        _record_answer(peer_rec, peer_id, ask_id, answer_text, claims=claims,
                       as_of=_coerce_as_of(res.get("as_of")),
                       near_miss=bool(res.get("near_miss")),
                       clip_grant=_sanitize_grant(res.get("clip_grant")))
        return {"ok": True, "status": "answered", "ask_id": ask_id,
                "answer": answer_text, "claims": claims or [],
                "as_of": _coerce_as_of(res.get("as_of")),
                "peer": peer_rec.get("name", "?")}
    if res.get("status") == "declined":
        _record_answer(peer_rec, peer_id, ask_id, None, declined=True)
        return {"ok": True, "status": "declined", "ask_id": ask_id,
                "peer": peer_rec.get("name", "?")}
    _update_sent(ask_id, "pending", None)
    return {"ok": True, "status": "pending", "ask_id": ask_id,
            "peer": peer_rec.get("name", "?")}


def _queue_offline(peer_id, ask_id, question, kind, loop_id, exc) -> bool:
    """Mark queued only after the mailbox row is durable. Returns True on success.

    Previously `_update_sent(..., "queued")` ran unconditionally and
    `mailbox_enqueue` was swallowed — status said queued while flush_mailbox
    had nothing to retry.
    """
    try:
        from app.services import team_layer
        team_layer.mailbox_enqueue({
            "ask_id": ask_id, "peer_id": peer_id, "question": question,
            "kind": kind, "loop_id": loop_id,
        })
    except Exception as enq_exc:
        # Leave the row as "sent" (already written at ask()) so a later
        # presence pass / manual retry can still see an outstanding ask.
        print(f"[peer] mailbox enqueue failed for {ask_id} ({enq_exc}); "
              f"not marking queued after {exc}.")
        return False
    _update_sent(ask_id, "queued", None)
    print(f"[peer] queued ask {ask_id} for {peer_id} ({exc}).")
    return True


def _trim_sent(sent: list) -> list:
    """Bound decided history; never drop outstanding (sent/pending/queued).

    Same durability policy as `_save_asks_partitioned` / `_finish_ask` — the
    blind `sent[-N:]` trim used to drop still-open asks so a late answer hit
    "no matching outstanding ask".
    """
    outstanding = [s for s in sent if s.get("status") in _OUTSTANDING_SENT]
    done = [s for s in sent if s.get("status") not in _OUTSTANDING_SENT]
    done = done[-settings.peer.history:]
    return done + outstanding


def nudge_stale_pending() -> list[dict]:
    """Tell the asker when their question is sitting with a human who has not
    looked at it. Called from the presence heartbeat.

    Hosted removed the offline gap, and that changed what "pending" means. It
    used to be a message waiting in someone's inbox, which needs no commentary.
    Now the asker is usually waiting in real time, so silence past a couple of
    minutes is dead air in the UI — and dead air is what teaches a tester the
    feature does not work. This says it out loud, once per ask.
    """
    limit = float(settings.peer.pending_nudge_s or 0)
    if limit <= 0:
        return []
    now = time.time()
    nudged: list[dict] = []
    with _lock:
        sent = _load(_sent_path(), [])
        for s in sent:
            if s.get("status") != "pending" or s.get("nudged_at"):
                continue
            started = float(s.get("created_at") or 0)
            if not started or (now - started) < limit:
                continue
            s["nudged_at"] = now
            nudged.append(dict(s))
        if nudged:
            _save(_sent_path(), sent)
    for s in nudged:
        who = s.get("peer_name") or "Your teammate"
        mins = max(1, int((now - float(s.get("created_at") or now)) // 60))
        _emit_peer_result(
            f"{who} hasn't responded yet — their Sparrow has your question "
            f"(“{str(s.get('question') or '')[:120]}”) and is waiting "
            f"on them to approve it. {mins} min so far; I'll tell you the "
            f"moment it lands.")
        try:
            from app.services import peer_telemetry
            peer_telemetry.record(
                "pending_nudge", ask_id=s.get("ask_id", ""),
                peer_id=s.get("peer_id", ""), peer_name=s.get("peer_name", ""),
                kind=s.get("kind", ""), waited_s=round(now - float(
                    s.get("created_at") or now), 3))
        except Exception as exc:
            print(f"[peer] telemetry skipped ({exc}).")
    return nudged


def _update_sent(ask_id: str, status: str, answer: str | None,
                 claims: list | None = None, as_of: float | None = None,
                 near_miss: bool = False,
                 clip_grant: dict | None = None) -> None:
    with _lock:
        sent = _load(_sent_path(), [])
        for s in sent:
            if s.get("ask_id") == ask_id:
                s["status"] = status
                if answer is not None:
                    s["answer"] = answer
                    s["answered_at"] = time.time()
                # Provenance rides along in the sent log so the asker's UI and
                # synthesize_topic_knowledge can date an answer later without
                # re-asking. Absent for peers on an older build.
                if claims is not None:
                    s["claims"] = claims
                    s["as_of"] = as_of
                    s["near_miss"] = bool(near_miss)
                # The grant is a credential to THEIR recordings; it lives with
                # the answer it was issued for and dies with it.
                if clip_grant is not None:
                    s["clip_grant"] = clip_grant
        _save(_sent_path(), _trim_sent(sent))


def _record_answer(peer_rec: dict, peer_id: str, ask_id: str,
                   answer_text: str | None, declined: bool = False,
                   claims: list | None = None, as_of: float | None = None,
                   near_miss: bool = False,
                   clip_grant: dict | None = None) -> None:
    """Land one inbound answer. Phase 1.4: when the peer sent claims, they are
    persisted with their own provenance and peer attribution, so
    find_peer_answers and synthesize_topic_knowledge inherit dates and sources
    with no further work."""
    status = "declined" if declined else "answered"
    _update_sent(ask_id, status, answer_text or "", claims=claims,
                 as_of=as_of, near_miss=near_miss, clip_grant=clip_grant)
    _touch(peer_id, "answers")
    usable = peer_answer_usable(answer_text or "", claims=claims)
    try:
        from app.services import peer_telemetry
        peer_telemetry.record(
            "answer", ask_id=ask_id, peer_id=peer_id,
            peer_name=peer_rec.get("name", "?"),
            status=status, answer_chars=len(answer_text or ""),
            n_claims=len(claims) if claims is not None else None,
            has_as_of=bool(as_of), near_miss=bool(near_miss),
            usable=bool(usable) if answer_text else False)
    except Exception as exc:
        print(f"[peer] telemetry skipped ({exc}).")
    if answer_text:
        # Attribution in the raw text: this event grounds future chat answers,
        # and a fact learned from a teammate must read as theirs, not ours.
        name = peer_rec.get("name") or "a teammate"
        # A near-miss is an honest "nothing on that, here's what I do have" —
        # a useful thing to SHOW the asker, but not knowledge about the topic
        # they asked about, so it never mints facts.
        if not usable or near_miss:
            why = "near-miss" if near_miss else "unusable"
            print(f"[peer] skip ingest of {why} answer from {name}")
            _publish_event(
                "peer.answer",
                f"[from {name}'s Sparrow — not ingested] {answer_text[:500]}",
                {"peer_id": peer_id, "peer": name, "ask_id": ask_id,
                 "ingested": False, "near_miss": bool(near_miss)})
            return
        if ingest_enabled():
            _ingest_answer(name, peer_id, ask_id, answer_text, claims=claims,
                           as_of=as_of)
        else:
            _publish_event("peer.answer",
                           f"[from {name}'s Sparrow] {answer_text}",
                           {"peer_id": peer_id, "peer": name, "ask_id": ask_id,
                            "claims": claims or [], "as_of": as_of})


# A peer may send at most this many claims per answer, each this long. Bounds,
# not trust: the payload is attacker-controlled in the same sense any paired
# peer's input is.
_MAX_PEER_CLAIMS = 12
_MAX_CLAIM_CHARS = 600


def _sanitize_claims(raw) -> list | None:
    """Normalise an inbound `claims` array, or None when the peer sent none.

    The ids inside a claim identify rows in THEIR database, never ours, so
    they are renamed `peer_fact_id` / `peer_event_id` on the way in. A remote
    id that kept the local name would eventually be read as a local fact id —
    and a collision would attribute their memory to one of our own rows.
    """
    if raw is None:
        return None
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw[:_MAX_PEER_CLAIMS]:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()[:_MAX_CLAIM_CHARS]
        if not text:
            continue
        claim = {"text": text,
                 "speaker": str(item.get("speaker") or "").strip()[:120],
                 "when": str(item.get("when") or "").strip()[:40]}
        for src, dst in (("fact_id", "peer_fact_id"),
                         ("source_event_id", "peer_event_id")):
            try:
                if item.get(src) is not None:
                    claim[dst] = int(item[src])
            except (TypeError, ValueError):
                pass
        ts = _coerce_as_of(item.get("ts"))
        if ts is not None:
            claim["ts"] = ts
        out.append(claim)
    return out


def sent_ask(ask_id: str) -> dict | None:
    """One row from our outbound log, by ask_id."""
    for s in _load(_sent_path(), []):
        if s.get("ask_id") == ask_id:
            return dict(s)
    return None


def peer_record(peer_id: str) -> dict | None:
    """One paired peer's record, or None if we are no longer paired."""
    rec = _load(_peers_path(), {}).get(peer_id)
    return dict(rec) if rec else None


def _sanitize_grant(raw) -> dict | None:
    """Normalise an inbound clip grant: an opaque token plus the events it
    covers, both bounded. Their event ids, like their fact ids, name rows in
    THEIR store — we only ever hand them back to them."""
    if not isinstance(raw, dict):
        return None
    token = str(raw.get("token") or "").strip()
    if not token or len(token) > 200:
        return None
    events: list[int] = []
    for eid in raw.get("events") or []:
        try:
            events.append(int(eid))
        except (TypeError, ValueError):
            continue
    if not events:
        return None
    return {"token": token, "events": events[:12],
            "expires_at": _coerce_as_of(raw.get("expires_at"))}


def _coerce_as_of(value) -> float | None:
    """A finite epoch seconds value, or None. Rejects a peer's clock skew far
    enough out that a freshness stamp would mislead rather than inform."""
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return None
    if ts != ts or ts in (float("inf"), float("-inf")):  # NaN / inf
        return None
    now = time.time()
    if ts <= 0 or ts > now + 86400:      # more than a day in the future
        return None
    return ts


def handle_answer(peer: dict, payload: dict) -> dict:
    """Authenticated inbound delivery of an answer to an ask WE sent. Refused
    unless ask_id matches an outstanding ask to THAT peer — a paired peer
    cannot inject unsolicited 'answers' into memory."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "body must be a JSON object"}
    ask_id = str(payload.get("ask_id") or "").strip()[:64]
    with _lock:
        sent = _load(_sent_path(), [])
        item = next((s for s in sent if s.get("ask_id") == ask_id
                     and s.get("peer_id") == peer.get("peer_id")
                     and s.get("status") in _OUTSTANDING_SENT), None)
    if item is None:
        return {"ok": False, "error": "no matching outstanding ask"}
    # Answer may land while we still show queued (mailbox not yet flushed).
    try:
        from app.services import team_layer
        team_layer.mailbox_remove(ask_id)
    except Exception:
        pass
    if payload.get("declined"):
        _record_answer(peer, peer["peer_id"], ask_id, None, declined=True)
        _notify_chat(f"{peer.get('name', 'A teammate')}'s Sparrow declined "
                     f"to answer: “{item.get('question', '')[:120]}”")
        _after_answer(item, declined=True)
        return {"ok": True, "status": "declined"}
    answer_text = str(payload.get("answer") or "").strip()
    if not answer_text:
        return {"ok": False, "error": "empty answer"}
    answer_text = answer_text[: settings.peer.max_text_chars]
    claims = _sanitize_claims(payload.get("claims"))
    _record_answer(peer, peer["peer_id"], ask_id, answer_text,
                   claims=claims, as_of=_coerce_as_of(payload.get("as_of")),
                   near_miss=bool(payload.get("near_miss")),
                   clip_grant=_sanitize_grant(payload.get("clip_grant")))
    print(f"[peer] answer from {peer.get('name', '?')}: {answer_text[:80]}")
    _notify_chat(f"{peer.get('name', 'A teammate')}'s Sparrow answered: "
                 f"“{answer_text[:400]}”")
    _after_answer(item, declined=False)
    return {"ok": True, "status": "recorded"}


def _after_answer(item: dict, declined: bool) -> None:
    try:
        from app.services import team_layer
        if item.get("kind") == "handoff" and item.get("loop_id"):
            team_layer.mark_loop(item["loop_id"],
                                 "declined" if declined else "open")
        rollup = team_layer.maybe_rollup(item.get("team_ask_id"))
        if rollup:
            _notify_chat(rollup)
    except Exception:
        pass


# --- claims with provenance (Phase 3) ---------------------------------------
def ingest_enabled() -> bool:
    """Answered peer asks become memory + graph claims. QUILL_PEER_INGEST=0
    keeps them as plain context events instead."""
    import os
    return os.environ.get("QUILL_PEER_INGEST", "1") not in ("0", "false", "False")


def _ingest_answer(name: str, peer_id: str, ask_id: str,
                   answer_text: str, claims: list | None = None,
                   as_of: float | None = None) -> None:
    """Store one answered ask as a TEXT event and queue fact extraction — the
    same chain typed chat takes (chat_ingest), with three deliberate downgrades
    for hearsay: OBSERVED tier (the teammate's ASSISTANT said it — this user
    neither said nor witnessed it), source=peer.answer (the source-policy
    engine's `peer_answer` class: no contact scraping, no identity evidence,
    no people updates), and the belief store weighs it below hearing the
    teammate directly. Facts still pass the write-time hygiene gate, and
    conflicts with what this user knows go through normal KG adjudication —
    a teammate's claim can never silently overwrite the user's own."""
    try:
        from app.events import Event, Modality
        from app.services.attachments import _index_event
        from app.storage import get_store

        text = f"[from {name}'s Sparrow] {answer_text}"
        # Phase 1.4: the peer's provenance rides in meta — their fact/event
        # ids (peer-scoped, never confused with ours), who asserted each
        # claim, and as_of. find_peer_answers and synthesize_topic_knowledge
        # inherit dates and sources from here with no further work, so a stale
        # teammate answer can be recognised as stale instead of read as current.
        meta = {"origin": "peer", "peer_id": peer_id, "peer": name,
                "ask_id": ask_id}
        if claims:
            meta["peer_claims"] = claims
            meta["as_of"] = as_of
        ev = Event(
            time=time.time(), modality=Modality.TEXT, raw=text,
            summary=f"[peer.answer] {text[:120]}", source="peer.answer",
            meta=meta,
        )
        _conf.attach(ev, _conf.OBSERVED)
        anchor = get_store().insert(ev)
        _index_event(anchor, ev)
        from app.services.worker import worker
        worker.enqueue("peer_ingest",
                       payload={"event_id": anchor, "text": answer_text,
                                "peer": name})
    except Exception as exc:
        print(f"[peer] answer ingest skipped ({exc}).")


def run_ingest_job(payload: dict) -> None:
    """Worker job: mine one peer answer for facts via the same extractor +
    hygiene gate as speech/chat, then chain a graph rebuild. Registered in
    main.py alongside chat_ingest (only when extraction is on)."""
    text = (payload or {}).get("text") or ""
    anchor = (payload or {}).get("event_id")
    if not text:
        return
    from app.services.documents import _persist_facts
    from app.services.extractor import extractor
    from app.services.worker import worker
    from app.storage import get_store

    store = get_store()
    now = time.time()
    facts = extractor._extract_text(text)
    n = _persist_facts(store, facts, anchor, text, now,
                       event_source=(payload or {}).get("source")
                       or "peer.answer")
    try:
        if anchor is not None:
            store.mark_extracted([anchor], now)
    except Exception:
        pass
    if n:
        print(f"[peer] {n} fact(s) from a teammate's answer.")
        worker.enqueue("graph", unique=True)


# --- chat intent (ask a teammate from the chat box) -------------------------
# Deterministic, calendar_intent-style: "ask sarah: are the slides done?" or
# "ask sarah's mnemos whether the slides are done". The addressee must resolve
# to a PAIRED peer or the message is not a team ask — "ask me anything" and
# "ask the professor about X" fall through to normal chat routing.
#
# Push form ("tell Justin what we did today"): compose from OUR memory, then
# deliver as kind=notify so THEIR Sparrow surfaces the update. Distinct from
# ask (pull from their memory) and handoff (they take a task).
_ASK_COLON_RE = re.compile(
    r"^\s*ask\s+(?P<who>[^:,]{1,40}?)\s*[:,]\s*(?P<q>.{3,})$", re.I)
_ASK_PLAIN_RE = re.compile(
    r"^\s*ask\s+(?P<who>[A-Za-z][\w.'-]{0,40})\s+(?P<q>.{3,})$", re.I)
_POSSESSIVE_RE = re.compile(r"(?:'s)?\s+(?:mnemos|assistant|instance|sparrow)\s*$", re.I)
_TELL_ME_RE = re.compile(r"^\s*tell\s+me\b", re.I)
_TELL_RE = re.compile(
    r"^\s*(?:tell|message|msg)\s+"
    r"(?P<who>[A-Za-z][\w.'-]{0,40}(?:\s+[A-Za-z][\w.'-]{0,40})?)\s+"
    r"(?:that\s+|about\s+|[:\,]\s*)?(?P<q>.{3,})$", re.I)
_LET_KNOW_RE = re.compile(
    r"^\s*let\s+"
    r"(?P<who>[A-Za-z][\w.'-]{0,40}(?:\s+[A-Za-z][\w.'-]{0,40})?)\s+"
    r"know\s+(?:that\s+|about\s+)?(?P<q>.{3,})$", re.I)
# Explicit human-app channels win over peer notify ("email Justin…").
_EXPLICIT_CHANNEL_RE = re.compile(
    r"^\s*(?:email|e-mail|mail|text|sms|imessage|i-message|slack|whatsapp|"
    r"discord|telegram|messenger|gchat|hangouts)\b", re.I)
_EMAIL_ENVELOPE_RE = re.compile(
    r"^\s*(?:to|subject|body|cc|bcc)\s*:\s*", re.I | re.M)

_PEER_KINDS = ("question", "handoff", "notify",
               "org_digest", "org_priority", "org_escalate")
_ORG_KINDS = ("org_digest", "org_priority", "org_escalate")


def _person_alias_keys(person_id: int) -> set[str]:
    """Display + alias tokens for a linked Person (casefolded)."""
    keys: set[str] = set()
    try:
        from app.services.memory import memory
        p = memory._ensure_store().get_person(int(person_id))
        if not p:
            return keys
        names = [p.get("name"), p.get("canonical_name")]
        aliases = p.get("aliases") or []
        if isinstance(aliases, list):
            names.extend(aliases)
        for raw in names:
            n = str(raw or "").casefold().strip()
            if n:
                keys.add(n)
                keys.add(n.split()[0])
    except Exception:
        pass
    return keys


def _resolve_peer_name(who: str) -> dict | None:
    """A paired peer whose name matches `who` (case-insensitive; full name or
    first token; trailing "'s mnemos/assistant" stripped), else None.

    Also matches aliases of a user-linked Person — only when that person still
    maps to a paired peer_id (never invents a peer from a Person alone).
    """
    who = _POSSESSIVE_RE.sub("", (who or "").strip())
    who = re.sub(r"'s$", "", who, flags=re.I)
    key = who.casefold().strip()
    if not key:
        return None
    for p in peers():
        name = str(p.get("name") or "").casefold()
        if name and (key == name or key == name.split()[0]):
            return p
        pid = p.get("person_id")
        if pid is not None and key in _person_alias_keys(int(pid)):
            return p
    return None


def parse_team_ask(text: str) -> dict | None:
    """{"peer_id", "peer_name", "question"} when `text` is a chat request to
    ask a paired teammate's instance, else None (never guesses).

    Group form (`ask #platform: …` / `ask the platform team: …`) returns
    fanout=True even when the team is unknown, so chat does not fall through.

    Resolves the addressee against paired peer names with LONGEST match first
    so "ask User 2 about Venture Pulse" keeps the question as
    "about Venture Pulse" — not "2 about Venture Pulse" (the single-token
    regex used to bind who=User and leave the digit in the question).
    """
    try:
        from app.services.team_layer import parse_group_ask
        group = parse_group_ask(text)
        if group is not None:
            return group
    except Exception:
        pass
    t = (text or "").strip()
    # "asking …" is narrative, not an imperative team ask.
    if not t or re.match(r"^\s*asking\b", t, re.I):
        return None
    if not re.match(r"^\s*ask\b", t, re.I):
        return None
    rest = re.sub(r"^\s*ask\s+", "", t, count=1, flags=re.I).lstrip()
    if not rest:
        return None

    candidates: list[tuple[int, dict, str]] = []
    for p in peers():
        name = str(p.get("name") or "").strip()
        keys: set[str] = set()
        if name:
            keys.add(name)
            keys.add(name.split()[0])
        pid = p.get("person_id")
        if pid is not None:
            keys |= _person_alias_keys(int(pid))
        for key in keys:
            if not key or not rest.casefold().startswith(key.casefold()):
                continue
            after = rest[len(key):]
            after = _POSSESSIVE_RE.sub("", after)
            after = re.sub(r"^'s\s*", "", after, flags=re.I).lstrip()
            after = re.sub(r"^[:,]\s*", "", after)
            if len(after.strip()) < 3:
                continue
            candidates.append((len(key), p, after.strip()))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    _, peer, question = candidates[0]
    # "ask sarah to review the slides" is a HANDOFF (do this), not a
    # question (tell me this) — her human must accept it.
    kind = "question"
    hand = re.match(r"to\s+(.+)$", question, re.I | re.S)
    if hand:
        kind, question = "handoff", hand.group(1).strip()
    if not question.rstrip("?").strip():
        return None
    return {"peer_id": peer["peer_id"], "peer_name": peer["name"],
            "question": question, "kind": kind}


def looks_like_team_tell(text: str) -> bool:
    """Shape check only — true for tell/message/let-know forms (not 'tell me').

    Explicit human-app verbs (email/text/sms/…) are never peer-tell shapes so
    Writing / phone / ghost-browser paths still own those.
    """
    t = (text or "").strip()
    if not t or _TELL_ME_RE.match(t) or _EXPLICIT_CHANNEL_RE.match(t):
        return False
    return bool(_TELL_RE.match(t) or _LET_KNOW_RE.match(t)
                or re.match(r"^\s*(?:tell|message|msg)\s+\S+", t, re.I)
                or re.match(r"^\s*let\s+\S+\s+know\b", t, re.I))


_RECALL_RE = re.compile(
    r"^\s*what\s+(?P<aux>did|does|has)\s+"
    r"(?P<who>.+?)\s+"
    r"(?:'s\s+)?(?:sparrow|mnemos|assistant)\s+"
    r"(?P<verb>say|said|tell|told|answer(?:ed)?|know|known)\s+"
    r"(?:about\s+|regarding\s+|on\s+)?(?P<topic>.+?)\s*\??\s*$",
    re.I | re.S,
)


def parse_peer_recall(text: str) -> dict | None:
    """"What did User 2's Sparrow say about Venture Pulse?" → peer + topic.

    These are recall/re-ask intents, not free-form chat — without this they
    fall through to the agent, which narrates an open task instead of
    surfacing (or refreshing) the peer answer.
    """
    m = _RECALL_RE.match(text or "")
    if not m:
        return None
    who = m.group("who").strip()
    who = _POSSESSIVE_RE.sub("", who).strip()
    who = re.sub(r"'s$", "", who, flags=re.I).strip()
    peer = _resolve_peer_name(who)
    if peer is None:
        # Longest paired-name prefix (User 2, not User).
        rest = who
        candidates: list[tuple[int, dict]] = []
        for p in peers():
            name = str(p.get("name") or "").strip()
            if name and rest.casefold().startswith(name.casefold()):
                candidates.append((len(name), p))
        if not candidates:
            return None
        candidates.sort(key=lambda x: -x[0])
        peer = candidates[0][1]
    topic = (m.group("topic") or "").strip().rstrip("?").strip()
    if len(topic) < 2:
        return None
    return {"peer_id": peer["peer_id"], "peer_name": peer["name"],
            "topic": topic}


def peer_answer_usable(answer: str, question: str = "",
                       claims: list | None = None) -> bool:
    """True when a stored peer answer is worth showing/ingesting.

    Phase 1.5 — structural: an answer is usable when it carries at least one
    claim. `claims=[]` from an upgraded peer means "I have nothing typed on
    this", which is honest but not knowledge, so it does not ingest.

    `claims=None` means the peer did not send structure — an older build, or a
    row stored before this phase. Those fall through to the legacy heuristic
    below, which is the substring blocklist this phase exists to retire. It
    stays for INGRESS compatibility only; nothing on the egress path consults
    it, and it can be deleted once every paired instance sends claims.
    """
    if claims is not None:
        return bool(claims)
    text = _strip_peer_update_leaks(answer or "").strip()
    if len(text) < 8:
        return False
    # Normalize curly quotes so "Here's what I found" still matches.
    low = (text.lower()
           .replace("\u2019", "'").replace("\u2018", "'")
           .replace("\u201c", '"').replace("\u201d", '"'))
    if "here's what i found" in low or ":::confirmed" in low:
        return False
    if "you are sparrow" in low or "user you are assisting" in low:
        return False
    if "would you like me to ask" in low or "want me to ask" in low:
        return False
    if "exact details are incomplete" in low:
        return False
    if "not a person" in low and "based on the context" in low:
        return False
    if "i would need to check the latest information" in low:
        return False
    if "not ingested" in low:
        return False
    if "make a note of it" in low or "i can make a note" in low:
        return False
    # Pure echo of the asker's brief — no new teammate knowledge.
    if "based on the context" in low and (
            "new project that you have been put on" in low
            or "don't have more specific details" in low):
        return False
    if question and "Context from my side" in question:
        brief = question.split("Context from my side", 1)[-1]
        brief = re.sub(r"^[^:\n]*:\s*", "", brief).strip().casefold()
        brief_toks = set(re.findall(r"[a-z0-9]{4,}", brief))
        ans_toks = set(re.findall(r"[a-z0-9]{4,}", low))
        if brief_toks and ans_toks:
            overlap = len(brief_toks & ans_toks) / max(len(ans_toks), 1)
            if overlap >= 0.7 and len(ans_toks - brief_toks) < 4:
                return False
    return True


def find_peer_answers(peer_id: str | None, topic: str, *, limit: int = 5) -> list[dict]:
    """Recent answered outbound asks (optionally one peer) that mention `topic`."""
    topic_l = (topic or "").casefold().strip()
    tokens = [t for t in re.findall(r"[a-z0-9]{3,}", topic_l) if t not in
              {"the", "and", "for", "about", "what", "know", "said", "tell"}]
    out: list[dict] = []
    for r in _load(_sent_path(), []):
        if peer_id and r.get("peer_id") != peer_id:
            continue
        if r.get("status") != "answered":
            continue
        q = str(r.get("question") or "")
        a = str(r.get("answer") or "")
        blob = f"{q}\n{a}".casefold()
        if topic_l and topic_l not in blob:
            if not tokens or not any(t in blob for t in tokens):
                continue
        claims = r.get("claims")
        # Rows stored before Phase 1 (and answers from peers on an older
        # build) have no claims, so they still get the legacy text cleanup.
        cleaned = a.strip() if claims else _strip_peer_update_leaks(a).strip()
        out.append({
            "ask_id": r.get("ask_id"),
            "peer_name": r.get("peer_name") or "teammate",
            "question": q,
            "answer": cleaned,
            "claims": claims or [],
            "as_of": r.get("as_of"),
            "near_miss": bool(r.get("near_miss")),
            "created_at": r.get("created_at") or r.get("answered_at") or 0,
            "usable": peer_answer_usable(cleaned, q, claims=claims),
        })
    # Newest last, and the caller takes the tail — but rank by the answer's
    # own freshness when the peer told us one, not by when we happened to ask.
    out.sort(key=lambda x: float(x.get("as_of") or x.get("created_at") or 0))
    return out[-limit:]


_WHAT_WE_KNOW_RE = re.compile(
    r"^\s*what\s+do\s+we\s+(?:now\s+)?know\s+about\s+(?P<topic>.+?)\s*\??\s*$",
    re.I | re.S,
)


def parse_what_we_know(text: str) -> str | None:
    """'What do we know about Venture Pulse now?' → topic, else None."""
    m = _WHAT_WE_KNOW_RE.match(text or "")
    if not m:
        return None
    topic = (m.group("topic") or "").strip().rstrip("?").strip()
    topic = re.sub(r"\s+now$", "", topic, flags=re.I).strip()
    return topic or None


def synthesize_topic_knowledge(topic: str) -> str:
    """Blend our facts + usable teammate answers for a topic — no re-ask."""
    topic = (topic or "").strip()
    if not topic:
        return ""
    own: list[str] = []
    try:
        from app.services import peer_retrieval as pr
        # Same typed path the egress composer uses, so "what do we know" is
        # answered from dated facts rather than rendered grounding lines.
        for c in pr.facts_for_topic(topic, limit=8):
            when = pr.describe_age(c["ts"])
            who = c.get("speaker") or ""
            stamp = " · ".join([p for p in (who, when) if p])
            own.append(f"{c['text']}" + (f" ({stamp})" if stamp else ""))
    except Exception as exc:
        print(f"[peer] topic synthesis grounding skipped ({exc}).")
    peer_bits: list[str] = []
    for h in find_peer_answers(None, topic, limit=8):
        if not h.get("usable") or h.get("near_miss"):
            continue
        who = h.get("peer_name") or "a teammate"
        # Their answer's own freshness, not when we asked — "as of Aug 14"
        # changes how the reader uses it.
        stamp = ""
        if h.get("as_of"):
            from app.services import peer_retrieval as pr
            stamp = f" (as of {pr.describe_age(h['as_of'])})"
        peer_bits.append(f"From {who}'s Sparrow{stamp}: {h['answer']}")
    lines: list[str] = []
    if own:
        lines.append("From our memory:")
        lines.extend(f"- {x}" for x in own)
    if peer_bits:
        if lines:
            lines.append("")
        lines.append("From teammates:")
        lines.extend(f"- {x}" for x in peer_bits)
    if not lines:
        return (
            f"We only know what you've told me so far about {topic}, and no "
            f"teammate has added usable detail yet. You can "
            f"`ask <teammate>: what do you know about {topic}?` to pull more."
        )
    return "\n".join(lines)


def parse_team_tell(text: str) -> dict | None:
    """{"peer_id", "peer_name", "topic", "kind": "notify"} when chat is a push
    update to a PAIRED peer ("tell Justin what we did today"), else None.

    Resolves the addressee against paired peer names first (longest match)
    so "what/about/that" never get eaten as a surname. Never guesses.
    """
    t = (text or "").strip()
    if not t or _TELL_ME_RE.match(t) or _EXPLICIT_CHANNEL_RE.match(t):
        return None
    lower = t.casefold()
    rest = None
    for pref in ("tell ", "message ", "msg "):
        if lower.startswith(pref):
            rest = t[len(pref):].lstrip()
            break
    if rest is None and lower.startswith("let "):
        # "let <who> know <topic>"
        after = t[4:].lstrip()
        know_m = re.match(
            r"^(?P<who>.+?)\s+know\s+(?:that\s+|about\s+)?(?P<q>.{3,})$",
            after, re.I | re.S)
        if not know_m:
            return None
        peer = _resolve_peer_name(know_m.group("who"))
        if peer is None:
            return None
        return {"peer_id": peer["peer_id"], "peer_name": peer["name"],
                "topic": know_m.group("q").strip(), "kind": "notify"}
    if rest is None:
        return None

    # Prefer the longest paired peer name that prefixes `rest`.
    candidates: list[tuple[int, dict, str]] = []
    for p in peers():
        name = str(p.get("name") or "").strip()
        if not name:
            continue
        for key in {name, name.split()[0]}:
            if rest.casefold().startswith(key.casefold()):
                after = rest[len(key):].lstrip()
                after = re.sub(r"^(?:that|about)\s+", "", after, flags=re.I)
                after = re.sub(r"^[:,]\s*", "", after)
                if len(after) >= 3:
                    candidates.append((len(key), p, after))
        pid = p.get("person_id")
        if pid is not None:
            for alias in _person_alias_keys(int(pid)):
                if rest.casefold().startswith(alias):
                    after = rest[len(alias):].lstrip()
                    after = re.sub(r"^(?:that|about)\s+", "", after, flags=re.I)
                    after = re.sub(r"^[:,]\s*", "", after)
                    if len(after) >= 3:
                        candidates.append((len(alias), p, after))
    if not candidates:
        return None
    candidates.sort(key=lambda x: -x[0])
    _, peer, topic = candidates[0]
    return {"peer_id": peer["peer_id"], "peer_name": peer["name"],
            "topic": topic.strip(), "kind": "notify"}


_REMIND_ME_SENT_RE = re.compile(
    r"(?:^|(?<=[.!?]))\s*(?:and\s+|also\s+|then\s+)?remind me\b[^.!?]*[.!?]?",
    re.I)


def _strip_self_reminders(topic: str) -> str:
    """Drop 'remind me …' sentences from a tell topic — they address Sparrow,
    not the teammate (extraction already turned them into local tasks)."""
    out = _REMIND_ME_SENT_RE.sub(" ", topic or "").strip()
    return out if out else (topic or "")


def _strip_notify_envelope(text: str) -> str:
    """Drop To:/Subject:/Body: scaffolding models sometimes paste into updates."""
    lines = []
    for line in (text or "").splitlines():
        if _EMAIL_ENVELOPE_RE.match(line):
            # Keep content after the first Body: label; drop bare header lines.
            if re.match(r"^\s*body\s*:\s*", line, re.I):
                rest = re.sub(r"^\s*body\s*:\s*", "", line, flags=re.I).strip()
                if rest:
                    lines.append(rest)
            continue
        lines.append(line)
    out = "\n".join(lines).strip()
    # Single-line "To: X Subject: Y Body: Z" mash — keep only after Body:.
    mashed = re.search(r"\bbody\s*:\s*(.+)$", out, re.I | re.S)
    if mashed and re.search(r"\b(?:to|subject)\s*:", out, re.I):
        out = mashed.group(1).strip()
    return out


_LEAK_LINE_RE = re.compile(
    r"(?i)^(?:\s*[-•*]?\s*)?(?:"
    r"about you\b|"
    r"you are (?:sparrow|quill|mnemos|the user.?s)|"
    r"the user you are assisting\b|"
    r"address them by name\b|"
    r"user profile\b|"
    r"user tools\b|"
    r"drafting rule\b|"
    r"here'?s what i found,? with the evidence\b|"
    r"retrieved memories\b|"
    r":::(?:confirmed|likely|conflicting|missing)\b"
    r")"
)


def _strip_peer_update_leaks(text: str) -> str:
    """Remove identity / answer_check / system-prompt lines that must never
    leave as a teammate notify body."""
    kept: list[str] = []
    skipping_fence = False
    for line in (text or "").splitlines():
        raw = line.strip()
        if re.match(r"^:::(?:confirmed|likely|conflicting|missing)\s*$", raw, re.I):
            skipping_fence = True
            continue
        if raw == ":::" and skipping_fence:
            skipping_fence = False
            continue
        if skipping_fence:
            continue
        if _LEAK_LINE_RE.match(raw):
            continue
        kept.append(line)
    out = "\n".join(kept).strip()
    # If the model mostly echoed instructions, treat as empty.
    low = out.lower()
    if ("you are sparrow" in low or "user you are assisting" in low
            or "about you (the assistant)" in low):
        return ""
    return out


def _emit_peer_result(text: str) -> None:
    """Peer outcomes as chat results (not system whispers / Takeaway email)."""
    try:
        from app.services import agent_bridge
        agent_bridge.worker._emit("result", text)
    except Exception:
        _notify_chat(text)


def _chat_ask_run(peer_id: str, question: str, kind: str = "question") -> None:
    """One chat-initiated ask/handoff/notify, end to end; every outcome lands
    in the chat pane. Runs on a background thread — the peer's compose or
    approval can take a while and /chat must return immediately."""
    res = ask(peer_id, question, kind)
    name = res.get("peer") or "the teammate"
    status = res.get("status")
    if kind == "notify":
        if status == "answered":
            _emit_peer_result(f"Delivered to {name}'s Sparrow.")
        elif status == "pending":
            _emit_peer_result(
                f"Sent to {name}'s Sparrow — waiting for them to accept "
                "on Team (/peer).")
        elif status == "queued":
            _emit_peer_result(
                f"{name}'s Sparrow isn't reachable — queued until they're online.")
        elif status == "declined":
            _emit_peer_result(f"{name}'s Sparrow declined the update.")
        else:
            _emit_peer_result(
                f"Couldn't reach {name}'s Sparrow "
                f"({res.get('error', 'unknown error')}).")
        return
    if status == "answered":
        _notify_chat(f"{name}'s Sparrow answered: “{res.get('answer', '')}”")
    elif status == "pending" and kind == "handoff":
        _notify_chat(f"Handed off to {name} — waiting for them to accept.")
    elif status == "pending":
        _notify_chat(f"Asked {name}'s Sparrow — it's waiting for their "
                     "approval; I'll surface the answer when it arrives.")
    elif status == "queued":
        _notify_chat(f"{name}'s Sparrow isn't reachable — queued until "
                     "they're online.")
    elif status == "declined":
        _notify_chat(f"{name}'s Sparrow declined to answer that.")
    else:
        _notify_chat(f"I couldn't reach {name}'s Sparrow "
                     f"({res.get('error', 'unknown error')}).")


def _chat_tell_run(peer_id: str, peer_name: str, topic: str) -> None:
    """Compose an update from OUR memory, then OFFER it — the human's 'yes'
    is the send. Outbound peer messages are egress, so they never leave on
    the model's own judgment (the QUILL_PEER_AUTO_ANSWER dev/sim flag keeps
    the old compose-and-push flow for simulations)."""
    try:
        composed = compose_peer_update(_strip_self_reminders(topic))
        body = (composed.get("text") or "").strip()
        if not body:
            _emit_peer_result(
                f"I don't have enough in memory yet to update {peer_name} "
                f"about “{topic[:120]}”.")
            return
        if settings.peer.auto_answer:
            _emit_peer_result(
                f"Update for {peer_name}'s Sparrow\n\n{body}")
            _chat_ask_run(peer_id, body, kind="notify")
            return
        from app.services import agent_bridge
        agent_bridge.worker.propose_peer_tell(peer_id, peer_name, body)
    except Exception as exc:
        _emit_peer_result(
            f"Couldn't prepare an update for {peer_name} ({exc}).")


def chat_ask_async(peer_id: str, question: str,
                   kind: str = "question") -> None:
    threading.Thread(target=_chat_ask_run, args=(peer_id, question, kind),
                     daemon=True).start()


def chat_tell_async(peer_id: str, peer_name: str, topic: str) -> None:
    threading.Thread(target=_chat_tell_run,
                     args=(peer_id, peer_name, topic),
                     daemon=True).start()


def answers(ask_id: str | None = None) -> list[dict]:
    """Sent asks with their current status/answers — for the UI and polling.

    Carries the answer's freshness stamp and WHICH clips the teammate approved,
    but never the grant token itself: `/peer/clip/play` looks that up server-
    side by ask_id, so the credential has no reason to reach a browser.
    """
    rows = _load(_sent_path(), [])
    if ask_id:
        rows = [r for r in rows if r.get("ask_id") == ask_id]
    out = []
    for r in rows:
        row = {k: r.get(k) for k in ("ask_id", "peer_name", "question",
                                     "status", "answer", "created_at",
                                     "answered_at", "kind", "loop_id",
                                     "team_slug", "team_ask_id")}
        row["as_of"] = r.get("as_of")
        row["near_miss"] = bool(r.get("near_miss"))
        grant = r.get("clip_grant") or {}
        if grant.get("events"):
            row["clip_grant"] = {"events": list(grant["events"]),
                                 "expires_at": grant.get("expires_at")}
        out.append(row)
    return out


def status() -> dict:
    """One snapshot for pages: peers + pairing + queues."""
    out = {"enabled": settings.peer.enabled,
            "auto_answer": settings.peer.auto_answer,
            "name": instance_name(),
            "base_url": my_base_url(),
            "internal_url": my_internal_url(),
            "classes": list(CLASSES),
            "actions": list(ACTIONS),
            "peers": peers(),
            "pairing_active": pairing_active(),
            "pending_asks": pending_asks(),
            "sent": answers()[-20:]}
    try:
        from app.services import team_layer
        out.update(team_layer.status_bits())
    except Exception:
        pass
    return out
