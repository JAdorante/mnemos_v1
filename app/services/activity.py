"""Activity consolidation — fold desktop capture events into "what was I doing?".

Desktop capture (desktop_capture.py) emits one Event per selected screen frame
(`desktop.screen`) and one per mouse click (`desktop.click`). Individually those
answer "what was on screen at 11:42:07", not "what was I working on before
lunch". This layer is the desktop analog of audio's turns->sessions rollup:
adjacent desktop events in the SAME foreground app are one *activity* — an app
focus block with a window/focus trail, a click trail, and a short summary built
from the screen descriptions.

Like turns and sessions, activities are a *derived, rebuildable* layer — raw
events stay the canonical truth and every activity links back to its member
event ids. Grouping is a pure function (`group_activities`) so it's trivially
testable; `rebuild()` recomputes the whole table from the current desktop
events (idempotent). The worker re-runs it whenever new desktop events arrive,
exactly as audio events re-run consolidation.

Multimodal enrichment: after grouping, `join_context` (also pure) attaches the
audio transcripts and webcam vision events whose time falls inside each block,
so the summary can say what was *heard* and *seen* while the work happened.
Crucially the join never creates or splits an activity — segmentation stays
desktop-only, so an audio-only or webcam-only stretch produces no block and a
conversation in the middle of a coding session doesn't fracture it.
"""
from __future__ import annotations

import bisect
import time as _time
from dataclasses import dataclass, field

from app.config import settings
from app.events import Event, Modality
from app.storage import Store, get_store

# The event sources this layer folds. Filtering is by source, not modality:
# screens are VISION and clicks are INPUT, but both belong to the same block.
SOURCES = ("desktop.screen", "desktop.click")

# Caps so a marathon block doesn't bloat one row; member ids keep provenance.
_MAX_SUMMARY_CHARS = 700
_MAX_WINDOWS = 8
_MAX_NOTES = 4
# Per-snippet caps for the multimodal join ("heard:" / "saw:" segments); the
# how-many caps live in ConsolidationConfig (QUILL_ACTIVITY_MAX_HEARD/SAW).
_MAX_HEARD_CHARS = 90
_MAX_SAW_CHARS = 110


def app_of(window_title: str) -> str:
    """Best-effort application name from a foreground window title.

    Window titles conventionally end with the app name ("storage.py - nexus_v1
    - Cursor", "Inbox - Outlook"), so take the last dash-separated segment.
    Purely structural — no app- or user-specific tables.
    """
    t = (window_title or "").strip()
    if not t:
        return "desktop"
    for sep in (" — ", " – ", " - "):
        if sep in t:
            t = t.rsplit(sep, 1)[-1].strip()
    return t or "desktop"


def _window(ev: Event) -> str:
    w = ev.meta.get("window") if isinstance(ev.meta, dict) else None
    return (w or "").strip()


def _screen_note(ev: Event) -> str:
    """A short description of what was on screen, without the [window] prefix
    (the window/focus trail is tracked separately)."""
    vision = ev.meta.get("vision") if isinstance(ev.meta, dict) else None
    if isinstance(vision, dict) and (vision.get("description") or "").strip():
        return vision["description"].strip()
    s = (ev.summary or ev.raw or "").strip()
    if s.startswith("[") and "] " in s:
        s = s.split("] ", 1)[1]
    return s


@dataclass
class Activity:
    start: float
    end: float
    app: str = "desktop"
    windows: list[str] = field(default_factory=list)   # focus trail, first-seen order
    summary: str = ""
    event_ids: list[int] = field(default_factory=list)
    n_screens: int = 0
    n_clicks: int = 0
    # Multimodal context (filled by join_context, not by grouping): co-timed
    # audio/webcam event ids kept separate from the desktop event_ids so the
    # two provenance classes stay distinguishable.
    n_audio: int = 0
    n_webcam: int = 0
    ctx_event_ids: list[int] = field(default_factory=list)
    # Internal accumulators (folded into `summary` by _compose).
    _notes: list[str] = field(default_factory=list, repr=False)
    _clicks_by_window: dict = field(default_factory=dict, repr=False)

    def to_dict(self) -> dict:
        return {
            "start": self.start, "end": self.end, "app": self.app,
            "windows": self.windows, "summary": self.summary,
            "event_ids": self.event_ids,
            "n_screens": self.n_screens, "n_clicks": self.n_clicks,
            "n_audio": self.n_audio, "n_webcam": self.n_webcam,
            "ctx_event_ids": self.ctx_event_ids,
            "duration_s": round(self.end - self.start, 2),
        }


def _compose(a: Activity) -> None:
    """Fold the accumulated screen notes + click trail into one short summary."""
    parts: list[str] = []
    if a._notes:
        parts.append("; ".join(a._notes[:_MAX_NOTES]))
    if a.n_clicks:
        trail = ", ".join(
            f"{n} in {w}" if w else f"{n} on desktop"
            for w, n in list(a._clicks_by_window.items())[:_MAX_WINDOWS]
        )
        plural = "s" if a.n_clicks != 1 else ""
        parts.append(f"{a.n_clicks} click{plural} ({trail})" if trail
                     else f"{a.n_clicks} click{plural}")
    head = a.app
    if a.windows:
        head += f" — {'; '.join(a.windows[:_MAX_WINDOWS])}"
    body = ". ".join(p for p in parts if p)
    a.summary = (f"{head}: {body}" if body else head)[:_MAX_SUMMARY_CHARS]


def group_activities(rows: list[tuple[int, Event]], max_gap_s: float) -> list[Activity]:
    """Group (id, Event) desktop-capture pairs into app-focus activities.

    A new activity starts when the foreground app changes or the gap since the
    previous desktop event exceeds `max_gap_s` (capture off / machine idle).
    Window-title changes within the same app stay in one activity and become the
    focus trail. Input order is normalized to ascending time here.
    """
    ordered = sorted(rows, key=lambda r: r[1].time)
    acts: list[Activity] = []
    cur: Activity | None = None
    for eid, ev in ordered:
        win = _window(ev)
        app = app_of(win)
        starts_new = (
            cur is None
            or (ev.time - cur.end) > max_gap_s
            or app != cur.app
        )
        if starts_new:
            cur = Activity(start=ev.time, end=ev.time, app=app)
            acts.append(cur)
        cur.end = max(cur.end, ev.time)
        cur.event_ids.append(int(eid))
        if win and win not in cur.windows:
            cur.windows.append(win)
        if ev.source == "desktop.click":
            cur.n_clicks += 1
            cur._clicks_by_window[win] = cur._clicks_by_window.get(win, 0) + 1
        else:
            cur.n_screens += 1
            note = _screen_note(ev)
            if note and (not cur._notes or cur._notes[-1] != note):
                cur._notes.append(note)
    for a in acts:
        _compose(a)
    return acts


# --------------------------- multimodal enrichment ---------------------------
def _heard_snippet(ev: Event) -> str:
    """Short spoken snippet from an AUDIO event. Transcript-less audio events
    (e.g. source 'audio.skipped', kept as audio-only provenance) yield ''."""
    s = (ev.summary or ev.raw or "").strip()
    return s[:_MAX_HEARD_CHARS].strip()


def _saw_snippet(ev: Event) -> str:
    """Short webcam description hint from a VISION event — prefer the VLM's
    description over the raw payload (which may be OCR text or a placeholder)."""
    vision = ev.meta.get("vision") if isinstance(ev.meta, dict) else None
    if isinstance(vision, dict) and (vision.get("description") or "").strip():
        s = vision["description"].strip()
    else:
        s = (ev.summary or ev.raw or "").strip()
    if s == "[frame captured]":  # frame saved but never described — no signal
        return ""
    return s[:_MAX_SAW_CHARS].strip()


def _in_window(rows: list[tuple[int, Event]], start: float,
               end: float) -> list[tuple[int, Event]]:
    """The (id, Event) pairs whose time falls in [start, end]. `rows` must be
    sorted ascending by time (join_context sorts once for all activities)."""
    times = [ev.time for _, ev in rows]
    lo = bisect.bisect_left(times, start)
    hi = bisect.bisect_right(times, end)
    return rows[lo:hi]


def join_context(acts: list[Activity], audio_rows: list[tuple[int, Event]],
                 webcam_rows: list[tuple[int, Event]], *,
                 max_heard: int | None = None,
                 max_saw: int | None = None) -> list[Activity]:
    """Pure enrichment join: attach co-timed audio/webcam events to each block.

    For every activity, the audio and webcam events whose time falls inside
    [start, end] become its context — counted, linked via ctx_event_ids, and
    folded into the summary as short "heard: ..." / "saw: ..." segments
    (deduped, capped, and always within the overall summary cap). Grouping is
    untouched: this never creates, splits, or re-times an activity, so it is
    safe to call with any (or no) context events. Mutates and returns `acts`.
    """
    if max_heard is None:
        max_heard = settings.consolidation.activity_max_heard
    if max_saw is None:
        max_saw = settings.consolidation.activity_max_saw
    audio_sorted = sorted(audio_rows, key=lambda r: r[1].time)
    webcam_sorted = sorted(webcam_rows, key=lambda r: r[1].time)
    for a in acts:
        heard_hits = _in_window(audio_sorted, a.start, a.end)
        saw_hits = _in_window(webcam_sorted, a.start, a.end)
        a.n_audio = len(heard_hits)
        a.n_webcam = len(saw_hits)
        a.ctx_event_ids = [int(eid) for eid, _ in heard_hits + saw_hits]
        heard: list[str] = []
        for _, ev in heard_hits:
            s = _heard_snippet(ev)
            if s and s.lower() not in {h.lower() for h in heard}:
                heard.append(s)
            if len(heard) >= max_heard:
                break
        saw: list[str] = []
        for _, ev in saw_hits:
            s = _saw_snippet(ev)
            if s and s.lower() not in {x.lower() for x in saw}:
                saw.append(s)
            if len(saw) >= max_saw:
                break
        extra: list[str] = []
        if heard:
            extra.append("heard: " + "; ".join(heard))
        if saw:
            extra.append("saw: " + "; ".join(saw))
        if extra:
            base = a.summary.rstrip(". ") if a.summary else a.app
            a.summary = f"{base}. {'. '.join(extra)}"[:_MAX_SUMMARY_CHARS]
    return acts


def _polish_closed(acts: list[Activity], *, now: float | None = None) -> None:
    """Optional LLM pass (QUILL_ACTIVITY_SUMMARIZE=1): compress the heuristic
    summary of CLOSED activities only — blocks whose end is older than the
    activity gap, i.e. blocks that can no longer grow, so the spend is never
    repeated for the same content growing. One call per activity, best-effort:
    any failure keeps the heuristic summary (the MVP) and disables the rest of
    the pass. The default path (flag off) makes ZERO LLM calls."""
    now = _time.time() if now is None else now
    gap = settings.consolidation.activity_gap_s
    try:
        from app.services.model_router import router
    except Exception as exc:
        print(f"[activity] summarize unavailable ({exc}); keeping heuristics.")
        return
    system = (
        "You compress desktop activity log entries. Rewrite the entry as one "
        "or two short plain sentences (max 350 characters) describing what the "
        "person was doing — keep the application name, the gist of the work, "
        "and any heard/saw context that adds meaning. Output the rewritten "
        "summary only, no preamble."
    )
    for a in acts:
        if (now - a.end) <= gap or not a.summary:
            continue  # still open (could grow) or nothing to compress
        try:
            text = router.complete(
                "activity_summarize", system=system,
                messages=[{"role": "user", "content": a.summary}],
                max_tokens=300,
            ).strip()
            if text:
                a.summary = text[:_MAX_SUMMARY_CHARS]
        except Exception as exc:
            print(f"[activity] summarize skipped ({exc}).")
            return  # one failure likely means all will fail — stop spending


def rebuild(store: Store | None = None) -> int:
    """Recompute the activities table from every desktop event, enriched with
    co-timed audio transcripts and webcam vision events. Returns count."""
    store = store or get_store()
    rows = store.all_with_ids()
    desktop = [(eid, ev) for eid, ev in rows if ev.source in SOURCES]
    acts = group_activities(desktop, settings.consolidation.activity_gap_s)
    # Context selection is by modality, not brittle source strings: transcripts
    # are AUDIO ('audio.whisper', plus transcript-less 'audio.skipped'); webcam
    # frames are VISION from a non-desktop source ('vision.claude') — desktop
    # screens are also VISION, so they're excluded by source.
    audio_rows = [(eid, ev) for eid, ev in rows if ev.modality == Modality.AUDIO]
    webcam_rows = [(eid, ev) for eid, ev in rows
                   if ev.modality == Modality.VISION and ev.source not in SOURCES]
    try:
        join_context(acts, audio_rows, webcam_rows)
    except Exception as exc:  # enrichment must never break the desktop rollup
        print(f"[activity] context join skipped ({exc}).")
    if settings.consolidation.activity_summarize:
        _polish_closed(acts)
    store.replace_activities(acts)
    # After a fresh rollup, maybe surface a likely-next offer (no-op unless
    # QUILL_ANTICIPATE=1 and the newest block looks idle).
    try:
        from app.services import anticipation
        anticipation.consider(store)
    except Exception as exc:
        print(f"[activity] anticipate skipped ({exc}).")
    return len(acts)


def _fmt_range(start: float, end: float) -> str:
    try:
        s = _time.strftime("%b %d, %I:%M %p", _time.localtime(start)).replace(" 0", " ")
        e = _time.strftime("%I:%M %p", _time.localtime(end)).lstrip("0")
        return f"{s}–{e}"
    except (ValueError, TypeError, OSError):
        return ""


def describe_recent(store: Store | None = None, limit: int = 6) -> list[str]:
    """The last few activities as human-readable, time-anchored lines — the
    chat grounding block that lets "what was I working on before lunch?" be
    answered from real observation instead of a guess."""
    store = store or get_store()
    lines: list[str] = []
    for a in store.recent_activities(limit):
        when = _fmt_range(a.get("start") or 0, a.get("end") or 0)
        mins = max(1, round((a.get("duration_s") or 0) / 60))
        lines.append(f"[{when}, ~{mins} min] {a.get('summary') or a.get('app', '')}")
    return lines
