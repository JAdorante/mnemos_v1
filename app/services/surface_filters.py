"""Shared surface filters — keep terminal / CLI / log noise out of intake,
and keep public social/feed screens activity-only for fact extraction.

Desktop capture grabs the whole monitor; a Cursor window with a terminal
panel still has title "… - Cursor", so window-title filters alone are not
enough. Strategy:

  * Dedicated console windows → do not capture/publish at all.
  * Mixed IDE screens → strip CLI/log lines from OCR, description, and
    VLM items before the event is stored or mined.
  * Public feed / short-form post viewers → still captured for activity,
    but screen_extract skips people/claims unless the user is clearly
    composing their own draft or reply (platform-agnostic chrome signals).
"""
from __future__ import annotations

import os
import re

# Terminals / servers / IDE consoles — OCR here is logs or shell, not todos.
CONSOLE_WINDOW = re.compile(
    r"powershell|windows.?powershell|windows.?terminal|\bwt\.exe\b|"
    r"\bcmd\.exe\b|command prompt|conhost|terminus|"
    r"uvicorn|flask|exec_webapp|python\.exe|"
    r"developer tools|devtools|"
    r"(?:^|[\s\-—|])console(?:$|[\s\-—|])|"
    r"terminal(?:\s+panel)?|integrated.?terminal",
    re.I,
)

# Server/console log lines the VLM loves to stuff into items[].
LOG_LINE = re.compile(
    r"serving flask|debug mode\s*:|running on https?://|"
    r"\buvicorn\b|\bwerkzeug\b|application startup|started server process|"
    r"listening on|error code:|\berrno\b|traceback \(most recent|"
    r"file \".+\", line \d|https?://127\.0\.0\.1|https?://0\.0\.0\.0|"
    r"\[(audio|vision|desktop|vlm|todo|agent|launch|screen_extract)\]|"
    r"get /\w|post /\w|http/1\.|pid=\d|exit_code|"
    r"press ctrl\+|warning:|info:|debug:",
    re.I,
)

# Shell prompts + common package-manager / VCS one-liners.
# Avoid bare verbs that are also English ("go onto…", "make dinner").
CLI_LINE = re.compile(
    r"^\s*(?:\$|#|>{1,2}|PS\s+[A-Z]:[^>\n]*>)\s*\S+"
    r"|^\s*(?:clasp|git|npm|npx|yarn|pnpm|pip3?|"
    r"cargo|docker|kubectl|ssh|curl|wget|choco|winget|conda|poetry|"
    r"uv|hugo|cmake|gradle|mvn|rustc|deno|bun)\s+"
    r"[A-Za-z0-9_./:-]"
    r"|^\s*(?:python|py|node)\s+\S+"
    r"|^\s*go\s+(?:build|run|test|mod|get|install|vet)\b"
    r"|^\s*make\s+[A-Za-z0-9_.-]{2,}",
    re.I | re.M,
)

_BAD_TITLE = re.compile(
    r"user-scoped|activity ownership|my contacts|people_?\d*|"
    r"serving flask|exec_webapp|debug mode|memory tag|fact:task|"
    r"content_type|screen_type|checklist for vinceo",
    re.I,
)

# --- Public social / feed surfaces (platform-agnostic) --------------------
# Structural chrome shared across networks — engagement rows, feed tabs,
# @handles — not a single vendor's product name.
FEED_ENGAGEMENT = re.compile(
    r"\b\d+(?:[.,]\d+)?[KkMmBb]?\s*"
    r"(?:Views?|likes?|reposts?|replies?|comments?|shares?|bookmarks?|"
    r"reactions?|upvotes?|retweets?)\b",
    re.I,
)
FEED_CHROME = re.compile(
    r"\b(?:for you|following|trending|explore|news.?feed|home.?feed|"
    r"suggested for you|people you may know|who to follow|"
    r"view quotes|relevant people|communities?|"
    r"repost(?:ed)?|retweet(?:ed)?|quote(?:d)?\s+post|"
    r"liked by|shared by|commented on)\b",
    re.I,
)
FEED_HANDLE = re.compile(r"(?<!\w)@[\w.]{2,30}\b")
# Soft app-family hints — only strengthen a structural feed hit, never alone.
FEED_APP_HINT = re.compile(
    r"\b(?:twitter|x\.com|\bx\b|facebook|instagram|linkedin|reddit|tiktok|"
    r"threads|mastodon|bluesky|bsky|youtube|snapchat|tumblr|nextdoor|"
    r"quora|medium|substack|discord|truth social|weibo|vk\.com)\b",
    re.I,
)
# User is authoring — allow extract (their draft/reply may be real work).
USER_COMPOSE_TITLE = re.compile(
    r"\b(?:compose|new post|create post|create thread|draft|"
    r"write post|share post|start a post)\b",
    re.I,
)
USER_COMPOSE_BODY = re.compile(
    r"\b(?:what(?:'s|s)? (?:on your mind|happening)|share your thoughts|"
    r"start a (?:post|thread|conversation)|write (?:a )?(?:post|comment|reply)|"
    r"add (?:a )?(?:comment|reply)|replying to @|"
    r"post your reply|leave a comment)\b",
    re.I,
)


def _env_title_excludes() -> tuple[str, ...]:
    raw = (os.getenv("QUILL_DESKTOP_CAPTURE_EXCLUDE_TITLES", "") or "").strip()
    if not raw:
        return ()
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


_SELF_TITLE_RE: re.Pattern | None = None


def _self_title_re() -> re.Pattern:
    """The app's OWN UI titles. Brand is read from the theme (single source of
    truth), so a rebrand keeps this correct without touching filter code."""
    global _SELF_TITLE_RE
    if _SELF_TITLE_RE is None:
        brand_words = {"vinceo", "mnemos", "quill"}
        try:
            from app.api.vinceo_theme import BRAND
            brand_words.add((BRAND or "").split(".")[0].strip().lower())
        except Exception:
            pass
        words = "|".join(re.escape(w) for w in sorted(brand_words) if w)
        # Brand anywhere in a title, the served non-brand page titles, or a
        # bare localhost tab pointed at our own server.
        _SELF_TITLE_RE = re.compile(
            rf"\b(?:{words})\b|memory console|weekly check-in|memory changes|"
            r"localhost:8000|127\.0\.0\.1:8000",
            re.I)
    return _SELF_TITLE_RE


def is_self_window(window: str) -> bool:
    """The app's own chat/console/profile pages. Watching our own dashboard
    feeds the graph the app's own labels — 'Memory Console' was minted as an
    entity in the constellation (live, July 28 2026). Self-observation is a
    feedback loop, not memory."""
    return bool(_self_title_re().search(window or ""))


def is_console_window(window: str) -> bool:
    """Windows we never capture into memory: dedicated terminals / console
    apps, anything env-excluded, and the app's OWN UI (self-observation)."""
    w = window or ""
    if CONSOLE_WINDOW.search(w):
        return True
    if is_self_window(w):
        return True
    low = w.lower()
    return any(p in low for p in _env_title_excludes())


def is_noise_line(line: str) -> bool:
    t = (line or "").strip()
    if not t:
        return False
    return bool(CLI_LINE.search(t) or LOG_LINE.search(t))


def cli_line_count(text: str) -> int:
    if not text:
        return 0
    return len(CLI_LINE.findall(text))


def log_line_count(text: str) -> int:
    if not text:
        return 0
    return len(LOG_LINE.findall(text))


def strip_noise_lines(text: str) -> str:
    """Remove CLI / log lines; keep the rest (emails, notes, UI copy, …)."""
    if not text:
        return ""
    kept: list[str] = []
    for line in text.splitlines():
        if is_noise_line(line):
            continue
        kept.append(line)
    # Collapse runs of blank lines left by stripping.
    out: list[str] = []
    blank = 0
    for line in kept:
        if not line.strip():
            blank += 1
            if blank <= 1:
                out.append(line)
            continue
        blank = 0
        out.append(line)
    return "\n".join(out).strip()


def scrub_item_list(items: list | None) -> list:
    """Drop VLM checklist items that are shell/log noise."""
    if not items:
        return []
    clean: list = []
    for it in items:
        if isinstance(it, str):
            s = strip_noise_lines(it)
            if s and not is_noise_line(s):
                clean.append(s)
        elif it is not None:
            clean.append(it)
    return clean


def scrub_vision_result(res: dict | None) -> dict | None:
    """Return a copy of the VLM result with CLI/log noise removed from text
    fields and items. None if nothing useful remains after scrubbing."""
    if not isinstance(res, dict):
        return None
    out = dict(res)
    ocr = strip_noise_lines(str(out.get("ocr_text") or ""))
    desc = strip_noise_lines(str(out.get("description") or ""))
    items = scrub_item_list(out.get("items") if isinstance(out.get("items"), list) else [])
    out["ocr_text"] = ocr
    out["description"] = desc
    out["items"] = items

    remaining = f"{ocr}\n{desc}\n" + "\n".join(
        x for x in items if isinstance(x, str))
    if not remaining.strip():
        return None
    return out


def should_ingest_screen(
    window: str = "",
    *,
    ocr: str = "",
    summary: str = "",
    content_type: str = "",
) -> bool:
    """False when this frame should never enter memory (dedicated console or
    content that is only CLI/log after scrubbing)."""
    if is_console_window(window):
        return False
    title_blob = f"{window}"
    if _BAD_TITLE.search(title_blob):
        # Self-UI titles still get captured elsewhere; here we only block
        # obvious schema/log titles mistaken for real screens.
        pass
    raw = f"{ocr}\n{summary}"
    cleaned = strip_noise_lines(raw)
    noise_hits = cli_line_count(raw) + log_line_count(raw)
    if noise_hits and len(cleaned) < 40:
        return False
    if noise_hits >= 2 and not cleaned:
        return False
    # Pure code surfaces with no remaining prose after scrub — skip intake.
    if (content_type or "").strip().lower() == "code" and noise_hits >= 1 and len(cleaned) < 80:
        return False
    return True


def is_log_or_cli_surface(
    window: str = "",
    title: str = "",
    ocr: str = "",
    summary: str = "",
    *,
    content_type: str = "",
) -> bool:
    """True when a surface is dominated by terminal / CLI / log noise.

    Prefer `should_ingest_screen` + `strip_noise_lines` for intake. This
    remains for todo_watcher (don't offer CLI lines as todos).
    """
    ctype = (content_type or "").strip().lower()
    if ctype in ("code",) and (cli_line_count(ocr) or cli_line_count(summary)):
        return True
    if is_console_window(window):
        return True
    if _BAD_TITLE.search(title or ""):
        return True

    blob = f"{window}\n{title}\n{(ocr or '')[:800]}\n{(summary or '')[:400]}"
    if log_line_count(blob) >= 2:
        return True
    low = blob.lower()
    if "serving flask" in low or "running on http" in low:
        return True

    body = f"{(ocr or '')}\n{(summary or '')}"
    cli_hits = cli_line_count(body)
    if cli_hits >= 2:
        return True
    compact = re.sub(r"\s+", " ", body).strip()
    if cli_hits >= 1 and len(compact) < 160:
        return True
    return False


def event_is_log_or_cli(ev) -> bool:
    """Convenience for Event-like objects from desktop.screen capture."""
    meta = ev.meta if isinstance(getattr(ev, "meta", None), dict) else {}
    vision = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    window = str(meta.get("window") or "")
    title = str(vision.get("title") or meta.get("title") or "")
    ocr = str(vision.get("ocr_text") or "")
    summary = str(getattr(ev, "summary", None) or getattr(ev, "raw", None) or "")
    ctype = str(meta.get("content_type") or vision.get("content_type") or "")
    return is_log_or_cli_surface(
        window, title, ocr, summary, content_type=ctype)


def _feed_env_hints() -> tuple[str, ...]:
    raw = (os.getenv("QUILL_SOCIAL_FEED_TITLES", "") or "").strip()
    if not raw:
        return ()
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def is_user_social_compose(
    window: str = "",
    title: str = "",
    ocr: str = "",
    summary: str = "",
) -> bool:
    """True when the user is authoring a draft/reply, not just browsing a feed.

    Compose-titled windows always qualify. A bare "Post your reply" affordance
    on someone else's viral post does NOT — that needs compose title or a
    reply-to draft without third-party engagement chrome dominating.
    """
    head = f"{window}\n{title}"
    if USER_COMPOSE_TITLE.search(head):
        return True
    body = f"{ocr}\n{summary}"
    blob = f"{head}\n{body}"
    if not USER_COMPOSE_BODY.search(blob):
        return False
    # Viewing someone else's post: engagement metrics + @handle → not compose.
    if FEED_ENGAGEMENT.search(body) and FEED_HANDLE.search(body):
        return False
    if FEED_ENGAGEMENT.findall(body) and len(FEED_ENGAGEMENT.findall(body)) >= 2:
        return False
    return True


def is_social_feed_surface(
    window: str = "",
    title: str = "",
    ocr: str = "",
    summary: str = "",
) -> bool:
    """True for public feed / short-form post viewers (any network).

    Uses structural UI chrome (engagement counts, feed tabs, @handles), with
    optional app-family hints only as a booster — never a single hard-coded
    platform rule.
    """
    head = f"{window}\n{title}"
    body = f"{(ocr or '')[:1200]}\n{(summary or '')[:600]}"
    blob = f"{head}\n{body}"
    low_head = head.lower()
    if any(h in low_head for h in _feed_env_hints()):
        return True

    engagement_hits = len(FEED_ENGAGEMENT.findall(blob))
    chrome = bool(FEED_CHROME.search(blob))
    handles = len(FEED_HANDLE.findall(blob))
    app_hint = bool(FEED_APP_HINT.search(blob))

    # Strong: multiple engagement rows (Views / likes / replies …).
    if engagement_hits >= 2:
        return True
    # Engagement + handle or feed chrome.
    if engagement_hits >= 1 and (handles >= 1 or chrome):
        return True
    # Feed chrome + handle (timeline without parsed counts).
    if chrome and handles >= 1:
        return True
    # App-family hint only when paired with chrome or a handle.
    if app_hint and (chrome or handles >= 1 or engagement_hits >= 1):
        return True
    return False


def is_activity_only_social(
    window: str = "",
    title: str = "",
    ocr: str = "",
    summary: str = "",
) -> bool:
    """Feed/browse surfaces that must not mint people/claims — activity only.

    Returns False when the user is clearly composing their own draft/reply.
    """
    if is_user_social_compose(window, title, ocr, summary):
        return False
    return is_social_feed_surface(window, title, ocr, summary)


def event_is_activity_only_social(ev) -> bool:
    """screen_extract gate: skip fact mining; event stays for activity."""
    meta = ev.meta if isinstance(getattr(ev, "meta", None), dict) else {}
    vision = meta.get("vision") if isinstance(meta.get("vision"), dict) else {}
    window = str(meta.get("window") or "")
    title = str(vision.get("title") or meta.get("title") or "")
    ocr = str(vision.get("ocr_text") or getattr(ev, "raw", None) or "")
    summary = str(getattr(ev, "summary", None) or "")
    return is_activity_only_social(window, title, ocr, summary)
