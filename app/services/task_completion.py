"""Tasks and commitments that close themselves (connector capture & task
fulfillment spec, Feature 2) — plus the slot watcher (Feature 3) and the
connector salience gate (Feature 1), all driven from one seam.

A task persists until Sparrow has EVIDENCE it is done or the user declines
it. Capture can propose completion; only evidence or the user can confirm
it. When Sparrow is unsure, it asks.

The detector hooks `Store.insert` (not the event bus): several producers —
typed chat, documents, peer answers, research writeback, connector sync —
insert directly and never publish, and only the insert seam knows the row
id an evidence cite needs. For each new event it:

  1. matches open tasks by counterparty, entities, and text overlap;
  2. classifies the event as completes | progresses | unrelated with
     deterministic rules first (a sent-mail read-back to the counterparty,
     a calendar block with the counterparty that has ended, a phone call
     toast, an audio session with that speaker), LLM tie-break only behind
     QUILL_TASK_COMPLETION_LLM=1;
  3. on `completes`, asks outcome_verify.status_from_evidence: `verified`
     → status done with the event as evidence_id and a chat notice;
     anything weaker → status `uncertain` with ONE yes/no question that
     never auto-closes;
  4. evaluates the event against every open slot (services/slots.py).

Off switch: QUILL_TASK_AUTOCOMPLETE=0. Tests set QUILL_TASK_COMPLETION_SYNC=1
so the hook runs inline instead of on the background thread.
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
from typing import Any

from app.events import Event, Modality
from app.services import outcome_verify as ov
from app.services.commitment_state import OPEN_STATES, TransitionError

UNCERTAIN_HORIZON_S = float(os.environ.get("QUILL_TASK_UNCERTAIN_HORIZON_S",
                                           str(48 * 3600)))
OVERLAP_MIN = float(os.environ.get("QUILL_TASK_MATCH_OVERLAP", "0.5"))

_hook_installed = False
_queue: "queue.Queue[tuple]" = queue.Queue()
_thread: threading.Thread | None = None
_lock = threading.Lock()

_CALL_KIND = re.compile(r"\b(call|phone|ring|meet|meeting|sync|catch\s*up|"
                        r"chat|talk|zoom|huddle)\b", re.I)
_SEND_KIND = re.compile(r"\b(send|email|e-mail|mail|share|forward|deliver|"
                        r"ship|submit)\b", re.I)
_CALL_TOAST_DONE = re.compile(r"\b(call ended|ended|duration|\d+\s*(?:min|m)\b"
                              r"|\d+:\d\d)", re.I)
_CALL_TOAST = re.compile(r"\b(call|facetime)\b", re.I)
_MISSED = re.compile(r"\b(missed|declined|voicemail)\b", re.I)

# Verbs are case-insensitive (scoped flag); the counterparty must still be
# a capitalised name, so "email Dana Whitfield about the deck" binds
# "Dana Whitfield" and "call me maybe" binds nothing.
_TASK_INTENT = re.compile(
    r"^\s*(?i:(?:please|remind me to|i need to|i have to|i should|i want to)\s+)?"
    r"(?P<verb>(?i:have a (?:call|meeting|chat|sync)|call|phone|meet with|meet|"
    r"catch up with|talk to|talk with|sync with|email|e-mail|send|text))\s+"
    r"(?i:with\s+)?(?P<who>[A-Z][\w'.-]*(?:\s+[A-Z][\w'.-]*){0,3})"
    r"(?P<rest>.*)$")
_SLOT_INTENT = re.compile(
    r"^\s*(?:(?:please|can you|could you)\s+)?(?:get me|find me|find|fetch|"
    r"track down|look for|look out for|keep an eye out for|keep an eye on|"
    r"watch for|watch out for|let me know when you (?:see|get|find))\s+"
    r"(?P<need>.{3,})$", re.I)
_KEEP_EYE = re.compile(r"\b(keep an eye out|keep an eye on|watch for|"
                       r"let me know (?:when|if) (?:it|you|that))\b", re.I)


def enabled() -> bool:
    return os.environ.get("QUILL_TASK_AUTOCOMPLETE", "1") not in (
        "0", "false", "False")


def llm_enabled() -> bool:
    return os.environ.get("QUILL_TASK_COMPLETION_LLM", "0") in ("1", "true", "True")


def sync_mode() -> bool:
    return os.environ.get("QUILL_TASK_COMPLETION_SYNC", "0") in ("1", "true", "True")


# --- wiring ------------------------------------------------------------------
def attach() -> bool:
    """Register the insert hook (idempotent) and start the worker thread."""
    global _hook_installed, _thread
    from app.storage import add_insert_hook
    with _lock:
        if not _hook_installed:
            add_insert_hook(_on_insert)
            _hook_installed = True
        if not sync_mode() and (_thread is None or not _thread.is_alive()):
            _thread = threading.Thread(target=_drain, name="task-completion",
                                       daemon=True)
            _thread.start()
    return True


def detach() -> None:
    global _hook_installed
    from app.storage import remove_insert_hook
    with _lock:
        remove_insert_hook(_on_insert)
        _hook_installed = False


def _on_insert(store, event_id: int, event: Event) -> None:
    if sync_mode():
        process(store, event_id, event)
    else:
        _queue.put((store, event_id, event))


def _drain() -> None:
    while True:
        try:
            store, eid, ev = _queue.get()
        except Exception:
            return
        try:
            process(store, eid, ev)
        except Exception as exc:
            print(f"[task_completion] event {eid} skipped ({exc}).")


def process(store, event_id: int, event: Event | dict, *,
            now: float | None = None) -> dict[str, Any]:
    """Evaluate one persisted event: task completion, slot fills, salience."""
    now = float(now if now is not None else time.time())
    out: dict[str, Any] = {"event_id": int(event_id), "completion": [],
                           "fills": [], "salience": None}
    if enabled():
        try:
            out["completion"] = detect(store, event_id, event, now=now)
        except Exception as exc:
            print(f"[task_completion] detect skipped ({exc}).")
    try:
        from app.services import slots
        if slots.enabled():
            out["fills"] = slots.evaluate_event(store, event_id, event, now=now)
    except Exception as exc:
        print(f"[task_completion] slot watch skipped ({exc}).")
    try:
        from app.services import salience
        out["salience"] = salience.evaluate(
            store, event_id, event, now=now,
            fills=out["fills"], completions=out["completion"])
    except Exception as exc:
        print(f"[task_completion] salience skipped ({exc}).")
    return out


# --- event views ---------------------------------------------------------------
def _ev_field(event: Event | dict, key: str, default=None):
    if isinstance(event, dict):
        return event.get(key, default)
    return getattr(event, key, default)


def _ev_meta(event: Event | dict) -> dict:
    meta = _ev_field(event, "meta") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _ev_list(event: Event | dict, key: str) -> list[str]:
    raw = _ev_field(event, key) or []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = []
    return [str(x) for x in (raw or [])]


def _ev_modality(event: Event | dict) -> str:
    m = _ev_field(event, "modality")
    return m.value if isinstance(m, Modality) else str(m or "")


def _ev_text(event: Event | dict) -> str:
    meta = _ev_meta(event)
    parts = [str(meta.get("title") or ""), str(_ev_field(event, "summary") or ""),
             str(_ev_field(event, "raw") or "")[:4000]]
    people = _ev_list(event, "people")
    for key in ("attendees", "organizer", "from", "to", "cc"):
        val = meta.get(key)
        if isinstance(val, list):
            for a in val:
                if isinstance(a, dict):
                    parts.append(" ".join(str(v) for v in a.values() if v))
                else:
                    parts.append(str(a))
        elif isinstance(val, dict):
            parts.append(" ".join(str(v) for v in val.values() if v))
        elif val:
            parts.append(str(val))
    parts.extend(people)
    return "\n".join(p for p in parts if p)


def _tokens(text: str) -> set[str]:
    from app.services.slots import need_tokens
    return need_tokens(text)


def _overlap(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta)


# --- task views ------------------------------------------------------------------
def task_kind(task: dict) -> str:
    kind = (task.get("task_kind") or "").strip().lower()
    if kind:
        return kind
    text = task.get("text") or ""
    if _CALL_KIND.search(text):
        return "call"
    if _SEND_KIND.search(text):
        return "send"
    return "generic"


def counterparty_of(task: dict, store=None) -> str:
    cp = (task.get("counterparty_name") or "").strip()
    if cp:
        return cp
    self_names = _self_names(store)
    for key in ("to_person", "from_person", "owner"):
        name = (task.get(key) or "").strip()
        if name and name.lower() not in self_names:
            return name
    return ""


def _self_names(store) -> set[str]:
    try:
        from app.services.open_loops import _self_names as sn
        return sn(store) if store is not None else {"me", "i", "myself"}
    except Exception:
        return {"me", "i", "myself"}


def _name_in(name: str, text: str, people: list[str]) -> bool:
    n = (name or "").strip().lower()
    if not n:
        return False
    if any(n == p.lower() or n in p.lower() or p.lower() in n for p in people
           if p):
        return True
    first = n.split()[0]
    low = text.lower()
    return bool(re.search(r"\b" + re.escape(n) + r"\b", low)
                or (len(first) >= 3 and re.search(r"\b" + re.escape(first) + r"\b", low)))


def open_tasks(store) -> list[dict]:
    """Tasks the detector may close: open work that is not a slot. An
    `uncertain` task stays a candidate so stronger evidence can still close
    it (a weak repeat never asks twice — see apply)."""
    rows = []
    for row in store.list_tasks(("open", "uncertain")):
        state = (row.get("commitment_state") or "active").lower()
        if state not in OPEN_STATES or state == "awaiting_data":
            continue
        if (row.get("task_kind") or "") == "slot":
            continue
        rows.append(row)
    return rows


def candidate_tasks(store, event: Event | dict) -> list[tuple[dict, dict]]:
    """Open tasks this event could be about, with the match reasons."""
    text = _ev_text(event)
    people = _ev_list(event, "people")
    out = []
    for task in open_tasks(store):
        why: dict[str, Any] = {}
        cp = counterparty_of(task, store)
        if cp and _name_in(cp, text, people):
            why["counterparty"] = cp
        ov_ = _overlap(task.get("text") or "", text)
        if ov_ >= OVERLAP_MIN:
            why["overlap"] = round(ov_, 3)
        if why:
            out.append((task, why))
    return out


# --- classification ----------------------------------------------------------------
def _fmt_clock(ts: float | None) -> str:
    try:
        return time.strftime("%-I:%M %p", time.localtime(float(ts))).lower()
    except Exception:
        return "?"


def _parse_ts(val) -> float | None:
    if val in (None, ""):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        pass
    try:
        import datetime as dt
        s = str(val).strip().replace("Z", "+00:00")
        d = dt.datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def classify(task: dict, event: Event | dict, *, store=None,
             now: float | None = None) -> tuple[str, ov.Evidence | None, dict]:
    """(verdict, evidence, detail) for one task × event.

    verdict ∈ completes | progresses | unrelated. `completes` with weak
    evidence is what yields an `uncertain` task downstream — the verdict
    says what the event claims, the evidence says how much to trust it.
    """
    now = float(now if now is not None else time.time())
    kind = task_kind(task)
    cp = counterparty_of(task, store)
    text = _ev_text(event)
    people = _ev_list(event, "people")
    meta = _ev_meta(event)
    source = str(_ev_field(event, "source") or "").lower()
    modality = _ev_modality(event)
    cp_hit = bool(cp) and _name_in(cp, text, people)
    detail: dict[str, Any] = {"kind": kind, "counterparty": cp, "cp_hit": cp_hit,
                              "source": source}

    # 1. Sent-mail read-back to the counterparty.
    sent_like = (meta.get("direction") == "sent" or bool(meta.get("from_self"))
                 or ov_looks_sent(text))
    if kind == "send" and cp_hit and sent_like:
        src = ov.SRC_MAIL if ".mail" in source or source.startswith("exhaust") \
            else ov.SRC_SENT
        ev = ov.Evidence(True, src, f"sent to {cp} (read-back from {source})",
                         status=ov.VERIFIED, meta={"event_source": source})
        return "completes", ev, detail

    # 2. Calendar block with the counterparty that has ended.
    if "calendar" in source and cp_hit and kind in ("call", "generic"):
        start = _parse_ts(meta.get("start"))
        end = _parse_ts(meta.get("end"))
        title = str(meta.get("summary") or meta.get("title") or "").strip()
        if end is not None and end <= now:
            mins = int(round((end - (start or end)) / 60)) if start else None
            note = (f"{mins}-min {title or 'call'} on calendar, ended "
                    f"{_fmt_clock(end)}" if mins else
                    f"{title or 'calendar block'} ended {_fmt_clock(end)}")
            ev = ov.Evidence(True, ov.SRC_CAL, note, status=ov.VERIFIED,
                             meta={"start": start, "end": end, "title": title})
            return "completes", ev, {**detail, "minutes": mins}
        return "progresses", None, {**detail, "scheduled": True}

    # 3. Phone Link call toast.
    if modality == Modality.NOTIFICATION.value and cp_hit and kind in (
            "call", "generic") and _CALL_TOAST.search(text):
        if _MISSED.search(text):
            return "progresses", None, {**detail, "missed": True}
        if _CALL_TOAST_DONE.search(text):
            ev = ov.Evidence(True, "phone_call_toast",
                             f"call toast: {text.strip()[:80]}",
                             status=ov.VERIFIED)
            return "completes", ev, detail
        ev = ov.Evidence(True, "phone_call_toast",
                         f"call toast without duration: {text.strip()[:80]}",
                         status=ov.OUTCOME_UNCERTAIN)
        return "completes", ev, detail

    # 4. Audio session with that speaker — heard them, but nothing proves
    #    it was THE call. Weak on purpose: it asks.
    if modality == Modality.AUDIO.value and cp_hit and kind == "call":
        ev = ov.Evidence(True, "audio_session",
                         f"heard {cp} in a captured session",
                         status=ov.OUTCOME_UNCERTAIN)
        return "completes", ev, detail

    # 5. Text overlap only → progress, or an LLM tie-break when allowed.
    ov_ = _overlap(task.get("text") or "", text)
    if ov_ >= OVERLAP_MIN or cp_hit:
        if llm_enabled():
            llm = _llm_classify(task, text)
            if llm == "completes":
                return "completes", ov.llm_only_evidence(
                    True, "local model judged the task complete"), {
                    **detail, "overlap": ov_, "llm": llm}
            if llm == "unrelated":
                return "unrelated", None, {**detail, "overlap": ov_, "llm": llm}
        return "progresses", None, {**detail, "overlap": round(ov_, 3)}
    return "unrelated", None, detail


def ov_looks_sent(text: str) -> bool:
    try:
        from app.services.commitment_complete import looks_like_sent_toast
        return looks_like_sent_toast(text)
    except Exception:
        return False


_LLM_SCHEMA = {"type": "object",
               "properties": {"verdict": {"type": "string",
                                          "enum": ["completes", "progresses",
                                                   "unrelated"]}},
               "required": ["verdict"]}


def _llm_classify(task: dict, text: str) -> str | None:
    try:
        from app.services.model_router import router
        res = router.complete_json(
            "task_completion",
            system=("Does the observation show the task is DONE (completes), "
                    "moving forward but not done (progresses), or about "
                    "something else (unrelated)? Be strict: only 'completes' "
                    "when the observation itself shows the task finished."),
            messages=[{"role": "user", "content":
                       f"TASK: {task.get('text')}\n\nOBSERVATION:\n{text[:1500]}"}],
            schema=_LLM_SCHEMA, max_tokens=32)
        v = str((res or {}).get("verdict") or "").strip().lower()
        return v if v in ("completes", "progresses", "unrelated") else None
    except Exception as exc:
        print(f"[task_completion] llm tie-break skipped ({exc}).")
        return None


# --- applying a verdict --------------------------------------------------------------
def detect(store, event_id: int, event: Event | dict, *,
           now: float | None = None) -> list[dict]:
    now = float(now if now is not None else time.time())
    results = []
    for task, why in candidate_tasks(store, event):
        verdict, evidence, detail = classify(task, event, store=store, now=now)
        res = {"fact_id": int(task["fact_id"]), "verdict": verdict,
               "match": why, "detail": detail,
               "evidence": evidence.as_dict() if evidence else None}
        if verdict == "completes" and evidence is not None:
            try:
                res.update(apply(store, task, evidence, int(event_id), now=now))
            except TransitionError as exc:
                res["error"] = str(exc)
        results.append(res)
    return results


def apply(store, task: dict, evidence: ov.Evidence, event_id: int, *,
          now: float | None = None) -> dict:
    """Evidence, not opinion: verified → done; anything weaker → uncertain
    with one question for the user."""
    now = float(now if now is not None else time.time())
    fid = int(task["fact_id"])
    status = ov.status_from_evidence(evidence)
    cite = {"source": evidence.source, "note": evidence.note[:240],
            "status": status, "evidence_event_id": int(event_id)}
    if status == ov.VERIFIED:
        out = store.transition_commitment(
            fid, "completed", actor="capture", reason="evidence_verified",
            evidence=cite, evidence_id=int(event_id), ts=now)
        text = task.get("text") or ""
        _notify(f"Marked done: {text} (evidence: {evidence.note})",
                stream={"type": "task.completed", "task_id": fid,
                        "event_id": int(event_id), "evidence": evidence.note,
                        "actions": [{"label": "Not done", "reply": "reopen"}]})
        return {"applied": "completed", **out}
    question = _question_for(task, evidence)
    state = (task.get("commitment_state") or "active").lower()
    if state == "uncertain":
        return {"applied": "already_uncertain", "question": question}
    out = store.transition_commitment(
        fid, "uncertain", actor="capture", reason="evidence_weak",
        evidence=cite, evidence_id=int(event_id), question=question, ts=now)
    ask_user(store, task, question, event_id)
    return {"applied": "uncertain", "question": question, **out}


def _question_for(task: dict, evidence: ov.Evidence) -> str:
    kind = task_kind(task)
    cp = task.get("counterparty_name") or task.get("to_person") or ""
    what = (f"the call with {cp}" if kind == "call" and cp
            else (task.get("text") or "this"))
    saw = evidence.note or "some evidence"
    return f"Did {what} happen? I saw {saw} but nothing that proves it."


def ask_user(store, task: dict, question: str, event_id: int | None) -> bool:
    """Surface ONE yes/no in the chat stream (pending_ask open-loop type)."""
    try:
        from app.services.agent_bridge import worker
        return bool(worker.propose_task_question({
            "fact_id": int(task["fact_id"]), "text": task.get("text") or "",
            "question": question, "event_id": event_id}))
    except Exception as exc:
        print(f"[task_completion] question via chat skipped ({exc}).")
        _notify(question, stream={"type": "task.question",
                                  "task_id": int(task["fact_id"]),
                                  "actions": [{"label": "Yes", "reply": "yes"},
                                              {"label": "Not yet", "reply": "no"}]})
        return False


def answer(store, fact_id: int, done: bool, *, actor: str = "user",
           now: float | None = None) -> dict:
    """Resolve an `uncertain` question: yes → done (user confirmed, with the
    weak evidence kept as the cite); not yet → back to open."""
    now = float(now if now is not None else time.time())
    row = store.get_task(int(fact_id))
    if not row:
        return {"ok": False, "error": "no such task"}
    prior = {}
    raw = row.get("completion_evidence_json")
    if isinstance(raw, str) and raw.strip():
        try:
            prior = json.loads(raw)
        except Exception:
            prior = {}
    if done:
        evidence = {**prior, "source": "user_confirm",
                    "note": f"user confirmed: {row.get('question') or ''}"[:240]}
        out = store.transition_commitment(
            int(fact_id), "completed", actor=actor, reason="user_confirm",
            evidence=evidence, ts=now)
        _notify(f"Marked done: {row.get('text') or ''}",
                stream={"type": "task.completed", "task_id": int(fact_id)})
        return out
    out = store.transition_commitment(
        int(fact_id), "active", actor=actor, reason="user_not_yet", ts=now)
    return out


# --- user-created tasks --------------------------------------------------------------
def parse_task_intent(text: str) -> dict | None:
    """"Have a call with Marc" → {"kind": "call", "counterparty": "Marc"};
    "get me the Boston quote" / "keep an eye out for the Acme invoice" →
    {"kind": "slot", "need": ...}. None when the text is not a task."""
    t = (text or "").strip()
    if not t or len(t) > 240:
        return None
    m = _SLOT_INTENT.match(t)
    if m:
        need = m.group("need").strip().rstrip("?.! ")
        if need and not re.match(r"^(?:me|us|it|that|this)\b", need, re.I):
            return {"kind": "slot", "need": need,
                    "watch": bool(_KEEP_EYE.search(t))}
    m = _TASK_INTENT.match(t)
    if m:
        verb = m.group("verb").lower()
        who = m.group("who").strip().rstrip("?.!,")
        if who.lower() in ("i", "me", "you", "us", "the", "a", "an"):
            return None
        kind = "send" if re.search(r"email|e-mail|send|text", verb) else "call"
        return {"kind": kind, "counterparty": who,
                "text": t.rstrip("?.! ")}
    return None


def create_user_task(store, text: str, *, counterparty: str | None = None,
                     kind: str | None = None, source_event_id: int | None = None,
                     now: float | None = None) -> int:
    """Mint a task the user typed: counterparty=X, kind=call, state active."""
    now = float(now if now is not None else time.time())
    to_pid = None
    if counterparty:
        try:
            to_pid = store.find_person_exact(counterparty)
        except Exception:
            to_pid = None
    fid = store.add_commitment(
        (text or "").strip(), source_event_id=source_event_id,
        extracted_at=now, state="detected", task_kind=kind,
        counterparty_name=counterparty, to_person_id=to_pid,
        requester_kind="user", allow_declined_thread=True)
    if fid:
        store.transition_commitment(fid, "active", actor="user",
                                    reason="user_created", ts=now)
    return fid


# --- horizon -------------------------------------------------------------------------
def horizon_items(store, *, now: float | None = None) -> list[dict]:
    """Uncertain tasks unanswered for 48 h: the question, on the strip."""
    now = float(now if now is not None else time.time())
    out = []
    for row in store.list_tasks(("uncertain",)):
        asked = float(row.get("updated_at") or row.get("extracted_at") or now)
        if now - asked < UNCERTAIN_HORIZON_S:
            continue
        q = row.get("question") or f"Is this done? {row.get('text') or ''}"
        out.append({
            "kind": "pending_ask",
            "label": q[:80],
            "p_need": 0.9,
            "when_s": 0.0,
            "when_label": "needs a yes/no",
            "reason": ["unanswered for 48h", "never auto-closes"],
            "evidence": {"kind": "task_question", "fact_id": int(row["fact_id"]),
                         "text": q[:200]},
            "fact_id": int(row["fact_id"]),
            "node_type": "fact",
            "node_id": int(row["fact_id"]),
            "actions": [{"label": "Yes", "reply": "yes"},
                        {"label": "Not yet", "reply": "not yet"}],
        })
    return out


# --- helpers -------------------------------------------------------------------------
def _notify(text: str, *, stream: dict | None = None) -> None:
    try:
        from app.services.agent_bridge import worker
        worker._emit("result", text, stream=stream)
    except Exception:
        pass
