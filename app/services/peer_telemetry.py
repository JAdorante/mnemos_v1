"""Peer round-trip telemetry — the trail that makes the peer channel evaluable.

One JSONL row per event, joined by `ask_id`, in the same shape family as
`escalate_distill.jsonl`. With a handful of pilot testers there is never enough
traffic to *infer* whether peer is failing on consent latency, retrieval, or
composition, so each of those is recorded as its own event:

    ask_sent   — we dispatched an ask       (asker side)
    gate       — the disclosure gate decided (answerer side)
    verdict    — a human approved/declined   (answerer side)
    answer     — an answer landed back       (asker side)

`rollup()` joins them into the three pilot metrics: round-trip completion rate,
median time-to-answer, and repeat use.

PRIVACY: this trail records metadata ONLY — never question or answer text.
Lengths, classified topic, policy action, and timings, nothing else. The pilot
operator reads this file; they must not thereby read either tenant's memory.
The one identifier kept is `peer_id` (already an opaque pairing id) plus the
peer's display name, which the operator provisioned in the first place.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.config import settings

_lock = threading.Lock()

# Terminal states for an outbound ask, split by whether the asker got something
# they could use. "declined" is a completed round trip but not a usable answer.
_USABLE_STATES = frozenset({"answered"})
_TERMINAL_STATES = frozenset({"answered", "declined", "denied", "refused"})


def enabled() -> bool:
    return bool(settings.peer.enabled and settings.peer.telemetry_enabled)


def _path() -> Path:
    return Path(settings.peer.telemetry_path)


def record(event: str, *, ask_id: str = "", peer_id: str = "",
           peer_name: str = "", kind: str = "", **fields: Any) -> dict | None:
    """Append one telemetry row. Never raises — telemetry must not break peer.

    Callers pass metadata only; any key whose name suggests content
    (`question`, `answer`, `text`, `body`) is dropped rather than trusted, so a
    future call site cannot leak memory text into the trail by accident.
    """
    if not enabled():
        return None
    row: dict[str, Any] = {
        "id": uuid.uuid4().hex[:12],
        "time": time.time(),
        "event": event,
        "ask_id": ask_id or "",
        "peer_id": peer_id or "",
        "peer_name": peer_name or "",
        "kind": kind or "",
    }
    for k, v in fields.items():
        if k in ("question", "answer", "text", "body", "claims", "prose"):
            continue
        row[k] = v
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with _lock:
            with p.open("a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
    except Exception as exc:
        print(f"[peer_telemetry] write skipped ({exc}).")
        return None
    return row


def read_rows(limit: int | None = None) -> list[dict]:
    """Every row, oldest first. Small by design (one line per peer event)."""
    p = _path()
    if not p.is_file():
        return []
    out: list[dict] = []
    try:
        with _lock:
            for ln in p.read_text(encoding="utf-8").splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    out.append(json.loads(ln))
                except Exception:
                    continue
    except Exception as exc:
        print(f"[peer_telemetry] read skipped ({exc}).")
        return []
    return out[-limit:] if limit else out


def rollup(rows: list[dict] | None = None) -> dict:
    """The three pilot metrics, plus the breakdown that says WHERE it failed.

    completion_rate  — asks that produced a usable answer / asks sent
    median_answer_s  — send → usable answer, in seconds
    repeat_use       — askers (by peer pair) who asked more than once
    """
    rows = read_rows() if rows is None else rows
    by_ask: dict[str, dict] = defaultdict(dict)
    for r in rows:
        aid = str(r.get("ask_id") or "")
        if not aid:
            continue
        ev = r.get("event")
        slot = by_ask[aid]
        # First writer wins per event so a retry can't restart the clock.
        if ev not in slot:
            slot[ev] = r

    sent = [a for a in by_ask.values() if "ask_sent" in a]
    answered, latencies = [], []
    for a in sent:
        ans = a.get("answer")
        if not ans:
            continue
        if ans.get("status") in _USABLE_STATES and ans.get("usable") is not False:
            answered.append(a)
            dt = float(ans.get("time") or 0) - float(a["ask_sent"].get("time") or 0)
            if dt >= 0:
                latencies.append(dt)

    # Verdict latency — how long the answerer's human sat on it. This is the
    # number that separates "retrieval is bad" from "consent is dead air".
    verdict_waits = [float(r.get("waited_s") or 0) for r in rows
                     if r.get("event") == "verdict" and r.get("waited_s") is not None]

    pairs = defaultdict(int)
    for a in sent:
        pairs[str(a["ask_sent"].get("peer_id") or "")] += 1
    repeat = sum(1 for n in pairs.values() if n > 1)

    gates = defaultdict(int)
    for r in rows:
        if r.get("event") == "gate":
            gates[str(r.get("action") or "?")] += 1

    return {
        "asks_sent": len(sent),
        "answered_usable": len(answered),
        "completion_rate": (len(answered) / len(sent)) if sent else None,
        "median_answer_s": statistics.median(latencies) if latencies else None,
        "median_verdict_wait_s": (statistics.median(verdict_waits)
                                  if verdict_waits else None),
        "pairs_asked": len(pairs),
        "pairs_repeat": repeat,
        "repeat_use": (repeat / len(pairs)) if pairs else None,
        "gate_actions": dict(gates),
        "terminal_states": _state_counts(by_ask),
    }


def _state_counts(by_ask: dict[str, dict]) -> dict:
    out: dict[str, int] = defaultdict(int)
    for a in by_ask.values():
        ans = a.get("answer")
        if ans:
            out[str(ans.get("status") or "?")] += 1
        elif "ask_sent" in a:
            out[str(a["ask_sent"].get("status") or "open")] += 1
    return dict(out)


def reset_for_tests() -> None:
    """Drop the trail. Tests only — there is no product path that erases it."""
    try:
        _path().unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[peer_telemetry] reset skipped ({exc}).")
