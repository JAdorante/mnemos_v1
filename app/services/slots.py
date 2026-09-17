"""Open slots — tasks that wait for data (connector capture & task
fulfillment spec, Feature 3).

When someone asks for something Sparrow does not have, Sparrow does not just
say "no data". It offers to create a *slot*: a commitment row in state
`awaiting_data` whose `slot_json` names what is needed (`need`), who asked
(`requester`), and how sure a match has to be (`match_threshold`). Every
persisted event is then scored against the open slots (task_completion hooks
Store.insert); a score above threshold is a *candidate fill*, which produces
an OFFER — Deliver / Not it — never a silent completion.

Delivery to the user is the excerpt plus a provenance link. Delivery to a
peer goes through peer_channel.deliver_fill, redacted under that peer's
disclosure policy, and the slot moves to `done` with evidence_id = the fill
event. "Not it" lowers that event's score and keeps the slot open; the pair
is remembered in slot_candidates so the same event is never offered twice.

Trust: an observed-tier fill can propose delivery; it cannot authorize an
agent step. The only auto path is deliver_on_fill, opted into per slot, and
even that shows a 10-second undo. Off switch: QUILL_SLOTS=0.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any

from app.events import Event, Modality

DEFAULT_THRESHOLD = float(os.environ.get("QUILL_SLOT_MATCH_THRESHOLD", "0.78"))
REVIEW_AFTER_S = float(os.environ.get("QUILL_SLOT_REVIEW_AFTER_S",
                                      str(14 * 86400)))
UNDO_S = float(os.environ.get("QUILL_SLOT_UNDO_S", "10"))
AUDIO_MIN_CONF = float(os.environ.get("QUILL_SLOT_AUDIO_MIN_CONF", "0.6"))
SEARCH_ASSIST_S = float(os.environ.get("QUILL_SLOT_SEARCH_S", "10"))
# Sources that can never fill a slot: the ask itself, our own notices, and
# the slot's own creation statement.
_NEVER_FILL_PREFIXES = ("peer.ask", "peer.notify", "slot.", "peer.slot")

_lock = threading.RLock()
_pending_auto: dict[int, threading.Timer] = {}
_need_vec_cache: dict[str, Any] = {}

_STOP = frozenset({
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "my", "me",
    "i", "you", "we", "is", "are", "be", "with", "at", "this", "that", "it",
    "as", "from", "about", "what", "whats", "status", "get", "find", "please",
    "can", "could", "would", "latest", "current", "any", "there", "did",
    "have", "has", "was", "were", "do", "does", "us", "our", "your", "their",
    "know", "keep", "eye", "out", "look", "send", "sent", "yet", "update",
})

_NEED_PREFIX = re.compile(
    r"^\s*(?:(?:hey|hi|please|can you|could you|would you|i need|i want|"
    r"get me|find me|find|get|fetch|pull|look for|look up|track down|"
    r"keep an eye out for|keep an eye on|watch for|watch out for|"
    r"let me know (?:when|if) (?:you )?(?:see|get|find)|"
    r"what(?:'s| is| was) the (?:status|state|latest) (?:of|on)|"
    r"what(?:'s| is| was)|where(?:'s| is)|do (?:you|we) have|"
    r"is there|any (?:news|word|update) on|status (?:of|on))\s+)+",
    re.I)
_NEED_SUFFIX = re.compile(
    r"\s*(?:\?|\.|!|,)*\s*(?:please|thanks|thank you|when it (?:arrives|"
    r"lands|comes in)|if it (?:arrives|lands|comes in))?\s*[?.!]*\s*$", re.I)
_ARTICLE = re.compile(r"^(?:the|a|an|our|my|that|this)\s+", re.I)


def enabled() -> bool:
    return os.environ.get("QUILL_SLOTS", "1") not in ("0", "false", "False")


def _now(now: float | None) -> float:
    return float(now if now is not None else time.time())


# --- need normalisation ------------------------------------------------------
def normalize_need(text: str) -> str:
    """"What's the status of the Boston quote? Ask User 2." →
    "Boston quote". Strips the asking phrases, an addressee clause, and the
    article; keeps the user's casing so entities still read as names."""
    t = (text or "").strip()
    t = re.sub(r"\s*[—-]\s*ask\s+.+$", "", t, flags=re.I)
    t = re.sub(r"[.?!;]\s*ask\s+.+$", "", t, flags=re.I)
    t = re.sub(r"^\s*ask\s+#?[\w -]{1,40}?\s*[:,]\s*", "", t, flags=re.I)
    t = _NEED_PREFIX.sub("", t)
    t = _NEED_SUFFIX.sub("", t)
    t = _ARTICLE.sub("", t.strip())
    t = re.sub(r"\s+", " ", t).strip(" \"'“”")
    return t or (text or "").strip()


def need_tokens(need: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9'-]{1,}", (need or "").lower()):
        w = w.strip("'-")
        if len(w) < 2 or w in _STOP:
            continue
        out.add(_stem(w))
    return out


def _stem(w: str) -> str:
    """Plural folding only: quotes→quote, invoices→invoice, copies→copy.
    Deliberately no -es/-ed/-ing stripping — "quotes" and "quote" must fold
    to the same token, and a heavier stemmer split them."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def _text_tokens(text: str) -> set[str]:
    return {_stem(w.strip("'-")) for w in
            re.findall(r"[a-z0-9][a-z0-9'-]{1,}", (text or "").lower())
            if len(w.strip("'-")) >= 2}


def resolve_entities(need: str, *, store=None) -> list[str]:
    """Entity names the need refers to: exact binding against the graph's
    canonical names (bind only — a need never mints, and no embedding is
    loaded for it), falling back to the capitalised runs so a slot for
    "Boston deal quote" still knows Boston is the anchor."""
    out: list[str] = []
    caps = re.findall(r"\b(?:[A-Z][\w&'-]+(?:\s+[A-Z][\w&'-]+)*)", need or "")
    seen: set[str] = set()
    for cand in caps:
        key = cand.strip()
        if not key or key.lower() in seen or key.lower() in _STOP:
            continue
        seen.add(key.lower())
        name = key
        try:
            if store is not None:
                eid = store.find_entity_exact(key)
                if eid:
                    ent = store.get_entity(int(eid)) or {}
                    name = ent.get("canonical_name") or ent.get("name") or key
        except Exception:
            name = key
        if name not in out:
            out.append(name)
    return out


def index_if_bound(store, event_id: int, event: Event) -> None:
    """Semantic-index a freshly inserted event ONLY when the memory engine
    is bound to this very store. A temp store (tests, replay, a read-only
    pilot mount) must never write into the process-default LanceDB."""
    try:
        from app.services.memory import memory
        if memory._store is not store:
            return
        from app.services.attachments import _index_event
        _index_event(event_id, event)
    except Exception:
        pass


# --- similarity --------------------------------------------------------------
def _sim_mode() -> str:
    return (os.environ.get("QUILL_SLOT_SIM") or "auto").strip().lower()


def _cosine(need: str, text: str) -> float | None:
    if _sim_mode() == "overlap":
        return None
    try:
        import numpy as np
        from app.services.embeddings import embedder
        key = need.strip().lower()
        vec = _need_vec_cache.get(key)
        if vec is None:
            vec = embedder.encode(need)
            _need_vec_cache[key] = vec
        other = embedder.encode(text[:2000])
        denom = float(np.linalg.norm(vec) * np.linalg.norm(other)) or 1.0
        return float(np.dot(vec, other) / denom)
    except Exception:
        return None


def coverage(need: str, text: str) -> float:
    """Fraction of the need's content tokens present in the text."""
    nt = need_tokens(need)
    if not nt:
        return 0.0
    tt = _text_tokens(text)
    return len(nt & tt) / len(nt)


def similarity(need: str, text: str) -> float:
    """max(cosine, token coverage): an exact identifier in the text is as
    strong as a semantic neighbour, and a short need against a long document
    rarely clears 0.78 on cosine alone."""
    cov = coverage(need, text)
    cos = _cosine(need, text)
    if cos is None:
        return cov
    return max(cov, cos)


def event_text(event: Event | dict) -> str:
    if isinstance(event, dict):
        meta = event.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        parts = [str(meta.get("title") or ""), str(event.get("summary") or ""),
                 str(event.get("raw") or "")[:4000]]
    else:
        meta = event.meta or {}
        parts = [str(meta.get("title") or ""), event.summary or "",
                 (event.raw or "")[:4000]]
    return "\n".join(p for p in parts if p)


def entity_overlap(entities: list[str], event: Event | dict, text: str) -> float | None:
    ents = [e for e in (entities or []) if e]
    if not ents:
        return None
    low = text.lower()
    ev_ents = []
    if isinstance(event, dict):
        raw = event.get("entities")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = []
        ev_ents = [str(x).lower() for x in (raw or [])]
    else:
        ev_ents = [str(x).lower() for x in (event.entities or [])]
    hit = 0
    for e in ents:
        el = e.lower()
        if el in low or el in ev_ents or any(el in x for x in ev_ents):
            hit += 1
    return hit / len(ents)


def score_event(slot: dict, event: Event | dict) -> tuple[float, dict]:
    need = slot.get("need") or ""
    text = event_text(event)
    if not need or not text.strip():
        return 0.0, {"sim": 0.0, "ent": None}
    sim = similarity(need, text)
    ent = entity_overlap(slot.get("entities") or [], event, text)
    score = sim if ent is None else max(sim, 0.6 * sim + 0.4 * ent)
    return round(min(1.0, score), 4), {"sim": round(sim, 4), "ent": ent}


def eligible(event: Event | dict, *, slot: dict | None = None,
             event_id: int | None = None) -> bool:
    if isinstance(event, dict):
        source = str(event.get("source") or "")
        modality = str(event.get("modality") or "")
        conf = event.get("confidence")
        meta = event.get("meta") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
    else:
        source = event.source or ""
        modality = event.modality.value
        conf = event.confidence
        meta = event.meta or {}
    if any(source.startswith(p) for p in _NEVER_FILL_PREFIXES):
        return False
    if meta.get("origin") == "slot" or meta.get("slot_notice"):
        return False
    if modality == Modality.AUDIO.value:
        try:
            if conf is None or float(conf) < AUDIO_MIN_CONF:
                return False
        except (TypeError, ValueError):
            return False
    if slot is not None and event_id is not None and \
            slot.get("created_from") not in (None, "") and \
            int(slot.get("created_from")) == int(event_id):
        return False
    return True


# --- CRUD --------------------------------------------------------------------
def _store(store):
    if store is not None:
        return store
    from app.storage import get_store
    return get_store()


def create(store, need: str, *, requester: dict | None = None,
           created_from: int | None = None, entities: list[str] | None = None,
           counterparty: str | None = None, deliver_on_fill: bool = False,
           match_threshold: float | None = None, origin_id: str | None = None,
           team_slug: str | None = None, review_after: float | None = None,
           actor: str | None = None, now: float | None = None) -> int:
    """Persist an open slot. Returns the commitment fact id (0 when the
    source thread already produced a declined task)."""
    store = _store(store)
    now = _now(now)
    need_text = normalize_need(need)
    requester = dict(requester or {"kind": "user", "id": None})
    requester.setdefault("kind", "user")
    ents = list(entities) if entities is not None else resolve_entities(
        need_text, store=store)
    slot = {
        "need": need_text,
        "need_raw": (need or "").strip(),
        "need_embedding_id": None,
        "entities": ents,
        "counterparty": counterparty,
        "requester": requester,
        "created_from": created_from,
        "deliver_on_fill": bool(deliver_on_fill),
        "match_threshold": float(match_threshold if match_threshold is not None
                                 else DEFAULT_THRESHOLD),
        "origin_id": origin_id,
        "team_slug": team_slug,
        "created_at": now,
    }
    fid = store.add_commitment(
        f"Find: {need_text}", source_event_id=created_from,
        extracted_at=now, state="detected", task_kind="slot",
        counterparty_name=counterparty,
        requester_kind=requester.get("kind"),
        requester_id=(str(requester.get("id")) if requester.get("id")
                      not in (None, "") else None),
        slot=slot,
        review_after=(review_after if review_after is not None
                      else now + REVIEW_AFTER_S))
    if not fid:
        return 0
    store.transition_commitment(
        fid, "awaiting_data", actor=actor or requester.get("kind") or "user",
        reason="slot_created", ts=now,
        evidence={"source": "slot", "requester": requester,
                  **({"evidence_event_id": int(created_from)}
                     if created_from else {})})
    # Best-effort semantic index of the need so memory search can find the
    # slot itself later ("what am I waiting on?") — only into the index that
    # belongs to THIS store.
    try:
        from app.services.memory import memory
        if memory._store is store:
            memory.index_fact(fid, "slot", need_text, now)
            slot["need_embedding_id"] = fid
            store.set_slot(fid, slot)
    except Exception:
        pass
    return fid


def get(store, fact_id: int) -> dict | None:
    row = _store(store).get_task(int(fact_id))
    if not row or not row.get("slot"):
        return None
    return row


def open_slots(store) -> list[dict]:
    if not enabled():
        return []
    return _store(store).list_open_slots()


def update(store, fact_id: int, **changes) -> dict | None:
    store = _store(store)
    row = get(store, fact_id)
    if not row:
        return None
    slot = dict(row["slot"])
    for k, v in changes.items():
        if k == "need" and v:
            slot["need"] = normalize_need(str(v))
            slot["need_raw"] = str(v)
        elif k in ("entities", "counterparty", "deliver_on_fill",
                   "match_threshold", "requester", "origin_id", "team_slug"):
            slot[k] = v
    store.set_slot(fact_id, slot, review_after=row.get("review_after"))
    return get(store, fact_id)


def drop(store, fact_id: int, *, actor: str = "user",
         reason: str = "dropped", now: float | None = None) -> dict:
    store = _store(store)
    out = store.transition_commitment(
        int(fact_id), "declined", actor=actor, reason=reason, ts=_now(now))
    _cancel_auto(int(fact_id))
    return out


def keep(store, fact_id: int, *, now: float | None = None) -> dict | None:
    store = _store(store)
    row = get(store, fact_id)
    if not row:
        return None
    store.set_slot(fact_id, row["slot"], review_after=_now(now) + REVIEW_AFTER_S)
    return get(store, fact_id)


def cancel(store, fact_id: int, *, actor: str = "peer", reason: str,
           now: float | None = None) -> dict:
    store = _store(store)
    _cancel_auto(int(fact_id))
    return store.transition_commitment(
        int(fact_id), "cancelled", actor=actor, reason=reason, ts=_now(now))


def stale(store, *, now: float | None = None) -> list[dict]:
    """Open slots past review_after — Keep / Drop on the Horizon strip."""
    now = _now(now)
    out = []
    for row in open_slots(store):
        ra = row.get("review_after")
        if ra is not None and float(ra) <= now:
            out.append(row)
    return out


def resolve_siblings(store, origin_id: str | None, *,
                     except_fact_id: int | None = None,
                     reason: str = "resolved elsewhere",
                     now: float | None = None) -> list[int]:
    """Close every local slot sharing `origin_id` as cancelled."""
    if not origin_id:
        return []
    store = _store(store)
    closed = []
    for row in open_slots(store):
        if except_fact_id is not None and int(row["fact_id"]) == int(except_fact_id):
            continue
        if (row["slot"] or {}).get("origin_id") != origin_id:
            continue
        try:
            cancel(store, int(row["fact_id"]), reason=reason, now=now)
            closed.append(int(row["fact_id"]))
        except Exception as exc:
            print(f"[slots] sibling close skipped ({exc}).")
    return closed


# --- watching ----------------------------------------------------------------
def evaluate_event(store, event_id: int, event: Event | dict, *,
                   now: float | None = None) -> list[dict]:
    """Score one persisted event against every open slot. Returns the
    candidate fills that were OFFERED (score above the slot's threshold)."""
    if not enabled():
        return []
    store = _store(store)
    now = _now(now)
    offered: list[dict] = []
    for row in open_slots(store):
        slot = row["slot"] or {}
        if not eligible(event, slot=slot, event_id=event_id):
            continue
        if store.slot_candidate(int(row["fact_id"]), int(event_id)):
            continue  # the Not-it memory: never offered twice
        score, parts = score_event(slot, event)
        threshold = float(slot.get("match_threshold") or DEFAULT_THRESHOLD)
        if score < threshold:
            continue
        rid = store.add_slot_candidate(int(row["fact_id"]), int(event_id),
                                       score, "offered", ts=now)
        if rid is None:
            continue
        cand = {"fact_id": int(row["fact_id"]), "event_id": int(event_id),
                "score": score, "parts": parts, "need": slot.get("need"),
                "requester": slot.get("requester") or {}}
        offered.append(cand)
        try:
            offer_fill(store, row, int(event_id), event, score, now=now)
        except Exception as exc:
            print(f"[slots] fill offer skipped ({exc}).")
    return offered


def excerpt(event: Event | dict, limit: int = 240) -> str:
    text = event_text(event).strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit] + ("…" if len(text) > limit else "")


def requester_label(slot: dict) -> str:
    req = slot.get("requester") or {}
    if req.get("kind") == "peer":
        return req.get("name") or "your teammate"
    return "you"


def offer_fill(store, row: dict, event_id: int, event: Event | dict,
               score: float, *, now: float | None = None) -> bool:
    """A candidate fill is an OFFER: Deliver to <requester> / Not it."""
    slot = row["slot"] or {}
    who = requester_label(slot)
    need = slot.get("need") or row.get("text") or "that"
    ex = excerpt(event)
    if slot.get("requester", {}).get("kind") == "peer":
        head = f"{who} was asking about the {need} — send it?"
    else:
        head = f"This looks like the {need} you were waiting on."
    message = (f"{head}\n\n“{ex}”\n\nReply 'deliver' to send it to {who}"
               f"{'' if who != 'you' else ' (marks the task done)'}, or "
               "'not it' to keep watching.")
    cand = {
        "fact_id": int(row["fact_id"]), "event_id": int(event_id),
        "score": float(score), "need": need, "requester": slot.get("requester"),
        "message": message, "excerpt": ex,
    }
    if slot.get("deliver_on_fill"):
        # Pre-approved at slot creation: still an undo window, never silent.
        schedule_auto_delivery(store, int(row["fact_id"]), int(event_id),
                               delay_s=UNDO_S, now=now)
        _notify(f"Delivering the {need} to {who} in {int(UNDO_S)} s "
                f"(you pre-approved this slot) — reply 'undo' to stop.\n\n“{ex}”",
                stream={"type": "slot.fill_offer", "task_id": cand["fact_id"],
                        "event_id": cand["event_id"], "auto": True,
                        "actions": [{"label": "Undo", "reply": "undo"}]})
        try:
            from app.services.agent_bridge import worker
            worker.propose_slot_undo(cand)
        except Exception:
            pass
        return True
    try:
        from app.services.agent_bridge import worker
        return bool(worker.propose_slot_fill(cand))
    except Exception as exc:
        print(f"[slots] offer via chat skipped ({exc}).")
        _notify(message, stream={"type": "slot.fill_offer",
                                 "task_id": cand["fact_id"],
                                 "event_id": cand["event_id"],
                                 "actions": _fill_actions(who)})
        return False


def _fill_actions(who: str) -> list[dict]:
    return [{"label": f"Deliver to {who}", "reply": "deliver"},
            {"label": "Not it", "reply": "not it"}]


def reject_fill(store, fact_id: int, event_id: int, *, actor: str = "user",
                now: float | None = None) -> dict:
    """"Not it": lower that event's score, keep the slot open, never offer
    the same event for this slot again."""
    store = _store(store)
    _cancel_auto(int(fact_id))
    cand = store.slot_candidate(int(fact_id), int(event_id))
    score = float(cand["score"]) * 0.5 if cand else 0.0
    if cand:
        store.set_slot_candidate_verdict(int(fact_id), int(event_id), "rejected",
                                         score=score, ts=_now(now))
    else:
        store.add_slot_candidate(int(fact_id), int(event_id), score, "rejected",
                                 ts=_now(now))
    return {"ok": True, "fact_id": int(fact_id), "event_id": int(event_id),
            "verdict": "rejected", "score": score, "status": "awaiting_data"}


def deliver(store, fact_id: int, event_id: int, *, actor: str = "user",
            now: float | None = None) -> dict:
    """Deliver a fill to the slot's requester and close the slot with the
    fill as evidence. Only the first fill delivers; a later one is logged."""
    store = _store(store)
    now = _now(now)
    _cancel_auto(int(fact_id))
    row = get(store, fact_id)
    if not row:
        return {"ok": False, "error": "no such slot", "fact_id": int(fact_id)}
    if (row.get("status") or "") != "awaiting_data":
        store.add_slot_candidate(int(fact_id), int(event_id), 0.0, "superseded",
                                 ts=now)
        store.set_slot_candidate_verdict(int(fact_id), int(event_id), "superseded",
                                         ts=now)
        print(f"[slots] second fill for slot #{fact_id} (event {event_id}) "
              f"logged, not delivered (status={row.get('status')}).")
        return {"ok": False, "error": "slot already resolved",
                "status": row.get("status"), "fact_id": int(fact_id),
                "event_id": int(event_id), "superseded": True}
    ev = store.get_event(int(event_id))
    if not ev:
        return {"ok": False, "error": "no such event", "fact_id": int(fact_id)}
    slot = row["slot"] or {}
    req = slot.get("requester") or {"kind": "user"}
    need = slot.get("need") or row.get("text")
    ex = excerpt(ev, limit=600)
    result: dict[str, Any]
    if req.get("kind") == "peer":
        from app.services import peer_channel
        result = peer_channel.deliver_fill(str(req.get("id") or ""), row, ev)
    else:
        result = {"ok": True, "status": "delivered", "to": "user"}
        _notify(
            f"Delivered: the {need}.\n\n“{ex}”\n\nSource: /memory?event={int(event_id)}",
            stream={"type": "task.completed", "task_id": int(fact_id),
                    "event_id": int(event_id), "provenance":
                    {"event_id": int(event_id), "source": ev.get("source")}})
    if not result.get("ok"):
        return {**result, "fact_id": int(fact_id), "event_id": int(event_id)}
    verdict = "delivered" if result.get("status") == "delivered" else "withheld"
    if not store.set_slot_candidate_verdict(int(fact_id), int(event_id), verdict,
                                            ts=now):
        store.add_slot_candidate(int(fact_id), int(event_id), 1.0, verdict, ts=now)
    evidence = {"source": "slot_fill", "evidence_event_id": int(event_id),
                "note": ex[:240], "delivery": result.get("status"),
                "requester": req}
    out = store.transition_commitment(
        int(fact_id), "completed", actor=actor, reason="slot_filled",
        evidence=evidence, evidence_id=int(event_id), ts=now)
    _audit("slot.deliver", fact_id=int(fact_id), event_id=int(event_id),
           requester=req, status=result.get("status"))
    # `status` is the DELIVERY outcome (delivered | policy_denied); the
    # task's own status rides as task_status.
    return {**result, "fact_id": int(fact_id), "event_id": int(event_id),
            "verdict": verdict, "task_status": out.get("status"),
            "to_state": out.get("to_state"), "evidence_id": int(event_id)}


# --- auto delivery with undo ----------------------------------------------------
def schedule_auto_delivery(store, fact_id: int, event_id: int, *,
                           delay_s: float | None = None,
                           now: float | None = None) -> bool:
    delay = UNDO_S if delay_s is None else float(delay_s)
    store = _store(store)
    with _lock:
        _cancel_auto(int(fact_id))
        if delay <= 0:
            deliver(store, int(fact_id), int(event_id), actor="user", now=now)
            return True

        def _fire() -> None:
            with _lock:
                _pending_auto.pop(int(fact_id), None)
            try:
                deliver(store, int(fact_id), int(event_id), actor="user")
            except Exception as exc:
                print(f"[slots] auto delivery failed ({exc}).")

        t = threading.Timer(delay, _fire)
        t.daemon = True
        _pending_auto[int(fact_id)] = t
        t.start()
    return True


def undo(fact_id: int) -> bool:
    """Cancel a pending pre-approved delivery. True when one was pending."""
    return _cancel_auto(int(fact_id))


def _cancel_auto(fact_id: int) -> bool:
    with _lock:
        t = _pending_auto.pop(int(fact_id), None)
    if t is not None:
        try:
            t.cancel()
        except Exception:
            pass
        return True
    return False


def pending_auto() -> list[int]:
    with _lock:
        return sorted(_pending_auto)


# --- search assist (Option A) ----------------------------------------------------
def search_assist(store, need: str, *, allow_connectors: bool = True,
                  timeout_s: float | None = None, min_score: float = 0.35,
                  now: float | None = None) -> list[dict]:
    """One-shot local search: memory first, then connector-backed lookups
    when allowed. Bounded to memory and connected sources — never the open
    web. Returns candidate hits; a hit short-circuits the slot."""
    store = _store(store)
    need_text = normalize_need(need)
    deadline = _now(now) + float(SEARCH_ASSIST_S if timeout_s is None
                                 else timeout_s)
    hits: list[dict] = []
    try:
        from app.services.memory import memory
        for h in memory.search(need_text, limit=8) or []:
            text = h.get("text") or h.get("raw") or h.get("summary") or ""
            score = max(float(h.get("score") or 0.0), coverage(need_text, text))
            if score < min_score:
                continue
            eid = h.get("id") or h.get("event_id")
            if eid is None and h.get("time") is not None:
                ids = store.event_ids_at(float(h["time"]))
                eid = ids[0] if ids else None
            hits.append({"event_id": (int(eid) if eid is not None else None),
                         "score": round(score, 4), "text": text[:400],
                         "source": h.get("source"), "via": "memory"})
    except Exception as exc:
        print(f"[slots] memory search assist skipped ({exc}).")
    if allow_connectors and time.time() < deadline:
        try:
            from app.services.connectors import registry
            from app.services.connectors import scheduler
            for c in registry.all():
                if time.time() >= deadline:
                    break
                lookup = getattr(c, "lookup", None)
                if not callable(lookup):
                    continue
                try:
                    if not c.connected():
                        continue
                    items = lookup(need_text, timeout_s=max(
                        1.0, deadline - time.time())) or []
                except Exception as exc:
                    print(f"[slots] connector lookup {c.id} skipped ({exc}).")
                    continue
                for item in items:
                    eid = scheduler.land_item(c, item, store=store)
                    if eid:
                        hits.append({"event_id": int(eid), "score": 1.0,
                                     "text": str(item.get("text") or "")[:400],
                                     "source": f"{c.id}.{item.get('kind') or 'item'}",
                                     "via": "connector"})
        except Exception as exc:
            print(f"[slots] connector search assist skipped ({exc}).")
    hits.sort(key=lambda h: -float(h.get("score") or 0))
    return hits


# --- Option B: navigate to source ------------------------------------------------
_APP_HINTS = ("salesforce", "mail", "drive")


def fetch_goal(need: str, app_hint: str | None) -> str:
    hint = (app_hint or "").strip().lower()
    where = {"salesforce": "Salesforce", "mail": "your mail",
             "drive": "Google Drive"}.get(hint, hint or "the app you keep it in")
    return (f"Fetch the {normalize_need(need)} from {where}: open the record "
            f"or message that holds it and read back its full contents. "
            f"Read only — do not edit, send, or delete anything.")


def navigate_to_source(store, fact_id: int, *, app_hint: str | None = None,
                       need: str | None = None) -> dict:
    """Hand a planner goal {goal: fetch, need, app_hint} to the browser agent.
    Runs in the user's own profile under the existing hash-bound approval
    gate; the fetched page lands as a DOCUMENT event with source=agent.fetch
    which then fills the slot through the normal Deliver offer."""
    store = _store(store)
    row = get(store, fact_id) if fact_id else None
    need_text = need or ((row or {}).get("slot") or {}).get("need") or ""
    if not need_text:
        return {"ok": False, "error": "no need to fetch"}
    hint = (app_hint or "").strip().lower() or None
    fetch = {"goal": "fetch", "need": need_text, "app_hint": hint,
             "slot_id": int(fact_id) if fact_id else None}
    goal = fetch_goal(need_text, hint)
    try:
        from app.services.agent_bridge import worker
        worker.send(goal, fetch=fetch)
    except Exception as exc:
        return {"ok": False, "error": f"agent unavailable: {exc}", "goal": goal}
    _audit("slot.fetch", fact_id=int(fact_id or 0), app_hint=hint)
    return {"ok": True, "goal": goal, "fetch": fetch}


def land_fetch_result(fetch: dict, result: str, status: str | None,
                      *, store=None, now: float | None = None) -> int | None:
    """The page/record the agent read becomes a DOCUMENT event
    (source=agent.fetch); the insert hook then evaluates it against slots."""
    body = (result or "").strip()
    st = (status or "").strip().lower()
    if not body or st in ("error", "blocked", "cancelled", "plan_only",
                          "stopped_user", "desktop_unavailable"):
        return None
    if body.lower().startswith(("(no answer", "refused:", "okay, i won't")):
        return None
    store = _store(store)
    now = _now(now)
    from app.services import confidence as _conf
    ev = Event(
        time=now, modality=Modality.DOCUMENT, raw=body,
        summary=f"[fetch] {fetch.get('need') or ''}: {body[:120]}",
        source="agent.fetch",
        meta={"section": "agent", "origin": "agent_fetch",
              "title": str(fetch.get("need") or ""),
              "app_hint": fetch.get("app_hint"),
              "slot_id": fetch.get("slot_id"),
              "agent_status": st or None,
              "never_authorizes": True, "external_source": True},
    )
    _conf.attach(ev, _conf.OBSERVED, capture=0.9)
    eid = store.insert(ev)
    index_if_bound(store, eid, ev)
    try:
        from app.services.model_log import model_log
        model_log.log_egress(
            kind="agent_fetch",
            destination=str(fetch.get("app_hint") or "browser"),
            privacy_class=ev.privacy_class,
            approving_action="approval_gate",
            meta={"slot_id": fetch.get("slot_id"), "event_id": eid})
    except Exception:
        pass
    return eid


# --- erasure -------------------------------------------------------------------------
def notify_erasure(store=None) -> list[dict]:
    """Before 'Delete everything': tell every peer holding a slot for this
    user that it is resolved (reason erased). Returns what was sent."""
    store = _store(store)
    sent: list[dict] = []
    try:
        from app.services import peer_channel
    except Exception:
        return sent
    for row in open_slots(store):
        slot = row.get("slot") or {}
        origin = slot.get("origin_id")
        req = slot.get("requester") or {}
        targets = []
        if req.get("kind") == "peer" and req.get("id"):
            targets.append(str(req["id"]))
        for pid in slot.get("member_peer_ids") or []:
            if pid not in targets:
                targets.append(pid)
        for pid in targets:
            try:
                res = peer_channel.send_slot_resolved(
                    pid, origin_id=origin, slot_id=int(row["fact_id"]),
                    reason="erased")
                sent.append({"peer_id": pid, "slot_id": int(row["fact_id"]),
                             "origin_id": origin, "ok": bool(res.get("ok"))})
            except Exception as exc:
                sent.append({"peer_id": pid, "slot_id": int(row["fact_id"]),
                             "origin_id": origin, "ok": False, "error": str(exc)})
    return sent


# --- horizon -----------------------------------------------------------------------------
def horizon_items(store, *, now: float | None = None) -> list[dict]:
    """Stale slots (past review_after) as Keep / Drop chips."""
    now = _now(now)
    out = []
    for row in stale(store, now=now):
        slot = row.get("slot") or {}
        days = int((now - float(slot.get("created_at") or row.get("extracted_at")
                                 or now)) / 86400)
        out.append({
            "kind": "slot_review",
            "label": f"Still watching for: {slot.get('need') or row.get('text')}",
            "p_need": 0.7,
            "when_s": 0.0,
            "when_label": f"open {days}d — keep watching?",
            "reason": [f"requested by {requester_label(slot)}",
                       "no fill yet"],
            "evidence": {"need": slot.get("need"), "requester": slot.get("requester")},
            "fact_id": int(row["fact_id"]),
            "node_type": "fact",
            "node_id": int(row["fact_id"]),
            "actions": [{"label": "Keep", "reply": "keep"},
                        {"label": "Drop", "reply": "drop"}],
        })
    return out


# --- helpers -----------------------------------------------------------------------------
def _notify(text: str, *, stream: dict | None = None) -> None:
    try:
        from app.services.agent_bridge import worker
        worker._emit("result", text, stream=stream)
    except Exception:
        pass


def _audit(event: str, **fields) -> None:
    try:
        from app.services import agent_log
        agent_log.audit(event, **fields)
    except Exception:
        pass
