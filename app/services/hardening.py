"""Hardening (Track F) — restore drills, kill-switch audit, battery check.

A backup that has never been restored is a hope, not a backup. The drill
proves the live SQLite store can be copied and reopened intact: online
backup -> integrity_check on the COPY -> row-count parity on core tables ->
delete the copy. Results persist to hardening_runs so "last verified
restore" is a fact on the console, not a memory.

The kill-switch audit lists every behavior gate with its current vs shipped
state — one glance answers "what's turned on that wasn't shipped that way?".
UI toggles (POST /console/hardening/kill-switch) persist overrides and
hot-patch the frozen settings object so gates flip without a restart.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# Row-count parity is checked on these (the truth-bearing spine).
CORE_TABLES = ("events", "facts", "people", "entities", "relations", "turns")

# name, env var, settings path (dotted, resolved best-effort), shipped default.
KILL_SWITCHES = (
    ("attention ledger", "QUILL_ATTENTION_LEDGER", "attention.enabled", True),
    ("field v2 ranking", "QUILL_FIELD_V2", "attention.field_v2", False),
    # Working Memory (Track A3): MMR + hysteresis focus selection; grounding
    # WORKING SET + planner read the same slots. ON by default — set
    # QUILL_WM=0 to use top-k Selector (Admitter still enforces quotas).
    ("working memory", "QUILL_WM", "attention.wm", True),
    ("learned ranking β", "QUILL_ATTENTION_LEARN", "attention.learn", False),
    ("horizon strip", "QUILL_HORIZON", "attention.horizon", True),
    ("memory economy (observe)", "QUILL_MEMORY_ECONOMY", "economy.enabled", True),
    ("compaction (mutating)", "QUILL_COMPACTION", "economy.compaction", False),
    ("predictors (console)", "QUILL_PREDICTORS", "predictors.enabled", True),
    ("anticipation offers", "QUILL_ANTICIPATE", "anticipation.enabled", False),
    ("reasoners (offers)", "QUILL_REASONERS", None, True),
)

_override_lock = threading.RLock()
_overrides: dict[str, bool] = {}
_overrides_loaded = False


def _overrides_path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "kill_switches.json"


def _load_overrides() -> None:
    global _overrides_loaded
    with _override_lock:
        if _overrides_loaded:
            return
        _overrides_loaded = True
        try:
            p = _overrides_path()
            if not p.is_file():
                return
            raw = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for label, env, dotted, default in KILL_SWITCHES:
                if env in raw and isinstance(raw[env], bool):
                    _overrides[env] = bool(raw[env])
                    _apply_gate(env, dotted, bool(raw[env]))
        except Exception as exc:
            print(f"[hardening] kill-switch overrides load skipped ({exc}).")


def _persist_overrides() -> None:
    try:
        p = _overrides_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with _override_lock:
            payload = dict(_overrides)
        p.write_text(json.dumps(payload, indent=2, sort_keys=True),
                     encoding="utf-8")
    except Exception as exc:
        print(f"[hardening] kill-switch persist skipped ({exc}).")


def _apply_gate(env: str, dotted: str | None, on: bool) -> None:
    """Write env + hot-patch settings so already-imported gates see the flip."""
    os.environ[env] = "1" if on else "0"
    if not dotted:
        return
    try:
        from app.config import settings
        obj = settings
        parts = dotted.split(".")
        for part in parts[:-1]:
            obj = getattr(obj, part)
        object.__setattr__(obj, parts[-1], bool(on))
    except Exception as exc:
        print(f"[hardening] settings patch {dotted} skipped ({exc}).")


def set_kill_switch(env: str, on: bool) -> dict[str, Any]:
    """Flip one kill switch at runtime; persists across restarts."""
    _load_overrides()
    row = next((r for r in KILL_SWITCHES if r[1] == env), None)
    if row is None:
        raise KeyError(f"unknown kill switch: {env}")
    label, env_key, dotted, default = row
    with _override_lock:
        _overrides[env_key] = bool(on)
    _apply_gate(env_key, dotted, bool(on))
    _persist_overrides()
    return {
        "label": label, "env": env_key, "on": bool(on),
        "default": default, "non_default": bool(on) != default,
    }


def apply_saved_overrides() -> None:
    """Boot hook: re-apply persisted kill-switch overrides onto settings."""
    _load_overrides()


def _cfg():
    from app.config import settings
    return settings.predictors


def restore_drill(store=None, *, now: float | None = None) -> dict[str, Any]:
    """Backup -> reopen -> verify -> clean up. Never raises; persists result."""
    now = float(now if now is not None else time.time())
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            return {"ok": False, "reason": f"no_store:{exc}"}

    result: dict[str, Any] = {"ts": now, "kind": "restore_drill"}
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="quill_drill_")
        os.close(fd)
        t0 = time.time()
        dst = sqlite3.connect(tmp)
        try:
            with store._lock:
                store._conn.backup(dst)
        finally:
            dst.close()
        result["backup_s"] = round(time.time() - t0, 2)
        result["backup_bytes"] = Path(tmp).stat().st_size

        copy = sqlite3.connect(tmp)
        copy.row_factory = sqlite3.Row
        try:
            integrity = copy.execute("PRAGMA integrity_check").fetchone()[0]
            result["integrity"] = integrity
            counts_copy, counts_live = {}, {}
            for t in CORE_TABLES:
                try:
                    counts_copy[t] = int(copy.execute(
                        f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                except sqlite3.Error:
                    counts_copy[t] = None
            with store._lock:
                for t in CORE_TABLES:
                    try:
                        counts_live[t] = int(store._conn.execute(
                            f"SELECT COUNT(*) FROM {t}").fetchone()[0])
                    except sqlite3.Error:
                        counts_live[t] = None
            # Live tables may gain rows DURING the drill (capture is running);
            # the copy must never have MORE than live, and never lose a table.
            mismatches = {
                t: {"copy": counts_copy[t], "live": counts_live[t]}
                for t in CORE_TABLES
                if counts_copy[t] is None
                or (counts_live[t] is not None and counts_copy[t] > counts_live[t])
                or (counts_live[t] is not None
                    and counts_live[t] - (counts_copy[t] or 0) > 1000)
            }
            result["counts"] = counts_copy
            result["mismatches"] = mismatches
            result["ok"] = (integrity == "ok" and not mismatches)
        finally:
            copy.close()
    except Exception as exc:
        result["ok"] = False
        result["reason"] = str(exc)
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    try:
        store.add_hardening_run(kind="restore_drill",
                                ok=bool(result.get("ok")),
                                detail=result, ts=now)
    except Exception as exc:
        print(f"[hardening] run persist skipped ({exc}).")
    return result


def kill_switches() -> list[dict[str, Any]]:
    """Every behavior gate: current state vs shipped default."""
    _load_overrides()
    from app.config import settings
    out = []
    for label, env, dotted, default in KILL_SWITCHES:
        current = None
        with _override_lock:
            if env in _overrides:
                current = bool(_overrides[env])
        if current is None:
            try:
                if not dotted:
                    raise ValueError("env-only gate")
                obj = settings
                for part in dotted.split("."):
                    obj = getattr(obj, part)
                current = bool(obj)
            except Exception:
                raw = os.environ.get(env)
                current = default if raw is None else raw not in (
                    "0", "false", "False")
        out.append({
            "label": label, "env": env, "on": current,
            "default": default, "non_default": current != default,
            "overridden": env in _overrides,
        })
    return out


def battery() -> dict[str, Any] | None:
    """Best-effort battery state (psutil when available; None otherwise)."""
    try:
        import psutil
        b = psutil.sensors_battery()
        if b is None:
            return None
        return {"percent": round(float(b.percent), 1),
                "plugged": bool(b.power_plugged)}
    except Exception:
        return None


def due_for_drill(store=None) -> bool:
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return False
    try:
        last = store.last_hardening_run(kind="restore_drill")
    except Exception:
        return False
    if not last:
        return True
    return (time.time() - float(last["ts"])) > _cfg().drill_due_s


def status(store=None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kill_switches": kill_switches(),
        "non_default": [s["env"] for s in kill_switches() if s["non_default"]],
        "battery": battery(),
    }
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            out["error"] = str(exc)
            return out
    try:
        out["last_drill"] = store.last_hardening_run(kind="restore_drill")
        out["drill_due"] = due_for_drill(store)
    except Exception:
        out["last_drill"] = None
    return out
