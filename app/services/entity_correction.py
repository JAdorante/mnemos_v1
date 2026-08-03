"""Shared entity-correction — collapse ASR mis-hearings of a NAME to one identity.

The resolver's cascade (exact -> prefix -> embedding) collapses spelling and
nickname variants, but it misses the one that hurts most on real data: an ASR
*mis-hearing* of a name. "Abby Nengel" comes back as "Abby Nangle" — not exact,
not a prefix, and short name strings sit too close together for the (deliberately
high) embedding threshold to separate a true variant from a false merge. So a
single person fragments into two.

This service adds the missing layer: a phonetic (Soundex) + edit-distance check
that both the person resolver AND the entity resolver route through, so the rule
for "are these the same name?" lives in exactly one place. It is pure Python (no
deps) and deliberately conservative — a false merge is worse than a missed one:

  * multi-token names are surname-anchored: the given names must be compatible
    (equal / prefix / phonetic) AND the surnames must sound alike or be one typo
    apart. "Abby Nangle" ~ "Abby Nengel" merges; "Marc Smith" ~ "Mike Jones" does not.
  * single-token names need BOTH a phonetic match AND high edit similarity, so
    "Jon"~"John" and "Sara"~"Sarah" merge while "Marc"~"Mike" (different Soundex)
    never do.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# A correction is accepted only at/above this combined score. Tunable; high on
# purpose (un-merging two wrongly-joined people is painful).
MIN_SCORE = float(os.environ.get("QUILL_ENTITY_CORRECT_MIN_SCORE", "0.72"))

_NONWORD = re.compile(r"[^a-z0-9 ]+")
_WS = re.compile(r"\s+")

_SOUNDEX = {**dict.fromkeys("BFPV", "1"),
            **dict.fromkeys("CGJKQSXZ", "2"),
            **dict.fromkeys("DT", "3"),
            "L": "4", **dict.fromkeys("MN", "5"), "R": "6"}


def normalize(name: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace."""
    s = _NONWORD.sub(" ", (name or "").lower())
    return _WS.sub(" ", s).strip()


def tokens(name: str) -> list[str]:
    return [t for t in normalize(name).split(" ") if t]


def soundex(token: str) -> str:
    """Classic Soundex of one alphabetic token: first letter + 3 digits.

    Consonants sharing a code collapse; vowels (and H/W handling) reset the run
    so a repeat after a vowel still counts. 'nangle' and 'nengel' both -> N524."""
    s = "".join(ch for ch in token.upper() if ch.isalpha())
    if not s:
        return ""
    out = s[0]
    prev = _SOUNDEX.get(s[0], "0")
    for ch in s[1:]:
        code = _SOUNDEX.get(ch, "0")
        if code != "0" and code != prev:
            out += code
            if len(out) >= 4:
                break
        # Vowels reset the run (coded twice across a vowel); H/W do not.
        if ch not in "HW":
            prev = code
    return (out + "000")[:4]


def _lev(a: str, b: str) -> int:
    """Levenshtein edit distance (iterative, O(len(a)*len(b)) time, O(len(b)))."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def edit_sim(a: str, b: str) -> float:
    """1 - normalized edit distance, in 0..1 (1.0 == identical)."""
    if not a and not b:
        return 1.0
    m = max(len(a), len(b)) or 1
    return 1.0 - _lev(a, b) / m


def _phonetic_eq(a: str, b: str) -> bool:
    sa, sb = soundex(a), soundex(b)
    return bool(sa) and sa == sb


def _prefix(a: str, b: str, min_len: int = 3) -> bool:
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    return len(short) >= min_len and long.startswith(short)


@dataclass
class Match:
    name: str            # the candidate canonical name matched
    ref: object          # the candidate's id / payload (opaque to this service)
    score: float
    method: str          # exact | phonetic-surname | phonetic | edit


def score_pair(a: str, b: str) -> tuple[float, str] | None:
    """Score two names as the-same-identity, or None if they're not close.
    Conservative and surname-anchored for multi-token names (see module docs)."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return None
    if na == nb:
        return (1.0, "exact")

    ta, tb = na.split(" "), nb.split(" ")
    if len(ta) >= 2 and len(tb) >= 2:
        gn_ok = (ta[0] == tb[0] or _prefix(ta[0], tb[0])
                 or _phonetic_eq(ta[0], tb[0]) or edit_sim(ta[0], tb[0]) >= 0.8)
        sa, sb = ta[-1], tb[-1]
        sn_ok = _phonetic_eq(sa, sb) or edit_sim(sa, sb) >= 0.75
        if gn_ok and sn_ok:
            return (round(0.5 + 0.5 * edit_sim(na, nb), 4), "phonetic-surname")

    # Single-token (or mismatched-arity) names: demand BOTH signals agree.
    whole = edit_sim(na.replace(" ", ""), nb.replace(" ", ""))
    if _phonetic_eq(na.replace(" ", ""), nb.replace(" ", "")) and whole >= 0.70:
        return (round(whole, 4), "phonetic")
    # A very-close spelling (single typo) even without a phonetic hit.
    if whole >= 0.86:
        return (round(whole, 4), "edit")
    return None


class Corrector:
    def __init__(self, min_score: float = MIN_SCORE) -> None:
        self.min_score = min_score

    def match(self, name: str, candidates: list[dict], *,
              min_score: float | None = None) -> Match | None:
        """Best same-identity match for `name` among `candidates` (each a dict
        with 'name', optional 'aliases', and any id payload under 'id'/'ref'),
        or None. Compares against each candidate's canonical name AND aliases so
        a variant already recorded on one identity pulls the next mis-hearing in."""
        floor = self.min_score if min_score is None else min_score
        best: Match | None = None
        for c in candidates or []:
            ref = c.get("id", c.get("ref"))
            forms = [c.get("name", ""), *(c.get("aliases") or [])]
            for form in forms:
                scored = score_pair(name, form)
                if not scored:
                    continue
                sc, method = scored
                if sc >= floor and (best is None or sc > best.score):
                    best = Match(name=c.get("name", form), ref=ref,
                                 score=sc, method=method)
        return best


corrector = Corrector()
