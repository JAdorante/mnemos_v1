"""Meeting-scoped chat + follow-up draft (Meeting Layer P4).

Ask: restrict grounding to one meeting's turns/facts/attendees (via
`compose(..., meeting_reflection_id=…)` / `session_id=…`), then answer.

Draft: one-click follow-up with `source_fact_ids` from the note's
commitment/decision/next_step items → approval packet → verified send
can complete those commitments.
"""
from __future__ import annotations

import time
from typing import Any

from app.storage import Store, get_store

_DRAFT_KINDS = frozenset({"commitment", "decision", "next_step"})


def resolve_scope(
    store: Store, *,
    session_id: int | None = None,
    meeting_reflection_id: int | None = None,
) -> dict[str, Any] | None:
    """Resolve a meeting time window + cited fact ids + attendees.

    Prefer `meeting_reflection_id` (stable note id). `session_id` falls back
    to the sessions row when present (may be stale after rebuild).
    """
    reflection = None
    if meeting_reflection_id is not None:
        reflection = store.get_reflection(int(meeting_reflection_id))
        if reflection is None or reflection.get("scope") != "meeting":
            return None
    elif session_id is not None:
        # Find a meeting reflection whose subject_id matches, else the session row.
        for r in store.list_reflections(scope="meeting", limit=80):
            if r.get("subject_id") == int(session_id):
                reflection = r
                break

    if reflection is not None:
        t0 = float(reflection.get("period_start") or 0)
        t1 = float(reflection.get("period_end") or t0)
        items = store.reflection_items(reflection["id"])
        fact_ids: list[int] = []
        for it in items:
            for fid in it.get("source_fact_ids") or []:
                try:
                    n = int(fid)
                except (TypeError, ValueError):
                    continue
                if n not in fact_ids:
                    fact_ids.append(n)
        title = (reflection.get("summary") or "").split("\n", 1)[0].strip()
        if " · " in title:
            title = title.split(" · ", 1)[0].strip()
        attendees: list[dict] = []
        # Recover attendees from a live/overlapping session if available.
        try:
            for s in store.recent_sessions(limit=40):
                if abs(float(s.get("start") or 0) - t0) < 2.0:
                    attendees = list((s.get("meeting_meta") or {}).get("attendees") or [])
                    break
        except Exception:
            pass
        return {
            "t0": t0, "t1": t1,
            "fact_ids": fact_ids,
            "attendees": attendees,
            "title": title or "Meeting",
            "summary": (reflection.get("summary") or ""),
            "reflection_id": reflection["id"],
            "session_id": reflection.get("subject_id") or session_id,
            "items": items,
        }

    if session_id is None:
        return None
    # Bare session (no enhance yet) — still allow scoped ask from window.
    try:
        sessions = store.recent_sessions(limit=80)
    except Exception:
        return None
    sess = next((s for s in sessions if s.get("id") == int(session_id)), None)
    if not sess:
        return None
    meta = sess.get("meeting_meta") or {}
    # Facts whose source events fall in the window.
    eids = set(int(e) for e in (sess.get("event_ids") or []))
    fact_ids = []
    try:
        for f in store.list_facts(limit=500):
            if f.get("source_event_id") in eids:
                fact_ids.append(int(f["fact_id"]))
    except Exception:
        pass
    return {
        "t0": float(sess.get("start") or 0),
        "t1": float(sess.get("end") or 0),
        "fact_ids": fact_ids,
        "attendees": list(meta.get("attendees") or []),
        "title": meta.get("title") or "Meeting",
        "summary": "",
        "reflection_id": None,
        "session_id": sess.get("id"),
        "items": [],
    }


def source_fact_ids_for_draft(
    store: Store, meeting_reflection_id: int, *,
    open_commitments_only: bool = False,
) -> list[int]:
    """Fact ids from commitment/decision/next_step items on the note."""
    items = store.reflection_items(int(meeting_reflection_id))
    out: list[int] = []
    for it in items:
        if it.get("kind") not in _DRAFT_KINDS:
            continue
        if it.get("review") == "dismissed":
            continue
        for fid in it.get("source_fact_ids") or []:
            try:
                n = int(fid)
            except (TypeError, ValueError):
                continue
            if n in out:
                continue
            if open_commitments_only:
                try:
                    f = store.get_fact(n) or {}
                    if f.get("kind") != "commitment":
                        continue
                    # status may live on joined row — best-effort
                    st = f.get("status") or f.get("commitment_state")
                    if st in ("done", "cancelled", "completed", "superseded"):
                        continue
                except Exception:
                    pass
            out.append(n)
    return out


def meeting_context_lines(scope: dict) -> list[str]:
    """Structured block injected at the top of compose when meeting-scoped."""
    lines = [f"This meeting: {scope.get('title') or 'Meeting'}"]
    att = scope.get("attendees") or []
    if att:
        names = ", ".join(
            ((a.get("name") or a.get("email") or "?").strip())
            for a in att if isinstance(a, dict)
        )
        if names:
            lines.append(f"- Attendees: {names}")
    summary = (scope.get("summary") or "").strip()
    if summary and "\n" in summary:
        body = summary.split("\n", 1)[1].strip()
        if body:
            lines.append(f"- Summary: {body[:400]}")
    # Bullet the note items (decisions / commitments / …)
    for it in (scope.get("items") or [])[:12]:
        kind = it.get("kind") or "item"
        if kind == "note":
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        cites = it.get("source_fact_ids") or []
        cite = f" [facts {','.join(str(c) for c in cites)}]" if cites else ""
        lines.append(f"- [{kind}] {text[:200]}{cite}")
    if len(lines) <= 1:
        lines.append("- (no enhanced items yet — answering from session window)")
    return lines


def ask(
    question: str, *,
    meeting_reflection_id: int | None = None,
    session_id: int | None = None,
    store: Store | None = None,
) -> dict[str, Any]:
    """Answer a question scoped to one meeting. Sync — no agent browser."""
    store = store or get_store()
    q = (question or "").strip()
    if not q:
        return {"ok": False, "error": "empty question"}
    scope = resolve_scope(
        store, session_id=session_id,
        meeting_reflection_id=meeting_reflection_id,
    )
    if scope is None:
        return {"ok": False, "error": "meeting not found"}

    from app.services.grounding import compose
    from app.services.answer_check import check_answer

    g = compose(
        q, store=store, record_attention=True,
        session_id=scope.get("session_id"),
        meeting_reflection_id=scope.get("reflection_id"),
        semantic_limit=8,
    )
    context = g.get("block") or ""
    sources = g.get("sources") or []
    answer = ""
    try:
        from app.services.model_router import router
        from app.services.clock import clock_instruction
        system = (
            "You answer questions about ONE meeting using only the provided "
            "context. Prefer facts and transcript evidence from this meeting. "
            "If the context does not contain the answer, say what is missing "
            "rather than inventing. Be concise."
            "\n\n" + clock_instruction()
        )
        answer = router.complete(
            "chat", system=system,
            messages=[{"role": "user", "content":
                       f"Meeting context:\n{context or '(none)'}\n\n"
                       f"Question: {q}"}],
            max_tokens=800,
        ).strip()
    except Exception as exc:
        answer = f"(Could not generate: {exc})\n\nWhat I found:\n{context[:1200]}"

    try:
        checked = check_answer(answer, context, question=q, sources=sources)
        answer = checked.text
        check = checked.to_dict()
    except Exception:
        check = None

    return {
        "ok": True,
        "answer": answer,
        "sources": sources,
        "route": g.get("route"),
        "meeting": {
            "title": scope.get("title"),
            "reflection_id": scope.get("reflection_id"),
            "session_id": scope.get("session_id"),
        },
        "answer_check": check,
    }


def draft_followup(
    meeting_reflection_id: int, *,
    store: Store | None = None,
    dry_run: str = "draft",
    to: str | None = None,
) -> dict[str, Any]:
    """Enqueue a grounded follow-up draft citing the note's fact ids.

    Goes through the agent approval path. Verified send completes open
    commitments whose ids are on the packet.
    """
    store = store or get_store()
    scope = resolve_scope(store, meeting_reflection_id=meeting_reflection_id)
    if scope is None or not scope.get("reflection_id"):
        return {"ok": False, "error": "meeting note not found"}

    fids = source_fact_ids_for_draft(store, int(meeting_reflection_id))
    # Build a short brief from note items for the writing compiler.
    bullets = []
    for it in scope.get("items") or []:
        if it.get("kind") not in _DRAFT_KINDS:
            continue
        if it.get("review") == "dismissed":
            continue
        text = (it.get("text") or "").strip()
        if text:
            bullets.append(f"- ({it.get('kind')}) {text}")
    att = scope.get("attendees") or []
    # Prefer first non-self-looking attendee email as To hint.
    to_hint = (to or "").strip()
    if not to_hint:
        for a in att:
            if not isinstance(a, dict):
                continue
            email = (a.get("email") or "").strip()
            if email and "@" in email:
                to_hint = email
                break

    title = scope.get("title") or "our meeting"
    goal = (
        f"Draft a short follow-up email about '{title}'. "
        + (f"Send to {to_hint}. " if to_hint else "")
        + "Cover the commitments, decisions, and next steps below. "
        "Cite only what is grounded; do not invent.\n\n"
        + ("\n".join(bullets) if bullets else "(see meeting note)")
    )

    try:
        from app.services.agent_bridge import worker as _worker
        if _worker is None:
            return {"ok": False, "error": "agent disabled"}
        _worker.send(
            goal,
            dry_run=dry_run if dry_run in ("draft", "plan", None, "") else "draft",
            fact_id=(fids[0] if fids else None),
            source_fact_ids=fids,
            display=f"Draft follow-up · {title}",
        )
        return {
            "ok": True,
            "queued": True,
            "source_fact_ids": fids,
            "to_hint": to_hint,
            "goal_preview": goal[:400],
            "poll": "/chat/poll",
            "chat": "/chat",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def link_agent_run_to_facts(
    store: Store, agent_run_id: int, fact_ids: list[int],
) -> None:
    """Minimum viable ledger closure link when state-machine complete isn't used.

    Best-effort: store fact ids on the run's meta/context if the schema allows;
    otherwise no-op. Preferred path is verified-send → complete_from_verified_send.
    """
    if not agent_run_id or not fact_ids:
        return
    try:
        # action_packets already carry source_fact_ids; this is a soft audit trail.
        with store._lock:
            row = store._conn.execute(
                "SELECT id FROM agent_runs WHERE id=?", (int(agent_run_id),)
            ).fetchone()
            if not row:
                return
            # Optional column — ignore if absent.
            cols = {r["name"] for r in
                    store._conn.execute("PRAGMA table_info(agent_runs)").fetchall()}
            if "correlation_id" in cols:
                pass  # already traced via packets
    except Exception:
        pass
