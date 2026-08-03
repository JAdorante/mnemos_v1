"""KG v2 — identity keys (Change 1, post-review).

Merge identity must not carry semantics: every node gets an opaque
`canonical_id` (random 128-bit hex) minted at create time, and the
normalized name is demoted to a *blocking key* — a lookup hint stored in
`kg_node_keys`, where the same key value MAY map to multiple nodes.
That ambiguity is exactly what the resolver adjudicates; nothing here
enforces global uniqueness of a key value, and nothing anywhere may
parse a canonical_id for meaning.

Stdlib-only on purpose (storage.py imports this).
"""
from __future__ import annotations

import re
import unicodedata

# OCR / stylized-text confusables folded before phonetic hashing so
# "0penAI" and "OpenAI" land on the same phonetic blocking key.
_CONFUSABLES = str.maketrans({
    "0": "o", "1": "l", "3": "e", "4": "a", "5": "s", "7": "t", "$": "s",
    "@": "a", "!": "i",
})

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9 ]+")


def norm_name(name: str) -> str:
    """Case/diacritic/whitespace-normalized form — the `norm_name` blocking key."""
    s = unicodedata.normalize("NFKD", (name or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _NON_ALNUM.sub(" ", s.lower())
    return _WS.sub(" ", s).strip()


def phonetic(name: str) -> str:
    """Cheap phonetic-ish key: fold confusables, drop non-leading vowels,
    collapse doubles. Coarse by design — it's a *blocking* key (recall),
    not a match decision (precision)."""
    s = norm_name((name or "").translate(_CONFUSABLES))
    out: list[str] = []
    for word in s.split():
        kept = [word[0]]
        for ch in word[1:]:
            if ch in "aeiou":
                continue
            if kept[-1] != ch:
                kept.append(ch)
        out.append("".join(kept))
    return " ".join(out)


def blocking_keys(name: str, *, key_type: str = "norm_name") -> list[tuple[str, str]]:
    """(key_type, key_value) pairs to write for one observed name/alias."""
    n = norm_name(name)
    if not n:
        return []
    keys = [(key_type, n)]
    p = phonetic(name)
    if p:
        keys.append(("phonetic", p))
    return keys
