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

import hashlib
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

# Registrable domains that are infrastructure, not projects. They are still
# recorded as `url_domain` (the privacy gate and segmentation need the literal
# domain), but they never become a kind="domain" ATTRIBUTION candidate — the
# second-level label of google.com or slack.com names no project of the user's.
_DOMAIN_STOP = frozenset({
    "google.com", "gmail.com", "googleusercontent.com", "youtube.com",
    "github.com", "gitlab.com", "bitbucket.org", "stackoverflow.com",
    "microsoft.com", "office.com", "live.com", "sharepoint.com", "apple.com",
    "amazon.com", "x.com", "twitter.com", "linkedin.com", "reddit.com",
    "wikipedia.org", "medium.com", "substack.com", "notion.so", "slack.com",
    "zoom.us", "dropbox.com", "figma.com", "atlassian.net", "openai.com",
    "chatgpt.com", "anthropic.com", "claude.ai", "bing.com",
    "duckduckgo.com", "localhost",
})

# Mail clients (matched against activity.app_of(window)).
_MAIL_APPS = frozenset({
    "outlook", "mail", "thunderbird", "gmail", "proton mail", "apple mail",
})

# Language / format / runtime names. A bare "owner/name" slug whose OWNER is
# one of these is a UI list ("JavaScript / JSON" in a syntax picker), not a
# repository. Observed live on the pilot as the repo identifiers JSON,
# TypeScript, Node.js and some — each reaching attribution at score 1.0.
# Applies ONLY to the bare-slug scan: a path under github.com IS authoritative,
# which is why "docker/app" from a real URL still survives.
_LANG_WORDS = frozenset({
    "javascript", "typescript", "json", "yaml", "yml", "xml", "html", "css",
    "scss", "python", "java", "node.js", "nodejs", "node", "ruby", "rust",
    "golang", "php", "sql", "markdown", "bash", "shell", "powershell",
    "swift", "kotlin", "scala", "perl", "lua", "dart", "elixir", "haskell",
    "csv", "toml", "ini", "text", "plaintext", "binary", "utf-8", "ascii",
    "http", "https", "tcp", "udp", "api", "rest", "graphql", "docker",
    "kubernetes", "linux", "windows", "macos", "android", "ios",
})

# Sparrow's own surfaces. The hosted pilot is reached through an ephemeral
# Cloudflare quick tunnel, so the app's own UI carries no brand word in the
# shared frame's window label ("Primary Monitor", "screen:0:0") and
# surface_filters.is_self_window cannot see it. Observed live: user1's ONLY
# identifier was the pilot's own *.trycloudflare.com address. Watching our own
# dashboard is a feedback loop, not memory.
_INFRA_HOSTS = frozenset({"trycloudflare.com", "cfargotunnel.com",
                          "ngrok.io", "ngrok-free.app", "localhost"})


def _is_infra_host(host: str) -> bool:
    import os
    try:
        from app.perception import psl
        dom = psl.registrable_domain(host) or (host or "").strip().lower()
    except Exception:
        dom = (host or "").strip().lower()
    low = (host or "").strip().lower()
    # The PSL lists tunnel providers as PRIVATE suffixes, so the registrable
    # domain of a quick tunnel is the whole random hostname, not the provider.
    # Match on the suffix, not on equality.
    for infra in _INFRA_HOSTS:
        if dom == infra or low == infra or low.endswith("." + infra):
            return True
    own = (os.getenv("QUILL_PUBLIC_HOST") or "").strip().lower()
    return bool(own and (dom == own or (host or "").lower().endswith(own)))


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


def _trusted_url(url: str | None) -> tuple[str, list[str]] | None:
    """(host, path segments) of a browser-supplied URL, TRUNCATED.

    The registrable domain plus one path segment is everything the identifier
    layer needs — plus a second segment on repo hosts, where /owner/name IS
    the slug. Truncating here rather than gating on QUILL_PERCEPTION_URL_FULL
    closes the back door in one place: identifier `value` persists to
    `quill.db`, so an untruncated trusted URL would store full paths even with
    the full-URL flag off, and two flag-checked code paths would drift.
    """
    if not url:
        return None
    try:
        from app.perception import psl
        clean = str(url).split("?", 1)[0].split("#", 1)[0].strip()
        low = clean.lower()
        if not (low.startswith("http://") or low.startswith("https://")):
            return None
        host = psl.host_of(clean)
        if not host or not psl.registrable_domain(host):
            return None
        # `_URL` strips "www." from its host group, so an OCR-mined host never
        # carries it. Strip it here too or the host-collision rule below would
        # miss every www-prefixed page.
        if host.startswith("www."):
            host = host[4:]
        rest = clean.split("//", 1)[1]
        path = rest.split("/", 1)[1] if "/" in rest else ""
        segs = [x for x in path.split("/") if x]
        keep = 2 if host in _REPO_HOSTS else 1
        # CAL: the RESOURCE digest. The registrable domain is too coarse to
        # identify SaaS work — `drive.google.com` names no document and
        # `claude.ai` names no conversation, while
        # `drive.google.com/document/d/1AbC…` is globally unique and assigned
        # by an external authority, which is the definition of an identity key.
        # Storing that path would reopen what QUILL_PERCEPTION_URL_FULL closes,
        # so we keep the IDENTITY and discard the CONTENT: a digest binds on
        # first sight and reads back as nothing. Computed here, where the full
        # URL is still in hand and the truncation decision already lives.
        res = ""
        if len(segs) > keep:
            res = hashlib.sha256(
                ("/".join([host, *segs])).encode("utf-8")).hexdigest()[:16]
        return host, segs[:keep], res
    except Exception:
        return None


def is_mail_window(window: str) -> bool:
    try:
        from app.services.activity import app_of
        return app_of(window or "").strip().lower() in _MAIL_APPS
    except Exception:
        return False


def extract_identifiers(text: str, *, window: str = "",
                        browser_url: str | None = None) -> list[dict]:
    """[{kind, value, norm, src, privacy?}] from one frame's OCR text (+ its
    window title, + the committed browser URL when WS2d supplies one). Pure,
    deterministic, conservative; bounded by settings.identifiers.max_per_frame.
    Never raises.

    `src` is "browser_url" for anything mined from the trusted URL and "ocr"
    for everything else. It is observability, NOT the suppression mechanism:
    the anchor flattens identifiers to bare norm strings before scoring, so a
    src field cannot reach `context_anchor._SOURCE_RANK`, and that scorer
    breaks ties only within one candidate name anyway — a correct
    "mnemos_v1" and a misread "mnemos_vl" are different names and both would
    survive at score 1.0. Suppression therefore has to happen HERE.
    """
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

    # The committed browser URL goes FIRST. `_seen_add` appends first-come and
    # stops at `cap`, so anything appended after an OCR misread of the address
    # bar would lose the slot to it.
    trusted = _trusted_url(browser_url)
    trusted_host = trusted[0] if trusted else None
    if trusted:
        host, segs, res = trusted
        value = "https://" + host + ("/" + "/".join(segs) if segs else "")
        norm = f"{host}/{segs[0]}" if segs else host
        if _on("urls"):
            row = {"kind": "url", "value": value, "norm": norm,
                   "src": "browser_url"}
            if res:
                row["res"] = res       # opaque resource identity — see _trusted_url
            _seen_add(out, seen, row, cap)
        if _on("repos") and host in _REPO_HOSTS and len(segs) >= 2:
            repo_name = re.sub(r"\.git$", "", segs[1])
            _seen_add(out, seen, {"kind": "repo",
                                  "value": f"{segs[0]}/{repo_name}",
                                  "norm": repo_name, "src": "browser_url"}, cap)
        # kind="domain": the registrable domain as an attribution candidate.
        # `norm` is the domain's own label ("acme" from acme.co.uk) because
        # that is the surface an entity name is matched against —
        # entity_alias.normalize() would turn "acme.co.uk" into "acme co uk"
        # and match nothing.
        try:
            from app.perception import psl
            dom, label = psl.registrable_domain(host), psl.sld_label(host)
        except Exception:
            dom = label = None
        if dom and label and dom not in _DOMAIN_STOP:
            _seen_add(out, seen, {"kind": "domain", "value": dom,
                                  "norm": label, "src": "browser_url"}, cap)

    # URLs next: their spans are masked before the repo scan so a URL path
    # never double-reports as a bare repo slug (the host-aware repo emit
    # below covers github-style URLs deliberately).
    masked = blob
    if _on("urls"):
        for m in list(_URL.finditer(blob)) + list(
                _URL_SCHEME_ONLY.finditer(blob)):
            host = m.group(1).lower()
            # Mask first, unconditionally: a suppressed span must still not
            # fall through to the bare-slug scan below.
            masked = masked.replace(m.group(0), " " * len(m.group(0)))
            # Host-collision suppression. An OCR misread of the address bar
            # produces a DIFFERENT norm, so _seen_add does not dedupe it and
            # both would reach entity_alias.resolve at score 1.0. Dropping
            # same-host OCR hits kills exactly that case while leaving a
            # link in the page body (different host) intact.
            # Known residual, accepted under precision-over-recall: a body
            # link to another repo on the SAME host is dropped, and a misread
            # of the HOST itself ("githuh.com/owner/x") collides with nothing
            # and survives.
            if trusted_host and host == trusted_host:
                continue
            # The regex accepts any dotted token; the public suffix list is
            # what decides whether it is a hostname. This is the guard that
            # kills OCR-garbled private IPs (192.168, 127.65, 172.19 — all
            # observed live) before they become identifiers.
            try:
                from app.perception import psl as _psl
                if not _psl.registrable_domain(host):
                    continue
            except Exception:
                pass
            if _is_infra_host(host):
                continue          # our own console / tunnel — self-observation
            path = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
            path = (path or "").split("?", 1)[0].split("#", 1)[0]
            segs = [s for s in path.split("/") if s]
            first = segs[0] if segs else ""
            norm = f"{host}/{first}" if first else host
            if not _seen_add(out, seen, {"kind": "url",
                                         "value": m.group(0).split("?", 1)[0],
                                         "norm": norm, "src": "ocr"}, cap):
                break
            # github.com/owner/name → the repo identifier too.
            if _on("repos") and host in _REPO_HOSTS and len(segs) >= 2:
                repo_name = re.sub(r"\.git$", "", segs[1])
                if not _seen_add(out, seen, {
                        "kind": "repo", "value": f"{segs[0]}/{repo_name}",
                        "norm": repo_name, "src": "ocr"}, cap):
                    break

    if _on("repos"):
        for m in _REPO.finditer(masked):
            owner, name = m.group(1), m.group(2)
            if owner.strip().lower() in _LANG_WORDS:
                continue  # "JavaScript/JSON" is a syntax picker, not a repo
            if not (_ENGINEERED.search(owner) or _ENGINEERED.search(name)
                    or any(c.isupper() for c in owner[1:])):
                continue  # prose-shaped ("input/output") — skip
            if "." in owner and "." not in name and _ENGINEERED.search(owner):
                # "storage.py - nexus" style false pair guard: an owner that
                # is itself a filename is not a repo owner.
                if re.search(r"\.\w{1,4}$", owner):
                    continue
            # No host on a bare slug, so the host-collision rule above
            # cannot reach these by construction. `src` distinguishes it from
            # the host-anchored emission above: there the repo host is sitting
            # right next to the slug, here there is only a slash in prose, and
            # the guards above are heuristics rather than proof. Consumers that
            # grade evidence (CAL's binding grammar) need to tell the two
            # apart — a real pitch document on this corpus reached here as
            # VC/PE, ASR/VLM and payload_hash/expires_at.
            if not _seen_add(out, seen, {"kind": "repo",
                                         "value": f"{owner}/{name}",
                                         "norm": name, "src": "ocr_slug"}, cap):
                break

    if _on("paths"):
        # Over `masked`, not `blob`: _POSIX_PATH happily matches the
        # "//github.com/docker/app.git" of a URL and emits kind="path" with
        # norm "github.com" — which all three consumer filters DO take, at
        # score 1.0. Observed live on the pilot. A URL is not a filesystem
        # path, and its span is already masked for exactly this reason.
        for m in list(_WIN_PATH.finditer(masked)) + list(
                _POSIX_PATH.finditer(masked)):
            p = m.group(0)
            segs = re.split(r"[\\/]+", p)
            root = _path_root(segs)
            if len(root) < 2:
                continue
            if not _seen_add(out, seen, {"kind": "path", "value": p[:260],
                                         "norm": root, "src": "ocr"}, cap):
                break

    if _on("tickets"):
        for m in _TICKET.finditer(blob):
            if m.group(1).upper() in _TICKET_STOP:
                continue
            if not _seen_add(out, seen, {"kind": "ticket", "value": m.group(0),
                                         "norm": m.group(0).upper(),
                                         "src": "ocr"}, cap):
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
                                         "value": seg, "norm": seg,
                                         "src": "ocr"}, cap):
                break

    if _on("mail_subjects") and is_mail_window(window):
        privacy = str(getattr(cfg, "mail_subject_privacy", "personal")
                      or "personal")
        for m in _SUBJECT.finditer(text):
            subj = m.group(1).strip()
            if not _seen_add(out, seen, {"kind": "email_subject",
                                         "value": subj,
                                         "norm": subj.lower(),
                                         "src": "ocr",
                                         "privacy": privacy}, cap):
                break

    return out


def normalize_identifier(ident: dict) -> str:
    """Idempotent normal form of one identifier (the `norm` field)."""
    return str((ident or {}).get("norm") or "").strip()


# The consuming filter appears at THREE sites and they are deliberately not
# identical. Before "fixing" the inconsistency by hoisting a shared constant,
# read what each one feeds:
#   1. context_anchor._identifier_norms  — attribution candidates, scored 1.0
#      at top source rank. kind="domain" joins this one, flagged.
#   2. entity_candidate_names (below)    — Event.entities. Deliberately NOT
#      extended: see its docstring.
#   3. identifier_rollup.derive_edges    — entity--observed_on_screen-->event
#      graph edges. Deliberately NOT extended: see its comment.
def entity_candidate_names(idents: list[dict]) -> list[str]:
    """The identifier norms plausible as entity names (repo names, title
    segments, path roots) — what rides Event.entities and feeds alias
    resolution. URLs / tickets / subjects stay meta-only.

    kind="domain" (WS2d) is deliberately absent. Two reasons, in order: a bare
    domain is weaker evidence than a repo slug and should earn its binding
    through alias recurrence rather than ride a name list, and nothing
    currently READS Event.entities back — so adding it here would be an
    unreviewed write with no consumer to evaluate it against. If a consumer
    appears, decide this then, on that consumer's evidence."""
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
        if "identifiers" in meta:
            return          # already stamped by the producer — idempotent
        window = str(meta.get("window") or "")
        raw = getattr(ev, "raw", "") or ""
        if not raw and not window:
            return
        from app.services import privacy_class as pc
        if pc.classify_text(raw, title=window) == pc.NEVER_SEND:
            return
        # WS2d: the committed browser URL, when L0/the capture path supplied
        # one. Falls back to url_domain so a domain-only capture (the default
        # storage mode) still yields the kind="domain" candidate.
        burl = meta.get("browser_url")
        if not burl and meta.get("url_domain"):
            burl = f"https://{meta['url_domain']}"
        idents = extract_identifiers(raw, window=window, browser_url=burl)
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
