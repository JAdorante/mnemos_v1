"""Cheap chat intercepts as suggestions, not hijacks.

Deterministic parsers still run on the request path — they are free and useful —
but they no longer consume the turn. They return chips the answer can show
("Make this a task?"). The calendar LLM parse runs only when the user accepts
the calendar chip, on a background thread.
"""
from __future__ import annotations

import threading
from typing import Any


def collect(text: str) -> list[dict[str, Any]]:
    """Synchronous, LLM-free candidates for one user message."""
    display = (text or "").strip()
    if not display:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(kind: str, label: str, action: str, payload: dict) -> None:
        if kind in seen:
            return
        seen.add(kind)
        out.append({
            "kind": kind,
            "label": label,
            "action": action,
            "payload": payload,
        })

    try:
        from app.services import calendar_intent
        if calendar_intent.looks_like_calendar_add(display):
            _add("calendar_add", "Add to calendar", "calendar.propose",
                 {"text": display})
    except Exception as exc:
        print(f"[chat_suggestions] calendar gate skipped ({exc}).")

    peer_owned = False
    try:
        from app.services import peer_channel
        team = peer_channel.parse_team_ask(display)
        tell = None if team else peer_channel.parse_team_tell(display)
        recall = None if (team or tell) else peer_channel.parse_peer_recall(display)
        know = None if (team or tell or recall) else peer_channel.parse_what_we_know(display)
        peer_owned = bool(team or tell)
        if know:
            _add("topic_knowledge", "What we know about this", "peer.topic",
                 {"topic": know})
        if recall:
            _add("peer_recall",
                 f"Ask {recall.get('peer_name') or 'them'}'s Sparrow",
                 "peer.recall", dict(recall))
        if tell:
            _add("peer_tell",
                 f"Send update to {tell.get('peer_name') or 'them'}",
                 "peer.tell", dict(tell))
        elif peer_channel.looks_like_team_tell(display) and not team:
            _add("peer_tell_unpaired", "Needs a paired teammate",
                 "peer.tell_unpaired", {"text": display})
        if team:
            if team.get("fanout"):
                name = team.get("team_name") or "the team"
                _add("peer_team_ask", f"Ask {name}", "peer.team_ask", dict(team))
            else:
                who = team.get("peer_name") or "them"
                verb = ("Hand off to" if team.get("kind") == "handoff"
                        else f"Ask {who}'s Sparrow")
                _add("peer_ask", verb, "peer.ask", dict(team))
    except Exception as exc:
        print(f"[chat_suggestions] peer gate skipped ({exc}).")
        peer_owned = False

    if not peer_owned:
        try:
            from app.services import task_completion as _tc
            intent = _tc.parse_task_intent(display)
            if intent:
                if intent.get("kind") == "slot":
                    _add("slot_create", "Watch for this", "slot.create",
                         dict(intent))
                else:
                    _add("task_create", "Make this a task", "task.create",
                         {**dict(intent), "text": intent.get("text") or display})
        except Exception as exc:
            print(f"[chat_suggestions] task gate skipped ({exc}).")

    try:
        from app.services.triggers import authoring as trigger_authoring
        if trigger_authoring.looks_like_trigger_request(display):
            _add("trigger_draft", "Make this a trigger", "trigger.author",
                 {"text": display})
    except Exception as exc:
        print(f"[chat_suggestions] trigger gate skipped ({exc}).")

    return out


def stream_actions(suggestions: list[dict] | None) -> list[dict]:
    """Shape for the chat result stream (buttons under the answer)."""
    actions = []
    for s in suggestions or []:
        if not s.get("action") or not s.get("label"):
            continue
        actions.append({
            "label": s["label"],
            "action": s["action"],
            "payload": s.get("payload") or {},
            "kind": s.get("kind"),
        })
    return actions


def apply(action: str, payload: dict | None = None, *, worker=None) -> dict[str, Any]:
    """Run the side effect a suggestion chip asked for."""
    payload = dict(payload or {})
    action = (action or "").strip()
    if worker is None:
        from app.services.agent_bridge import worker as _w
        worker = _w

    if action == "calendar.propose":
        return _apply_calendar(payload, worker)
    if action == "task.create":
        return _apply_task(payload, worker)
    if action == "slot.create":
        return _apply_slot(payload, worker)
    if action == "peer.topic":
        return _apply_peer_topic(payload, worker)
    if action == "peer.recall":
        return _apply_peer_recall(payload, worker)
    if action == "peer.tell":
        return _apply_peer_tell(payload, worker)
    if action == "peer.tell_unpaired":
        worker._emit(
            "result",
            "I can only push updates to a paired Sparrow. Pair them on "
            "Team (/peer), or say `email …` / `text …` to draft outbound.")
        return {"ok": True, "routed": "peer_tell_unpaired"}
    if action == "peer.ask":
        return _apply_peer_ask(payload, worker)
    if action == "peer.team_ask":
        return _apply_peer_team_ask(payload, worker)
    if action == "trigger.author":
        return _apply_trigger(payload, worker)
    return {"ok": False, "error": f"unknown suggestion action: {action}"}


def _apply_calendar(payload: dict, worker) -> dict[str, Any]:
    text = str(payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "text required"}
    worker._emit("progress", "Parsing that into a calendar event…")

    def _run() -> None:
        try:
            from app.services import calendar_intent, icloud_account
            event = calendar_intent.parse(text)
            if not event:
                worker._emit(
                    "result",
                    "I couldn't turn that into a calendar event — try naming "
                    "a day and time.")
                return
            if not icloud_account.status()["connected"]:
                worker._emit(
                    "result",
                    "To add calendar events I need your iCloud connected — "
                    "do it once on the Phone page (/phone), then ask me again.")
                return
            worker.propose_calendar(event)
        except Exception as exc:
            worker._emit("error", f"Calendar suggestion failed: {exc}")

    threading.Thread(target=_run, name="chat-cal-suggest", daemon=True).start()
    return {"ok": True, "routed": "calendar_propose_async"}


def _apply_task(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import task_completion as _tc
        from app.services.memory import memory
        store = memory._ensure_store()
        text = str(payload.get("text") or "").strip()
        fid = _tc.create_user_task(
            store, text,
            counterparty=payload.get("counterparty"),
            kind=payload.get("kind"))
        if fid:
            worker._emit(
                "result",
                f"Added to your tasks: {text} "
                f"(with {payload.get('counterparty')}). I'll mark it done "
                "when I see evidence, and ask if I'm unsure.",
                stream={"type": "task.created", "task_id": fid})
            return {"ok": True, "routed": "task_created", "task_id": fid}
        worker._emit(
            "result", "You declined that one earlier — not re-adding it.")
        return {"ok": True, "routed": "task_declined"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_slot(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import slots as _slots
        from app.services.memory import memory
        if not _slots.enabled():
            return {"ok": False, "error": "slots disabled"}
        store = memory._ensure_store()
        need = _slots.normalize_need(payload.get("need") or "")
        hits = _slots.search_assist(store, need, allow_connectors=False,
                                    timeout_s=3)
        if hits and hits[0].get("event_id") is not None:
            h = hits[0]
            worker._emit(
                "result",
                f"I already have something on the {need}:\n\n"
                f"“{(h.get('text') or '')[:400]}”\n\n"
                f"Source: /memory?event={h['event_id']}. Want me "
                "to keep watching for a newer one? Reply 'yes'.")
        worker.propose_slot_create({
            "need": need, "requester": {"kind": "user", "id": None},
            "requester_name": "you"})
        return {"ok": True, "routed": "slot_offer"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_peer_topic(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import peer_channel
        topic = payload.get("topic")
        if topic is None:
            topic = payload
        worker._emit("result", peer_channel.synthesize_topic_knowledge(topic))
        return {"ok": True, "routed": "topic_knowledge"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_peer_recall(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import peer_channel
        hits = peer_channel.find_peer_answers(
            payload["peer_id"], payload["topic"])
        usable = [h for h in hits if h.get("usable")]
        if usable:
            best = usable[-1]
            worker._emit(
                "result",
                f"From {payload['peer_name']}'s Sparrow "
                f"(asked “{best['question'][:120]}”):\n\n{best['answer']}")
            return {"ok": True, "routed": "peer_recall"}
        q = payload["topic"]
        if not str(q).lower().startswith((
                "what", "who", "when", "where", "why", "how", "do ", "does ",
                "is ", "are ", "can ", "could ")):
            q = f"what do you know about {q}?"
        peer_channel.chat_ask_async(payload["peer_id"], q, "question")
        worker._emit(
            "result",
            f"I don't have a usable answer from {payload['peer_name']}'s "
            f"Sparrow about “{str(payload['topic'])[:80]}” yet — asking them now…")
        return {"ok": True, "routed": "peer_recall_reask"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_peer_tell(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import peer_channel
        peer_channel.chat_tell_async(
            payload["peer_id"], payload["peer_name"], payload["topic"])
        worker._emit(
            "result",
            f"Composing an update for {payload['peer_name']} — I'll show "
            "it here for your OK before anything is sent.")
        return {"ok": True, "routed": "peer_tell"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_peer_ask(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import peer_channel
        peer_channel.chat_ask_async(
            payload["peer_id"], payload["question"],
            payload.get("kind", "question"))
        verb = ("Handing off to" if payload.get("kind") == "handoff"
                else "Asking")
        worker._emit("result", f"{verb} {payload['peer_name']}'s Sparrow…")
        return {"ok": True, "routed": "peer_ask"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_peer_team_ask(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services import team_layer
        if payload.get("unknown"):
            worker._emit(
                "result",
                f"No team named {payload.get('team_name')!r} yet — "
                "create it on the Team page (/peer).")
            return {"ok": True, "routed": "peer_team_unknown"}
        if not payload.get("peer_ids"):
            worker._emit(
                "result",
                f"The {payload.get('team_name') or 'that'} team has no "
                "paired members yet — add them on /peer.")
            return {"ok": True, "routed": "peer_team_empty"}
        team_layer.chat_team_ask_async(
            payload["team_slug"], payload["question"],
            payload.get("kind", "question"))
        n = len(payload["peer_ids"])
        verb = ("Handing off to" if payload.get("kind") == "handoff"
                else "Asking")
        worker._emit(
            "result",
            f"{verb} the {payload.get('team_name')} team "
            f"({n} teammate{'s' if n != 1 else ''})…")
        return {"ok": True, "routed": "peer_team_ask"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _apply_trigger(payload: dict, worker) -> dict[str, Any]:
    try:
        from app.services.triggers import authoring as trigger_authoring
        text = str(payload.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "text required"}
        worker._emit(
            "result",
            "Designing that trigger — I'll show you a draft to approve "
            "in a moment…")
        trigger_authoring.author_async(text)
        return {"ok": True, "routed": "trigger_draft"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
