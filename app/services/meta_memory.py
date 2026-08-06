"""Meta-memory audits (Track A4 / Field §13.2) — deterministic first.

First slice: at-risk commitments (escalate U — D8 attention-only auto-apply),
stale facts, and forget candidates (review-first reflection items).
"""
from __future__ import annotations

import os
import time
from typing import Any

URGENT_U = 0.80
STALE_DAYS = 45.0
FORGET_DAYS = 120.0
DROPPED_DAYS = 21.0
FADING_DAYS = 60.0
QUESTION_DAYS = 14.0
WEAKEN_DAYS = 30.0
# Multi-year wrong-year ISO dues (audit: 2023-07-07 → 1125d) are Memory cleanup,
# not urgency / chat fuel. Align with QUILL_REASONER_MAX_OVERDUE_D.
MAX_AT_RISK_OVERDUE_D = float(
    os.environ.get("QUILL_REASONER_MAX_OVERDUE_D", "60") or "60")


def _auto_urgency_enabled() -> bool:
    try:
        from app.config import settings
        return bool(settings.attention.meta_auto_urgency)
    except Exception:
        return True


def scan_at_risk(store, *, now: float | None = None) -> list[dict]:
    """Open commitments/tasks that look overdue or due soon without recent touch."""
    now = float(now if now is not None else time.time())
    out = []
    try:
        facts = (store.list_facts(kind="commitment", status="open", limit=200)
                 + store.list_facts(kind="task", status="open", limit=200))
    except Exception:
        return []
    for f in facts:
        fid = f.get("fact_id") or f.get("id")
        if fid is None:
            continue
        due = f.get("due")
        due_ts = None
        if isinstance(due, (int, float)):
            due_ts = float(due)
        elif isinstance(due, str) and due.strip():
            try:
                from app.services.clock import coerce_due, is_iso_due, parse_due
                # ISO as-is; legacy US slash / free text via coerce (junk → None).
                norm = due if is_iso_due(due) else coerce_due(due)
                if norm and is_iso_due(norm):
                    due_ts = parse_due(norm).timestamp()
            except Exception:
                due_ts = None
        extracted = float(f.get("extracted_at") or f.get("updated_at") or 0)
        risk = 0.0
        why = []
        if due_ts is not None:
            dt = due_ts - now
            if dt < 0:
                overdue_d = int(-dt / 86400) or 1
                if overdue_d > MAX_AT_RISK_OVERDUE_D:
                    continue  # wrong-year / multi-year junk → Memory cleanup
                risk = min(1.0, 0.75 + min(14.0, float(overdue_d)) * 0.04)
                why.append(f"overdue by {overdue_d}d")
            elif dt < 2 * 86400:
                risk = 0.78
                why.append("due within 2 days")
        age_days = (now - extracted) / 86400.0 if extracted else 0
        if age_days > 14 and risk < 0.7:
            risk = max(risk, 0.55)
            why.append(f"open {int(age_days)}d with no due date")
        if risk < 0.75:
            continue
        out.append({
            "kind": "risk",
            "fact_id": int(fid),
            "text": (f.get("text") or "")[:120],
            "risk": round(risk, 3),
            "why": why,
            "subject": f.get("owner") or f.get("from_person") or "",
        })
    out.sort(key=lambda x: -x["risk"])
    return out


def apply_urgency(store, risks: list[dict] | None = None,
                  *, now: float | None = None) -> dict[str, Any]:
    """D8 attention-only: set node_dynamics.U and att_state=urgent for at-risk."""
    if not _auto_urgency_enabled():
        return {"applied": 0, "skipped": "disabled"}
    now = float(now if now is not None else time.time())
    risks = risks if risks is not None else scan_at_risk(store, now=now)
    n = 0
    for r in risks[:8]:
        fid = r.get("fact_id")
        if fid is None:
            continue
        try:
            store.set_node_urgency("fact", int(fid), URGENT_U, state="urgent")
            n += 1
        except Exception:
            # Fallback: att_state only
            try:
                store.set_att_state("fact", int(fid), "urgent")
                n += 1
            except Exception:
                pass
    return {"applied": n, "candidates": len(risks)}


def _is_living(f: dict) -> bool:
    """Tasks/commitments must be open; claims/other kinds with null status count."""
    st = (f.get("status") or "").lower()
    if st in ("done", "cancelled", "closed"):
        return False
    if f.get("review") == "dismissed":
        return False
    if (f.get("state") or "active") != "active":
        return False
    return True


def scan_stale_facts(store, *, now: float | None = None,
                     stale_days: float = STALE_DAYS) -> list[dict]:
    now = float(now if now is not None else time.time())
    out = []
    try:
        facts = store.list_facts(limit=300)
    except Exception:
        return []
    for f in facts:
        if not _is_living(f):
            continue
        if f.get("kind") in ("task", "commitment"):
            continue  # handled by at-risk
        if f.get("status") not in (None, "", "open"):
            continue
        fid = f.get("fact_id") or f.get("id")
        ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        if not ts:
            continue
        age = (now - ts) / 86400.0
        if age < stale_days:
            continue
        out.append({
            "kind": "stale_fact",
            "fact_id": int(fid),
            "text": (f.get("text") or "")[:120],
            "age_days": round(age, 1),
            "subject": f.get("owner") or "",
        })
    return out[:12]


def scan_forget_candidates(store, *, now: float | None = None,
                           forget_days: float = FORGET_DAYS) -> list[dict]:
    """Review-first proposals — never auto-tombstone."""
    now = float(now if now is not None else time.time())
    out = []
    try:
        facts = store.list_facts(limit=300)
    except Exception:
        return []
    for f in facts:
        if not _is_living(f):
            continue
        fid = f.get("fact_id") or f.get("id")
        ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        conf = float(f.get("confidence") or 0.5)
        if not ts:
            continue
        age = (now - ts) / 86400.0
        if age < forget_days or conf >= 0.85:
            continue
        out.append({
            "kind": "forget_candidate",
            "fact_id": int(fid),
            "text": (f.get("text") or "")[:120],
            "age_days": round(age, 1),
            "confidence": conf,
            "subject": f.get("owner") or "",
        })
    return out[:8]


def scan_dropped_threads(store, *, now: float | None = None,
                         days: float = DROPPED_DAYS) -> list[dict]:
    """Commitments/tasks open past DROPPED_DAYS with no recent access."""
    now = float(now if now is not None else time.time())
    out = []
    try:
        facts = store.list_facts(status="open", limit=300)
    except Exception:
        return []
    for f in facts:
        if f.get("kind") not in ("task", "commitment"):
            continue
        fid = f.get("fact_id") or f.get("id")
        ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        if not ts:
            continue
        age = (now - ts) / 86400.0
        if age < days:
            continue
        last_access = _last_access(store, f)
        quiet = (now - last_access) / 86400.0 if last_access else age
        if quiet < days * 0.5:
            continue
        out.append({
            "kind": "dropped_thread",
            "fact_id": int(fid),
            "text": (f.get("text") or "")[:120],
            "age_days": round(age, 1),
            "quiet_days": round(quiet, 1),
            "subject": f.get("owner") or "",
        })
    return out[:10]


def scan_fading_ideas(store, *, now: float | None = None,
                      days: float = FADING_DAYS) -> list[dict]:
    """Idea-like facts that aged out without reinforcement."""
    now = float(now if now is not None else time.time())
    out = []
    try:
        facts = store.list_facts(limit=300)
    except Exception:
        return []
    for f in facts:
        if not _is_living(f):
            continue
        kind = (f.get("kind") or "").lower()
        text = (f.get("text") or "").lower()
        if kind not in ("idea", "insight", "note") and "idea:" not in text[:40]:
            if kind not in ("preference",) and "what if" not in text:
                continue
        fid = f.get("fact_id") or f.get("id")
        ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        conf = float(f.get("confidence") or 0.5)
        if not ts:
            continue
        age = (now - ts) / 86400.0
        if age < days or conf >= 0.9:
            continue
        out.append({
            "kind": "fading_idea",
            "fact_id": int(fid),
            "text": (f.get("text") or "")[:120],
            "age_days": round(age, 1),
            "confidence": conf,
            "subject": f.get("owner") or "",
        })
    return out[:8]


def scan_open_questions(store, *, now: float | None = None,
                        days: float = QUESTION_DAYS) -> list[dict]:
    """Open facts that look like unanswered questions."""
    now = float(now if now is not None else time.time())
    out = []
    try:
        facts = store.list_facts(limit=300)
    except Exception:
        return []
    for f in facts:
        if not _is_living(f):
            continue
        # Typed rows must still be open; claims (null status) are fine.
        if f.get("kind") in ("task", "commitment") and f.get("status") != "open":
            continue
        text = (f.get("text") or "").strip()
        if "?" not in text and not text.lower().startswith(
                ("how ", "why ", "what ", "when ", "where ", "should ")):
            continue
        fid = f.get("fact_id") or f.get("id")
        ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        if not ts:
            continue
        age = (now - ts) / 86400.0
        if age < days:
            continue
        out.append({
            "kind": "open_question",
            "fact_id": int(fid),
            "text": text[:120],
            "age_days": round(age, 1),
            "subject": f.get("owner") or "",
        })
    return out[:8]


def scan_weakening_relationships(store, *, now: float | None = None,
                                 days: float = WEAKEN_DAYS) -> list[dict]:
    """People quiet longer than WEAKEN_DAYS."""
    now = float(now if now is not None else time.time())
    out = []
    try:
        people = store.all_people()
    except Exception:
        return []
    for p in people or []:
        nid = p.get("id")
        name = (p.get("name") or "").strip()
        if not nid or not name:
            continue
        last = _node_last_seen(store, nid, {
            "last_seen": p.get("last_seen"),
            "id": nid,
        })
        if not last:
            continue
        quiet = (now - last) / 86400.0
        if quiet < days:
            continue
        out.append({
            "kind": "weakening_relationship",
            "node_id": int(nid),
            "name": name[:80],
            "quiet_days": round(quiet, 1),
        })
    out.sort(key=lambda r: -float(r.get("quiet_days") or 0))
    return out[:8]


def _last_access(store, fact: dict) -> float | None:
    try:
        fid = fact.get("fact_id") or fact.get("id")
        m = store.node_dynamics_map([("fact", int(fid))])
        d = m.get(("fact", int(fid))) or {}
        recent = d.get("access_recent")
        if isinstance(recent, str):
            import json as _json
            recent = _json.loads(recent or "[]")
        if recent:
            return float(max(recent))
        if d.get("updated_at"):
            return float(d["updated_at"])
    except Exception:
        pass
    return float(fact.get("updated_at") or fact.get("extracted_at") or 0) or None


def _node_last_seen(store, nid, node: dict) -> float | None:
    for key in ("last_seen", "updated_at", "ts", "created_at"):
        v = node.get(key)
        if v:
            try:
                return float(v)
            except Exception:
                pass
    try:
        m = store.node_dynamics_map([("person", int(nid))])
        d = m.get(("person", int(nid))) or {}
        recent = d.get("access_recent")
        if isinstance(recent, str):
            import json as _json
            recent = _json.loads(recent or "[]")
        if recent:
            return float(max(recent))
        if d.get("updated_at"):
            return float(d["updated_at"])
    except Exception:
        pass
    return None


def run(store=None, *, write_reflections: bool = True) -> dict[str, Any]:
    """One meta-memory pass: escalate urgency + emit review-first items."""
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    risks = scan_at_risk(store)
    urgency = apply_urgency(store, risks)
    stale = scan_stale_facts(store)
    forget = scan_forget_candidates(store)
    dropped = scan_dropped_threads(store)
    fading = scan_fading_ideas(store)
    questions = scan_open_questions(store)
    weakening = scan_weakening_relationships(store)

    learning_weak: list[dict] = []
    try:
        from app.services import learning_memory as _lme
        learning_weak = _lme.weak_concepts(store, limit=8)
    except Exception as exc:
        print(f"[meta_memory] learning_weak skipped ({exc}).")

    written = 0
    if write_reflections:
        written = _write_reflection_items(
            store, risks, stale, forget,
            dropped=dropped, fading=fading,
            questions=questions, weakening=weakening,
        )

    return {
        "ok": True,
        "at_risk": len(risks),
        "urgency": urgency,
        "stale_facts": len(stale),
        "forget_candidates": len(forget),
        "dropped_threads": len(dropped),
        "fading_ideas": len(fading),
        "open_questions": len(questions),
        "weakening_relationships": len(weakening),
        "learning_weak": len(learning_weak),
        "reflection_items": written,
        "samples": {
            "risk": risks[:3],
            "stale": stale[:2],
            "forget": forget[:2],
            "dropped": dropped[:2],
            "fading": fading[:2],
            "questions": questions[:2],
            "weakening": weakening[:2],
            "learning_weak": learning_weak[:3],
        },
    }


def _write_reflection_items(store, risks, stale, forget,
                            dropped=None, fading=None,
                            questions=None, weakening=None) -> int:
    """Attach audit items to a lightweight daily reflection shell."""
    try:
        now = time.time()
        refls = store.list_reflections(scope="daily", limit=1)
        rid = None
        if refls:
            r = refls[0]
            rts = float(r.get("created_at") or r.get("ts") or 0)
            if now - rts < 18 * 3600:
                rid = r.get("id") or r.get("reflection_id")
        if rid is None:
            rid = store.add_reflection(
                scope="daily",
                period_start=now - 86400,
                period_end=now,
                summary="Meta-memory audit (automatic).",
                confidence=0.6,
                created_at=now,
            )
        n = 0
        for r in risks[:5]:
            store.add_reflection_item(
                int(rid), kind="risk",
                text=f"At risk: {r.get('text')}",
                detail="; ".join(r.get("why") or []),
                subject=r.get("subject") or "",
                confidence=float(r.get("risk") or 0.7),
                source_fact_ids=[r["fact_id"]],
                created_at=now,
            )
            n += 1
        for s in stale[:4]:
            store.add_reflection_item(
                int(rid), kind="stale_fact",
                text=f"Possibly stale ({s['age_days']}d): {s.get('text')}",
                detail="Review whether this still holds.",
                subject=s.get("subject") or "",
                confidence=0.55,
                source_fact_ids=[s["fact_id"]],
                created_at=now,
            )
            n += 1
        for f in forget[:3]:
            store.add_reflection_item(
                int(rid), kind="forget_candidate",
                text=f"Forget candidate: {f.get('text')}",
                detail="Review-first — will not delete without approval.",
                subject=f.get("subject") or "",
                confidence=0.5,
                source_fact_ids=[f["fact_id"]],
                created_at=now,
            )
            n += 1
        for d in (dropped or [])[:3]:
            store.add_reflection_item(
                int(rid), kind="dropped_thread",
                text=f"Dropped thread ({d.get('quiet_days')}d quiet): {d.get('text')}",
                detail="Commitment/task with no recent touch — reopen or close?",
                subject=d.get("subject") or "",
                confidence=0.6,
                source_fact_ids=[d["fact_id"]],
                created_at=now,
            )
            n += 1
        for idea in (fading or [])[:3]:
            store.add_reflection_item(
                int(rid), kind="fading_idea",
                text=f"Fading idea ({idea.get('age_days')}d): {idea.get('text')}",
                detail="Reinforce, archive, or let it go.",
                subject=idea.get("subject") or "",
                confidence=0.55,
                source_fact_ids=[idea["fact_id"]],
                created_at=now,
            )
            n += 1
        for q in (questions or [])[:3]:
            store.add_reflection_item(
                int(rid), kind="open_question",
                text=f"Open question ({q.get('age_days')}d): {q.get('text')}",
                detail="Still unanswered — worth a brief?",
                subject=q.get("subject") or "",
                confidence=0.6,
                source_fact_ids=[q["fact_id"]],
                created_at=now,
            )
            n += 1
        for w in (weakening or [])[:3]:
            store.add_reflection_item(
                int(rid), kind="weakening_relationship",
                text=f"Quiet contact ({w.get('quiet_days')}d): {w.get('name')}",
                detail="Relationship signal — optional nudge, never auto-message.",
                subject=w.get("name") or "",
                confidence=0.55,
                source_fact_ids=[],
                created_at=now,
            )
            n += 1
        return n
    except Exception as exc:
        print(f"[meta_memory] reflection write skipped ({exc}).")
        return 0
