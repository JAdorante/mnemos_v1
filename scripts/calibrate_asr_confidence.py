"""Fit a new engine's ingest thresholds so a confidence-scale change cannot
quietly change what becomes a memory. (Perception plan §3.3.)

The problem, concretely
----------------------
`ingest_filter` decides whether an utterance is stored, flagged, demoted to
audio-only, or dropped as a hallucination. Three of its thresholds are numbers
on faster-whisper's `avg_logprob` scale — roughly -0.1 for crisp speech down to
-1.5 for garbage. A TDT decoder's confidence is a different quantity with a
different distribution. Point Whisper's thresholds at it and nothing errors:
the filter simply starts drawing the keep/drop line somewhere else, and a memory
product silently begins discarding real speech or storing ghosts.

The fit
-------
Both engines are scored on the same fixtures by `eval_asr.py`, which records
each utterance's confidence *and* the filter's inputs. From those two
distributions we fit a **quantile map**: the value at reference quantile q maps
to the candidate value at quantile q. It is monotone, assumes nothing about the
shape or units of either scale, and preserves the only thing a threshold really
encodes — *what fraction of utterances fall below the line*.

Each reference threshold is then pushed through that map. That keeps the three
logprob thresholds coherent with each other, which matters: they are ordered
(`phrase > low_conf > min`) and an independently-fitted set can cross over and
produce a filter that flags an utterance it also drops.

Why the fit is then checked rather than trusted
-----------------------------------------------
Distribution matching is a proxy. What we actually care about is behaviour on
the two populations the fixtures label: real speech (must not be dropped) and
no-speech probes (must be dropped). So the derived thresholds are **replayed**
through the real `assess()` against the candidate's own utterances, and the
resulting operating point is printed beside the reference's. If the false-drop
rate on real speech drifts — the deletion-dangerous direction — `--match
false-drop` re-derives `min_avg_logprob` to hit the reference's rate directly
and leaves the other thresholds on the quantile map.

Usage
-----
    python scripts/calibrate_asr_confidence.py fit \\
        data/eval/asr/report_whisper-baseline.json \\
        data/eval/asr/report_parakeet.json
    # ... read the comparison, then commit it:
    python scripts/calibrate_asr_confidence.py fit REF CAND --write
    python scripts/calibrate_asr_confidence.py show
    python scripts/calibrate_asr_confidence.py verify data/eval/asr/report_parakeet.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.config import settings                                    # noqa: E402
from app.services import asr_calibration as calib                  # noqa: E402
from app.services.ingest_filter import assess                      # noqa: E402

# The logprob-scale thresholds, and the direction each is compared in. All three
# are "below this is worse", so the quantile map applies to all three the same
# way — but they must stay ordered, which `_ordered` enforces after fitting.
LOGPROB_THRESHOLDS = ("phrase_avg_logprob", "low_conf_logprob", "min_avg_logprob")
NO_SPEECH_THRESHOLDS = ("phrase_no_speech_prob", "low_conf_no_speech",
                        "max_no_speech_prob")
KEEP_ACTIONS = ("keep", "keep_low_confidence", "needs_user_review")


# ---------------------------------------------------------------------------
# report -> utterances
# ---------------------------------------------------------------------------
class _Seg:
    """A stand-in carrying the two signals `assess()` reads off a segment.

    Replaying the real filter is the point: a reimplementation of its rules here
    would be a second thing to keep in sync, and the first time they diverged
    the calibration would be fitted for a filter nobody runs.
    """

    __slots__ = ("avg_logprob", "no_speech_prob")

    def __init__(self, logp, nsp):
        self.avg_logprob = logp
        self.no_speech_prob = nsp


def utterances(report: dict) -> list[dict]:
    """Flatten a report into per-utterance rows with their filter inputs.

    `is_speech` comes from the clip's `expect_speech`: an utterance out of a
    no-speech probe is, by construction, something the filter should refuse.
    That is the only ground truth this needs, and it is the one the fixture set
    is built to provide.
    """
    out = []
    for clip in report.get("per_clip", []):
        if "error" in clip:
            continue
        is_speech = bool(clip.get("expect_speech", True))
        for seg in clip.get("segments", []):
            if not (seg.get("text") or "").strip():
                continue          # nothing for the filter to judge
            out.append({
                "clip": clip.get("id"), "is_speech": is_speech,
                "text": seg["text"],
                "avg_confidence": seg.get("avg_confidence"),
                "no_speech_prob": seg.get("no_speech_prob"),
                "confidence_kind": seg.get("confidence_kind"),
                "action": seg.get("action"), "kept": bool(seg.get("kept")),
            })
    return out


def _values(rows: list[dict], key: str) -> list[float]:
    return sorted(float(r[key]) for r in rows
                  if isinstance(r.get(key), (int, float)))


# ---------------------------------------------------------------------------
# quantile map
# ---------------------------------------------------------------------------
def quantile_of(values: list[float], x: float) -> float:
    """Fraction of `values` at or below x, linearly interpolated between the
    two bracketing order statistics. Clamped to [0, 1]: a threshold outside the
    observed range maps to the end of the distribution, which is the honest
    answer — the fixtures contain no evidence about what lies beyond."""
    if not values:
        return 0.0
    n = len(values)
    if x <= values[0]:
        return 0.0
    if x >= values[-1]:
        return 1.0
    lo, hi = 0, n - 1
    while lo < hi:                       # first index with values[i] >= x
        mid = (lo + hi) // 2
        if values[mid] < x:
            lo = mid + 1
        else:
            hi = mid
    prev = values[lo - 1]
    span = values[lo] - prev
    frac = 0.0 if span <= 0 else (x - prev) / span
    return max(0.0, min(1.0, (lo - 1 + frac) / (n - 1)))


def value_at(values: list[float], q: float) -> float | None:
    """The value at quantile q — the inverse of `quantile_of`."""
    if not values:
        return None
    q = max(0.0, min(1.0, q))
    pos = q * (len(values) - 1)
    lo = int(pos)
    hi = min(len(values) - 1, lo + 1)
    frac = pos - lo
    return values[lo] + frac * (values[hi] - values[lo])


def map_threshold(ref_vals: list[float], cand_vals: list[float],
                  threshold: float) -> tuple[float | None, float]:
    """Push one threshold through the quantile map. Returns (value, quantile)."""
    q = quantile_of(ref_vals, threshold)
    return value_at(cand_vals, q), q


def _ordered(mapped: dict[str, float]) -> tuple[dict[str, float], list[str]]:
    """Keep the logprob thresholds in their required order.

    `phrase >= low_conf >= min` is not cosmetic: `assess()` reaches rule 5
    ("kept but flagged") only for utterances that passed rule 4 ("no reliable
    text"), so a `low_conf` below `min` describes a band that cannot occur, and
    a `phrase` below `low_conf` lets a denylisted ghost be flagged instead of
    dropped. Quantile mapping is monotone so this should hold automatically —
    it is enforced anyway, and any correction is reported, because a violation
    means the two distributions were too small or too degenerate to fit.
    """
    notes: list[str] = []
    out = dict(mapped)
    order = [k for k in LOGPROB_THRESHOLDS if k in out]
    for a, b in zip(order, order[1:]):
        if out[b] > out[a]:
            notes.append(f"{b} ({out[b]:.3f}) exceeded {a} ({out[a]:.3f}); "
                         f"clamped — the fit is under-determined")
            out[b] = out[a]
    return out, notes


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------
def operating_point(rows: list[dict], cfg) -> dict:
    """Run the real `assess()` over these utterances with `cfg` and summarise.

    Two rates carry the decision. `false_drop_rate` is speech the filter refused
    to store — the deletion-dangerous one, and the reason this whole script
    exists. `hallucination_drop_rate` is probe text it correctly refused.
    """
    speech = [r for r in rows if r["is_speech"]]
    probes = [r for r in rows if not r["is_speech"]]

    def judge(r):
        seg = _Seg(r.get("avg_confidence"), r.get("no_speech_prob"))
        return assess(r["text"], [seg], cfg)

    speech_v = [judge(r) for r in speech]
    probe_v = [judge(r) for r in probes]
    kept_speech = [v for v in speech_v if v.action in KEEP_ACTIONS]
    dropped_probe = [v for v in probe_v if v.action not in KEEP_ACTIONS]
    flagged = [v for v in speech_v
               if v.action in ("keep_low_confidence", "needs_user_review")]
    return {
        "n_speech": len(speech), "n_probe": len(probes),
        "false_drop_rate": (round(1 - len(kept_speech) / len(speech), 4)
                            if speech else None),
        "hallucination_drop_rate": (round(len(dropped_probe) / len(probes), 4)
                                    if probes else None),
        "flagged_rate": (round(len(flagged) / len(speech), 4)
                         if speech else None),
        "actions": _counts(v.action for v in speech_v + probe_v),
    }


def _counts(items) -> dict[str, int]:
    out: dict[str, int] = {}
    for i in items:
        out[str(i)] = out.get(str(i), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def match_false_drop(rows: list[dict], base_cfg, target_rate: float | None,
                     ceiling: float) -> tuple[float | None, str]:
    """Loosen `min_avg_logprob` until it drops no more real speech than the
    reference does. Returns (value, note).

    Two deliberate limits on what this is allowed to do.

    It only ever **loosens**. The quantile map already set the threshold; this
    step exists solely to repair the deletion-dangerous direction, so `ceiling`
    is the quantile-mapped value and the search never goes above it. Letting it
    tighten as well would turn a calibration into a fit against the fixtures'
    own speech, which is a different and much worse thing: an engine tuned to
    drop exactly the utterances this particular corpus finds hard.

    It searches rather than inverts. The rate is a step function of the
    threshold — it moves only as utterances cross it — and it interacts with the
    other rules inside `assess()`, so running the filter and looking is the only
    reliable way to ask "what threshold gives this behaviour". Bounds come from
    the candidate's own observed confidences, because a fixed range in Whisper's
    units is meaningless for an engine whose confidences are, say, all positive.
    """
    from dataclasses import replace

    speech = [r for r in rows if r["is_speech"]]
    if not speech or target_rate is None:
        return None, "no reference false-drop rate to match"

    def rate(t: float) -> float:
        cfg = replace(base_cfg, min_avg_logprob=t)
        return operating_point(rows, cfg)["false_drop_rate"] or 0.0

    current = rate(ceiling)
    if current <= target_rate:
        return None, (f"false-drop rate ({current:.3f}) is already at or below "
                      f"the reference ({target_rate:.3f}); quantile value kept")

    conf = _values(rows, "avg_confidence")
    if not conf:
        return None, "candidate reports no confidences to search over"
    span = max(1e-6, conf[-1] - conf[0])
    lo, hi = conf[0] - 0.1 * span, ceiling
    if rate(lo) > target_rate:
        # Even refusing to apply the rule at all drops too much speech, so the
        # excess is coming from another rule (silence probability, or the phrase
        # denylist) and moving this threshold cannot fix it.
        return None, (f"false-drop rate stays above the reference even with "
                      f"min_avg_logprob at the bottom of the observed range — "
                      f"the excess is not coming from this threshold")
    for _ in range(60):
        mid = (lo + hi) / 2
        if rate(mid) > target_rate:
            hi = mid          # too aggressive, loosen
        else:
            lo = mid
    return round(lo, 4), (f"min_avg_logprob loosened from {ceiling:.4f} to "
                          f"{lo:.4f} to match the reference's false-drop rate "
                          f"({target_rate:.3f})")


# ---------------------------------------------------------------------------
# fit
# ---------------------------------------------------------------------------
def fit(ref_report: dict, cand_report: dict, *, match: str = "quantile") -> dict:
    from dataclasses import replace

    ref_rows, cand_rows = utterances(ref_report), utterances(cand_report)
    if not ref_rows or not cand_rows:
        raise SystemExit("[calib] one of the reports has no scored utterances.")

    ref_engine = (ref_report.get("config") or {}).get("engine_id")
    cand_engine = (cand_report.get("config") or {}).get("engine_id")
    if ref_engine == cand_engine:
        raise SystemExit(
            f"[calib] both reports are {cand_engine!r}. Calibration maps one "
            f"engine's scale onto another's; scoring an engine against itself "
            f"would fit the identity and claim it meant something.")

    ref_conf = _values(ref_rows, "avg_confidence")
    cand_conf = _values(cand_rows, "avg_confidence")
    if len(cand_conf) < 20:
        print(f"[calib] WARNING: only {len(cand_conf)} candidate utterances "
              f"have a confidence. A quantile map fitted on this little is not "
              f"a calibration; add fixtures before trusting the numbers.")

    base = settings.ingest
    mapped: dict[str, float] = {}
    quantiles: dict[str, float] = {}
    for name in LOGPROB_THRESHOLDS:
        ref_t = float(getattr(base, name))
        val, q = map_threshold(ref_conf, cand_conf, ref_t)
        quantiles[name] = round(q, 4)
        if val is not None:
            mapped[name] = round(val, 4)
    mapped, notes = _ordered(mapped)

    # no_speech_prob is already a 0..1 probability, so it needs no rescaling —
    # but an engine that does not emit it at all changes which rules can fire,
    # and that is a behaviour change the operator has to know about.
    cand_nsp = _values(cand_rows, "no_speech_prob")
    if not cand_nsp:
        notes.append(
            "candidate emits no no_speech_prob: rules 2 and 3 "
            "(silence-probability) never fire for it, so hallucination "
            "rejection rests entirely on the confidence thresholds and the "
            "phrase denylist")

    ref_point = operating_point(ref_rows, base)
    cand_before = operating_point(cand_rows, base)
    cand_cfg = replace(base, **mapped)
    cand_after = operating_point(cand_rows, cand_cfg)

    if match == "false-drop":
        ceiling = mapped.get("min_avg_logprob", float(base.min_avg_logprob))
        tuned, note = match_false_drop(
            cand_rows, cand_cfg, ref_point["false_drop_rate"], ceiling)
        notes.append(note)
        if tuned is not None:
            mapped["min_avg_logprob"] = tuned
            mapped, more = _ordered(mapped)
            notes.extend(more)
            cand_cfg = replace(base, **mapped)
            cand_after = operating_point(cand_rows, cand_cfg)

    return {
        "engine_id": cand_engine,
        "confidence_kind": (cand_rows[0].get("confidence_kind")
                            or (cand_report.get("config") or {}).get(
                                "confidence_kind")),
        "fitted_at": round(time.time(), 3),
        "fitted_from": {"reference": ref_engine, "candidate": cand_engine,
                        "reference_tag": ref_report.get("tag"),
                        "candidate_tag": cand_report.get("tag")},
        "n_utterances": len(cand_conf),
        "method": match,
        "thresholds": mapped,
        "quantiles": quantiles,
        "notes": notes,
        "operating_point": {
            "reference": ref_point,
            "candidate_uncalibrated": cand_before,
            "candidate_calibrated": cand_after,
        },
    }


def report_fit(result: dict) -> None:
    base = settings.ingest
    print(f"\n=== calibration: {result['engine_id']} "
          f"(from {result['fitted_from']['reference']}) ===")
    print(f"  fitted on {result['n_utterances']} utterances · "
          f"method={result['method']}")
    print(f"\n  {'threshold':24s} {'reference':>10s} {'quantile':>9s} "
          f"{'candidate':>10s}")
    for name in LOGPROB_THRESHOLDS:
        if name not in result["thresholds"]:
            continue
        print(f"  {name:24s} {getattr(base, name):>10.3f} "
              f"{result['quantiles'].get(name, 0):>9.3f} "
              f"{result['thresholds'][name]:>10.3f}")

    op = result["operating_point"]
    print(f"\n  {'operating point':24s} {'reference':>10s} "
          f"{'cand (raw)':>11s} {'cand (cal)':>11s}")
    for key in ("false_drop_rate", "hallucination_drop_rate", "flagged_rate"):
        def f(d):
            v = d.get(key)
            return "—" if v is None else f"{v:.3f}"
        print(f"  {key:24s} {f(op['reference']):>10s} "
              f"{f(op['candidate_uncalibrated']):>11s} "
              f"{f(op['candidate_calibrated']):>11s}")

    ref_fd = op["reference"]["false_drop_rate"]
    cal_fd = op["candidate_calibrated"]["false_drop_rate"]
    if ref_fd is not None and cal_fd is not None and cal_fd > ref_fd + 0.02:
        print(f"\n  ! the calibrated candidate still drops more real speech "
              f"than the reference ({cal_fd:.3f} vs {ref_fd:.3f}).")
        print(f"    Re-run with --match false-drop, or add fixtures — this is "
              f"the direction that loses memories.")
    for n in result["notes"]:
        print(f"  · {n}")


def write(result: dict, dest: Path | None = None) -> Path:
    dest = dest or calib.path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    doc: dict[str, Any] = {"version": 1, "engines": {}}
    if dest.is_file():
        try:
            existing = json.loads(dest.read_text(encoding="utf-8"))
            if isinstance(existing.get("engines"), dict):
                doc = existing
                doc.setdefault("version", 1)
        except Exception as exc:
            print(f"[calib] existing {dest.name} unreadable ({exc}); "
                  f"writing a fresh file.")
    doc["engines"][result["engine_id"]] = result
    dest.write_text(json.dumps(doc, indent=1), encoding="utf-8")
    calib.reset_cache()
    return dest


# ---------------------------------------------------------------------------
def _load(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"[calib] no report at {p}")
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fit per-engine ingest thresholds (perception §3.3)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fit", help="fit a candidate engine against a reference")
    f.add_argument("reference", help="eval_asr report for the calibrated engine")
    f.add_argument("candidate", help="eval_asr report for the new engine")
    f.add_argument("--match", choices=("quantile", "false-drop"),
                   default="quantile",
                   help="'false-drop' re-derives min_avg_logprob to match the "
                        "reference's false-drop rate on real speech")
    f.add_argument("--write", action="store_true",
                   help="commit the fit to data/asr_calibration.json")
    f.add_argument("-o", "--out", default=None)

    s = sub.add_parser("show", help="print the current calibration table")

    v = sub.add_parser("verify", help="replay a report through the filter, "
                                      "calibrated vs not")
    v.add_argument("report")

    args = ap.parse_args()

    if args.cmd == "show":
        table = calib.load(force=True)
        if not table:
            print(f"[calib] no calibration at {calib.path()} — every engine is "
                  f"judged on the shipped thresholds.")
            return 0
        for engine, entry in sorted(table.items()):
            print(f"\n{engine}")
            print(f"  fitted from {entry.get('fitted_from', {}).get('reference')}"
                  f" on {entry.get('n_utterances')} utterances"
                  f" (method={entry.get('method')})")
            for k, val in sorted((entry.get("thresholds") or {}).items()):
                print(f"    {k:24s} {val}")
            for n in entry.get("notes") or []:
                print(f"    · {n}")
        return 0

    if args.cmd == "verify":
        rep = _load(args.report)
        rows = utterances(rep)
        engine = (rep.get("config") or {}).get("engine_id")
        shipped = operating_point(rows, settings.ingest)
        live = operating_point(rows, calib.cfg_for(engine))
        state = calib.describe(engine)
        print(f"[calib] {engine}: "
              f"{'calibrated' if state['calibrated'] else 'NOT calibrated'}"
              f" · {len(rows)} utterances")
        print(f"\n  {'metric':24s} {'shipped':>10s} {'in use':>10s}")
        for key in ("false_drop_rate", "hallucination_drop_rate", "flagged_rate"):
            def g(d):
                x = d.get(key)
                return "—" if x is None else f"{x:.3f}"
            print(f"  {key:24s} {g(shipped):>10s} {g(live):>10s}")
        return 0

    # --- fit
    result = fit(_load(args.reference), _load(args.candidate), match=args.match)
    report_fit(result)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
        print(f"\n[calib] wrote {args.out}")
    if args.write:
        dest = write(result)
        print(f"\n[calib] {result['engine_id']} committed to {dest}")
        print("        Transcripts from this engine are now judged on these "
              "thresholds. Delete the entry to fall back to the defaults.")
    else:
        print("\n[calib] dry run — pass --write to commit this to "
              f"{calib.path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
