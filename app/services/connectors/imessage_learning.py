"""Texts into this user's own model: what they received, paired with what they
wrote back. Their real replies are the one training target that needs no cloud
model and no rating, because the user wrote it.

Stored human_confirmed=False: nobody judged the reply as a model answer. That
keeps it out of exemplars, fleet sharing and the promotion holdout, and only
scripts/distill_curate.load_text_replies reads it back, for this seat's adapter.
"""
from __future__ import annotations

import os

from app.services import learning_store

TASK = "imessage.reply"
REPLY_WINDOW_S = 6 * 3600
MAX_CONTEXT = 5
SOURCE = "imessage.message"
QUOTE_TAG = f"[{SOURCE}]"


def enabled() -> bool:
    return os.environ.get("QUILL_IMESSAGE_LEARN", "1") not in ("0", "false", "False")


def _words(body: str) -> str:
    # land_item bodies read "To Sam Lee: words"; the target is just the words.
    return (body or "").partition(": ")[2].strip()


def _context(item: dict, store) -> list[str]:
    """The received texts this reply answers, oldest first. Empty when the user
    spoke last, since a second bubble in a row isn't a reply to anything."""
    ts = float(item.get("ts") or 0)
    thread = item.get("thread_id")
    rows = store.recent_events(source_substr=SOURCE, since=ts - REPLY_WINDOW_S,
                               limit=200)
    out: list[str] = []
    for row in rows:
        meta = row.get("meta") or {}
        if meta.get("thread_id") != thread or meta.get("external_id") == item.get("external_id"):
            continue
        if float(row.get("time") or 0) > ts:
            continue
        if meta.get("direction") != "received" or len(out) >= MAX_CONTEXT:
            break
        out.append(str(row.get("raw") or ""))
    return list(reversed(out))


def learn_from(item: dict, *, store) -> str | None:
    """Record a reply pair if `item` is the user answering someone one-to-one."""
    if not enabled() or not item.get("from_self"):
        return None
    meta = item.get("meta") or {}
    target = _words(str(item.get("body") or ""))
    if meta.get("group") or not item.get("thread_id") or not target:
        return None
    context = _context(item, store)
    if not context:
        return None
    return learning_store.record(
        task_type=TASK, input_text="\n".join(context), verdict="accepted",
        verdict_source=TASK, final_target=target, human_confirmed=False,
        privacy_floor="sensitive", created_at=item.get("ts"),
        source_refs={"imessage": True, "external_id": item.get("external_id")},
        store=store)


def forget(*, store) -> int:
    """Delete the training data made from texts. Returns how many rows went."""
    from app.services.escalate_log import escalate_log
    gone = 0
    for pair in store.list_learning_pairs(limit=100000):
        refs = pair.get("source_refs") or {}
        if pair.get("task_type") == TASK or refs.get("imessage") or refs.get("quotes_imessage"):
            if learning_store.delete(pair["id"], store=store):
                gone += 1
    return gone + escalate_log.purge_containing(QUOTE_TAG)
