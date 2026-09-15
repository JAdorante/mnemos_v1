"""CAL Stage 5 — the measurement harness everything else is gated on.

The design puts learned segmentation last and conditions it explicitly: only
after boundary F1 has a stable baseline to beat. There is no baseline, because
there is no labelled corpus — so this is the prerequisite, not the research.

It is also where §14's metrics become real numbers instead of aspirations. Two
of them deserve saying out loud:

  unbound rate      5–15% is the target, and ZERO is a failure, not a win. A
                    system that always produces a label is overconfident, and
                    its precision number is measuring its own nerve.

  correction        the metric that separates a learning system from one with a
  persistence       feedback button. If a user fixes an attribution and the
                    same class of error recurs next week, the correction wrote
                    an edge where it should have written a BINDING.

Everything here is deterministic. Nothing calls a model, and nothing writes to
the graph — an evaluator that mutates what it measures is not one.

The loop, end to end:

    python -m app.services.context.evaluate sheet --day 2026-08-26
        -> data/cal_eval/2026-08-26.csv, one row per event, with the system's
           own prediction beside two blank columns for a human to fill.
    (edit the CSV: type the project, or "=" to accept the prediction; "1" or
     "=" in label_boundary where a new stretch of work really starts — or
     label whole stretches from the terminal:)
    python -m app.services.context.evaluate fill --labels data/cal_eval/2026-08-26.csv \
        --from 10:31 --to 12:02 --project Boostrun
    python -m app.services.context.evaluate score --labels data/cal_eval/2026-08-26.csv
        -> boundary F1, attribution P/R, unbound rate, and the confusions.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from collections import Counter
from pathlib import Path

DEFAULT_TOLERANCE_S = 120.0
ACCEPT = "="            # in a label column: "the prediction is right"
SHEET_DIR = Path("data/cal_eval")

SHEET_COLUMNS = ("event_id", "time", "hhmm", "modality", "source", "window",
                 "text", "predicted_project", "predicted_boundary",
                 "label_project", "label_boundary")


def _norm(label) -> str | None:
    """Labels are typed by a person and titled by a machine; neither should
    lose a match to case or a trailing space."""
    if label is None:
        return None
    s = str(label).strip().casefold()
    return s or None


# --- boundary segmentation (§14, "Episode boundary F1") ----------------------
def match_boundaries(predicted, truth, *, tolerance_s: float = DEFAULT_TOLERANCE_S):
    """Greedy nearest-match of predicted boundaries to true ones.

    A boundary is right if it lands within `tolerance_s` of a real one — human
    labellers do not agree to the second, and scoring exact equality would
    measure transcription accuracy rather than segmentation. Each true boundary
    can be claimed once, so duplicating a boundary cannot inflate recall.
    """
    truth = sorted(float(t) for t in truth or [])
    used = set()
    pairs = []
    for p in sorted(float(x) for x in predicted or []):
        best, best_d = None, None
        for i, t in enumerate(truth):
            if i in used:
                continue
            d = abs(p - t)
            if d <= tolerance_s and (best_d is None or d < best_d):
                best, best_d = i, d
        if best is not None:
            used.add(best)
            pairs.append((p, truth[best], best_d))
        else:
            pairs.append((p, None, None))
    return pairs, used


def boundary_f1(predicted, truth, *, tolerance_s: float = DEFAULT_TOLERANCE_S) -> dict:
    """Precision / recall / F1 over episode boundaries."""
    pairs, used = match_boundaries(predicted, truth, tolerance_s=tolerance_s)
    tp = len(used)
    fp = sum(1 for _p, t, _d in pairs if t is None)
    fn = max(0, len(list(truth or [])) - tp)
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    rec = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (2 * prec * rec / (prec + rec)) if (prec + rec) else 0.0
    offs = [d for _p, _t, d in pairs if d is not None]
    return {"precision": prec, "recall": rec, "f1": f1,
            "tp": tp, "fp": fp, "fn": fn,
            "median_offset_s": (sorted(offs)[len(offs) // 2] if offs else None)}


# --- attribution (§14, precision / recall / unbound rate) --------------------
def attribution(predicted: dict, truth: dict) -> dict:
    """Per-event attribution quality.

    Both maps are event_id → label, where a label of None means "unbound".
    Recall is over ATTRIBUTABLE events — the ones a human could label — because
    scoring a system for failing to name something unnameable measures the
    corpus, not the system.
    """
    truth = {e: _norm(v) for e, v in (truth or {}).items()}
    predicted = {e: _norm(v) for e, v in (predicted or {}).items()}
    ids = set(truth)
    attributable = {e for e in ids if truth.get(e) is not None}
    attributed = {e for e in ids if predicted.get(e) is not None}
    correct = {e for e in attributed & attributable
               if predicted[e] == truth[e]}
    wrong = attributed - correct
    prec = len(correct) / len(attributed) if attributed else 0.0
    rec = len(correct) / len(attributable) if attributable else 0.0
    unbound = (len(ids) - len(attributed)) / len(ids) if ids else 0.0
    return {"precision": prec, "recall": rec,
            "unbound_rate": unbound,
            "n_events": len(ids), "n_attributable": len(attributable),
            "n_attributed": len(attributed), "n_correct": len(correct),
            "n_wrong": len(wrong),
            # Zero is not a win. An honest unknown is a better product than a
            # wrong label, and a 0% unbound rate means the bands are too loose.
            "unbound_healthy": 0.05 <= unbound <= 0.15}


def confusions(predicted: dict, truth: dict, *, top: int = 10) -> list:
    """The wrong attributions, grouped: (predicted, labelled, count).

    A precision number says how often the layer is wrong; this says HOW — a
    person's name swallowing a project is a binding problem, a project named
    for a stretch that was really unbound is a band problem.
    """
    c: Counter = Counter()
    for e, want in (truth or {}).items():
        got = (predicted or {}).get(e)
        if _norm(got) == _norm(want):
            continue
        c[(got if got is not None else "—", want if want is not None else "—")] += 1
    return [(p, t, n) for (p, t), n in c.most_common(top)]


def model_calls_per_1000(n_calls: int, n_events: int) -> float:
    """The compounding metric. Flat over weeks means the layer is not working,
    whatever the accuracy says."""
    return (1000.0 * float(n_calls) / n_events) if n_events else 0.0


def correction_persistence(corrections) -> dict:
    """Share of corrections that prevented the SAME class of error recurring.

    `corrections` is an iterable of (error_class, corrected_at, recurred_after)
    where `recurred_after` is a timestamp or None. A correction that wrote an
    edge instead of a binding shows up here and nowhere else.
    """
    rows = list(corrections or [])
    if not rows:
        return {"n": 0, "persistence": None}
    held = sum(1 for _c, _at, again in rows if again is None)
    return {"n": len(rows), "persistence": held / len(rows),
            "recurred": len(rows) - held}


# --- predictions, from a replay --------------------------------------------
def predictions_from(episodes) -> tuple[dict, set]:
    """What a replay claims: (event_id → label or None, {event_ids that open
    an episode}).

    `_event_ids` is in stream order, so the first id of an episode is the
    event at which the segmenter said a new stretch began.
    """
    predicted: dict = {}
    starts: set = set()
    for e in episodes or []:
        label = e.get("title") if e.get("node_type") else None
        ids = [int(eid) for eid, _inherited in (e.get("_event_ids") or [])]
        for eid in ids:
            predicted[eid] = label
        if ids:
            starts.add(ids[0])
    return predicted, starts


# --- the labelling sheet -----------------------------------------------------
def labelling_sheet(store, *, t0: float, t1: float, limit: int = 2000,
                    episodes=None) -> list:
    """Emit one day of captured events for a human to label, ready to edit.

    The design asks for one real day hand-labelled with project and boundary —
    an afternoon, and the difference between shipping on measurement and
    shipping on vibes. This produces the sheet so that afternoon is clicking
    rather than typing: pass a replay's `episodes` and each row carries the
    system's own claim beside the blank the human fills. That is a bias — a
    labeller who sees the prediction agrees with it more than one who does
    not — and it is the trade the design makes explicitly. Label blind by
    leaving `episodes` out.
    """
    predicted, starts = predictions_from(episodes) if episodes else ({}, set())
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, time, modality, source, summary, raw, meta FROM events "
            "WHERE time >= ? AND time < ? ORDER BY time ASC LIMIT ?",
            (float(t0), float(t1), int(limit))).fetchall()
    out = []
    for r in rows:
        try:
            meta = json.loads(r["meta"] or "{}")
        except Exception:
            meta = {}
        text = (r["summary"] or r["raw"] or "")[:160].replace("\n", " ")
        eid = int(r["id"])
        out.append({
            "event_id": eid, "time": float(r["time"]),
            "hhmm": dt.datetime.fromtimestamp(float(r["time"])).strftime("%H:%M:%S"),
            "modality": r["modality"], "source": r["source"],
            "window": str(meta.get("window") or meta.get("display_label") or ""),
            "text": text,
            "predicted_project": predicted.get(eid) or "",
            "predicted_boundary": "1" if eid in starts else "",
            "label_project": "",      # <- human fills these two ("=" accepts)
            "label_boundary": "",     # <- "1" if a new episode starts here
        })
    return out


def write_sheet(rows, path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=SHEET_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in SHEET_COLUMNS})
    return path


def read_sheet(path) -> list:
    """A filled sheet back as rows; a spreadsheet round-trip must not turn an
    id into a string or a timestamp into '1.7e9'."""
    out = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not (r.get("event_id") or "").strip():
                continue
            r["event_id"] = int(float(r["event_id"]))
            r["time"] = float(r["time"])
            out.append(r)
    return out


def load_labels(rows) -> tuple[dict, list]:
    """Split a filled sheet into (event_id → project, [boundary timestamps]).

    "=" in a label column accepts the prediction beside it, so agreeing with
    the system is one keystroke. A blank project means "no project" — an
    unfinished sheet scores as if its blanks were unattributable, so finish it.
    """
    truth: dict = {}
    boundaries: list = []
    for r in rows or []:
        eid = int(r.get("event_id"))
        proj = (r.get("label_project") or "").strip()
        if proj == ACCEPT:
            proj = (r.get("predicted_project") or "").strip()
        truth[eid] = proj or None
        b = str(r.get("label_boundary") or "").strip()
        if b == ACCEPT:
            b = str(r.get("predicted_boundary") or "").strip()
        if b.lower() in ("1", "true", "yes"):
            boundaries.append(float(r.get("time")))
    return truth, boundaries


def score_run(episodes, truth: dict, boundaries, *,
              tolerance_s: float = DEFAULT_TOLERANCE_S) -> dict:
    """Grade a replay's episodes against a labelled day.

    `episodes` are dicts as `episodes.build` emits (they carry `_event_ids`).
    """
    predicted_boundaries = [float(e["started_at"]) for e in episodes or []]
    predicted, _starts = predictions_from(episodes)
    return {"boundaries": boundary_f1(predicted_boundaries, boundaries,
                                      tolerance_s=tolerance_s),
            "attribution": attribution(predicted, truth),
            "confusions": confusions(predicted, truth)}


# --- the loop, as commands ---------------------------------------------------
def _day_bounds(day: str | None, rows=None) -> tuple[float, float, str]:
    if day:
        d = dt.datetime.strptime(day, "%Y-%m-%d")
    elif rows:
        d = dt.datetime.fromtimestamp(min(float(r["time"]) for r in rows))
        d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        raise SystemExit("--day is required when there is no sheet to read it from")
    t0 = d.timestamp()
    return t0, t0 + 86400, d.strftime("%Y-%m-%d")


def _open_store(db: str | None, *, readonly: bool = True):
    # Measurement against a :ro pilot volume must not CREATE / PRAGMA-write.
    from app.storage import Store
    path = Path(db) if db else Path(
        __import__("app.config", fromlist=["settings"]).settings.storage.db_path)
    return Store(db_path=path, readonly=readonly)


def sheet_for_day(store, day: str, *, blind: bool = False) -> list:
    from app.services.context import replay as rp
    t0, t1, _ = _day_bounds(day)
    eps = None if blind else rp.replay(store, t0=t0, t1=t1,
                                       run_id=f"eval:{day}")["episodes"]
    return labelling_sheet(store, t0=t0, t1=t1, episodes=eps)


def score_labels(store, rows, *, day: str | None = None,
                 tolerance_s: float = DEFAULT_TOLERANCE_S,
                 escalate: bool = False, ask=None,
                 min_confidence: float = 0.5) -> dict:
    from app.services.context import replay as rp
    t0, t1, day = _day_bounds(day, rows)
    res = rp.replay(store, t0=t0, t1=t1, run_id=f"eval:{day}")
    truth, boundaries = load_labels(rows)
    escalations = None
    if escalate:
        # The model reader, over what the cheap path left blank. In memory:
        # the episodes are renamed for the scorer and nothing is written.
        from app.services.context import naming
        escalations = naming.name_unbound(store, res["episodes"], ask=ask,
                                          min_confidence=min_confidence)
    out = score_run(res["episodes"], truth, boundaries, tolerance_s=tolerance_s)
    if escalations is not None:
        # Grade each escalation against the labelled majority of its stretch.
        for rec in escalations:
            ep = next((e for e in res["episodes"]
                       if e.get("frame_seg_id") == rec["frame_seg_id"]), None)
            votes = Counter(truth.get(int(eid)) for eid, _i in
                            (ep.get("_event_ids") if ep else []) or [])
            want, _n = (votes.most_common(1) or [(None, 0)])[0]
            rec["labelled"] = want
            rec["correct"] = (_norm(rec["choice"]) == _norm(want)
                              if rec["applied"] else None)
        out["escalations"] = escalations
        # Two calls per offered episode (both orders), none when nothing in
        # the text named a project.
        out["model_calls"] = sum(
            (2 if len(r["candidates"]) > 1 else 1) for r in escalations
            if r["error"] != "no_candidates")
    out["day"] = day
    out["n_events"] = res["events"]
    out["n_episodes"] = len(res["episodes"])
    out["n_rows"] = len(rows)
    out["n_blank"] = sum(1 for r in rows
                         if not (r.get("label_project") or "").strip())
    out["n_true_boundaries"] = len(boundaries)
    return out


def unlabelled(s: dict) -> bool:
    """A sheet nobody has touched grades the system against nothing — every
    prediction is a false positive and the unbound rate looks healthy by
    accident. Say so instead of printing zeros."""
    return s["n_rows"] == 0 or (s["n_blank"] == s["n_rows"]
                                and s["n_true_boundaries"] == 0)


def fill(rows, *, start: str, end: str, project: str | None = None,
         accept: bool = False, boundary: bool = True) -> int:
    """Label a stretch of the day at once: every row whose local clock time
    is in [start, end) gets `project` (or "=" with `accept`), and the first
    row of the stretch gets the boundary. A day is a dozen stretches, not
    three hundred rows — this is how a terminal user labels one in minutes.

    Times are "HH:MM" or "HH:MM:SS". An empty project clears the stretch.
    """
    def _secs(t: str) -> int:
        parts = [int(x) for x in t.strip().split(":")]
        while len(parts) < 3:
            parts.append(0)
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    lo, hi = _secs(start), _secs(end)
    label = ACCEPT if accept else (project or "").strip()
    n = 0
    first = True
    for r in rows:
        t = _secs(r["hhmm"]) if r.get("hhmm") else int(
            dt.datetime.fromtimestamp(float(r["time"])).strftime("%H")) * 3600
        if not (lo <= t < hi):
            continue
        r["label_project"] = label
        r["label_boundary"] = "1" if (boundary and first) else ""
        first = False
        n += 1
    return n


def report(s: dict) -> str:
    if unlabelled(s):
        return (f"\n{s['day']} — {s['n_rows']} rows, none labelled yet.\n"
                f"  Fill `label_project` (or `=` to accept the prediction) and "
                f"`label_boundary` (`1` or `=`) in the sheet, then score again.")
    b, a = s["boundaries"], s["attribution"]
    off = (f"{b['median_offset_s']:.0f}s" if b["median_offset_s"] is not None
           else "n/a")
    lines = [
        f"\n{s['day']} — {s['n_events']} events, {s['n_episodes']} episodes, "
        f"{s['n_rows']} labelled rows ({s['n_blank']} blank project labels, "
        f"{s['n_true_boundaries']} labelled boundaries)\n",
        f"  boundaries   P {b['precision']:.2f}  R {b['recall']:.2f}  "
        f"F1 {b['f1']:.2f}   (tp {b['tp']}, fp {b['fp']}, fn {b['fn']}, "
        f"median offset {off})",
        f"  attribution  P {a['precision']:.2f}  R {a['recall']:.2f}  "
        f"unbound {100 * a['unbound_rate']:.0f}%"
        f"{'  (healthy)' if a['unbound_healthy'] else '  (outside 5–15%)'}   "
        f"correct {a['n_correct']} / attributed {a['n_attributed']} / "
        f"attributable {a['n_attributable']}",
    ]
    if s.get("confusions"):
        lines.append("\n  confusions (predicted → labelled, events):")
        for p, t, n in s["confusions"]:
            lines.append(f"    {p:<28} → {t:<28} {n:>4}")
    if s.get("escalations") is not None:
        lines.append(f"\n  model reader: {s['model_calls']} calls over "
                     f"{len(s['escalations'])} unbound episodes "
                     f"({model_calls_per_1000(s['model_calls'], s['n_events']):.1f} "
                     f"per 1000 events)")
        for r in s["escalations"]:
            hh = lambda t: dt.datetime.fromtimestamp(t).strftime("%H:%M")
            cands = ", ".join(f"{n} x{k}" for n, k in r["candidates"]) or "—"
            if r["error"] == "no_candidates":
                verdict = "no candidates: nothing in the text names a project"
            elif r["applied"]:
                verdict = (f"→ {r['choice']} @{r['confidence']:.2f}  "
                           f"{'RIGHT' if r['correct'] else 'WRONG'} "
                           f"(labelled {r['labelled'] or '—'})")
            elif r["choice"]:
                verdict = (f"→ {r['choice']} @{r['confidence']:.2f}, below "
                           f"threshold; stays blank (labelled {r['labelled'] or '—'})")
            else:
                verdict = (f"→ null{(' (' + r['error'] + ')') if r['error'] else ''}"
                           f" (labelled {r['labelled'] or '—'})")
            lines.append(f"    {hh(r['started_at'])}–{hh(r['ended_at'])} "
                         f"{r['n_events']:>3} ev  [{cands}]  {verdict}")
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="CAL §14: produce a labelling sheet, or score a filled one")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sheet", help="one day of events as a CSV to label")
    s.add_argument("--day", required=True, help="YYYY-MM-DD (local)")
    s.add_argument("--out", default=None,
                   help=f"CSV path (default {SHEET_DIR}/<day>.csv)")
    s.add_argument("--blind", action="store_true",
                   help="omit the system's predictions from the sheet")
    s.add_argument("--db", default=None)
    c = sub.add_parser("score", help="grade a replay against a filled sheet")
    c.add_argument("--labels", required=True, help="the filled CSV")
    c.add_argument("--day", default=None, help="default: the sheet's own day")
    c.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE_S,
                   help="seconds a boundary may miss by")
    c.add_argument("--json", action="store_true", help="metrics as JSON")
    c.add_argument("--escalate", action="store_true",
                   help="run the model reader over unbound episodes first "
                        "(local model; nothing is written)")
    c.add_argument("--min-confidence", type=float, default=0.5,
                   help="model confidence needed to apply a name")
    c.add_argument("--db", default=None)
    f = sub.add_parser("fill", help="label a time range of a sheet in place")
    f.add_argument("--labels", required=True, help="the CSV to edit")
    f.add_argument("--from", dest="start", required=True, help="HH:MM local")
    f.add_argument("--to", dest="end", required=True, help="HH:MM local, exclusive")
    g = f.add_mutually_exclusive_group(required=True)
    g.add_argument("--project", help="what this stretch was about ('' to clear)")
    g.add_argument("--accept", action="store_true",
                   help="write '=' — the predictions here are right")
    f.add_argument("--no-boundary", action="store_true",
                   help="this stretch continues the previous one")
    a = ap.parse_args(argv)
    if a.cmd == "fill":
        rows = read_sheet(a.labels)
        n = fill(rows, start=a.start, end=a.end, project=a.project,
                 accept=a.accept, boundary=not a.no_boundary)
        write_sheet(rows, a.labels)
        blank = sum(1 for r in rows if not (r.get("label_project") or "").strip())
        print(f"{n} rows {a.start}–{a.end} -> "
              f"{ACCEPT if a.accept else (a.project or '(cleared)')!r}; "
              f"{blank}/{len(rows)} rows still blank")
        return 0
    store = _open_store(a.db)
    if a.cmd == "sheet":
        rows = sheet_for_day(store, a.day, blind=a.blind)
        path = write_sheet(rows, a.out or SHEET_DIR / f"{a.day}.csv")
        print(f"{len(rows)} rows -> {path}"
              f"{'  (blind: no predictions)' if a.blind else ''}")
        return 0
    rows = read_sheet(a.labels)
    got = score_labels(store, rows, day=a.day, tolerance_s=a.tolerance,
                       escalate=a.escalate, min_confidence=a.min_confidence)
    if a.json:
        print(json.dumps(got, indent=2, default=str))
    else:
        print(report(got))
    return 2 if unlabelled(got) else 0


__all__ = ["boundary_f1", "match_boundaries", "attribution", "confusions",
           "model_calls_per_1000", "correction_persistence",
           "predictions_from", "labelling_sheet", "write_sheet", "read_sheet",
           "load_labels", "score_run", "sheet_for_day", "score_labels",
           "fill", "report", "unlabelled", "main", "DEFAULT_TOLERANCE_S", "ACCEPT", "SHEET_COLUMNS"]


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())
