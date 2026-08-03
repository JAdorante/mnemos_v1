"""The agent loop: observe -> act -> verify, with escalation and re-planning.

`Agent` holds a persistent browser + session so goals can be run back-to-back
(the chat use case): the browser stays on whatever page the last task left it,
and a short transcript is fed forward so follow-ups ("now summarize that") work.

Recovery ladder (FR-MODEL-3, FR-ACT-3, §9):
failed verify -> retry-with-wait -> escalate Sonnet->Opus -> re-plan -> ask_human.
"""
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit, parse_qsl

from . import config as cfg
from .browser import BrowserDriver
from .credentials import get as get_creds, host_from_url, inject_login, try_save_from_reply
from .failures import INSTRUCT, LOGIN, REPLAN, STOP, classify
from .llm import LLM
from .memory import Memory, redact
from .modes import resolve_mode
from .perception import render_observation, signature
from .provider_tips import tips_for_url


def _plan_text(plan):
    steps = plan.get("steps", [])
    lines = [
        f"{i + 1}. {s['description']}  (done when: {s.get('success_criteria', '')})"
        for i, s in enumerate(steps)
    ]
    if plan.get("notes"):
        lines.append(f"Notes: {plan['notes']}")
    return "\n".join(lines) if lines else "(no explicit plan)"


def _history_text(hist):
    if not hist:
        return "(none yet)"
    out = []
    for h in hist[-cfg.HISTORY_WINDOW:]:
        line = f"- step {h['step']}: {h['action']}({json.dumps(h['args'])}) -> {h['result']}"
        if h.get("verified") is False:
            line += f"  [verify FAILED: {h.get('vreason', '')}]"
        if h.get("read_text"):
            line += "\n    read: " + h["read_text"][:600]
        out.append(line)
    return "\n".join(out)


def _route_text(r):
    flags = []
    surf = r.get("surface")
    if surf and surf not in ("none",):
        flags.append(surf)
    elif r.get("requires_browser"):
        flags.append("browser")
    if r.get("requires_user_approval"):
        flags.append("needs approval")
    tail = f"  [{', '.join(flags)}]" if flags else "  [no web action]"
    site = f" · {r['site']}" if r.get("site") else ""
    return (f"Route: intent={r.get('intent')} → {r.get('tool')}{site}{tail}\n"
            f"       {r.get('rationale', '')}")


# --- the learning layer (procedural memory) --------------------------------
# After a goal finishes we distill its trajectory into a page-independent
# "recipe" (the winning path) plus the distinct verify-failure lessons, key it
# by (intent, site), and feed it back to the planner next time. Element ids are
# per-page and useless later, so a step is generalized to its action + the
# element's accessible name; navigate URLs keep host+path+param-keys but drop
# the values (which carry this run's specifics, e.g. a particular recipient).

def _generalize_url(url):
    try:
        u = urlsplit(url or "")
        if not u.netloc:
            return url
        base = f"{u.scheme or 'https'}://{u.netloc}{u.path}".rstrip("/")
        keys = [k for k, _ in parse_qsl(u.query)]
        return base + (f" (params: {', '.join(dict.fromkeys(keys))})" if keys else "")
    except Exception:
        return url


def _norm_site(site, url):
    """Prefer the router's site label; fall back to the page host."""
    s = (site or "").strip().lower()
    if s:
        return s
    try:
        host = urlsplit(url or "").netloc.lower().split(":")[0]
        return (host[4:] if host.startswith("www.") else host) or "web"
    except Exception:
        return "web"


def _distill(hist, status: str | None = None):
    """Trajectory -> (recipe, failure_notes). The recipe keeps steps that moved
    the task forward; for messaging/SPA walls we also keep successful reads.
    Failure notes include structural lessons on stall stops."""
    recipe, notes = [], []
    for h in hist:
        act = h.get("action")
        if h.get("verified") is False and h.get("vreason"):
            notes.append(h["vreason"].strip())
        if act == "navigate":
            recipe.append(f"navigate → {_generalize_url((h.get('args') or {}).get('url', ''))}")
        elif act in ("click", "type", "select") and h.get("verified"):
            tgt = (h.get("target") or "").strip()
            recipe.append(f"{act} “{tgt}”" if tgt else act)
        elif act == "read" and h.get("verified"):
            recipe.append("read page text")
    if status in ("stopped_repeat", "stopped_no_progress"):
        notes.append(
            "SPA/chat UIs: message text is often not interactive — use Visible "
            "page text or `read` (no element_id); do not re-click the same "
            "conversation row")
    deduped = [s for i, s in enumerate(recipe) if i == 0 or s != recipe[i - 1]]
    return deduped, list(dict.fromkeys(notes))


def _lessons_text(skill):
    """Render a recalled skill as a compact block appended to the planner's
    input. Empty string when there's nothing learned yet."""
    if not skill or (not skill.get("recipe") and not skill.get("failure_notes")):
        return ""
    out = ["\n\nLESSONS FROM PAST RUNS (procedural memory — reuse what worked, "
           "avoid what failed):"]
    if skill.get("recipe"):
        rate = f"{skill['successes']}/{skill['attempts']}"
        best = skill.get("best_steps")
        tail = f", best {best} steps" if best else ""
        out.append(f"Winning path last time (succeeded {rate}{tail}):")
        out += [f"  {i + 1}. {s}" for i, s in enumerate(skill["recipe"])]
    if skill.get("failure_notes"):
        out.append("Known pitfalls to avoid:")
        out += [f"  - {n}" for n in skill["failure_notes"]]
    return "\n".join(out)


_COMMIT_RE = [re.compile(p, re.I) for p in cfg.COMMIT_PATTERNS]
# Compose affordances whose accessible name contains "send" but are INPUTS,
# not commit buttons — Snapchat Web's "Send a chat" textbox was false-firing
# the approval gate every turn (live, July 22 2026).
_COMPOSE_ROLES = frozenset({
    "textbox", "searchbox", "combobox", "textarea",
})
_COMPOSE_NAME_RE = re.compile(
    r"\bsend a (?:chat|message|snap|dm|text)\b|"
    r"\b(?:type|write|enter|compose|draft) (?:a )?(?:message|chat|snap|reply)\b|"
    r"\bmessage\b.{0,20}\bhere\b|"
    r"\bstart (?:a )?(?:chat|conversation|message)\b|"
    r"\bnew message\b|"
    r"\bwrite (?:something|here)\b",
    re.I)
# Interpret an approval reply by intent, not exact match — but let any negation
# win, so "don't send" / "no, cancel" are never read as approval.
_NO_WORDS = ("no", "dont", "don't", "cancel", "stop", "wait", "nope", "abort",
             "never", "hold", "nvm", "nevermind")
_YES_WORDS = ("approve", "approved", "yes", "yeah", "yep", "yup", "ok", "okay",
              "sure", "send", "go ahead", "go", "do it", "confirm", "proceed",
              "sounds good", "please do", "click send")


class _SpiralGuard:
    """Detects action spirals: consecutive exact repeats (A-A-A) AND
    oscillations (A-B-A-B — same action retried from the same page state,
    which a last-action-only counter never trips on)."""

    def __init__(self):
        self.last = None
        self.same = 0
        self._pairs = {}

    def observe(self, act_sig, state_key):
        """Record an action about to run; return the spiral count (max of the
        consecutive-repeat streak and prior visits of this state+action pair)."""
        self.same = self.same + 1 if act_sig == self.last else 0
        self.last = act_sig
        pair = f"{act_sig}@{state_key}"
        self._pairs[pair] = self._pairs.get(pair, 0) + 1
        return max(self.same, self._pairs[pair] - 1)

    def forgive(self, act_sig, state_key):
        """After a recovery (e.g. auto-read), grant the action a fresh start."""
        self.same = 0
        self.last = None
        self._pairs.pop(f"{act_sig}@{state_key}", None)


def _element(scan, eid):
    for e in scan.get("elements", []):
        if e.get("id") == eid:
            return e
    return {}


def _element_name(scan, eid):
    return _element(scan, eid).get("name", "")


def _looks_irreversible(name, extra=(), *, role=None, editable=False):
    """True if the accessible name looks like a commit control. `extra` adds the
    active mode's approval patterns on top of the global safety net.

    Editable / textbox roles and compose-placeholder names (e.g. Snapchat's
    "Send a chat") are never treated as commits — focusing the composer is
    preparation, not sending.
    """
    if not name:
        return False
    if editable or (role or "").strip().lower() in _COMPOSE_ROLES:
        return False
    if _COMPOSE_NAME_RE.search(name):
        return False
    return (any(r.search(name) for r in _COMMIT_RE)
            or any(r.search(name) for r in extra))


# Heuristic for "the human just pasted a credential where it doesn't belong":
# a single token, 6-64 chars, letters+digits, no spaces/@/url. Deliberately
# conservative so real one-word answers (names, "continue") aren't redacted.
_SECRETISH = re.compile(r"^(?=.{6,64}$)(?=.*[A-Za-z])(?=.*\d)\S+$")


def _looks_like_secret(t):
    t = (t or "").strip()
    if not t or " " in t or "@" in t or t.lower().startswith("http"):
        return False
    return bool(_SECRETISH.match(t))


def _scrub(ans, log):
    """Redact a chat reply that looks like a pasted credential (FR-SEC-1)."""
    if try_save_from_reply(ans):
        return ans  # intentional save directive — handled by caller
    if _looks_like_secret(ans):
        log("   [!] that looked like a password/secret — I did NOT store or send "
            "it. To save credentials for next time, use:\n"
            "      /save-creds <site> <username> <password>\n"
            "   (stored locally in .credentials.env, never sent to the model).")
        return "[redacted: use /save-creds to store login details safely]"
    return ans


def _login_save_hint(host: str) -> str:
    site = host or "example.com"
    return (
        f"To remember this login for {site}, reply:\n"
        f"  /save-creds {site} <username> <password>\n"
        "(saved to .credentials.env on this machine — never sent to the model.)"
    )


def _is_yes(text):
    t = (text or "").strip().lower()
    if not t:
        return False
    if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in _NO_WORDS):
        return False  # any negation cancels
    return any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in _YES_WORDS)


def _ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return "(no console input available)"


# --- structured, source-grounded approval packets (FR-SEC / approval UX) ----
# An irreversible action is presented as an inspectable packet — what will
# happen, the exact content, WHY (grounded in vinceo.ai memory), and the SOURCE —
# instead of a bare "Can I send this?". The user replies Approve / Edit / Cancel.

_PACKET_FIELDS = [
    ("action", "Action"),
    ("to", "To"),
    ("subject", "Subject"),
    ("body", "Body"),
    ("why", "Why"),
    ("source", "Source"),
]


def _render_packet(fields):
    """Render the structured approval fields as a readable packet block. Falls
    back to summary/details when the model gave only the flat form."""
    lines = []
    for key, label in _PACKET_FIELDS:
        val = (fields.get(key) or "").strip()
        if not val:
            continue
        if "\n" in val or len(val) > 60:      # multi-line content (a body) on its own lines
            lines.append(f"{label}:\n{val}")
        else:
            lines.append(f"{label}: {val}")
    body = "\n\n".join(lines)
    extra = (fields.get("details") or "").strip()
    if extra and extra not in body:
        body += (("\n\n" if body else "") + extra)
    return body


def _classify_approval(reply):
    """approve / cancel / edit. A clear yes approves; a clear no (or empty)
    cancels; anything else is treated as edit feedback to revise the draft."""
    t = (reply or "").strip()
    if not t:
        return "cancel"
    if _is_yes(t):
        return "approve"
    if any(re.search(r"\b" + re.escape(w) + r"\b", t.lower()) for w in _NO_WORDS):
        return "cancel"
    return "edit"


def _risk_from_route(route):
    """Coarse risk tier for the run log (Phase 5). The route's approval flag is
    the strongest signal; intent keywords sharpen send/buy/delete vs. read/draft.
    A precise tier is the Planner's job later — this just labels the run."""
    route = route or {}
    intent = (route.get("intent") or "").lower()
    if any(k in intent for k in ("delete", "remove", "buy", "purchase", "pay", "order")):
        return "high"
    if route.get("requires_user_approval"):
        return "high"
    if any(k in intent for k in ("send", "submit", "post", "book", "schedule")):
        return "medium"
    if not route.get("requires_browser", True):
        return "low"
    return "low"


class _NullRecorder:
    """No-op stand-in so the agent runs unrecorded (standalone CLI / one-shot /
    tests) without littering call sites with None-checks. The real Recorder
    (app.services.agent_log) is injected by the vinceo.ai bridge."""

    current_run_id = None

    def start_run(self, *a, **k):
        return None

    def annotate_run(self, *a, **k):
        pass

    def finish_run(self, *a, **k):
        pass

    def record_packet(self, *a, **k):
        return None

    def record_from_packet(self, *a, **k):
        return None

    def record_decision(self, *a, **k):
        pass

    def record_feedback(self, *a, **k):
        pass

    def record_steps(self, *a, **k):
        pass


class Agent:
    """A persistent browser session you can run goals against repeatedly."""

    def __init__(self, headless=True, start_url=None, on_log=None, on_ask=None,
                 profile=None, channel=None, cdp_url=None, memory_provider=None,
                 dry_run=None, recorder=None, source_provider=None,
                 session_replies=None):
        # progress + human-handoff are routable: the CLI prints/inputs, the web
        # UI pushes to the page. Defaults keep run.py / chat.py unchanged.
        self._log = on_log or (lambda s: print(s))
        self._ask_fn = on_ask or (lambda q: _ask((q + "\n" if q else "") + "  your reply > "))
        # Optional vinceo.ai bridge: (goal) -> a text block of relevant memories that
        # ground the task in what vinceo.ai has seen/heard. None = standalone agent.
        self._memory_provider = memory_provider
        # Optional vinceo.ai source bridge: (fact_id) -> the verbatim stored quote +
        # clip that grounds an action. Lets the approval packet's Source line be the
        # real DB fact, not model-written text. Injected like memory_provider to keep
        # browser_agent decoupled from app.*; set per run via run_goal(source_fact_id=).
        self._source_provider = source_provider
        self._source_fact_id = None
        self._grounded_source_ids = None
        # Optional shared cross-lane replies: () -> list[str] of recent result
        # texts across ALL agent instances (the bridge runs browser goals and
        # desktop/phone goals on SEPARATE Agents — "the message you just told
        # me" usually points at the OTHER lane's answer). None = standalone.
        self._session_replies = session_replies
        # Optional vinceo.ai agent-run recorder (Phase 5). Injected like the memory
        # provider so browser_agent stays importable without app.*; a no-op stub
        # keeps every record_* call safe when the agent runs standalone.
        self._recorder = recorder or _NullRecorder()
        # Default dry-run posture for this session (Track B #8); overridable per
        # run_goal(). Falls back to the AGENT_DRY_RUN env default.
        self.dry_run = dry_run if dry_run in cfg.DRY_RUN_LEVELS else cfg.DRY_RUN
        self.llm = LLM()
        self.mem = Memory(cfg.SESSIONS_ROOT / "episodic.db")
        self.session_id = uuid.uuid4().hex[:12]
        self.sdir = cfg.SESSIONS_ROOT / self.session_id
        (self.sdir / "shots").mkdir(parents=True, exist_ok=True)
        (self.sdir / "ax").mkdir(parents=True, exist_ok=True)
        # A named profile persists login across runs (FR-SEC-2). Resolve a bare
        # name to a dir under PROFILES_ROOT; an absolute/relative path is used as-is.
        udir = None
        self.profile = profile
        if profile:
            p = Path(profile)
            udir = p if (p.is_absolute() or p.parts[:1] in ((".",), ("..",))) else cfg.PROFILES_ROOT / profile
        self.channel = channel or cfg.DEFAULT_CHANNEL
        self._start_url = start_url
        self.driver = BrowserDriver(headless=headless, user_data_dir=udir,
                                    channel=self.channel, cdp_url=cdp_url)
        # Lazy browser start: desktop / phone_link goals must not open Playwright
        # (the shared profile is often already held by Exec.AI → "Opening in
        # existing browser session"). Browser starts on first web goal.
        self._browser_started = False
        self.transcript = []   # [{goal, result}] — conversational context
        self.step = 0          # global step counter across all goals
        self.last_steps = 0
        self.last_replans = 0
        self.last_route = None  # the most recent vinceo.ai routing envelope
        self.last_mode = None   # the resolved agent mode for the last request
        self.last_study_mode = None  # sticky student persona {id, label, ...}
        self.last_dry_run = None
        self._autonomous_run = False
        self.mem.start_session(self.session_id, "(interactive session)", {})

    def _ensure_browser(self) -> None:
        """Start Playwright only when a web goal actually needs it."""
        if self._browser_started:
            return
        self.driver.start()
        self._browser_started = True
        if self.driver.cdp_url:
            self._log(f"Attached to your running Chrome at {self.driver.cdp_url} — "
                      "using your existing logged-in session (I won't close your browser).")
        elif self.profile:
            self._log(f"Using persistent profile '{self.profile}' — "
                      "log in once here and the session is reused next run.")
        if self._start_url:
            try:
                self.driver.goto(self._start_url)
            except Exception as e:
                print(f"[warn] start URL failed: {e}")

    # --- navigation helpers ------------------------------------------------
    def current_url(self):
        if not self._browser_started:
            return None
        try:
            return self.driver.scan().get("url")
        except Exception:
            return None

    def open(self, url):
        self._ensure_browser()
        if not url.startswith("http"):
            url = "https://" + url
        self.driver.goto(url)

    def cost(self):
        return self.llm.cost()

    def _maybe_stored_login(self, scan: dict) -> bool:
        """If we have saved creds for this host, inject them (never via the LLM)."""
        url = (scan or {}).get("url") or self.current_url() or ""
        host = host_from_url(url)
        creds = get_creds(host)
        if not creds:
            return False
        if inject_login(self.driver, scan, creds):
            self._log(f"   filled saved login for {host} from .credentials.env")
            return True
        return False

    # Session conversation window — follow-ups ("text that", "what you just said")
    # need more than a couple of clipped turns.
    _TRANSCRIPT_TURNS = 8
    _TRANSCRIPT_LAST_CHARS = 1200
    _TRANSCRIPT_OLDER_CHARS = 500
    _SESSION_SMS_MAX_CHARS = 1600

    # Agent-authored prompts/statuses — text the user SAW but that is never
    # "the message you just told me" (observed live: an empty-body SMS ask
    # "What would you like me to text …?" became the resolved body).
    _NOT_A_REPLY_PREFIXES = (
        "what would you like", "who would you like", "which one",
        "send this text message", "sms send cancelled", "offer expired",
        "cancelled", "canceled", "error", "phone_link_disabled",
        "no answer", "(",
    )

    @classmethod
    def _substantive_reply(cls, text: str) -> bool:
        low = (text or "").strip().lower()
        if not low:
            return False
        if "reply 'approve'" in low or "reply 'yes'" in low:
            return False
        if "unavailable" in low[:60]:
            return False
        return not any(low.startswith(s) for s in cls._NOT_A_REPLY_PREFIXES)

    def _last_assistant_text(self) -> str:
        """Latest substantive assistant reply, for anaphora. The shared
        cross-lane pool (bridge-injected, globally ordered across the browser
        and desktop/phone Agents) is preferred; this agent's own transcript is
        the standalone fallback."""
        provider = getattr(self, "_session_replies", None)
        pools: list[list[str]] = []
        if provider:
            try:
                pools.append([str(x or "") for x in provider()])
            except Exception:
                pass
        pools.append([str((t or {}).get("result") or "")
                      for t in (self.transcript or [])])
        for pool in pools:
            for text in reversed(pool):
                text = text.strip()
                if self._substantive_reply(text):
                    return text
        return ""

    def _transcript_text(self, goal: str = ""):
        """Session turns for the model. Prefer this over memory for follow-ups."""
        if not self.transcript:
            return ""
        turns = self.transcript[-self._TRANSCRIPT_TURNS:]
        out = [
            "SESSION CONVERSATION (prefer for follow-ups / "
            "\"just said\" / \"that message\" — not personal memory):",
        ]
        last_i = len(turns) - 1
        for i, t in enumerate(turns):
            cap = (self._TRANSCRIPT_LAST_CHARS if i == last_i
                   else self._TRANSCRIPT_OLDER_CHARS)
            result = str(t.get("result") or "")
            out.append(f"- you asked: {t.get('goal', '')}")
            out.append(f"  result: {result[:cap]}")
        last = self._last_assistant_text()
        if last:
            out.append("")
            out.append("LAST_ASSISTANT_REPLY (use when the user refers to "
                       "\"that\" / \"the message you just told me\" / "
                       "\"what you just said\"):")
            out.append(last[:self._TRANSCRIPT_LAST_CHARS])
        return "\n".join(out) + "\n\n"

    def _session_followup(self, goal: str) -> bool:
        """True when the goal likely refers to prior turns in this chat."""
        try:
            from browser_agent.phone_parse import refers_to_prior_reply
            if refers_to_prior_reply(goal or ""):
                return True
        except Exception:
            pass
        g = (goal or "").lower()
        cues = ("just said", "just told", "just asked", "earlier",
                "what we just", "what did we just", "recall what",
                "remember what", "that message", "this message",
                "your last", "you just")
        return any(c in g for c in cues)

    def _build_ctx(self, goal: str) -> str:
        """Memory + session transcript; session first on follow-up goals."""
        mem = self._memory_context(goal)
        session = self._transcript_text(goal)
        if self._session_followup(goal) and session:
            return session + mem
        return mem + session

    def _memory_context(self, goal):
        """Pull relevant vinceo.ai memories for this goal, as a prependable block."""
        if not self._memory_provider:
            return ""
        try:
            block = self._memory_provider(goal)
        except Exception as exc:
            self._log(f"   (memory lookup skipped: {exc})")
            return ""
        if block:
            self._log("   grounded the task in relevant vinceo.ai memories.")
            return block.rstrip() + "\n\n"
        return ""

    def _resolve_session_message(self, parsed: dict, goal: str) -> None:
        """Fill anaphoric/empty SMS body from the last assistant reply in-session."""
        if not isinstance(parsed, dict):
            return
        action = (parsed.get("action") or "").strip()
        if action not in ("send_sms", "reply"):
            return
        msg = (parsed.get("message") or "").strip()
        try:
            from browser_agent.phone_parse import (is_anaphoric_body,
                                                    refers_to_prior_reply)
        except Exception:
            is_anaphoric_body = lambda _t: False  # noqa: E731
            refers_to_prior_reply = lambda _t: False  # noqa: E731
        goal_ref = refers_to_prior_reply(goal or "")
        msg_ref = (not msg) or is_anaphoric_body(msg) or refers_to_prior_reply(msg)
        if not (goal_ref or msg_ref):
            return
        # Concrete dictated body + non-anaphoric goal → leave alone.
        if msg and not is_anaphoric_body(msg) and not goal_ref:
            return
        prior = self._last_assistant_text()
        if not prior or is_anaphoric_body(prior):
            return
        filled = prior.strip()
        if len(filled) > self._SESSION_SMS_MAX_CHARS:
            filled = filled[: self._SESSION_SMS_MAX_CHARS - 1] + "…"
        parsed["message"] = filled
        parsed["_session_body"] = True
        self._log(f"   [session] resolved message body from prior reply "
                  f"({len(filled)} chars)")

    # --- vinceo.ai intent/action router (runs once per request) ----------------
    def route(self, user_request):
        """Classify a request into the vinceo.ai envelope without executing it."""
        r = self.llm.route(user_request, self._transcript_text(user_request))
        self.last_route = r
        return r

    # --- approval gate (P2) ------------------------------------------------
    def _grounded_fields(self, fields):
        """Return `fields` with the Source overridden by the verbatim fact from
        vinceo.ai's DB for this run — so the packet can only cite a fact that
        actually exists. No-op (returns a copy unchanged) when there's no fact id
        or no source provider, i.e. the standalone agent or an ungrounded goal."""
        fields = dict(fields or {})
        self._grounded_source_ids = None
        fid = getattr(self, "_source_fact_id", None)
        prov = getattr(self, "_source_provider", None)
        if fid and prov:
            try:
                src = prov(fid)
            except Exception:
                src = None
            if src and src.get("block"):
                fields["source"] = src["block"]
                self._grounded_source_ids = [src.get("fact_id", fid)]
        return fields

    def _approval_decision(self, summary, fields=None):
        """Present a source-grounded approval packet and read the decision.

        Returns (decision, feedback) where decision is 'approve' | 'cancel' |
        'edit'. On 'edit', feedback is the user's revision instruction, which the
        caller feeds back so the draft can be revised before re-asking."""
        if not cfg.REQUIRE_APPROVAL or getattr(self, "_autonomous_run", False):
            if getattr(self, "_autonomous_run", False):
                self._log(f"   [autonomous] auto-approved: {summary}")
            return "approve", ""
        self._log(f"[approval needed] {summary}")
        fields = dict(fields or {})
        # UI commit-gate (click "Send" looks irreversible): do NOT attach an
        # unrelated memory Source — that polluted Snapchat approvals with
        # random facts. Only ground when this is a structured draft packet.
        looks_ui_gate = bool(re.search(
            r"looks irreversible|click\s+[\"']", summary or "", re.I))
        has_draft = bool((fields.get("body") or fields.get("to")
                          or fields.get("subject") or "").strip())
        if looks_ui_gate and not has_draft:
            fields.pop("source", None)
            fields.pop("why", None)
            self._grounded_source_ids = None
        else:
            # Drop model-invented why/source on message goals unless we have a
            # real fact id for this run.
            intent = ((self.last_route or {}).get("intent") or "").lower()
            if re.search(r"send|message|chat|sms|email|dm", intent):
                if not getattr(self, "_source_fact_id", None):
                    fields.pop("why", None)
                    fields.pop("source", None)
            fields = self._grounded_fields(fields)
        packet = _render_packet(fields or {})
        # Persist the packet before asking (Phase 5): this is the source-grounded
        # unit the human is about to judge, attached to the current run.
        packet_id = self._recorder.record_packet(
            summary=summary, fields=fields or {}, goal=summary,
            execution_surface="browser", risk_level=_risk_from_route(self.last_route),
            source_fact_ids=self._grounded_source_ids)
        prompt = ("APPROVAL NEEDED — " + summary
                  + (("\n\n" + packet) if packet else "")
                  + "\n\nReply 'approve' to proceed, 'cancel' to stop, or tell me "
                  "what to change (e.g. \"change the subject to …\") to edit it first.")
        ans = self._ask_fn(prompt)
        decision = _classify_approval(ans)
        # Record the verdict. On 'edit' the revision text is the training signal
        # that previously evaporated once the run ended.
        self._recorder.record_decision(
            packet_id, decision, user_edit=(ans if decision == "edit" else None))
        self._log({"approve": "   approved",
                   "cancel": f"   declined ({ans!r})",
                   "edit": f"   edit requested ({ans!r})"}[decision])
        return decision, (ans if decision == "edit" else "")

    def _require_approval(self, summary, details=""):
        """Back-compat boolean gate (commit-click safety net). Treats an edit
        request as 'not approved' — the caller re-asks after revising."""
        decision, _ = self._approval_decision(summary, {"details": details})
        return decision == "approve"

    # --- desktop/OS control (guarded) --------------------------------------
    def _desktop(self):
        """Lazily build the guarded DesktopDriver, wiring its approval + log to
        this agent's handoff channel (so the web UI / CLI drives approvals).
        Returns None if desktop control is disabled or unavailable."""
        if getattr(self, "_desktop_driver", None) is not None:
            return self._desktop_driver
        import os

        if os.environ.get("QUILL_DESKTOP", "1") in ("0", "false", "False"):
            return None
        try:
            from desktop_agent import DesktopDriver
            from desktop_agent import config as dcfg
        except Exception as exc:
            self._log(f"   desktop control unavailable: {exc}")
            return None

        def _approve(summary, details="", action=None):
            # Approval is the LIVE human's — never memory/context (guardrail).
            # Allowlisted app launches auto-approve by default (still sandboxed by
            # the app allowlist + path jail). Other mutating verbs stay gated
            # unless this is an autonomous run within the desktop-autonomy ceiling.
            auto_launch = os.environ.get("QUILL_DESKTOP_AUTO_LAUNCH", "1") not in (
                "0", "false", "False")
            if (auto_launch and action == "launch_app"
                    and dcfg.desktop_autoapprove("launch_app")):
                self._log(f"   [auto-launch] {summary}")
                return True
            if getattr(self, "_autonomous_run", False):
                if action is None or dcfg.desktop_autoapprove(action):
                    self._log(f"   [autonomous:{dcfg.AGENT_AUTONOMY_DESKTOP}] "
                              f"auto-approved {action or ''}: {summary}")
                    return True
                self._log(f"   [autonomous] {action} exceeds desktop autonomy "
                          f"ceiling ({dcfg.AGENT_AUTONOMY_DESKTOP}); asking human")
            prompt = ("APPROVAL NEEDED — " + summary
                      + (("\n\n" + details) if details else "")
                      + "\nReply 'approve' to proceed, or anything else to cancel.")
            return _is_yes(self._ask_fn(prompt))

        self._desktop_driver = DesktopDriver(on_log=self._log, on_approve=_approve)
        return self._desktop_driver

    def _desktop_dispatch(self, d, name, args):
        """Translate one chosen tool call into a guarded DesktopDriver method."""
        from desktop_agent import guards

        if name == "make_dir":
            return d.make_dir(args.get("name", ""))
        if name == "write_file":
            return d.write_file(args.get("path", ""), args.get("content", ""),
                                project=args.get("project"))
        if name == "list_dir":
            return d.list_dir(args.get("name", ""))
        if name == "launch_app":
            largs = []
            proj = args.get("project")
            if proj:
                p = guards.safe_child(d.jail, proj)
                if p is None:
                    return {"ok": False, "detail": f"bad project name {proj!r}"}
                largs = [str(p)]
            return d.launch_app(args.get("app", ""), largs)
        if name == "run_command":
            cwd = None
            proj = args.get("project")
            if proj:
                p = guards.safe_child(d.jail, proj)
                cwd = str(p) if p else None
            return d.run_command(args.get("argv") or [], cwd=cwd)
        if name == "click_at":
            return d.click_at(args.get("x", 0), args.get("y", 0),
                              button=args.get("button", "left"))
        if name == "type_text":
            return d.type_text(args.get("text", ""))
        if name == "press_key":
            return d.press_key(args.get("key", ""))
        if name == "ui_scan":
            return d.ui_scan(args.get("app", ""), title=args.get("title", ""))
        if name == "ui_invoke":
            return d.ui_invoke(args.get("control_id"))
        if name == "ui_set_text":
            return d.ui_set_text(args.get("control_id"), args.get("text", ""))
        return {"ok": False, "detail": f"unknown desktop action {name!r}"}

    @staticmethod
    def _desktop_hist(hist):
        if not hist:
            return "(none yet)"
        return "\n".join(f"- {n}({json.dumps(a)}) -> {r}" for n, a, r in hist[-8:])

    def _run_desktop_goal(self, goal, ctx, level=None):
        """Observe(sandbox)->act loop over the guarded desktop tools. Every
        mutating action passes the human approval gate inside the driver."""
        level = level if level in cfg.DRY_RUN_LEVELS else self.dry_run
        auto = level in cfg.AUTONOMOUS_LEVELS
        d = self._desktop()
        if d is None:
            msg = "Desktop control is disabled or unavailable on this machine."
            self.transcript.append({"goal": goal, "result": msg})
            self.last_steps, self.last_replans = 0, 0
            return msg, "desktop_unavailable"
        d.new_task()
        if auto:
            self._log("Desktop task — autonomous mode (no approval prompts)")
        self._log(f"Desktop task — sandbox: {d.jail}")
        try:
            from desktop_agent import config as dcfg
            pixel_on = dcfg.PIXEL_UI and dcfg.PIXEL_VISION
        except Exception:
            pixel_on = False
        # Deterministic preflight: tell the planner what's installed/enabled/
        # possible BEFORE it acts, so it never loops on a refused UI action or a
        # missing app. Computed once (env is static for the task); injected below.
        pf_block = ""
        try:
            from desktop_agent import preflight as _pf
            pf = _pf.preflight(goal, autonomous=auto, jail=d.jail,
                               actions_used=d.actions)
            pf_block = _pf.format_preflight(pf) + "\n\n"
            self._log(_pf.summary_line(pf))
        except Exception:
            pass
        hist, status, result = [], "running", None
        asks = 0   # ask_human budget — stop instead of nagging
        cap = min(cfg.MAX_STEPS, 20 if pixel_on else 12)
        screen_tag = ""
        last_act_sig, same_act = None, 0
        denied_sigs: set[str] = set()
        for _ in range(cap):
            listing = d.list_dir()
            entries = listing.get("entries", []) if listing.get("ok") else []
            shot_bytes, screen_tag = None, ""
            if pixel_on:
                shot = d.screenshot_bytes()
                if shot.get("ok") and shot.get("image"):
                    shot_bytes = shot["image"]
                    screen_tag = f"\nSCREEN: {shot.get('width')}x{shot.get('height')} px " \
                                 "(click_at coordinates match this screenshot).\n"
            deny_note = ""
            if denied_sigs:
                deny_note = (
                    "\n\nUSER DENIED these actions — do NOT retry them. The app may "
                    "already be open (check the screenshot) or the user changed their "
                    "mind. Adapt: ui_scan the app and act via ui_invoke/ui_set_text, "
                    "or call ask_human.\nDenied: "
                    + "; ".join(sorted(denied_sigs)[:6])
                    + "\n"
                )
            auto_note = ""
            if auto:
                auto_note = (
                    "\n[AUTONOMOUS MODE — jailed file authoring and app launches run "
                    "without prompts; higher-risk actions (clicking/typing inside "
                    "apps, shell commands) still pause for approval unless enabled "
                    "(see the PREFLIGHT autonomy line). Call done with a short "
                    "summary when finished.]\n"
                )
            content = (
                ctx
                + pf_block
                + f"TASK:\n{goal}\n\n"
                f"SANDBOX (jail): {d.jail}\nCURRENT CONTENTS: {entries}\n"
                f"{screen_tag}{auto_note}{deny_note}\n"
                f"RECENT ACTIONS:\n{self._desktop_hist(hist)}\n\n"
                "Choose the next single action toward the task."
            )
            act = self.llm.choose_desktop_action(content, image=shot_bytes)
            name, args = act["name"], act.get("input") or {}
            act_sig = f"{name}:{json.dumps(args, sort_keys=True)}"
            if act_sig in denied_sigs and name not in ("ask_human", "done"):
                self._log(f"   blocked retry of denied action {name}")
                hist.append((name, args, "BLOCKED — user already denied this; pick another"))
                same_act = same_act + 1 if act_sig == last_act_sig else 1
                last_act_sig = act_sig
                if same_act >= 2:
                    result = ("Stopped — the same action was denied or blocked twice. "
                              "Reply in chat if you want me to try a different approach.")
                    status = "cancelled"
                    break
                continue
            self.step += 1
            tag = "  (+screenshot)" if shot_bytes else ""
            self._log(f"[desktop {self.step}] {name} {json.dumps(args)}{tag}")

            if name == "done":
                result, status = args.get("result", "Done."), "success"
                break
            if name == "ask_human":
                q = args.get("question", "(no question)")
                asks += 1
                if asks > 3:
                    result = ("I'm blocked without more input. What I still "
                              f"need: {q}\nSend the details in chat and I'll "
                              "pick the task back up.")
                    status = "needs_input"
                    break
                ans = _scrub(self._ask_fn(q), self._log)
                hist.append((name, args, f"human: {ans}"))
                same_act, last_act_sig = 0, None
                continue
            res = self._desktop_dispatch(d, name, args)
            detail = res.get("detail", "")
            if res.get("ok"):
                outcome = "ok: " + detail
                same_act, last_act_sig = 0, act_sig
            else:
                if detail == "denied":
                    denied_sigs.add(act_sig)
                    outcome = (f"USER DENIED {name} — do not retry; adapt or ask_human")
                else:
                    outcome = "refused: " + detail
                same_act = same_act + 1 if act_sig == last_act_sig else 1
                last_act_sig = act_sig
            hist.append((name, args, outcome))
            if same_act >= cfg.REPEAT_ACTION_LIMIT and name not in ("ask_human", "done"):
                self._log("   same desktop action repeated without progress; stopping.")
                result = result or (
                    "Stopped — I kept repeating an action you declined or that failed. "
                    "Say what you'd like instead (e.g. 'FL Studio is already open, "
                    "just click File > New').")
                status = "cancelled"
                break
        else:
            status = "stopped_step_cap"
        if status == "running":
            status = "stopped"
        result = result or "(desktop task ended without an explicit result)"
        self._recorder.record_steps([
            {"step_index": i, "action_type": n, "input": a, "output": r,
             "status": "done"}
            for i, (n, a, r) in enumerate(hist)
        ])
        self.transcript.append({"goal": goal, "result": result})
        self.last_steps, self.last_replans = len(hist), 0
        return result, status

    def _run_phone_link_goal(self, goal, ctx):
        """Parse a natural-language phone/SMS goal and drive the Phone Link app."""
        from app.services import phone_link as pl

        def _approve(summary, details=""):
            if getattr(self, "_autonomous_run", False):
                self._log(f"   [autonomous] auto-approved: {summary}")
                return True
            prompt = summary + ("\n\n" + details if details else "")
            prompt += ("\n\nReply 'approve' to proceed, or anything else to cancel.")
            return _is_yes(self._ask_fn(prompt))

        self._log("Phone Link task — parsing goal …")
        # Fast path: common "text <name> <body>" shapes skip the router LLM hop.
        try:
            from browser_agent.phone_parse import try_parse_phone_goal
            parsed = try_parse_phone_goal(goal)
        except Exception:
            parsed = None
        if parsed:
            self._log(f"Phone plan (heuristic): {json.dumps(parsed)}")
        else:
            parsed = self.llm.parse_phone_goal(goal, ctx)
            self._log(f"Phone plan: {json.dumps(parsed)}")
        self._resolve_session_message(parsed, goal)
        self._correct_phone_plan(parsed, ctx)
        result, status = pl.execute_goal(
            goal, parsed, on_log=self._log, on_approve=_approve)
        self.transcript.append({"goal": goal, "result": result})
        self.last_steps, self.last_replans = 1, 0
        return result, status

    def _correct_phone_plan(self, parsed, ctx):
        """Fix voice-transcription typos in a parsed phone plan before it acts.

        Speech-to-text guesses ("text Abby" -> recipient "Abby Nagle") don't match
        real contacts, and a dictated body can carry homophone/number errors. We
        ground the recipient against the actual Phone Link contacts and clean the
        body, mutating `parsed` in place and stashing a human-readable list in
        parsed['_corrections'] so the approval card shows exactly what changed.
        Entirely best-effort — any failure leaves the plan as-dictated."""
        from app.services import phone_link as pl

        corrections = []
        action = (parsed.get("action") or "").lower()
        recipient = (parsed.get("recipient") or "").strip()
        message = (parsed.get("message") or "").strip()

        # 1) recipient -> closest real contact
        if recipient:
            try:
                contacts = (pl.list_contacts() or {}).get("contacts") or []
            except Exception as exc:
                self._log(f"   [voice] contact list unavailable ({exc})")
                contacts = []
            # #11: supplement the phone scrape with people vinceo.ai has HEARD (the
            # KG vocabulary). The scrape often returns the notifications feed
            # instead of real contacts; the names the user actually talks about
            # are a second candidate source, so "text Abby" can still ground to
            # "Abby Nengel" from memory. Deduped, phone contacts kept first.
            try:
                from app.services.vocabulary import vocabulary as _vocab
                known = _vocab.known_recipients()
                if known:
                    have = {c.strip().lower() for c in contacts}
                    added = [k for k in known if k.strip().lower() not in have]
                    if added:
                        contacts = list(contacts) + added
                        self._log(f"   [voice] +{len(added)} known name(s) from "
                                  f"memory for grounding")
            except Exception as exc:
                self._log(f"   [voice] memory vocabulary unavailable ({exc})")
            # Unconditional, and it lists the actual names — so when a correction
            # is missing or wrong we can see immediately whether the scrape found
            # the real contacts or just the notifications feed.
            sample = ", ".join(contacts[:14]) + (" …" if len(contacts) > 14 else "")
            self._log(f"   [voice] grounding recipient “{recipient}” against "
                      f"{len(contacts)} contact(s): {sample}")
            if contacts:
                try:
                    res = self.llm.resolve_recipient(recipient, contacts, ctx)
                    if res.get("changed") and res.get("name"):
                        corrections.append(
                            f"recipient: “{recipient}” → “{res['name']}”")
                        parsed["recipient"] = res["name"]
                        if res.get("alternatives"):
                            parsed["_recipient_alternatives"] = res["alternatives"]
                    else:
                        self._log(f"   [voice] recipient kept as “{recipient}” "
                                  f"(no closer contact / already a match)")
                except Exception as exc:
                    self._log(f"   [voice] recipient resolve failed ({exc})")

        # 2) message body cleanup (outgoing text only) — skip LLM when the
        # body already looks typed/clean (no STT artifacts).
        if message and action in ("send_sms", "text", "message", "reply"):
            try:
                from browser_agent.phone_parse import message_looks_clean
                if message_looks_clean(message):
                    self._log("   [voice] message looks clean — skip LLM cleanup")
                else:
                    clean = self.llm.clean_message(message, ctx)
                    if clean.get("changed"):
                        note = f" ({clean['note']})" if clean.get("note") else ""
                        corrections.append(
                            f"message: “{message}” → “{clean['text']}”{note}")
                        parsed["message"] = clean["text"]
            except Exception as exc:
                self._log(f"   [voice] message cleanup failed ({exc})")

        if corrections:
            parsed["_corrections"] = corrections
            self._log("   [voice] " + " | ".join(corrections))
            # #12: thread these act-time corrections back onto the SOURCE utterance's
            # provenance chain (recipient grounding / body cleanup are edits to what
            # was heard), so the recording shows the dictation AND every fix applied
            # before the send. Best-effort; resolves the source event via the fact.
            self._record_phone_corrections(recipient, message, parsed)

    def _record_phone_corrections(self, recipient, message, parsed):
        """Append recipient-grounding / body-cleanup corrections to the source
        utterance's provenance chain (#12). Best-effort — never blocks a send."""
        try:
            from app.services import provenance as _prov
            from app.storage import get_store
            fid = getattr(self, "_source_fact_id", None)
            if not fid:
                return
            fact = get_store().get_fact(int(fid))
            sev = fact.get("source_event_id") if fact else None
            if not sev:
                return
            new_recip = (parsed.get("recipient") or "").strip()
            if new_recip and new_recip.lower() != (recipient or "").strip().lower():
                _prov.append_correction(int(sev), _prov.RECIPIENT_GROUNDING,
                                        before=recipient, after=new_recip,
                                        note="snapped to known contact")
            new_msg = (parsed.get("message") or "").strip()
            if new_msg and new_msg != (message or "").strip():
                _prov.append_correction(int(sev), _prov.BODY_CLEANUP,
                                        before=message, after=new_msg,
                                        note="voice-typo cleanup")
        except Exception as exc:
            self._log(f"   [voice] provenance record skipped ({exc})")

    # --- the loop ----------------------------------------------------------
    def run_goal(self, goal, dry_run=None, surface=None, packet=None,
                 source_fact_id=None, study_mode=None):
        """Bracket the run with vinceo.ai's agent-run log (Phase 5), then delegate.

        The heavy lifting is in _run_goal_inner; this thin wrapper opens a run
        row, records the per-run cost/steps/status on every exit path — including
        an exception — and never lets a logging hiccup change what the caller
        sees. With a _NullRecorder (standalone agent) it is a pass-through.

        `packet` is a pre-compiled ActionPacket from the Personal Agent Layer
        (app.services.agent_planner). When present it is persisted up-front,
        linked to this run — so the *intended* action is on record before the
        hands act, not just the approval snapshot the browser agent produces.

        `study_mode` is an optional student persona id (lecture_notes, homework…);
        when set it stacks guidance onto router/direct-answer/browser context
        without replacing browser task modes (email/calendar/…).
        """
        level = dry_run if dry_run in cfg.DRY_RUN_LEVELS else self.dry_run
        # Cleared each run; set only on answered_no_browser when ModelRouter
        # wrote an escalate distill row (chat UI verdict wiring).
        self.last_distill_id = None
        # Which stored fact grounds this run (renders the approval packet's Source
        # verbatim from the DB). Set every run; None clears a prior grounding so a
        # later ungrounded goal can't inherit a stale citation. A packet from the
        # Personal Agent Layer can supply it too.
        self._source_fact_id = source_fact_id or (
            (getattr(packet, "source_fact_ids", None) or [None])[0])
        cost_before = self.cost()
        self._recorder.start_run(
            goal, surface=surface, dry_run=level,
            agent_type=(getattr(packet, "suggested_agent", None)
                        or ("desktop" if surface == "desktop"
                            else "phone_link" if surface == "phone_link" else None)))
        if packet is not None:
            self._recorder.record_from_packet(packet)
        status = "error"
        self._autonomous_run = level in cfg.AUTONOMOUS_LEVELS
        if self._autonomous_run:
            self._log("→ autonomous mode (no approval prompts until done)")
        try:
            result, status = self._run_goal_inner(
                goal, dry_run=dry_run, surface=surface, study_mode=study_mode)
            return result, status
        finally:
            self._autonomous_run = False
            try:
                per_run_cost = round(self.cost() - cost_before, 6)
            except Exception:
                per_run_cost = None
            self._recorder.finish_run(status=status, cost=per_run_cost,
                                      steps=self.last_steps)

    def _study_block(self, study_mode=None) -> str:
        """Resolve study-mode guidance; remember last_study_mode for the UI."""
        try:
            from app.services import agent_chat_mode as _smode
        except Exception:
            self.last_study_mode = None
            return ""
        mid = (study_mode or "").strip().lower() or None
        if mid and mid in {r["id"] for r in _smode.registry()}:
            info = {
                "id": mid,
                "label": next(r["label"] for r in _smode.registry() if r["id"] == mid),
                "posture": next(
                    (r.get("posture") for r in _smode.registry() if r["id"] == mid),
                    ""),
            }
            block = _smode.context_block(mid)
        else:
            info = _smode.current()
            block = _smode.context_block(info.get("id"))
        self.last_study_mode = {
            "id": info.get("id"),
            "label": info.get("label"),
            "posture": info.get("posture"),
        }
        if block:
            self._log(f"Study mode: {info.get('label')} — {info.get('posture')}")
        return block

    def _run_goal_inner(self, goal, dry_run=None, surface=None, study_mode=None):
        # `dry_run` overrides this session's default posture for one run.
        level = dry_run if dry_run in cfg.DRY_RUN_LEVELS else self.dry_run
        self.last_dry_run = level

        study_ctx = self._study_block(study_mode)
        study_guidance = study_ctx.strip()

        # Ground in memories + session transcript (session first on follow-ups).
        # Both flow to the router, planner, executor, and direct-answer path.
        ctx = study_ctx + self._build_ctx(goal)

        # Forced surface (e.g. the /desktop route): skip the router entirely and
        # hand straight to that loop. Lets a caller who already knows the task is
        # a desktop/OS action bypass the LLM's web-vs-desktop guess.
        if surface == "desktop":
            self._log("→ desktop (forced route)")
            forced = {"surface": "desktop", "intent": "desktop_task", "forced": True}
            self.last_route = forced
            self._recorder.annotate_run(surface="desktop", intent="desktop_task",
                                        agent_type="desktop", risk_level="medium")
            try:
                self.mem.log_event(self.session_id, goal, forced)
            except Exception:
                pass
            return self._run_desktop_goal(goal, ctx, level=level)

        if surface == "phone_link":
            self._log("→ phone_link (forced route)")
            forced = {"surface": "phone_link", "intent": "phone_task", "forced": True}
            self.last_route = forced
            self._recorder.annotate_run(surface="phone_link", intent="phone_task",
                                        agent_type="phone_link", risk_level="medium")
            try:
                self.mem.log_event(self.session_id, goal, forced)
            except Exception:
                pass
            return self._run_phone_link_goal(goal, ctx)

        # Route first (no Playwright yet): decide web vs desktop vs answer-only.
        route = self.llm.route(goal, ctx)
        self.last_route = route
        self._log(_route_text(route))
        self._recorder.annotate_run(
            surface=route.get("surface"), intent=route.get("intent"),
            risk_level=_risk_from_route(route))
        try:
            self.mem.log_event(self.session_id, goal, route)
        except Exception:
            pass

        # Desktop/OS task (open an app, make a project, run a build command):
        # hand off to the guarded desktop loop. Checked before the no-browser
        # branch because desktop tasks also have requires_browser=False.
        if route.get("surface") == "desktop":
            return self._run_desktop_goal(goal, ctx, level=level)

        if route.get("surface") == "phone_link":
            return self._run_phone_link_goal(goal, ctx)

        # No web action needed (a memory/conversational question): answer directly.
        if not route.get("requires_browser", True):
            ans = self.llm.direct_answer(goal, ctx, mode_guidance=study_guidance)
            # Escalate distill id (if any) for chat 👍/👎/✏️ → set_user_outcome.
            self.last_distill_id = getattr(self.llm, "last_distill_id", None)
            self.transcript.append({"goal": goal, "result": ans})
            self.last_steps, self.last_replans = 0, 0
            return ans, "answered_no_browser"

        # Web path only from here — start Playwright now.
        self._ensure_browser()

        approval_note = ""
        if route.get("requires_user_approval"):
            approval_note = (
                f"\n\n[Note: '{route.get('intent')}' can need approval before an "
                "irreversible step (send/submit/buy/delete). I prepare and draft "
                "up to that point and pause — reply 'approve' when you want me to "
                "commit it.]")

        # Learning layer: recall what worked (and what failed) for this kind of
        # task on this site, and hand it to the planner. intent+site are stable
        # keys across runs; the recipe/pitfalls make the plan shorter and dodge
        # past mistakes (fewer steps, lower cost, no repeated errors).
        intent = route.get("intent") or "unknown"
        site = _norm_site(route.get("site"), self.current_url())

        # Agent mode (Track B #3): a deterministic policy bundle for this family
        # of task — guidance for the planner/executor and extra approval patterns
        # for the non-LLM commit gate. Composes with the dry-run level below.
        # Study mode stacks on top (persona) and does not replace this policy.
        mode = resolve_mode(intent, site)
        self.last_mode = mode
        mode_ctx = mode.context_block()
        mode_extra = mode.approval_res()
        self._log(f"Mode: {mode.label} — {mode.posture}")
        if level != "approval":
            self._log(f"Dry-run: {level}")

        lessons = ""
        try:
            skill = self.mem.recall_skill(intent, site)
            lessons = _lessons_text(skill)
            if lessons:
                self._log(f"   recalled a learned playbook for {intent}@{site} "
                          f"({skill['successes']}/{skill['attempts']} past successes).")
        except Exception:
            pass

        base_goal = mode_ctx + ((ctx + "Current task: " + goal) if ctx else goal)
        plan = self.llm.plan(base_goal + lessons, self.current_url())
        ptext = _plan_text(plan)
        self._log(f"Plan:\n{ptext}")

        # Dry-run: plan-only — return the plan without touching the browser.
        if level == "plan":
            self.last_steps, self.last_replans = 0, 0
            result = (f"Plan (dry-run: plan-only — nothing was executed):\n{ptext}"
                      + approval_note)
            self.transcript.append({"goal": goal, "result": result})
            return result, "plan_only"

        # A short note the executor sees each step when the posture limits it.
        dry_note = {
            "navigate": "\n[Dry-run: navigate-only — do NOT type/click/select; "
                        "browse and read, then finish with done.]",
            "draft": "\n[Dry-run: draft-only — prepare fully but do not commit; "
                     "I will stop at the approval point.]",
            "full": "\n[Autonomous mode — complete the task without approval prompts. "
                    "Call done when finished.]",
            "autonomous": "\n[Autonomous mode — complete the task without approval "
                          "prompts. Call done when finished.]",
        }.get(level, "")

        hist, stall, replans, goal_steps = [], 0, 0, 0
        asks = 0   # ask_human budget: stop instead of nagging (observed live:
                   # the same sign-in request was asked three times)
        status, result = "running", None
        gathered, consec_reads, no_progress, last_sig = [], 0, 0, None
        spiral = _SpiralGuard()   # exact repeats + A/B/A oscillation cycles
        auto_read_done = False  # one free page-read when a click spiral hits
        harvested_urls = set()  # proactive page-text harvest, once per URL
        approved_commit = False   # a fresh request_approval covers the next commit click

        while goal_steps < cfg.MAX_STEPS:
            goal_steps += 1
            self.step += 1
            scan = self.driver.scan()
            if self._maybe_stored_login(scan):
                scan = self.driver.scan()
            ax_path = self.sdir / "ax" / f"step_{self.step}.json"
            ax_path.write_text(json.dumps(scan)[:200000], encoding="utf-8")
            shot_path = self.sdir / "shots" / f"step_{self.step}.png"
            shot_bytes = self.driver.screenshot_bytes()
            if shot_bytes:
                try:
                    shot_path.write_bytes(shot_bytes)
                except Exception:
                    pass
            else:
                self.driver.screenshot(str(shot_path))

            before = signature(scan)

            # anti-spiral: is the page actually changing between steps?
            no_progress = no_progress + 1 if before == last_sig else 0
            last_sig = before
            if no_progress >= cfg.NO_PROGRESS_LIMIT:
                self._log("   no progress for several steps; stopping with partial result")
                status = "stopped_no_progress"
                tail = "\n---\n".join(gathered)[-1500:]
                result = ("(Stopped — the page stopped changing and I wasn't making "
                          "progress.) Information gathered so far:\n" + tail)
                break

            # Proactive harvest: on entering a chat conversation (recognized by
            # URL shape OR page structure), pull the visible thread text into
            # GATHERED before the model acts — prevents the click spiral instead
            # of recovering from it. Once per URL so it can't flood the prompt.
            cur_url = scan.get("url") or ""
            if cur_url not in harvested_urls:
                from .surfaces import is_open_conversation_url, looks_like_chat
                if (is_open_conversation_url(cur_url) or looks_like_chat(scan)):
                    harvested_urls.add(cur_url)
                    pt = (scan.get("page_text") or "").strip()
                    if pt:
                        gathered.append("Visible conversation text:\n" + pt[:2500])
                        while (sum(len(g) for g in gathered) > cfg.GATHERED_CAP
                               and len(gathered) > 1):
                            gathered.pop(0)
                        self._log("   chat surface — harvested visible thread text.")

            ginfo = "\n---\n".join(gathered)[-cfg.GATHERED_CAP:] or "(none yet)"
            nudge = ""
            if consec_reads >= cfg.READ_NUDGE_AT:
                nudge += ("\nNote: you have read the page several times — the gathered "
                          "information above is almost certainly enough. Take a new "
                          "action or call done; do not read the same page again.")
            if no_progress >= cfg.NO_PROGRESS_NUDGE:
                nudge += ("\nNote: the page has not changed for a few steps, so you are "
                          "not making progress. Change approach or finish now with done.")
            # Approaching a click-repeat spiral: SPA chats often need `read`
            # (message bodies aren't interactive nodes) — nudge before hard stop.
            if (spiral.same >= max(1, cfg.REPEAT_ACTION_LIMIT - 1)
                    and isinstance(spiral.last, str)
                    and spiral.last.startswith("click:")):
                nudge += (
                    "\nNote: you are repeating the same click. If a conversation "
                    "is already open, use `read` (no element_id) or the screenshot "
                    "to gather message text, then `done` — do not click the same "
                    "row again."
                )

            # Provider-specific tip (e.g. Gmail compose deep-link), injected ONLY
            # when that provider's page is actually loaded — kept OUT of the general
            # executor prompt so it stays provider-agnostic, and it vanishes on
            # navigation away. Empty (no-op) for about:blank / unknown hosts.
            tip = tips_for_url(scan.get("url", "") or self.current_url() or "")
            tip_block = f"PROVIDER TIP:\n{tip}\n\n" if tip else ""
            content = (
                mode_ctx + ctx
                + f"GOAL:\n{goal}\n\nPLAN:\n{ptext}\n\n"
                f"INFORMATION GATHERED:\n{ginfo}\n\n"
                f"RECENT ACTIONS:\n{_history_text(hist)}\n\n"
                f"{tip_block}"
                f"CURRENT PAGE:\n{render_observation(scan)}\n"
                f"{nudge}{dry_note}\n\nChoose the next single action toward the goal."
            )
            escalate = stall >= cfg.ESCALATE_AT or no_progress >= cfg.ESCALATE_AT
            # Adaptive vision: sparse tree, stuck, OR chat SPA (message bodies
            # often aren't in the AX list — pixels arrive before the spiral).
            from .surfaces import wants_early_vision
            chat_vision = wants_early_vision(
                scan.get("url") or self.current_url(), escalate=escalate,
                scan=scan)
            send_vision = bool(shot_bytes) and cfg.EXECUTOR_VISION and (
                cfg.VISION_ALWAYS or escalate or chat_vision
                or scan.get("count", 0) < cfg.VISION_SPARSE_AT)
            act = self.llm.choose_action(
                content, escalate=escalate,
                image=shot_bytes if send_vision else None)
            name, args = act["name"], act.get("input") or {}
            tag = "  (escalated -> Opus)" if escalate else ""
            if send_vision:
                tag += "  (+screenshot)"
            self._log(f"[step {self.step}] {name} {json.dumps(args)}{tag}")

            # stop a spiral: the same action repeated with no effect gets nowhere.
            # Also catch OSCILLATION (click A, click B, click A…): exact-repeat
            # counting never trips on an A/B cycle, so we count visits to each
            # (page-state, action) pair — re-taking a previously tried action
            # from the same page state is a revisit even if it wasn't the last act.
            act_sig = f"{name}:{json.dumps(args, sort_keys=True)}"
            state_key = "|".join(str(before.get(k) or "") for k in
                                 ("url", "content_hash", "page_hash", "selected"))
            if (spiral.observe(act_sig, state_key) >= cfg.REPEAT_ACTION_LIMIT
                    and name not in ("ask_human", "request_approval", "done")):
                # One recovery: auto-read visible page text (SPA chats) instead
                # of dying with gathered=(none). General — not host-specific.
                if name == "click" and not auto_read_done:
                    auto_read_done = True
                    self._log("   repeat click — auto-reading page text once.")
                    try:
                        txt = (self.driver.read(None) or "").strip()
                    except Exception:
                        txt = (scan.get("page_text") or "").strip()
                    if not txt:
                        txt = (scan.get("page_text") or "").strip()
                    if txt:
                        gathered.append(txt[:2500])
                        hist.append({
                            "step": self.step, "action": "read",
                            "args": {}, "result": f"auto-read {len(txt)} chars",
                            "verified": True, "vreason": "auto-read on repeat",
                            "read_text": txt[:2000], "target": None,
                        })
                        spiral.forgive(act_sig, state_key)
                        stall = 0
                        continue
                self._log("   same action repeated with no effect; stopping.")
                status = "stopped_repeat"
                result = result or (
                    "(Stopped — I kept repeating the same action with no change. "
                    "Information gathered so far:\n"
                    + ("\n---\n".join(gathered)[-1500:] or "(none)"))
                break

            entry = {"step": self.step, "action": name, "args": redact(args),
                     "result": "", "verified": None, "vreason": "", "read_text": None,
                     # accessible name of the target element, for procedural memory:
                     # element_ids are per-page, so the recipe generalizes to the name.
                     "target": (_element_name(scan, args.get("element_id"))
                                if name in ("click", "type", "select") else None)}

            # Dry-run: navigate-only disables every mutating action. Skip it
            # (the model is also told not to try); the no-progress/repeat guards
            # end the run if it can't proceed by reading alone.
            if level == "navigate" and name in ("type", "click", "select"):
                self._log(f"   [navigate-only] skipping {name} — mutations disabled this run.")
                entry["result"], entry["verified"] = f"skipped: {name} disabled (navigate-only)", True
                hist.append(entry)
                gathered.append(f"(navigate-only mode: did not {name}.)")
                consec_reads, stall = 0, 0
                continue

            if name == "done":
                result = args.get("result", "")
                entry["result"], entry["verified"] = "DONE", True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, "", str(shot_path), str(ax_path))
                status = "success"
                break

            if name == "ask_human":
                q = args.get("question", "(no question)")
                asks += 1
                if asks > 3:
                    result = ("I'm blocked without more input. What I still "
                              f"need: {q}\nSend the details in chat and I'll "
                              "pick the task back up.")
                    status = "needs_input"
                    break
                self._log(f"[ask_human] {q}")
                ans = _scrub(self._ask_fn(q), self._log)
                entry["result"], entry["verified"] = f"human: {ans}", True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, ans, str(shot_path), str(ax_path))
                stall, consec_reads = 0, 0
                continue

            if name == "request_approval":
                # Dry-run: draft-only stops AT the commit gate and returns the
                # prepared packet without ever prompting to send (safe demo).
                if level == "draft":
                    packet = _render_packet(self._grounded_fields(args))
                    entry["result"], entry["verified"] = "draft-only stop", True
                    hist.append(entry)
                    self.mem.log_step(self.session_id, self.step, before["url"], name,
                                      redact(args), act, True, "draft-only",
                                      str(shot_path), str(ax_path))
                    status = "stopped_draft"
                    result = ("Prepared and stopped before committing "
                              "(dry-run: draft-only).\n\n"
                              + (packet or args.get("summary", "")))
                    break
                if getattr(self, "_autonomous_run", False):
                    approved_commit = True
                    entry["result"] = "auto-approved (autonomous)"
                    entry["verified"] = True
                    hist.append(entry)
                    self.mem.log_step(self.session_id, self.step, before["url"], name,
                                      redact(args), act, True, "autonomous",
                                      str(shot_path), str(ax_path))
                    stall, consec_reads = 0, 0
                    continue
                decision, feedback = self._approval_decision(
                    args.get("summary", "(no summary)"), args)
                if decision == "approve":
                    approved_commit = True   # don't re-prompt on the click that follows
                    entry["result"] = "approved"
                elif decision == "edit":
                    # Not approved — the user wants changes first. Surface the
                    # revision instruction so the executor re-drafts, then it can
                    # re-request approval on the corrected content.
                    entry["result"] = f"edit requested: {feedback}"
                    gathered.append(f"USER EDIT REQUEST (revise the draft before "
                                    f"re-requesting approval): {feedback}")
                else:
                    entry["result"] = "declined"
                entry["verified"] = True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, entry["result"],
                                  str(shot_path), str(ax_path))
                stall, consec_reads = 0, 0
                continue

            if name == "read":
                txt = self.driver.read(args.get("element_id"))
                entry["result"] = f"read {len(txt)} chars"
                entry["read_text"], entry["verified"] = txt, True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  redact(args), act, True, "", str(shot_path), str(ax_path))
                # keep read text available across the history window (FR-MEM-1)
                gathered.append(txt)
                while sum(len(g) for g in gathered) > cfg.GATHERED_CAP and len(gathered) > 1:
                    gathered.pop(0)
                consec_reads += 1
                stall = 0
                continue

            # mutating action: execute, then verify the ones with a real expected
            # state change. scroll/wait_for often produce no signature delta, so
            # verifying them yields false failures — skip (FR-ACT-3 in spirit).
            consec_reads = 0

            # Login is a human step: never type into a password field via the LLM.
            # Try saved credentials first; otherwise hand off to the human.
            if name == "type" and _element(scan, args.get("element_id")).get("role") == "password":
                host = host_from_url(scan.get("url", "") or self.current_url() or "")
                if self._maybe_stored_login(scan):
                    scan = self.driver.scan()
                    entry["result"], entry["verified"] = "auto-login from saved credentials", True
                    hist.append(entry)
                    self.mem.log_step(self.session_id, self.step, scan.get("url", ""),
                                      name, {"element_id": args.get("element_id")}, act, True,
                                      "stored login", str(shot_path), str(ax_path))
                    stall = 0
                    continue
                self._log("   refusing to type into a password field — please sign "
                          "in yourself in the browser window.")
                hint = _login_save_hint(host)
                ans = self._ask_fn(
                    "That's a password field. Sign in in the browser window, reply "
                    "'continue' when done, or save credentials for next time:\n"
                    + hint)
                saved = try_save_from_reply(ans, scan.get("url", ""))
                if saved:
                    self._log(f"   saved login for {saved['site']} → .credentials.env")
                    ans = "continue"
                else:
                    ans = _scrub(ans, self._log)
                entry["result"], entry["verified"] = f"login handoff: {ans}", True
                hist.append(entry)
                self.mem.log_step(self.session_id, self.step, before["url"], name,
                                  {"element_id": args.get("element_id")}, act, True,
                                  "login handoff", str(shot_path), str(ax_path))
                stall = 0
                continue

            # Non-LLM safety net: a click on a control that looks like it commits
            # something irreversible (Send/Submit/Buy/Delete...) is stopped for
            # approval even if the model didn't call request_approval itself.
            committed, commit_name = False, ""
            if name == "click":
                el = _element(scan, args.get("element_id"))
                elname = el.get("name", "") or ""
                if _looks_irreversible(
                        elname, mode_extra,
                        role=el.get("role"),
                        editable=bool(el.get("editable"))):
                    committed, commit_name = True, elname
                    # Dry-run: draft-only stops before the committing click.
                    if level == "draft":
                        entry["result"] = f"draft-only: did not click '{elname}'"
                        entry["verified"] = True
                        hist.append(entry)
                        self.mem.log_step(self.session_id, self.step, before["url"],
                                          name, redact(args), act, True, "draft-only",
                                          str(shot_path), str(ax_path))
                        status = "stopped_draft"
                        result = (f"Prepared everything and stopped before clicking "
                                  f"'{elname}' (dry-run: draft-only).")
                        break
                    if getattr(self, "_autonomous_run", False):
                        self._log(f"   [autonomous] committing '{elname}' without prompt.")
                    elif approved_commit:
                        approved_commit = False   # already approved a moment ago
                        self._log(f"   proceeding — '{elname}' already approved.")
                    else:
                        decision, feedback = self._approval_decision(
                            f"click \"{elname}\" on {before['url']} — this looks "
                            "irreversible (send/submit/buy/delete).",
                            {"action": f"click '{elname}'"})
                        if decision != "approve":
                            if decision == "edit":
                                entry["result"] = f"edit requested: {feedback}"
                                entry["vreason"] = "edit requested before commit"
                                gathered.append(f"USER EDIT REQUEST (revise before "
                                                f"committing): {feedback}")
                            else:
                                entry["result"] = f"blocked: user declined '{elname}'"
                                entry["vreason"] = "approval declined"
                            entry["verified"] = False
                            hist.append(entry)
                            self.mem.log_step(self.session_id, self.step, before["url"],
                                              name, redact(args), act, False,
                                              entry["vreason"], str(shot_path), str(ax_path))
                            stall = 0
                            continue

            res = self.driver.execute(name, args)
            if name in ("scroll", "wait_for"):
                verified, vnote = res["ok"], "not verified (low-risk action)"
            elif name == "navigate":
                # a navigation that landed on a rendered page is success; no need
                # to ask the verifier (which false-failed on slow-rendering SPAs).
                after = signature(self.driver.scan())
                verified = res["ok"] and after.get("count", 0) > 0
                vnote = "navigated" if verified else "page did not render"
            else:
                after = signature(self.driver.scan())
                # SPA chrome: clicking an already-open conversation often leaves
                # interactive names unchanged; richer signature (url/page_hash/
                # selected) still catches real thread changes. If nothing moved,
                # treat as verified no-op and harvest page_text once.
                if (name == "click"
                        and after.get("url") == before.get("url")
                        and after.get("content_hash") == before.get("content_hash")
                        and after.get("page_hash") == before.get("page_hash")
                        and after.get("selected") == before.get("selected")
                        and after.get("compose") == before.get("compose")):
                    verified, vnote = True, "no-op click (page state unchanged)"
                    if not auto_read_done:
                        pt = (scan.get("page_text") or "").strip()
                        if pt:
                            auto_read_done = True
                            gathered.append(pt[:2500])
                            self._log("   no-op click — harvested visible page text.")
                else:
                    v = self.llm.verify(f"{name} {json.dumps(args)}", before, after)
                    verified = bool(v.get("satisfied")) and res["ok"]
                    vnote = v.get("reason", "")
            entry["result"] = res["detail"]
            entry["verified"] = verified
            entry["vreason"] = vnote
            hist.append(entry)
            self.mem.log_step(self.session_id, self.step, before["url"], name,
                              redact(args), act, verified, vnote, str(shot_path), str(ax_path))

            # An approved irreversible click that visibly changed the page took
            # effect (e.g. the compose window closed after Send) — the task is
            # done. Stop here so it can't re-click and re-prompt endlessly.
            # Post-commit verification: don't just trust "the page changed" —
            # confirm the drafted text actually landed (appears in the visible
            # thread) or the composer emptied. A send that silently failed is
            # worse than a stall: it's a false positive on an irreversible act.
            if committed and res["ok"] and (
                    after.get("content_hash") != before.get("content_hash")
                    or after.get("page_hash") != before.get("page_hash")
                    or after.get("count") != before.get("count")
                    or after.get("url") != before.get("url")):
                drafted = [p.split("=", 1)[1] for p in
                           (before.get("compose") or "").split("|")
                           if "=" in p and len(p.split("=", 1)[1]) >= 4]
                confirm = ""
                if drafted:
                    self.driver.wait_briefly()
                    post = self.driver.scan()
                    post_sig = signature(post)
                    ptext = (post.get("page_text") or "")
                    appeared = any(d in ptext for d in drafted)
                    emptied = not post_sig.get("compose")
                    if appeared or emptied:
                        confirm = " Confirmed: the message appears in the thread." \
                            if appeared else \
                            " Confirmed: the composer cleared after sending."
                        self._log("   post-commit check: send confirmed.")
                    else:
                        confirm = (" NOTE: I could not confirm the message "
                                   "appeared in the thread — please verify it "
                                   "actually sent.")
                        self._log("   post-commit check: could NOT confirm the "
                                  "send — reporting as unconfirmed.")
                self._log(f"   '{commit_name}' completed and the page changed — done.")
                status = "success"
                result = result or (
                    f"Done — '{commit_name}' was approved and completed." + confirm)
                break

            if verified:
                stall = 0
                continue

            # recovery ladder
            stall += 1
            self._log(f"   verify failed ({vnote}); stall={stall}")
            if stall == 1:
                self.driver.wait_briefly()  # most failures are timing/races (§9)
            elif stall >= cfg.REPLAN_AT:
                if replans < cfg.MAX_REPLANS:
                    replans += 1
                    stall = 0
                    plan = self.llm.plan(
                        f"{goal}\n[revised attempt {replans}; progress so far:\n"
                        f"{_history_text(hist)}]" + lessons, self.current_url())
                    ptext = _plan_text(plan)
                    self._log(f"   re-planned (#{replans}):\n{ptext}")
                else:
                    # Recovery (Track B #10): label WHY it's blocked and offer a
                    # concrete menu instead of a bare "I'm stuck".
                    fail = classify("stuck", hist, self.driver.scan(), route)
                    self._log(f"   blocked: {fail.kind}")
                    ans = _scrub(self._ask_fn(fail.interactive_prompt()), self._log)
                    opt = fail.option_for(ans)
                    choice = opt.action if opt else STOP
                    hist.append({"step": self.step, "action": "ask_human",
                                 "args": {"question": fail.kind}, "result": f"human: {ans}",
                                 "verified": True, "vreason": "", "read_text": None})
                    if choice == REPLAN:
                        replans, stall = 0, 0   # grant another cycle of attempts
                        plan = self.llm.plan(
                            f"{goal}\n[another approach; progress so far:\n"
                            f"{_history_text(hist)}]" + lessons, self.current_url())
                        ptext = _plan_text(plan)
                        self._log(f"   trying another path:\n{ptext}")
                    elif choice == INSTRUCT:
                        instr = self.llm.direct_answer(
                            "Write concise, numbered step-by-step instructions the "
                            "user can follow by hand to finish this task: " + goal,
                            ctx + "\n\nProgress so far:\n" + _history_text(hist))
                        result = "Here's how to finish it by hand:\n\n" + instr
                        status = "handed_off_instructions"
                        break
                    elif choice == LOGIN:
                        _scrub(self._ask_fn("Go ahead in the browser window (sign in "
                                            "/ solve it), then reply 'continue'."),
                               self._log)
                        stall = 0   # resume; the next iteration re-scans the page
                    else:
                        status = "stopped_user"
                        result = result or "Stopped — you chose not to continue."
                        break
        else:
            status = "stopped_step_cap"

        if status == "running":
            status = "stopped"

        # Recovery (Track B #10): on a blocked terminal stop, append the labeled
        # failure + the recovery menu so the user can pick a way forward next turn.
        if status in ("stopped_no_progress", "stopped_repeat", "stopped_step_cap"):
            try:
                fail = classify(status, hist, self.driver.scan(), route)
                result = (result or "") + fail.terminal_note()
            except Exception:
                pass

        # Learning layer: fold this run into procedural memory. On success the
        # winning path is stored (kept if it's a new shortest); either way the
        # verify-failure lessons accumulate. Best-effort — never break the task.
        try:
            recipe, notes = _distill(hist, status=status)
            self.mem.learn_skill(intent, site, status, goal_steps, recipe, notes)
        except Exception:
            pass

        # Phase 5: fold this run's steps into vinceo.ai's canonical agent-run log, so
        # the trajectory (not just the browser agent's private episodic.db) is
        # inspectable alongside facts, packets, and the human's verdicts.
        self._recorder.record_steps([
            {"step_index": h.get("step"), "action_type": h.get("action"),
             "input": h.get("args"), "output": h.get("result"),
             "verification": h.get("vreason"),
             "status": ("verified" if h.get("verified") else "failed")}
            for h in hist
        ])

        result = (result or "") + approval_note
        self.transcript.append({"goal": goal, "result": result or f"({status})"})
        self.last_steps, self.last_replans = goal_steps, replans
        return result, status

    def close(self):
        try:
            self.mem.end_session(self.session_id, "ended", None)
        except Exception:
            pass
        if self._browser_started:
            try:
                self.driver.close()
            except Exception:
                pass


def run(goal, start_url=None, headless=True, profile=None, channel=None, cdp_url=None):
    """One-shot entry point used by run.py — builds an Agent, runs once, closes."""
    agent = Agent(headless=headless, start_url=start_url, profile=profile,
                  channel=channel, cdp_url=cdp_url)
    try:
        print(f"\n=== Session {agent.session_id} ===\nGoal: {goal}")
        result, status = agent.run_goal(goal)
        print("\n=== Result ===")
        print(f"Status: {status}")
        if result:
            print(f"\n{result}\n")
        print(f"Steps: {agent.last_steps}   Re-plans: {agent.last_replans}")
        print(f"Estimated cost: ${agent.cost():.4f}")
        for m, u in agent.llm.usage.items():
            print(f"  {m}: in={u['in']} out={u['out']} cache_read={u['cache_read']}")
        print(f"Episodic log: {cfg.SESSIONS_ROOT / 'episodic.db'}  (session {agent.session_id})")
        print(f"Artifacts:    {agent.sdir}")
        return {"session_id": agent.session_id, "status": status, "result": result}
    finally:
        agent.close()
