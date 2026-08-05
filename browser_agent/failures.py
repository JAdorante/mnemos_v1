"""Failure taxonomy + recovery options (Track B #10).

When a run stalls, "I'm stuck" is useless. This module labels *why* the agent
is blocked and offers the user concrete ways forward. Classification reads the
run's status, the recent action history, and the current page signature — all
signals the loop already has — so it folds in without new observation cost.

    classify(status, hist, scan) -> Failure(kind, label, message, options)

The options are the recovery menu (Track B spec): "you log in / I try another
path / I draft instructions / stop". `interactive_prompt()` renders them for a
human handoff; `terminal_note()` renders them as a suffix on a returned result
so the user can pick one in their next chat turn.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# Recovery actions the caller can act on. Kept stable so the orchestrator can
# branch on `.action` rather than parsing prose.
LOGIN = "login"          # human signs in / solves it in the browser, then continue
REPLAN = "replan"        # agent tries a different path
INSTRUCT = "instruct"    # agent writes step-by-step instructions for the human
STOP = "stop"


@dataclass(frozen=True)
class Option:
    action: str
    text: str


@dataclass(frozen=True)
class Failure:
    kind: str                 # taxonomy label (login_wall, captcha, ...)
    label: str                # human phrase
    message: str              # what happened, one line
    options: list[Option] = field(default_factory=list)

    def interactive_prompt(self) -> str:
        menu = "\n".join(f"  {i + 1}. {o.text}" for i, o in enumerate(self.options))
        return (f"I'm blocked — {self.label}. {self.message}\n\n"
                f"How would you like to proceed?\n{menu}\n\n"
                "Reply with a number, or tell me what to do.")

    def terminal_note(self) -> str:
        menu = "\n".join(f"  {i + 1}. {o.text}" for i, o in enumerate(self.options))
        return (f"\n\n[blocked: {self.label} — {self.message}]\n"
                f"You can:\n{menu}")

    def option_for(self, reply: str) -> Option | None:
        """Map a user's reply (a number, or words) to one of the options."""
        t = (reply or "").strip().lower()
        if not t:
            return None
        m = re.match(r"\s*(\d+)", t)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(self.options):
                return self.options[idx]
        # word intent -> action
        if any(w in t for w in ("log in", "login", "sign in", "signin", "i'll log")):
            return _find(self.options, LOGIN)
        if any(w in t for w in ("another", "different", "try again", "retry", "replan", "other path")):
            return _find(self.options, REPLAN)
        if any(w in t for w in ("instruction", "steps", "draft", "write", "tell me how")):
            return _find(self.options, INSTRUCT)
        if any(w in t for w in ("stop", "cancel", "nevermind", "leave it", "abort")):
            return _find(self.options, STOP)
        return None


def _find(options: list[Option], action: str) -> Option | None:
    return next((o for o in options if o.action == action), None)


# --- signal detection ------------------------------------------------------

_LOGIN_RE = re.compile(r"\b(sign in|signin|log in|login|password|username|"
                       r"authenticate|two-factor|2fa|verification code)\b", re.I)
_CAPTCHA_RE = re.compile(r"\b(captcha|recaptcha|hcaptcha|are you human|"
                         r"i'm not a robot|not a robot|verify you are)\b", re.I)


def _page_text(scan: dict | None) -> str:
    if not scan:
        return ""
    parts = [scan.get("url", ""), scan.get("title", "")]
    for e in scan.get("elements", [])[:80]:
        parts.append(str(e.get("name", "")))
        parts.append(str(e.get("role", "")))
    return " ".join(parts)


def _has_password_field(scan: dict | None) -> bool:
    if not scan:
        return False
    return any(e.get("role") == "password" for e in scan.get("elements", []))


def _recent_reasons(hist: list[dict]) -> str:
    return " ".join((h.get("vreason") or "") + " " + (h.get("result") or "")
                    for h in (hist or [])[-6:]).lower()


# --- classification --------------------------------------------------------

def classify(status: str, hist: list[dict] | None, scan: dict | None,
             route: dict | None = None) -> Failure:
    """Label why the run is blocked and attach recovery options."""
    hist = hist or []
    text = _page_text(scan)
    reasons = _recent_reasons(hist)

    # login and captcha are read straight off the page — most actionable first.
    if _has_password_field(scan) or _LOGIN_RE.search(text):
        return Failure(
            "login_wall", "a login wall",
            "the page wants a sign-in before I can go further, and I don't enter "
            "credentials.",
            [Option(LOGIN, "You sign in in the browser window, then I continue."),
             Option(INSTRUCT, "I write out the steps for you to finish by hand."),
             Option(STOP, "Stop here.")])

    if _CAPTCHA_RE.search(text):
        return Failure(
            "captcha", "a CAPTCHA",
            "the site is asking to prove you're human, which only you can solve.",
            [Option(LOGIN, "You solve the CAPTCHA in the browser window, then I continue."),
             Option(REPLAN, "I try a different route that may avoid it."),
             Option(STOP, "Stop here.")])

    # render/timeout: navigation landed on an empty page, or reads timed out.
    blank = bool(scan) and scan.get("count", scan.get("counts", 1)) == 0
    if status == "stopped_no_progress" and (blank or "did not render" in reasons
                                            or "timeout" in reasons):
        return Failure(
            "timeout", "a page that won't load",
            "the page stopped responding or never finished rendering.",
            [Option(REPLAN, "I retry / try a different path."),
             Option(LOGIN, "You open the page in the browser and get it loading, then I continue."),
             Option(STOP, "Stop here.")])

    if status == "stopped_no_progress" or status == "stopped_repeat":
        return Failure(
            "wrong_page" if "verify fail" in reasons else "no_progress",
            "no progress",
            "the page stopped changing in response to my actions — I may be on "
            "the wrong page or missing a step.",
            [Option(REPLAN, "I try another path."),
             Option(INSTRUCT, "I write out the steps for you to finish."),
             Option(STOP, "Stop here.")])

    if status == "stopped_step_cap":
        return Failure(
            "step_cap", "the step limit",
            "I hit the cap on actions for one task before finishing.",
            [Option(REPLAN, "I keep going with a fresh plan."),
             Option(INSTRUCT, "I hand you the steps to finish."),
             Option(STOP, "Stop here.")])

    # generic stuck (e.g. re-plans exhausted): offer the full menu.
    return Failure(
        "stuck", "I couldn't complete it",
        "I tried a few approaches without getting there.",
        [Option(LOGIN, "You take over in the browser, then I continue."),
         Option(REPLAN, "I try one more path."),
         Option(INSTRUCT, "I write out the steps for you to finish."),
         Option(STOP, "Stop here.")])


def missing_memory_failure() -> Failure:
    """Explicit case: the task referenced something Mnemos should know but the
    grounded memory came back empty. Raised by the caller, not auto-detected."""
    return Failure(
        "missing_memory", "missing context",
        "the task refers to something I don't have in memory (I couldn't find "
        "the detail Mnemos was supposed to have heard/seen).",
        [Option(INSTRUCT, "Tell me the missing detail and I'll continue."),
         Option(STOP, "Stop here.")])
