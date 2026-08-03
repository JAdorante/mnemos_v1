"""Tunable configuration for the P0 agent loop.

Model strategy follows FS-BA-001 §2 (tiered routing). Loop guards follow NFR-6.
All values are plain module attributes so they can be overridden from run.py.
"""
import os
from pathlib import Path

# --- Tiered model routing (FR-MODEL-1) -------------------------------------
ROUTER_MODEL = "claude-sonnet-4-6"     # QUILL intent/action router (once per request)
PLANNER_MODEL = "claude-opus-4-8"      # infrequent, high-leverage planning
EXECUTOR_MODEL = "claude-sonnet-4-6"   # per-step action selection (the hot path)
ESCALATION_MODEL = "claude-opus-4-8"   # FR-MODEL-3: executor stuck -> escalate
VERIFIER_MODEL = "claude-haiku-4-5"    # high-volume yes/no post-condition checks

# Effort is the single biggest cost knob (FR-MODEL-2). Nested under output_config.
# Haiku 4.5 does NOT support `effort` and is never passed one.
ROUTER_EFFORT = "low"                  # classification is cheap; runs once per request
PLANNER_EFFORT = "high"                # Opus supports low|medium|high|xhigh|max
EXECUTOR_EFFORT = "low"                # routine steps run cheap
ESCALATION_EFFORT = "high"             # think harder when stuck

# --- Loop guards (NFR-6) ----------------------------------------------------
MAX_STEPS = 40                         # hard cap on actions per task
MAX_REPLANS = 3                        # re-plans before a forced ask_human
ESCALATE_AT = 2                        # consecutive failed verifies -> use Opus
REPLAN_AT = 3                          # consecutive failed verifies -> re-plan
HISTORY_WINDOW = 10                    # working-memory window (FR-MEM-1)
MAX_ELEMENTS = 200                     # cap interactive elements per observation

# Anti-spiral guards (the page isn't changing / we keep re-reading)
READ_NUDGE_AT = 3                      # consecutive reads -> tell it to decide
NO_PROGRESS_NUDGE = 3                  # page unchanged N steps -> nudge to finish
NO_PROGRESS_LIMIT = 6                  # page unchanged N steps -> stop, return partial
REPEAT_ACTION_LIMIT = 3                # identical action repeated N times -> stop
GATHERED_CAP = 3500                    # chars of read-text kept available to the model

# JS-heavy/SPA pages (e.g. x.com) often report a blank DOM right after navigation.
# Wait for interactive elements to render before acting/verifying, rather than
# acting on nothing (which stalled and produced false verify failures).
RENDER_RETRIES = 3                     # rescans while a page still looks blank
RENDER_WAIT_MS = 2500                  # ms to wait for render on each rescan

# --- Pricing for the cost estimate (NFR-4), $ per 1M tokens (in, out) -------
RATES = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}

SESSIONS_ROOT = Path(os.environ.get("AGENT_DATA_DIR", "./sessions"))

# --- Approval gate (P2) ----------------------------------------------------
REQUIRE_APPROVAL = True                # gate irreversible actions on human OK
# Accessible-name patterns that look like they COMMIT something irreversible.
# A click on a matching control is stopped for approval even if the model did
# not ask — a non-LLM safety net, so a mis-classifying model can't send/buy/
# delete on its own (in the spirit of FR-SEC-1's non-LLM injector/guard).
COMMIT_PATTERNS = [
    r"\bsend\b", r"\bsubmit\b", r"\bpublish\b", r"\bdelete\b", r"\bremove\b",
    r"\bbuy\b", r"\bpurchase\b", r"\bplace order\b", r"\bpay\b", r"\bcheckout\b",
    r"\bconfirm\b", r"\bbook\b", r"\bschedule\b", r"\bsave changes\b",
    r"\bsave contact\b", r"\bsave & close\b",
]

# --- Session reuse (P1): persistent browser profiles -----------------------
# A named profile is a dedicated user-data-dir under here; cookies/localStorage
# survive across runs, so you log into a site (Gmail, a CRM) once by hand and
# the agent reuses that authenticated session next time (FR-SEC-2).
PROFILES_ROOT = SESSIONS_ROOT / "profiles"
# Real installed Chrome ("chrome") is far less likely to be blocked by login
# providers than bundled Chromium. Override per-run with --chrome / --channel.
DEFAULT_CHANNEL = os.environ.get("AGENT_BROWSER_CHANNEL") or None
