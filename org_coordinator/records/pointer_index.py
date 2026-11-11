"""Team search, Connected rung: the central pointer index.

Each member's Sparrow publishes a pointer per item (owner, tags, people,
type, time, where it lives; app/services/records/pointers.py has the shape),
and a teammate's search reads them back through one rule:

  * Permissions are checked when a search runs, not only when content is
    fetched. Each candidate is put in its tier before anything is ranked:
    open (a shared project both reader and owner can read) or routed (at
    most "ask <owner>"). The CHECK on the table keeps titles and snippets
    off every pointer outside a shared project, whatever a node sends.
  * Pointers come out promptly: a node withdraws a deleted item, a member's
    pointers in a project go when they unshare it or lose read on it, all of
    theirs go when they leave, and every pointer in the org goes when the
    org leaves the Connected rung or turns publishing off.
  * The index is open only on Connected. Sealed and Perimeter orgs never
    receive pointers: their nodes answer teammates directly instead.

Withdrawals never depend on the index being open, so a node can always take
its pointers back.
"""
from __future__ import annotations

from typing import Any

from app.services.records import pointers as P
from org_coordinator.records import audit
from org_coordinator.records.service import (Principal, ServiceError, _now,
                                             effective_permissions,
                                             readable_scopes)


def index_state(org: dict[str, Any] | None) -> str:
    """'open', or 'closed:<why>'."""
    org = org or {}
    rung = org.get("rung") or "connected"
    if rung != "connected":
        return f"closed:rung_{rung}"
    ts = (org.get("policy_json") or {}).get("team_search") or {}
    if isinstance(ts, dict) and ts.get("publish") is False:
        return "closed:disabled"
    return "open"


def _active(repo, principal: Principal) -> dict:
    m = repo.get_member(principal.member_id)
    if m is None or m["status"] != "active":
        raise ServiceError(403, "member_inactive")
    return m


def _refs(values: Any, cap: int) -> list[str]:
    if not isinstance(values, list):
        raise ServiceError(400, "bad_item_refs")
    if len(values) > cap:
        raise ServiceError(413, "too_many_item_refs")
    return sorted({str(v)[:P.MAX_REF] for v in values if str(v or "").strip()})


def publish(db, principal: Principal, items: Any, *,
            now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    if not isinstance(items, list):
        raise ServiceError(400, "bad_pointers")
    if len(items) > P.MAX_BATCH:
        raise ServiceError(413, "batch_too_large")
    rows: dict[str, dict] = {}
    rejected = downgraded = 0
    with db.tenant(principal.org_id) as repo:
        member = _active(repo, principal)
        state = index_state(repo.get_org())
        if state != "open":
            raise ServiceError(409, "index_closed", state.split(":", 1)[1])
        readable: dict[str, bool] = {}
        for raw in items:
            try:
                p = P.clean(raw)
            except ValueError:
                rejected += 1
                continue
            sid = p["scope_id"]
            if sid:
                if sid not in readable:
                    sc = repo.get_scope(sid)
                    readable[sid] = bool(
                        sc and sc["kind"] == "project"
                        and "read" in effective_permissions(repo, member, sid))
                if not readable[sid]:
                    # Not a shared project this member is in: keep the
                    # routing pointer, drop what only project members see.
                    p = P.without_text(p)
                    downgraded += 1
            meta, text = P.keys_for(p)
            rows[p["item_ref"]] = {
                "owner_id": member["id"], "item_ref": p["item_ref"],
                "node_id": principal.node_id or "", "kind": p["kind"],
                "scope_id": p["scope_id"], "tags": p["tags"],
                "people": p["people"], "terms": meta, "text_terms": text,
                "item_at": p["at"], "source": p["source"],
                "title": p["title"], "snippet": p["snippet"],
                "refs": p["refs"], "published_at": now}
        n = repo.upsert_pointers(list(rows.values()))
        if n:
            audit.append(repo, member["id"], "pointers.publish", member["id"],
                         {"count": n, "downgraded": downgraded}, at=now)
    return {"ok": True, "published": n, "downgraded": downgraded,
            "rejected": rejected}


def withdraw(db, principal: Principal, item_refs: Any, *,
             now: float | None = None) -> dict[str, Any]:
    now = _now(now)
    refs = _refs(item_refs, 5000)
    with db.tenant(principal.org_id) as repo:
        member = _active(repo, principal)
        n = repo.delete_pointers(member["id"], refs)
        if n:
            audit.append(repo, member["id"], "pointers.withdraw", member["id"],
                         {"count": n}, at=now)
    return {"ok": True, "withdrawn": n}


def reconcile(db, principal: Principal, keep_refs: Any, *,
              now: float | None = None) -> dict[str, Any]:
    """The node's full list of live items. Anything else this node has in
    the index is withdrawn: a delete the node failed to report never
    outlives the next reconcile. An empty list withdraws everything."""
    now = _now(now)
    keep = set(_refs(keep_refs, 200000))
    with db.tenant(principal.org_id) as repo:
        member = _active(repo, principal)
        have = repo.pointer_refs(member["id"], node_id=principal.node_id or "")
        stale = sorted(set(have) - keep)
        n = 0
        for i in range(0, len(stale), 5000):
            n += repo.delete_pointers(member["id"], stale[i:i + 5000])
        if n:
            audit.append(repo, member["id"], "pointers.reconcile",
                         member["id"], {"withdrawn": n}, at=now)
    return {"ok": True, "withdrawn": n, "kept": len(have) - n}


def unshare(db, principal: Principal, scope_id: str, *,
            now: float | None = None) -> dict[str, Any]:
    """This member stops sharing their items in one project. Their pointers
    there come out now; the node republishes them later as plain routing
    pointers, with no title or snippet."""
    now = _now(now)
    sid = str(scope_id or "")[:P.MAX_REF]
    if not sid:
        raise ServiceError(400, "scope_required")
    with db.tenant(principal.org_id) as repo:
        member = _active(repo, principal)
        n = repo.delete_owner_pointers(member["id"], scope_id=sid)
        audit.append(repo, member["id"], "pointers.unshare",
                     f"{sid}:{member['id']}", {"withdrawn": n}, at=now)
    return {"ok": True, "withdrawn": n}


# ----------------------------------------------- called inside a tenant txn --
def purge_member(repo, member_id: str, *, why: str, at: float,
                 actor: str) -> int:
    n = repo.delete_owner_pointers(member_id)
    if n:
        audit.append(repo, actor, "pointers.purge", member_id,
                     {"withdrawn": n, "why": why}, at=at)
    return n


def drop_unreadable(repo, member_id: str, *, at: float, actor: str) -> int:
    """After a grant change: pointers a member keeps in a project they can no
    longer read come out (their node republishes them without the project)."""
    member = repo.get_member(member_id)
    n = 0
    for sid in repo.owner_pointer_scopes(member_id):
        if member is None or "read" not in effective_permissions(repo, member,
                                                                 sid):
            n += repo.delete_owner_pointers(member_id, scope_id=sid)
    if n:
        audit.append(repo, actor, "pointers.purge", member_id,
                     {"withdrawn": n, "why": "lost_read"}, at=at)
    return n


def purge_org(repo, *, why: str, at: float, actor: str) -> int:
    n = repo.delete_all_pointers()
    if n:
        audit.append(repo, actor, "pointers.purge", repo.org_id,
                     {"withdrawn": n, "why": why}, at=at)
    return n


# ---------------------------------------------------------------- search --
def search(db, principal: Principal, *, query: str, limit: int = 8,
           ask_limit: int = 3, now: float | None = None) -> dict[str, Any]:
    q = (query or "")[:500]
    with db.tenant(principal.org_id) as repo:
        reader = _active(repo, principal)
        state = index_state(repo.get_org())
        if state != "open":
            return {"index": state, "pointers": [], "ask": []}
        keys = P.query_keys(q)
        if not keys:
            return {"index": state, "pointers": [], "ask": []}
        projects = {s["id"]: s for s in repo.list_scopes()
                    if s["kind"] == "project"}
        open_scopes = [s for s in readable_scopes(repo, reader)
                       if s in projects]
        people = {m["id"]: m for m in repo.list_members()
                  if m["status"] == "active"}
        rows = [r for r in repo.pointer_candidates(
                    keys, open_scopes=open_scopes, exclude_owner=reader["id"])
                if r["owner_id"] in people]
        perms: dict[tuple[str, str], bool] = {}

        def owner_can_read(owner: str, sid: str) -> bool:
            if (owner, sid) not in perms:
                perms[(owner, sid)] = "read" in effective_permissions(
                    repo, people[owner], sid)
            return perms[(owner, sid)]

        out = P.search(
            [{**r, "owner": r["owner_id"], "at": r["item_at"]} for r in rows],
            q, reader=reader["id"], readable=set(open_scopes),
            owner_can_read=owner_can_read,
            owner_name=lambda m: (people[m].get("display_name")
                                  or people[m]["email"]),
            scope_name=lambda s: projects[s]["name"],
            limit=max(1, min(int(limit), 25)),
            ask_limit=max(0, min(int(ask_limit), 10)))
    return {"index": state, **out}


def common_scopes(db, principal: Principal, *, member_id: str) -> dict[str, Any]:
    """Shared projects both the caller and `member_id` can read: what a node
    answering that teammate's search directly (Sealed / Perimeter) may show
    in full. Everything else it may only route."""
    with db.tenant(principal.org_id) as repo:
        me = _active(repo, principal)
        other = repo.get_member(member_id)
        if other is None or other["status"] != "active":
            return {"member_active": False, "scopes": []}
        mine = set(readable_scopes(repo, me))
        theirs = set(readable_scopes(repo, other))
        scopes = [{"id": s["id"], "name": s["name"]} for s in repo.list_scopes()
                  if s["kind"] == "project" and s["id"] in mine & theirs]
    return {"member_active": True, "scopes": scopes}


def status(db, principal: Principal) -> dict[str, Any]:
    with db.tenant(principal.org_id) as repo:
        member = _active(repo, principal)
        return {"index": index_state(repo.get_org()),
                "published": repo.count_pointers(member["id"])}
