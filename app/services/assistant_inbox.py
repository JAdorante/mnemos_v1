"""Review inbox for what cloud assistants save into Sparrow.

save_to_sparrow (services/remote_mcp.py) only PROPOSES: a capture lands
here as `pending` and nothing reaches memory until the user accepts it on
/assistants. Accepting stores one TEXT event with source
``assistant.<chatgpt|claude|grok|…>`` and queues the same extractor typed
chat uses, so tasks / claims / people / project links come out of it the
normal way. The event is observed-tier (an assistant wrote it, not the
user) and carries ``never_authorizes`` — a saved note can inform Sparrow,
never instruct it. Rejecting wipes the text and keeps only the audit row;
removing an accepted note erases its event and everything extracted from it.

Every capture is routed to a project (services/projects.py): an existing one
if it fits, a new one if it clearly is one, otherwise Miscellaneous. Accepting
files it there. Sharing is a separate choice per note: it publishes the note
as a record in that project's org scope (records/note_claims.py), where
teammates can read it.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

from app.services.remote_mcp import ASSISTANT_LABELS, connect

MAX_TEXT = 8000
KINDS = ("decision", "requirement", "task", "fact", "idea", "question",
         "reference", "note")
_DETAILS = {"decided_by": 200, "owner": 200, "due": 40, "rationale": 2000}


def max_pending() -> int:
    return int(os.environ.get("QUILL_ASSISTANT_INBOX_MAX", "500"))


def _clip(v: Any, n: int) -> str:
    return str(v or "").strip()[:n]


def propose(grant: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    text = _clip(args.get("text"), MAX_TEXT + 1)
    if not text:
        return {"ok": False, "error": "text required"}
    if len(text) > MAX_TEXT:
        return {"ok": False, "error": f"text over {MAX_TEXT} characters — "
                                      "split it into separate saves"}
    project = _clip(args.get("project"), 120)
    kind = _clip(args.get("kind"), 20).lower()
    if kind not in KINDS:
        kind = "note"
    from app.services import projects, redact
    flags = sorted(set(redact.scan(text)))
    now = time.time()
    routed = projects.route_name(project)
    details = {k: _clip(args.get(k), n) for k, n in _DETAILS.items()
               if _clip(args.get(k), n)}
    with connect() as c:
        dup = c.execute(
            "SELECT id FROM captures WHERE status='pending' AND text=? "
            "AND project=?", (text, project)).fetchone()
        if dup:
            return {"ok": True, "status": "pending", "inbox_id": dup[0],
                    "duplicate": True}
        n = c.execute("SELECT COUNT(*) FROM captures WHERE "
                      "status='pending'").fetchone()[0]
        if n >= max_pending():
            return {"ok": False, "error": "the user's Sparrow inbox is full; "
                                          "ask them to review it"}
        cur = c.execute(
            "INSERT INTO captures (created_at, grant_id, assistant, "
            "client_label, project, kind, title, text, source_url, "
            "conversation, flags, route_action, route_reason, details) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now, grant.get("id"), grant.get("assistant") or "other",
             grant.get("label") or "", routed["project"], kind,
             _clip(args.get("title"), 200), text,
             _clip(args.get("source_url"), 1000),
             _clip(args.get("conversation_title"), 200), json.dumps(flags),
             routed["action"], routed["reason"], json.dumps(details)))
        inbox_id = cur.lastrowid
    if not routed["project"]:
        _queue_route(inbox_id)
    return {"ok": True, "status": "pending", "inbox_id": inbox_id,
            "project": routed["project"] or None,
            "project_action": routed["action"],
            "note": "Saved to the user's Sparrow review inbox. It becomes "
                    "memory only if they accept it."}


def _queue_route(inbox_id: int) -> None:
    try:
        from app.services.worker import worker
        worker.enqueue("assistant_route", payload={"inbox_id": int(inbox_id)})
    except Exception as exc:
        print(f"[assistant_inbox] route job not queued ({exc}).")


def run_route_job(payload: dict) -> None:
    """A note saved with no project: ask the local model where it goes. Only a
    still-pending, still-unrouted note changes, so this never overrides a
    reviewer."""
    from app.services import projects
    inbox_id = int((payload or {}).get("inbox_id") or 0)
    with connect() as c:
        row = c.execute("SELECT title, text FROM captures WHERE id=? AND "
                        "status='pending' AND project=''", (inbox_id,)).fetchone()
    if not row:
        return
    routed = projects.classify(row["text"], title=row["title"] or "")
    with connect() as c:
        c.execute("UPDATE captures SET project=?, route_action=?, route_reason=?"
                  " WHERE id=? AND status='pending' AND project=''",
                  (routed["project"], routed["action"], routed["reason"],
                   inbox_id))


def _row(r) -> dict[str, Any]:
    d = dict(r)
    d["flags"] = json.loads(d.get("flags") or "[]")
    d["details"] = json.loads(d.get("details") or "{}")
    d["assistant_label"] = ASSISTANT_LABELS.get(d.get("assistant") or "",
                                                "Other")
    return d


def items(status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
    with connect() as c:
        rows = c.execute(
            "SELECT * FROM captures WHERE status=? ORDER BY created_at DESC "
            "LIMIT ?", (status, max(1, min(int(limit), 500)))).fetchall()
    queued = [r["id"] for r in rows if r["share_state"] == "queued"]
    if queued:
        for inbox_id in queued:
            try:
                sync_share_state(inbox_id)
            except Exception as exc:
                print(f"[assistant_inbox] share sync skipped ({exc}).")
        with connect() as c:
            rows = c.execute(
                "SELECT * FROM captures WHERE status=? ORDER BY created_at DESC "
                "LIMIT ?", (status, max(1, min(int(limit), 500)))).fetchall()
    return [_row(r) for r in rows]


def counts() -> dict[str, int]:
    with connect() as c:
        rows = c.execute("SELECT status, COUNT(*) FROM captures "
                         "GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}


def _event_text(item: dict[str, Any]) -> str:
    from app.services import projects
    head = []
    # Miscellaneous is a shelf, not a subject. Naming it here would teach the
    # extractor there is a project called that.
    if item.get("project") and item["project"] != projects.MISC:
        head.append(f"Project: {item['project']}")
    if (item.get("details") or {}).get("status") == "suggested":
        head.append("Status: suggested by an AI assistant, not decided")
    if item.get("title"):
        head.append(item["title"])
    body = item["text"]
    if item.get("source_url"):
        body += f"\nSource: {item['source_url']}"
    return "\n".join(head + [body])


def _review_details(item: dict[str, Any], decided_by: str | None,
                    status: str | None) -> dict[str, Any]:
    """The reviewer says whether a person decided this or the assistant only
    suggested it. Unsaid, a decision with no named decider stays a suggestion,
    so nobody reads an AI's idea as what the team decided."""
    details = dict(item.get("details") or {})
    if decided_by is not None:
        details["decided_by"] = decided_by.strip()[:200]
    if not details.get("decided_by"):
        details.pop("decided_by", None)
    if item.get("kind") == "decision":
        if status in ("decided", "suggested"):
            details["status"] = status
        else:
            details["status"] = ("decided" if details.get("decided_by")
                                 else "suggested")
        if details["status"] == "suggested":
            details.pop("decided_by", None)
    return details


def accept(inbox_id: int, *, text: str | None = None,
           project: str | None = None, share: bool = False,
           replaces: str | None = None, decided_by: str | None = None,
           decision_status: str | None = None,
           source: str | None = None) -> dict[str, Any]:
    """Pending capture → TEXT event + extraction job, filed under a project.
    The user may correct the text or project first; what they accept is what
    gets stored. `share` then publishes it to that project; a share that fails
    leaves the note accepted and says why, and can be retried."""
    from app.services import projects
    with connect() as c:
        row = c.execute("SELECT * FROM captures WHERE id=? AND "
                        "status='pending'", (int(inbox_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "no such pending item"}
        item = _row(row)
        proposed = item["text"]
        if text is not None and text.strip():
            item["text"] = text.strip()[:MAX_TEXT]
        # What the reviewer left in the box decides, and an empty box means
        # the note fits no project.
        chosen = item["project"] if project is None else project
        routed = projects.route_name(chosen)
        item["project"] = routed["project"] or projects.MISC
        item["details"] = _review_details(item, decided_by, decision_status)
        # Keys and passwords never belong in memory, whoever approved it.
        from app.services import redact
        item["text"] = redact.redact_text(item["text"])
        # Claim it before the (slower) event write so a double-click
        # cannot store the same capture twice.
        c.execute("UPDATE captures SET status='accepting', text=?, project=?, "
                  "details=? WHERE id=?",
                  (item["text"], item["project"], json.dumps(item["details"]),
                   item["id"]))
    try:
        event_id = _store_event(item)
    except Exception as exc:
        with connect() as c:
            c.execute("UPDATE captures SET status='pending' WHERE id=?",
                      (item["id"],))
        return {"ok": False, "error": f"could not store: {exc}"}
    with connect() as c:
        c.execute("UPDATE captures SET status='accepted', decided_at=?, "
                  "event_id=? WHERE id=?", (time.time(), event_id, item["id"]))
    edited = item["text"] != redact.redact_text(proposed)
    from app.services import decisions
    decisions.record(
        surface="assistant.inbox", subject=f"inbox:{item['id']}",
        verdict="edited" if edited else "approved",
        shown={"assistant": item.get("assistant"), "kind": item.get("kind"),
               "project": item.get("project"),
               "suggested_project": row["project"]},
        options=["accept", "edit", "reject"],
        edit={"text": item["text"]} if edited else None,
        meta={"event_id": event_id},
        learn=dict(task_type="assistant.save", input_text=proposed,
                   local_output=proposed, final_target=item["text"],
                   source_refs={"inbox_id": item["id"],
                                "event_id": event_id}))
    out: dict[str, Any] = {"ok": True, "event_id": event_id,
                           "project": item["project"]}
    if share:
        out["shared"] = share_note(item["id"], replaces=replaces, source=source)
    return out


def _note_value(item: dict[str, Any]) -> dict[str, Any]:
    d = item.get("details") or {}
    value: dict[str, Any] = {"text": item["text"], "note_kind": item.get("kind") or "note",
             "drafted_by": (item.get("client_label")
                            if item.get("assistant_label") == "Other" else
                            item.get("assistant_label")) or "an AI assistant"}
    for key, src, n in (("title", item.get("title"), 200),
                        ("source_url", item.get("source_url"), 1000),
                        ("status", d.get("status"), 20),
                        ("decided_by", d.get("decided_by"), 200),
                        ("owner", d.get("owner"), 200),
                        ("due", d.get("due"), 40),
                        ("rationale", d.get("rationale"), 2000)):
        if src:
            value[key] = str(src)[:n]
    return value


def share_note(inbox_id: int, *, replaces: str | None = None,
               source: str | None = None) -> dict[str, Any]:
    """Publish an accepted note to its project. Only a live human session may
    do this; the check is the records layer's, made from the request."""
    from app.services import projects
    from app.services.records import note_claims, org_client, promotion
    from app.storage import get_store
    with connect() as c:
        row = c.execute("SELECT * FROM captures WHERE id=? AND "
                        "status='accepted'", (int(inbox_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "no such accepted item"}
    item = _row(row)
    if item.get("share_state") in ("recorded", "queued", "forwarded"):
        return {"ok": True, "state": item["share_state"], "already": True,
                "record_id": item.get("record_id")}

    def failed(code: str, message: str) -> dict[str, Any]:
        with connect() as c:
            c.execute("UPDATE captures SET share_state='failed', share_error=? "
                      "WHERE id=?", (message[:300], item["id"]))
        return {"ok": False, "code": code, "error": message}

    # Checked before anything happens: an unverified caller must not even
    # create the project scope.
    if source != promotion.LIVE_SESSION:
        return failed("not_live_session", _share_message("not_live_session", ""))
    try:
        scope = projects.ensure_shared(item["project"])
        prior = None
        if replaces:
            prior = next((r for r in map(projects.record_view,
                                         org_client.scope_records(scope["id"]))
                          if r["record_id"] == replaces), None)
            if prior is None:
                return failed("bad_replaces",
                              "That record is not in this project any more.")
        out = note_claims.publish(
            get_store(), event_id=int(item["event_id"]),
            project=item["project"], scope_id=scope["id"],
            value=_note_value(item), replaces=prior,
            source=source)
    except projects.ShareError as exc:
        return failed(exc.code, exc.message)
    except promotion.PromotionError as exc:
        return failed(exc.code, _share_message(exc.code, exc.message))
    except org_client.OrgUnavailable:
        return failed("org_unavailable",
                      "The org service is not reachable right now.")
    except org_client.OrgRefused as exc:
        return failed(exc.code, exc.detail or exc.code)
    with connect() as c:
        c.execute("UPDATE captures SET share_state=?, share_scope=?, "
                  "share_error=NULL, claim_id=?, record_id=? WHERE id=?",
                  (out["state"], scope["id"], out.get("claim_id"),
                   out.get("record_id"), item["id"]))
    return out


def _share_message(code: str, detail: str) -> str:
    return {"not_live_session": "Sharing needs you signed in on this page.",
            "personal_only": f"This note must stay personal: {detail}.",
            }.get(code, detail or code)


def similar_records(inbox_id: int, project: str | None = None) -> dict[str, Any]:
    """Shared records this pending note might replace, for the reviewer. The
    org being away just means no suggestions."""
    from app.services import projects
    with connect() as c:
        row = c.execute("SELECT title, text, project FROM captures WHERE id=?",
                        (int(inbox_id),)).fetchone()
    if not row:
        return {"ok": False, "error": "no such item"}
    name = project if project is not None else row["project"]
    if not name or projects.is_misc(name):
        return {"ok": True, "records": []}
    try:
        recs = projects.team_records(name, query=f"{row['title'] or ''} {row['text']}"[:500],
                                     limit=3)
    except Exception as exc:
        print(f"[assistant_inbox] similar records skipped ({exc}).")
        recs = []
    return {"ok": True, "records": [
        r for r in recs if r.get("predicate") == "noted"]}


def _store_event(item: dict[str, Any]) -> int:
    from app.events import Event, Modality
    from app.services import confidence as _conf
    from app.services.attachments import _index_event
    from app.services import projects
    from app.services.worker import worker
    from app.storage import get_store

    projects.ensure_filed(item.get("project") or "", store=get_store())
    assistant = item.get("assistant") or "other"
    text = _event_text(item)
    ev = Event(
        time=time.time(), modality=Modality.TEXT, raw=text,
        summary=f"[{item['assistant_label']}] {text[:120]}",
        source=f"assistant.{assistant}",
        meta={"section": "assistant", "assistant": assistant,
              "client_label": item.get("client_label") or None,
              "project": item.get("project") or None,
              "kind": item.get("kind"), "inbox_id": item["id"],
              "conversation": item.get("conversation") or None,
              "source_url": item.get("source_url") or None,
              "external_source": True, "never_authorizes": True,
              "reviewed_keep": True},
    )
    _conf.attach(ev, _conf.OBSERVED)
    event_id = get_store().insert(ev)
    _index_event(event_id, ev)
    worker.enqueue("assistant_capture",
                   payload={"event_id": event_id, "text": text})
    return event_id


def reject(inbox_id: int) -> dict[str, Any]:
    with connect() as c:
        cur = c.execute(
            "UPDATE captures SET status='rejected', decided_at=?, text='', "
            "title='', source_url='', conversation='' WHERE id=? AND "
            "status='pending'", (time.time(), int(inbox_id)))
        if not cur.rowcount:
            return {"ok": False, "error": "no such pending item"}
    # The text was just wiped on purpose, so neither the decision nor a
    # learning pair keeps it.
    from app.services import decisions
    decisions.record(surface="assistant.inbox", subject=f"inbox:{inbox_id}",
                     verdict="rejected", options=["accept", "edit", "reject"])
    return {"ok": True}


def remove(inbox_id: int) -> dict[str, Any]:
    """Undo an accept: erase the stored event (and, through erase_event,
    the facts, graph evidence and commitments drawn from it), then wipe the
    text like a reject."""
    with connect() as c:
        row = c.execute("SELECT event_id FROM captures WHERE id=? AND "
                        "status='accepted'", (int(inbox_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "no such accepted item"}
    event_id = row[0]
    warning = _retract_shared(int(inbox_id))
    if event_id is not None:
        from app.storage import get_store
        try:
            res = get_store().erase_event(int(event_id))
        except Exception as exc:
            return {"ok": False, "error": f"could not remove: {exc}"}
        # Already forgotten elsewhere (memory delete) is fine — finish the undo.
        if not (res or {}).get("ok", True) and res.get("reason") != "missing":
            return {"ok": False, "error": "could not remove the stored note"}
    with connect() as c:
        c.execute("UPDATE captures SET status='removed', decided_at=?, "
                  "text='', title='', source_url='', conversation='' "
                  "WHERE id=?", (time.time(), int(inbox_id)))
    # The accepted text is gone from memory; the pair learned from it goes
    # too.
    from app.services import decisions
    decisions.undo(surface="assistant.inbox", subject=f"inbox:{inbox_id}",
                   meta={"event_id": event_id})
    out: dict[str, Any] = {"ok": True, "event_id": event_id}
    if warning:
        out["warning"] = warning
    return out


def _retract_shared(inbox_id: int) -> str | None:
    """Removing a shared note takes it off the project too. The note's own
    copy goes either way, so failing here warns rather than blocks."""
    from app.services import projects
    from app.services.records import org_client
    with connect() as c:
        row = c.execute("SELECT share_state, record_id, share_scope, claim_id "
                        "FROM captures WHERE id=?", (inbox_id,)).fetchone()
    if not row or not row["share_state"]:
        return None
    state = sync_share_state(inbox_id) or row["share_state"]
    if state == "failed":
        return None
    with connect() as c:
        rid = c.execute("SELECT record_id FROM captures WHERE id=?",
                        (inbox_id,)).fetchone()[0]
    if not rid:
        return ("This note is still waiting on approval or delivery, so it "
                "may yet appear in the project. Remove it there if it does.")
    try:
        org_client.retract_record(rid)
    except Exception as exc:
        print(f"[assistant_inbox] retract skipped ({exc}).")
        return "The shared copy is still in the project. Remove it on /projects."
    with connect() as c:
        c.execute("UPDATE captures SET share_state='retracted' WHERE id=?",
                  (inbox_id,))
    return None


def sync_share_state(inbox_id: int) -> str | None:
    """A queued share delivers later from the outbox. Catch up from the claim,
    and find the record the delivered version belongs to."""
    from app.services import projects
    from app.services.records import node_store, org_client
    from app.storage import get_store
    with connect() as c:
        row = c.execute("SELECT share_state, claim_id, share_scope FROM captures "
                        "WHERE id=?", (inbox_id,)).fetchone()
    if not row or row["share_state"] != "queued" or not row["claim_id"]:
        return row["share_state"] if row else None
    claim = node_store.get_claim(get_store(), row["claim_id"])
    if not claim or claim.get("status") not in ("recorded", "failed"):
        return "queued"
    state, rid = claim["status"], None
    if state == "recorded":
        try:
            for r in org_client.scope_records(row["share_scope"]):
                if r.get("version_id") == claim.get("record_ref"):
                    rid = r.get("record_id")
                    break
        except Exception as exc:
            print(f"[assistant_inbox] record lookup skipped ({exc}).")
    with connect() as c:
        c.execute("UPDATE captures SET share_state=?, record_id=? WHERE id=?",
                  (state, rid, inbox_id))
    return state
