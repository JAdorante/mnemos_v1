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
    from app.services import redact
    flags = sorted(set(redact.scan(text)))
    now = time.time()
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
            "conversation, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (now, grant.get("id"), grant.get("assistant") or "other",
             grant.get("label") or "", project, kind,
             _clip(args.get("title"), 200), text,
             _clip(args.get("source_url"), 1000),
             _clip(args.get("conversation_title"), 200), json.dumps(flags)))
        inbox_id = cur.lastrowid
    return {"ok": True, "status": "pending", "inbox_id": inbox_id,
            "note": "Saved to the user's Sparrow review inbox. It becomes "
                    "memory only if they accept it."}


def _row(r) -> dict[str, Any]:
    d = dict(r)
    d["flags"] = json.loads(d.get("flags") or "[]")
    d["assistant_label"] = ASSISTANT_LABELS.get(d.get("assistant") or "",
                                                "Other")
    return d


def items(status: str = "pending", limit: int = 200) -> list[dict[str, Any]]:
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
    head = []
    if item.get("project"):
        head.append(f"Project: {item['project']}")
    if item.get("title"):
        head.append(item["title"])
    body = item["text"]
    if item.get("source_url"):
        body += f"\nSource: {item['source_url']}"
    return "\n".join(head + [body])


def accept(inbox_id: int, *, text: str | None = None,
           project: str | None = None) -> dict[str, Any]:
    """Pending capture → TEXT event + extraction job. The user may correct
    the text or project first; what they accept is what gets stored."""
    with connect() as c:
        row = c.execute("SELECT * FROM captures WHERE id=? AND "
                        "status='pending'", (int(inbox_id),)).fetchone()
        if not row:
            return {"ok": False, "error": "no such pending item"}
        item = _row(row)
        if text is not None and text.strip():
            item["text"] = text.strip()[:MAX_TEXT]
        if project is not None:
            item["project"] = project.strip()[:120]
        # Keys and passwords never belong in memory, whoever approved it.
        from app.services import redact
        item["text"] = redact.redact_text(item["text"])
        # Claim it before the (slower) event write so a double-click
        # cannot store the same capture twice.
        c.execute("UPDATE captures SET status='accepting', text=?, project=? "
                  "WHERE id=?", (item["text"], item["project"], item["id"]))
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
    return {"ok": True, "event_id": event_id}


def _store_event(item: dict[str, Any]) -> int:
    from app.events import Event, Modality
    from app.services import confidence as _conf
    from app.services.attachments import _index_event
    from app.services.worker import worker
    from app.storage import get_store

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
              "external_source": True, "never_authorizes": True},
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
    return {"ok": True, "event_id": event_id}
