"""Entity model, Phase 1: candidates before nodes.

A name the extractor reads for the first time is not an entity yet. It is a
sighting. Every sighting of a non-person name goes into the ledger
(`entity_mentions`), from every source class; a name with no entity row is
`pending` there, and this module decides when pending sightings add up to a
node. That is what keeps "pet groomers", "k9" and a one-off phrase from a
forwarded PDF off the Orgs & tools tab while losing nothing: when the name
does prove real, its earlier sightings and the relations asserted about it
are adopted.

Promotion (`QUILL_ENTITY_CANDIDATES`, default on):

- Recurrence: mint-eligible sightings in at least
  `QUILL_ENTITY_CANDIDATE_SESSIONS` (default 2) distinct sessions, or in at
  least two distinct mint-eligible source classes. Sightings from sources
  that may only bind (assistant notes, news, screen OCR) are recorded for
  typing and scope, and count toward nothing: not as a session, not as a
  source class. A relation such a source asserts about a candidate is
  dropped, as it was before the ledger.
- Sessions (`session_labels`): a sighting a `sessions` row covers takes that
  row's span, any other is a point, and spans and points no more than
  `QUILL_ENTITY_CANDIDATE_SESSION_GAP_S` (default 3600 s) apart are one
  session. Distinct events in one session are one session.
- Strong signal: the user typed the name in their own chat. One sighting is
  enough, unless the name is code-like or weak.
- Code-like names ("k9", "4e7a1f0c", "Phase 1", a date) always need
  recurrence, even typed.
- Weak names (`entity_typing.weak_name`: a lowercase phrase such as
  "pet groomers" or "bowling alleys", or one lowercase word no sighting called a
  tool or an org) are never promoted on sightings the user did not type;
  typed in two distinct sessions, they are.

With the flag off a new name is minted at first sight, as before. The ledger
is written either way.

What kind a promoted name gets is not this module's call: the kind vote over
its sightings decides (`entity_typing.vote`), and it runs again whenever a
new sighting binds to an entity whose kind is still a model guess
(`entity_typing.retype`). A value ("$49", "Friday") is never promoted.

Filing a note under a project, an email domain, and a manual add are not
extraction: they call `store.resolve_entity` directly and mint at once. A
manual add also adopts whatever was pending under the name.

Pending sightings older than `QUILL_ENTITY_CANDIDATE_TTL_DAYS` (default 30)
are deleted with their pooled relations: the name, span and event of a name
seen once are forgotten, not kept. What they added up to stays, with no text
in it (a summary keyed by the name key's hash: sessions, source classes,
first and last seen, kinds; see `_fold`), for
`QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS` (default 365), so a name that
recurs every six weeks still promotes on its next sighting, and its kind
vote still hears the sightings that expired. The sweep runs
when an event is recorded and in the graph job.

Every threshold here is read from the environment at call time. The
defaults were chosen by replaying the extracted sightings of 24 synthetic
seats from unrelated industries through this gate, not one user's data.

A name the user forgot (`entity_tombstones`) is never written here.
"""
from __future__ import annotations

import bisect
import json
import os
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from app.services import entity_typing
from app.services.kg_keys import entity_name_key, norm_name

_OFF = ("0", "false", "False")
_DEFAULT_MIN_SESSIONS = 2
_DEFAULT_TTL_DAYS = 30.0
_DEFAULT_SESSION_GAP_S = 3600.0
# The tombstone reason of a dismissed candidate. Only a forgotten entity
# blocks the name from coming back as a person; a dismissal does not.
DISMISS_REASON = "candidate_dismiss"
# The event source of the user's own typed chat turns (chat_ingest). Its
# source class is `direct_message`, but so is any other chat-like source;
# "the user typed it" is this source and nothing else.
TYPED_SOURCE = "chat.user"
_SPAN_MAX = 400
_PARENT_MAX = 80                # projects.MAX_NAME


def enabled() -> bool:
    """QUILL_ENTITY_CANDIDATES gate, read at call time. Off: mint at first
    sight as before (the ledger is still written)."""
    return os.environ.get("QUILL_ENTITY_CANDIDATES", "1") not in _OFF


def min_sessions() -> int:
    try:
        return max(1, int(os.environ.get(
            "QUILL_ENTITY_CANDIDATE_SESSIONS", _DEFAULT_MIN_SESSIONS)))
    except (TypeError, ValueError):
        return _DEFAULT_MIN_SESSIONS


def ttl_days() -> float:
    try:
        return float(os.environ.get(
            "QUILL_ENTITY_CANDIDATE_TTL_DAYS", _DEFAULT_TTL_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_TTL_DAYS


def bound_cap() -> int:
    """QUILL_ENTITY_MENTION_CAP (default 500): the sightings kept per entity
    once a name is a node, newest first (`Store.prune_entity_mentions`, and
    on every bound write). Never below what the kind vote reads
    (`entity_typing.MAX_MENTIONS`), so a cap cannot change a vote. The
    oldest sightings of a busy entity go, except the newest few of each
    kind of evidence (source, filing project, parent, kind, typed), which
    the scope rule and the repair read with the rest."""
    floor = int(entity_typing.MAX_MENTIONS)
    try:
        v = int(os.environ.get("QUILL_ENTITY_MENTION_CAP", floor))
    except (TypeError, ValueError):
        v = floor
    return max(floor, v)


def session_gap_s() -> float:
    """QUILL_ENTITY_CANDIDATE_SESSION_GAP_S: two sightings no session row
    covers are one session when they are at most this far apart. An hour,
    not the people path's 300 s: a name said twice in one long meeting or
    one afternoon of mail is one sighting of interest, not two."""
    try:
        return max(0.0, float(os.environ.get(
            "QUILL_ENTITY_CANDIDATE_SESSION_GAP_S", _DEFAULT_SESSION_GAP_S)))
    except (TypeError, ValueError):
        return _DEFAULT_SESSION_GAP_S


# --------------------------------------------------------------- context --
@dataclass
class Context:
    """Where a batch of names was read: one event."""
    event_id: int | None = None
    event_source: str = ""
    source_class: str = ""
    mint_ok: bool = False           # the extractor's mint gate for this event
    observed_at: float = 0.0        # the event's own time
    now: float = 0.0                # processing time
    filing_project: str | None = None
    text: str = ""
    _text_key: str | None = field(default=None, repr=False)

    def typed(self, name: str) -> bool:
        """The user wrote `name` themselves: the event is their own chat
        turn and the name is in what they typed (a name the model produced
        that is not in the text was not typed by anyone). With no text to
        check against, the source alone decides."""
        if (self.event_source or "").lower() != TYPED_SOURCE:
            return False
        if not (self.text or "").strip():
            return True
        return _name_in_text(name, self)


def _name_in_text(name: str, ctx: Context) -> bool:
    key = norm_name(name)
    if key:
        if ctx._text_key is None:
            ctx._text_key = f" {norm_name(ctx.text)} "
        return f" {key} " in ctx._text_key
    # A name with no ASCII letter or digit: compare case-folded.
    return entity_name_key(name) in " ".join(ctx.text.casefold().split())


def context(store, *, anchor: int | None, now: float,
            event_source: str = "", source_class: str = "",
            mint_ok: bool = False, text: str = "") -> Context:
    """Build the context for one event and run the lazy expiry sweep.

    The sighting time is the anchor event's own time, not `now`: a backfill
    processes a month of turns in one second, and sessions are counted on
    when things were said. The filing project is the project the event was
    filed under (`events.meta["project"]`).

    Speech reaches the extractor without an event source, so its gate class
    is `unknown`. The ledger records what the anchor event really is
    (`private_conversation` / `meeting_transcript`), so a name heard in a
    meeting and seen in an email counts as two source classes."""
    ctx = Context(event_id=int(anchor) if anchor is not None else None,
                  event_source=(event_source or "").strip(),
                  source_class=source_class or "", mint_ok=bool(mint_ok),
                  observed_at=float(now), now=float(now), text=text or "")
    ev = None
    if anchor is not None:
        try:
            ev = store.get_event(int(anchor))
        except Exception:
            ev = None
    if ev:
        try:
            if ev.get("time") is not None:
                ctx.observed_at = float(ev["time"])
        except (TypeError, ValueError):
            pass
        meta = ev.get("meta")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = None
        if isinstance(meta, dict):
            project = meta.get("project")
            if isinstance(project, str) and project.strip():
                ctx.filing_project = project.strip()
        if not ctx.event_source:
            stored = str(ev.get("source") or "").strip()
            ctx.event_source = stored
            if stored.lower().startswith("audio"):
                try:
                    from app.services import source_policy
                    ctx.source_class = source_policy.classify_source(
                        event_source=stored)
                except Exception:
                    pass
    sweep_expired(store, now=ctx.now)
    return ctx


# ------------------------------------------------------------ promotion --
def mint_eligible(row: dict) -> bool:
    """May a sighting from this source make a node? The source class must
    allow `knowledge_entities` and the event must not be screen OCR: the
    same rule as the extractor's mint gate, read off the ledger row, so a
    policy that tightens later applies to what is already pending."""
    if (row.get("event_source") or "").lower().startswith("desktop.screen"):
        return False
    try:
        from app.services import source_policy
        return bool(source_policy.policy_for(
            row.get("source_class") or "unknown").knowledge_entities)
    except Exception:
        return False


@dataclass
class Verdict:
    """What the gate says about one pending name."""
    name_key: str
    promote: bool = False
    reason: str = "no_mentions"
    name: str = ""                  # the spelling a node would carry
    kind: str | None = None         # the kind the sightings propose
    kind_source: str = "model"      # who vouches for it (a prior: rule)
    confidence: float | None = None
    mentions: int = 0
    summarised: int = 0             # expired sightings the summary counts
    sessions: int = 0
    source_classes: int = 0
    weak: bool = False
    code_like: bool = False
    typed: bool = False


def _display_name(rows: list[dict]) -> str:
    """The spelling to mint under: what the user typed, else a proper-name
    spelling over a lowercase one, else the most common, else the latest."""
    counts = Counter(r["name"] for r in rows)
    best = max(rows, key=lambda r: (
        bool(r.get("typed")), not r.get("weak"), counts[r["name"]],
        float(r.get("observed_at") or 0.0), int(r["id"])))
    return best["name"]


def _kind_vote(store, key: str, name: str) -> entity_typing.Verdict:
    try:
        return entity_typing.vote(store, name_key=key, name=name)
    except Exception as exc:
        print(f"[entity_candidates] kind vote skipped ({exc}).")
        return entity_typing.Verdict()


def _retype(store, entity_id: int) -> None:
    """A sighting just bound to `entity_id`: the vote runs again (it changes
    nothing unless the kind is a model guess the evidence now outvotes)."""
    try:
        entity_typing.retype(store, int(entity_id))
    except Exception as exc:
        print(f"[entity_candidates] kind vote skipped ({exc}).")


def session_labels(store, timestamps) -> list[int]:
    """One session number per sighting time, in the order given.

    A sighting inside a `sessions` row takes that row's whole span; any
    other sighting is a point. Spans and points no more than
    `session_gap_s()` apart are one session, so distinct events in one
    sitting are one session, and two session rows a few minutes apart (the
    table cuts at the people path's 300 s) are one sitting too."""
    stamps = [float(t) for t in timestamps]
    if not stamps:
        return []
    gap = session_gap_s()
    try:
        rows = store.session_spans(min(stamps), max(stamps))
    except Exception:
        rows = []
    starts = [lo for lo, _ in rows]
    span: dict[float, tuple[float, float]] = {}
    for ts in set(stamps):
        # The session row that started last at or before `ts`, if it is
        # still running then (rows do not overlap: consolidation cuts them
        # in sequence).
        i = bisect.bisect_right(starts, ts) - 1
        span[ts] = rows[i] if i >= 0 and rows[i][1] >= ts else (ts, ts)
    label: dict[float, int] = {}
    n, end = -1, None
    for ts in sorted(span, key=lambda t: span[t]):
        lo, hi = span[ts]
        if end is None or lo - end > gap:
            n += 1
            end = hi
        else:
            end = max(end, hi)
        label[ts] = n
    return [label[t] for t in stamps]


def distinct_sessions(store, timestamps) -> int:
    """How many sessions a set of entity sightings spans
    (`session_labels`). The people path keeps its own rule
    (`mint_recurrence.distinct_sessions`, 300 s)."""
    return len(set(session_labels(store, timestamps)))


# --------------------------------------------------------------- summary --
# Check A: when pending sightings expire, what they added up to is kept,
# without any text, so a name that recurs every six weeks still promotes on
# its next sighting. One row per name key hash (`Store.entity_candidate_
# summary`): distinct mint-eligible sessions and source classes, first and
# last eligible sighting, sessions the user typed it in (and the latest of
# those sightings), whether every spelling was a lowercase phrase, the
# filing project as a hash, and a kind histogram ({kind: n, w (summed vote
# weight), cs (summed confidence), c (best confidence from an eligible
# source), s (eligible sessions), l (the latest of those sightings), cls
# (eligible source classes)}). Kept QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS
# (365) from its last update, and not at all at 0 (expiry then deletes, as
# before summaries). Forgetting or dismissing the name, or any tombstone,
# deletes it; a promoted name keeps it, as kind evidence the ledger no
# longer has, until it expires.
_DEFAULT_SUMMARY_TTL_DAYS = 365.0


def summary_ttl_days() -> float:
    try:
        return float(os.environ.get(
            "QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS",
            _DEFAULT_SUMMARY_TTL_DAYS))
    except (TypeError, ValueError):
        return _DEFAULT_SUMMARY_TTL_DAYS


def _summary(store, name_key: str) -> dict | None:
    try:
        return store.entity_candidate_summary(name_key)
    except Exception:
        return None


def _merged_sessions(store, past: int, last, rows: list[dict]) -> int:
    """Distinct sessions over `past` summarised sessions (the latest
    sighting in them at `last`) and the sightings in `rows`. The summary's
    last sighting stands in for its sessions: a sighting in that same
    sitting does not count twice."""
    stamps = [float(r["observed_at"]) for r in rows
              if r.get("observed_at") is not None]
    if not past or last is None or not stamps:
        return past + (distinct_sessions(store, stamps) if stamps else 0)
    return past + distinct_sessions(store, stamps + [float(last)]) - 1


def _with_summary(store, summary: dict | None, eligible: list[dict]) -> int:
    """Distinct sessions over the summary and today's eligible sightings."""
    return _merged_sessions(store, int((summary or {}).get("sessions") or 0),
                            (summary or {}).get("last_seen"), eligible)


def _typed_sessions(store, summary: dict | None, typed: list[dict]) -> int:
    """Distinct sessions the user typed the name in, the summary's
    included."""
    return _merged_sessions(
        store, int((summary or {}).get("typed_sessions") or 0),
        (summary or {}).get("typed_last"), typed)


def _last_seen(rows: list[dict], before=None):
    stamps = [float(r["observed_at"]) for r in rows
              if r.get("observed_at") is not None]
    if before is not None:
        stamps.append(float(before))
    return max(stamps) if stamps else None


def _fold(store, rows: list[dict], summary: dict | None) -> dict:
    """The summary of `summary` plus the sightings in `rows` (one name key,
    all about to expire). Numbers, kind and source-class ids and a hash
    only: the name, spans, events and projects are not kept."""
    s = dict(summary or {})
    eligible = [r for r in rows if mint_eligible(r)]
    s["sessions"] = _with_summary(store, summary, eligible)
    classes = set((summary or {}).get("source_classes") or ())
    classes.update(r["source_class"] for r in eligible
                   if r.get("source_class"))
    s["source_classes"] = sorted(classes)
    seen = [float(r["observed_at"]) for r in eligible
            if r.get("observed_at") is not None]
    if summary and summary.get("first_seen") is not None:
        seen.append(float(summary["first_seen"]))
    if summary and summary.get("last_seen") is not None:
        seen.append(float(summary["last_seen"]))
    s["first_seen"] = min(seen) if seen else None
    s["last_seen"] = max(seen) if seen else None
    # The lazy sweep runs on every record, so one sitting's sightings
    # usually expire in several sweeps: each count merges through the last
    # sighting already folded in (`_merged_sessions`), never just adds up.
    typed = [r for r in eligible if r.get("typed")]
    s["typed_sessions"] = _typed_sessions(store, summary, typed)
    s["typed_last"] = _last_seen(typed, (summary or {}).get("typed_last"))
    s["weak"] = bool((summary or {}).get("weak", True)) \
        and all(bool(r.get("weak")) for r in rows)
    project = entity_typing._one_filing_project(rows)
    scope = entity_typing.scope_hash(project) if project else ""
    if summary is not None and summary.get("scope") != scope:
        scope = "*"
    s["scope"] = scope
    s["mentions"] = int((summary or {}).get("mentions") or 0) + len(rows)
    kinds = {k: dict(v) for k, v in
             ((summary or {}).get("kinds") or {}).items()}
    by_kind: dict[str, list[dict]] = {}
    for r in rows:
        k = entity_typing._norm_kind(r.get("proposed_kind"))
        if k:
            by_kind.setdefault(k, []).append(r)
    for k, group in by_kind.items():
        h = kinds.setdefault(k, {"n": 0, "w": 0.0, "cs": 0.0, "c": 0.0,
                                 "s": 0, "cls": []})
        h["n"] = int(h.get("n") or 0) + len(group)
        h["w"] = round(float(h.get("w") or 0.0) + sum(
            entity_typing._confidence(r) * entity_typing.source_weight(r)
            for r in group), 6)
        h["cs"] = round(float(h.get("cs") or 0.0) + sum(
            entity_typing._confidence(r) for r in group), 6)
        elig = [r for r in group if mint_eligible(r)]
        stated = [c for c in (entity_typing._stated_confidence(r)
                              for r in elig) if c is not None]
        h["c"] = max([float(h.get("c") or 0.0)] + stated)
        h["s"] = _merged_sessions(store, int(h.get("s") or 0), h.get("l"),
                                  elig)
        h["l"] = _last_seen(elig, h.get("l"))
        h["cls"] = sorted(set(h.get("cls") or ()) | {
            r["source_class"] for r in elig if r.get("source_class")})
    s["kinds"] = kinds
    return s


def _weak(rows: list[dict], name: str, summary: dict | None = None) -> bool:
    """A name is weak only while every spelling seen is a lowercase noun
    phrase (one sighting written as a name, "Pet Groomers", is evidence it is
    one), and, for a single word, no sighting called it a tool or an org
    (`entity_typing.weak_name`). Expired sightings count through the
    summary."""
    kinds = [r.get("proposed_kind") for r in rows]
    if summary:
        if not summary.get("weak", True):
            return False
        kinds.extend((summary.get("kinds") or {}).keys())
    return (all(bool(r.get("weak")) for r in rows)
            and entity_typing.weak_name(name, kinds))


def evaluate(store, name_key: str) -> Verdict:
    """Decide whether the pending sightings of `name_key` make a node.
    Sightings that expired count through the name's summary: a name seen
    once six weeks ago and once today has been seen in two sessions."""
    v = Verdict(name_key=name_key)
    rows = store.entity_mentions(name_key=name_key, status="pending")
    if not rows:
        return v
    summary = _summary(store, name_key)
    v.mentions = len(rows)
    v.summarised = int((summary or {}).get("mentions") or 0)
    v.name = _display_name(rows)
    kv = _kind_vote(store, name_key, v.name)
    v.kind, v.confidence = kv.kind, kv.confidence
    v.kind_source = entity_typing.stored_source(kv)
    # Only what a mint-eligible source read counts toward recurrence. A
    # bind-only sighting (an assistant note, screen text, a news page) is
    # typing and scope evidence, never a session or a source class.
    eligible = [r for r in rows if mint_eligible(r)]
    v.sessions = _with_summary(store, summary, eligible)
    v.source_classes = len({r["source_class"] for r in eligible
                            if r.get("source_class")}
                           | set((summary or {}).get("source_classes")
                                 or ()))
    v.weak = _weak(rows, v.name, summary)
    v.code_like = entity_typing.code_like(v.name)
    typed = [r for r in eligible if r.get("typed")]
    v.typed = bool(typed)
    if not eligible:
        v.reason = "bind_only"
        return v
    # A value is a claim's object, never a node: it stays in the fact text
    # however often it is said.
    if entity_typing.value_literal(v.name):
        v.reason = "value_literal"
        return v
    need = min_sessions()
    if v.weak:
        if typed and _typed_sessions(store, summary, typed) >= need:
            v.promote, v.reason = True, "typed_recurrence"
        else:
            v.reason = "weak"
        return v
    if typed and not v.code_like:
        v.promote, v.reason = True, "typed"
    elif v.sessions >= need:
        v.promote, v.reason = True, "sessions"
    elif v.source_classes >= 2:
        v.promote, v.reason = True, "source_classes"
    else:
        v.reason = "code_like" if (typed and v.code_like) else "waiting"
    return v


def adopt(store, name_key: str, entity_id: int, *,
          ts: float | None = None) -> dict:
    """Bind every pending sighting of `name_key` to `entity_id`, keep each
    other spelling as an alias, and replay the relations pooled on the
    name. `Store.resolve_entity` does this itself when it creates a row;
    this is for a name that turned out to be an existing row."""
    if not name_key or not entity_id:
        return {"adopted": 0, "relations": 0}
    return store.adopt_entity_candidates(name_key, int(entity_id), ts=ts)


# ---------------------------------------------------------------- record --
def _parent_name(parent) -> str | None:
    """The project the model says a name belongs to, as one tidy string,
    or None. A local model that ignores the schema can send anything here,
    and a sighting must not be lost over it."""
    if not isinstance(parent, str):
        return None
    return " ".join(parent.split())[:_PARENT_MAX] or None


def _record(store, name: str, ctx: Context, *, kind=None, confidence=None,
            span=None, parent=None, entity_id: int | None = None) -> int:
    try:
        return store.record_entity_mention(
            name, proposed_kind=kind, confidence=confidence,
            source_class=ctx.source_class or None,
            event_source=ctx.event_source or None, event_id=ctx.event_id,
            span=(span or "").strip()[:_SPAN_MAX] or None,
            filing_project=ctx.filing_project, parent=_parent_name(parent),
            observed_at=ctx.observed_at, entity_id=entity_id,
            weak=entity_typing.common_noun_like(name),
            typed=ctx.typed(name))
    except Exception as exc:
        print(f"[entity_candidates] ledger write skipped ({exc}).")
        return 0


def record_bound(store, name: str, entity_id: int, ctx: Context, *,
                 kind=None, confidence=None, span=None, parent=None) -> int:
    """A sighting of a name that resolved to `entity_id`. Anything still
    pending under the name goes to that entity with it: the row may have
    come to answer to the name by a route that adopted nothing (a rename, an
    alias, a value literal)."""
    mid = _record(store, name, ctx, kind=kind, confidence=confidence,
                  span=span, parent=parent, entity_id=int(entity_id))
    try:
        adopt(store, entity_name_key(name), int(entity_id), ts=ctx.now)
    except Exception as exc:
        print(f"[entity_candidates] adoption skipped ({exc}).")
    if mid:
        _retype(store, int(entity_id))
        _lift_repair_hide(store, int(entity_id))
    return mid


def _lift_repair_hide(store, entity_id: int) -> None:
    """A row the automatic repair hid because only assistant notes or
    screen text named it is hidden until a real sighting: when the
    sightings bound since would have promoted the name, it is shown again
    (`entity_repair.lift_note_only_hide`). Only hidden rows are looked at."""
    try:
        if not store.entity_is_hidden(int(entity_id)):
            return
        from app.services import entity_repair
        entity_repair.lift_note_only_hide(store, int(entity_id))
    except Exception as exc:
        print(f"[entity_candidates] repair hide check skipped ({exc}).")


def consider_new(store, name: str, ctx: Context, *, kind=None,
                 confidence=None, span=None, parent=None,
                 mint: Callable[[str, str | None, float | None, str], int],
                 force: bool = False) -> int:
    """A name that matched nothing and was not forgotten: write the pending
    sighting, then promote if the sightings now add up. `mint(name, kind,
    confidence, kind_source)` creates the entity (the caller's resolver or
    store) with the kind the vote gives, vouched for by `kind_source`
    (`rule` when a deterministic prior decided, else `model`), and returns
    its id. Returns the entity id, or 0 while the name waits.

    `force` mints whatever the gate says (the gate is off): the name is
    still typed by the vote over its sightings, this one included."""
    key = entity_name_key(name)
    if not key:
        return 0
    if not _record(store, name, ctx, kind=kind, confidence=confidence,
                   span=span, parent=parent):
        if not force:
            return 0
        # Gate off and the ledger could not take the sighting: the name is
        # still minted at first sight, as it was before the ledger, with
        # what this one sighting votes for.
        try:
            return int(mint(name, entity_typing.mention_kind(
                kind, code=entity_typing.code_like(name),
                scoped=bool(ctx.filing_project)), confidence, "model") or 0)
        except Exception as exc:
            print(f"[entity_candidates] first-sight mint skipped ({exc}).")
            return 0
    verdict = evaluate(store, key)
    if not (verdict.promote or force):
        return 0
    try:
        eid = int(mint(verdict.name, verdict.kind, verdict.confidence,
                       verdict.kind_source) or 0)
    except Exception as exc:
        print(f"[entity_candidates] promotion of a candidate skipped ({exc}).")
        return 0
    if not eid:
        # Refused at the door (a person took the name, or it was forgotten
        # in the meantime): the sightings stay pending until they expire.
        return 0
    if verdict.promote:
        # The gate made this row: one promotion for the metrics, no name.
        # (A manual add, a filing or an email-domain org mints without the
        # gate and is not counted; nor is a first-sight mint with it off.)
        try:
            store.log_entity_candidates("promoted", names=1,
                                        sightings=verdict.mentions)
        except Exception as exc:
            print(f"[entity_candidates] promotion count skipped ({exc}).")
    # A new row adopted its sightings as it was created. When the name
    # folded into a row that was already there (a fuzzy neighbour minted
    # since), they are still pending: hand them over, and let the vote see
    # them.
    adopt(store, key, eid, ts=ctx.now)
    _retype(store, eid)
    return eid


@dataclass(frozen=True)
class Observed:
    """What a sighting came to: an entity id, or a name still waiting."""
    entity_id: int = 0
    pending: bool = False
    name: str = ""


def observe(store, resolver, name: str, kind: str | None = None, *,
            ctx: Context, confidence=None, span=None, parent=None,
            touch_alias: bool = True) -> Observed:
    """One name the extractor read in `ctx`: resolve it, write the ledger,
    and mint only when the gate says so.

    - It matches an entity: touched (and re-kinded by rank) as before, with
      a `bound` sighting.
    - It can never be an entity, or the user forgot it: nothing is written.
    - It is new: a `pending` sighting, then the promotion rule.

    A bind-only source (`ctx.mint_ok` false) matches by exact name only, as
    before; its sighting of a new name still lands in the ledger, for the
    kind vote and scope, but never counts toward promotion."""
    nm = (name or "").strip()
    if not nm:
        return Observed()
    raw_kind = kind
    gate = enabled()

    def _mint(n, k, c, src):
        return resolver.resolve_entity(n, k, ts=ctx.now, kind_source=src,
                                       kind_confidence=c)

    if ctx.mint_ok:
        m = resolver.match_entity(nm, kind)
        if m.status == "rejected":
            resolver.mint_entity(m, ts=ctx.now)     # person-shaped -> people
            return Observed()
        if m.status == "new" and m.forgotten:
            return Observed()
        if m.status == "existing":
            # The model proposed this kind: lowest rank, so it never
            # overrides a rule, a filing or the user (and never re-kinds a
            # row here at all: the vote in `record_bound` does that).
            eid = resolver.mint_entity(
                m, kind, ts=ctx.now, kind_source="model",
                kind_confidence=confidence)
            if not eid:
                return Observed()
            record_bound(store, nm, eid, ctx, kind=raw_kind,
                         confidence=confidence, span=span, parent=parent)
            return Observed(entity_id=int(eid))
        # A new name. Gate off: minted at first sight, as before, with the
        # kind the vote gives this one sighting.
        eid = consider_new(store, nm, ctx, kind=raw_kind,
                           confidence=confidence, span=span, parent=parent,
                           mint=_mint, force=not gate)
        if not gate:
            return Observed(entity_id=eid)
        return Observed(entity_id=eid) if eid \
            else Observed(pending=True, name=nm)

    # Bind-only: an exact name that already exists, nothing else.
    eid = store.find_entity_exact(nm)
    if eid:
        if touch_alias:
            try:
                store.touch_entity(eid, ctx.now, alias=nm)
            except Exception:
                pass
        record_bound(store, nm, eid, ctx, kind=raw_kind,
                     confidence=confidence, span=span, parent=parent)
        return Observed(entity_id=int(eid))
    m = resolver.match_entity(nm, kind)
    if m.status != "new" or m.forgotten:
        # Rejected, forgotten, or only a fuzzy neighbour: a bind-only source
        # never folded a spelling into a neighbour, and still does not.
        return Observed()
    # A new name from a bind-only source, gate on or off: the sighting is
    # remembered for typing and scope, and that is all. It counts toward no
    # promotion, and it is not reported as pending, so the extractor pools
    # no relation on it: an edge this source asserted about a name that
    # does not exist is dropped, as it was before the ledger.
    _record(store, nm, ctx, kind=raw_kind, confidence=confidence,
            span=span, parent=parent)
    return Observed()


# ---------------------------------------------------------------- expiry --
# One sweep at a time: two folding the same name at once would both build
# on the old summary and one count would be lost.
_sweep_lock = threading.Lock()


def sweep_expired(store, *, now: float | None = None,
                  bound: bool = False) -> dict:
    """Delete pending sightings (and pooled relations) past the TTL, after
    folding what they add up to into each name's text-free summary, and
    drop summaries past their own TTL. When the fold cannot be written,
    nothing expires this time.

    `bound` (the graph job's sweep, not the one on every record) also
    bounds the sightings of names that are nodes
    (`Store.prune_entity_mentions`): those bound to a row that is gone are
    deleted, each entity keeps its newest `bound_cap()`, and spans older
    than the TTL are cleared (the rest of the row stays, for the kind vote,
    the scope rule and the repair)."""
    now = float(now if now is not None else time.time())
    cutoff = now - ttl_days() * 86400.0
    keep_days = summary_ttl_days()
    folded = 0
    with _sweep_lock:
        expiring = []
        try:
            expiring = store.expiring_entity_candidates(cutoff)
        except Exception as exc:
            print(f"[entity_candidates] expiry read skipped ({exc}).")
        groups: dict[str, list[dict]] = {}
        for r in expiring:
            groups.setdefault(r["name_key"], []).append(r)
        fold_failed = False
        # A summary TTL of 0 keeps nothing: expiry is a plain delete.
        if groups and keep_days > 0:
            updates = {key: _fold(store, rows, _summary(store, key))
                       for key, rows in groups.items()}
            try:
                folded = int(store.fold_entity_candidates(
                    updates, [int(r["id"]) for r in expiring], now=now))
            except Exception as exc:
                print(f"[entity_candidates] summary fold skipped ({exc}).")
                fold_failed = True
        gone = {"mentions": 0, "relations": 0}
        # Deleting what could not be folded would lose its counts (a name
        # that recurs every six weeks would start again): after a failed
        # fold the next sweep folds and expires it.
        if not fold_failed:
            try:
                gone = store.expire_entity_candidates(
                    cutoff, summary_cutoff=now - keep_days * 86400.0)
            except Exception as exc:
                print(f"[entity_candidates] expiry sweep skipped ({exc}).")
    if bound:
        try:
            gone["bound"] = store.prune_entity_mentions(
                cap=bound_cap(), span_cutoff=cutoff)
        except Exception as exc:
            print(f"[entity_candidates] ledger bound skipped ({exc}).")
    # Folded sightings are deleted too: one count of what left the ledger.
    gone["mentions"] = int(gone.get("mentions") or 0) + folded
    if gone["mentions"]:
        # For the seat's metrics: how many names expired, and no more.
        try:
            store.log_entity_candidates("expired", names=len(groups),
                                        sightings=gone["mentions"])
        except Exception as exc:
            print(f"[entity_candidates] expiry count skipped ({exc}).")
    return gone


# ------------------------------------------------------ routes' helpers --
DEFAULT_LIST_LIMIT = 200


def list_candidates(store, *, limit: int | None = DEFAULT_LIST_LIMIT,
                    totals: dict | None = None) -> list[dict]:
    """The pending names with what is known about it, most recent first:
    the newest `limit` of them (None: all). Names are ordered on their
    sightings alone, and the vote, the sessions and the summary are worked
    out only for the ones returned, so a seat with thousands of pending
    names answers as fast as one with a few. `totals`, when given, gets
    {"pending": names waiting} (before the limit)."""
    groups: dict[str, list[dict]] = {}
    for r in store.entity_mentions(status="pending"):
        groups.setdefault(r["name_key"], []).append(r)

    def newest(rows: list[dict]) -> float:
        return max((float(r["observed_at"]) for r in rows
                    if r.get("observed_at") is not None), default=0.0)
    ordered = sorted(groups.items(), key=lambda kv: (
        -newest(kv[1]), _display_name(kv[1]).lower()))
    out = []
    waiting = 0
    for key, rows in ordered:
        # A row under the name (a hidden value literal, or one made by a
        # caller that adopted nothing) means it is not waiting to exist.
        if any(store.find_entity_exact(n) is not None
               for n in dict.fromkeys(r["name"] for r in rows)):
            continue
        waiting += 1
        if limit is not None and len(out) >= max(0, int(limit)):
            continue
        # What the model said, in stored form; `proposed_kind` is what the
        # vote would make of it.
        kinds = Counter(entity_typing._norm_kind(r.get("proposed_kind"))
                        for r in rows)
        kinds.pop(None, None)
        # Sessions and source classes are what promotion counts: the
        # mint-eligible sightings only, with what expired ones added up to
        # (`summarised`). `source_class_names` lists today's, all of them.
        eligible = [r for r in rows if mint_eligible(r)]
        summary = _summary(store, key)
        classes = sorted({r["source_class"] for r in rows
                          if r.get("source_class")})
        stamps = [float(r["observed_at"]) for r in rows
                  if r.get("observed_at") is not None]
        if summary and summary.get("first_seen") is not None:
            stamps.append(float(summary["first_seen"]))
        name = _display_name(rows)
        kind = _kind_vote(store, key, name).kind
        out.append({
            "name": name,
            "proposed_kind": kind,
            "proposed_kinds": dict(kinds.most_common()),
            "mentions": len(rows),
            "summarised": int((summary or {}).get("mentions") or 0),
            "bind_only_mentions": len(rows) - len(eligible),
            "sessions": _with_summary(store, summary, eligible),
            "source_classes": len(
                {r["source_class"] for r in eligible if r.get("source_class")}
                | set((summary or {}).get("source_classes") or ())),
            "source_class_names": classes,
            "first_seen": min(stamps) if stamps else None,
            "last_seen": max(stamps) if stamps else None,
            "weak": _weak(rows, name, summary),
            "code_like": entity_typing.code_like(name),
            "typed": any(bool(r.get("typed")) for r in eligible),
            "mint_eligible": bool(eligible),
        })
    out.sort(key=lambda c: (-(c["last_seen"] or 0.0), c["name"].lower()))
    if totals is not None:
        totals["pending"] = waiting
    return out


def add_manual(store, name: str, kind: str | None = None, *,
               now: float | None = None) -> dict:
    """The user adds an entity by hand: minted at once at user rank (which
    lifts a tombstone and brings a hidden row back), and anything pending
    under the name is adopted."""
    nm = " ".join((name or "").split())
    if not nm:
        return {"ok": False, "error": "empty_name"}
    now = float(now if now is not None else time.time())
    existed = store.find_entity_exact(nm) is not None
    eid = int(store.resolve_entity(nm, kind, ts=now, kind_source="user",
                                   kind_confidence=1.0) or 0)
    if not eid:
        return {"ok": False, "error": "not_created"}
    # A new row adopted what was pending as it was created; a row that was
    # already there (a hidden one coming back) adopts it here.
    adopt(store, entity_name_key(nm), eid, ts=now)
    return {"ok": True, "id": eid, "created": not existed}


def dismiss(store, name: str) -> dict:
    """The user says a pending name is nothing: its sightings and pooled
    relations are deleted and the name is tombstoned, so extraction cannot
    bring it back as an entity. The tombstone keeps the kind the vote gave
    it, but it does not stop the name being a person (`DISMISS_REASON`):
    the user said "not a thing", not "an org". Only a name with no entity
    row can be dismissed (an entity is forgotten from its own row)."""
    nm = (name or "").strip()
    key = entity_name_key(nm)
    if not key:
        return {"ok": False, "error": "empty_name"}
    if store.find_entity_exact(nm) is not None:
        return {"ok": False, "error": "is_entity"}
    rows = store.entity_mentions(name_key=key, status="pending")
    if not rows:
        return {"ok": False, "error": "not_pending"}
    kind = _kind_vote(store, key, _display_name(rows)).kind
    store.tombstone_entity_name(nm, kind=kind, reason=DISMISS_REASON,
                                actor="user")
    return {"ok": True, "dismissed": len(rows)}
