"""Tunable configuration for the P0 agent loop.

Model strategy follows FS-BA-001 §2 (tiered routing). Loop guards follow NFR-6.
All values are plain module attributes so they can be overridden from run.py.
"""
import json
import os
from pathlib import Path

# --- Tiered model routing (FR-MODEL-1) -------------------------------------
# Every tier's model + effort is overridable via env so the same code runs against
# any account's model line-up (no hardcoded IDs). Defaults match the tiered
# routing strategy; AGENT_* wins where set.
ROUTER_MODEL = os.environ.get("AGENT_ROUTER_MODEL", "claude-sonnet-4-6")     # Sparrow intent/action router (once per request)
PLANNER_MODEL = os.environ.get("AGENT_PLANNER_MODEL", "claude-opus-4-8")     # infrequent, high-leverage planning
EXECUTOR_MODEL = os.environ.get("AGENT_EXECUTOR_MODEL", "claude-sonnet-4-6") # per-step action selection (the hot path)
ESCALATION_MODEL = os.environ.get("AGENT_ESCALATION_MODEL", "claude-opus-4-8")  # FR-MODEL-3: executor stuck -> escalate
VERIFIER_MODEL = os.environ.get("AGENT_VERIFIER_MODEL", "claude-haiku-4-5")  # high-volume yes/no post-condition checks

# Effort is the single biggest cost knob (FR-MODEL-2). Nested under output_config.
# Haiku 4.5 does NOT support `effort` and is never passed one (so no VERIFIER_EFFORT;
# llm._json_call wraps effort in _PARAM_FALLBACK for models that reject it anyway).
ROUTER_EFFORT = os.environ.get("AGENT_ROUTER_EFFORT", "low")                 # classification is cheap; runs once per request
PLANNER_EFFORT = os.environ.get("AGENT_PLANNER_EFFORT", "high")             # Opus supports low|medium|high|xhigh|max
EXECUTOR_EFFORT = os.environ.get("AGENT_EXECUTOR_EFFORT", "low")            # routine steps run cheap
ESCALATION_EFFORT = os.environ.get("AGENT_ESCALATION_EFFORT", "high")       # think harder when stuck

# --- Ghost browser (agent view streamed into chat; no screen takeover) ------
# 'hidden'   — headed Chrome parked off-screen (logins keep working; reveal it
#              via POST /agent/ghost/reveal for a sign-in handoff)
# 'headless' — no window at all (some login providers dislike headless)
# 'off'      — legacy behavior: a visible window on the user's screen
GHOST_MODE = os.environ.get("QUILL_GHOST_BROWSER", "hidden").strip().lower()
if GHOST_MODE not in ("off", "hidden", "headless"):
    GHOST_MODE = "hidden"

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

# --- Executor vision (FR-PERC): DOM + screenshot ---------------------------
# The executor reasons over the accessibility-tree text by default. When that
# view is thin — a near-blank/canvas page, image-only buttons — or when it's
# stuck, we also send the page screenshot so Claude can read the pixels (its own
# built-in OCR), no separate OCR engine needed. Adaptive by default to keep the
# per-step hot path cheap; flip AGENT_VISION_ALWAYS=1 to send every step.
EXECUTOR_VISION = os.environ.get("AGENT_EXECUTOR_VISION", "1") not in ("0", "false", "False")
VISION_ALWAYS = os.environ.get("AGENT_VISION_ALWAYS", "0") not in ("0", "false", "False")
VISION_SPARSE_AT = int(os.environ.get("AGENT_VISION_SPARSE_AT", "6"))  # <N elements -> attach shot

# --- Pixel fallback for graphics surfaces (canvas games, maps, editors) -----
# When a <canvas>/<video>/embed dominates the viewport, nothing inside it can
# ever have an element_id — the agent gets click_at / drag / press_key, confined
# to that surface's rectangle, grounded on the attached screenshot. Off => the
# agent can only look at such pages, never act on them.
BROWSER_PIXEL = os.environ.get("AGENT_BROWSER_PIXEL", "1") not in ("0", "false", "False")
# Longest edge of the grounding screenshot. Kept under Claude's 1568px vision
# resize so the image the model measures is the image we map coordinates from.
PIXEL_SHOT_MAX_EDGE = int(os.environ.get("AGENT_PIXEL_SHOT_MAX_EDGE", "1400"))
# Slack (CSS px) allowed outside the surface rect — canvas borders are fuzzy.
PIXEL_EDGE_PAD = int(os.environ.get("AGENT_PIXEL_EDGE_PAD", "8"))
# Mouse-move steps in a drag; too few and canvas apps miss the drag entirely.
PIXEL_DRAG_STEPS = int(os.environ.get("AGENT_PIXEL_DRAG_STEPS", "24"))

# --- Perception depth on JS-wired pages -------------------------------------
# The SCAN_JS pointer-cursor sweep is a heuristic; CDP can ask Chrome directly
# which elements have click listeners (DOMDebugger.getEventListeners) — the
# ground truth the sweep approximates. Runs only when the semantic scan is
# sparse, main frame only, and degrades silently when CDP is unavailable.
CDP_LISTENERS = os.environ.get("AGENT_CDP_LISTENERS", "1") not in ("0", "false", "False")
CDP_SPARSE_AT = int(os.environ.get("AGENT_CDP_SPARSE_AT", "8"))    # skip on busy pages
CDP_MAX_NODES = int(os.environ.get("AGENT_CDP_MAX_NODES", "300"))  # listener probes per scan

# A `done` whose result is about a DIFFERENT task than the goal (observed
# live: asked to play solitaire, declared done with an X-feed summary) gets
# one cheap goal-vs-result check; the first drifting done is rejected with a
# nudge instead of accepted. Honest failure reports still pass.
DONE_CHECK = os.environ.get("AGENT_DONE_CHECK", "1") not in ("0", "false", "False")

# --- Learning: post-mortems + step distillation ------------------------------
# On a non-success run a cheap model writes 1-3 one-line lessons from the
# trajectory; they accumulate in procedural memory next to the verify notes.
POSTMORTEM = os.environ.get("AGENT_POSTMORTEM", "1") not in ("0", "false", "False")
# Append (observation -> action, verified) pairs per executor step to
# sessions/agent_distill.jsonl — the imitation-learning substrate for the
# local rung (same idea as data/escalate_distill.jsonl on the text side).
DISTILL = os.environ.get("AGENT_DISTILL", "1") not in ("0", "false", "False")
DISTILL_OBS_CAP = int(os.environ.get("AGENT_DISTILL_OBS_CAP", "6000"))  # chars of prompt kept

# JS-heavy/SPA pages (e.g. x.com) often report a blank DOM right after navigation.
# Wait for interactive elements to render before acting/verifying, rather than
# acting on nothing (which stalled and produced false verify failures).
RENDER_RETRIES = 3                     # rescans while a page still looks blank
RENDER_WAIT_MS = 2500                  # ms to wait for render on each rescan

# --- API resilience: transient 429 / 5xx / 529 handling --------------------
# The Anthropic SDK retries connection errors, 408/409/429, and >=500 (incl. 529
# overloaded_error) with exponential backoff, honoring the server's retry-after
# header. Its default is only 2 retries, which a sustained overload can blow
# through — surfacing a raw OverloadedError to the user. Give it more headroom.
LLM_MAX_RETRIES = int(os.environ.get("QUILL_LLM_MAX_RETRIES", "6"))

# --- Pricing for the cost estimate (NFR-4), $ per 1M tokens (in, out) -------
# De-duplicated: the numbers live in data/model_prices.json (shared with
# app/services/model_log.PRICES) so the two can't drift. Override the whole file
# with QUILL_MODEL_PRICES=/path/to.json. The literal below is only a fail-safe
# fallback if the file is missing/unreadable; a parity test pins them together.
_RATES_FALLBACK = {
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def _load_model_prices(fallback):
    """Load {model_id: (in, out)} from data/model_prices.json (or QUILL_MODEL_PRICES).
    Fails safe to `fallback` on any problem, so a bad/missing file never breaks the
    agent — cost just falls back to the built-in table."""
    raw = os.environ.get("QUILL_MODEL_PRICES")
    path = Path(raw) if raw else Path(__file__).resolve().parent.parent / "data" / "model_prices.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        out = {}
        for k, v in data.items():
            if k.startswith("_") or not isinstance(v, (list, tuple)) or len(v) != 2:
                continue
            out[k] = (float(v[0]), float(v[1]))
        return out or dict(fallback)
    except Exception:
        return dict(fallback)


RATES = _load_model_prices(_RATES_FALLBACK)

SESSIONS_ROOT = Path(os.environ.get("AGENT_DATA_DIR", "./sessions"))

# --- Dry-run posture (Track B #8) ------------------------------------------
# How far a run is allowed to go, independent of the task's mode. A safety lever
# for demos and cautious runs. Set per-run (Agent(dry_run=...) / run_goal) or
# globally via AGENT_DRY_RUN. Levels, least -> most permissive:
#   plan     : route + plan, then stop and return the plan (no browser actions).
#   navigate : read/navigate/scroll only — no type/click/select (safe browsing).
#   draft    : prepare freely, but STOP at the first commit gate and return the
#              packet WITHOUT prompting (safe demo — never asks to send).
#   approval : prepare, and pause for human approval at each commit gate (default).
#   full / autonomous : execute the full task without approval prompts; stops when
#              the agent calls done (or step cap). Use for trusted one-shot tasks.
DRY_RUN_LEVELS = ("plan", "navigate", "draft", "approval", "full", "autonomous")
AUTONOMOUS_LEVELS = frozenset({"full", "autonomous"})
DRY_RUN = os.environ.get("AGENT_DRY_RUN", "approval").strip().lower()
if DRY_RUN not in DRY_RUN_LEVELS:
    DRY_RUN = "approval"

# --- Approval gate (P2) ----------------------------------------------------
REQUIRE_APPROVAL = True                # gate irreversible actions on human OK
# Approval binding (plan 0.4 → default-on for 5.2): at the irreversible commit
# click, re-hash the about-to-execute args and require hash == packet.payload_hash
# and now < expires_at.
#   off      — disabled
#   shadow   — log drift/expiry, allow the click
#   enforce  — hard-stop + re-ask with diff on mismatch (code default)
_APPROVAL_BIND_RAW = os.environ.get("QUILL_APPROVAL_BIND", "enforce").strip().lower()
if _APPROVAL_BIND_RAW in ("0", "off", "false", "no"):
    APPROVAL_BIND = "off"
elif _APPROVAL_BIND_RAW in ("shadow", "log"):
    APPROVAL_BIND = "shadow"
elif _APPROVAL_BIND_RAW in ("enforce", "on", "1", "true", "yes"):
    APPROVAL_BIND = "enforce"
else:
    APPROVAL_BIND = "enforce"
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
