"""Node -> Org Record Service client (records layer).

Separate from app/services/org_client.py, which talks to the legacy JSON
coordinator (directory, digests). This one holds the node's org MEMBERSHIP:
the credential minted when an invite was redeemed, the short-lived access
token exchanged for it, and the caches the service pushes on heartbeat
(policy, scopes + this member's grants). Caches drive the UI only — every
permission check happens in the service at request time.

Transport is urllib by default; tests swap in a function that routes to the
service's FastAPI app in-process (`set_transport`).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable
from urllib import error, request

from app.atomic_json import write_json

_TIMEOUT_S = 10.0
_CHAT_TIMEOUT_S = 3.0
_transport: Callable[..., tuple[int, dict]] | None = None
_access: dict[str, Any] = {}
_warned_unreadable = False


class OrgUnavailable(RuntimeError):
    """The service could not be reached (network, 5xx). Retry later."""


class OrgRefused(RuntimeError):
    """The service answered and said no. `code` is its error code."""

    def __init__(self, status: int, code: str, detail: str = "") -> None:
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


def set_transport(fn: Callable[..., tuple[int, dict]] | None) -> None:
    global _transport
    _transport = fn
    _access.clear()


def _path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "org_membership.json"


def membership() -> dict[str, Any]:
    try:
        raw = _path().read_text("utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except ValueError:
        # A crash can leave this file zeroed, and the node then looks like it
        # never joined. Say so, or nobody notices until sharing stops working.
        global _warned_unreadable
        if not _warned_unreadable:
            _warned_unreadable = True
            print(f"[records.org_client] {_path().name} is unreadable; this "
                  "node is out of the org until it is restored.")
        return {}


def _save(data: dict[str, Any]) -> None:
    p = _path()
    # Synced before the replace: this holds the node credential, and a power
    # loss must not zero it. The temp file is already mode 0600.
    write_json(p, data, sort_keys=True)


def joined() -> bool:
    m = membership()
    return bool(m.get("member_id") and m.get("credential"))


def member_id() -> str | None:
    return membership().get("member_id")


def cached_scopes() -> list[dict[str, Any]]:
    return list(membership().get("scopes") or [])


def cached_permissions(scope_id: str) -> set[str]:
    for s in cached_scopes():
        if s.get("id") == scope_id:
            return set(s.get("permissions") or [])
    return set()


def _http(method: str, url: str, body: dict | None,
          headers: dict[str, str],
          timeout: float = _TIMEOUT_S) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = request.Request(url, data=data, method=method,
                          headers={"Content-Type": "application/json",
                                   **headers})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status), json.loads(resp.read() or b"{}")
    except error.HTTPError as exc:
        try:
            payload = json.loads(exc.read() or b"{}")
        except ValueError:
            payload = {}
        return int(exc.code), payload
    except (error.URLError, TimeoutError, OSError) as exc:
        raise OrgUnavailable(str(exc)) from exc


def _call(method: str, path: str, body: dict | None = None, *,
          auth: bool = True, base_url: str | None = None,
          timeout: float = _TIMEOUT_S) -> dict:
    base = (base_url or membership().get("service_url") or "").rstrip("/")
    if not base and _transport is None:
        raise OrgUnavailable("not joined to an org")
    headers: dict[str, str] = {}
    if auth:
        headers["Authorization"] = f"Bearer {_access_token()}"
    send = _transport or (lambda m, p, b, h: _http(m, base + p, b, h,
                                                     timeout))
    status, payload = send(method, path, body, headers)
    if status >= 500:
        raise OrgUnavailable(f"{status} {payload}")
    if status >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        code = (detail.get("code") if isinstance(detail, dict) else None) or \
            str(status)
        msg = detail.get("message") if isinstance(detail, dict) else str(detail)
        if status == 401 and auth:
            _access.clear()
        raise OrgRefused(status, code, msg or "")
    return payload


def _access_token() -> str:
    if _access.get("token") and float(_access.get("exp") or 0) > time.time() + 30:
        return _access["token"]
    m = membership()
    if not m.get("credential"):
        raise OrgUnavailable("not joined to an org")
    out = _call("POST", "/auth/token", {"credential": m["credential"]},
                auth=False)
    _access.update(token=out["access_token"], exp=float(out["expires_at"]))
    return _access["token"]


def join(service_url: str, invite_code: str, node_id: str, *,
         label: str = "", store=None) -> dict[str, Any]:
    out = _call("POST", "/join", {"invite_code": invite_code,
                                  "node_id": node_id, "label": label},
                auth=False, base_url=service_url)
    _save({"service_url": service_url.rstrip("/"), "org_id": out["org_id"],
           "member_id": out["member_id"], "node_id": node_id,
           "credential": out["credential"], "joined_at": time.time(),
           "scopes": [], "policy": {}})
    _access.clear()
    heartbeat(store)
    return {k: v for k, v in out.items() if k != "credential"}


def _my_peer_url() -> str:
    try:
        from app.services import peer_channel
        return peer_channel.my_base_url()
    except Exception:
        return ""


def heartbeat(store=None) -> dict[str, Any]:
    """Refresh policy + scope/grant caches, then reconcile what the service
    reports back: write-back outcomes for my packets, drift on scopes I
    approve, and the org team roster. Missed pushes reconcile here."""
    from urllib.parse import urlencode
    out = _call("GET", "/nodes/heartbeat?" + urlencode(
        {"peer_url": _my_peer_url()}))
    m = membership()
    m.update(scopes=out.get("scopes") or [], policy=out.get("policy") or {},
             rung=out.get("rung") or m.get("rung"),
             team_index=out.get("team_index") or m.get("team_index"),
             heartbeat_at=time.time(), org_id=out.get("org_id", m.get("org_id")),
             roster=out.get("roster") or [])
    _save(m)
    try:
        from app.services.records import retention
        retention.save_org_policy(out.get("policy") or {})
    except Exception as exc:
        print(f"[records.org_client] policy save skipped ({exc}).")
    try:
        from app.services import team_layer
        team_layer.sync_org_teams(out.get("roster") or [],
                                  my_member_id=m.get("member_id"))
    except Exception as exc:
        print(f"[records.org_client] org team sync skipped ({exc}).")
    if out.get("sync") or out.get("drift"):
        try:
            from app.services.records import promotion
            if store is None:
                from app.storage import get_store
                store = get_store()
            out["reconciled"] = promotion.reconcile(
                store, sync=out.get("sync") or [], drift=out.get("drift") or [])
        except Exception as exc:
            print(f"[records.org_client] reconcile skipped ({exc}).")
    return out


def preview(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return list(_call("POST", "/packets/preview",
                      {"payload": payload}).get("preview") or [])


def member_for_peer(peer: dict[str, Any]) -> str | None:
    """The org member behind a paired peer: the roster entry whose node
    reported one of the peer's URLs at heartbeat."""
    urls = {(peer.get(k) or "").strip().rstrip("/").lower()
            for k in ("base_url", "internal_url")} - {""}
    for team in membership().get("roster") or []:
        for mem in team.get("members") or []:
            u = (mem.get("peer_url") or "").strip().rstrip("/").lower()
            if u and u in urls:
                return mem.get("member_id")
    return None


def answerable(asker_member_id: str, question: str) -> list[dict[str, Any]]:
    return list(_call("POST", "/records/answerable",
                      {"asker_member_id": asker_member_id,
                       "question": question}).get("records") or [])


def ensure_project(name: str, parent_id: str) -> dict[str, Any]:
    return _call("POST", f"/orgs/{membership().get('org_id')}/projects",
                 {"name": name, "parent_id": parent_id})


def search_records(query: str, *, scope: str | None = None,
                   limit: int = 8) -> list[dict[str, Any]]:
    from urllib.parse import urlencode
    params = {"q": query, "limit": limit, **({"scope": scope} if scope else {})}
    return list(_call("GET", "/records/search?" + urlencode(params))
                .get("records") or [])


def scope_records(scope_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    from urllib.parse import urlencode
    q = urlencode({"scope": scope_id, "limit": limit})
    return list(_call("GET", f"/records?{q}").get("records") or [])


def record_versions(record_id: str) -> dict[str, Any]:
    from urllib.parse import quote
    return _call("GET", f"/records/{quote(record_id, safe='')}/versions")


def retract_record(record_id: str) -> dict[str, Any]:
    from urllib.parse import quote
    return _call("POST", f"/records/{quote(record_id, safe='')}/retract", {})


def submit_packet(body: dict[str, Any]) -> dict[str, Any]:
    return _call("POST", "/packets", body)


def forward_packet(body: dict[str, Any]) -> dict[str, Any]:
    return _call("POST", "/packets/forward", body)


def forwarded_packets() -> list[dict[str, Any]]:
    return list(_call("GET", "/packets/forwarded").get("packets") or [])


def current_record(scope_id: str, subject_ref: str,
                   predicate: str) -> dict[str, Any] | None:
    from urllib.parse import urlencode
    q = urlencode({"scope": scope_id, "subject": subject_ref,
                   "predicate": predicate})
    rows = _call("GET", f"/records?{q}").get("records") or []
    return rows[0] if rows else None


def report_expired(event_refs: list[int], expired_at: float) -> dict[str, Any]:
    return _call("POST", "/evidence/expired",
                 {"node_id": membership().get("node_id"),
                  "event_refs": [int(e) for e in event_refs],
                  "expired_at": float(expired_at)})


# --------------------------------------------- team search: pointer index --
def publish_pointers(pointers: list[dict[str, Any]]) -> dict[str, Any]:
    return _call("POST", "/pointers", {"pointers": pointers})


def withdraw_pointers(item_refs: list[str]) -> dict[str, Any]:
    return _call("POST", "/pointers/withdraw", {"item_refs": list(item_refs)})


def reconcile_pointers(item_refs: list[str]) -> dict[str, Any]:
    return _call("POST", "/pointers/reconcile", {"item_refs": list(item_refs)})


def unshare_pointers(scope_id: str) -> dict[str, Any]:
    return _call("POST", "/pointers/unshare", {"scope_id": scope_id})


def search_pointers(query: str, *, limit: int = 8,
                    ask_limit: int = 3) -> dict[str, Any]:
    from urllib.parse import urlencode
    # On the chat path: a slow org costs a few seconds, not ten.
    return _call("GET", "/pointers/search?" + urlencode(
        {"q": query, "limit": limit, "ask_limit": ask_limit}),
        timeout=_CHAT_TIMEOUT_S)


def common_scopes(member_id: str) -> dict[str, Any]:
    from urllib.parse import urlencode
    return _call("GET", "/scopes/common?" + urlencode({"member_id": member_id}))


def drain_outbox(store=None, *, now: float | None = None) -> dict[str, Any]:
    """Deliver queued packets and expiry notices. Packets past TTL fail."""
    from app.services.records import node_store, promotion
    if store is None:
        from app.storage import get_store
        store = get_store()
    now = float(now if now is not None else time.time())
    counts = {"delivered": 0, "refused": 0, "retry": 0}
    for row in node_store.due_outbox(store, now=now):
        delay = min(3600.0, 60.0 * (2 ** min(int(row["attempts"]), 6)))
        try:
            if row["kind"] == "packet_submit":
                promotion.deliver(store, row["ref"], now=now)
            elif row["kind"] == "evidence_expired":
                if not joined():
                    node_store.outbox_done(store, row["id"])
                    continue
                report_expired(row["body"]["event_refs"],
                               row["body"]["expired_at"])
            node_store.outbox_done(store, row["id"])
            counts["delivered"] += 1
        except OrgUnavailable as exc:
            node_store.outbox_retry(store, row["id"], str(exc), delay_s=delay)
            counts["retry"] += 1
        except OrgRefused as exc:
            node_store.outbox_done(store, row["id"])
            counts["refused"] += 1
            print(f"[records.org_client] outbox {row['kind']} refused ({exc}).")
    return counts
