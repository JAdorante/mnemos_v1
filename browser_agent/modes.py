"""Agent modes — per-domain policy bundles (Track B #3).

A *mode* is the operating policy for a family of tasks: email, calendar,
research, shopping, CRM, or form-filling. Each bundles

  - guidance   : a short block injected into the planner and executor so the
                 agent behaves domain-appropriately (what to prepare, where the
                 irreversible line is);
  - approval   : extra accessible-name patterns that force the non-LLM approval
                 gate in this mode, on top of the global COMMIT_PATTERNS;
  - posture    : one line shown to the user so the boundary is visible.

The mode is resolved DETERMINISTICALLY from the router's intent+site (no extra
LLM call, and unit-testable). It composes with — but is independent of — the
dry-run level (Track B #8): the mode says *what* is irreversible here, the
dry-run level says *how far* the agent is allowed to go this run.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Mode:
    key: str
    label: str
    posture: str                              # one-line boundary shown to the user
    guidance: str                             # injected into planner + executor
    approval_patterns: tuple[str, ...] = ()   # extra commit patterns forced to approval
    read_only: bool = False                   # research: gather only, never commit

    def approval_res(self) -> list[re.Pattern]:
        return [re.compile(p, re.I) for p in self.approval_patterns]

    def context_block(self) -> str:
        """The block prepended to planner/executor input for this mode."""
        tag = " (read-only: gather and prepare, never commit)" if self.read_only else ""
        return f"ACTIVE MODE — {self.label}{tag}:\n{self.guidance}\n\n"


# --- the registry ----------------------------------------------------------
# approval_patterns are word-ish regex fragments matched against a control's
# accessible name; they only ADD to the global safety net, never remove from it.

EMAIL = Mode(
    key="email",
    label="Email",
    posture="I'll draft and prepare; sending needs your approval.",
    guidance=(
        "This is an email task. You may open the mailbox, read threads, and "
        "compose a draft (recipient, subject, body) freely. Do NOT send, reply, "
        "or forward without approval — request_approval with the full draft "
        "first. Leaving a saved draft is the safe endpoint."
    ),
    approval_patterns=(r"\bsend\b", r"\breply\b", r"\bforward\b"),
)

CALENDAR = Mode(
    key="calendar",
    label="Calendar",
    posture="I'll prepare the event; creating/changing it needs your approval.",
    guidance=(
        "This is a calendar task. You may open the calendar, read availability, "
        "and fill in an event's title, time, guests, and notes. Do NOT create, "
        "save, delete, or send invites without approval — request_approval with "
        "the event details first. Stop before the Save/Create/Send button."
    ),
    approval_patterns=(r"\bsave\b", r"\bcreate\b", r"\binvite\b", r"\bdelete\b"),
)

RESEARCH = Mode(
    key="research",
    label="Research",
    posture="Read-only: I gather and summarize, and won't submit anything.",
    guidance=(
        "This is a research task. Gather, read, and summarize — cite the pages "
        "you used. You may type into SEARCH boxes to find information, but do "
        "not fill or submit forms, sign up, post, or change anything. There is "
        "nothing to commit; finish with a sourced summary via done."
    ),
    read_only=True,
)

SHOPPING = Mode(
    key="shopping",
    label="Shopping",
    posture="I'll find and compare items; checkout/purchase needs your approval.",
    guidance=(
        "This is a shopping task. You may search, open product pages, compare, "
        "and add items to the cart. Do NOT check out, buy, pay, or place an "
        "order without approval — request_approval with the item, price, and "
        "total first. Stop before the Buy/Checkout/Place-order button."
    ),
    approval_patterns=(r"\badd to cart\b", r"\bcheckout\b", r"\bplace order\b",
                       r"\bbuy\b", r"\bpay\b"),
)

CRM = Mode(
    key="crm",
    label="CRM",
    posture="I'll prepare the record; saving changes needs your approval.",
    guidance=(
        "This is a CRM task. You may open records, read them, and fill in field "
        "values (contact, deal, note). Do NOT create, save, or delete a record "
        "without approval — request_approval with the exact field values first. "
        "Stop before the Save/Create/Delete button."
    ),
    approval_patterns=(r"\bsave\b", r"\bcreate\b", r"\bdelete\b", r"\bupdate\b"),
)

FORM = Mode(
    key="form",
    label="Form",
    posture="I'll fill the form; submitting needs your approval.",
    guidance=(
        "This is a form-filling task. You may fill every field and review the "
        "completed form. Do NOT submit without approval — request_approval with "
        "the field values first. Stop before the Submit button."
    ),
    approval_patterns=(r"\bsubmit\b", r"\bconfirm\b"),
)

MESSAGING = Mode(
    key="messaging",
    label="Messaging",
    posture="I'll open the chat and draft; sending needs your approval.",
    guidance=(
        "This is a web chat / DM task (Snapchat Web, WhatsApp Web, Discord, "
        "Messenger, etc.). Message bodies are often NOT in the interactive "
        "element list — use Visible page text, `read` (no element_id), or the "
        "screenshot. Once the right conversation is open, do NOT re-click the "
        "same sidebar row. To compose: `type` into the message textbox "
        "(placeholder names like 'Send a chat' / 'Type a message' are the "
        "composer, not the Send button). Call request_approval with the exact "
        "draft before clicking a real Send control. For read-only goals, gather "
        "and finish with done — cite the page URL."
    ),
    approval_patterns=(r"^send$", r"\bsend message\b", r"\bsend snap\b"),
)

GENERAL = Mode(
    key="general",
    label="General",
    posture="I'll prepare actions and pause before anything irreversible.",
    guidance=(
        "Prepare freely (navigate, read, fill fields). Pause for approval before "
        "any irreversible step (send, submit, buy, delete, change a saved record)."
    ),
)

MODES = {m.key: m for m in (
    EMAIL, CALENDAR, RESEARCH, SHOPPING, CRM, FORM, MESSAGING, GENERAL)}

# intent/site keyword -> mode key. First match wins; order is most-specific first.
# Messaging BEFORE research: "read" alone must not steal chat-read goals.
_RULES: list[tuple[tuple[str, ...], str]] = [
    (("email", "gmail", "mail", "inbox", "outlook", "compose", "reply"), "email"),
    (("calendar", "event", "meeting", "schedule", "invite", "availability"), "calendar"),
    (("shop", "buy", "cart", "purchase", "order", "checkout", "product", "amazon"), "shopping"),
    (("crm", "salesforce", "hubspot", "lead", "deal", "record"), "crm"),
    (("form", "apply", "signup", "sign_up", "register", "application"), "form"),
    (("snapchat", "whatsapp", "discord", "telegram", "messenger", "imessage",
      "send_chat", "read_chat", "web_chat", "direct_message", "dm",
      "chat_message", "send_message", "read_message", "/web"), "messaging"),
    (("research", "summarize", "find", "look_up", "lookup", "compare", "read"), "research"),
]


def resolve_mode(intent: str | None, site: str | None) -> Mode:
    """Pick a mode from the router envelope, deterministically. Falls back to
    GENERAL when nothing matches, so behavior is never worse than today."""
    hay = f"{(intent or '').lower()} {(site or '').lower()}"
    # Host/path cues for web chat even when intent is vague.
    try:
        from .surfaces import is_chat_host, is_open_conversation_url, is_chat_surface
        if is_chat_host(site) or is_open_conversation_url(site) or is_chat_surface(
                site, intent=intent, site=site):
            return MESSAGING
    except Exception:
        pass
    for keys, mode_key in _RULES:
        if any(k in hay for k in keys):
            return MODES[mode_key]
    return GENERAL
