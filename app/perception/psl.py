"""Registrable-domain parse against a VENDORED public suffix list (WS2d).

The naive last-two-labels split L0 shipped with is wrong for every ccTLD
second-level registry (`bbc.co.uk` -> "co.uk") and for the private registries
we actually see (`owner.github.io` -> "github.io"), so a domain-keyed privacy
rule or activity block keyed on it would be silently wrong.

The list is vendored, not fetched: constraint 2 of the brief is no network in
the capture path, and a runtime fetch would also make the parse
non-deterministic across machines. `app/perception/data/public_suffix_list.dat`
is the upstream artifact verbatim (MPL-2.0, header retained) with its version
pinned alongside in `.VERSION`; refresh it with `scripts/update_psl.py`, which
is a developer command and is never called from perception code.

Staleness is safe in one direction only. A suffix added upstream after our
snapshot falls back to the default rule (rightmost label), which yields a
*wider* domain than the truth — `foo.newtld.example` would give
"newtld.example" rather than "foo.newtld.example". That over-groups; it never
invents a domain the host does not have. Precision over recall, per
constraint 3.
"""
from __future__ import annotations

import re
import threading
from pathlib import Path

_LIST_PATH = Path(__file__).with_name("data") / "public_suffix_list.dat"
_VERSION_PATH = Path(__file__).with_name("data") / "public_suffix_list.VERSION"

_lock = threading.Lock()
_normal: frozenset[str] | None = None   # "co.uk"
_wild: frozenset[str] = frozenset()     # parent of "*.ck" -> "ck"
_except: frozenset[str] = frozenset()   # "!www.ck" -> "www.ck"

# Dotted-quad / bracketed IPv6 hosts have no registrable domain.
_IPV4 = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_LABEL_OK = re.compile(r"^[a-z0-9_](?:[a-z0-9_\-]*[a-z0-9_])?$")


def _load() -> None:
    global _normal, _wild, _except
    with _lock:
        if _normal is not None:
            return
        normal: set[str] = set()
        wild: set[str] = set()
        exc: set[str] = set()
        try:
            text = _LIST_PATH.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"[perception.psl] list unavailable ({e}); "
                  "falling back to the default rule.")
            text = ""
        for line in text.splitlines():
            rule = line.strip()
            if not rule or rule.startswith("//"):
                continue
            rule = rule.split()[0].lower().strip(".")
            if not rule:
                continue
            if rule.startswith("!"):
                exc.add(rule[1:])
            elif rule.startswith("*."):
                wild.add(rule[2:])
            else:
                normal.add(rule)
        _normal, _wild, _except = frozenset(normal), frozenset(wild), frozenset(exc)


def version() -> str:
    try:
        for line in _VERSION_PATH.read_text(encoding="utf-8").splitlines():
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "unknown"


def rule_count() -> int:
    _load()
    return len(_normal or ()) + len(_wild) + len(_except)


def host_of(url_or_host: str) -> str | None:
    """Bare lowercase hostname from a URL or a host string. None when the
    input has no host shaped like a hostname."""
    s = (url_or_host or "").strip()
    if not s:
        return None
    if "//" in s:
        s = s.split("//", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    s = s.rsplit("@", 1)[-1]                       # userinfo
    if s.startswith("["):                          # IPv6 literal
        return None
    s = s.split(":", 1)[0].strip().strip(".").lower()
    if not s or _IPV4.match(s):
        return None
    labels = s.split(".")
    if len(labels) < 2 or not all(_LABEL_OK.match(x) for x in labels):
        return None
    return s


def public_suffix(host: str) -> str | None:
    """The public suffix of `host` per the PSL matching algorithm."""
    h = (host or "").strip().strip(".").lower()
    if not h:
        return None
    _load()
    labels = h.split(".")
    n = len(labels)
    # An exception rule wins outright: the suffix is the rule minus its
    # leftmost label.
    for i in range(n):
        if ".".join(labels[i:]) in _except:
            return ".".join(labels[i + 1:]) or None
    best = 0
    for i in range(n):
        cand = ".".join(labels[i:])
        parent = ".".join(labels[i + 1:])
        if cand in (_normal or ()) or (parent and parent in _wild):
            best = max(best, n - i)
    if best == 0:
        best = 1                                   # the default "*" rule
    return ".".join(labels[n - best:])


def registrable_domain(url_or_host: str) -> str | None:
    """Public suffix plus one label — "bbc.co.uk", "owner.github.io".

    None when the input is not a hostname, or when the host IS a public
    suffix and therefore names no registrable domain (a bare "co.uk").
    """
    host = host_of(url_or_host)
    if not host:
        return None
    suffix = public_suffix(host)
    if not suffix or host == suffix:
        return None
    # No TLD is all digits. Without this the PSL default rule ("the rightmost
    # label is a public suffix") happily accepts OCR-garbled private IPs:
    # observed live on the pilot as 192.168, 172.19, 127.65, 192.168.120.8000
    # — every one stamped as a url identifier on a screen frame.
    if suffix.replace(".", "").isdigit():
        return None
    extra = host[: -(len(suffix) + 1)]
    if not extra:
        return None
    return f"{extra.rsplit('.', 1)[-1]}.{suffix}"


def sld_label(url_or_host: str) -> str | None:
    """The registrable domain's own label — "acme" from "acme.co.uk". This is
    the surface an entity name is plausibly matched against; the full domain
    is what the privacy gate and segmentation key on."""
    reg = registrable_domain(url_or_host)
    if not reg:
        return None
    suffix = public_suffix(reg) or ""
    label = reg[: -(len(suffix) + 1)] if suffix and reg.endswith(suffix) else reg
    return label or None
