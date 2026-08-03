"""Per-provider execution tips — a DATA table, injected only when relevant.

The executor's system prompt (prompts.EXECUTOR_SYSTEM) is deliberately
provider-AGNOSTIC: it describes how to operate any web page. Site-specific recipes
(e.g. Gmail's compose deep-link) do not belong in that general prompt.

Tips live here as a host-keyed table and are injected into the executor's
PER-TURN content only when that provider's page is actually loaded. Chat SPAs
without a dedicated row still get GENERIC_WEB_CHAT_TIP via URL/host heuristics
in surfaces.py — no contact names.
"""
from __future__ import annotations

from .credentials import host_from_url

GENERIC_WEB_CHAT_TIP = (
    "Web chat tip: Message bodies are usually NOT in the interactive element "
    "list (look at Visible page text / use `read` with no element_id / the "
    "screenshot). Once a conversation is open, do NOT re-click the same "
    "sidebar row — that toggles or no-ops and stalls. Prefer Search to find a "
    "chat. To compose: `type` into the message textbox (placeholders like "
    "'Send a chat' / 'Type a message' are the composer, not Send). "
    "request_approval only before the real Send control."
)

# Keyed on the FULL host (mail.google.com, not google.com) so a tip appears only
# on the exact surface it applies to. Values are appended to the executor turn.
PROVIDER_TIPS: dict[str, str] = {
    "mail.google.com": (
        "Gmail compose tip: Gmail's compose widgets are hard to fill "
        "click-by-click. To draft a message reliably, use `navigate` to a compose "
        "deep link:\n"
        "  https://mail.google.com/mail/?view=cm&fs=1&to=EMAIL&su=SUBJECT&body=BODY\n"
        "URL-encode SUBJECT and BODY (spaces as %20, newlines as %0A). This opens a "
        "compose window already filled with the recipient, subject, and body. After "
        "it loads, the draft is ready — call `done` (do not send)."
    ),
    "snapchat.com": GENERIC_WEB_CHAT_TIP,
    "web.whatsapp.com": GENERIC_WEB_CHAT_TIP,
    "whatsapp.com": GENERIC_WEB_CHAT_TIP,
    "discord.com": GENERIC_WEB_CHAT_TIP,
    "web.telegram.org": GENERIC_WEB_CHAT_TIP,
    "telegram.org": GENERIC_WEB_CHAT_TIP,
    "messenger.com": GENERIC_WEB_CHAT_TIP,
    "instagram.com": GENERIC_WEB_CHAT_TIP,
    "slack.com": GENERIC_WEB_CHAT_TIP,
    "teams.microsoft.com": GENERIC_WEB_CHAT_TIP,
    "chat.google.com": GENERIC_WEB_CHAT_TIP,
    "messages.google.com": GENERIC_WEB_CHAT_TIP,
}


def tips_for_url(url: str) -> str:
    """Provider tip for `url`, or generic web-chat tip when the URL looks like
    a chat SPA. Empty for about:blank / unknown non-chat hosts.
    """
    host = host_from_url(url or "")
    if not host:
        return ""
    tip = PROVIDER_TIPS.get(host, "")
    if tip:
        return tip
    parts = host.split(".")
    for i in range(1, max(0, len(parts) - 1)):
        cand = ".".join(parts[i:])
        tip = PROVIDER_TIPS.get(cand, "")
        if tip:
            return tip
    try:
        from .surfaces import is_chat_host, is_open_conversation_url
        if is_chat_host(url) or is_open_conversation_url(url):
            return GENERIC_WEB_CHAT_TIP
    except Exception:
        pass
    return ""
