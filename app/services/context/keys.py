"""CAL Stage 0 — context keys.

A *context key* is a normalized, hashable string derived deterministically from
an observable signal. Keys are the join between the raw event stream and the
graph: extraction here is pure string/path work, so the 95% of events that carry
an already-bound identifier never reach a model.

    key := "<key_type>:<normalized_value>"

Two classes, with different binding rules (CAL §3.1):

  identity    globally unique by construction, assigned by an external
              authority — git remote, email address, calendar UID, channel ID,
              issue key, registrable domain. Binds on FIRST observation:
              there is nothing to disambiguate, only something to learn.

  convention  user- or tool-chosen, unique only by habit — local paths, branch
              names, bundle ids. Requires repeated observation across distinct
              days before it may bind, and binds no stronger than `medium`.
              (`entity_alias` already applies this discipline to names via
              `seen_days`; the binding table generalizes it.)

Composition is the leverage (CAL §3.3): `entity_resolver.py` is a terrible key —
it exists in a hundred repos. Scoped under a bound repo it becomes
`file:github.com/owner/repo#app/services/entity_resolver.py`, which is globally
unique. A weak signal composed with a strong key is a strong key; that is why
filesystem-shaped capture is so much cheaper to interpret than speech.

Stdlib-only on purpose — `app.storage` imports the extractors during seeding,
and nothing in this module may pull in a model, a network client, or config.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

# --- tiers (CAL §3.2) -------------------------------------------------------
# STRONG      may bind a node on its own.
# MEDIUM      binds only with corroboration from another medium-or-better key.
# SUPPORTING  may reorder candidates proposed by others. It may never PROPOSE a
#             candidate and may never mint a binding. Without that rule a system
#             that watched you code for an hour starts attributing your bank
#             statement to the repo you had open at 10:47.
STRONG = "strong"
MEDIUM = "medium"
SUPPORTING = "supporting"

IDENTITY = "identity"
CONVENTION = "convention"

# key_type -> (key_class, tier, default strength)
KEY_TYPES: dict[str, tuple[str, str, float]] = {
    "repo":         (IDENTITY,   STRONG,     0.95),
    "file":         (IDENTITY,   STRONG,     0.90),   # only minted when scoped
    "domain":       (IDENTITY,   STRONG,     0.85),
    "url":          (IDENTITY,   STRONG,     0.93),   # a RESOURCE, not a host
    "email":        (IDENTITY,   STRONG,     0.96),
    "thread":       (IDENTITY,   STRONG,     0.92),
    "channel":      (IDENTITY,   STRONG,     0.94),
    "calendar_uid": (IDENTITY,   STRONG,     0.93),
    "issue":        (IDENTITY,   STRONG,     0.90),
    "path":         (CONVENTION, MEDIUM,     0.60),
    "branch":       (CONVENTION, MEDIUM,     0.55),
    "bundle":       (CONVENTION, SUPPORTING, 0.25),
}


# Key types that may NAME a stretch of work, as opposed to merely voting on
# one. A branch is a detail of a repo, not a thing you work on; a bundle is app
# identity. Paths are conditional — see `_path_is_nameable`.
_NAMEABLE_TYPES = frozenset({"repo", "file", "url", "domain", "email",
                             "thread", "channel", "calendar_uid", "issue"})


def _path_is_nameable(value: str) -> bool:
    """Whether a path looks like a real location rather than prose.

    `_POSIX_PATH` needs only two separators, so a product route written in a
    document — `/console/audio-health` — arrives as a filesystem path and, left
    alone, titles an eighty-minute episode after a sentence someone wrote. A
    real working location either names a file or is deeper than two segments.
    """
    v = (value or "").replace("\\", "/").strip("/")
    if not v:
        return False
    segs = [s for s in v.split("/") if s]
    last = segs[-1] if segs else ""
    has_ext = "." in last and 1 <= len(last.rsplit(".", 1)[-1]) <= 5
    return has_ext or len(segs) >= 3


@dataclass(frozen=True)
class SignalKey:
    """One normalized identifier lifted off an event.

    `scope` names the strong key this was composed under, when there is one —
    `file`/`branch` keys are meaningless unscoped, and carrying the scope lets a
    caller prove the promotion rather than assert it.
    """
    key_type: str
    key_value: str
    key_class: str
    tier: str
    strength: float
    scope: str | None = None

    @property
    def key(self) -> str:
        return f"{self.key_type}:{self.key_value}"

    @property
    def nameable(self) -> bool:
        """May this key name a frame, or only contribute evidence to one?"""
        if self.tier == SUPPORTING:
            return False
        if self.key_type == "path":
            return _path_is_nameable(self.key_value)
        return self.key_type in _NAMEABLE_TYPES

    def __str__(self) -> str:      # pragma: no cover - debugging affordance
        return self.key


def _make(key_type: str, value: str, *, scope: str | None = None,
          tier: str | None = None) -> SignalKey | None:
    if not value:
        return None
    kclass, ktier, strength = KEY_TYPES[key_type]
    if tier is not None and tier != ktier:
        ktier, strength = tier, DEMOTED_STRENGTH.get(tier, strength)
    sk = SignalKey(key_type, value, kclass, ktier, strength, scope)
    return demote(sk) if is_generic(sk) else sk


DEMOTED_STRENGTH = {STRONG: 0.85, MEDIUM: 0.55, SUPPORTING: 0.25}


def demote(sk: SignalKey, tier: str = SUPPORTING) -> SignalKey:
    """Drop a key to a weaker tier — the sticky end of the spread rule (§3.4).

    Demotion is asymmetric by design: bleed is worse than a miss, so promoting a
    demoted key back should cost several times the evidence that demoted it.
    """
    return replace(sk, tier=tier, strength=min(sk.strength,
                                               DEMOTED_STRENGTH[tier]))


# --- generic-key stop-list (CAL §3.4, the seed half) -------------------------
# The measured half — per-key entropy of the project distribution — needs
# observation history and belongs to the nightly consolidation job. This is the
# cold-start seed so that day one does not bind `~/Downloads` to whatever the
# user happened to open first.
GENERIC_DOMAINS = frozenset({
    "google.com", "gstatic.com", "googleapis.com", "googleusercontent.com",
    "bing.com", "duckduckgo.com", "yahoo.com", "baidu.com",
    "youtube.com", "youtu.be", "facebook.com", "instagram.com", "x.com",
    "twitter.com", "reddit.com", "tiktok.com", "pinterest.com",
    "wikipedia.org", "wikimedia.org", "amazon.com", "apple.com",
    "microsoft.com", "live.com", "office.com", "cloudflare.com",
    "cloudfront.net", "akamai.net", "gravatar.com", "licdn.com",
    "stackoverflow.com", "medium.com", "substack.com", "news.ycombinator.com",
    "t.co", "bit.ly", "lnkd.in",
})
# Consumer mail hosts — generic as DOMAINS, never as person keys (see
# `is_generic`); `domain()` of an address at one of these carries no org signal.
GENERIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com",
    "yahoo.com", "icloud.com", "me.com", "aol.com", "proton.me",
    "protonmail.com", "fastmail.com", "gmx.com", "mail.com", "qq.com",
})
GENERIC_PATH_LEAVES = frozenset({
    "downloads", "desktop", "documents", "tmp", "temp", "home", "users",
    "pictures", "videos", "music", "public", "appdata", "library",
    "onedrive", "dropbox", "google drive", "icloud drive", "src", "code",
    "dev", "projects", "repos", "workspace", "node_modules", "site-packages",
})
GENERIC_BRANCHES = frozenset({
    "main", "master", "develop", "dev", "trunk", "release", "staging",
    "production", "prod", "head", "default",
})
GENERIC_BUNDLES = frozenset({
    "com.google.chrome", "com.microsoft.edge", "org.mozilla.firefox",
    "com.apple.safari", "com.brave.browser", "com.apple.terminal",
    "com.googlecode.iterm2", "com.microsoft.windowsterminal",
    "org.gnome.terminal", "com.apple.finder", "explorer.exe",
    "com.microsoft.windowsexplorer", "com.apple.systempreferences",
    "com.1password.1password", "com.apple.notificationcenterui",
})


def is_generic(sk: SignalKey) -> bool:
    """True for keys that are real identifiers but carry no project signal.

    These are exactly the keys that poison a graph if allowed to bind: every
    user has `~/Downloads`, every repo has `main`, everyone opens Chrome.
    """
    v = sk.key_value
    if sk.key_type == "domain":
        # Consumer mail hosts are listed here, not under `email`: a personal
        # gmail address is a perfectly strong key for a PERSON, while
        # `domain:gmail.com` identifies no organization at all.
        return v in GENERIC_DOMAINS or v in GENERIC_EMAIL_DOMAINS
    if sk.key_type == "branch":
        return v.rsplit("#", 1)[-1] in GENERIC_BRANCHES
    if sk.key_type == "bundle":
        return v in GENERIC_BUNDLES
    if sk.key_type == "path":
        leaf = os.path.basename(v.rstrip("/\\")).lower()
        return not leaf or leaf in GENERIC_PATH_LEAVES
    return False


# --- git remotes -------------------------------------------------------------
_SCP_REMOTE = re.compile(r"^(?:(?P<user>[^@/]+)@)?(?P<host>[^:/@]+):(?P<path>.+)$")
_DEFAULT_PORTS = {"http": "80", "https": "443", "ssh": "22", "git": "9418"}


def remote(url: str) -> SignalKey | None:
    """`repo:` key from any git remote spelling.

    All of these land on the same key, which is the point — the same repository
    observed over ssh in a terminal and over https in a browser must resolve to
    one node:

        git@github.com:Owner/Repo.git
        ssh://git@github.com:22/Owner/Repo
        https://token@github.com/owner/repo.git
        git://github.com/owner/repo

    Local (`file://`, bare path) remotes return None — they are paths, not
    identities, and `path()` is the honest key for them.
    """
    raw = (url or "").strip().rstrip("/")
    if not raw:
        return None
    host = path = ""
    if "://" in raw:
        parts = urlsplit(raw)
        if parts.scheme in ("file", ""):
            return None
        host, path = (parts.hostname or ""), parts.path
        if parts.port and str(parts.port) != _DEFAULT_PORTS.get(parts.scheme):
            host = f"{host}:{parts.port}"
    else:
        m = _SCP_REMOTE.match(raw)
        if not m:
            return None
        host, path = m.group("host"), m.group("path")
    host = host.lower().strip(".")
    path = re.sub(r"\.git$", "", path.strip("/"), flags=re.I)
    if not host or not path or "." not in host:
        return None
    return _make("repo", f"{host}/{path.lower()}")


# --- registrable domains -----------------------------------------------------
# Bounded static suffix table, NOT the Public Suffix List. It covers the ccTLD
# second levels and the handful of private suffixes where each subdomain is a
# different organization (`acme.atlassian.net` must not collapse to
# `atlassian.net`). Swap in `tldextract` when a dependency is acceptable; the
# call sites do not change.
# Private suffixes: one ORGANIZATION per subdomain. Applied on top of the PSL's
# ICANN section, which does not carry them.
_PRIVATE_SUFFIX = frozenset({
    "atlassian.net", "slack.com", "zendesk.com", "myshopify.com",
    "sharepoint.com", "notion.site", "freshdesk.com", "service-now.com",
    "my.salesforce.com", "lightning.force.com", "github.io", "gitlab.io",
    "pages.dev", "workers.dev", "vercel.app", "netlify.app", "herokuapp.com",
    "firebaseapp.com", "web.app", "azurewebsites.net", "readthedocs.io",
    "webflow.io",
})
_MULTI_SUFFIX = _PRIVATE_SUFFIX | frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "me.uk", "co.jp", "or.jp", "ne.jp",
    "com.au", "net.au", "org.au", "edu.au", "co.nz", "co.in", "com.br",
    "com.cn", "com.hk", "com.sg", "com.mx", "com.tr", "co.za", "co.kr",
    "com.ar", "com.tw", "co.il", "com.ua", "co.th", "com.my",
    # private suffixes: one organization per subdomain
    "atlassian.net", "slack.com", "zendesk.com", "myshopify.com",
    "sharepoint.com", "notion.site", "freshdesk.com", "service-now.com",
    "my.salesforce.com", "lightning.force.com", "github.io", "gitlab.io",
    "pages.dev", "workers.dev", "vercel.app", "netlify.app", "herokuapp.com",
    "firebaseapp.com", "web.app", "azurewebsites.net", "s3.amazonaws.com",
    "readthedocs.io", "zoom.us", "webflow.io",
})
_IPV4 = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def registrable(host_or_url: str) -> str:
    """The organization-owned part of a hostname. `''` when there isn't one.

    Delegates to `app.perception.psl`, which carries the real Public Suffix
    List; the static table below is a fallback for when that import is
    unavailable. Two suffix tables that disagree is the same failure as two
    stoplists that disagree, so there is only ever one authority at a time.
    """
    raw = (host_or_url or "").strip()
    if not raw:
        return ""
    try:
        from app.perception import psl
        host = (psl.host_of(raw) or "").lower().strip(".")
        got = psl.registrable_domain(raw)
        if got:
            # psl carries the ICANN section only, so it collapses
            # `acme.atlassian.net` to `atlassian.net` — the same one-level-too-
            # coarse error the domain stoplist makes. The PRIVATE suffixes below
            # are exactly the hosts where each subdomain is a different
            # organization, so they are applied on top rather than instead.
            labels = host.split(".")
            for depth in (3, 2):
                if len(labels) > depth and ".".join(labels[-depth:]) in _PRIVATE_SUFFIX:
                    return ".".join(labels[-depth - 1:])
            return got.lower()
        if host:
            return ""          # a real host the PSL rejects (IP, localhost)
    except Exception:
        pass
    if "://" in raw:
        raw = urlsplit(raw).hostname or ""
    elif "/" in raw:
        raw = raw.split("/", 1)[0]
    host = raw.split("@")[-1].split(":")[0].strip().strip(".").lower()
    if not host or host == "localhost" or _IPV4.match(host):
        return ""
    labels = host.split(".")
    if len(labels) < 2:
        return ""
    for depth in (3, 2):
        if len(labels) > depth and ".".join(labels[-depth:]) in _MULTI_SUFFIX:
            return ".".join(labels[-depth - 1:])
    return ".".join(labels[-2:])


def domain(host_or_url: str) -> SignalKey | None:
    """`domain:` key — the registrable domain, never the raw hostname.

    `mail.acme.com` and `acme.com` are one organization; keying on the hostname
    would make them two.
    """
    return _make("domain", registrable(host_or_url))


def url(norm: str, *, resource: str = "") -> SignalKey | None:
    """`url:` key — the RESOURCE a page is, not the host it lives on.

    The registrable domain is the wrong granularity for SaaS work.
    `drive.google.com` names no document and `claude.ai` names no conversation,
    so both are correctly stoplisted as domains — but
    `drive.google.com/document/d/1AbC…` is globally unique and assigned by an
    external authority, which is exactly an identity key. The stoplist was
    operating one level too coarse and discarding the identifier sitting in the
    path.

    `resource` is the opaque digest `identifiers._trusted_url` computes over the
    full path (see there for why it is a digest and not the path). A key with
    one is NOT subject to the domain stoplist: that list exists to stop
    `google.com` from naming a project, and it should never have suppressed the
    document underneath it. Without a digest there is no resource, and the key
    degrades to its host — where the stoplist rightly applies.
    """
    host_path = (norm or "").strip().strip("/").lower()
    if not host_path:
        return None
    host = host_path.split("/", 1)[0]
    if not registrable(host):
        return None
    if not resource:
        return domain(host)
    return SignalKey("url", f"{host_path}#{resource}", IDENTITY, STRONG,
                     KEY_TYPES["url"][2])


# --- the normalizer over app.perception.identifiers --------------------------
# identifiers.py OWNS extraction: it mines raw surface strings out of OCR text
# and window titles, and its stoplists are hardened against things actually
# observed on the pilot. This module owns the BINDING GRAMMAR: which of those
# surfaces is an identity, what it is worth, and whether it may bind at all.
# Two live extractors with divergent stoplists is the worst outcome available,
# so nothing here re-mines text — it only normalizes and grades.
#
# `title_segment` and `email_subject` are deliberately absent. They are not
# identities; they are surfaces for entity/person resolution, which is a
# different mechanism with a different failure mode.
_IDENT_UNGRADED = frozenset({"title_segment", "email_subject"})


def from_identifiers(idents, *, scope: SignalKey | str | None = None
                     ) -> list[SignalKey]:
    """Grade mined identifiers into binding keys. Order preserved, deduped.

    The grading is where over-mining gets caught. A bare `owner/name` slug read
    off OCR is only ever MEDIUM, because prose slashes reach the miner as repo
    slugs — a real pitch document on this corpus yielded `VC/PE`, `ASR/VLM` and
    `payload_hash/expires_at`. The same shape arriving from a browser URL on a
    known repo host is STRONG, because there the slug is authoritative.
    """
    out: list[SignalKey] = []
    seen: set[str] = set()
    for i in idents or []:
        if not isinstance(i, dict):
            continue
        kind = str(i.get("kind") or "")
        if kind in _IDENT_UNGRADED:
            continue
        value = str(i.get("value") or "")
        norm = str(i.get("norm") or "")
        sk = None
        if kind == "url":
            sk = url(norm or value, resource=str(i.get("res") or ""))
        elif kind == "domain":
            sk = domain(value or norm)
        elif kind == "repo":
            # A slug is a repository only when a repo HOST was observed beside
            # it. `ocr_slug` means the miner found a slash in prose and its
            # guards let it through; minting `repo:github.com/vc/pe` from that
            # asserts a repository that does not exist. Those surfaces are not
            # identities — they go to entity resolution like any other name.
            if str(i.get("src") or "") == "ocr_slug" or "/" not in value:
                sk = None
            else:
                sk = remote(f"https://github.com/{value}")
        elif kind == "path":
            # `value`, never `norm` — norm is the path's ROOT WORD, and feeding
            # a bare word to path() used to mint a key rooted at the reading
            # process's cwd.
            sk = path(value, resolve=False)
        elif kind == "ticket":
            sk = issue(value or norm)
        if sk is None or sk.key in seen:
            continue
        seen.add(sk.key)
        out.append(sk)
    if scope is not None:
        out = [s for s in out] + [f for f in (
            file(scope, s.key_value) for s in out if s.key_type == "path"
        ) if f is not None]
    return out


# --- filesystem --------------------------------------------------------------
def path(p: str, *, resolve: bool = True) -> SignalKey | None:
    """`path:` key — absolute, symlink-resolved, case-normalized.

    `resolve=False` skips the `realpath` syscall for callers on the hot capture
    path or working with a path that does not exist locally (an OCR'd path from
    a screenshot of someone else's machine, say).
    """
    raw = (p or "").strip().strip('"').strip("'")
    if not raw:
        return None
    try:
        raw = os.path.expanduser(raw)
    except (OSError, ValueError):
        return None
    # A relative fragment is NOT a path key. `abspath` would silently root it at
    # whatever process happened to be running, so mining the word "console" out
    # of prose would mint `path:<cwd>/console` — a binding that describes the
    # reader, not the user. Observed exactly that on a real corpus.
    if not os.path.isabs(raw):
        return None
    if resolve:
        try:
            raw = os.path.realpath(raw)
        except (OSError, ValueError):
            pass
    raw = os.path.normpath(raw)
    raw = os.path.normcase(raw)
    sep = os.sep
    if len(raw) > 1:
        raw = raw.rstrip(sep + "/")
    return _make("path", raw or sep)


def _rel(relpath: str) -> str:
    """Repo-relative path, posix-separated, refusing anything that escapes."""
    rel = re.sub(r"/{2,}", "/", (relpath or "").strip().replace("\\", "/"))
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.strip("/")
    if not rel or rel == ".." or rel.startswith("../") or "/../" in rel \
            or rel.endswith("/.."):
        return ""
    return rel


def file(scope: SignalKey | str, relpath: str) -> SignalKey | None:
    """`file:` key — a filename PROMOTED to strong by composition with its repo.

    Refuses to mint against a weak scope: a bare filename with no bound scope
    stays a mention, not a key (§3.3). That refusal is the whole safeguard —
    `entity_resolver.py` observed with nothing to anchor it must not become a
    binding for whichever project was open at the time.
    """
    scope_key = scope.key if isinstance(scope, SignalKey) else str(scope or "")
    if isinstance(scope, SignalKey) and scope.tier != STRONG:
        return None
    if not scope_key or ":" not in scope_key:
        return None
    rel = _rel(relpath)
    if not rel:
        return None
    return _make("file", f"{scope_key.split(':', 1)[1]}#{rel}", scope=scope_key)


def branch(scope: SignalKey | str, name: str) -> SignalKey | None:
    """`branch:` key — always repo-scoped; `main` alone means nothing."""
    scope_key = scope.key if isinstance(scope, SignalKey) else str(scope or "")
    if not scope_key.startswith("repo:"):
        return None
    nm = (name or "").strip().strip("/")
    nm = re.sub(r"^refs/(heads|remotes)/", "", nm)
    nm = re.sub(r"^origin/", "", nm)
    if not nm or nm == "HEAD":
        return None
    return _make("branch", f"{scope_key.split(':', 1)[1]}#{nm}", scope=scope_key)


# --- people and messages -----------------------------------------------------
_ADDR = re.compile(r"[^<>\s,;]+@[^<>\s,;]+")


def email(addr: str) -> SignalKey | None:
    """`email:` key — lowercased, display name dropped, plus-tag stripped.

    The local part is case-sensitive per RFC 5321 and case-insensitive in every
    mail system anyone actually runs; we follow practice, because two keys for
    one person is the more expensive error.
    """
    m = _ADDR.search(addr or "")
    if not m:
        return None
    local, _, host = m.group(0).rpartition("@")
    local = local.split("+", 1)[0].strip().lower()
    host = host.strip().strip(".").lower()
    if not local or "." not in host:
        return None
    return _make("email", f"{local}@{host}")


def _msgid(raw: str) -> str:
    s = (raw or "").strip()
    if s.startswith("<") and s.endswith(">"):
        s = s[1:-1]
    s = s.strip()
    return s if "@" in s and " " not in s else ""


def thread(*, references: str | list[str] | None = None,
           in_reply_to: str | None = None,
           message_id: str | None = None) -> SignalKey | None:
    """`thread:` key — the ROOT message id of a mail conversation.

    Rooting on References[0] is what makes a reply four hops down land on the
    same key as the original, so a thread is one context and not fifteen.
    Subject lines are deliberately not consulted: "Re: quick question" is not
    an identity.
    """
    refs: list[str] = []
    if isinstance(references, str):
        refs = references.replace(",", " ").split()
    elif references:
        refs = [str(r) for r in references]
    for cand in [*refs, in_reply_to or "", message_id or ""]:
        mid = _msgid(cand)
        if mid:
            return _make("thread", mid.lower())
    return None


_CHANNEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,}$")


def channel(platform: str, workspace: str, channel_id: str) -> SignalKey | None:
    """`channel:` key — workspace + channel ID, never the channel name.

    Names get renamed; `#eng` in two workspaces is two different rooms. A
    name-shaped reference returns None rather than minting a key that will be
    wrong later.
    """
    plat = re.sub(r"[^a-z0-9]+", "", (platform or "").lower())
    ws = (workspace or "").strip().strip("/")
    cid = (channel_id or "").strip().strip("/")
    if not plat or not cid or cid.startswith("#") or not _CHANNEL_ID.match(cid):
        return None
    return _make("channel", f"{plat}/{ws}/{cid}" if ws else f"{plat}/{cid}")


def calendar_uid(uid: str) -> SignalKey | None:
    """`calendar_uid:` key — the iCal UID, stable across every invite update."""
    val = (uid or "").strip().strip("<>").strip()
    if not val or len(val) < 6 or " " in val:
        return None
    return _make("calendar_uid", val.lower())


# --- issue trackers ----------------------------------------------------------
# Tokens shaped like an issue key that never are. Cheap to list, and each one
# left out is a spurious strong binding.
_ISSUE_DENY = frozenset({
    "UTF", "ISO", "SHA", "MD", "RFC", "HTTP", "HTTPS", "IPV", "AES", "RSA",
    "SSE", "GPT", "LLM", "API", "CVE", "ANSI", "ASCII", "BASE", "X", "COVID",
    "SPF", "TLS", "SSL", "JSON", "HTML", "CSS", "PNG", "JPEG", "MP", "H",
    "GB", "MB", "KB", "TB", "PY", "ES", "NET", "CI", "CD", "UTC", "GMT",
})
_ISSUE = re.compile(r"\b([A-Z][A-Z0-9]{1,9})-(\d{1,6})\b")


def issues(text: str) -> list[SignalKey]:
    """Every `ABC-123` issue key in a blob of text, deduped, order preserved.

    Defers to `identifiers`' ticket miner when it is importable, so the two
    modules cannot drift into disagreeing stoplists; the local regex is the
    fallback for callers (seeding, tests) that run without the perception
    package loaded.
    """
    try:
        from app.perception import identifiers as _idents
        mined = [i for i in _idents.extract_identifiers(text or "")
                 if i.get("kind") == "ticket"]
        if mined:
            out = []
            seen = set()
            for i in mined:
                raw_tok = str(i.get("value") or "").upper()
                proj, _, num = raw_tok.partition("-")
                if not (proj and num.isdigit()):
                    continue
                sk = _make("issue", f"{proj}-{int(num)}")
                if sk is not None and sk.key not in seen:
                    seen.add(sk.key)
                    out.append(sk)
            return out
    except Exception:
        pass
    out: list[SignalKey] = []
    seen: set[str] = set()
    for proj, num in _ISSUE.findall(text or ""):
        if proj in _ISSUE_DENY or proj.isdigit():
            continue
        val = f"{proj}-{int(num)}"
        if val in seen:
            continue
        seen.add(val)
        sk = _make("issue", val)
        if sk:
            out.append(sk)
    return out


def issue(token: str) -> SignalKey | None:
    """A single issue key, or None if the token isn't one."""
    found = issues(token)
    return found[0] if found else None


# --- applications ------------------------------------------------------------
_BUNDLE = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)+$")


def bundle(bundle_id: str) -> SignalKey | None:
    """`bundle:` key — reverse-DNS application id.

    Supporting tier always. Which app you are in is real evidence and never
    sufficient evidence: "Cursor is open" does not identify a project.
    """
    val = (bundle_id or "").strip().lower()
    if not val or not _BUNDLE.match(val):
        return None
    return _make("bundle", val)


__all__ = [
    "SignalKey", "STRONG", "MEDIUM", "SUPPORTING", "IDENTITY", "CONVENTION",
    "KEY_TYPES", "demote", "is_generic", "registrable",
    "remote", "domain", "url", "path", "file", "branch", "email", "thread",
    "channel", "calendar_uid", "issue", "issues", "bundle",
    "from_identifiers",
]
