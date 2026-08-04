"""Consolidation — merge adjacent utterances into conversational *turns*.

The capture pipeline emits one Event per VAD-segmented utterance, so a single
thought arrives as fragments: "way to gather.", "What, uh, what?", "I don't know
where I can…". Extracting facts from fragments yields garbage facts, so this step
groups adjacent AUDIO events — same speaker, small time gap — into one turn
*before* any extraction runs.

Raw events stay the canonical, per-utterance truth (and the provenance anchor);
turns are a derived layer that links back to the member event ids. Grouping is a
pure function (`group_turns`) so it's trivially testable; `rebuild()` recomputes
the whole `turns` table from the audio timeline (idempotent, cheap at prototype
scale — incremental consolidation can move into the worker later).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app.config import settings
from app.events import Event, Modality
from app.storage import Store, get_store


def enrolled_name(ev: Event) -> str:
    """The enrolled (known) speaker name, or '' if the utterance is anonymous.

    This is the only *reliable* break signal: anonymous cluster labels
    ("Speaker 14") are noisy and change between adjacent utterances, so we don't
    let them split a turn — only a confirmed identity does.
    """
    spk = ev.meta.get("speaker") if isinstance(ev.meta, dict) else None
    if isinstance(spk, dict) and spk.get("is_known"):
        return spk.get("name") or ""
    return ""


def speaker_label(turn: "Turn | dict | None") -> str:
    """Display name for extractor labeling (plan 2.1). Empty → 'unknown speaker'."""
    if turn is None:
        return "unknown speaker"
    if isinstance(turn, dict):
        spk = (turn.get("speaker") or "").strip()
    else:
        spk = (getattr(turn, "speaker", None) or "").strip()
    return spk if spk else "unknown speaker"


def format_turn_transcript(turn: "Turn | dict | None", text: str | None = None) -> str:
    """`[<speaker or 'unknown speaker'>]: <text>` for speaker-aware extraction."""
    if text is None:
        if turn is None:
            text = ""
        elif isinstance(turn, dict):
            text = turn.get("text") or ""
        else:
            text = getattr(turn, "text", None) or ""
    return f"[{speaker_label(turn)}]: {(text or '').strip()}"


def _display_label(ev: Event) -> str:
    """A label to show (anonymous cluster label or person), best-effort."""
    spk = ev.meta.get("speaker") if isinstance(ev.meta, dict) else None
    if isinstance(spk, dict):
        return spk.get("name") or spk.get("label") or ""
    return ev.people[0] if ev.people else ""


@dataclass
class Turn:
    start: float
    end: float
    speaker: str
    text: str
    event_ids: list[int] = field(default_factory=list)
    audio_paths: list[str] = field(default_factory=list)
    n_utterances: int = 0

    def settle_at(self, gap: float | None = None) -> float:
        """Wall-clock time at which this turn becomes SETTLED — i.e. the last
        moment a new utterance could still merge into it. Once now > settle_at,
        the turn is final and safe to extract/route exactly once."""
        g = settings.consolidation.max_gap_s if gap is None else gap
        return self.end + g

    def is_settled(self, now: float, gap: float | None = None) -> bool:
        """Has the silence gap since the last utterance elapsed? A settled turn
        can't grow, so it's safe to act on once (no double-counting)."""
        return now > self.settle_at(gap)

    def to_dict(self, now: float | None = None, gap: float | None = None) -> dict:
        d = {
            "start": self.start, "end": self.end, "speaker": self.speaker,
            "text": self.text, "event_ids": self.event_ids,
            "audio_paths": self.audio_paths, "n_utterances": self.n_utterances,
            "duration_s": round(self.end - self.start, 2),
        }
        if now is not None:
            d["settled"] = self.is_settled(now, gap)
            d["settle_at"] = round(self.settle_at(gap), 3)
        return d


def group_turns(rows: list[tuple[int, Event]], max_gap_s: float) -> list[Turn]:
    """Group (id, Event) pairs — assumed time-sorted ascending — into turns.

    A new turn starts when the silence gap since the last utterance exceeds
    `max_gap_s`, or when the *enrolled* speaker changes. Anonymous utterances
    (the common case until speakers are enrolled) group on time alone, so noisy
    cluster labels don't fragment a continuous turn.
    """
    turns: list[Turn] = []
    labels: list[list[str]] = []   # per-turn display labels, for a final vote
    keys: list[str] = []           # per-turn enrolled-speaker key
    cur: Turn | None = None
    for eid, ev in rows:
        key = enrolled_name(ev)
        text = (ev.summary or ev.raw or "").strip()
        apath = ev.meta.get("audio_path") if isinstance(ev.meta, dict) else None
        starts_new = (
            cur is None
            or (ev.time - cur.end) > max_gap_s
            or (key and keys[-1] and key != keys[-1])  # only a known identity breaks
        )
        if starts_new:
            cur = Turn(start=ev.time, end=ev.time, speaker="", text=text)
            turns.append(cur)
            labels.append([])
            keys.append(key)
        else:
            cur.text = (cur.text + " " + text).strip()
            cur.end = ev.time
            keys[-1] = keys[-1] or key   # remember an identity seen mid-turn
        cur.event_ids.append(eid)
        if apath:
            cur.audio_paths.append(apath)
        cur.n_utterances += 1
        lbl = _display_label(ev)
        if lbl:
            labels[-1].append(lbl)
    # Label each turn: prefer the enrolled name, else the dominant cluster label.
    for t, key, labs in zip(turns, keys, labels):
        t.speaker = key or (Counter(labs).most_common(1)[0][0] if labs else "")
    return turns


def settled_turns(rows: list[tuple[int, Event]], now: float,
                  gap: float | None = None) -> tuple[list[Turn], float | None]:
    """Group `rows` into turns and split off the SETTLED ones — the single shared
    definition of "this turn is final" that the extractor (and the #6 router, and
    telemetry) all key off, instead of each re-deriving it.

    Returns (settled, next_settle_in):
      settled         text-bearing turns whose silence gap has elapsed (final).
      next_settle_in  seconds until the earliest still-unsettled *text* turn
                      becomes settled, or None if there is none — the tail-latency
                      nudge, so the last thing said before a silence surfaces
                      without waiting for the next sound.

    Text-less fragments (which never produce a fact and are never marked) are
    excluded from both, so they can't cause an endless reschedule.
    """
    g = settings.consolidation.max_gap_s if gap is None else gap
    turns = group_turns(rows, g)
    settled = [t for t in turns if t.text.strip() and t.is_settled(now, g)]
    unsettled = [t for t in turns if t.text.strip() and not t.is_settled(now, g)]
    next_settle_in = (min(t.settle_at(g) for t in unsettled) - now
                      if unsettled else None)
    return settled, next_settle_in


def rebuild(store: Store | None = None) -> int:
    """Recompute the turns table from every audio event. Returns turn count."""
    store = store or get_store()
    rows = [(eid, ev) for eid, ev in store.all_with_ids()
            if ev.modality == Modality.AUDIO]
    rows.sort(key=lambda r: r[1].time)
    turns = group_turns(rows, settings.consolidation.max_gap_s)
    store.replace_turns(turns)
    return len(turns)
