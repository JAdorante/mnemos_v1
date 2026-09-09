"""Persistent connector preferences — tool access, team allowlist, customs.

Stored at ``data/connectors/prefs.json``. Custom MCP definitions live in the
same file under ``custom`` so one atomic write covers Manage-modal edits.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from app.config import settings

_lock = threading.RLock()

_DEFAULT: dict[str, Any] = {
    # auto  — every connected (and team-allowed) connector is on for chat
    #         unless the user toggles it off for this conversation
    # on_demand — only connectors the user toggles on for this conversation
    "tool_access": "auto",
    # When True, only ids in team_allowed may be connected on this install.
    # Owners flip this for shared / team seats (Claude Org → Connectors).
    "team_policy": False,
    "team_allowed": [],
    "custom": [],
}


def prefs_path() -> Path:
    return Path(settings.storage.data_dir) / "connectors" / "prefs.json"


def _load() -> dict[str, Any]:
    path = prefs_path()
    if not path.is_file():
        return dict(_DEFAULT)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_DEFAULT)
    if not isinstance(raw, dict):
        return dict(_DEFAULT)
    out = dict(_DEFAULT)
    out.update(raw)
    if out.get("tool_access") not in ("auto", "on_demand"):
        out["tool_access"] = "auto"
    if not isinstance(out.get("team_allowed"), list):
        out["team_allowed"] = []
    if not isinstance(out.get("custom"), list):
        out["custom"] = []
    out["team_policy"] = bool(out.get("team_policy"))
    return out


def _save(data: dict[str, Any]) -> None:
    path = prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def get() -> dict[str, Any]:
    with _lock:
        return _load()


def tool_access() -> str:
    return get().get("tool_access") or "auto"


def set_tool_access(mode: str) -> dict[str, Any]:
    mode = (mode or "").strip().lower()
    if mode not in ("auto", "on_demand"):
        return {"ok": False, "error": "tool_access must be auto or on_demand"}
    with _lock:
        data = _load()
        data["tool_access"] = mode
        _save(data)
    return {"ok": True, "tool_access": mode}


def team_policy() -> dict[str, Any]:
    d = get()
    return {
        "enabled": bool(d.get("team_policy")),
        "allowed": list(d.get("team_allowed") or []),
    }


def set_team_policy(*, enabled: bool | None = None,
                    allowed: list[str] | None = None) -> dict[str, Any]:
    with _lock:
        data = _load()
        if enabled is not None:
            data["team_policy"] = bool(enabled)
        if allowed is not None:
            clean = []
            seen = set()
            for raw in allowed:
                cid = str(raw or "").strip().lower()
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                clean.append(cid)
            data["team_allowed"] = clean
        _save(data)
    return {"ok": True, **team_policy()}


def is_team_allowed(connector_id: str) -> bool:
    """Whether this install's team policy permits connecting ``connector_id``."""
    d = get()
    if not d.get("team_policy"):
        return True
    allowed = {str(x).lower() for x in (d.get("team_allowed") or [])}
    return (connector_id or "").strip().lower() in allowed


def list_custom() -> list[dict[str, Any]]:
    return list(get().get("custom") or [])


def upsert_custom(entry: dict[str, Any]) -> dict[str, Any]:
    """Insert or replace a custom connector definition by id."""
    cid = (entry.get("id") or "").strip().lower()
    if not cid:
        return {"ok": False, "error": "id required"}
    with _lock:
        data = _load()
        rows = [r for r in (data.get("custom") or [])
                if (r.get("id") or "").strip().lower() != cid]
        rows.append(dict(entry))
        data["custom"] = rows
        _save(data)
    return {"ok": True, "connector": entry}


def remove_custom(connector_id: str) -> dict[str, Any]:
    cid = (connector_id or "").strip().lower()
    with _lock:
        data = _load()
        before = list(data.get("custom") or [])
        data["custom"] = [r for r in before
                          if (r.get("id") or "").strip().lower() != cid]
        if len(data["custom"]) == before.__len__():
            return {"ok": False, "error": "unknown custom connector"}
        _save(data)
    return {"ok": True, "removed": cid}
