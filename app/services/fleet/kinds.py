"""This Sparrow's signal-kind registry and blocked-subjects list.

Both are plain JSON files the owner (or compliance) edits, read on each use
so a change applies without a restart. Each fails closed in its own way:

  kinds    — a malformed registry falls back to the built-ins only, so a
             custom kind is refused rather than validated loosely.
  blocked  — missing or malformed raises BlockedListUnavailable, and the
             router then lets nothing leave.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from app.config import settings
from app.services.fleet import _files
from app.services.fleet import envelope as env


class BlockedListUnavailable(RuntimeError):
    pass


def registry() -> dict:
    p = Path(settings.fleet.kinds_path)
    if not p.is_file():
        return env.load_kinds(None)
    try:
        return env.load_kinds(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        print(f"[fleet] fleet_kinds.json ignored ({exc}); built-in kinds "
              "only.")
        return env.load_kinds(None)


def save_registry(kinds: dict) -> dict:
    """Validate every definition first; write nothing if any is bad."""
    body = {"kinds": kinds}
    env.load_kinds(body)
    with _files.lock:
        _files.save(settings.fleet.kinds_path,
                    {**body, "updated_at": time.time()})
    return registry()


def blocklist() -> env.Blocklist:
    p = Path(settings.fleet.blocked_list_path)
    if not p.is_file():
        raise BlockedListUnavailable("blocked list missing")
    try:
        return env.load_blocklist(json.loads(p.read_text(encoding="utf-8")))
    except Exception as exc:
        raise BlockedListUnavailable(f"blocked list unreadable ({exc})") \
            from None


def save_blocklist(subjects: list[str], patterns: list[str] | None = None
                   ) -> dict:
    body = {"subjects": [str(s) for s in subjects],
            "patterns": [str(p) for p in patterns or []]}
    env.load_blocklist(body)
    with _files.lock:
        _files.save(settings.fleet.blocked_list_path,
                    {**body, "updated_at": time.time()})
    return body
