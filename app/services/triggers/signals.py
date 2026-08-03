"""Signal catalog — the derived moments standing triggers subscribe to.

Triggers never match raw percepts (frames, audio chunks); they match a small
vocabulary of DERIVED events the system already computes elsewhere — the same
"one calm scan pass" posture as the Track D reasoners. `scan()` re-derives the
recent window on each engine tick, so nothing here hooks the hot ingest paths.

Every Signal carries provenance-aware `ambient`: True when the moment was
observed in content that ambient/external sources produced (screen frames,
phone notifications, documents, peer messages) rather than the user's own
speech/typing. The engine uses it as an injection rail — an ambient signal can
never carry a trigger past the offer band (see triggers/__init__.py).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# name -> human description (the authoring compiler + console read this).
CATALOG = {
    "task_done": "a task or commitment was just completed",
    "progress_on": "work tied to a project/entity moved forward",
    "commitment_due": "an open commitment is overdue or at risk",
    "dropped_thread": "an open thread has gone quiet",
    "app_session_ended": "a desktop app session just ended",
}

# Event sources whose CONTENT is authored by the outside world (or by ambient
# capture of it). Signals derived from them are marked ambient.
AMBIENT_SOURCE_PREFIXES = ("desktop.screen", "phone.", "documents.", "peer.")


def is_ambient_source(source: str | None) -> bool:
    s = (source or "").strip().lower()
    return bool(s) and s.startswith(AMBIENT_SOURCE_PREFIXES)


@dataclass
class Signal:
    name: str                     # one of CATALOG
    ts: float
    text: str                     # short human line ("Done: send the deck")
    entity: str | None = None
    person: str | None = None
    fact_id: int | None = None
    app: str | None = None
    confidence: float = 0.8
    ambient: bool = False
    payload: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        """Dedup identity (no timestamp — cooldowns own the rate limit)."""
        return "|".join([self.name, (self.entity or "").lower(),
                         (self.person or "").lower(),
                         str(self.fact_id or ""), (self.app or "").lower()])


def _recent_fact_signals(store, now: float, window_s: float) -> list[Signal]:
    """task_done + the per-entity progress_on rollup from recently moved facts."""
    cutoff = now - window_s
    try:
        facts = store.list_facts(limit=500)
    except Exception:
        return []
    done, touched = [], []
    for f in facts:
        if f.get("kind") not in ("task", "commitment"):
            continue
        ts = float(f.get("updated_at") or f.get("extracted_at") or 0)
        if ts < cutoff:
            continue
        if (f.get("status") or "") == "done":
            done.append((f, ts))
        elif (f.get("status") or "") == "open" and f.get("updated_at") and \
                float(f["updated_at"]) > float(f.get("extracted_at") or 0):
            touched.append((f, ts))  # re-asserted open work (touch_fact path)

    ent_map = store.fact_entities(
        [f.get("fact_id") for f, _ in done + touched])

    out: list[Signal] = []
    for f, ts in done:
        fid = f.get("fact_id")
        ents = ent_map.get(int(fid)) if fid else None
        out.append(Signal(
            name="task_done", ts=ts,
            text=f"Done: {(f.get('text') or '')[:120]}",
            entity=(ents[0] if ents else None),
            person=f.get("owner") or f.get("to_person"),
            fact_id=int(fid) if fid else None,
            confidence=min(0.95, float(f.get("confidence") or 0.7) + 0.1),
            ambient=is_ambient_source(f.get("event_source")),
            payload={"kind": f.get("kind")}))

    # progress_on: entity-level rollup — ≥1 completion or ≥2 touches counts.
    by_entity: dict[str, dict[str, Any]] = {}
    for bucket, rows in (("done", done), ("touched", touched)):
        for f, ts in rows:
            fid = f.get("fact_id")
            for ent in (ent_map.get(int(fid)) if fid else None) or []:
                g = by_entity.setdefault(
                    ent, {"done": 0, "touched": 0, "ts": ts,
                          "ambient": True, "fact_ids": []})
                g[bucket] += 1
                g["ts"] = max(g["ts"], ts)
                g["fact_ids"].append(fid)
                if not is_ambient_source(f.get("event_source")):
                    g["ambient"] = False  # any first-party evidence de-ambients
    for ent, g in by_entity.items():
        if g["done"] < 1 and g["touched"] < 2:
            continue
        n = g["done"] + g["touched"]
        out.append(Signal(
            name="progress_on", ts=g["ts"],
            text=(f"Progress on {ent}: {g['done']} done, "
                  f"{g['touched']} moved"),
            entity=ent,
            confidence=min(0.95, 0.7 + 0.05 * n),
            ambient=g["ambient"],
            payload={"done": g["done"], "touched": g["touched"],
                     "fact_ids": g["fact_ids"][:12]}))
    return out


def _meta_memory_signals(store, now: float) -> list[Signal]:
    """commitment_due + dropped_thread — straight reuse of the meta-memory
    scans the commitment reasoner already runs (no second formula)."""
    out: list[Signal] = []
    try:
        from app.services import meta_memory
        risks = meta_memory.scan_at_risk(store, now=now)
        dropped = meta_memory.scan_dropped_threads(store, now=now)
    except Exception:
        return []
    for r in risks[:12]:
        text = (r.get("text") or "").strip()
        if not text:
            continue
        out.append(Signal(
            name="commitment_due", ts=now,
            text=f"At risk: {text[:120]}",
            person=(r.get("subject") or "").strip() or None,
            fact_id=r.get("fact_id"),
            confidence=min(0.95, 0.6 + float(r.get("risk") or 0.5) * 0.3),
            payload={"why": list(r.get("why") or [])[:4]}))
    for d in dropped[:8]:
        text = (d.get("text") or "").strip()
        if not text:
            continue
        out.append(Signal(
            name="dropped_thread", ts=now,
            text=f"Gone quiet: {text[:120]}",
            person=(d.get("subject") or "").strip() or None,
            fact_id=d.get("fact_id"),
            confidence=0.7,
            payload={"quiet_days": d.get("quiet_days")}))
    return out


def _activity_signals(store, now: float, window_s: float) -> list[Signal]:
    """app_session_ended for activity blocks that closed inside the window."""
    try:
        acts = store.recent_activities(limit=10)
    except Exception:
        return []
    out: list[Signal] = []
    for a in acts:
        app = (a.get("app") or "").strip()
        end = float(a.get("end") or 0)
        if not app or app.lower() == "desktop":
            continue
        if end < now - window_s or end > now:
            continue
        out.append(Signal(
            name="app_session_ended", ts=end,
            text=f"Finished a {app} session",
            app=app, confidence=0.85,
            payload={"summary": (a.get("summary") or "")[:160],
                     "start": a.get("start")}))
    return out


def scan(store, *, now: float | None = None,
         window_s: float = 3600.0) -> list[Signal]:
    """Derive the recent window's signals. Read-only, never raises."""
    now = float(now if now is not None else time.time())
    out: list[Signal] = []
    for fn in (_recent_fact_signals, _meta_memory_signals, _activity_signals):
        try:
            if fn is _meta_memory_signals:
                out.extend(fn(store, now))
            else:
                out.extend(fn(store, now, window_s))
        except Exception as exc:
            print(f"[triggers] signal scan {fn.__name__} skipped ({exc}).")
    return out
