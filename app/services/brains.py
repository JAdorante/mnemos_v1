"""Brains — one subject, a fixed set of typed sections, facts an agent can use.

The constellation shows what has the user's attention; a brain shows what
Sparrow *knows* about one subject (a deal, a person, an org), grouped into
the sections an analyst would expect (Thesis, People, Status, Commitments,
Risks, Competitors, Decisions, Open questions). The compiler here reads the
tables that already exist — facts, claims, commitments, tasks, relations,
kg_predicates, entity/person attrs — and writes only `brain_items` (derived,
safe to drop) and system rows in `brain_gaps`. `brain_overrides` is the
user's word and is only ever written by the override functions.

Section assignment runs in a fixed order and the first match wins:

  1. a `move` or `hide` row in brain_overrides;
  2. the deterministic rules a section's schema file names in `sources`
     (claims by kind, open commitments, typed edges, attrs);
  3. the local keyword classifier for what is left (no cloud call);
  4. anything still unassigned stays out of the brain and is counted.

Layout is a pure function of stable ids (Fibonacci sphere for sections, a
hashed cone for leaves, a hashed direction plus activity for brains), so two
rebuilds over the same data give identical positions and a new fact never
moves anything that was already there.

The packet compiler (`compile_packet`) is the ONLY path that hands brain
text to an agent. It calls egress.decide() per item, never includes verbatim
quotes or personal-only claims, and logs one egress row per packet — allowed
or denied — so it shows in /privacy/egress.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.storage import Store, get_store

SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "brains"
TYPES = ("deal", "portco", "person", "org")
MIN_ITEMS = 3                       # a subject with fewer assigned items is no brain
ACTIVITY_WINDOW_S = 30 * 86400.0
ACTIVITY_FLOOR = 0.15
PIN_BOOST = 0.25
PENDING_APPROVAL = 0.6
PACKET_DEFAULT_TOKENS = 2000
PACKET_MAX_TOKENS = 8000
PACKET_MIN_TOKENS = 100
OVERVIEW_R_MIN, OVERVIEW_R_MAX = 2.0, 10.0
LEAF_R_MIN, LEAF_R_MAX = 1.4, 2.3
LEAF_CONE = 0.45                    # radians, half-angle of the leaf cone
RELAX_STEPS = 30

ENTITY_KIND_TO_TYPE = {
    "project": "deal", "deal": "deal",
    "org": "org", "company": "org", "organization": "org", "organisation": "org",
    "portco": "portco",
}
_COMPETES = frozenset({"competes_with", "competitor_of", "rival_of", "competes"})
_PEOPLE_PREDS = frozenset({
    "works_at", "part_of", "member_of", "affiliated_with", "founded",
    "works_on", "leads", "owns", "advises", "represents", "contact_at",
    "reports_to", "manages", "sponsor_of", "champion_of"})
_ROLE_ATTR_KEYS = ("role", "title", "position", "company", "org", "team")
_KEYWORDS = {
    "thesis": re.compile(
        r"\b(thesis|why we|upside|opportunit\w*|growth|margin\w*|moat|tam|"
        r"market size|unit economics|bet on|we like|rationale|value creation|"
        r"ebitda|arr|revenue)\b", re.I),
    "risks": re.compile(
        r"\b(risk\w*|concern\w*|objection\w*|worr\w+|blocker\w*|red flag\w*|"
        r"downside|exposure|churn|liabilit\w*|lawsuit|litigation|pushback|"
        r"hesitant|skeptical|sceptical|threat\w*)\b", re.I),
    "competitors": re.compile(
        r"\b(competitor\w*|compet\w+ with|rival\w*|versus|vs\.?|alternative to|"
        r"also looking at|other bidder\w*|also bidding)\b", re.I),
    "performance": re.compile(
        r"\b(revenue|ebitda|arr|mrr|growth|margin\w*|burn|runway|kpi\w*|"
        r"quarter\w*|forecast|budget|headcount)\b", re.I),
}
_HEADING = re.compile(r"(^|\s)#{1,6}(?=\s|$)")
_FENCE = re.compile(r"`{3,}")
_WS = re.compile(r"\s+")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep",
           "Oct", "Nov", "Dec")


class BrainError(Exception):
    status = 400


class BrainNotFound(BrainError):
    status = 404


class SectionNotFound(BrainError):
    status = 404


class BadPacketRequest(BrainError):
    status = 422


# ------------------------------------------------------------------ schema --
@lru_cache(maxsize=None)
def schema_for(btype: str) -> dict:
    if btype not in TYPES:
        raise BrainError(f"unknown brain type {btype!r}")
    raw = json.loads((SCHEMA_DIR / f"{btype}.json").read_text("utf-8"))
    secs = sorted(raw.get("sections") or [], key=lambda s: int(s.get("order", 0)))
    for i, s in enumerate(secs):
        s.setdefault("required", False)
        s.setdefault("max_items", 20)
        s.setdefault("sources", [])
        s["index"] = i
    raw["sections"] = secs
    raw.setdefault("version", f"{btype}-v1")
    raw.setdefault("phrase", f"a {btype}")
    return raw


def schema_version(btype: str) -> str:
    return str(schema_for(btype)["version"])


def sections(btype: str) -> list[dict]:
    return list(schema_for(btype)["sections"])


def section(btype: str, key: str) -> dict | None:
    for s in sections(btype):
        if s["key"] == key:
            return s
    return None


def schema_stamp() -> str:
    """All schema versions in one string: a change means a full rebuild."""
    return "|".join(f"{t}={schema_version(t)}" for t in TYPES)


# ------------------------------------------------------------------- hashing --
def _hash32(s: str) -> int:
    """FNV-1a + avalanche (the constellation's anchor hash), 32 bits."""
    h = 2166136261
    for ch in s:
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    h ^= h >> 15
    h = (h * 0x2C1B3C6D) & 0xFFFFFFFF
    h ^= h >> 12
    h = (h * 0x297A2D39) & 0xFFFFFFFF
    h ^= h >> 15
    return h


def _h01(s: str, salt: str) -> float:
    return _hash32(f"{salt}:{s}") / 4294967296.0


def _norm(name: str | None) -> str:
    from app.services.entity_alias import normalize
    return normalize(name or "")


def parse_brain_id(brain_id: str) -> tuple[str, int]:
    s = (brain_id or "").strip()
    m = re.match(r"^(entity|person):(\d+)$", s)
    if not m:
        raise BrainNotFound(f"no brain {brain_id!r}")
    return m.group(1), int(m.group(2))


def brain_id_of(subject_type: str, subject_id: int) -> str:
    return f"{subject_type}:{int(subject_id)}"


# ------------------------------------------------------------------- text ---
def clean_text(text: str | None) -> str:
    """One display line: no markdown headings, no code fences, no newlines —
    an item that reads "## Decisions" must not fake a section in a packet."""
    t = _FENCE.sub(" ", str(text or ""))
    t = _WS.sub(" ", t).strip()
    t = _HEADING.sub(r"\1", t)
    return _WS.sub(" ", t).strip()


def _source_label(source: str | None, modality: str | None = None) -> str:
    s = (source or "").lower()
    m = (modality or "").lower()
    if "meeting" in s:
        return "Meeting"
    if "mail" in s:
        return "Email"
    if "imessage" in s or "sms" in s or "text" == m:
        return "Text"
    if "calendar" in s:
        return "Calendar"
    if "chat" in s or "typed" in s or s == "user":
        return "Chat"
    if "screen" in s or "ocr" in s or "vision" in m:
        return "Screen"
    if "audio" in s or "whisper" in s or "audio" in m or "ambient" in s:
        return "Conversation"
    if "doc" in s or "pdf" in s:
        return "Document"
    if "web" in s or "browser" in s:
        return "Web"
    if "external" in s:
        return "Assistant"
    head = s.split(".")[0].strip()
    return head.title() if head else "Memory"


def _date(ts: float | None) -> str:
    if not ts:
        return ""
    try:
        lt = time.localtime(float(ts))
    except (TypeError, ValueError, OverflowError):
        return ""
    return f"{_MONTHS[lt.tm_mon - 1]} {lt.tm_mday}"


def _age_days(ts: float | None, now: float) -> float:
    if not ts:
        return 21.0
    return max(0.0, (now - float(ts)) / 86400.0)


# -------------------------------------------------------------------- index --
class _Index:
    """Everything a compile reads, loaded once per rebuild. `light=True`
    loads only what naming a subject needs (people, entities, overrides):
    the read path uses it for labels and types without touching facts."""

    def __init__(self, store: Store, now: float, *, light: bool = False) -> None:
        from app.services import graph
        self.store = store
        self.now = now
        self.people = {p["id"]: p for p in graph._real_people(store)}
        self.entities = {e["id"]: e for e in graph._real_entities(store)}
        self.hidden = store.constellation_hidden_nodes()
        self.canonical = _canonical_ids(store)
        self.overrides = _all_overrides(store)
        self._text_links: dict[tuple[str, int], set[int]] | None = None
        self._subject_links: dict[tuple[str, int], set[int]] = {}
        self._lower: list[tuple[int, str]] | None = None
        if light:
            return
        self.pinned = store.user_pinned_nodes()
        try:
            from app.services import self_profile
            self.self_pid = self_profile.self_person_id(store)
        except Exception:
            self.self_pid = None

        facts = []
        try:
            facts = store.list_facts(limit=10_000_000)
        except Exception:
            facts = []
        self.facts: dict[int, dict] = {}
        for f in facts:
            if (f.get("review") or "") == "dismissed" or f.get("superseded_by"):
                continue
            if f.get("kind") in ("task", "commitment") and (f.get("status") or "open") != "open":
                continue
            if not str(f.get("text") or "").strip():
                continue
            self.facts[int(f["fact_id"])] = f
        self.fact_people = _fact_person_ids(store)       # fact_id -> {role: pid}
        self.claims = _live_claims(store)
        self.relations = _all_relations(store)
        self.kg = _active_predicates(store)
        self.person_attrs = _all_attrs(store, "person_attrs", "person_id")
        self.entity_attrs = _all_attrs(store, "entity_attrs", "entity_id")
        self.gaps = _all_gaps(store)
        # fact -> subjects via typed edges
        self.fact_entities: dict[int, set[int]] = {}
        self.fact_persons: dict[int, set[int]] = {}
        for r in self.relations:
            if r["subj_type"] == "fact" and r["obj_type"] == "entity":
                self.fact_entities.setdefault(int(r["subj_id"]), set()).add(int(r["obj_id"]))
            elif r["subj_type"] == "person" and r["obj_type"] == "fact":
                self.fact_persons.setdefault(int(r["obj_id"]), set()).add(int(r["subj_id"]))
        for fid, roles in self.fact_people.items():
            for pid in roles.values():
                if pid:
                    self.fact_persons.setdefault(fid, set()).add(int(pid))
        self._name_index = _NameIndex(self.people, self.entities)

    def text_links(self) -> dict[tuple[str, int], set[int]]:
        """subject -> fact ids whose text names the subject (name or alias).
        One pass over every fact through the inverted name index: the full
        rebuild calls this once; a dirty pass never does (see below)."""
        if self._text_links is None:
            out: dict[tuple[str, int], set[int]] = {}
            for fid, f in self.facts.items():
                for key in self._name_index.subjects_in(f.get("text") or ""):
                    out.setdefault(key, set()).add(fid)
            self._text_links = out
        return self._text_links

    def _links_for_subject(self, stype: str, sid: int) -> set[int]:
        """Text links for ONE subject: a substring prefilter over lowercased
        fact text, then the word-boundary pattern. Linear in facts, so a
        dirty pass recompiling a few brains never pays for the whole index."""
        if self._text_links is not None:
            return set(self._text_links.get((stype, sid), set()))
        key = (stype, sid)
        hit = self._subject_links.get(key)
        if hit is not None:
            return set(hit)
        from app.services import graph
        row = (self.entities if stype == "entity" else self.people).get(sid) or {}
        pats = (graph._entity_patterns(row) if stype == "entity"
                else graph._person_patterns(row))
        needles = []
        for pat in pats:
            raw = pat.pattern[2:-2].replace("\\", "").lower()
            needles.append((raw, pat))
        if self._lower is None:
            self._lower = [(fid, (f.get("text") or "").lower()) for fid, f in self.facts.items()]
        found: set[int] = set()
        for fid, low in self._lower:
            for raw, pat in needles:
                if raw in low and pat.search(low):
                    found.add(fid)
                    break
        self._subject_links[key] = found
        return set(found)

    def linked_facts(self, stype: str, sid: int) -> set[int]:
        out = self._links_for_subject(stype, sid)
        if stype == "entity":
            out |= {fid for fid, es in self.fact_entities.items() if sid in es}
        else:
            out |= {fid for fid, ps in self.fact_persons.items() if sid in ps}
        return {fid for fid in out if fid in self.facts}

    def subject_refs(self, stype: str, sid: int) -> set[str]:
        row = (self.entities if stype == "entity" else self.people).get(sid) or {}
        names = [row.get("name") or ""] + list(row.get("aliases") or [])
        refs = {f"{stype}:{_norm(n)}" for n in names if _norm(n)}
        if stype == "person" and self.self_pid == sid:
            refs.add("self")
        return refs

    def label(self, stype: str, sid: int) -> str:
        row = (self.entities if stype == "entity" else self.people).get(sid) or {}
        return str(row.get("name") or f"{stype} {sid}")


class _NameIndex:
    """First-word inverted index so linking facts to subjects by name is
    O(words), not O(facts x subjects)."""

    def __init__(self, people: dict, entities: dict) -> None:
        from app.services import graph
        self.first: dict[str, list[tuple[tuple[str, int], Any]]] = {}
        for pid, p in people.items():
            for pat in graph._person_patterns(p):
                self._add(("person", pid), pat)
        for eid, e in entities.items():
            for pat in graph._entity_patterns(e):
                self._add(("entity", eid), pat)

    def _add(self, key: tuple[str, int], pat) -> None:
        # pattern text is \bESCAPED\b — recover the first word for the bucket
        raw = pat.pattern[2:-2]
        word = re.split(r"\\?\s", raw, 1)[0].replace("\\", "").lower()
        if not word:
            return
        self.first.setdefault(word, []).append((key, pat))

    def subjects_in(self, text: str) -> set[tuple[str, int]]:
        out: set[tuple[str, int]] = set()
        if not text:
            return out
        seen: set[str] = set()
        for w in re.findall(r"[\w'’.-]+", text.lower()):
            w = w.strip(".'’-")
            if w in seen or w not in self.first:
                continue
            seen.add(w)
            for key, pat in self.first[w]:
                if key in out:
                    continue
                if pat.search(text):
                    out.add(key)
        return out


def _rows(store: Store, sql: str, args: tuple = ()) -> list[dict]:
    with store._lock:
        rows = store._conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def _canonical_ids(store: Store) -> dict[tuple[str, int], str]:
    out: dict[tuple[str, int], str] = {}
    for tbl, st in (("entities", "entity"), ("people", "person")):
        try:
            for r in _rows(store, f"SELECT id, canonical_id FROM {tbl}"):
                out[(st, int(r["id"]))] = str(r.get("canonical_id") or f"{st}:{r['id']}")
        except Exception:
            pass
    return out


def _fact_person_ids(store: Store) -> dict[int, dict[str, int | None]]:
    out: dict[int, dict] = {}
    try:
        for r in _rows(store, "SELECT fact_id, owner_person_id FROM tasks "
                              "WHERE status = 'open'"):
            out.setdefault(int(r["fact_id"]), {})["owner"] = r["owner_person_id"]
        for r in _rows(store, "SELECT fact_id, from_person_id, to_person_id "
                              "FROM commitments WHERE status = 'open'"):
            d = out.setdefault(int(r["fact_id"]), {})
            d["from"] = r["from_person_id"]
            d["to"] = r["to_person_id"]
    except Exception:
        pass
    return out


def _live_claims(store: Store) -> list[dict]:
    try:
        from app.services.records import node_store
        rows = node_store.list_claims(store, limit=10_000_000)
    except Exception:
        return []
    dead = {"rejected", "discarded", "expired"}
    return [c for c in rows if (c.get("status") or "") not in dead]


def _all_relations(store: Store) -> list[dict]:
    try:
        return _rows(store, "SELECT * FROM relations ORDER BY id")
    except Exception:
        return []


def _active_predicates(store: Store) -> list[dict]:
    try:
        return store.list_kg_predicates(statuses=("active",), limit=10_000_000)
    except Exception:
        return []


def _all_attrs(store: Store, table: str, id_col: str) -> dict[int, dict[str, dict]]:
    out: dict[int, dict[str, dict]] = {}
    try:
        for r in _rows(store, f"SELECT {id_col} AS sid, key, value, fact_id, "
                              f"updated_at FROM {table}"):
            out.setdefault(int(r["sid"]), {})[str(r["key"])] = r
    except Exception:
        pass
    return out


def _all_overrides(store: Store) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = {}
    for r in _rows(store, "SELECT * FROM brain_overrides ORDER BY id"):
        out.setdefault((r["subject_type"], int(r["subject_id"])), []).append(r)
    return out


def _all_gaps(store: Store) -> dict[tuple[str, int], list[dict]]:
    out: dict[tuple[str, int], list[dict]] = {}
    for r in _rows(store, "SELECT * FROM brain_gaps ORDER BY id"):
        out.setdefault((r["subject_type"], int(r["subject_id"])), []).append(r)
    return out


# ----------------------------------------------------------------- subjects --
def subjects(store: Store, idx: _Index | None = None) -> list[dict]:
    """Every candidate subject with its brain type (None = no type in v1)."""
    idx = idx or _Index(store, time.time(), light=True)
    out: list[dict] = []
    for eid, e in sorted(idx.entities.items()):
        if ("entity", eid) in idx.hidden or e.get("hidden"):
            continue
        out.append({"subject_type": "entity", "subject_id": eid,
                    "label": e["name"], "kind": e.get("kind"),
                    "type": brain_type(idx, "entity", eid)})
    for pid, p in sorted(idx.people.items()):
        if ("person", pid) in idx.hidden:
            continue
        out.append({"subject_type": "person", "subject_id": pid,
                    "label": p["name"], "kind": "person",
                    "type": brain_type(idx, "person", pid)})
    return out


def brain_type(idx: _Index, stype: str, sid: int) -> str | None:
    for o in idx.overrides.get((stype, sid), []):
        if o["action"] == "set_type" and o.get("brain_type") in TYPES:
            return o["brain_type"]
    if stype == "person":
        return "person"
    kind = str((idx.entities.get(sid) or {}).get("kind") or "").lower()
    return ENTITY_KIND_TO_TYPE.get(kind)


# -------------------------------------------------------------------- items --
def _item(item_type: str, item_id: Any, text: str, *, review: str,
          confidence: float | None, event_id: int | None, source: str,
          observed_at: float | None, privacy: str | None, evidence: str | None,
          tags: dict | None = None) -> dict:
    return {
        "item_type": item_type, "item_id": str(item_id), "text": clean_text(text),
        "review_state": review, "confidence": confidence,
        "source_event_id": event_id, "source_label": source,
        "observed_at": observed_at, "privacy_class": privacy,
        "evidence_id": evidence, "tags": tags or {},
    }


def _fact_review(f: dict) -> str:
    return "approved" if (f.get("review") or "") in ("approved", "edited") else "pending"


def _claim_review(c: dict) -> str:
    if c.get("record_ref") or (c.get("status") or "") in ("approved", "recorded"):
        return "approved"
    return "pending"


def _fact_privacy(f: dict) -> str:
    cls = f.get("source_privacy_class")
    if cls:
        return str(cls)
    try:
        from app.services import egress
        return egress.classify(f.get("text") or "", source=f.get("event_source") or "")
    except Exception:
        return "sensitive"


def _fact_item(f: dict, idx: _Index, *, text: str | None = None,
               item_type: str | None = None) -> dict:
    fid = int(f["fact_id"])
    kind = f.get("kind") or "claim"
    it = item_type or ("commitment" if kind == "commitment" else
                       "task" if kind == "task" else "fact")
    return _item(
        it, fid, text or f.get("text") or "",
        review=_fact_review(f), confidence=f.get("confidence"),
        event_id=f.get("source_event_id"),
        source=_source_label(f.get("event_source"), f.get("source_modality")),
        observed_at=f.get("source_time") or f.get("extracted_at"),
        privacy=_fact_privacy(f), evidence=f"fact:{fid}",
        tags={"fact_kind": kind, "due": f.get("due"),
              "pinned": ("fact", fid) in idx.pinned,
              "section_hint": (f.get("section_hint") or None)})


def _claim_text(c: dict) -> str:
    v = c.get("value") or {}
    if c.get("kind") == "status":
        state = str(v.get("state") or "").strip()
        extra = str(v.get("text") or "").strip()
        dim = str(c.get("predicate") or "").replace("_", " ").replace(".", " ")
        head = f"{dim}: {state}" if dim and dim not in ("status",) else state
        return f"{head}. {extra}".strip(". ") if extra else head
    t = str(v.get("text") or "").strip()
    if c.get("kind") == "commitment" and v.get("due"):
        t = f"{t} (due {v['due']})"
    if not t:
        t = f"{c.get('subject_label') or ''} {c.get('predicate') or ''} {v.get('value') or ''}".strip()
    return t


def _claim_item(c: dict) -> dict:
    ev = c.get("evidence") or []
    first = ev[0] if ev and isinstance(ev[0], dict) else {}
    obs = first.get("t") or c.get("created_at")
    src = _source_label(str(first.get("source") or c.get("capture_source") or ""))
    return _item(
        "claim", c["id"], _claim_text(c),
        review=_claim_review(c), confidence=c.get("confidence"),
        event_id=first.get("event_id"), source=src, observed_at=obs,
        privacy=c.get("privacy_class") or "internal", evidence=None,
        tags={"claim_kind": c.get("kind"), "record_ref": c.get("record_ref"),
              "personal_only": bool(c.get("personal_only")),
              "conflict_with": c.get("conflict_with"),
              "status": c.get("status")})


def _commitment_sentence(f: dict) -> str:
    text = str(f.get("text") or "").strip()
    frm, to = f.get("from_person"), f.get("to_person")
    if frm and to:
        s = f"{frm} owes {to}: {text}"
    elif frm:
        s = f"{frm}: {text}"
    elif to:
        s = f"Owed to {to}: {text}"
    else:
        s = text
    if f.get("due"):
        s += f" (due {f['due']})"
    return s


def _task_sentence(f: dict, *, prefix: str = "") -> str:
    s = str(f.get("text") or "").strip()
    if f.get("owner"):
        s = f"{f['owner']}: {s}"
    if f.get("due"):
        s += f" (due {f['due']})"
    return f"{prefix}{s}"


def _humanize_pred(p: str) -> str:
    return str(p or "").replace("_", " ")


def _role_suffix(idx: "_Index", other: tuple[str, int]) -> str:
    """" (CFO)" when the linked person has a hand-set role or title."""
    if other[0] != "person":
        return ""
    attrs = idx.person_attrs.get(int(other[1])) or {}
    for key in ("role", "title", "position"):
        a = attrs.get(key)
        if a and str(a.get("value") or "").strip():
            return f" ({str(a['value']).strip()})"
    return ""


def _edge_item(kind: str, row: dict, text: str, *, other: tuple[str, int],
               observed_at: float | None) -> dict:
    """kind: 'kg' (kg_predicates row) or 'rel' (relations row)."""
    if kind == "kg":
        approved = bool(row.get("protected")) or str(row.get("layer") or "") == "user"
        conf = float(row.get("confidence") or 0.5)
    else:
        approved = str(row.get("origin") or "") == "user"
        conf = float(row.get("confidence") or 0.6)
    return _item(
        "relation", f"{kind}:{row['id']}", text,
        review="approved" if approved else "pending", confidence=conf,
        event_id=row.get("source_event_id"), source="Graph",
        observed_at=observed_at, privacy="internal",
        evidence=f"{other[0]}:{other[1]}",
        tags={"predicate": row.get("predicate"), "other": other,
              "weight": float(row.get("weight") or 1.0)})


def _collect_items(idx: _Index, stype: str, sid: int, label: str) -> list[dict]:
    """Every item that could belong to this brain, unassigned, with tags."""
    items: dict[tuple[str, str], dict] = {}

    def add(it: dict) -> None:
        items.setdefault((it["item_type"], it["item_id"]), it)

    # Facts linked by edge or by name.
    for fid in sorted(idx.linked_facts(stype, sid)):
        f = idx.facts[fid]
        kind = f.get("kind")
        if kind == "commitment":
            add(_fact_item(f, idx, text=_commitment_sentence(f)))
        elif kind == "task":
            add(_fact_item(f, idx, text=_task_sentence(f)))
        else:
            add(_fact_item(f, idx))
    # Claims about this subject (and commitments naming this person).
    refs = idx.subject_refs(stype, sid)
    lab_n = _norm(label)
    for c in idx.claims:
        if c.get("subject_ref") in refs:
            add(_claim_item(c))
        elif stype == "person" and c.get("kind") == "commitment":
            v = c.get("value") or {}
            if lab_n and lab_n in (_norm(v.get("owner")), _norm(v.get("counterparty"))):
                add(_claim_item(c))
    # Typed edges to people / orgs / competitors. kg_predicates and relations
    # are dual-written for the same edge; the belief row wins, the legacy
    # relation only fills in edges the KG does not hold.
    seen_edges: set[tuple] = set()
    for r in idx.kg:
        st, si = r["subj_type"], int(r["subj_id"])
        ot, oi = r["obj_type"], int(r["obj_id"])
        if (st, si) == (stype, sid):
            other = (ot, oi)
        elif (ot, oi) == (stype, sid):
            other = (st, si)
        else:
            continue
        oname = idx.label(*other) if other[1] in (idx.people if other[0] == "person" else idx.entities) else None
        if not oname:
            continue
        pred = str(r.get("predicate") or "")
        if (st, si) == (stype, sid):
            text = f"{label} {_humanize_pred(pred)} {oname}"
        else:
            text = f"{oname} {_humanize_pred(pred)} {label}"
        text += _role_suffix(idx, other)
        seen_edges.add((st, si, pred, ot, oi))
        add(_edge_item("kg", r, text, other=other,
                       observed_at=r.get("last_seen") or r.get("updated_at")))
    for r in idx.relations:
        st, si = r["subj_type"], int(r["subj_id"])
        ot, oi = r["obj_type"], int(r["obj_id"])
        if (st, si) == (stype, sid):
            other = (ot, oi)
        elif (ot, oi) == (stype, sid):
            other = (st, si)
        else:
            continue
        if other[0] not in ("person", "entity"):
            continue
        if other[1] not in (idx.people if other[0] == "person" else idx.entities):
            continue
        pred = str(r.get("predicate") or "")
        if pred in ("pins", "constellation_hidden", "mentioned_in", "about",
                    "evidenced_by"):
            continue
        if (st, si, pred, ot, oi) in seen_edges:
            continue
        oname = idx.label(*other)
        if pred == "co_occurs":
            if float(r.get("weight") or 0) < 2 or (st, si) != (stype, sid):
                continue
            text = f"Often discussed with {oname}"
        elif (st, si) == (stype, sid):
            text = f"{label} {_humanize_pred(pred)} {oname}"
        else:
            text = f"{oname} {_humanize_pred(pred)} {label}"
        text += _role_suffix(idx, other)
        add(_edge_item("rel", r, text, other=other, observed_at=r.get("created_at")))
    # Attrs the user set by hand.
    attrs = (idx.entity_attrs if stype == "entity" else idx.person_attrs).get(sid) or {}
    for key, a in sorted(attrs.items()):
        if stype == "entity" and key != "status":
            continue
        if stype == "person" and key not in _ROLE_ATTR_KEYS:
            continue
        fid = a.get("fact_id")
        iid = str(int(fid)) if fid else f"attr:{key}"
        text = (f"Status: {a['value']}" if key == "status"
                else f"{label}'s {key}: {a['value']}")
        it = _item("fact", iid, text, review="approved", confidence=1.0,
                   event_id=None, source="You", observed_at=a.get("updated_at"),
                   privacy="internal", evidence=f"fact:{fid}" if fid else None,
                   tags={"attr": key})
        items[(it["item_type"], it["item_id"])] = it   # the attr wins over the raw fact
    return list(items.values())


# --------------------------------------------------------------- assignment --
def _matches_source(src: str, it: dict, stype: str, sec: dict | None = None) -> bool:
    t = it["tags"]
    ty = it["item_type"]
    if src == "hint:model":
        # the extractor's or the backlog pass's guess, honoured only after
        # every deterministic rule had its turn
        hint = t.get("section_hint")
        if not hint or ty != "fact" or t.get("attr"):
            return False
        return hint in ((sec or {}).get("hints") or [])
    if src == "people_links":
        return ty == "relation" and t.get("other", ("",))[0] == "person" and (
            stype == "entity") and t.get("predicate") not in _COMPETES
    if src == "affiliations":
        return (ty == "relation" and stype == "person"
                and t.get("other", ("",))[0] == "entity"
                and t.get("predicate") not in _COMPETES)
    if src == "person_attrs":
        return ty == "fact" and t.get("attr") in _ROLE_ATTR_KEYS
    if src == "person_links":
        return ty == "relation" and stype == "person" and t.get("other", ("",))[0] == "person"
    if src == "status_claims":
        return ty == "claim" and t.get("claim_kind") == "status"
    if src == "status_attr":
        return ty == "fact" and t.get("attr") == "status"
    if src == "next_task":
        return ty == "task"
    if src == "open_tasks":
        return ty == "task"
    if src == "open_commitments":
        return ty == "commitment"
    if src == "commitment_claims":
        return ty == "claim" and t.get("claim_kind") == "commitment"
    if src == "decision_claims":
        return ty == "claim" and t.get("claim_kind") == "decision"
    if src == "competes_with":
        return ty == "relation" and t.get("predicate") in _COMPETES
    if src == "question_facts":
        return ty == "fact" and t.get("fact_kind") == "question"
    if src.startswith("keywords:"):
        bucket = src.split(":", 1)[1]
        rx = _KEYWORDS.get(bucket)
        if rx is None:
            return False
        if ty == "fact" and t.get("fact_kind") in ("claim", "idea", None) and not t.get("attr"):
            return bool(rx.search(it["text"]))
        if ty == "claim" and t.get("claim_kind") == "fact":
            return bool(rx.search(it["text"]))
        return False
    if src == "remaining_facts":
        if ty == "fact" and t.get("fact_kind") in ("claim", "idea", None) and not t.get("attr"):
            return True
        return ty == "claim" and t.get("claim_kind") == "fact"
    return False


def _assign(idx: _Index, stype: str, sid: int, btype: str, items: list[dict]
            ) -> tuple[dict[str, list[dict]], int, int]:
    """Items -> {section_key: [items]}, plus (unassigned, hidden) counts."""
    secs = sections(btype)
    keys = {s["key"] for s in secs}
    by_section: dict[str, list[dict]] = {s["key"]: [] for s in secs}
    hidden = 0
    unassigned = 0
    overrides = {}
    for o in idx.overrides.get((stype, sid), []):
        if o["action"] in ("move", "hide") and o.get("item_type") and o.get("item_id"):
            overrides[(o["item_type"], str(o["item_id"]))] = o   # last word wins
    rest: list[dict] = []
    for it in items:
        o = overrides.get((it["item_type"], it["item_id"]))
        if o is None:
            rest.append(it)
        elif o["action"] == "hide":
            hidden += 1
        elif o["action"] == "move" and o.get("section_key") in keys:
            it["assigned_by"] = "user"
            by_section[o["section_key"]].append(it)
        else:
            rest.append(it)

    structural = [(s, src) for s in secs for src in s["sources"]
                  if not src.startswith("keywords:")
                  and src not in ("remaining_facts", "gaps", "hint:model")]
    keyword = [(s, src) for s in secs for src in s["sources"] if src.startswith("keywords:")]
    hinted = [(s, src) for s in secs for src in s["sources"] if src == "hint:model"]
    remaining = [(s, src) for s in secs for src in s["sources"] if src == "remaining_facts"]
    for pass_rules in (structural, keyword, hinted, remaining):
        left: list[dict] = []
        for it in rest:
            placed = False
            for s, src in pass_rules:
                if _matches_source(src, it, stype, s):
                    it["assigned_by"] = "model" if src == "hint:model" else "rule"
                    if src == "next_task" and it["item_type"] == "task":
                        it["text"] = clean_text("Next step: " + it["text"])
                    by_section[s["key"]].append(it)
                    placed = True
                    break
            if not placed:
                left.append(it)
        rest = left
    unassigned = len(rest)
    by_section["_unassigned"] = rest
    return by_section, unassigned, hidden


def _rank(it: dict, now: float) -> float:
    from app.services.graph import temporal_salience
    conf = max(0.05, min(1.0, float(it.get("confidence") if it.get("confidence") is not None else 0.6)))
    recency = temporal_salience(_age_days(it.get("observed_at"), now))
    approval = 1.0 if it["review_state"] == "approved" else PENDING_APPROVAL
    r = conf * recency * approval
    if it["tags"].get("pinned"):
        r += PIN_BOOST
    return round(r, 6)


def _cap_sections(btype: str, by_section: dict[str, list[dict]], now: float) -> int:
    """Rank within each section and apply max_items (Status keeps the newest
    three plus the next step). Returns the number of items dropped by caps."""
    dropped = 0
    for s in sections(btype):
        rows = by_section.get(s["key"]) or []
        for it in rows:
            it["rank"] = _rank(it, now)
        if s["key"] == "status":
            tasks = [r for r in rows if r["item_type"] == "task"]
            rest = [r for r in rows if r["item_type"] != "task"]
            rest.sort(key=lambda r: (-(r.get("observed_at") or 0), -r["rank"], r["item_id"]))
            tasks.sort(key=lambda r: (str(r["tags"].get("due") or "9999"), -r["rank"], r["item_id"]))
            keep = rest[:3] + tasks[:1]
            dropped += len(rows) - len(keep)
            rows = keep
        else:
            rows.sort(key=lambda r: (-r["rank"], r["item_id"]))
            cap = int(s.get("max_items") or 20)
            dropped += max(0, len(rows) - cap)
            rows = rows[:cap]
        rows.sort(key=lambda r: (-r["rank"], r["item_id"]))
        by_section[s["key"]] = rows
    return dropped


# --------------------------------------------------------------------- gaps --
def _conflict_pairs(items: list[dict], idx: _Index) -> list[tuple[str, str, str]]:
    """(pair_key, text_a, text_b) for unresolved conflict_with pairs."""
    open_states = {"draft", "proposed", "conflicting", "edited"}
    by_id = {c["id"]: c for c in idx.claims}
    out: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for it in items:
        if it["item_type"] != "claim":
            continue
        other_id = it["tags"].get("conflict_with")
        if not other_id or (it["tags"].get("status") or "") not in open_states:
            continue
        other = by_id.get(other_id)
        if not other or (other.get("status") or "") not in open_states:
            continue
        key = "|".join(sorted([str(it["item_id"]), str(other_id)]))
        if key in seen:
            continue
        seen.add(key)
        out.append((key, it["text"], clean_text(_claim_text(other))))
    return out


def _reconcile_gaps(store: Store, idx: _Index, stype: str, sid: int, btype: str,
                    label: str, by_section: dict[str, list[dict]],
                    all_items: list[dict], now: float) -> list[dict]:
    """System gaps: one per empty required section, one per unresolved
    conflict pair. Resolve what no longer applies; keep user gaps as they
    are. Returns the open gap rows."""
    wanted: dict[tuple[str, str], str] = {}   # (section_key, question) -> resolve-with
    for s in sections(btype):
        if s.get("required") and s.get("gap") and not by_section.get(s["key"]):
            wanted[(s["key"], str(s["gap"]).replace("{label}", label))] = ""
    oq = "open_questions"
    for key, a, b in _conflict_pairs(all_items, idx):
        q = f"Which is right: \u201c{a}\u201d or \u201c{b}\u201d?"
        wanted[(oq, q)] = ""
    existing = idx.gaps.get((stype, sid), [])
    open_rows: list[dict] = []
    with store._lock:
        for g in existing:
            k = (g["section_key"], g["question"])
            if g.get("resolved_at"):
                continue
            if g["created_by"] == "system" and k not in wanted:
                # answered: a later item filled the section or settled the conflict
                answer = ""
                rows = by_section.get(g["section_key"]) or []
                if rows:
                    answer = f"{rows[0]['item_type']}:{rows[0]['item_id']}"
                store._conn.execute(
                    "UPDATE brain_gaps SET resolved_item = ?, resolved_at = ? WHERE id = ?",
                    (answer or None, now, int(g["id"])))
                continue
            wanted.pop(k, None)
            open_rows.append(g)
        for (sk, q) in wanted:
            cur = store._conn.execute(
                "INSERT INTO brain_gaps (subject_type, subject_id, section_key, "
                "question, created_by, created_at) VALUES (?,?,?,?,'system',?)",
                (stype, int(sid), sk, q, now))
            open_rows.append({"id": int(cur.lastrowid), "subject_type": stype,
                              "subject_id": sid, "section_key": sk, "question": q,
                              "created_by": "system", "created_at": now,
                              "resolved_item": None, "resolved_at": None})
        store._conn.commit()
    open_rows.sort(key=lambda g: int(g["id"]))
    return open_rows


def _gap_item(g: dict) -> dict:
    it = _item("gap", g["id"], g["question"], review="gap", confidence=None,
               event_id=None, source="You" if g.get("created_by") == "user" else "Sparrow",
               observed_at=g.get("created_at"), privacy="internal", evidence=None,
               tags={"created_by": g.get("created_by")})
    it["assigned_by"] = "rule"
    it["rank"] = 0.0
    return it


# ------------------------------------------------------------------ compile --
_ITEM_COLS = ("section_key", "text", "review_state", "confidence",
              "source_event_id", "source_label", "observed_at", "privacy_class",
              "assigned_by", "schema_version")


def compile_subject(store: Store, idx: _Index, stype: str, sid: int,
                    *, now: float | None = None) -> dict:
    """Recompile one subject. Writes brain_items + system gaps. Returns a
    summary: {brain: bool, items, unassigned, hidden, changed}."""
    now = float(now if now is not None else idx.now)
    btype = brain_type(idx, stype, sid)
    label = idx.label(stype, sid)
    if btype is None or (stype, sid) in idx.hidden:
        return {"brain": False, "deleted": _delete_subject(store, stype, sid),
                "items": 0, "unassigned": 0, "hidden": 0, "changed": False}
    items = _collect_items(idx, stype, sid, label)
    by_section, unassigned, hidden = _assign(idx, stype, sid, btype, items)
    by_section.pop("_unassigned", None)
    unassigned += _cap_sections(btype, by_section, now)
    n_items = sum(len(v) for k, v in by_section.items() if k != "open_questions") + \
        len([r for r in by_section.get("open_questions", []) if r["item_type"] != "gap"])
    if n_items < MIN_ITEMS:
        return {"brain": False, "deleted": _delete_subject(store, stype, sid),
                "items": n_items, "unassigned": unassigned, "hidden": hidden,
                "changed": False}
    assigned = [it for rows in by_section.values() for it in rows]
    gaps = _reconcile_gaps(store, idx, stype, sid, btype, label, by_section,
                           assigned, now)
    for g in gaps:
        by_section.setdefault("open_questions", []).append(_gap_item(g))
    sv = schema_version(btype)
    desired: dict[tuple[str, str], dict] = {}
    for sk, rows in by_section.items():
        for it in rows:
            it["section_key"] = sk
            it["schema_version"] = sv
            desired[(it["item_type"], it["item_id"])] = it
    changed = _persist(store, stype, sid, desired, now)
    _unassigned.setdefault(id(store), {})[(stype, sid)] = unassigned
    return {"brain": True, "items": n_items, "unassigned": unassigned,
            "hidden": hidden, "changed": changed, "type": btype}


def _persist(store: Store, stype: str, sid: int,
             desired: dict[tuple[str, str], dict], now: float) -> bool:
    changed = False
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM brain_items WHERE subject_type = ? AND subject_id = ?",
            (stype, int(sid))).fetchall()
        existing = {(r["item_type"], r["item_id"]): dict(r) for r in rows}
        for key, old in existing.items():
            if key not in desired:
                store._conn.execute("DELETE FROM brain_items WHERE id = ?", (old["id"],))
                changed = True
        for key, it in desired.items():
            vals = {
                "section_key": it["section_key"], "text": it["text"],
                "review_state": it["review_state"], "confidence": it.get("confidence"),
                "source_event_id": it.get("source_event_id"),
                "source_label": it.get("source_label"),
                "observed_at": it.get("observed_at"),
                "privacy_class": it.get("privacy_class"),
                "assigned_by": it.get("assigned_by") or "rule",
                "schema_version": it.get("schema_version"),
            }
            old = existing.get(key)
            if old is None:
                store._conn.execute(
                    "INSERT INTO brain_items (subject_type, subject_id, section_key, "
                    "item_type, item_id, text, review_state, confidence, "
                    "source_event_id, source_label, observed_at, privacy_class, "
                    "rank, assigned_by, schema_version, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (stype, int(sid), vals["section_key"], key[0], key[1],
                     vals["text"], vals["review_state"], vals["confidence"],
                     vals["source_event_id"], vals["source_label"],
                     vals["observed_at"], vals["privacy_class"],
                     float(it.get("rank") or 0.0), vals["assigned_by"],
                     vals["schema_version"], now))
                changed = True
                continue
            diff = any(_neq(old.get(c), vals[c]) for c in _ITEM_COLS)
            rank = float(it.get("rank") or 0.0)
            if diff:
                store._conn.execute(
                    "UPDATE brain_items SET " + ", ".join(f"{c} = ?" for c in _ITEM_COLS)
                    + ", rank = ?, updated_at = ? WHERE id = ?",
                    (*[vals[c] for c in _ITEM_COLS], rank, now, old["id"]))
                changed = True
            elif abs(float(old.get("rank") or 0.0) - rank) > 1e-6:
                # rank drifts with time; that is not a change the client must see
                store._conn.execute("UPDATE brain_items SET rank = ? WHERE id = ?",
                                    (rank, old["id"]))
        store._conn.commit()
    return changed


def _neq(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a or 0) - float(b or 0)) > 1e-6
        except (TypeError, ValueError):
            return True
    return (a if a is not None else None) != (b if b is not None else None)


def _delete_subject(store: Store, stype: str, sid: int) -> int:
    with store._lock:
        cur = store._conn.execute(
            "DELETE FROM brain_items WHERE subject_type = ? AND subject_id = ?",
            (stype, int(sid)))
        store._conn.execute(
            "UPDATE brain_gaps SET resolved_at = ? WHERE subject_type = ? AND "
            "subject_id = ? AND created_by = 'system' AND resolved_at IS NULL",
            (time.time(), stype, int(sid)))
        store._conn.commit()
    return int(cur.rowcount or 0)


# ------------------------------------------------------------------ rebuild --
_state_lock = threading.Lock()
_state: dict[int, dict] = {}          # id(store) -> {version, schema, at}
_building: dict[int, threading.Thread] = {}   # a full rebuild running off the request path
FULL_REBUILD_INLINE_FACTS = 3000       # above this a first/full rebuild goes to a thread
_unassigned: dict[int, dict[tuple[str, int], int]] = {}   # per brain, last compile


def rebuild(store: Store | None = None, *, scope: str = "full",
            subjects_only: list[tuple[str, int]] | None = None,
            now: float | None = None) -> dict:
    """Compile brains. scope='full' recompiles every subject; 'dirty'
    recompiles graph_dirty nodes, the subjects of rows that changed since the
    last compile, and anything in `subjects_only`."""
    store = store or get_store()
    now = float(now if now is not None else time.time())
    idx = _Index(store, now)
    subs = subjects(store, idx)
    want: list[tuple[str, int]]
    if scope == "full" and not subjects_only:
        idx.text_links()
        want = [(s["subject_type"], s["subject_id"]) for s in subs]
        # subjects that lost their rows (hidden, retyped) still need cleaning
        for r in _rows(store, "SELECT DISTINCT subject_type, subject_id FROM brain_items"):
            k = (r["subject_type"], int(r["subject_id"]))
            if k not in want:
                want.append(k)
    else:
        st = _state.get(id(store)) or {}
        # the last compile read everything as of its own `now`; anything
        # stamped at or after that moment is what it could have missed
        since = float(st.get("at") or 0.0)
        keys = set(subjects_only or [])
        keys |= _dirty_subjects(store, idx, since)
        want = sorted(keys)
    counts = {"brains": 0, "subjects": len(want), "items": 0, "unassigned": 0,
              "changed": 0, "removed": 0}
    known = {(s["subject_type"], s["subject_id"]) for s in subs}
    for key in want:
        if key not in known:
            counts["removed"] += 1 if _delete_subject(store, *key) else 0
            continue
        res = compile_subject(store, idx, key[0], key[1], now=now)
        if res["brain"]:
            counts["brains"] += 1
            counts["items"] += res["items"]
        counts["unassigned"] += res["unassigned"]
        counts["changed"] += 1 if res.get("changed") else 0
    with _state_lock:
        version = store.memory_version()
        _state[id(store)] = {"version": version, "schema": schema_stamp(), "at": now}
        # ?since=<this token> means "after this compile": rows this pass
        # wrote carry updated_at == now, so a strict compare excludes them.
        _since_seen.setdefault(version, now)
    _list_cache.pop(id(store), None)
    _meta_cache.pop(id(store), None)
    return counts


def _dirty_subjects(store: Store, idx: _Index, since: float) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    # graph_dirty is the extraction pipeline's queue and graph.rebuild clears
    # it on its own schedule; only marks stamped since our last compile are
    # news to us (the timestamps below cover the rest either way).
    try:
        dirty = {(str(r["node_type"]), int(r["node_id"])) for r in _rows(
            store, "SELECT node_type, node_id FROM graph_dirty WHERE ts >= ?", (since,))}
    except Exception:
        dirty = set()
    fact_ids: set[int] = set()
    for nt, nid in dirty:
        if nt in ("person", "entity"):
            out.add((nt, int(nid)))
        elif nt == "fact":
            fact_ids.add(int(nid))
    for fid, f in idx.facts.items():
        if float(f.get("updated_at") or f.get("extracted_at") or 0) >= since:
            fact_ids.add(fid)
    for fid in fact_ids:
        f = idx.facts.get(fid)
        if f:
            out |= idx._name_index.subjects_in(f.get("text") or "")
        out |= {("entity", e) for e in idx.fact_entities.get(fid, set())}
        out |= {("person", p) for p in idx.fact_persons.get(fid, set())}
    recent_claims = [c for c in idx.claims
                     if float(c.get("updated_at") or c.get("created_at") or 0) >= since]
    if recent_claims:
        by_ref: dict[str, set[tuple[str, int]]] = {}
        for s in subjects(store, idx):
            key = (s["subject_type"], s["subject_id"])
            for ref in idx.subject_refs(*key):
                by_ref.setdefault(ref, set()).add(key)
        for c in recent_claims:
            out |= by_ref.get(str(c.get("subject_ref") or ""), set())
            v = c.get("value") or {}
            for who in (v.get("owner"), v.get("counterparty")):
                if who:
                    out |= by_ref.get(f"person:{_norm(who)}", set())
    for r in idx.relations:
        if float(r.get("created_at") or 0) >= since:
            for t, i in ((r["subj_type"], r["subj_id"]), (r["obj_type"], r["obj_id"])):
                if t in ("person", "entity"):
                    out.add((t, int(i)))
    for r in idx.kg:
        if float(r.get("updated_at") or 0) >= since:
            for t, i in ((r["subj_type"], r["subj_id"]), (r["obj_type"], r["obj_id"])):
                if t in ("person", "entity"):
                    out.add((t, int(i)))
    for eid, e in idx.entities.items():
        if float(e.get("last_seen") or 0) >= since:
            out.add(("entity", eid))
    for pid, p in idx.people.items():
        if float(p.get("last_seen") or 0) >= since:
            out.add(("person", pid))
    # overrides written since the last compile
    for key, rows in idx.overrides.items():
        if any(float(o.get("created_at") or 0) >= since for o in rows):
            out.add(key)
    for key, rows in idx.gaps.items():
        # system gaps carry the compile's own stamp; only the user's count
        if any(g.get("created_by") == "user" and float(g.get("created_at") or 0) >= since
               for g in rows):
            out.add(key)
    return out


def ensure_compiled(store: Store | None = None) -> dict:
    """Bring brain_items up to date before a read: a full rebuild the first
    time or after a schema change, an incremental one when memory moved."""
    store = store or get_store()
    st = _state.get(id(store))
    version = store.memory_version()
    with _state_lock:
        if st and st.get("version") == version and st.get("schema") == schema_stamp():
            return {"ok": True, "rebuilt": "none"}
    stamps = {r["v"] for r in _rows(store, "SELECT DISTINCT schema_version AS v FROM brain_items")}
    current = {schema_version(t) for t in TYPES}
    has_rows = bool(stamps)
    if not st or not has_rows or not stamps <= current:
        t = _building.get(id(store))
        if t is not None and t.is_alive():
            return {"ok": True, "rebuilt": "building"}
        n_facts = 0
        try:
            n_facts = int(_rows(store, "SELECT COUNT(*) AS n FROM facts")[0]["n"])
        except Exception:
            pass
        if n_facts > FULL_REBUILD_INLINE_FACTS:
            # The spec keeps a full rebuild off the request path: serve what
            # is there (possibly nothing) and let the map poll until the
            # version token moves.
            def _run():
                try:
                    rebuild(store, scope="full")
                except Exception as exc:
                    print(f"[brains] background rebuild failed ({exc}).")
            t = threading.Thread(target=_run, name="brains-rebuild", daemon=True)
            _building[id(store)] = t
            t.start()
            return {"ok": True, "rebuilt": "building"}
        res = rebuild(store, scope="full")
        return {"ok": True, "rebuilt": "full", **res}
    res = rebuild(store, scope="dirty")
    return {"ok": True, "rebuilt": "dirty", **res}


def building(store: Store) -> bool:
    t = _building.get(id(store))
    return bool(t is not None and t.is_alive())


# ------------------------------------------------------------------- layout --
def _fib_dir(i: int, n: int) -> tuple[float, float, float]:
    n = max(1, n)
    y = 1.0 - 2.0 * (i + 0.5) / n
    r = math.sqrt(max(0.0, 1.0 - y * y))
    phi = i * 2.399963229728653          # golden angle
    return (round(r * math.cos(phi), 5), round(y, 5), round(r * math.sin(phi), 5))


def section_dirs(btype: str) -> dict[str, tuple[float, float, float]]:
    secs = sections(btype)
    return {s["key"]: _fib_dir(s["index"], len(secs)) for s in secs}


def _basis(d: tuple[float, float, float]):
    ax = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = _cross(d, ax)
    ul = math.sqrt(sum(c * c for c in u)) or 1.0
    u = tuple(c / ul for c in u)
    v = _cross(d, u)
    return u, v


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def leaf_pos(btype: str, section_key: str, item_type: str, item_id: str
             ) -> tuple[float, float, float]:
    """Inside a cone around the section direction; stable per item id."""
    d = section_dirs(btype).get(section_key) or (0.0, 1.0, 0.0)
    u, v = _basis(d)
    sid = f"{item_type}:{item_id}"
    ang = 2.0 * math.pi * _h01(sid, "leaf-angle")
    cone = LEAF_CONE * math.sqrt(_h01(sid, "leaf-cone"))
    dist = LEAF_R_MIN + (LEAF_R_MAX - LEAF_R_MIN) * _h01(sid, "leaf-dist")
    ca, sa = math.cos(ang), math.sin(ang)
    cc, sc = math.cos(cone), math.sin(cone)
    return tuple(round(dist * (cc * d[k] + sc * (ca * u[k] + sa * v[k])), 4)
                 for k in range(3))


def _relax_numpy(brains: list[dict], order: list[int]) -> bool:
    """The same overlap relaxation as the pure-Python loop below, vectorised:
    close pairs come from a KD-tree each step, pushes are summed and applied
    at once, so the result is a pure function of the input (id order fixes
    the array layout). False when numpy/scipy are missing."""
    try:
        import numpy as np
        from scipy.spatial import cKDTree
    except Exception:
        return False
    n = len(brains)
    if n < 2:
        return True
    P = np.array([brains[i]["_p"] for i in order], dtype=np.float64)
    R = np.array([brains[i]["_r"] for i in order], dtype=np.float64)
    reach = float(1.2 * 2.0 * R.max())
    for _ in range(RELAX_STEPS):
        pairs = cKDTree(P).query_pairs(reach, output_type="ndarray")
        if len(pairs) == 0:
            break
        i, j = pairs[:, 0], pairs[:, 1]
        D = P[j] - P[i]
        dist = np.sqrt((D * D).sum(axis=1))
        min_sep = 1.2 * (R[i] + R[j])
        close = dist < min_sep
        if not close.any():
            break
        i, j, D, dist, min_sep = i[close], j[close], D[close], dist[close], min_sep[close]
        zero = dist < 1e-6
        if zero.any():
            D[zero, 0] += 1e-3 * (1 + i[zero] % 3)
            D[zero, 1] += 1e-3 * (1 + j[zero] % 5)
            D[zero, 2] += 1e-3
            dist = np.sqrt((D * D).sum(axis=1))
        step = D / dist[:, None] * ((min_sep - dist) / 2.0)[:, None]
        np.add.at(P, i, -step)
        np.add.at(P, j, step)
    for k, i in enumerate(order):
        brains[i]["_p"] = [float(P[k, 0]), float(P[k, 1]), float(P[k, 2])]
    return True


def overview_positions(brains: list[dict]) -> None:
    """Sets brain['pos'] in place: hashed direction, distance from activity,
    then RELAX_STEPS deterministic overlap pushes in id order."""
    for b in brains:
        cid = b["_canonical"]
        theta = 2.0 * math.pi * _h01(cid, "brain-theta")
        z = 2.0 * _h01(cid, "brain-z") - 1.0
        r = math.sqrt(max(0.0, 1.0 - z * z))
        d = (r * math.cos(theta), z, r * math.sin(theta))
        dist = OVERVIEW_R_MAX - (OVERVIEW_R_MAX - OVERVIEW_R_MIN) * float(b["activity"])
        b["_p"] = [d[0] * dist, d[1] * dist, d[2] * dist]
        b["_r"] = 0.5 + 0.5 * float(b["activity"])
    order = sorted(range(len(brains)), key=lambda i: brains[i]["id"])
    if _relax_numpy(brains, order):
        for b in brains:
            b["pos"] = [round(c, 4) for c in b.pop("_p")]
            b["size"] = round(b.pop("_r"), 4)
            b.pop("_canonical", None)
        return
    cell = 2.0 * max([b["_r"] for b in brains] or [1.0]) * 1.2
    for _ in range(RELAX_STEPS):
        grid: dict[tuple[int, int, int], list[int]] = {}
        for i in order:
            p = brains[i]["_p"]
            grid.setdefault(tuple(int(math.floor(c / cell)) for c in p), []).append(i)
        moved = False
        for i in order:
            pi = brains[i]["_p"]
            gi = tuple(int(math.floor(c / cell)) for c in pi)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for j in grid.get((gi[0] + dx, gi[1] + dy, gi[2] + dz), []):
                            if j <= i:
                                continue
                            pj = brains[j]["_p"]
                            min_sep = 1.2 * (brains[i]["_r"] + brains[j]["_r"])
                            dv = [pj[k] - pi[k] for k in range(3)]
                            dist = math.sqrt(sum(c * c for c in dv))
                            if dist >= min_sep:
                                continue
                            if dist < 1e-6:
                                dv = [1e-3 * (1 + (i % 3)), 1e-3 * (1 + (j % 5)), 1e-3]
                                dist = math.sqrt(sum(c * c for c in dv))
                            push = (min_sep - dist) / 2.0
                            for k in range(3):
                                step = dv[k] / dist * push
                                pi[k] -= step
                                pj[k] += step
                            moved = True
        if not moved:
            break
    for b in brains:
        b["pos"] = [round(c, 4) for c in b.pop("_p")]
        b["size"] = round(b.pop("_r"), 4)
        b.pop("_canonical", None)


# -------------------------------------------------------------------- reads --
_list_cache: dict[int, tuple[str, list[dict]]] = {}
_since_seen: dict[str, float] = {}


def _activity(items: list[dict], now: float) -> float:
    from app.services.graph import _sigmoid
    n = sum(1 for it in items
            if it.get("item_type") != "gap" and it.get("observed_at")
            and now - float(it["observed_at"]) <= ACTIVITY_WINDOW_S)
    return round(max(ACTIVITY_FLOOR, 2.0 * _sigmoid(n / 4.0) - 1.0), 4)


def _brain_rows(store: Store, stype: str | None = None, sid: int | None = None) -> dict[tuple[str, int], list[dict]]:
    sql = "SELECT * FROM brain_items"
    args: tuple = ()
    if stype is not None:
        sql += " WHERE subject_type = ? AND subject_id = ?"
        args = (stype, int(sid))
    sql += " ORDER BY subject_type, subject_id, section_key, rank DESC, item_id"
    out: dict[tuple[str, int], list[dict]] = {}
    for r in _rows(store, sql, args):
        out.setdefault((r["subject_type"], int(r["subject_id"])), []).append(r)
    return out


def _summary(idx_like: dict, stype: str, sid: int, rows: list[dict], now: float) -> dict:
    btype = idx_like["types"].get((stype, sid))
    secs = sections(btype)
    per: dict[str, dict] = {s["key"]: {"key": s["key"], "count": 0, "approved": 0} for s in secs}
    sources: set = set()
    updated = 0.0
    for r in rows:
        p = per.get(r["section_key"])
        if p is None:
            continue
        if r["item_type"] != "gap":
            p["count"] += 1
            if r["review_state"] == "approved":
                p["approved"] += 1
        if r.get("source_event_id"):
            sources.add(("e", r["source_event_id"]))
        elif r.get("source_label"):
            sources.add(("l", r["source_label"]))
        updated = max(updated, float(r.get("updated_at") or 0))
    return {
        "id": brain_id_of(stype, sid), "type": btype,
        "label": idx_like["labels"].get((stype, sid), f"{stype} {sid}"),
        "activity": _activity(rows, now),
        "sections": [per[s["key"]] for s in secs],
        "items_total": sum(p["count"] for p in per.values()),
        "sources_total": len(sources),
        "updated_at": updated,
        "_canonical": idx_like["canonical"].get((stype, sid), brain_id_of(stype, sid)),
    }


_meta_cache: dict[int, tuple[str, dict]] = {}


def _labels_and_types(store: Store) -> dict:
    version = store.memory_version()
    hit = _meta_cache.get(id(store))
    if hit and hit[0] == version:
        return hit[1]
    now = time.time()
    labels: dict[tuple[str, int], str] = {}
    types: dict[tuple[str, int], str] = {}
    idx = _Index(store, now, light=True)
    for s in subjects(store, idx):
        key = (s["subject_type"], s["subject_id"])
        labels[key] = s["label"]
        if s["type"]:
            types[key] = s["type"]
    meta = {"labels": labels, "types": types, "canonical": idx.canonical}
    _meta_cache[id(store)] = (version, meta)
    return meta


def list_brains(store: Store | None = None, *, btype: str | None = None,
                limit: int = 200, since: str | None = None,
                now: float | None = None) -> dict:
    store = store or get_store()
    now = float(now if now is not None else time.time())
    state = ensure_compiled(store)
    version = store.memory_version()
    cached = _list_cache.get(id(store))
    if cached and cached[0] == version:
        brains = cached[1]
    else:
        meta = _labels_and_types(store)
        brains = []
        for (stype, sid), rows in _brain_rows(store).items():
            if (stype, sid) not in meta["types"]:
                continue
            brains.append(_summary(meta, stype, sid, rows, now))
        overview_positions(brains)
        brains.sort(key=lambda b: (-b["activity"], b["label"].lower(), b["id"]))
        _list_cache[id(store)] = (version, brains)
    _since_seen.setdefault(version, now)
    out = brains
    if btype:
        out = [b for b in out if b["type"] == btype]
    if since:
        cut = _since_seen.get(since)
        if cut is not None:
            out = [b for b in out if float(b["updated_at"] or 0) > cut]
    limit = max(1, min(int(limit or 200), 500))
    return {"brains": out[:limit], "version": version, "total": len(out),
            "unassigned": int(sum(_unassigned.get(id(store), {}).values())),
            "building": state.get("rebuilt") == "building" or building(store)}


def get_brain(store: Store | None, brain_id: str, *, now: float | None = None) -> dict:
    store = store or get_store()
    now = float(now if now is not None else time.time())
    stype, sid = parse_brain_id(brain_id)
    ensure_compiled(store)
    rows = _brain_rows(store, stype, sid).get((stype, sid)) or []
    meta = _labels_and_types(store)
    if not rows or (stype, sid) not in meta["types"]:
        raise BrainNotFound(f"no brain {brain_id}")
    summ = _summary(meta, stype, sid, rows, now)
    summ.pop("_canonical", None)
    btype = summ["type"]
    dirs = section_dirs(btype)
    by_sec: dict[str, list[dict]] = {}
    for r in rows:
        by_sec.setdefault(r["section_key"], []).append(r)
    ranks = [float(r["rank"] or 0) for r in rows if r["item_type"] != "gap"]
    top = max(ranks) if ranks else 1.0
    secs_out = []
    for s in sections(btype):
        items = []
        for r in by_sec.get(s["key"], []):
            items.append({
                "ref": f"{r['item_type']}:{r['item_id']}",
                "item_type": r["item_type"], "item_id": r["item_id"],
                "text": r["text"], "review_state": r["review_state"],
                "confidence": r.get("confidence"),
                "source_label": r.get("source_label"),
                "source_event_id": r.get("source_event_id"),
                "observed_at": r.get("observed_at"),
                "evidence_id": _evidence_id(r),
                "assigned_by": r.get("assigned_by"),
                "privacy_class": r.get("privacy_class"),
                "rank": float(r.get("rank") or 0.0),
                "pos": list(leaf_pos(btype, s["key"], r["item_type"], r["item_id"])),
                "size": round(0.35 + 0.65 * (float(r["rank"] or 0) / top if top > 0 else 0.5), 3)
                if r["item_type"] != "gap" else 0.4,
            })
        secs_out.append({"key": s["key"], "label": s["label"],
                         "required": bool(s.get("required")),
                         "dir": list(dirs[s["key"]]), "items": items,
                         "count": len([i for i in items if i["item_type"] != "gap"]),
                         "approved": len([i for i in items if i["review_state"] == "approved"])})
    summ["sections"] = secs_out
    summ["unassigned"] = int(_unassigned.get(id(store), {}).get((stype, sid), 0))
    summ["schema_version"] = schema_version(btype)
    summ["phrase"] = schema_for(btype).get("phrase")
    return summ


def _evidence_id(r: dict) -> str | None:
    it, iid = r["item_type"], str(r["item_id"])
    if it in ("fact", "task", "commitment") and iid.isdigit():
        return f"fact:{iid}"
    if it == "relation":
        return None
    return None


# ---------------------------------------------------------------- overrides --
def _touch(store: Store, stype: str, sid: int) -> None:
    rebuild(store, scope="dirty", subjects_only=[(stype, sid)])


def move_item(store: Store, brain_id: str, item_type: str, item_id: str,
              section_key: str) -> dict:
    stype, sid = parse_brain_id(brain_id)
    btype = _type_of(store, stype, sid)
    if section(btype, section_key) is None:
        raise SectionNotFound(f"no section {section_key!r} on a {btype}")
    if item_type == "gap":
        raise BadPacketRequest("gaps live in Open questions")
    now = time.time()
    with store._lock:
        store._conn.execute(
            "INSERT INTO brain_overrides (subject_type, subject_id, item_type, item_id, "
            "action, section_key, created_at) VALUES (?,?,?,?,'move',?,?)",
            (stype, int(sid), str(item_type), str(item_id), section_key, now))
        store._conn.commit()
    _log_adjudication(store, "brain_move", stype, sid, item_type, item_id, section_key)
    _touch(store, stype, sid)
    b = get_brain(store, brain_id)
    for s in b["sections"]:
        for it in s["items"]:
            if it["item_type"] == item_type and it["item_id"] == str(item_id):
                return {"ok": True, "item": {**it, "section": s["key"]}, "brain": b["id"]}
    return {"ok": True, "item": None, "brain": b["id"]}


def hide_item(store: Store, brain_id: str, item_type: str, item_id: str) -> dict:
    stype, sid = parse_brain_id(brain_id)
    _type_of(store, stype, sid)
    now = time.time()
    with store._lock:
        store._conn.execute(
            "INSERT INTO brain_overrides (subject_type, subject_id, item_type, item_id, "
            "action, created_at) VALUES (?,?,?,?,'hide',?)",
            (stype, int(sid), str(item_type), str(item_id), now))
        store._conn.commit()
    _log_adjudication(store, "brain_hide", stype, sid, item_type, item_id, None)
    _touch(store, stype, sid)
    return {"ok": True}


def set_type(store: Store, brain_id: str, brain_type_: str) -> dict:
    stype, sid = parse_brain_id(brain_id)
    if brain_type_ not in TYPES:
        raise BadPacketRequest(f"brain_type must be one of {list(TYPES)}")
    if (stype == "person") != (brain_type_ == "person"):
        raise BadPacketRequest("a person stays a person, and an entity cannot become one")
    now = time.time()
    with store._lock:
        store._conn.execute(
            "INSERT INTO brain_overrides (subject_type, subject_id, action, brain_type, "
            "created_at) VALUES (?,?,'set_type',?,?)",
            (stype, int(sid), brain_type_, now))
        store._conn.commit()
    _touch(store, stype, sid)
    res = list_brains(store)
    for b in res["brains"]:
        if b["id"] == brain_id:
            return {"ok": True, "brain": b}
    return {"ok": True, "brain": None, "note": "fewer than 3 items after retyping"}


def add_gap(store: Store, brain_id: str, section_key: str, question: str) -> dict:
    stype, sid = parse_brain_id(brain_id)
    btype = _type_of(store, stype, sid)
    if section(btype, section_key) is None:
        raise SectionNotFound(f"no section {section_key!r} on a {btype}")
    q = clean_text(question)
    if not q:
        raise BadPacketRequest("question required")
    now = time.time()
    with store._lock:
        cur = store._conn.execute(
            "INSERT INTO brain_gaps (subject_type, subject_id, section_key, question, "
            "created_by, created_at) VALUES (?,?,?,?,'user',?)",
            (stype, int(sid), section_key, q, now))
        gid = int(cur.lastrowid)
        store._conn.commit()
    _touch(store, stype, sid)
    return {"ok": True, "gap": {"id": gid, "section": section_key, "question": q,
                                "created_by": "user", "created_at": now}}


def _type_of(store: Store, stype: str, sid: int) -> str:
    idx = _Index(store, time.time(), light=True)
    bt = brain_type(idx, stype, sid)
    if bt is None or sid not in (idx.entities if stype == "entity" else idx.people):
        raise BrainNotFound(f"no brain {brain_id_of(stype, sid)}")
    return bt


def _log_adjudication(store: Store, kind: str, stype: str, sid: int,
                      item_type: str, item_id: str, section_key: str | None) -> None:
    """A user move or hide is a labelled example for the section classifier
    (step 5). Same table as the KG flywheel; frozen features, local only."""
    try:
        with store._lock:
            store._conn.execute(
                "INSERT INTO kg_adjudications (kind, features_json, decision, "
                "decided_by, created_at) VALUES (?,?,?,?,?)",
                (kind, json.dumps({"subject": brain_id_of(stype, sid),
                                   "item": f"{item_type}:{item_id}",
                                   "section": section_key}),
                 section_key or "hide", "user", time.time()))
            store._conn.commit()
    except Exception:
        pass


# ------------------------------------------------------------------- packet --
def _tokens(text: str) -> int:
    return int(math.ceil(len(text) / 4.0))


def _render(label: str, phrase: str, secs: list[dict], omitted: dict[str, int]) -> str:
    lines = [
        f"Context for {label}, {phrase}. Use only the facts below.",
        "Each ends with its source and date. Items marked UNKNOWN are open.",
        "Do not guess at them.",
    ]
    for s in secs:
        if not s["lines"] and not omitted.get(s["key"]):
            continue
        lines.append("")
        lines.append(f"## {s['label']}")
        lines.extend(s["lines"])
        n = omitted.get(s["key"], 0)
        if n:
            lines.append(f"({n} more omitted)")
    return "\n".join(lines) + "\n"


def _item_line(r: dict) -> str:
    if r["item_type"] == "gap":
        return f"- UNKNOWN: {r['text']}"
    src = r.get("source_label") or "Memory"
    when = _date(r.get("observed_at"))
    tail = f"[{src} · {when}]" if when else f"[{src}]"
    pre = "(unreviewed) " if r["review_state"] == "pending" else ""
    return f"- {pre}{r['text']} {tail}"


def compile_packet(store: Store | None, brain_id: str, *, scope: str = "brain",
                   section_key: str | None = None, include_pending: bool = False,
                   budget_tokens: int = PACKET_DEFAULT_TOKENS,
                   destination: str = "clipboard", now: float | None = None) -> dict:
    """The one packet compiler: the Copy button and the MCP tool both call
    this and get identical text for identical arguments."""
    from app.services import egress
    store = store or get_store()
    now = float(now if now is not None else time.time())
    if scope not in ("brain", "section"):
        raise BadPacketRequest("scope must be brain or section")
    if destination not in ("clipboard", "mcp"):
        raise BadPacketRequest("destination must be clipboard or mcp")
    try:
        budget = int(budget_tokens)
    except (TypeError, ValueError):
        raise BadPacketRequest("budget_tokens must be an integer")
    if budget < PACKET_MIN_TOKENS or budget > PACKET_MAX_TOKENS:
        raise BadPacketRequest(f"budget_tokens must be {PACKET_MIN_TOKENS}..{PACKET_MAX_TOKENS}")
    brain = get_brain(store, brain_id, now=now)
    btype = brain["type"]
    if scope == "section":
        if not section_key or section(btype, section_key) is None:
            raise SectionNotFound(f"no section {section_key!r} on a {btype}")
    label = brain["label"]
    phrase = str(schema_for(btype).get("phrase") or f"a {btype}")
    dest = f"{destination}:{brain_id}"
    click = destination == "clipboard"
    r = egress.rung()

    # Does the rung close the whole path for this destination?
    pre = egress.decide(path="brain_packet", destination=dest, policy="allow",
                        data_class="work", text="", approved=click, log=False)
    if not pre.allowed:
        egress.record_send(pre, path="brain_packet", destination=dest, ok=False,
                           data_class="work", approved=click,
                           approving_action="user_click" if click else None,
                           meta={"brain": brain_id, "scope": scope,
                                 "section": section_key, "included": 0,
                                 "withheld": {"pending": 0, "privacy": 0, "budget": 0,
                                              "personal": 0}})
        return {"text": "", "tokens": 0, "included": 0,
                "withheld": {"pending": 0, "privacy": 0, "budget": 0, "personal": 0},
                "verdict": "deny", "reason": pre.reason, "rung": r,
                "egress_id": _egress_id(store)}

    personal_ids, recorded_ids = _personal_claim_ids(store)
    withheld = {"pending": 0, "privacy": 0, "budget": 0, "personal": 0}
    secs: list[dict] = []
    refs: list[str] = []
    worst = pre
    for s in brain["sections"]:
        if scope == "section" and s["key"] != section_key:
            continue
        kept: list[dict] = []
        for it in s["items"]:
            if it["item_type"] == "gap":
                kept.append(it)
                continue
            if it["item_type"] == "claim" and it["item_id"] in personal_ids:
                withheld["personal"] += 1
                continue
            recorded = it["item_type"] == "claim" and it["item_id"] in recorded_ids
            if r == "perimeter":
                if not recorded:
                    withheld["pending" if it["review_state"] == "pending" else "privacy"] += 1
                    continue
            elif it["review_state"] == "pending" and not include_pending:
                withheld["pending"] += 1
                continue
            approved = click and (r != "perimeter" or recorded)
            v = egress.decide(path="brain_packet", destination=dest, policy="allow",
                              data_class="work", text=it["text"],
                              privacy_class=it.get("privacy_class"),
                              approved=approved, log=False)
            if not v.allowed:
                withheld["privacy"] += 1
                continue
            if egress._ORDER.get(v.action, 0) > egress._ORDER.get(worst.action, 0):
                worst = v
            kept.append(it)
        secs.append({"key": s["key"], "label": s["label"], "items": kept})

    # Items in rank order are already ordered by get_brain; gaps go last.
    for s in secs:
        s["items"].sort(key=lambda i: (i["item_type"] == "gap", -_rank_of(i), i["ref"]))
        s["lines"] = [_item_line(_row_view(i)) for i in s["items"]]
    omitted: dict[str, int] = {}
    text = _render(label, phrase, secs, omitted)
    while _tokens(text) > budget:
        # drop the lowest-ranked droppable item from the fullest section
        cands = [s for s in secs if any(i["item_type"] != "gap" for i in s["items"])]
        if not cands:
            break
        s = max(cands, key=lambda s: (sum(1 for i in s["items"] if i["item_type"] != "gap"),
                                       -_min_rank(s)))
        drop_i = max((i for i, it in enumerate(s["items"]) if it["item_type"] != "gap"),
                     key=lambda i: (-_rank_of(s["items"][i]), s["items"][i]["ref"]))
        s["items"].pop(drop_i)
        s["lines"].pop(drop_i)
        omitted[s["key"]] = omitted.get(s["key"], 0) + 1
        withheld["budget"] += 1
        text = _render(label, phrase, secs, omitted)
    included = sum(1 for s in secs for i in s["items"])
    refs = [i["ref"] for s in secs for i in s["items"]]
    egress.record_send(worst, path="brain_packet", destination=dest, ok=True,
                       data_class="work", approved=click,
                       approving_action="user_click" if click else None,
                       meta={"brain": brain_id, "scope": scope, "section": section_key,
                             "included": included, "withheld": dict(withheld),
                             "tokens": _tokens(text), "items": refs[:200]})
    return {"text": text, "tokens": _tokens(text), "included": included,
            "withheld": withheld, "verdict": "allow", "rung": r,
            "egress_id": _egress_id(store)}


def _row_view(it: dict) -> dict:
    return {"item_type": it["item_type"], "text": it["text"],
            "review_state": it["review_state"],
            "source_label": it.get("source_label"),
            "observed_at": it.get("observed_at")}


def _rank_of(it: dict) -> float:
    return float(it.get("rank") or 0.0)


def _min_rank(s: dict) -> float:
    rs = [_rank_of(i) for i in s["items"] if i["item_type"] != "gap"]
    return min(rs) if rs else 0.0


def _personal_claim_ids(store: Store) -> tuple[set[str], set[str]]:
    """(claim ids that never leave: personal_only=1, claim ids a record backs)."""
    personal: set[str] = set()
    recorded: set[str] = set()
    try:
        for r in _rows(store, "SELECT id, personal_only, record_ref, status FROM claims"):
            if r.get("personal_only"):
                personal.add(str(r["id"]))
            if r.get("record_ref") or (r.get("status") or "") == "recorded":
                recorded.add(str(r["id"]))
    except Exception:
        pass
    return personal, recorded


_egress_seq = 0


def _egress_id(store: Store) -> int:
    """A monotonic id for the packet's egress row (the trail itself is a
    JSONL file without row ids; this ties the response to the log line)."""
    global _egress_seq
    _egress_seq += 1
    return int(time.time()) * 1000 + (_egress_seq % 1000)


def unassigned_facts(store: Store, idx: "_Index | None" = None) -> dict[tuple[str, int], list[dict]]:
    """Per brain subject, the fact items no rule placed (what the backlog
    classifier should look at). Read-only: nothing is written."""
    idx = idx or _Index(store, time.time())
    out: dict[tuple[str, int], list[dict]] = {}
    for sub in subjects(store, idx):
        key = (sub["subject_type"], sub["subject_id"])
        if not sub["type"]:
            continue
        items = _collect_items(idx, key[0], key[1], sub["label"])
        by_section, _, _ = _assign(idx, key[0], key[1], sub["type"], items)
        rest = [it for it in by_section.get("_unassigned", [])
                if it["item_type"] == "fact" and str(it["item_id"]).isdigit()
                and not it["tags"].get("attr")]
        if rest:
            out[key] = rest
    return out


# ------------------------------------------------------------------ resolve --
def resolve_subject(store: Store, name: str) -> str | None:
    """A brain id for a name or id: projects.resolve, then exact entity /
    person, then a brain label match."""
    s = (name or "").strip()
    if not s:
        return None
    if re.match(r"^(entity|person):\d+$", s):
        return s
    try:
        from app.services import projects
        hit = projects.resolve(s, store=store)
        if hit and hit.get("name"):
            eid = store.find_entity_exact(str(hit["name"]))
            if eid:
                return brain_id_of("entity", int(eid))
    except Exception:
        pass
    try:
        eid = store.find_entity_exact(s)
        if eid:
            return brain_id_of("entity", int(eid))
    except Exception:
        pass
    try:
        pid = store.find_person_exact(s)
        if pid:
            return brain_id_of("person", int(pid))
    except Exception:
        pass
    try:
        from app.services import graph
        p = graph._resolve_person(store, s)
        if p:
            return brain_id_of("person", int(p["id"]))
    except Exception:
        pass
    low = s.lower()
    for b in list_brains(store, limit=500)["brains"]:
        if b["label"].lower() == low:
            return b["id"]
    return None
