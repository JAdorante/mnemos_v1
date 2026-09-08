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


# Where each provider's real sign-in wall lives — used to send the user (or a
# revealed agent window) straight to the right page, and by the live site
# sweep (tests/test_ghost_browser.py) that proves the park/reveal sign-in
# handoff against the real thing. Keyed on host; mirror hosts (outlook/live/
# office) share one identity provider URL.
LOGIN_URLS: dict[str, str] = {
    "accounts.google.com": "https://accounts.google.com/",
    "google.com": "https://accounts.google.com/",
    "gmail.com": "https://accounts.google.com/",
    "mail.google.com": "https://accounts.google.com/",
    "chat.google.com": "https://accounts.google.com/",
    "messages.google.com": "https://accounts.google.com/",
    "github.com": "https://github.com/login",
    "discord.com": "https://discord.com/login",
    "web.whatsapp.com": "https://web.whatsapp.com/",
    "whatsapp.com": "https://web.whatsapp.com/",
    "web.telegram.org": "https://web.telegram.org/k/",
    "telegram.org": "https://web.telegram.org/k/",
    "messenger.com": "https://www.messenger.com/login/",
    "instagram.com": "https://www.instagram.com/accounts/login/",
    "slack.com": "https://slack.com/signin",
    "teams.microsoft.com": "https://login.microsoftonline.com/",
    "outlook.com": "https://login.microsoftonline.com/",
    "live.com": "https://login.microsoftonline.com/",
    "office.com": "https://login.microsoftonline.com/",
    "microsoft.com": "https://login.microsoftonline.com/",
    "snapchat.com": "https://accounts.snapchat.com/accounts/v2/login",
    "linkedin.com": "https://www.linkedin.com/login",
    "x.com": "https://x.com/i/flow/login",
    "twitter.com": "https://x.com/i/flow/login",
}


def login_url_for(site_or_url: str) -> str:
    """The provider's sign-in URL for a host or URL ('' when unknown).
    Subdomains fall back to their registrable parent (www.github.com →
    github.com)."""
    host = (site_or_url or "").strip().lower()
    if "://" in host:
        host = host_from_url(host)
    else:
        host = host.split("/")[0].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    while host:
        if host in LOGIN_URLS:
            return LOGIN_URLS[host]
        host = host.partition(".")[2]
    return ""


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
