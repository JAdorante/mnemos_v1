"""Commitment completion candidates (plan 4.2).

Three producers, one invariant:

  (a) User speech with resolves_commitment / "sent/done" → cosine-near open
      commitment → **offer**, never auto-complete.
  (b) Agent verified send whose packet `source_fact_ids` include the
      commitment → `transition_commitment(completed)` with evidence.
  (c) Screen Sent-toast / Sent-folder OCR → same offer path as (a).

A generated plan must never complete anything — only verified execution
(or an accepted user offer) may call `transition_commitment`.
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any

from app.services.commitment_state import OPEN_STATES

# Cosine / overlap floor for matching a resolve hint to an open commitment.
MATCH_MIN = float(os.environ.get("QUILL_COMMIT_RESOLVE_MIN", "0.55"))
_COOLDOWN_S = 300
_recent: dict[str, float] = {}
_lock = threading.Lock()

_RESOLVE_HINT = re.compile(
    r"\b("
    r"i\s+(?:just\s+)?(?:sent|emailed|texted|messaged|shipped|mailed)|"
    r"i\s+(?:just\s+)?(?:finished|completed|done\s+with)|"
    r"(?:it'?s|thats|that's)\s+done|"
    r"already\s+sent|all\s+done|marked\s+(?:it\s+)?done|"
    r"i\s+took\s+care\s+of"
    r")\b",
    re.I,
)

_SENT_TOAST = re.compile(
    r"\b("
    r"message\s+sent|email\s+sent|your\s+message\s+has\s+been\s+sent|"
    r"sent\s+to\s+\w+|moved\s+to\s+sent|in\s+sent\s+(?:items|folder)|"
    r"sent\s+folder"
    r")\b",
    re.I,
)


def looks_like_resolve(text: str) -> bool:
    return bool(_RESOLVE_HINT.search(text or ""))


def looks_like_sent_toast(text: str) -> bool:
    return bool(_SENT_TOAST.search(text or ""))


def _enabled() -> bool:
    return os.environ.get("QUILL_COMMIT_RESOLVE", "1") not in (
        "0", "false", "False")


def _hash(key: str) -> str:
    return hashlib.sha1((key or "").strip().lower().encode()).hexdigest()


def _token_overlap(a: str, b: str) -> float:
    stop = {
        "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "my",
        "me", "i", "you", "we", "is", "are", "be", "with", "at", "this",
        "that", "it", "as", "from", "about", "just", "sent", "done", "already",
    }
    ta = {w for w in re.findall(r"[a-z0-9]{3,}", (a or "").lower()) if w not in stop}
    tb = {w for w in re.findall(r"[a-z0-9]{3,}", (b or "").lower()) if w not in stop}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(1, len(ta | tb))


def _is_open_commitment(row: dict | None) -> bool:
    if not row or row.get("kind") != "commitment":
        return False
    if (row.get("status") or "") != "open":
        return False
    state = (row.get("commitment_state") or "active").strip().lower()
    return state in OPEN_STATES or not row.get("commitment_state")


def find_open_matches(
    text: str,
    *,
    store=None,
    min_score: float | None = None,
    k: int = 4,
) -> list[dict[str, Any]]:
    """Nearest open commitments for a resolve hint. Best-effort; never raises."""
    text = (text or "").strip()
    if not text:
        return []
    min_score = MATCH_MIN if min_score is None else float(min_score)
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
    except Exception:
        return []

    # Cosine via fact vectors when available.
    try:
        from app.services.memory import memory
        for fid, score, ftext in memory.similar_facts(
                "commitment", text, k=k) or []:
            if score < min_score:
                continue
            row = store.get_fact(int(fid))
            if not _is_open_commitment(row):
                continue
            fid_i = int(fid)
            if fid_i in seen:
                continue
            seen.add(fid_i)
            out.append({
                "fact_id": fid_i,
                "text": (row or {}).get("text") or ftext,
                "score": float(score),
                "via": "cosine",
            })
    except Exception:
        pass

    # Token-overlap fallback (tests / cold vector index).
    try:
        opens = store.list_facts(kind="commitment", status="open", limit=40)
        ranked = []
        for row in opens:
            if not _is_open_commitment(row):
                continue
            fid = int(row["fact_id"])
            if fid in seen:
                continue
            sc = _token_overlap(text, row.get("text") or "")
            if sc >= max(0.25, min_score * 0.5):
                ranked.append((sc, row))
        ranked.sort(key=lambda x: -x[0])
        for sc, row in ranked[:k]:
            fid = int(row["fact_id"])
            if fid in seen:
                continue
            seen.add(fid)
            out.append({
                "fact_id": fid,
                "text": row.get("text") or "",
                "score": float(sc),
                "via": "overlap",
            })
    except Exception:
        pass

    out.sort(key=lambda r: -float(r.get("score") or 0))
    return out[:k]


def offer_resolve(
    fact_id: int,
    commitment_text: str,
    *,
    source: str,
    quote: str = "",
    event_id: int | None = None,
    score: float | None = None,
) -> bool:
    """Surface a yes/no 'mark complete?' offer. Never transitions."""
    if not _enabled():
        return False
    try:
        fact_id = int(fact_id)
        text = (commitment_text or "").strip()
        if not text:
            return False
        h = _hash(f"resolve:{fact_id}:{text}")
        now = time.time()
        with _lock:
            last = _recent.get(h)
            if last is not None and now - last < _COOLDOWN_S:
                return False
            _recent[h] = now

        conf_s = (f" · {round(float(score) * 100)}% match"
                  if score is not None else "")
        quote_s = f"\nHeard: “{quote.strip()[:160]}”\n" if quote.strip() else "\n"
        message = (
            f"Looks done{conf_s}: “{text}”{quote_s}\n"
            "Reply 'yes' to mark this commitment completed (with this as "
            "evidence), or 'no' to leave it open."
        )
        from app.services.agent_bridge import worker
        return worker.propose_commitment_resolve({
            "fact_id": fact_id,
            "text": text,
            "message": message,
            "source": source,
            "quote": quote,
            "event_id": event_id,
            "score": score,
        })
    except Exception as exc:
        print(f"[commit-resolve] offer skipped ({exc}).")
        return False


def offer_matches_for_text(
    text: str,
    *,
    source: str,
    event_id: int | None = None,
    store=None,
    force: bool = False,
) -> list[dict]:
    """Match + offer for resolve-like / sent-toast text. Offers only."""
    text = (text or "").strip()
    if not text or not _enabled():
        return []
    if not force and not (looks_like_resolve(text) or looks_like_sent_toast(text)):
        return []
    offered = []
    for m in find_open_matches(text, store=store):
        ok = offer_resolve(
            m["fact_id"], m["text"],
            source=source, quote=text[:200],
            event_id=event_id, score=m.get("score"))
        if ok:
            offered.append(m)
    return offered


def accept_resolve_offer(pend: dict) -> dict:
    """User accepted a commitment_resolve offer → complete with cited evidence."""
    from app.storage import get_store
    from app.services.commitment_state import TransitionError

    fid = pend.get("fact_id")
    if fid is None:
        return {"ok": False, "error": "no fact_id"}
    evidence = {
        "source": pend.get("source") or "user_confirm_resolve",
        "note": (pend.get("quote") or pend.get("text") or "")[:240],
    }
    if pend.get("event_id") is not None:
        evidence["evidence_event_id"] = int(pend["event_id"])
    try:
        out = get_store().transition_commitment(
            int(fid), "completed",
            reason="user_confirm_resolve",
            evidence=evidence,
            actor="user",
        )
        return out
    except TransitionError as exc:
        return {"ok": False, "error": str(exc)}


def complete_from_verified_send(
    source_fact_ids: list[int] | None,
    *,
    packet_id: int | None = None,
    evidence_event_id: int | None = None,
    dry_run: str | None = None,
    store=None,
) -> list[dict]:
    """Auto-complete open commitments cited on a verified send packet.

    Plan-only / dry_run=plan never completes (AC).
    """
    if dry_run in ("plan", "dry"):
        return []
    if not _enabled():
        return []
    ids = [int(x) for x in (source_fact_ids or []) if x is not None]
    if not ids:
        return []
    try:
        if store is None:
            from app.storage import get_store
            store = get_store()
    except Exception:
        return []

    done: list[dict] = []
    for fid in ids:
        try:
            row = store.get_fact(fid)
            if not _is_open_commitment(row):
                continue
            evidence = {
                "source": "verified_send",
                "source_fact_ids": ids,
            }
            if packet_id is not None:
                evidence["packet_id"] = int(packet_id)
            if evidence_event_id is not None:
                evidence["evidence_event_id"] = int(evidence_event_id)
            else:
                # Packet/run cite is enough for 4.1 evidence_ok; prefer a note.
                evidence["note"] = f"verified send for commitment #{fid}"
            out = store.transition_commitment(
                fid, "completed",
                reason="agent_verified_send",
                evidence=evidence,
                actor="agent",
            )
            if out.get("ok") and not out.get("noop"):
                done.append(out)
                print(f"[commit-resolve] verified send completed "
                      f"commitment #{fid}")
        except Exception as exc:
            print(f"[commit-resolve] verified send skip #{fid} ({exc}).")
    return done


def clear_cooldowns_for_tests() -> None:
    with _lock:
        _recent.clear()
