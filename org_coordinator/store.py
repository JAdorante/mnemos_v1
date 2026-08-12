"""JSON-backed persistence for the Org Coordinator (no SQLite dependency)."""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

_lock = threading.RLock()

ROLES = frozenset({"ic", "manager", "exec", "ceo"})


def data_dir() -> Path:
    return Path(os.environ.get("QUILL_ORG_COORD_DATA",
                               os.environ.get("QUILL_DATA_DIR", "data"))
                ) / "org_coordinator"


def _path(name: str) -> Path:
    return data_dir() / name


def _load(name: str, default: Any) -> Any:
    p = _path(name)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(name: str, data: Any) -> None:
    p = _path(name)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


def list_nodes() -> dict[str, dict]:
    with _lock:
        return dict(_load("directory.json", {}))


def get_node(node_id: str) -> dict | None:
    return list_nodes().get(node_id)


def upsert_node(node: dict) -> dict:
    with _lock:
        reg = _load("directory.json", {})
        nid = node["node_id"]
        prev = reg.get(nid) or {}
        merged = {**prev, **node, "updated_at": time.time()}
        if "created_at" not in merged:
            merged["created_at"] = time.time()
        reg[nid] = merged
        _save("directory.json", reg)
        return merged


def reports_of(manager_id: str) -> list[dict]:
    return [n for n in list_nodes().values()
            if (n.get("reports_to") or "") == manager_id]


def manager_chain(node_id: str) -> list[dict]:
    """Walk reports_to upward: [self, manager, ..., ceo]."""
    chain: list[dict] = []
    seen: set[str] = set()
    cur = get_node(node_id)
    while cur and cur.get("node_id") not in seen:
        chain.append(cur)
        seen.add(cur["node_id"])
        parent = cur.get("reports_to") or ""
        if not parent:
            break
        cur = get_node(parent)
    return chain


def skip_level_target(node_id: str, *, min_role: str = "exec") -> dict | None:
    """First ancestor at or above min_role (exec/ceo)."""
    rank = {"ic": 0, "manager": 1, "exec": 2, "ceo": 3}
    need = rank.get(min_role, 2)
    for n in manager_chain(node_id)[1:]:
        if rank.get(n.get("role") or "ic", 0) >= need:
            return n
    return None


def list_goals() -> list[dict]:
    with _lock:
        return list(_load("goals.json", []))


def add_goal(title: str, *, horizon: str = "", priority: float = 0.8,
             owner_role: str = "ceo", detail: str = "") -> dict:
    with _lock:
        goals = _load("goals.json", [])
        g = {
            "id": uuid.uuid4().hex[:12],
            "title": (title or "").strip()[:400],
            "detail": (detail or "").strip()[:2000],
            "horizon": (horizon or "").strip()[:120],
            "priority": float(priority),
            "owner_role": owner_role if owner_role in ROLES else "ceo",
            "active": True,
            "created_at": time.time(),
        }
        goals.append(g)
        _save("goals.json", goals)
        return g


def set_goal_active(goal_id: str, active: bool) -> dict | None:
    with _lock:
        goals = _load("goals.json", [])
        for g in goals:
            if g.get("id") == goal_id:
                g["active"] = bool(active)
                _save("goals.json", goals)
                return g
        return None


def append_digest(digest: dict) -> dict:
    with _lock:
        rows = _load("digests.json", [])
        row = {**digest, "id": uuid.uuid4().hex[:12],
               "received_at": time.time()}
        rows.append(row)
        # Bound history
        rows = rows[-500:]
        _save("digests.json", rows)
        return row


def digests_for(node_id: str | None = None, *, limit: int = 50) -> list[dict]:
    rows = _load("digests.json", [])
    if node_id:
        rows = [r for r in rows if r.get("node_id") == node_id]
    return rows[-limit:]


def digests_from_reports(manager_id: str, *, since: float = 0.0,
                         limit: int = 100) -> list[dict]:
    report_ids = {n["node_id"] for n in reports_of(manager_id)}
    rows = [r for r in _load("digests.json", [])
            if r.get("node_id") in report_ids
            and float(r.get("received_at") or 0) >= since]
    return rows[-limit:]


def append_escalation(row: dict) -> dict:
    p = _path("escalations.jsonl")
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {**row, "id": uuid.uuid4().hex[:12], "at": time.time()}
    with _lock:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def mint_token() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex[:16]
