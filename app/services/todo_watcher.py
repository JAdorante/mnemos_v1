"""Proactive to-do watcher — the hear/see -> offer -> act trigger.

Subscribes to the event bus. When vision (webcam *or* desktop screen) shows a
REAL user to-do list, this:

  1. Upserts clean items into open tasks (deduped).
  2. Offers (in chat) to run the *actionable* ones — debounced.

Ignores vinceo.ai's own UI, VLM schema leakage, anticipation text, and other
garbage that the model sometimes stuffs into `items`. Disable with
QUILL_TODO_WATCH=0 (or QUILL_AGENT=0).
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time

from app.events import Modality

_recent_offer: dict[str, float] = {}     # items-hash -> last offered time
_lock = threading.Lock()
_COOLDOWN_S = 300                   # don't re-offer the same list within 5 min

# Numbered / bulleted lines in OCR when the VLM omitted `items`.
_ITEM_LINE = re.compile(
    r"^\s*(?:(?:\d+[\.)])|[-*•▪◦])\s+(.+\S)\s*$"
)
_TODO_MARKERS = (
    "to do list", "todo list", "to-do list", "todos:", "to dos:",
    "action items", "action item",
)
# Meeting notes / agendas are memory, not a batch of agent jobs.
_NOTES_TITLE = re.compile(
    r"\bnotes?\b|\bmeeting\b|\bminutes\b|\bagenda\b|\bdiscussion\b|"
    r"\bstandup\b|\bretro(spective)?\b|\bbrainstorm\b",
    re.I,
)
_TODO_TITLE = re.compile(
    r"\bto[- ]?dos?\b|\baction items?\b|\bchecklist\b|\btasks?\b",
    re.I,
)
# Section headings common in notes docs (not actionable by themselves).
_SECTION_WORDS = frozenset({
    "overview", "background", "context", "summary", "agenda", "goal", "goals",
    "objective", "objectives", "status", "access level", "notes", "note",
    "attendees", "participants", "decisions", "next steps", "parking lot",
    "appendix", "intro", "introduction", "scope", "purpose",
})
# A line should look like something a person would ask an agent to *do*.
_ACTION_CUE = re.compile(
    r"\b(email|e-mail|text|sms|call|phone|send|open|find|search|buy|book|"
    r"schedule|write|draft|reply|message|remind|check|confirm|follow\s*up|"
    r"look\s*(?:up|into|for)|go\s+to|visit|order|pay|submit|create|make|build|update|"
    r"fix|add|remove|delete|share|post|upload|download|meet|pick\s*up|"
    r"bring|ask|tell|notify|research|review|prepare|ship|cancel|"
    r"reschedule|forward)\b|"
    r"https?://|[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}",
    re.I,
)
# Don't treat vinceo.ai's own chrome / memory UI as a user to-do page.
_SELF_WINDOW = re.compile(
    r"vinceo(?:\.ai)?|mnemos|memory console|exec\.ai|/onboarding|nexus_v1\s*-\s*cursor",
    re.I,
)
# Real list surfaces we trust on desktop capture.
_TRUSTED_TODO_WINDOW = re.compile(
    r"notepad|sticky notes|onenote|todoist|microsoft to.?do|"
    r"things\b|reminders|apple notes|google keep|workflowy|trello",
    re.I,
)
# VLM schema keys + common leakage the model dumps into items[].
_SCHEMA_JUNK = frozenset({
    "description", "ocr_text", "people_count", "objects", "object", "objecets",
    "scene_type", "sceen_type", "screen_type", "content_type", "title", "items",
    "item_confidences", "confidence", "mixed", "none", "notes", "todo_list",
    "questions", "diagram", "table", "form", "code", "computer", "remembered",
    "remembered:", "checklist", "checklists",
})
_JUNK_ID = re.compile(
    r"^(people_\d+|fact:\w+|objecets?|[-:]?\d{5,}|:)$", re.I
)
_ANTICIPATION = re.compile(
    r"after working (with|in)|often switch to|recent transitions|"
    r"pattern match|% pattern|/8 recent|/5 recent",
    re.I,
)
_META_PROSE = re.compile(
    r"pre-empted|system-generated|self-generated|one platform|"
    r"not necessarily remembered|reply 'yes'|web-doable",
    re.I,
)
# Hallucinated / schema-ish list titles — never a user's notepad.
_BAD_TITLE = re.compile(
    r"user-scoped|activity ownership|my contacts|people_?\d*|"
    r"serving flask|exec_webapp|debug mode|memory tag|fact:task|"
    r"content_type|screen_type|checklist for vinceo",
    re.I,
)


def _enabled() -> bool:
    return (os.environ.get("QUILL_TODO_WATCH", "1") not in ("0", "false", "False")
            and os.environ.get("QUILL_AGENT") not in ("0", "false", "False"))


def _hash(items: list[str]) -> str:
    return hashlib.sha1("\n".join(items).strip().lower().encode()).hexdigest()


def _is_self_ui(ev) -> bool:
    """True when the frame is vinceo.ai/Cursor-on-vinceo.ai — never a user notepad list."""
    meta = ev.meta or {}
    win = str(meta.get("window") or "")
    if _SELF_WINDOW.search(win):
        return True
    # Webcam frames describing the vinceo.ai UI via OCR/summary.
    vision = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    blob = " ".join([
        str(vision.get("title") or ""),
        str(vision.get("ocr_text") or "")[:400],
        str(ev.summary or "")[:400],
    ]).lower()
    if "vinceo" in blob and ("checklist for vinceo" in blob
                             or "memory console" in blob
                             or "reply 'yes'" in blob):
        return True
    return False


def _is_log_surface(window: str, title: str, ocr: str, summary: str) -> bool:
    """True for terminals / server logs / CLI mistaken for checklists."""
    from app.services.surface_filters import is_log_or_cli_surface
    if is_log_or_cli_surface(window, title, ocr, summary):
        return True
    if _BAD_TITLE.search(title or ""):
        return True
    return False


def _trusted_desktop_todo(window: str, title: str, ocr: str) -> bool:
    """Desktop frames must look like a real list app / To Do header."""
    if _TRUSTED_TODO_WINDOW.search(window or ""):
        return True
    if _TRUSTED_TODO_WINDOW.search(title or ""):
        return True
    head = f"{title}\n{(ocr or '')[:240]}".lower()
    return any(m in head for m in _TODO_MARKERS)


def _is_junk_item(text: str) -> bool:
    t = (text or "").strip()
    if len(t) < 5 or len(t) > 280:
        return True
    low = t.lower().rstrip(":").strip()
    if low in _SCHEMA_JUNK:
        return True
    if _JUNK_ID.match(t) or _JUNK_ID.match(low):
        return True
    if _ANTICIPATION.search(t) or _META_PROSE.search(t):
        return True
    from app.services.surface_filters import CLI_LINE, LOG_LINE
    if LOG_LINE.search(t) or CLI_LINE.search(t):
        return True
    # snake_case / schema-ish tokens with no spaces
    if " " not in t and ("_" in t or t.isdigit()):
        return True
    if t.startswith("{") or t.startswith("'type'") or '"type"' in t[:20]:
        return True
    if re.fullmatch(r"[\d\s\-.:eE+]+", t):
        return True
    # Trailing orphan label
    if t.endswith(":") and len(t) < 24:
        return True
    return False


def _is_notes_document(title: str, ocr: str = "") -> bool:
    """True for meeting-notes / agenda docs — not a to-do list to auto-run."""
    t = (title or "").strip()
    if not t:
        return False
    if _TODO_TITLE.search(t):
        return False
    if _NOTES_TITLE.search(t):
        return True
    head = (ocr or "")[:200].lower()
    # Untitled notes that open with Overview:/Agenda: are still notes.
    if re.search(r"^(overview|agenda|background|attendees)\s*:", head, re.M):
        return True
    return False


def _is_actionable(text: str) -> bool:
    """Worth offering to the agent — a real task phrase, not a notes heading."""
    if _is_junk_item(text):
        return False
    body = (text or "").strip()
    # "Overview: This document captures…" → evaluate the part after the label.
    m = re.match(r"^([^:]{2,48}):\s*(.*)$", body)
    if m:
        label = m.group(1).strip().lower()
        rest = m.group(2).strip()
        if label in _SECTION_WORDS or label.rstrip("s") in _SECTION_WORDS:
            if not rest:
                return False
            body = rest
    words = [w for w in re.findall(r"[A-Za-z']+", body)]
    if len(words) < 2:
        return False
    # Descriptive notes / status lines without an action cue are not agent jobs.
    if not _ACTION_CUE.search(body):
        return False
    return True


def _clean_items(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in items:
        t = (raw or "").strip()
        if not t or _is_junk_item(t):
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    return out


def _items_from_ocr(ocr: str) -> list[str]:
    out: list[str] = []
    for line in (ocr or "").splitlines():
        m = _ITEM_LINE.match(line)
        if not m:
            continue
        text = m.group(1).strip()
        if text.lower().rstrip(":") in ("to do list", "todo list", "to-do list",
                                         "todos", "action items", "checklist"):
            continue
        if not _is_junk_item(text):
            out.append(text)
    return out


def _looks_like_todo(title: str, ocr: str, summary: str) -> bool:
    blob = f"{title}\n{ocr}\n{summary}".lower()
    # Self-referential / UI copy is not a user list.
    if "checklist for vinceo" in blob or "reply 'yes'" in blob:
        return False
    if any(m in blob for m in _TODO_MARKERS):
        return True
    t = (title or "").strip().lower().rstrip(":")
    return t in ("to do", "todo", "to-do", "todos", "action items", "to do list")


def _todo_payload(ev) -> dict | None:
    """Pull title/items from a VISION event; reject UI/schema garbage."""
    if _is_self_ui(ev):
        return None

    meta = ev.meta or {}
    vision = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    ctype = (meta.get("content_type") or vision.get("content_type") or "none")
    title = (vision.get("title") or "").strip()
    raw_items = [str(x).strip() for x in (meta.get("items") or vision.get("items") or [])
                 if str(x).strip()]
    confidences = meta.get("item_confidences")
    ocr = (vision.get("ocr_text") or ev.raw or "") or ""
    summary = ev.summary or ""
    window = str(meta.get("window") or "")
    source = getattr(ev, "source", "") or ""

    if _is_log_surface(window, title, ocr, summary):
        return None

    if not raw_items:
        raw_items = _items_from_ocr(ocr)

    items = _clean_items(raw_items)
    if not items:
        return None

    # If most of what the model returned was junk, don't trust the residue.
    if raw_items and len(items) / max(1, len(raw_items)) < 0.5:
        return None

    actionable = [it for it in items if _is_actionable(it)]
    if not actionable:
        return None

    explicit = ctype == "todo_list"
    inferred = (not explicit) and _looks_like_todo(title, ocr, summary)
    notes_doc = _is_notes_document(title, ocr)
    # Explicit todo_list from the model still needs to look sane; if the title
    # is clearly vinceo.ai meta, drop it.
    if "vinceo" in title.lower() and "checklist" in title.lower():
        return None
    if _BAD_TITLE.search(title):
        return None
    # Meeting notes: never treat the VLM's "todo_list" guess as an agent batch.
    # Keep clearly actionable lines for the Tasks board only (no chat offer).
    if notes_doc:
        explicit = False
        inferred = False
    if not explicit and not inferred and not notes_doc:
        return None
    if notes_doc and not actionable:
        return None

    # Desktop OCR of terminals/IDEs is the main false-positive source — require
    # a trusted list window or a clear "To Do List" header.
    if source.startswith("desktop") and not _trusted_desktop_todo(window, title, ocr):
        # Notes on a trusted surface (e.g. Notepad titled "… Notes") may still
        # ingest actionable lines without offering.
        if not (notes_doc and _TRUSTED_TODO_WINDOW.search(window)):
            return None

    if not title and inferred:
        title = "To Do List"

    # Align confidences to cleaned actionable list (best-effort by index).
    conf_out = None
    if isinstance(confidences, list) and len(confidences) == len(raw_items):
        conf_out = []
        for it in actionable:
            try:
                idx = raw_items.index(it)
                conf_out.append(confidences[idx])
            except ValueError:
                conf_out.append(None)

    # Only auto-offer real to-do lists — never dump a notes doc into the agent.
    offer = (not notes_doc) and (explicit or inferred)

    return {
        "title": title,
        "items": actionable,          # only what we'd ever offer/ingest
        "confidences": conf_out,
        "frame_path": meta.get("frame_path"),
        "inferred": inferred and not explicit,
        "offer": offer,
        "notes_doc": notes_doc,
        "content_type": ctype,
        "source": getattr(ev, "source", "") or "",
        "window": str(meta.get("window") or ""),
    }


def _on_event(ev) -> None:
    try:
        if getattr(ev, "modality", None) != Modality.VISION:
            return
        if not _enabled():
            return

        payload = _todo_payload(ev)
        if not payload:
            return

        items = payload["items"]
        title = payload["title"]
        now = time.time()

        # --- update the Tasks board (clean items only) ---------------------
        created: list[int] = []
        try:
            from app.services.extractor import extractor
            created = extractor.ingest_todo_items(
                items, title=title,
                confidences=payload.get("confidences"), ts=now)
            if created:
                print(f"[todo] recorded {len(created)} new task(s) "
                      f"from {payload.get('source') or 'vision'}"
                      f"{' (inferred)' if payload.get('inferred') else ''} "
                      f"[{(payload.get('window') or '')[:40]}].")
            else:
                print(f"[todo] tasks board up to date "
                      f"({len(items)} clean item(s) already open).")
        except Exception as exc:
            print(f"[todo] task ingest skipped ({exc}).")

        # --- chat offer (debounced) ----------------------------------------
        # Notes documents may update the Tasks board with actionable lines but
        # must never dump a whole meeting-notes file into the agent.
        if not payload.get("offer", True):
            if payload.get("notes_doc"):
                print(f"[todo] notes doc kept off the agent offer path "
                      f"({len(items)} actionable line(s) on the board).")
            return

        h = _hash(items)
        with _lock:
            last = _recent_offer.get(h)
            if last is not None and now - last < _COOLDOWN_S:
                return
            _recent_offer[h] = now

        from app.services.agent_bridge import worker

        offered = worker.propose_todo(
            items, title,
            frame_path=payload.get("frame_path"),
            event_time=getattr(ev, "time", None))
        if offered:
            print(f"[todo] offered {len(items)} to-do item(s) in chat — reply yes/no.")
    except Exception as exc:  # never break the capture path
        print(f"[todo] watcher error: {exc}")


def attach() -> None:
    from app.events import bus

    bus.subscribe(_on_event)
    print("[todo] watching for to-do lists (offers to act via chat).")
