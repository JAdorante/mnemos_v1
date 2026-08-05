"""#8 — Voice pipeline eval harness: golden clips + metrics + A/B.

The goal (per the roadmap) is not academic perfection — it's *knowing whether a
code change made Mnemos better or worse*. SNR is a lying proxy (it went up while
ASR got worse in #2); this measures the things that matter, on a fixed golden set,
reproducibly.

Golden set is built from three sources, so every metric has real ground truth:
  * TTS clips (offline SAPI)  — KNOWN reference text -> true WER / entity / task
    labels, plus controllable noisy + far-field degradations of the same clip.
  * real mic clips (from the DB) — correct audio distribution; the stored
    transcript is the reference (measures drift / regression under real audio).
  * synthetic silence/noise    — expect_drop=True -> hallucination-drop rate.

Pipeline stages mirror app/services/audio.py: audio_quality -> (skip_bad?) ->
denoise(noisy) -> Whisper -> ingest_filter verdict.

Usage:
    python scripts/eval_voice.py build            # (re)build golden set + manifest
    python scripts/eval_voice.py run  [--limit N] [--tag baseline] [--tasks]
    python scripts/eval_voice.py compare a.json b.json   # A/B: did it get better?

A/B example (does enabling the spectral gate for ASR help or hurt?):
    python scripts/eval_voice.py run --tag off  -o off.json
    QUILL_DENOISE_SPECTRAL_ASR=1 python scripts/eval_voice.py run --tag on -o on.json
    python scripts/eval_voice.py compare off.json on.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.config import settings                                    # noqa: E402
from app.services.audio_quality import score as aq_score          # noqa: E402
from app.services.ingest_filter import assess                     # noqa: E402
from app.services.denoise import enhance                          # noqa: E402

EVAL_DIR = _ROOT / "data" / "eval"
CLIPS_DIR = EVAL_DIR / "clips"
MANIFEST = EVAL_DIR / "manifest.jsonl"
SR = 16000


# ---------------------------------------------------------------------------
# wav io + degradations
# ---------------------------------------------------------------------------
def read_wav(path: str):
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    return x, sr


def write_wav(path: Path, x, sr: int = SR):
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(x, -1, 1) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
        wf.writeframes(pcm.tobytes())


def _rng():
    return np.random.default_rng(20260709)


def add_noise(x, snr_db, rng):
    rms = float(np.sqrt(np.mean(x * x))) or 1e-4
    std = rms / (10 ** (snr_db / 20.0))
    return (x + std * rng.standard_normal(len(x))).astype(np.float32)


def far_field(x, rng):
    """Crude far-field sim: low-pass (muffled), attenuate, short echo, room hiss."""
    # 1-pole low-pass ~ telephone band
    y = np.empty_like(x); a = 0.72; acc = 0.0
    for i, v in enumerate(x):
        acc = a * acc + (1 - a) * v; y[i] = acc
    echo = np.zeros_like(y); d = int(0.045 * SR)
    if len(y) > d:
        echo[d:] = 0.35 * y[:-d]
    y = 0.5 * (y + echo)
    return add_noise(y, 12, rng).astype(np.float32)


# ---------------------------------------------------------------------------
# metrics (pure — unit-testable without audio)
# ---------------------------------------------------------------------------
import re as _re

_PUNCT = _re.compile(r"[^\w\s]")


def norm_words(s: str):
    return _PUNCT.sub(" ", (s or "").lower()).split()


def _levenshtein(a, b):
    m, n = len(a), len(b)
    if m == 0:
        return n
    d = list(range(n + 1))
    for i in range(1, m + 1):
        prev, d[0] = d[0], i
        for j in range(1, n + 1):
            cur = d[j]
            d[j] = min(d[j] + 1, d[j - 1] + 1, prev + (a[i - 1] != b[j - 1]))
            prev = cur
    return d[n]


def wer(ref: str, hyp: str) -> float:
    r, h = norm_words(ref), norm_words(hyp)
    if not r:
        return 0.0 if not h else 1.0
    return _levenshtein(r, h) / len(r)


def entity_recall(entities, hyp: str):
    """Fraction of expected entities present in the hypothesis (token-subset,
    case-insensitive). Returns (found, total)."""
    hw = set(norm_words(hyp))
    found = 0
    for e in entities or []:
        et = norm_words(e)
        if et and all(t in hw for t in et):
            found += 1
    return found, len(entities or [])


# ---------------------------------------------------------------------------
# TTS (offline SAPI) — best-effort; returns False if unavailable
# ---------------------------------------------------------------------------
def sapi_render(text: str, path: Path) -> bool:
    try:
        import pythoncom
        import win32com.client as wc
        pythoncom.CoInitialize()
        fs = wc.Dispatch("SAPI.SpFileStream")
        fmt = wc.Dispatch("SAPI.SpAudioFormat")
        fmt.Type = 22                       # SAFT16kHz16BitMono
        fs.Format = fmt
        path.parent.mkdir(parents=True, exist_ok=True)
        fs.Open(str(path), 3, False)        # SSFMCreateForWrite
        spv = wc.Dispatch("SAPI.SpVoice")
        spv.AudioOutputStream = fs
        spv.Speak(text)
        fs.Close()
        return path.is_file() and path.stat().st_size > 1000
    except Exception as exc:
        print(f"[eval] SAPI TTS unavailable ({exc}); skipping synthesized clips.")
        return False


# Known-reference sentences by category. entities = names that must survive ASR;
# is_task marks imperative/command utterances.
_TTS = {
    "clean": [
        ("The quarterly report is due on Friday afternoon.", [], False),
        ("I finished reading the design document last night.", [], False),
        ("We should schedule the review for early next week.", [], False),
        ("The meeting ran long so I missed the earlier train.", [], False),
    ],
    "command": [
        ("Text Abby that I will be five minutes late.", ["Abby"], True),
        ("Remind me to email Marc the pricing follow up.", ["Marc"], True),
        ("Send Dad the address for dinner tonight.", ["Dad"], True),
        ("Add buy groceries to my task list for tomorrow.", [], True),
    ],
    "names": [
        ("Abby Nengel and Justin met with Marc and Lori.",
         ["Abby", "Nengel", "Justin", "Marc", "Lori"], False),
        ("Tell Kristi Sacco about the Villanova networking event.",
         ["Kristi", "Sacco", "Villanova"], False),
        ("Lori and Justin are joining the Venture Pulse call.",
         ["Lori", "Justin", "Venture", "Pulse"], False),
    ],
}


# ---------------------------------------------------------------------------
# golden set builder
# ---------------------------------------------------------------------------
def build_golden(n_real: int = 18):
    rng = _rng()
    CLIPS_DIR.mkdir(parents=True, exist_ok=True)
    entries = []

    def add(cid, category, audio_path, reference, *, ref_type, expect_drop=False,
            entities=None, is_task=False, speaker=None):
        entries.append({
            "id": cid, "category": category, "audio_path": str(audio_path),
            "reference": reference, "ref_type": ref_type, "expect_drop": expect_drop,
            "entities": entities or [], "is_task": is_task, "speaker": speaker,
        })

    # 1. synthetic silence / noise -> should be dropped (true label)
    write_wav(CLIPS_DIR / "sil_1.wav", (0.0008 * rng.standard_normal(int(SR * 1.4))).astype(np.float32))
    write_wav(CLIPS_DIR / "sil_2.wav", (0.0004 * rng.standard_normal(int(SR * 1.0))).astype(np.float32))
    write_wav(CLIPS_DIR / "noise_1.wav", (0.03 * rng.standard_normal(int(SR * 1.4))).astype(np.float32))
    for cid in ("sil_1", "sil_2", "noise_1"):
        add(cid, "silence", CLIPS_DIR / f"{cid}.wav", "", ref_type="true", expect_drop=True)

    # 2. TTS clips (known reference) + noisy / far-field degradations
    tts_ok = None
    for cat, items in _TTS.items():
        for i, (text, ents, is_task) in enumerate(items):
            base = CLIPS_DIR / f"tts_{cat}_{i}.wav"
            if tts_ok is None:
                tts_ok = sapi_render(text, base)
            elif tts_ok:
                sapi_render(text, base)
            if not tts_ok:
                continue
            add(f"tts_{cat}_{i}", cat, base, text, ref_type="true",
                entities=ents, is_task=is_task)
            # degrade a couple per category into noisy + far-field variants
            if i < 2:
                x, srr = read_wav(base)
                nz = CLIPS_DIR / f"tts_{cat}_{i}_noisy.wav"
                ff = CLIPS_DIR / f"tts_{cat}_{i}_farfield.wav"
                write_wav(nz, add_noise(x, 5, rng)); write_wav(ff, far_field(x, rng))
                add(f"tts_{cat}_{i}_noisy", "noisy", nz, text, ref_type="true",
                    entities=ents, is_task=is_task)
                add(f"tts_{cat}_{i}_farfield", "farfield", ff, text, ref_type="true",
                    entities=ents, is_task=is_task)

    # 3. real mic clips from the DB (stored transcript = drift reference)
    try:
        from app.storage import get_store
        store = get_store()
        rows = [ev for ev in store.all()
                if ev.modality.value == "audio" and (ev.raw or "").strip()
                and (ev.meta or {}).get("audio_path")]
        rng2 = _rng()
        picks = rng2.permutation(len(rows))[: n_real * 3]
        added = 0
        for idx in picks:
            ev = rows[int(idx)]
            ap = ev.meta["audio_path"]
            if not Path(ap).is_file():
                continue
            x, srr = read_wav(ap)
            if srr != SR or len(x) < SR * 0.8:
                continue
            q = aq_score(x, SR)["quality"]
            cat = "clean_real" if q == "good" else "noisy_real"
            text = ev.raw.strip()
            ents = _guess_names(text)
            add(f"real_{added}", cat, ap, text, ref_type="drift", entities=ents)
            added += 1
            if added >= n_real:
                break
    except Exception as exc:
        print(f"[eval] real-clip bootstrap skipped ({exc}).")

    with open(MANIFEST, "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    by = {}
    for e in entries:
        by[e["category"]] = by.get(e["category"], 0) + 1
    print(f"[eval] built {len(entries)} golden clips -> {MANIFEST}")
    for k, v in sorted(by.items()):
        print(f"        {k:14s} {v}")
    return entries


_NAME_RE = _re.compile(r"\b([A-Z][a-z]+)\b")


def _guess_names(text: str):
    """Provisional entity labels for real clips: capitalized non-sentence-initial
    words. Rough, but gives entity-recall a signal on drift refs."""
    words = text.split()
    out = []
    for i, w in enumerate(words):
        m = _NAME_RE.fullmatch(w.strip(".,!?;:"))
        if m and i > 0:                     # skip sentence-initial capitals
            out.append(m.group(1))
    return list(dict.fromkeys(out))[:6]


# ---------------------------------------------------------------------------
# pipeline (mirrors app/services/audio.py order)
# ---------------------------------------------------------------------------
class EvalPipeline:
    def __init__(self, bias_prompt=None):
        from faster_whisper import WhisperModel
        self.model = WhisperModel(settings.audio.whisper_model, device="cpu",
                                  compute_type="int8")
        self.bias_prompt = bias_prompt or None   # #3 Whisper initial_prompt

    def process(self, x, sr):
        aq = aq_score(x, sr) if settings.audio_quality.enabled else None
        skipped = bool(aq and settings.audio_quality.skip_bad and aq["quality"] == "bad")
        out = {"quality": aq["quality"] if aq else None, "skipped": skipped,
               "asr_text": "", "action": "skipped" if skipped else None,
               "asr_latency_ms": 0.0, "denoised": False}
        if skipped:
            out["kept_as_memory"] = False
            return out
        asr_audio = x
        if (aq and settings.denoise.enabled
                and aq["quality"] in settings.denoise.routes):
            y, info = enhance(x, sr)
            if info.get("applied"):
                asr_audio = y; out["denoised"] = True
        t0 = time.time()
        segs, _ = self.model.transcribe(asr_audio.astype(np.float32), language="en",
                                        vad_filter=False, beam_size=1,
                                        initial_prompt=self.bias_prompt)
        segs = list(segs)
        out["asr_latency_ms"] = round((time.time() - t0) * 1000, 1)
        text = " ".join(s.text.strip() for s in segs).strip()
        out["asr_text"] = text
        if settings.ingest.enabled and text:
            v = assess(text, segs)
            out["action"] = v.action
            out["kept_as_memory"] = v.action in (
                "keep", "keep_low_confidence", "needs_user_review")
        else:
            out["action"] = "keep" if text else "empty"
            out["kept_as_memory"] = bool(text)
        return out


# ---------------------------------------------------------------------------
# evaluate + report
# ---------------------------------------------------------------------------
def evaluate(entries, limit=None, do_tasks=False, bias_prompt=None):
    pipe = EvalPipeline(bias_prompt=bias_prompt)
    per = []
    for e in (entries[:limit] if limit else entries):
        try:
            x, sr = read_wav(e["audio_path"])
        except Exception as exc:
            print(f"[eval] skip {e['id']} ({exc})"); continue
        r = pipe.process(x, sr)
        row = {**{k: e[k] for k in ("id", "category", "ref_type", "expect_drop",
                                    "is_task")},
               "quality": r["quality"], "action": r["action"],
               "denoised": r["denoised"], "asr_latency_ms": r["asr_latency_ms"],
               "kept": r["kept_as_memory"], "hyp": r["asr_text"]}
        if not e["expect_drop"]:
            row["wer"] = round(wer(e["reference"], r["asr_text"]), 3)
            f, t = entity_recall(e["entities"], r["asr_text"])
            row["ent_found"], row["ent_total"] = f, t
        if do_tasks:
            row["pred_task"] = _predict_task(r["asr_text"])
        per.append(row)
    cfg = _config_snapshot()
    cfg["bias_prompt"] = (bias_prompt[:120] + "…") if bias_prompt and len(bias_prompt) > 120 else bias_prompt
    return {"config": cfg, "per_entry": per,
            "overall": _aggregate(per, do_tasks),
            "by_category": _by_category(per)}


def _predict_task(text):
    """Cheap deterministic task classifier for the harness (a proxy for the real
    LLM extractor — keeps `run` offline/free). Upgrade to the router/extractor
    when measuring the extractor itself rather than the ASR feeding it."""
    t = (text or "").lower()
    verbs = ("text ", "email ", "remind me", "send ", "call ", "add ", "schedule ",
             "message ", "tell ", "ask ")
    return any(t.startswith(v) or (" " + v) in t for v in verbs)


def _aggregate(per, do_tasks):
    def mean(vals):
        vals = [v for v in vals if v is not None]
        return round(sum(vals) / len(vals), 3) if vals else None

    scored = [r for r in per if "wer" in r]
    drops = [r for r in per if r["expect_drop"]]
    ent_f = sum(r.get("ent_found", 0) for r in per)
    ent_t = sum(r.get("ent_total", 0) for r in per)
    agg = {
        "n": len(per),
        "wer": mean([r.get("wer") for r in scored]),
        "wer_true": mean([r["wer"] for r in scored if r["ref_type"] == "true"]),
        "wer_drift": mean([r["wer"] for r in scored if r["ref_type"] == "drift"]),
        "entity_recall": round(ent_f / ent_t, 3) if ent_t else None,
        "hallucination_drop_rate":
            round(sum(1 for r in drops if not r["kept"]) / len(drops), 3) if drops else None,
        "false_keep_rate":
            round(sum(1 for r in drops if r["kept"]) / len(drops), 3) if drops else None,
        "asr_latency_ms_avg": mean([r["asr_latency_ms"] for r in per]),
        "denoised_pct": round(100 * sum(1 for r in per if r["denoised"]) / len(per), 1) if per else 0,
    }
    if do_tasks:
        tp = sum(1 for r in per if r.get("is_task") and r.get("pred_task"))
        fn = sum(1 for r in per if r.get("is_task") and not r.get("pred_task"))
        fp = sum(1 for r in per if not r.get("is_task") and r.get("pred_task"))
        agg["task_precision"] = round(tp / (tp + fp), 3) if (tp + fp) else None
        agg["task_recall"] = round(tp / (tp + fn), 3) if (tp + fn) else None
        non_task = [r for r in per if not r.get("is_task")]
        agg["false_task_offer_rate"] = round(fp / len(non_task), 3) if non_task else None
    return agg


def _by_category(per):
    cats = {}
    for r in per:
        cats.setdefault(r["category"], []).append(r)
    out = {}
    for cat, rows in sorted(cats.items()):
        scored = [x for x in rows if "wer" in x]
        drops = [x for x in rows if x["expect_drop"]]
        out[cat] = {
            "n": len(rows),
            "wer": round(sum(x["wer"] for x in scored) / len(scored), 3) if scored else None,
            "drop_rate": round(sum(1 for x in drops if not x["kept"]) / len(drops), 3) if drops else None,
        }
    return out


def _config_snapshot():
    s = settings
    return {
        "whisper_model": s.audio.whisper_model,
        "aq_enabled": s.audio_quality.enabled, "skip_bad": s.audio_quality.skip_bad,
        "denoise_backend": s.denoise.backend, "denoise_spectral_asr": s.denoise.spectral_asr,
        "denoise_routes": list(s.denoise.routes),
        "asr_bias": s.asr_bias.enabled,
        "ingest_enabled": s.ingest.enabled,
        "ingest_min_logprob": s.ingest.min_avg_logprob,
    }


def summarize(report):
    o = report["overall"]
    print("\n=== overall ===")
    for k, v in o.items():
        print(f"  {k:26s} {v}")
    print("\n=== by category ===")
    print(f"  {'category':14s} {'n':>3s} {'wer':>7s} {'drop':>6s}")
    for cat, m in report["by_category"].items():
        w = "" if m["wer"] is None else f"{m['wer']:.3f}"
        dr = "" if m["drop_rate"] is None else f"{m['drop_rate']:.2f}"
        print(f"  {cat:14s} {m['n']:>3d} {w:>7s} {dr:>6s}")


def compare(path_a, path_b):
    a = json.load(open(path_a)); b = json.load(open(path_b))
    oa, ob = a["overall"], b["overall"]
    print(f"A = {path_a}   config={a['config']}")
    print(f"B = {path_b}   config={b['config']}\n")
    # lower-is-better metrics
    lower = {"wer", "wer_true", "wer_drift", "false_keep_rate", "asr_latency_ms_avg",
             "false_task_offer_rate"}
    print(f"  {'metric':26s} {'A':>9s} {'B':>9s} {'d(B-A)':>9s}  verdict")
    for k in sorted(set(oa) | set(ob)):
        va, vb = oa.get(k), ob.get(k)
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            continue
        d = round(vb - va, 3)
        if abs(d) < 1e-9:
            verdict = "same"
        elif k in lower:
            verdict = "BETTER" if d < 0 else "worse"
        else:
            verdict = "BETTER" if d > 0 else "worse"
        print(f"  {k:26s} {va:>9} {vb:>9} {d:>+9}  {verdict}")


# ---------------------------------------------------------------------------
def _load_manifest():
    if not MANIFEST.is_file():
        print(f"[eval] no manifest at {MANIFEST}; run `build` first.")
        sys.exit(2)
    return [json.loads(l) for l in open(MANIFEST, encoding="utf-8") if l.strip()]


def main():
    ap = argparse.ArgumentParser(description="Voice pipeline eval harness (#8)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build"); b.add_argument("--real", type=int, default=18)
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, default=None)
    r.add_argument("--tag", default="run")
    r.add_argument("-o", "--out", default=None)
    r.add_argument("--tasks", action="store_true")
    r.add_argument("--bias", action="store_true",
                   help="#3: bias Whisper with the real KG vocabulary (initial_prompt)")
    r.add_argument("--bias-oracle", action="store_true",
                   help="#3 mechanism test: bias with the golden set's own names")
    c = sub.add_parser("compare"); c.add_argument("a"); c.add_argument("b")
    args = ap.parse_args()

    if args.cmd == "build":
        build_golden(n_real=args.real)
    elif args.cmd == "run":
        entries = _load_manifest()
        bias = None
        if args.bias_oracle:
            ents = sorted({e for it in entries for e in it["entities"] if e})
            bias = ("Names: " + ", ".join(ents) + ".") if ents else None
            print(f"[eval] bias(oracle) = {bias!r}")
        elif args.bias:
            from app.services.vocabulary import vocabulary
            bias = vocabulary.whisper_prompt() or None
            print(f"[eval] bias(graph) = {bias!r}")
        print(f"[eval] running {args.tag}: {len(entries)} clips, config={_config_snapshot()}")
        report = evaluate(entries, limit=args.limit, do_tasks=args.tasks, bias_prompt=bias)
        report["tag"] = args.tag
        summarize(report)
        out = args.out or str(EVAL_DIR / f"report_{args.tag}.json")
        json.dump(report, open(out, "w"), indent=1)
        print(f"\n[eval] wrote {out}")
    elif args.cmd == "compare":
        compare(args.a, args.b)


if __name__ == "__main__":
    main()
