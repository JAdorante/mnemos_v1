"""CAL Stage 2 — replay: run the segmenter over captured history.

The segmenter is a pure function over an ordered stream, so replaying a day
that already happened and consuming the live bus are the same code fed
differently. That is not a testing convenience — it is the only way to get
design feedback on segmentation today rather than after another week of
capture, and it makes a real day into a re-runnable fixture: change a
threshold, re-segment eleven hours in a second, compare.

    python -m app.services.context.replay --day 2026-08-26
    python -m app.services.context.replay --day 2026-08-26 --persist
"""
from __future__ import annotations

import argparse
import datetime as dt
import json

from app.services.context import episodes as ep_build
from app.services.context import keys as ckeys
from app.services.context import surfaces as sf
from app.services.context.frames import Anchor, Segmenter, StreamEvent


def anchors_for(raw: str, meta: dict, index: sf.SurfaceIndex) -> tuple[Anchor, ...]:
    """Every anchor an event carries — surfaces first, then binding keys.

    Both paths feed the same field. A window title naming a known person and a
    git remote naming a repo are different KINDS of evidence, graded
    differently, but they compete for the same attention.
    """
    out: list[Anchor] = []
    title = str(meta.get("window") or "")
    if title:
        for hit in index.from_title(title):
            out.append(Anchor(hit.node_type, hit.node_id, hit.name,
                              hit.strength, "medium", hit.nameable))
    burl = meta.get("browser_url") or (
        f"https://{meta['url_domain']}" if meta.get("url_domain") else None)
    try:
        from app.perception import identifiers as idents
        mined = meta.get("identifiers")
        if not mined:
            mined = idents.extract_identifiers(raw or "", window=title,
                                               browser_url=burl)
    except Exception:
        mined = []
    for sk in ckeys.from_identifiers(mined):
        if sk.tier == ckeys.SUPPORTING:
            continue
        out.append(Anchor("key", sk.key, sk.key_value, sk.strength, sk.tier,
                          sk.nameable))
    return tuple(out)


def stream_for(store, t0: float, t1: float) -> list[StreamEvent]:
    """Captured events in [t0, t1) as segmenter input, oldest first."""
    index = sf.SurfaceIndex(store)
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, time, raw, summary, meta FROM events "
            "WHERE time >= ? AND time < ? ORDER BY time ASC",
            (float(t0), float(t1))).fetchall()
    from app.services.activity import app_of
    out: list[StreamEvent] = []
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        title = str(meta.get("window") or "")
        out.append(StreamEvent(
            event_id=int(r["id"]), t=float(r["time"]),
            anchors=anchors_for(r["raw"] or "", meta, index),
            app=(app_of(title) if title else "") or str(meta.get("app_name") or ""),
            title=title))
    return out


def replay(store, *, t0: float, t1: float, run_id: str = "replay",
           persist: bool = False, **seg_kw) -> dict:
    evs = stream_for(store, t0, t1)
    seg = Segmenter(**seg_kw)
    places = []
    for e in evs:
        places.append(seg.feed(e))
    seg.close()
    eps = ep_build.build(seg, places, run_id=run_id)
    if persist:
        store.clear_context_run(run_id)
        store.save_context_frames(seg.frames, run_id)
        for e in eps:
            store.save_episode({k: v for k, v in e.items()
                                if not k.startswith("_")},
                               e.get("_event_ids"))
    return {"events": len(evs), "frames": len(seg.frames),
            "episodes": eps, "segmenter": seg}


def _hhmm(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%H:%M")


def timeline(eps: list[dict]) -> str:
    """The artifact. Blanks stay blank — an honest unknown beats a wrong label."""
    lines = []
    for e in eps:
        mins = max(0.0, (e["ended_at"] - e["started_at"])) / 60.0
        anchored = e["n_events"] - e["n_inherited"]
        label = e["title"] if e["node_type"] else f"— ({e['title']})"
        kind = f"  [{e['kind']}]" if e.get("kind") else ""
        lines.append(
            f"  {_hhmm(e['started_at'])}–{_hhmm(e['ended_at'])}  "
            f"{mins:5.1f}m  {label:<34}{kind}"
            f"   {e['n_events']:>3} ev ({anchored} anchored, "
            f"coh {e['coherence']:.2f})")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Replay a captured day through CAL")
    ap.add_argument("--day", help="YYYY-MM-DD (local)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--persist", action="store_true")
    a = ap.parse_args(argv)
    from app.storage import get_store
    store = get_store()
    if a.day:
        d = dt.datetime.strptime(a.day, "%Y-%m-%d")
        t0 = d.timestamp()
        t1 = t0 + 86400
    else:
        t1 = dt.datetime.now().timestamp()
        t0 = t1 - 86400
    run_id = a.run_id or f"replay:{a.day or 'last24h'}"
    res = replay(store, t0=t0, t1=t1, run_id=run_id, persist=a.persist)
    eps = res["episodes"]
    total = sum(e["n_events"] for e in eps)
    named = [e for e in eps if e["node_type"]]
    span = sum(e["ended_at"] - e["started_at"] for e in eps)
    nspan = sum(e["ended_at"] - e["started_at"] for e in named)
    print(f"\n{a.day or 'last 24h'} — {res['events']} events, "
          f"{res['frames']} frames, {len(eps)} episodes\n")
    print(timeline(eps))
    print(f"\n  events in episodes : {total}/{res['events']}")
    print(f"  episodes named     : {len(named)}/{len(eps)}")
    print(f"  attributable time  : {nspan/60:.0f} / {span/60:.0f} min "
          f"({100.0*nspan/span if span else 0:.0f}%)")
    if a.persist:
        print(f"  persisted under run_id={run_id!r}")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
