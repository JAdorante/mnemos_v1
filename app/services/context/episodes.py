"""CAL Stage 2 — episodes: the stretch of stream a frame explains.

An episode is a ROOT frame plus everything nested under it. Interruptions fold
in rather than splitting: a glance at mail during an hour of work is part of
that hour, and a timeline that says otherwise is describing the capture system
rather than the day.

Deliberately no model anywhere in here. The design's own warning is that
summarizing before coverage is understood produces confidently wrong prose you
cannot debug — so episodes carry a title drawn from their anchor, a kind drawn
from observed apps, and an honest blank when neither is known. Summaries are a
later layer, added where anchor density earns them.
"""
from __future__ import annotations

from dataclasses import replace

# App -> episode kind. A placeholder for the `fast_heads` classifier the design
# specifies; rules are honest about being rules, and return nothing rather than
# guessing when the evidence is thin.
_KIND_BY_APP = {
    "code": "build", "cursor": "build", "visual studio code": "build",
    "vscode": "build", "pycharm": "build", "intellij": "build",
    "sublime text": "build", "vim": "build", "neovim": "build",
    "terminal": "build", "iterm2": "build", "windows terminal": "build",
    "gnome-terminal": "build", "konsole": "build", "powershell": "build",
    "outlook": "comms", "mail": "comms", "thunderbird": "comms",
    "gmail": "comms", "slack": "comms", "discord": "comms",
    "microsoft teams": "comms", "messages": "comms",
    "zoom": "meeting", "google meet": "meeting", "webex": "meeting",
    "chrome": "research", "chromium": "research", "firefox": "research",
    "safari": "research", "mozilla firefox": "research", "edge": "research",
    "arc": "research", "brave": "research",
    "excel": "admin", "numbers": "admin", "quickbooks": "admin",
    "system settings": "admin", "settings": "admin",
    "figma": "design", "canva": "design", "sketch": "design",
}
_MIN_EVENTS = 2          # a single stray event is not an episode
MIN_OWN_EVIDENCE = 2     # ...and one sighting does not name one


def kind_for(apps: dict) -> str | None:
    """Episode kind from observed app share, or None when it isn't clear."""
    if not apps:
        return None
    tally: dict[str, int] = {}
    for app, n in apps.items():
        k = _KIND_BY_APP.get(str(app or "").strip().lower())
        if k:
            tally[k] = tally.get(k, 0) + int(n)
    if not tally:
        return None
    total = sum(tally.values())
    kind, n = max(tally.items(), key=lambda kv: kv[1])
    # A plurality is not a description. If the day was half mail and half code,
    # saying "comms" is worse than saying nothing.
    return kind if n / total >= 0.6 else None


def title_for(frame, apps: dict) -> str:
    """What to call this stretch. An unbound frame says so."""
    if frame.name:
        return frame.name
    if frame.key:
        return f"{frame.key[0]}:{frame.key[1]}"
    if apps:
        return max(apps.items(), key=lambda kv: kv[1])[0]
    return "unbound"


def build(segmenter, placements, *, run_id: str = "") -> list[dict]:
    """Frames + placements -> episode dicts, ready for `Store.save_episode`.

    Children fold into their root, so `episode_events` carries every event of
    the stretch including the ones that only inherited — which is most of them,
    and the reason a per-event coverage bar was the wrong measure.
    """
    frames = {f.id: f for f in segmenter.frames}

    def root_of(fid):
        """Walk to the owning root, refusing to fold a child into a stretch
        that does not contain it.

        Defensive: the segmenter promotes orphans, but a parent whose span has
        already ended cannot own later events, and silently folding them there
        is how an eighteen-minute episode ends up reporting two hundred.
        """
        seen = set()
        while fid in frames and frames[fid].parent_id is not None:
            if fid in seen:
                break
            child, parent = frames[fid], frames.get(frames[fid].parent_id)
            if parent is None:
                break
            p_end = parent.ended_at if parent.ended_at is not None \
                else parent.last_evidence_at
            if p_end is not None and child.started_at > p_end:
                break
            seen.add(fid)
            fid = parent.id
        return fid

    events_by_root: dict[int, list[tuple[int, bool]]] = {}
    for p in placements:
        if p.frame_id is None:
            continue
        events_by_root.setdefault(root_of(p.frame_id), []).append(
            (p.event_id, p.inherited))

    out: list[dict] = []
    for f in segmenter.frames:
        if f.parent_id is not None:
            continue
        evs = events_by_root.get(f.id, [])
        if len(evs) < _MIN_EVENTS:
            continue
        apps: dict[str, int] = dict(f.apps)
        for child in segmenter.frames:
            if child.parent_id is not None and root_of(child.id) == f.id:
                for a, n in (child.apps or {}).items():
                    apps[a] = apps.get(a, 0) + n
        n_inherited = sum(1 for _e, inh in evs if inh)
        # A frame whose own anchor was never re-evidenced is a guess, not a
        # stretch of work. One sighting named a whole episode after a path
        # mined out of prose; two is the cheapest bar that refuses it.
        own = len(evs) - n_inherited
        named = f.key is not None and own >= MIN_OWN_EVIDENCE
        # Coherence over the FOLDED stretch, not the root frame alone. The
        # frame-level ratio ignores child events, so an episode could report
        # forty anchored events beside a coherence of zero.
        coherence = (len(evs) - n_inherited) / len(evs) if evs else 0.0
        out.append({
            "run_id": run_id,
            "frame_seg_id": f.id,
            "node_type": f.key[0] if named else None,
            "node_id": str(f.key[1]) if named else None,
            "title": title_for(f, apps) if named else title_for(
                replace(f, key=None, name=""), apps),
            "kind": kind_for(apps),
            "started_at": f.started_at,
            "ended_at": f.ended_at if f.ended_at is not None else f.last_evidence_at,
            "state": "closed" if f.ended_at is not None else "open",
            "n_events": len(evs),
            "n_inherited": n_inherited,
            "coherence": coherence,
            "apps": apps,
            "_event_ids": evs,
        })
    out.sort(key=lambda e: e["started_at"])
    return out


__all__ = ["build", "kind_for", "title_for"]
