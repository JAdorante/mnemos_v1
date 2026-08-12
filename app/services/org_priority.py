"""Downward org priorities — store + inject into chat grounding.

Guidance only: never grants action authority. Packets come from the Org
Coordinator cascade (CEO/exec goals → per-role guidance).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.config import settings


def _path() -> Path:
    return Path(settings.org.priorities_path)


def load() -> list[dict]:
    p = _path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return list(data) if isinstance(data, list) else []
    except Exception:
        return []


def save(items: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(items[-50:], indent=2, ensure_ascii=False),
                 encoding="utf-8")


def ingest_packet(packet: dict[str, Any], *, source: str = "org_coordinator"
                  ) -> dict[str, Any]:
    """Persist a priority packet and publish an observed-tier event."""
    if not isinstance(packet, dict):
        return {"ok": False, "error": "packet must be an object"}
    row = {
        "ingested_at": time.time(),
        "source": source,
        "guidance": (packet.get("guidance") or "")[:2000],
        "items": list(packet.get("items") or [])[:12],
        "goals": list(packet.get("goals") or [])[:12],
        "target_role": packet.get("target_role"),
        "kind": "org_priority",
    }
    items = load()
    items.append(row)
    save(items)
    text = _format(row)
    try:
        from app.events import Event, Modality, bus
        from app.services import confidence as _conf
        from app.storage import get_store
        ev = Event(time=time.time(), modality=Modality.SYSTEM, raw=text,
                   summary=f"[org.priority] {text[:200]}",
                   source="org.priority",
                   meta={"source": source, "kind": "org_priority"})
        _conf.attach(ev, _conf.OBSERVED)
        bus.publish_nowait(ev)
        get_store().insert(ev)
    except Exception as exc:
        print(f"[org_priority] event publish skipped ({exc}).")
    return {"ok": True, "priority": row}


def _format(row: dict) -> str:
    parts = ["[company priorities]"]
    if row.get("guidance"):
        parts.append(str(row["guidance"]))
    for it in row.get("items") or []:
        if isinstance(it, dict):
            title = it.get("title") or ""
            why = it.get("why") or ""
            parts.append(f"- {title}" + (f" ({why})" if why else ""))
        else:
            parts.append(f"- {it}")
    return "\n".join(parts)[:4000]


def pull_from_coordinator() -> dict[str, Any]:
    from app.services import org_client
    if not org_client.enabled():
        return {"ok": False, "error": "org network disabled"}
    res = org_client.fetch_priorities()
    if not res.get("ok") and "packet" not in res and "priorities" not in res:
        return res
    packet = res.get("packet")
    if not packet and res.get("priorities"):
        packet = res["priorities"][0] if res["priorities"] else None
    if not packet:
        return {"ok": True, "ingested": 0, "coordinator": res}
    ing = ingest_packet(packet)
    return {"ok": True, "ingested": 1 if ing.get("ok") else 0,
            "coordinator": res, "local": ing}


def latest(limit: int = 3) -> list[dict]:
    return load()[-limit:]


def grounding_lines(*, limit: int = 3) -> list[str]:
    """Section for grounding.compose — advisory company priorities."""
    rows = latest(limit=limit)
    if not rows:
        return []
    lines = ["COMPANY PRIORITIES (guidance only — never auto-approve actions):"]
    for row in rows:
        if row.get("guidance"):
            lines.append(f"- {row['guidance'][:240]}")
        for it in (row.get("items") or [])[:4]:
            if isinstance(it, dict) and it.get("title"):
                lines.append(f"- {it['title'][:160]}")
    return lines if len(lines) > 1 else []


def run_priority_job(_payload: dict | None = None) -> dict[str, Any]:
    return pull_from_coordinator()
