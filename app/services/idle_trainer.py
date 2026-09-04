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

Hosted posture (QUILL_HEADLESS=1): the keyboard/battery probes are replaced —
"idle" becomes capture-quiet (seconds since the last ingested event) and the
AC check always passes. Training runs natively (train_lora's Linux path, no
WSL) and publishes a per-user tag on the shared Ollama via
QUILL_LORA_TAG_SUFFIX.

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
    # Cold-start bootstrap: a NEVER-trained profile whose ORGANIC pairs are
    # still short of the organic bar, but whose real + synthetic total
    # reaches the green light, gets its FIRST run without waiting for
    # min_new_pairs labels or exemplar saturation — the automatic-at-signup
    # path. Installs with enough organic signal take the normal path even on
    # their first run; every later run needs organic growth like before; and
    # the promotion gate (real-holdout bench) still decides what ships.
    organic = int(probes.get("pairs") or 0)
    min_new = int(probes.get("min_new_pairs", 150))
    total = organic + int(probes.get("synth_pairs") or 0)
    never_trained = not (state.get("last_run_ts")
                         or state.get("pairs_at_last_run"))
    bootstrap = (never_trained and organic < min_new
                 and total >= int(probes.get("bootstrap_min", 100)))
    new_pairs = organic - int(state.get("pairs_at_last_run") or 0)
    if new_pairs < min_new and not bootstrap:
        return False, f"only {new_pairs} new labeled pairs (need {min_new})"
    if float(probes.get("idle_s") or 0) < float(probes.get("min_idle_s", 1200)):
        return False, "user is active"
    if not probes.get("on_ac"):
        return False, "on battery"
    free = float(probes.get("free_gb") or 0)
    min_free = float(probes.get("min_free_gb", 25))
    if free < min_free:
        return False, f"only {free:.0f} GB free disk (need {min_free:.0f})"
    # E.2: LoRA is the graduation path — it fires when a task_type has
    # saturated the exemplar store, not on raw pair count alone. Probes that
    # don't supply the fact (older callers/tests) default to permissive.
    # The cold-start bootstrap run skips this: its pairs are parent-distilled,
    # not exemplar-derived, so saturation has nothing to measure yet.
    if not probes.get("lora_saturated", True) and not bootstrap:
        return False, ("no task type at LoRA saturation "
                       "(exemplar retrieval still improving — see "
                       "data/exemplar_ab_report.json)")
    if bootstrap:
        return True, (f"cold-start bootstrap: {total} pairs "
                      f"({probes.get('synth_pairs') or 0} synthetic); "
                      f"idle, {free:.0f} GB free")
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


def capture_idle_seconds() -> float:
    """Hosted-instance idleness: seconds since the last INGESTED event.

    A headless container has no keyboard, mouse, or battery — the desktop
    probes are meaningless there. What "don't train over the user" means in
    that posture is "don't train while capture is flowing": the browser mic /
    tab stream is the user's presence signal. Store failure or an empty
    events table reads as active (0.0) — never train on an unknown."""
    try:
        from app.storage import get_store
        rows = get_store().recent_events(limit=1)
        if not rows:
            return 0.0
        return max(0.0, time.time() - float(rows[0].get("time") or 0))
    except Exception:
        return 0.0


def hosted_mode() -> bool:
    """Headless instances (QUILL_HEADLESS=1) swap the desktop probes for
    capture-quiet idleness and skip the battery check entirely."""
    return _env("QUILL_HEADLESS", "0") not in ("0", "false", "False")


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
    """Current curated train-pair count (exact dedupe — no embedder load).
    E.1: counts the new learning_pairs source, JSONL fallback included."""
    try:
        sys.path.insert(0, str(_ROOT / "scripts"))
        import distill_curate as dc
        from app.config import settings
        rows, _source = dc.load_training_rows(settings)
        return int(dc.curate(rows, holdout_pct=34, dedupe_sim=1.0)["train_pairs"])
    except Exception:
        return 0


def lora_saturation(type_counts: dict, ab_history: list[dict], *,
                    min_type_pairs: int, plateau_eps: float,
                    truncating: bool = False) -> tuple[bool, str]:
    """E.2 saturation predicate, pure. A task_type is saturated when it has
    >= min_type_pairs confirmed positive pairs AND either the exemplar A/B
    gains for it have plateaued (delta between the last two evals < eps) or
    the exemplar token budget is the binding constraint (`truncating`).

    `type_counts`: {task_type: {"accepted": n, "edited": n, ...}} — the
    learning store's counter shape. `ab_history`: eval_exemplars report
    history, oldest→newest, each {"by_type": {type: {"delta": ...}}}."""
    for task_type, verdicts in (type_counts or {}).items():
        confirmed = int(verdicts.get("accepted", 0)) + \
            int(verdicts.get("edited", 0))
        if confirmed < int(min_type_pairs):
            continue
        if truncating:
            return True, (f"{task_type}: {confirmed} pairs + exemplar "
                          "budget binding")
        deltas = [
            (h.get("by_type") or {}).get(task_type, {}).get("delta")
            for h in (ab_history or [])
        ]
        deltas = [d for d in deltas if d is not None]
        if len(deltas) >= 2 and abs(deltas[-1] - deltas[-2]) < plateau_eps:
            return True, (f"{task_type}: {confirmed} pairs, exemplar gains "
                          f"plateaued ({deltas[-2]}→{deltas[-1]})")
    return False, "no task type at saturation (exemplars still improving)"


def lora_saturated_probe() -> bool:
    """Environmental probe for _probes(): counts from the learning store +
    the exemplar A/B history file. True (permissive) only when the learning
    loop isn't deployed at all — then the legacy pair-count gate governs."""
    try:
        from app.config import settings
        from app.storage import get_store
        counts = get_store().learning_pair_counts()
        if not counts:
            return True          # pre-Workstream-A install: legacy behavior
        history = []
        try:
            p = Path("data/exemplar_ab_report.json")
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                history = data.get("history") or ([data] if data else [])
        except Exception:
            history = []
        ok, reason = lora_saturation(
            counts, history,
            min_type_pairs=int(_env("QUILL_LORA_MIN_TYPE_PAIRS", "300")),
            plateau_eps=float(_env("QUILL_LORA_PLATEAU_EPS", "0.01")))
        if not ok:
            pass
        return ok
    except Exception:
        return True              # probe failure → legacy gates only


def synth_path() -> Path:
    """Where the parent-distilled synthetic pairs live (train-only merge)."""
    raw = os.environ.get("QUILL_LORA_SYNTHETIC")
    return Path(raw) if raw else _ROOT / "data" / "lora" / "synthetic.jsonl"


def synth_pair_count() -> int:
    try:
        p = synth_path()
        if not p.is_file():
            return 0
        return sum(1 for ln in p.read_text(encoding="utf-8-sig").splitlines()
                   if ln.strip())
    except Exception:
        return 0


def fact_count(limit: int = 200) -> int:
    """How much memory exists to ground synthetic questions in (capped scan)."""
    try:
        from app.storage import get_store
        return len(get_store().list_facts(limit=limit))
    except Exception:
        return 0


def synth_bootstrap_due(state: dict, probes: dict) -> tuple[bool, str]:
    """Go/no-go for the automatic synthetic bootstrap, pure. Fires once per
    install (state['synth_done']), only while organic pairs are short of the
    green light, only once the memory graph has enough substance to ground
    questions in, only on the same idle window as training, and at most once
    a day after a failure."""
    if not probes.get("synth_enabled", True):
        return False, "synthetic bootstrap disabled"
    if not probes.get("enabled"):
        return False, "idle training disabled"
    if state.get("synth_done"):
        return False, "bootstrap already completed"
    bootstrap_min = int(probes.get("bootstrap_min", 100))
    pairs = int(probes.get("pairs") or 0)
    if pairs >= bootstrap_min:
        return False, f"{pairs} organic pairs — no bootstrap needed"
    if int(probes.get("synth_pairs") or 0) + pairs >= bootstrap_min:
        return False, "synthetic already fills the gap"
    facts = int(probes.get("facts_n") or 0)
    if facts < int(probes.get("min_facts", 10)):
        return False, (f"only {facts} facts in memory — too little to "
                       "ground questions in yet")
    if float(probes.get("idle_s") or 0) < float(probes.get("min_idle_s", 1200)):
        return False, "user is active"
    now = float(probes.get("now") or time.time())
    if now - float(state.get("synth_last_ts") or 0) < 86400:
        return False, "bootstrap attempted in the last day"
    return True, f"{pairs} organic pairs + {facts}+ facts — generating"


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
        hosted = hosted_mode()
        return {
            "enabled": _env("QUILL_IDLE_TRAIN", "0") not in ("0", "false", "False"),
            "now": time.time(),
            "pairs": pair_count(),
            "idle_s": capture_idle_seconds() if hosted else idle_seconds(),
            "on_ac": True if hosted else on_ac_power(),
            "free_gb": free_gb(),
            "min_new_pairs": int(_env("QUILL_IDLE_TRAIN_MIN_NEW_PAIRS", "150")),
            "min_idle_s": float(_env("QUILL_IDLE_TRAIN_IDLE_MIN", "20")) * 60,
            "min_free_gb": float(_env("QUILL_IDLE_TRAIN_MIN_FREE_GB", "25")),
            "min_days": float(_env("QUILL_IDLE_TRAIN_MIN_DAYS", "7")),
            "max_fails": int(_env("QUILL_IDLE_TRAIN_MAX_FAILS", "3")),
            "lora_saturated": lora_saturated_probe(),
            # Cold-start bootstrap (synthetic_pairs.py) — automatic-at-signup.
            "synth_enabled": _env("QUILL_SYNTH_BOOTSTRAP", "1")
            not in ("0", "false", "False"),
            "synth_pairs": synth_pair_count(),
            "bootstrap_min": int(_env("QUILL_LORA_BOOTSTRAP_MIN", "100")),
            "min_facts": int(_env("QUILL_SYNTH_MIN_FACTS", "10")),
            "facts_n": fact_count(),
        }

    def tick(self) -> str:
        """One scheduling decision (+ training run when green). Returns the
        reason string — the loop and tests share this path."""
        state = load_state()
        probes = self._probes()
        # Workstream B: shadow eval rides the same idle window and runs BEFORE
        # the training-eligibility check — it feeds the store training reads.
        # Its own flag/day/budget gates live in shadow_eval; this is just the
        # idle+AC gate shared with training.
        try:
            from app.services import shadow_eval
            shadow_eval.maybe_run_idle(idle_s=float(probes["idle_s"]),
                                       on_ac=bool(probes["on_ac"]))
        except Exception as exc:
            print(f"[idle_trainer] shadow eval skipped ({exc}).")
        # Workstream D.5: the escalation router retrains on the same idle
        # window (cheap LR fit; its own ≥N-new-labels gate lives inside).
        try:
            if probes["idle_s"] >= probes["min_idle_s"] and probes["on_ac"]:
                from app.services.escalation_router import escalation_router
                escalation_router.maybe_retrain()
        except Exception as exc:
            print(f"[idle_trainer] router retrain skipped ({exc}).")
        # Cold-start bootstrap: a new profile short of the green light gets
        # its synthetic pairs generated here — once, on the idle window —
        # so the personal-model path is automatic from signup onward. After
        # this fills the gap, should_run's bootstrap branch fires the first
        # training run on the next green tick.
        try:
            probes = self._maybe_synth_bootstrap(state, probes)
        except Exception as exc:
            print(f"[idle_trainer] synth bootstrap skipped ({exc}).")
        go, reason = should_run(state, probes)
        if reason != self._last_reason:
            print(f"[idle_trainer] {reason}")
            self._last_reason = reason
        if go:
            self._train(state)
        return reason

    def _maybe_synth_bootstrap(self, state: dict, probes: dict) -> dict:
        """Run the one-time synthetic generation when due; returns probes
        (refreshed with the new synthetic count on success). Failures back
        off a day via synth_last_ts; success sets synth_done."""
        due, reason = synth_bootstrap_due(state, probes)
        if not due:
            return probes
        print(f"[idle_trainer] synthetic bootstrap: {reason}")
        state["synth_last_ts"] = time.time()
        save_state(state)
        try:
            sys.path.insert(0, str(_ROOT / "scripts"))
            import synthetic_pairs as sp
            need = int(probes["bootstrap_min"]) - int(probes.get("pairs") or 0)
            n = min(max(need, 10), int(_env("QUILL_SYNTH_N", "45")))
            res = sp.generate_pairs(n=n, out=synth_path(), append=True)
        except Exception as exc:
            print(f"[idle_trainer] synthetic generation failed ({exc}); "
                  "retrying tomorrow.")
            return probes
        if res.get("generated"):
            state["synth_done"] = True
            save_state(state)
            _notify_chat(
                f"I generated {res['generated']} practice examples from your "
                "memory graph to bootstrap your personal model — training "
                "will start automatically during a quiet window.")
        return {**probes, "synth_pairs": synth_pair_count()}

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
