"""One inbox — every pending decision, grouped by what is asked.

Frontend spec (2026-11-06), screen "Inbox": one queue over the existing
offer, review, alias, assistant, consent, peer-ask and dispute queues. Each
kind maps to one existing backend verb; nothing new is decided here.

Rules this module keeps:
  * Reads are pure. `collect()` only reads; the housekeeping some list routes
    do on GET (expiring stale offers) stays with the worker's own job.
  * The server decides the chip. Every evidence box carries
    {kind, source, event_id}; the page never infers one.
  * Undo is offered only where a clean reverse exists, and for 10 seconds.
  * User vocabulary in every label: people, projects, facts, sources.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
from typing import Any, Callable

UNDO_TTL_S = 10.0
NEEDS_YOU = ("decide", "consent", "offer")
KIND_ORDER = {"decide": 0, "consent": 1, "offer": 2, "person": 3,
              "confirm": 4, "review": 5, "assistant": 6}
# Fact kinds in user vocabulary (never "claim" in a label — spec rule 4).
FACT_KIND_LABEL = {"claim": "fact", "task": "task", "commitment": "commitment",
                   "question": "question", "idea": "idea", "decision": "decision",
                   "preference": "preference"}
KIND_LABEL = {"decide": "Decide", "confirm": "Confirm", "consent": "Consent",
              "review": "Review", "offer": "Offer", "person": "Person",
              "assistant": "Assistant"}

_undo: dict[str, tuple[float, str, Callable[[], Any]]] = {}
_undo_lock = threading.Lock()
_count_cache: dict[str, Any] = {"at": 0.0, "key": None, "n": 0}


# --------------------------------------------------------------------- shapes

def _age_label(age_s: float | None) -> str:
    if age_s is None:
        return ""
    if age_s < 3600:
        m = max(1, int(age_s // 60))
        return f"{m} min"
    if age_s < 86400:
        return f"{int(age_s // 3600)} h"
    d = int(age_s // 86400)
    return "1 day" if d == 1 else f"{d} days"


def _chip(kind: str, source: str = "", event_id: int | str | None = None) -> dict:
    if kind not in ("observed", "extracted", "confirmed", "peer", "review"):
        kind = "extracted"
    return {"kind": kind, "source": source or "", "event_id": event_id}


def _evidence(value: str, chip: dict, *, quote: str | None = None,
              confirmed_by: str | None = None) -> dict:
    out = {"value": value or "", "chip": chip, "source": chip.get("source") or ""}
    if quote:
        out["quote"] = quote
    if confirmed_by:
        out["confirmed_by"] = confirmed_by
    return out


def _action(key: str, label: str, *, primary: bool = False,
            href: str | None = None, local: bool = False) -> dict:
    out = {"key": key, "label": label, "primary": bool(primary)}
    if href:
        out["href"] = href
    if local:
        out["local"] = True
    return out


def _item(item_id: str, kind: str, title: str, *, now: float,
          entity: dict | None = None, evidence: list | None = None,
          actions: list | None = None, created_at: float | None = None,
          meta: str = "") -> dict:
    age = None
    if created_at:
        try:
            age = max(0.0, now - float(created_at))
        except (TypeError, ValueError):
            age = None
    return {
        "id": item_id, "kind": kind, "kind_label": KIND_LABEL.get(kind, kind),
        "title": title, "entity": entity, "evidence": evidence or [],
        "actions": actions or [], "created_at": created_at,
        "age_s": age, "age_label": _age_label(age), "meta": meta,
    }


def fact_chip(row: dict) -> dict:
    """Server-side chip for a fact row: review stamp + source class."""
    mod = str(row.get("source_modality") or row.get("source") or "").lower()
    origin = str(row.get("origin") or "").lower()
    src = _fact_source_label(row)
    if mod.startswith("peer") or origin == "peer":
        return _chip("peer", src, row.get("source_event_id"))
    if row.get("review") in ("approved", "edited"):
        return _chip("confirmed", src, row.get("source_event_id"))
    if mod.startswith(("user", "note", "onboarding")) or origin == "user":
        return _chip("observed", src, row.get("source_event_id"))
    return _chip("extracted", src, row.get("source_event_id"))


def _fact_source_label(row: dict) -> str:
    mod = (str(row.get("source_modality") or "").split(":")[0]) or "memory"
    label = {"audio": "heard", "vision": "seen", "user": "you"}.get(mod, mod)
    st = row.get("source_time") or row.get("created_at")
    when = ""
    if st:
        try:
            when = time.strftime("%b %-d", time.localtime(float(st)))
        except (ValueError, TypeError, OverflowError):
            when = ""
    who = row.get("from_person") or row.get("owner") or ""
    bits = [label] + ([who] if who else []) + ([when] if when else [])
    return " · ".join(bits)


# ------------------------------------------------------------------- collect

def collect(store, *, worker=None, now: float | None = None) -> dict:
    """{needs_you, can_wait, handled_7d, errors}. Pure read."""
    now = float(now if now is not None else time.time())
    items: list[dict] = []
    errors: list[str] = []
    for name, fn in (
        ("decide", lambda: _approval_items(worker, now)),
        ("disputes", lambda: _conflict_items(store, now)),
        ("consent", lambda: _consent_items(now)),
        ("offers", lambda: _offer_items(now)),
        ("peer", lambda: _peer_ask_items(now)),
        ("records", lambda: _claim_items(store, now)),
        ("aliases", lambda: _alias_items(store, now)),
        ("review", lambda: _review_items(store, now)),
        ("assistant", lambda: _assistant_items(now)),
    ):
        try:
            items.extend(fn())
        except Exception as exc:  # one broken queue must not empty the inbox
            errors.append(f"{name}: {exc}")

    def _key(it: dict):
        return (KIND_ORDER.get(it["kind"], 9), -(it.get("age_s") or 0))

    needs = sorted([i for i in items if i["kind"] in NEEDS_YOU], key=_key)
    waits = sorted([i for i in items if i["kind"] not in NEEDS_YOU],
                   key=lambda i: -(i.get("age_s") or 0))
    out = {"needs_you": needs, "can_wait": waits,
           "handled_7d": handled(store, since_s=7 * 86400, now=now),
           "generated_at": now}
    if errors:
        out["errors"] = errors
    return out


def needs_you_count(store, *, worker=None, ttl_s: float = 3.0) -> int:
    """Cheap count for the rail pill, cached briefly per memory version."""
    try:
        key = store.memory_version()
    except Exception:
        key = None
    now = time.time()
    if (_count_cache["key"] == key and key is not None
            and now - _count_cache["at"] < ttl_s):
        return int(_count_cache["n"])
    try:
        n = len(collect(store, worker=worker, now=now)["needs_you"])
    except Exception:
        n = 0
    _count_cache.update(at=now, key=key, n=n)
    return n


# ----------------------------------------------------------------- the queues

def _approval_items(worker, now: float) -> list[dict]:
    if worker is None:
        return []
    peek = getattr(worker, "pending_approval_packet", None)
    pkt = peek() if callable(peek) else None
    if not pkt or pkt.get("packet_id") is None:
        return []
    fields = pkt.get("fields") if isinstance(pkt.get("fields"), dict) else {}
    value = fields.get("action") or pkt.get("summary") or "Sparrow wants to act"
    quote = " · ".join(str(fields[k]) for k in ("to", "subject") if fields.get(k))
    body = fields.get("body")
    ev = _evidence(str(value), _chip("observed", fields.get("source") or "agent"),
                   quote=(body or quote or None))
    return [_item(
        f"approval:{pkt['packet_id']}", "decide",
        pkt.get("summary") or "Approve what Sparrow is about to do",
        now=now, evidence=[ev], created_at=pkt.get("created_at"),
        meta=fields.get("why") or "",
        actions=[_action("approve", "Approve", primary=True),
                 _action("cancel", "Cancel")],
    )]


def _conflict_items(store, now: float) -> list[dict]:
    rows = store.list_adjudications(kind="conflict_flag", limit=100)
    seen: set[tuple[int, int]] = set()
    out: list[dict] = []
    for r in rows:
        try:
            feats = json.loads(r.get("features_json") or "{}")
        except (TypeError, ValueError):
            continue
        a, b = feats.get("old_predicate_id"), feats.get("new_predicate_id")
        if a is None or b is None:
            continue
        pair = (int(min(a, b)), int(max(a, b)))
        if pair in seen:
            continue
        seen.add(pair)
        pa, pb = store.get_kg_predicate(pair[0]), store.get_kg_predicate(pair[1])
        if not pa or not pb or not (pa.get("conflict") and pb.get("conflict")):
            continue
        from app.services import kg_beliefs
        ea = kg_beliefs.explain_predicate(store, pair[0])
        eb = kg_beliefs.explain_predicate(store, pair[1])
        if not (ea.get("ok") and eb.get("ok")):
            continue
        subject = ea.get("subject") or ""
        field = str(pa.get("predicate") or "").replace("_", " ")
        ev = []
        for p, e in ((pa, ea), (pb, eb)):
            layer = str(p.get("layer") or "asserted")
            kind = "confirmed" if layer == "user_verified" else "extracted"
            ev.append(_evidence(str(e.get("object") or ""),
                                _chip(kind, f"{layer.replace('_', ' ')} · "
                                      + _when(p.get("last_seen")))))
        out.append(_item(
            f"conflict:{pair[0]}-{pair[1]}", "decide",
            f"Two sources disagree on {subject}'s {field}", now=now,
            entity=_entity_ref(store, pa.get("subj_type"), pa.get("subj_id"), subject),
            evidence=ev, created_at=r.get("created_at"),
            actions=[_action("both_true", "Both are true", primary=True),
                     _action("inspect", "Look closer",
                             href=f"/kg/predicates/{pair[0]}/explain")],
        ))
    return out


def _consent_items(now: float) -> list[dict]:
    try:
        from app.services.connectors import list_status, get_connector
        from app.services import capture_consent
    except Exception:
        return []
    out: list[dict] = []
    for row in list_status() or []:
        cid = row.get("id")
        if not cid:
            continue
        c = get_connector(cid)
        sentence = getattr(c, "bodies_consent_sentence", None) if c else None
        if not sentence:
            continue
        try:
            wanted = capture_consent.allows_connector_bodies(cid)
            granted = bool(c.body_scope_granted())
        except Exception:
            continue
        if not wanted or granted:
            continue
        label = row.get("label") or row.get("name") or cid
        out.append(_item(
            f"consent:{cid}", "consent",
            f"{label} needs a wider sign-in to read message bodies", now=now,
            evidence=[_evidence(sentence, _chip("observed", f"{label} · settings"))],
            actions=[_action("allow", "Allow and re-sign in", primary=True,
                             href="/onboarding"),
                     _action("keep", "Keep as is")],
        ))
    return out


def _offer_items(now: float) -> list[dict]:
    try:
        from app.services.chat_log import get_log
        offers = get_log().open_offers(now=now)
    except Exception:
        return []
    out: list[dict] = []
    for o in offers:
        oid = o.get("offer_id")
        if oid is None:
            continue
        title = o.get("title") or o.get("message") or o.get("kind") or "Offer"
        choices = [c for c in (o.get("choices") or []) if isinstance(c, str)]
        if choices:
            actions = [_action(f"choice:{c}", c.capitalize(), primary=(i == 0))
                       for i, c in enumerate(choices)]
        else:
            actions = [_action("accept", "Yes", primary=True), _action("decline", "No")]
        person = o.get("person") or o.get("peer_name") or ""
        ev = _evidence(str(o.get("message") or title),
                       _chip("observed", f"Sparrow · {_when(o.get('created_at'))}"),
                       quote=(o.get("why") if isinstance(o.get("why"), str) else None))
        out.append(_item(
            f"offer:{oid}", "offer", str(title), now=now, evidence=[ev],
            created_at=o.get("created_at"), actions=actions,
            meta=(f"for {person}" if person else str(o.get("kind") or "")),
        ))
    return out


def _peer_ask_items(now: float) -> list[dict]:
    try:
        from app.services import peer_channel
        asks = peer_channel.pending_asks()
    except Exception:
        return []
    out: list[dict] = []
    for a in asks or []:
        if not a.get("id"):
            continue
        who = a.get("peer_name") or "A teammate"
        q = a.get("question") or a.get("topic") or ""
        out.append(_item(
            f"peer:{a['id']}", "offer", f"{who} asks: {q}".strip(), now=now,
            evidence=[_evidence(q, _chip("peer", f"{who} · {_when(a.get('created_at'))}"))],
            created_at=a.get("created_at"), meta=str(a.get("kind") or ""),
            actions=[_action("answer", "Answer from memory", primary=True),
                     _action("decline", "Decline")],
        ))
    return out


def _claim_items(store, now: float) -> list[dict]:
    try:
        from app.services.records import node_store
        rows = node_store.list_claims(store, status="proposed,conflicting", limit=100)
    except Exception:
        return []
    out: list[dict] = []
    for r in rows:
        try:
            pkt = node_store.open_packet_for_claim(store, r["id"])
        except Exception:
            pkt = None
        if not pkt:
            continue
        scope = r.get("proposed_scope") or "the team"
        out.append(_item(
            f"claim:{pkt['id']}", "offer",
            f"Share “{(r.get('value_text') or r.get('text') or r.get('kind') or 'a fact')}” with {scope}",
            now=now, created_at=r.get("created_at"),
            evidence=[_evidence(str(r.get("value_text") or r.get("text") or ""),
                                _chip("extracted", f"records · {scope}"))],
            actions=[_action("review", "Review in Records", primary=True,
                             href="/records")],
        ))
    return out


def _alias_items(store, now: float) -> list[dict]:
    from app.services import entity_alias
    out: list[dict] = []
    for r in entity_alias.proposals(store, limit=200) or []:
        name = r.get("entity_name") or f"entity {r.get('entity_id')}"
        days = len(r.get("seen_days") or [])
        src = r.get("source") or "seen"
        out.append(_item(
            f"alias:{r['id']}", "confirm",
            f"Is “{r.get('alias')}” the same as {name}?", now=now,
            entity=_entity_ref(store, r.get("entity_kind"), r.get("entity_id"), name),
            evidence=[_evidence(str(r.get("alias") or ""),
                                _chip("extracted", f"{src} · {days} day{'s' if days != 1 else ''}"))],
            created_at=r.get("created_at"),
            actions=[_action("yes", "Yes, same", primary=True),
                     _action("no", "No, different"),
                     _action("later", "Not sure", local=True)],
        ))
    return out


def _review_items(store, now: float) -> list[dict]:
    rows = store.list_facts(review="none", limit=300)
    out: list[dict] = []
    for r in rows:
        if r.get("state") in ("superseded", "archived"):
            continue
        fid = r.get("fact_id") or r.get("id")
        if fid is None:
            continue
        text = r.get("text") or ""
        out.append(_item(
            f"review:{fid}", "review", text[:140] or f"{r.get('kind') or 'fact'}",
            now=now, evidence=[_evidence(text, fact_chip(r),
                                         quote=(r.get("source_span") or None))],
            created_at=r.get("created_at") or r.get("source_time"),
            meta=FACT_KIND_LABEL.get(str(r.get("kind") or ""), "fact"),
            actions=[_action("keep", "Keep", primary=True),
                     _action("dismiss", "Dismiss")],
        ))
    return out


def _assistant_items(now: float) -> list[dict]:
    try:
        from app.services import assistant_inbox
        rows = assistant_inbox.items("pending", limit=200)
    except Exception:
        return []
    out: list[dict] = []
    for r in rows or []:
        label = r.get("assistant_label") or r.get("assistant") or "An assistant"
        title = r.get("title") or (r.get("text") or "")[:100]
        out.append(_item(
            f"assistant:{r['id']}", "assistant",
            f"{label} proposed a {r.get('kind') or 'note'}: {title}", now=now,
            evidence=[_evidence(r.get("text") or "",
                                _chip("extracted", f"{label} · {_when(r.get('created_at'))}"))],
            created_at=r.get("created_at"),
            meta=(f"project {r['project']}" if r.get("project") else ""),
            actions=[_action("accept", "Accept", primary=True),
                     _action("edit", "Edit", href="/assistants"),
                     _action("discard", "Discard")],
        ))
    return out


def _entity_ref(store, kind, entity_id, name) -> dict | None:
    if entity_id is None:
        return None
    try:
        eid = int(entity_id)
    except (TypeError, ValueError):
        return None
    k = str(kind or "")
    href = f"/profile?tab=people" if k == "person" else f"/org/{eid}"
    return {"id": eid, "name": name or "", "kind": k or "entity", "href": href}


def _when(ts) -> str:
    try:
        return time.strftime("%b %-d", time.localtime(float(ts)))
    except (TypeError, ValueError, OverflowError):
        return ""


# ------------------------------------------------------------------- handled

def handled(store, *, since_s: float = 7 * 86400, now: float | None = None) -> dict:
    """Things settled without asking: {count, breakdown:[{kind, count}]}."""
    now = float(now if now is not None else time.time())
    since = now - since_s
    breakdown: list[dict] = []

    def add(kind: str, n: int) -> None:
        if n > 0:
            breakdown.append({"kind": kind, "count": int(n)})

    try:
        sup = store.recent_supersessions(200)
        add("Facts updated on their own",
            sum(1 for s in sup if float(s.get("when") or 0) >= since))
    except Exception:
        pass
    try:
        adj = store.list_adjudications(limit=500)
        add("Beliefs settled on their own",
            sum(1 for a in adj if a.get("decided_by") == "auto"
                and a.get("decision") != "defer"
                and float(a.get("created_at") or 0) >= since))
    except Exception:
        pass
    try:
        from app.services.model_log import model_log
        inv = model_log.egress_inventory(recent=200)
        add("Shared by a standing rule",
            sum(1 for r in inv.get("recent") or []
                if r.get("approving_action") == "policy"
                and float(r.get("time") or 0) >= since))
    except Exception:
        pass
    return {"count": sum(b["count"] for b in breakdown), "breakdown": breakdown,
            "since_s": since_s}


def rule_in_force() -> dict:
    """The automation rule the Inbox copy quotes, read from settings so the
    copy never drifts from the behaviour."""
    try:
        from app.config import settings
        n = int(getattr(getattr(settings, "context_anchor", None),
                        "alias_autoconfirm_n", 3))
    except Exception:
        n = 3
    return {"alias_days": n,
            "text": (f"After a name is seen on {n} different days it is "
                     "confirmed without asking.")}


# ----------------------------------------------------------------------- act

def act(store, item_id: str, action: str, *, worker=None) -> dict:
    """Perform one inbox action through its existing verb.
    Returns {ok, result, undo_token?} or {ok: False, error}."""
    queue, _, native = (item_id or "").partition(":")
    action = (action or "").strip()
    if not queue or not native or not action:
        return {"ok": False, "error": "bad item or action"}
    handler = _ACT.get(queue)
    if handler is None:
        return {"ok": False, "error": f"unknown queue {queue!r}"}
    out = handler(store, native, action, worker)
    if out.get("ok") and callable(out.get("_undo")):
        tok = secrets.token_urlsafe(12)
        with _undo_lock:
            _undo[tok] = (time.time() + UNDO_TTL_S, item_id, out.pop("_undo"))
        out["undo_token"] = tok
        out["undo_s"] = UNDO_TTL_S
    out.pop("_undo", None)
    _count_cache["at"] = 0.0
    return out


def undo(token: str) -> dict:
    with _undo_lock:
        _sweep_unlocked()
        ent = _undo.pop(token or "", None)
    if ent is None:
        return {"ok": False, "error": "nothing to undo (window passed)"}
    _, item_id, fn = ent
    try:
        res = fn()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "item": item_id}
    _count_cache["at"] = 0.0
    return {"ok": True, "item": item_id, "result": res}


def _sweep_unlocked() -> None:
    now = time.time()
    for k in [k for k, (exp, _, _) in _undo.items() if exp < now]:
        _undo.pop(k, None)


def _fact_verdict_side_effects(fact: dict, outcome: str, verdict: str) -> None:
    """The same trail the /facts/{id}/approve|dismiss routes leave: usage
    counter, distill label, learning pair. Best effort, never raises."""
    try:
        from app.api import routes
        routes._usage().bump("facts_reviewed")
        routes._label_distill_outcome(fact, outcome)
        routes._harvest_fact_verdict(fact, verdict)
    except Exception as exc:
        print(f"[inbox] fact verdict side effects skipped ({exc}).")


def _act_review(store, native, action, worker) -> dict:
    fid = int(native)
    fact = store.get_fact(fid)
    if not fact:
        return {"ok": False, "error": "fact not found"}
    if action == "keep":
        store.review_fact(fid, "approved")
        _fact_verdict_side_effects(fact, "accepted", "accepted")

        def _back():
            store.unreview_fact(fid)
            try:
                from app.services import decisions
                decisions.undo(surface="facts.review", subject=str(fid),
                               via="inbox", store=store)
            except Exception:
                pass
            return {"review": None}
        return {"ok": True, "result": {"fact_id": fid, "review": "approved"},
                "label": "Kept", "_undo": _back}
    if action == "dismiss":
        store.review_fact(fid, "dismissed")
        _fact_verdict_side_effects(fact, "rejected", "dismissed")
        return {"ok": True, "result": {"fact_id": fid, "review": "dismissed"},
                "label": "Dismissed"}
    return {"ok": False, "error": f"unknown action {action!r}"}


def _act_alias(store, native, action, worker) -> dict:
    aid = int(native)
    if action == "yes":
        if not store.confirm_entity_alias(aid, True):
            return {"ok": False, "error": "alias not found"}
        return {"ok": True, "result": {"confirmed": True}, "label": "Same",
                "_undo": lambda: store.confirm_entity_alias(aid, False)}
    if action == "no":
        row = store.get_entity_alias(aid) if hasattr(store, "get_entity_alias") else None
        if row is None:
            row = next((r for r in store.list_entity_aliases(confirmed=False)
                        if int(r.get("id", -1)) == aid), None)
        if row is None or not store.delete_entity_alias(aid):
            return {"ok": False, "error": "alias not found"}

        def _back():
            return store.upsert_entity_alias(
                int(row["entity_id"]), row["alias"], row["alias_norm"],
                source=row.get("source") or "", confirmed=False)
        return {"ok": True, "result": {"deleted": True}, "label": "Different",
                "_undo": _back}
    return {"ok": False, "error": f"unknown action {action!r}"}


def _act_offer(store, native, action, worker) -> dict:
    if worker is None:
        return {"ok": False, "error": "agent disabled"}
    from app.services.chat_log import get_log
    offer = next((o for o in get_log().open_offers()
                  if str(o.get("offer_id")) == str(native)), None)
    if offer is None:
        return {"ok": False, "error": "offer expired or already resolved"}
    choice = None
    if action.startswith("choice:"):
        choice, accept = action.split(":", 1)[1], True
    elif action == "accept":
        accept = True
    elif action == "decline":
        accept = False
    else:
        return {"ok": False, "error": f"unknown action {action!r}"}
    res = worker.resolve_offer(str(offer["offer_id"]), str(offer["payload_hash"]),
                               accept, choice=choice)
    ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
    return {"ok": ok, "result": res,
            "label": (choice.capitalize() if choice else ("Yes" if accept else "No")),
            **({} if ok else {"error": (res or {}).get("error") or "refused"})}


def _act_peer(store, native, action, worker) -> dict:
    from app.services import peer_channel
    if action not in ("answer", "decline"):
        return {"ok": False, "error": f"unknown action {action!r}"}
    res = peer_channel.decide_ask(str(native), action == "answer")
    ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
    return {"ok": ok, "result": res,
            "label": "Answered" if action == "answer" else "Declined",
            **({} if ok else {"error": (res or {}).get("error") or "refused"})}


def _act_assistant(store, native, action, worker) -> dict:
    from app.services import assistant_inbox
    iid = int(native)
    if action == "accept":
        res = assistant_inbox.accept(iid, decided_by="inbox")
        return {"ok": True, "result": res, "label": "Accepted",
                "_undo": lambda: assistant_inbox.remove(iid)}
    if action == "discard":
        return {"ok": True, "result": assistant_inbox.reject(iid), "label": "Discarded"}
    return {"ok": False, "error": f"unknown action {action!r}"}


def _act_conflict(store, native, action, worker) -> dict:
    if action != "both_true":
        return {"ok": False, "error": f"unknown action {action!r}"}
    a, _, b = native.partition("-")
    pa, pb = int(a), int(b)
    from app.services import kg_beliefs
    res = kg_beliefs.resolve_conflict_both_true(store, pa, pb)

    def _back():
        now = time.time()
        for pid in (pa, pb):
            store.set_kg_predicate_conflict(pid, True, ts=now)
        return {"reflagged": [pa, pb]}
    return {"ok": True, "result": res, "label": "Both true", "_undo": _back}


def _act_approval(store, native, action, worker) -> dict:
    if worker is None:
        return {"ok": False, "error": "agent disabled"}
    if action not in ("approve", "cancel"):
        return {"ok": False, "error": f"unknown action {action!r}"}
    pkt = worker.pending_approval_packet() or {}
    if str(pkt.get("packet_id")) != str(native):
        return {"ok": False, "error": "that approval is no longer pending"}
    res = worker.decide_approval(int(native), str(pkt.get("payload_hash") or ""),
                                 action, approved_via="inbox")
    ok = bool(res.get("ok", True)) if isinstance(res, dict) else True
    return {"ok": ok, "result": res,
            "label": "Approved" if action == "approve" else "Cancelled",
            **({} if ok else {"error": (res or {}).get("error") or "refused"})}


def _act_consent(store, native, action, worker) -> dict:
    if action != "keep":
        return {"ok": False, "error": f"unknown action {action!r}"}
    from app.services import capture_consent
    res = capture_consent.record_connector_bodies(str(native), enabled=False)
    return {"ok": True, "result": res, "label": "Kept as is",
            "_undo": lambda: capture_consent.record_connector_bodies(
                str(native), enabled=True)}


_ACT = {
    "review": _act_review, "alias": _act_alias, "offer": _act_offer,
    "peer": _act_peer, "assistant": _act_assistant, "conflict": _act_conflict,
    "approval": _act_approval, "consent": _act_consent,
}
