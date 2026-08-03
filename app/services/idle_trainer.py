"""Idle LoRA retraining — Phase 3's "learns while you sleep" scheduler.

Runs `scripts/train_lora.py` (curate -> train -> package -> gate) when — and
only when — ALL of these hold:

  * opted in                 QUILL_IDLE_TRAIN=1 (default OFF: it's the user's
                             GPU + electricity, and training needs WSL2+CUDA)
  * enough NEW signal        >= QUILL_IDLE_TRAIN_MIN_NEW_PAIRS labeled pairs
                             since the last run (verdicts/edits accrue these
                             passively — no new labels means no run, ever)
  * user is idle             no keyboard/mouse for QUILL_IDLE_TRAIN_IDLE_MIN
  * on AC power              never on battery
  * disk headroom            >= QUILL_IDLE_TRAIN_MIN_FREE_GB free (the merge
                             stage transiently needs ~19GB before cleanup)
  * rate cap + backoff       >= QUILL_IDLE_TRAIN_MIN_DAYS since the last run,
                             doubling per consecutive failure; a failure
                             streak pauses the scheduler entirely

Condition-driven, not clock-driven: a desktop app can't assume the machine is
on at 3am Sunday, and retraining without new data is pure waste. The decision
function is pure (`should_run`) — every environmental fact arrives via a
`probes` dict so tests need no OS, GPU, or clock.

Storage stays O(1) across runs: train_lora.py itself deletes merge/GGUF
intermediates and prunes superseded Ollama tags + run dirs after the gate.

Promotion is an offer, never a silent swap: a gate win lands in chat with the
flip + rollback lines. State lives in data/lora/trainer_state.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_PATH = _ROOT / "data" / "lora" / "trainer_state.json"
GATE_PATH = _ROOT / "data" / "lora" / "runs" / "last_gate.json"

_TRAIN_TIMEOUT_S = 3 * 3600   # QLoRA on 7B with hundreds of pairs: well under


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default) or default


def load_state(path: Path = STATE_PATH) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    from app.atomic_json import write_json
    write_json(path, state)


def should_run(state: dict, probes: dict) -> tuple[bool, str]:
    """The whole go/no-go decision, pure. `state` is the persisted trail
    (last_run_ts, pairs_at_last_run, consecutive_failures); `probes` carries
    every environmental fact: now, enabled, pairs, idle_s, on_ac, free_gb,
    plus the thresholds (min_new_pairs, min_idle_s, min_free_gb, min_days,
    max_fails). Returns (go, human-readable reason)."""
    if not probes.get("enabled"):
        return False, "disabled (set QUILL_IDLE_TRAIN=1 to opt in)"
    fails = int(state.get("consecutive_failures") or 0)
    max_fails = int(probes.get("max_fails", 3))
    if fails >= max_fails:
        return False, (f"paused after {fails} consecutive failed runs — "
                       "fix the cause, then reset data/lora/trainer_state.json")
    now = float(probes.get("now") or time.time())
    min_days = float(probes.get("min_days", 7))
    wait_days = min_days * (2 ** fails)   # failure backoff: 7d, 14d, 28d…
    last = float(state.get("last_run_ts") or 0)
    if now - last < wait_days * 86400:
        return False, f"rate cap: {wait_days:.0f}d between runs not yet reached"
    new_pairs = int(probes.get("pairs") or 0) - int(state.get("pairs_at_last_run") or 0)
    min_new = int(probes.get("min_new_pairs", 150))
    if new_pairs < min_new:
        return False, f"only {new_pairs} new labeled pairs (need {min_new})"
    if float(probes.get("idle_s") or 0) < float(probes.get("min_idle_s", 1200)):
        return False, "user is active"
    if not probes.get("on_ac"):
        return False, "on battery"
    free = float(probes.get("free_gb") or 0)
    min_free = float(probes.get("min_free_gb", 25))
    if free < min_free:
        return False, f"only {free:.0f} GB free disk (need {min_free:.0f})"
    return True, f"{new_pairs} new pairs; idle, AC power, {free:.0f} GB free"


# --- environmental probes (Windows; every failure returns a safe value) ------
def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input (GetLastInputInfo)."""
    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]

        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0.0
        return max(0.0, (ctypes.windll.kernel32.GetTickCount() - lii.dwTime) / 1000.0)
    except Exception:
        return 0.0   # unknown -> "active": never train over the user


def on_ac_power() -> bool:
    """True on AC (or when the machine has no battery / status is unknown on a
    desktop). GetSystemPowerStatus: ACLineStatus 1=AC, 0=battery, 255=unknown."""
    try:
        import ctypes

        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [("ACLineStatus", ctypes.c_ubyte),
                        ("BatteryFlag", ctypes.c_ubyte),
                        ("BatteryLifePercent", ctypes.c_ubyte),
                        ("SystemStatusFlag", ctypes.c_ubyte),
                        ("BatteryLifeTime", ctypes.c_ulong),
                        ("BatteryFullLifeTime", ctypes.c_ulong)]

        sps = SYSTEM_POWER_STATUS()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)):
            return True
        return sps.ACLineStatus != 0    # 1 or 255 (desktop/unknown) both pass
    except Exception:
        return True


def free_gb() -> float:
    try:
        return shutil.disk_usage(_ROOT).free / 1024 ** 3
    except OSError:
        return 0.0


def pair_count() -> int:
    """Current curated train-pair count (exact dedupe — no embedder load)."""
    try:
        sys.path.insert(0, str(_ROOT / "scripts"))
        import distill_curate as dc
        from app.config import settings
        rows = dc.load_all_text(Path(settings.escalate_log.path))
        return int(dc.curate(rows, holdout_pct=34, dedupe_sim=1.0)["train_pairs"])
    except Exception:
        return 0


def _notify_chat(msg: str) -> None:
    try:
        from app.services import agent_bridge
        agent_bridge.worker._emit("system", msg)
    except Exception:
        pass


class IdleTrainer:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._last_reason = ""

    def _probes(self) -> dict:
        return {
            "enabled": _env("QUILL_IDLE_TRAIN", "0") not in ("0", "false", "False"),
            "now": time.time(),
            "pairs": pair_count(),
            "idle_s": idle_seconds(),
            "on_ac": on_ac_power(),
            "free_gb": free_gb(),
            "min_new_pairs": int(_env("QUILL_IDLE_TRAIN_MIN_NEW_PAIRS", "150")),
            "min_idle_s": float(_env("QUILL_IDLE_TRAIN_IDLE_MIN", "20")) * 60,
            "min_free_gb": float(_env("QUILL_IDLE_TRAIN_MIN_FREE_GB", "25")),
            "min_days": float(_env("QUILL_IDLE_TRAIN_MIN_DAYS", "7")),
            "max_fails": int(_env("QUILL_IDLE_TRAIN_MAX_FAILS", "3")),
        }

    def tick(self) -> str:
        """One scheduling decision (+ training run when green). Returns the
        reason string — the loop and tests share this path."""
        state = load_state()
        go, reason = should_run(state, self._probes())
        if reason != self._last_reason:
            print(f"[idle_trainer] {reason}")
            self._last_reason = reason
        if go:
            self._train(state)
        return reason

    def _train(self, state: dict) -> None:
        started = time.time()
        print("[idle_trainer] all conditions met — starting a training run.")
        _notify_chat("Starting an idle training run on your recent corrections "
                     "— I'll report how the new model scores when it's done.")
        try:
            r = subprocess.run(
                [sys.executable, str(_ROOT / "scripts" / "train_lora.py")],
                cwd=str(_ROOT), capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=_TRAIN_TIMEOUT_S)
            ok = r.returncode == 0
            if not ok:
                tail = (r.stdout or "")[-1500:] + "\n" + (r.stderr or "")[-1500:]
                print(f"[idle_trainer] run failed (rc={r.returncode}):\n{tail}")
        except subprocess.TimeoutExpired:
            ok = False
            print(f"[idle_trainer] run timed out after {_TRAIN_TIMEOUT_S}s.")
        except Exception as exc:
            ok = False
            print(f"[idle_trainer] run crashed: {exc}")
        state["last_run_ts"] = time.time()
        state["pairs_at_last_run"] = int(self._probes()["pairs"])
        state["consecutive_failures"] = 0 if ok else \
            int(state.get("consecutive_failures") or 0) + 1
        save_state(state)
        if ok:
            self._report_gate(started)

    def _report_gate(self, started: float) -> None:
        """Surface the gate verdict in chat. Promotion is an OFFER — config
        never flips silently; the flip and rollback lines are one edit each."""
        try:
            if GATE_PATH.stat().st_mtime < started:
                return   # stale file from an older run — nothing to report
            gate = json.loads(GATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        tag, base = gate.get("tag", "?"), gate.get("base", "?")
        top = "; ".join((gate.get("reasons") or [])[:3])
        if gate.get("promote"):
            _notify_chat(
                f"Training finished — the new model ({tag}) beat the current "
                f"one on your holdout: {top}.\nTo switch: set "
                f"QUILL_TEXT_LOCAL_MODEL={tag} in .env and restart. "
                f"Rollback is the same line with {base}.")
        else:
            _notify_chat(
                f"Training finished — the new model didn't beat the current "
                f"one ({top}), so nothing changes. It keeps learning from "
                "your corrections.")

    def start(self) -> None:
        """Hourly scheduling loop on a daemon thread. Cheap no-op when the
        opt-in is off (checked every tick, so flipping the env + restart is
        the only ceremony)."""
        if self._thread is not None:
            return
        tick_s = float(_env("QUILL_IDLE_TRAIN_TICK_S", "3600"))

        def _loop() -> None:
            while True:
                try:
                    self.tick()
                except Exception as exc:
                    print(f"[idle_trainer] tick skipped ({exc}).")
                time.sleep(tick_s)

        self._thread = threading.Thread(target=_loop, name="idle_trainer",
                                        daemon=True)
        self._thread.start()


idle_trainer = IdleTrainer()
