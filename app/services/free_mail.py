"""Free-mail providers: is this address someone's employer, or a mailbox
anyone can open?

One list for every place that asks. An address at a free-mail host says
nothing about where its owner works, so it is never an org, never a works_at
edge, never a "colleague" and never a project key. The list is the public
`free-email-domains` package (vendored verbatim in
`app/services/data/freemail_domains.json`; source, version, hash and
licence beside it),
plus the hosts the code listed before it was vendored, so nothing excluded
then is included now.

School and government domains are affiliation evidence, not free mail: a
domain under `.edu`, `.edu.<cc>`, `.ac.<cc>`, `.gov`, `.gov.<cc>` (and the
other registries' `.go.<cc>`, `.gob.<cc>`, `.gouv.<cc>`, `.govt.<cc>`),
`.mil`, `.k12.*` or `.sch.<cc>` is never treated as free mail, even when the
list has it (it lists about a hundred, mostly throwaway services on
`.edu.pl`, and a few real schools and one ministry).

The list is read once, on first use, not at import: `people_pipeline` is
imported everywhere and most processes never ask. A missing or unreadable
file leaves the built-in hosts, which is what the code had before.
"""
from __future__ import annotations

import json
import threading
from collections.abc import Set
from pathlib import Path

_LIST_PATH = Path(__file__).with_name("data") / "freemail_domains.json"

# The hosts `people_pipeline` and `context/keys.py` listed before the public
# list was vendored. All of them are in it today; they stay here so a list
# refresh can never make one of them an employer again.
_BUILTIN = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "hotmail.com",
    "outlook.com", "live.com", "msn.com", "icloud.com", "me.com", "mac.com",
    "aol.com", "proton.me", "protonmail.com", "mail.com", "gmx.com",
    "fastmail.com", "qq.com",
})
# Second-level labels under a country code that mean a school or a
# government: harvard.edu, ox.ac.uk, gov.uk, moe.edu.sg, x.sch.uk, and the
# government spellings other registries use (x.go.jp, x.go.id, x.gob.mx,
# x.gouv.fr, x.govt.nz).
_SCHOOL_GOV_SLD = frozenset({
    "edu", "ac", "gov", "sch", "mil", "go", "gob", "gouv", "govt"})
_SCHOOL_GOV_TLD = frozenset({"edu", "gov", "mil"})
# The school labels among them: x.edu, moe.edu.sg, ox.ac.uk, x.lea.sch.uk
# (plus `k12` before the TLD, US school districts).
_ACADEMIC_SLD = frozenset({"edu", "ac", "sch"})
_ACADEMIC_TLD = frozenset({"edu", "ac"})

_lock = threading.Lock()
_domains: frozenset[str] | None = None


def is_school_or_government(domain: str | None) -> bool:
    """True for a domain under an academic or government suffix. Such an
    address is affiliation evidence (a school, an agency), never free
    mail."""
    labels = [x for x in (domain or "").strip().lower().rstrip(".").split(".")
              if x]
    if len(labels) < 2:
        return False
    if labels[-1] in _SCHOOL_GOV_TLD:
        return True
    if len(labels) >= 3 and len(labels[-1]) == 2 \
            and labels[-2] in _SCHOOL_GOV_SLD:
        return True
    # US school districts: jesup.k12.ia.us, k12.ca.us.
    return "k12" in labels[:-1]


def is_academic(domain: str | None) -> bool:
    """True for a school's domain, read from the registry's structure, not
    from a list of schools: an `edu` TLD (and the `.ac` TLD, as before), an
    `edu`, `ac` or `sch` second-level label under a country code
    (moe.edu.sg, cs.ox.ac.uk, stmarys.lancs.sch.uk, school.nsw.edu.au), or
    a `k12` label before the TLD (jesup.k12.ia.us). A school on a plain
    commercial or country domain is not recognised."""
    labels = [x for x in (domain or "").strip().lower().rstrip(".").split(".")
              if x]
    if len(labels) < 2:
        return False
    if labels[-1] in _ACADEMIC_TLD:
        return True
    if len(labels) >= 3 and len(labels[-1]) == 2 \
            and labels[-2] in _ACADEMIC_SLD:
        return True
    return "k12" in labels[:-1]


def _load() -> frozenset[str]:
    out = set(_BUILTIN)
    try:
        data = json.loads(_LIST_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
        out.update(str(d).strip().lower() for d in data
                   if isinstance(d, str) and d.strip())
    except Exception as exc:
        print(f"[free_mail] list unavailable ({exc}); using the built-in "
              f"hosts only.")
    return frozenset(d for d in out if not is_school_or_government(d))


def domains() -> frozenset[str]:
    """Every free-mail host, lower case, school and government domains
    excluded. Loaded on first call."""
    global _domains
    if _domains is None:
        with _lock:
            if _domains is None:
                _domains = _load()
    return _domains


def domain_of(address_or_domain: str | None) -> str:
    """The lower-case host of an address ("jo@Acme.io" -> "acme.io"), or the
    domain itself."""
    return (address_or_domain or "").rpartition("@")[2].strip().lower() \
        .rstrip(".")


def is_free_mail(address_or_domain: str | None) -> bool:
    """True when the address (or domain) is at a free-mail host."""
    dom = domain_of(address_or_domain)
    return bool(dom) and dom in domains()


class _FreeMailDomains(Set):
    """`domains()` as a set that loads on first use, for module constants
    that callers test with `in` (`people_pipeline._FREE_MAIL`,
    `context.keys.GENERIC_EMAIL_DOMAINS`)."""

    def __contains__(self, item) -> bool:
        return isinstance(item, str) \
            and item.strip().lower().rstrip(".") in domains()

    def __iter__(self):
        return iter(domains())

    def __len__(self) -> int:
        return len(domains())

    def __repr__(self) -> str:
        return "<free-mail domains>"


FREE_MAIL_DOMAINS = _FreeMailDomains()
