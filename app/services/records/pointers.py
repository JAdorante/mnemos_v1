"""Team search pointers: their shape, their match keys, and one ranking.

Shared by the Org Record Service (the central index, Connected rung) and by
every node, which keeps the same pointers locally and answers a teammate's
search itself on the Sealed and Perimeter rungs. Stdlib only: the org image
copies this file next to canonical.py.

A pointer says who has an item and where it lives, never what it says:

  owner       the org member whose Sparrow holds the item (the service sets
              it from the caller's credential, never from the body)
  item_ref    the owner's node's own id for it ("ev:1234")
  kind        email | meeting | message | document | note | item
  tags        the project / org tags it is filed under
  people      who was on it (display names, never addresses)
  at          when it happened
  source      the system it lives in (google.mail, slack.message, ...)
  refs        how many facts were filed from it
  scope_id    the shared project it belongs to, or None
  title, snippet
              ONLY with a scope_id. Anything else has neither, because a
              subject line like "Pricing - approved" is already the content.

Search applies the same split wherever it runs, and applies it BEFORE
ranking, so an item a reader may not open never shapes what they see:

  open     the item is in a shared project that both the reader and the
           owner can read: the full pointer, and its title and snippet are
           match keys too
  routed   anything else: at most "ask <owner>", one row per owner, matched
           on tags and people only, naming only the words the reader typed
"""
from __future__ import annotations

import math
import re
from typing import Any, Callable, Iterable

KINDS = ("email", "meeting", "message", "document", "note", "item")
MAX_TAGS = 8
MAX_PEOPLE = 8
MAX_NAME = 80
MAX_TITLE = 160
MAX_SNIPPET = 360
MAX_REF = 64
MAX_SOURCE = 64
MAX_REFS = 1000
MAX_BATCH = 500

_WORD = re.compile(r"[a-z0-9][a-z0-9&]*")
_STOP = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "did", "do",
    "does", "for", "from", "had", "has", "have", "how", "i", "in", "is", "it",
    "its", "me", "my", "of", "on", "or", "our", "re", "so", "that", "the",
    "their", "them", "there", "this", "to", "us", "was", "we", "were", "what",
    "when", "where", "which", "who", "whom", "why", "will", "with", "you",
    "your", "about", "any", "anyone", "anything", "know", "knows", "find",
    "tell", "show", "get", "give", "need", "want", "please", "should",
    "would", "could", "team", "teammate", "teammates", "someone", "ask",
})
# Words too common in tags to route anyone on their own ("the X project").
_GENERIC = frozenset({
    "project", "projects", "group", "company", "inc", "llc", "ltd", "corp",
    "new", "phase", "review", "meeting", "office", "center", "services",
    "call", "sync", "notes", "note", "update", "updates", "general", "misc",
    "miscellaneous", "team", "org", "client", "account",
})
_RECENCY_CUE = re.compile(
    r"\b(latest|most recent|recent(ly)?|now|current(ly)?|still|newest|"
    r"last|so far|yet|updated?|changed?)\b", re.I)


def words(text: str | None) -> list[str]:
    """Lowercase content words, in order, without stopwords."""
    return [w for w in _WORD.findall((text or "").lower())
            if w not in _STOP and (len(w) >= 2 or w.isdigit())]


def _phrase(value: str) -> str:
    return " ".join(_WORD.findall((value or "").lower()))


def match_keys(values: Iterable[str]) -> list[str]:
    """What a tag or a name can be found by: the whole name, and each
    distinctive word of it ("Bayview Clinic Group" -> "bayview clinic group",
    "bayview", "clinic")."""
    out: set[str] = set()
    for v in values or ():
        p = _phrase(str(v))
        if not p:
            continue
        out.add(p)
        for w in p.split():
            if len(w) >= 3 and w not in _GENERIC and w not in _STOP:
                out.add(w)
    return sorted(out)[:64]


def text_keys(title: str | None, snippet: str | None) -> list[str]:
    return sorted({w for w in words(f"{title or ''} {snippet or ''}")
                   if len(w) >= 3 and w not in _GENERIC})[:96]


def query_keys(query: str | None) -> list[str]:
    """The reader's words, plus adjacent pairs so "Rachel Osei" also finds
    the full name."""
    ws = words(query)
    keys = set(ws)
    keys.update(f"{a} {b}" for a, b in zip(ws, ws[1:]))
    return sorted(keys)[:48]


def _clip(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n].strip()


def _names(values: Any, cap: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for v in values if isinstance(values, (list, tuple)) else []:
        s = _clip(v, MAX_NAME)
        # A display name, never an address: "Dana <d@x.com>" arrives cleaned
        # from a node, and a bare address is dropped here as well.
        if not s or "@" in s:
            continue
        k = s.casefold()
        if k not in seen:
            seen.add(k)
            out.append(s)
        if len(out) >= cap:
            break
    return out


def clean(p: dict[str, Any]) -> dict[str, Any]:
    """The pointer as it may be stored: known fields only, clamped, and no
    title or snippet unless it names a shared project. Raises ValueError for
    a pointer that cannot be stored at all."""
    if not isinstance(p, dict):
        raise ValueError("pointer must be an object")
    ref = _clip(p.get("item_ref"), MAX_REF)
    if not ref:
        raise ValueError("item_ref required")
    kind = str(p.get("kind") or "item").strip().lower()
    if kind not in KINDS:
        kind = "item"
    try:
        at = float(p.get("at"))
    except (TypeError, ValueError) as exc:
        raise ValueError("at must be a number") from exc
    if not math.isfinite(at) or at <= 0:
        raise ValueError("at must be a positive time")
    try:
        refs = max(0, min(MAX_REFS, int(p.get("refs") or 0)))
    except (TypeError, ValueError):
        refs = 0
    scope = _clip(p.get("scope_id"), MAX_REF) or None
    out = {"item_ref": ref, "kind": kind, "scope_id": scope,
           "tags": _names(p.get("tags"), MAX_TAGS),
           "people": _names(p.get("people"), MAX_PEOPLE),
           "at": at, "source": _clip(p.get("source"), MAX_SOURCE) or "sparrow",
           "refs": refs, "title": None, "snippet": None}
    if scope:
        out["title"] = _clip(p.get("title"), MAX_TITLE) or None
        out["snippet"] = _clip(p.get("snippet"), MAX_SNIPPET) or None
    return out


def without_text(p: dict[str, Any]) -> dict[str, Any]:
    """The same pointer outside any shared project."""
    return {**p, "scope_id": None, "title": None, "snippet": None}


def keys_for(p: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(meta keys, text keys). Text keys exist only for a shared-project
    pointer; a routed match can never come from them."""
    meta = match_keys([*(p.get("tags") or []), *(p.get("people") or [])])
    text = text_keys(p.get("title"), p.get("snippet")) if p.get("scope_id") \
        else []
    return meta, text


# ------------------------------------------------------------------ ranking --
WEIGHTS = {"coverage": 1.0, "person": 0.15, "tag": 0.10, "recency": 0.10,
           "references": 0.05}


def _covered(qwords: list[str], keys: set[str]) -> set[str]:
    out = set()
    for w in qwords:
        if w in keys:
            out.add(w)
    return out


def score(p: dict[str, Any], query: str, *, text_ok: bool,
          t_range: tuple[float, float] = (0.0, 0.0)) -> tuple[float, dict, set]:
    """(score, signals, the reader's words this pointer matched). Zero when
    nothing matched. `text_ok` is False for a pointer the reader may not
    open: its title and snippet then count for nothing."""
    qwords = [w for w in words(query) if len(w) >= 2]
    qkeys = set(query_keys(query))
    if not qwords:
        return 0.0, {}, set()
    tag_keys = set(match_keys(p.get("tags") or []))
    people_keys = set(match_keys(p.get("people") or []))
    tkeys = set(text_keys(p.get("title"), p.get("snippet"))) if text_ok \
        else set()
    on_tag = qkeys & tag_keys
    on_person = qkeys & people_keys
    on_text = qkeys & tkeys
    if not (on_tag or on_person or on_text):
        return 0.0, {}, set()
    full = set()
    for k in on_tag | on_person | on_text:
        full.update(k.split())
    covered = _covered(qwords, full | on_text)
    f = {"coverage": len(covered) / len(set(qwords)),
         "person": 1.0 if on_person else 0.0,
         "tag": 1.0 if on_tag else 0.0,
         "recency": 0.0,
         "references": min(1.0, math.log1p(int(p.get("refs") or 0))
                           / math.log1p(8))}
    lo, hi = t_range
    if _RECENCY_CUE.search(query or "") and hi > lo:
        f["recency"] = (float(p.get("at") or lo) - lo) / (hi - lo)
    s = sum(WEIGHTS[k] * v for k, v in f.items())
    return s, {k: round(v, 3) for k, v in f.items() if v}, covered


def search(pointers: Iterable[dict[str, Any]], query: str, *, reader: str,
           readable: set[str], owner_can_read: Callable[[str, str], bool],
           owner_name: Callable[[str], str] = lambda m: m,
           scope_name: Callable[[str], str] = lambda s: s,
           limit: int = 8, ask_limit: int = 3) -> dict[str, list]:
    """One permission-filtered ranking over stored pointers.

    `pointers` are stored rows with an `owner` field. Each is put in its tier
    first: open when its scope is a shared project the reader can read AND
    the owner still can, routed otherwise. Only then is anything scored, and
    each tier is ranked on its own, so the order of open results never
    depends on what the reader cannot see. The reader's own items are
    skipped: their own Sparrow already has them."""
    rows = [p for p in pointers if p.get("owner") and p["owner"] != reader]
    times = [float(p.get("at") or 0.0) for p in rows if p.get("at")]
    t_range = (min(times), max(times)) if times else (0.0, 0.0)
    opened, routed = [], []
    for p in rows:
        sid = p.get("scope_id")
        if sid and sid in readable and owner_can_read(p["owner"], sid):
            opened.append(p)
        else:
            routed.append(without_text(p))
    hits = []
    for p in opened:
        s, sig, _ = score(p, query, text_ok=True, t_range=t_range)
        if s > 0:
            hits.append((s, p, sig))
    hits.sort(key=lambda t: (-t[0], -float(t[1].get("at") or 0)))
    out_open = [{
        "owner": p["owner"], "owner_name": owner_name(p["owner"]),
        "kind": p["kind"], "project": scope_name(p["scope_id"]),
        "scope_id": p["scope_id"], "people": list(p.get("people") or []),
        "at": p["at"], "source": p.get("source"),
        "title": p.get("title"), "snippet": p.get("snippet"),
        "location": {"owner": p["owner"], "node": p.get("node_id"),
                     "ref": p["item_ref"], "system": p.get("source")},
        "score": round(s, 4), "signals": sig}
        for s, p, sig in hits[:max(0, int(limit))]]
    by_owner: dict[str, list] = {}
    for p in routed:
        s, _sig, covered = score(p, query, text_ok=False, t_range=t_range)
        if s > 0:
            by_owner.setdefault(p["owner"], []).append((s, covered))
    asks = []
    for owner, got in by_owner.items():
        best = max(s for s, _ in got)
        matched = sorted(set().union(*(c for _, c in got)))
        asks.append((best + 0.05 * math.log1p(len(got)), owner, matched))
    asks.sort(key=lambda t: (-t[0], t[1]))
    out_ask = [{"owner": o, "owner_name": owner_name(o), "matched": m,
                "route": "ask"} for _s, o, m in asks[:max(0, int(ask_limit))]]
    return {"pointers": out_open, "ask": out_ask}
