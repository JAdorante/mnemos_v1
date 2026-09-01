"""Hard anchors (WS2) — exact identifiers regex-mined from OCR text.

Screen understanding flows through lossy vision prose, but the OCR layer
already lands verbatim text — and that text contains exact identifiers:
repo slugs, file paths, URLs, ticket ids, email subjects. They are the
highest-precision, zero-cloud-cost entity evidence available. This module
extracts them with deliberately conservative regexes (precision over recall:
a wrong identifier poisons attribution downstream; a missed one costs
nothing) and stamps them onto the desktop.screen Event so the activity
rollup, context anchors (WS1) and the graph rebuild can consume them.

Privacy: identifiers are less invasive than full OCR text but still
sensitive. A frame whose text classifies `never-send` yields NO identifiers;
mail-derived subjects are classed `personal` and escalate the event's
privacy_class, so the model router's existing egress enforcement applies
for free. Raw OCR text is never placed on events here — identifiers only.

Latency: pure regex on already-captured text, run where OCR already
executes. No LLM, no threads, no I/O.
"""
from __future__ import annotations

import re

# ------------------------------ regex families ------------------------------

# owner/name repo slug. Guards: not inside a path or URL (no adjacent '/'),
# and at least one half must look engineered (digit / -_. / mixed case) so
# prose like "input/output" or "and/or" never matches.
_REPO = re.compile(
    r"(?<![\w./\-])"
    r"([A-Za-z][A-Za-z0-9_.\-]{1,38})/([A-Za-z][A-Za-z0-9_.\-]{1,100})"
    r"(?![\w/])")

# Windows + POSIX file paths with at least two separators.
_WIN_PATH = re.compile(
    r"\b[A-Za-z]:\\(?:[\w .\-]+\\)+[\w .\-]+")
_POSIX_PATH = re.compile(
    r"(?<![\w.])(?:~?/)(?:[\w.\-]+/)+[\w.\-]+")

# URLs: scheme-full, or bare domain WITH a path (a bare domain alone in
# prose is too ambiguous). Query strings are stripped by the normalizer.
_URL = re.compile(
    r"\b(?:https?://)?(?:www\.)?"
    r"([a-z0-9][a-z0-9\-]{0,62}(?:\.[a-z0-9\-]{2,})+)"
    r"((?:/[\w.\-~%]+)+)", re.IGNORECASE)
_URL_SCHEME_ONLY = re.compile(
    r"\bhttps?://(?:www\.)?([a-z0-9][a-z0-9\-]{0,62}(?:\.[a-z0-9\-]{2,})+)"
    r"/?", re.IGNORECASE)

# Ticket ids (JIRA-style). Stoplist keeps acronym-number prose out.
_TICKET = re.compile(r"\b([A-Z]{2,6})-(\d{1,6})\b")
_TICKET_STOP = frozenset({
    "COVID", "UTF", "ISO", "RFC", "SHA", "GPT", "MD", "HTTP", "TLS", "IPV",
    "USB", "PCIE", "WIFI", "GSM", "LTE",
})

# Window-title segments that look engineered ("nexus_v1", "capital-connect")
# — the non-app halves of "storage.py - nexus_v1 - Cursor". Requires an
# internal -_ or a digit-suffix so prose segments ("Quarterly plan") and app
# names ("Google Chrome") stay out.
_TITLE_SEG_OK = re.compile(
    r"^[A-Za-z0-9][\w.\-]{1,40}$")
_ENGINEERED = re.compile(r"[_\-]|\d")

# Email subject lines (mail-client frames only).
_SUBJECT = re.compile(
    r"(?im)^(?:subject:\s*|(?:re|fw|fwd):\s*)(.{3,120}?)\s*$")

# Repo hosts whose /owner/name path doubles as a repo slug.
_REPO_HOSTS = frozenset({"github.com", "gitlab.com", "bitbucket.org"})

# Mail clients (matched against activity.app_of(window)).
_MAIL_APPS = frozenset({
    "outlook", "mail", "thunderbird", "gmail", "proton mail", "apple mail",
})

# Generic path segments that are never a project root.
_PATH_GENERIC = frozenset({
    "users", "home", "documents", "downloads", "desktop", "repos",
    "projects", "code", "src", "git", "dev", "work", "appdata", "local",
    "roaming", "temp", "tmp", "program files", "program files (x86)", "opt",
    "var", "usr", "mnt", "etc",
})


def _cfg():
    from app.config import settings
    return getattr(settings, "identifiers", None)


def _seen_add(out: list[dict], seen: set, item: dict, cap: int) -> bool:
    """Append if novel and under cap. Returns False once the cap is hit."""
    key = (item["kind"], item["norm"])
    if key in seen:
        return True
    if len(out) >= cap:
        return False
    seen.add(key)
    out.append(item)
    return True


def _path_root(segments: list[str]) -> str:
    """Best-guess project-root segment of a path: the first directory after
    the generic roots (and the username slot right after Users/home)."""
    dirs = [s for s in segments if s]
    skip_next = False
    for i, s in enumerate(dirs[:-1]):  # last element is the file
        low = s.strip().lower()
        if skip_next:
            skip_next = False
            continue
        if low in ("users", "home"):
            skip_next = True  # the username segment
            continue
        if low in _PATH_GENERIC or (len(low) == 2 and low.endswith(":")):
            continue
        return s.strip()
    return dirs[-2].strip() if len(dirs) >= 2 else ""


def is_mail_window(window: str) -> bool:
    try:
        from app.services.activity import app_of
        return app_of(window or "").strip().lower() in _MAIL_APPS
    except Exception:
        return False


def extract_identifiers(text: str, *, window: str = "") -> list[dict]:
    """[{kind, value, norm, privacy?}] from one frame's OCR text (+ its
    window title). Pure, deterministic, conservative; bounded by
    settings.identifiers.max_per_frame. Never raises."""
    cfg = _cfg()
    if cfg is not None and not getattr(cfg, "enabled", True):
        return []

    def _on(name: str) -> bool:
        return bool(getattr(cfg, name, True)) if cfg is not None else True

    cap = int(getattr(cfg, "max_per_frame", 24) or 24)
    text = text or ""
    window = window or ""
    blob = f"{window}\n{text}" if window else text
    out: list[dict] = []
    seen: set = set()

    # URLs first: their spans are masked before the repo scan so a URL path
    # never double-reports as a bare repo slug (the host-aware repo emit
    # below covers github-style URLs deliberately).
    masked = blob
    if _on("urls"):
        for m in list(_URL.finditer(blob)) + list(
                _URL_SCHEME_ONLY.finditer(blob)):
            host = m.group(1).lower()
            path = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            path = (path or "").split("?", 1)[0].split("#", 1)[0]
            segs = [s for s in path.split("/") if s]
            first = segs[0] if segs else ""
            norm = f"{host}/{first}" if first else host
            if not _seen_add(out, seen, {"kind": "url",
                                         "value": m.group(0).split("?", 1)[0],
                                         "norm": norm}, cap):
                break
            # github.com/owner/name → the repo identifier too.
            if _on("repos") and host in _REPO_HOSTS and len(segs) >= 2:
                repo_name = re.sub(r"\.git$", "", segs[1])
                if not _seen_add(out, seen, {
                        "kind": "repo", "value": f"{segs[0]}/{repo_name}",
                        "norm": repo_name}, cap):
                    break
            masked = masked.replace(m.group(0), " " * len(m.group(0)))

    if _on("repos"):
        for m in _REPO.finditer(masked):
            owner, name = m.group(1), m.group(2)
            if not (_ENGINEERED.search(owner) or _ENGINEERED.search(name)
                    or any(c.isupper() for c in owner[1:])):
                continue  # prose-shaped ("input/output") — skip
            if "." in owner and "." not in name and _ENGINEERED.search(owner):
                # "storage.py - nexus" style false pair guard: an owner that
                # is itself a filename is not a repo owner.
                if re.search(r"\.\w{1,4}$", owner):
                    continue
            if not _seen_add(out, seen, {"kind": "repo",
                                         "value": f"{owner}/{name}",
                                         "norm": name}, cap):
                break

    if _on("paths"):
        for m in list(_WIN_PATH.finditer(blob)) + list(
                _POSIX_PATH.finditer(blob)):
            p = m.group(0)
            segs = re.split(r"[\\/]+", p)
            root = _path_root(segs)
            if len(root) < 2:
                continue
            if not _seen_add(out, seen, {"kind": "path", "value": p[:260],
                                         "norm": root}, cap):
                break

    if _on("tickets"):
        for m in _TICKET.finditer(blob):
            if m.group(1).upper() in _TICKET_STOP:
                continue
            if not _seen_add(out, seen, {"kind": "ticket", "value": m.group(0),
                                         "norm": m.group(0).upper()}, cap):
                break

    # Window-title segments ("storage.py - nexus_v1 - Cursor" → nexus_v1).
    for line in ([window] if window else []) + blob.splitlines()[:6]:
        line = (line or "").strip()
        if not (8 <= len(line) <= 90):
            continue
        parts = [s.strip() for s in re.split(r"\s+[-–—]\s+", line)]
        if len(parts) < 2 or len(parts) > 4:
            continue
        # Last segment is conventionally the app name — never an identifier.
        for seg in parts[:-1]:
            if not _TITLE_SEG_OK.match(seg) or not _ENGINEERED.search(seg):
                continue
            if re.search(r"\.\w{1,4}$", seg):
                continue  # filenames ("storage.py") are not project names
            if not _seen_add(out, seen, {"kind": "title_segment",
                                         "value": seg, "norm": seg}, cap):
                break

    if _on("mail_subjects") and is_mail_window(window):
        privacy = str(getattr(cfg, "mail_subject_privacy", "personal")
                      or "personal")
        for m in _SUBJECT.finditer(text):
            subj = m.group(1).strip()
            if not _seen_add(out, seen, {"kind": "email_subject",
                                         "value": subj,
                                         "norm": subj.lower(),
                                         "privacy": privacy}, cap):
                break

    return out


def normalize_identifier(ident: dict) -> str:
    """Idempotent normal form of one identifier (the `norm` field)."""
    return str((ident or {}).get("norm") or "").strip()


def entity_candidate_names(idents: list[dict]) -> list[str]:
    """The identifier norms plausible as entity names (repo names, title
    segments, path roots) — what rides Event.entities and feeds alias
    resolution. URLs / tickets / subjects stay meta-only."""
    out, seen = [], set()
    for i in idents or []:
        if i.get("kind") not in ("repo", "title_segment", "path"):
            continue
        n = normalize_identifier(i)
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out


def stamp_event(ev) -> None:
    """Best-effort: mine ev.raw (+ meta.window) and stamp
    meta['identifiers'] / Event.entities. A frame whose text classifies
    never-send yields nothing; mail-derived identifiers escalate the
    event's privacy_class to their own. Never raises, never blocks
    persistence (audio.py enrichment pattern)."""
    try:
        cfg = _cfg()
        if cfg is not None and not getattr(cfg, "enabled", True):
            return
        meta = ev.meta if isinstance(getattr(ev, "meta", None), dict) else None
        if meta is None:
            return
        window = str(meta.get("window") or "")
        raw = getattr(ev, "raw", "") or ""
        if not raw and not window:
            return
        from app.services import privacy_class as pc
        if pc.classify_text(raw, title=window) == pc.NEVER_SEND:
            return
        idents = extract_identifiers(raw, window=window)
        if not idents:
            return
        meta["identifiers"] = idents
        names = entity_candidate_names(idents)
        if names:
            existing = {str(x).lower() for x in (ev.entities or [])}
            ev.entities = list(ev.entities or []) + [
                n for n in names if n.lower() not in existing]
        worst = None
        for i in idents:
            p = i.get("privacy")
            if p:
                worst = pc.max_class(worst, p)
        if worst:
            meta["privacy_class"] = pc.max_class(
                meta.get("privacy_class"), worst)
    except Exception as exc:
        print(f"[perception.identifiers] stamp skipped ({exc}).")
