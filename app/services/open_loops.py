"""Open-loop engine (plan 4.3) — deterministic detectors, precision-first.

Surfaces unfinished business the user is still waiting on:

  * waiting_on_me   — my overdue commitments (I owe them)
  * waiting_on_them — others' overdue promises to me
  * unanswered_q    — open questions (extractor `questions` + heuristics)
  * pending_ask     — agent awaiting yes/no or approval

Snooze via `commitments.last_surfaced` + dismiss telemetry. Horizon/Today
consume `horizon_items()`; never auto-completes a commitment.
"""
from __future__ import annotations

import os
import time
from typing import Any

from app.services.commitment_state import OPEN_STATES

# Default 24h snooze after surface/dismiss (precision-first).
SNOOZE_S = float(os.environ.get("QUILL_OPEN_LOOP_SNOOZE_S", str(24 * 3600)))
# Aging open commitment without due still counts after this many days.
AGING_DAYS = float(os.environ.get("QUILL_OPEN_LOOP_AGING_DAYS", "7"))

def _enabled() -> bool:
    return os.environ.get("QUILL_OPEN_LOOPS", "1") not in ("0", "false", "False")


def _parse_due_ts(due, *, now: float) -> float | None:
    if due is None:
        return None
    if isinstance(due, (int, float)):
        return float(due)
    s = str(due).strip()
    if not s:
        return None
    try:
        from app.services.clock import is_iso_due, parse_due
        if is_iso_due(s):
            return parse_due(s).timestamp()
    except Exception:
        pass
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _snoozed(last_surfaced, *, now: float) -> bool:
    if last_surfaced is None:
        return False
    try:
        return (now - float(last_surfaced)) < SNOOZE_S
    except (TypeError, ValueError):
        return False


def _is_open_commitment(row: dict) -> bool:
    if (row.get("kind") or "") != "commitment":
        return False
    if (row.get("status") or "") != "open":
        return False
    state = (row.get("commitment_state") or "active").strip().lower()
    return state in OPEN_STATES or not row.get("commitment_state")


def _self_names(store) -> set[str]:
    names = {"me", "i", "myself"}
    try:
        from app.services.identity import user_identity
        n = (user_identity(store).get("name") or "").strip().lower()
        if n:
            names.add(n)
            names.add(n.split()[0])
    except Exception:
        pass
    return names


def _name_is_self(name: str, self_names: set[str]) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    if n in self_names:
        return True
    try:
        from app.services.self_profile import is_self_name
        return is_self_name(name)
    except Exception:
        return False


def _party(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def _evidence(row: dict) -> dict[str, Any]:
    return {
        "fact_id": row.get("fact_id"),
        "source_event_id": row.get("source_event_id"),
        "source_span": (row.get("source_span") or "")[:200],
        "text": (row.get("text") or "")[:160],
        "due": row.get("due"),
        "from_person": row.get("from_person"),
        "to_person": row.get("to_person"),
    }


def _record_surface(*, kind: str, fact_id: int | None = None,
                    text: str = "") -> None:
    try:
        from app.services.cog_telemetry import cog_telemetry, OPEN_LOOP
        cog_telemetry.record(
            OPEN_LOOP, True, kind=kind, fact_id=fact_id, text=(text or "")[:80])
    except Exception:
        pass


def _record_dismiss(*, kind: str, fact_id: int | None = None,
                    text: str = "") -> None:
    try:
        from app.services.cog_telemetry import cog_telemetry, OPEN_LOOP_DISMISS
        cog_telemetry.record(
            OPEN_LOOP_DISMISS, True,
            kind=kind, fact_id=fact_id, text=(text or "")[:80])
    except Exception:
        pass


def detect_waiting_on_me(store, *, now: float | None = None) -> list[dict]:
    """My overdue (or aging) commitments — I owe the counterparty."""
    now = float(now if now is not None else time.time())
    self_names = _self_names(store)
    out: list[dict] = []
    try:
        rows = store.list_facts(kind="commitment", status="open", limit=200)
    except Exception:
        return []
    for row in rows:
        if not _is_open_commitment(row):
            continue
        if _snoozed(row.get("last_surfaced"), now=now):
            continue
        frm = _party(row, "from_person")
        if not _name_is_self(frm, self_names):
            continue
        due_ts = _parse_due_ts(row.get("due"), now=now)
        extracted = float(row.get("extracted_at") or row.get("updated_at") or 0)
        overdue = due_ts is not None and due_ts < now
        aging = (due_ts is None and extracted
                 and (now - extracted) >= AGING_DAYS * 86400)
        if not (overdue or aging):
            continue
        why = []
        if overdue:
            days = max(1, int((now - due_ts) / 86400))
            why.append(f"overdue by {days}d — you owe this")
        if aging:
            why.append(f"open {int((now - extracted) / 86400)}d with no due")
        to = _party(row, "to_person")
        if to:
            why.append(f"promised to {to}")
        p = 0.88 if overdue else 0.78
        out.append({
            "kind": "waiting_on_me",
            "label": (row.get("text") or "open commitment")[:80],
            "p_need": p,
            "when_s": (due_ts - now) if due_ts is not None else 0.0,
            "when_label": "overdue" if overdue else "aging",
            "reason": why[:4],
            "evidence": _evidence(row),
            "fact_id": int(row["fact_id"]),
            "node_type": "fact",
            "node_id": int(row["fact_id"]),
        })
    out.sort(key=lambda x: -float(x["p_need"]))
    return out


def detect_waiting_on_them(store, *, now: float | None = None) -> list[dict]:
    """Others' overdue promises to me — waiting on them (AC focus)."""
    now = float(now if now is not None else time.time())
    self_names = _self_names(store)
    out: list[dict] = []
    try:
        rows = store.list_facts(kind="commitment", status="open", limit=200)
    except Exception:
        return []
    for row in rows:
        if not _is_open_commitment(row):
            continue
        if _snoozed(row.get("last_surfaced"), now=now):
            continue
        frm = _party(row, "from_person")
        to = _party(row, "to_person")
        if _name_is_self(frm, self_names):
            continue  # I made the promise → waiting_on_me
        expects = bool(row.get("counterparty_expects"))
        to_self = _name_is_self(to, self_names) or not to
        if not (to_self or expects):
            continue
        due_ts = _parse_due_ts(row.get("due"), now=now)
        extracted = float(row.get("extracted_at") or row.get("updated_at") or 0)
        overdue = due_ts is not None and due_ts < now
        aging = (due_ts is None and extracted
                 and (now - extracted) >= AGING_DAYS * 86400)
        if not (overdue or aging or expects):
            continue
        if expects and not (overdue or aging):
            # Soft: counterparty flag without due — still surface lightly.
            if extracted and (now - extracted) < 2 * 86400:
                continue
        why = []
        who = frm or "them"
        if overdue:
            days = max(1, int((now - due_ts) / 86400))
            why.append(f"waiting on {who} — overdue {days}d")
        elif aging:
            why.append(f"waiting on {who} — open {int((now - extracted) / 86400)}d")
        else:
            why.append(f"waiting on {who}")
        span = (row.get("source_span") or "").strip()
        if span:
            why.append(f"evidence: “{span[:80]}”")
        p = 0.92 if overdue else (0.84 if aging else 0.80)
        out.append({
            "kind": "waiting_on_them",
            "label": (row.get("text") or f"waiting on {who}")[:80],
            "p_need": p,
            "when_s": (due_ts - now) if due_ts is not None else 0.0,
            "when_label": "waiting on them",
            "reason": why[:4],
            "evidence": _evidence(row),
            "fact_id": int(row["fact_id"]),
            "node_type": "fact",
            "node_id": int(row["fact_id"]),
        })
        # Best-effort: stamp counterparty_expects for future open-loop passes.
        if not expects:
            try:
                store.set_counterparty_expects(int(row["fact_id"]), True)
            except Exception:
                pass
    out.sort(key=lambda x: -float(x["p_need"]))
    return out


def detect_unanswered_questions(store, *, now: float | None = None) -> list[dict]:
    """Extractor `questions` rows + meta_memory open-question heuristics."""
    now = float(now if now is not None else time.time())
    out: list[dict] = []
    seen: set[int] = set()

    # Typed questions from extraction (plan 4.3) — surface after ~1 day.
    try:
        rows = store.list_facts(kind="question", limit=40)
    except Exception:
        rows = []
    for row in rows:
        fid = row.get("fact_id")
        if fid is None:
            continue
        ts = float(row.get("updated_at") or row.get("extracted_at") or 0)
        if ts and (now - ts) < 86400:
            continue
        text = (row.get("text") or "").strip()
        if not text:
            continue
        seen.add(int(fid))
        out.append({
            "kind": "unanswered_q",
            "label": text[:80],
            "p_need": 0.74,
            "when_s": 0.0,
            "when_label": "unanswered",
            "reason": ["extracted question still open"],
            "evidence": {
                "fact_id": int(fid),
                "source_span": (row.get("source_span") or "")[:200],
                "text": text[:160],
            },
            "fact_id": int(fid),
            "node_type": "fact",
            "node_id": int(fid),
        })

    try:
        from app.services import meta_memory
        qs = meta_memory.scan_open_questions(store, now=now) or []
    except Exception:
        qs = []
    for q in qs[:8]:
        fid = q.get("fact_id")
        if fid is None or int(fid) in seen:
            continue
        out.append({
            "kind": "unanswered_q",
            "label": (q.get("text") or "open question")[:80],
            "p_need": 0.72,
            "when_s": 0.0,
            "when_label": "unanswered",
            "reason": [f"open {q.get('age_days')}d"] if q.get("age_days")
            else ["open question"],
            "evidence": {
                "fact_id": int(fid),
                "text": (q.get("text") or "")[:160],
            },
            "fact_id": int(fid),
            "node_type": "fact",
            "node_id": int(fid),
        })
    return out


def detect_pending_asks(worker=None) -> list[dict]:
    """Pending agent yes/no or approval asks."""
    out: list[dict] = []
    if worker is None:
        try:
            from app.services.agent_bridge import worker as _w
            worker = _w
        except Exception:
            return []
    try:
        peek = getattr(worker, "pending_offer", None)
        prop = peek() if callable(peek) else None
        if prop:
            msg = (prop.get("message") or prop.get("title")
                   or (prop.get("items") or [""])[0] or "pending offer")
            out.append({
                "kind": "pending_ask",
                "label": str(msg).split("\n")[0][:80],
                "p_need": 0.95,
                "when_s": 0.0,
                "when_label": "needs a yes/no",
                "reason": ["agent offer waiting"],
                "evidence": {
                    "kind": prop.get("kind"),
                    "fact_id": prop.get("fact_id"),
                    "text": str(msg)[:200],
                },
                "fact_id": prop.get("fact_id"),
                "node_type": "fact" if prop.get("fact_id") else "agent",
                "node_id": int(prop["fact_id"]) if prop.get("fact_id") else 0,
            })
        _, state = worker.snapshot(10**9)
        if state.get("awaiting") and state.get("waiting_on"):
            q = str(state.get("waiting_on") or "")[:80]
            out.append({
                "kind": "pending_ask",
                "label": q or "approval needed",
                "p_need": 0.96,
                "when_s": 0.0,
                "when_label": "awaiting approval",
                "reason": ["agent ask_human"],
                "evidence": {"text": q},
                "fact_id": None,
                "node_type": "agent",
                "node_id": 0,
            })
    except Exception:
        pass
    return out


def detect(store, *, worker=None, now: float | None = None,
           limit: int = 8) -> list[dict]:
    """Run all detectors; return ranked open loops."""
    if not _enabled():
        return []
    now = float(now if now is not None else time.time())
    loops: list[dict] = []
    loops.extend(detect_waiting_on_them(store, now=now))
    loops.extend(detect_waiting_on_me(store, now=now))
    loops.extend(detect_unanswered_questions(store, now=now))
    loops.extend(detect_pending_asks(worker=worker))
    # Dedup by fact_id (prefer waiting_on_them over me when both — shouldn't)
    seen: set[str] = set()
    out: list[dict] = []
    loops.sort(key=lambda x: -float(x.get("p_need") or 0))
    for lp in loops:
        key = (f"{lp.get('kind')}:{lp.get('fact_id')}"
               if lp.get("fact_id") is not None
               else f"{lp.get('kind')}:{lp.get('label')}")
        if key in seen:
            continue
        seen.add(key)
        lp["id"] = (f"loop:{lp.get('kind')}:{lp.get('fact_id')}"
                    if lp.get("fact_id") is not None
                    else f"loop:{lp.get('kind')}:ask")
        lp["source"] = "open_loop"
        out.append(lp)
        if len(out) >= limit:
            break
    return out


def horizon_items(
    store, *, now: float | None = None, limit: int = 3,
    exclude: set[str] | None = None, worker=None,
) -> list[dict]:
    """Shape open loops as horizon chips for `horizon.predict`."""
    exclude = exclude or set()
    items = []
    for lp in detect(store, worker=worker, now=now, limit=limit + 4):
        nid = lp.get("node_id")
        key = lp.get("id") or (
            f"{lp.get('node_type')}:{nid}" if nid is not None else lp["kind"])
        # Also exclude colliding fact:N calendar chips.
        fact_key = (f"fact:{int(lp['fact_id'])}"
                    if lp.get("fact_id") is not None else None)
        if key in exclude or (fact_key and fact_key in exclude):
            continue
        items.append({
            "node_type": lp.get("node_type") or "fact",
            "node_id": int(nid) if nid is not None else 0,
            "id": fact_key or key,
            "label": lp.get("label"),
            "p_need": round(float(lp.get("p_need") or 0), 3),
            "when_s": round(float(lp.get("when_s") or 0), 1),
            "when_label": lp.get("when_label") or "open loop",
            "reason": list(lp.get("reason") or [])[:4],
            "source": "open_loop",
            "loop_kind": lp.get("kind"),
            "evidence": lp.get("evidence") or {},
            "event_key": None,
            "event_title": None,
            "fact_id": lp.get("fact_id"),
        })
        if len(items) >= limit:
            break
    return items


def mark_surfaced(store, items: list[dict], *, now: float | None = None) -> None:
    """Telemetry impression for open-loop chips. Does not snooze — that is
    dismiss/defer only (precision-first: keep visible until Not now)."""
    del store, now  # API symmetry with horizon.refresh
    for it in items or []:
        if (it.get("source") or "") != "open_loop":
            continue
        fid = it.get("fact_id") or (
            it.get("node_id") if it.get("node_type") == "fact" else None)
        kind = it.get("loop_kind") or "open_loop"
        _record_surface(kind=kind, fact_id=int(fid) if fid is not None else None,
                        text=it.get("label") or "")


def snooze(store, fact_id: int, *, now: float | None = None,
           kind: str = "open_loop", text: str = "") -> bool:
    """User dismissed / Not now — snooze via last_surfaced + dismiss metric."""
    now = float(now if now is not None else time.time())
    ok = False
    try:
        ok = bool(store.touch_commitment_surfaced(int(fact_id), now))
    except Exception:
        ok = False
    _record_dismiss(kind=kind, fact_id=int(fact_id), text=text)
    return ok


def dismiss_rate(*, window: int = 80) -> dict[str, Any]:
    """Precision-first metric: dismisses / surfaces from telemetry trail."""
    try:
        from app.services.cog_telemetry import (
            cog_telemetry, OPEN_LOOP, OPEN_LOOP_DISMISS,
        )
        path = getattr(cog_telemetry, "_path", None)
        if path is None:
            return {"n": 0, "dismiss_rate": None}
        import json
        from pathlib import Path
        p = Path(path)
        if not p.is_file():
            return {"n": 0, "dismiss_rate": None}
        surfaces, dismisses = 0, 0
        for line in p.read_text(encoding="utf-8").splitlines()[-800:]:
            try:
                d = json.loads(line)
            except Exception:
                continue
            m = d.get("metric")
            if m == OPEN_LOOP and d.get("hit"):
                surfaces += 1
            elif m == OPEN_LOOP_DISMISS and d.get("hit"):
                dismisses += 1
        # Cap to recent window of surface events
        if surfaces > window:
            # Approximate: scale dismisses proportionally (trail is append-only).
            pass
        if surfaces <= 0:
            return {"n": 0, "surfaces": 0, "dismisses": dismisses,
                    "dismiss_rate": None}
        return {
            "n": surfaces,
            "surfaces": surfaces,
            "dismisses": dismisses,
            "dismiss_rate": round(min(1.0, dismisses / surfaces), 4),
        }
    except Exception:
        return {"n": 0, "dismiss_rate": None}
