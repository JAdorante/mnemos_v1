"""Golden eval for the IntentRouter (priority #4) — is skipping SAFE and useful?

The router pre-filters turns so extraction skips the ones that can't produce a
fact. Its one inviolable rule: never skip a turn that carries a real task/claim.
So this eval's headline number is the **unsafe-skip rate** — cases labeled as
fact-bearing (`expect_extract: true`) that the router would wrongly skip. That
MUST be 0. The secondary number is skip recall on genuine filler (the calls it
saves). It runs offline in milliseconds — no LLM, no network.

    python scripts/eval_intent.py

Ground truth lives in data/bench/intent/golden.jsonl (one JSON object per line:
transcript + expect_extract + optional intent). Add cases as new turns surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import app.*

DATA = Path("data/bench/intent/golden.jsonl")


def main() -> int:
    from app.services.intent import classify

    cases = [json.loads(l) for l in DATA.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    if not cases:
        print(f"[eval] no cases in {DATA}")
        return 1

    unsafe = []          # expected extract, router skipped  (MUST be empty)
    saved = 0            # expected skip,   router skipped   (good: call saved)
    wasted = 0           # expected skip,   router extracted (harmless cost)
    intent_ok = intent_tot = 0
    want_extract = want_skip = 0

    for c in cases:
        r = classify(c["transcript"])
        exp = bool(c["expect_extract"])
        if exp:
            want_extract += 1
            if not r.should_extract:
                unsafe.append(c["transcript"])
        else:
            want_skip += 1
            if r.should_extract:
                wasted += 1
            else:
                saved += 1
        if c.get("intent"):
            intent_tot += 1
            intent_ok += 1 if r.intent == c["intent"] else 0

    def pct(a, b):
        return (a / b) if b else 1.0

    print(f"[eval] intent cases: {len(cases)} from {DATA}\n")
    print("=== IntentRouter golden eval ===")
    print(f"fact-bearing turns:   {want_extract}")
    print(f"UNSAFE skips:         {len(unsafe)}  "
          f"(unsafe-skip rate {pct(len(unsafe), want_extract):.2f})  "
          + ("<-- MUST be 0" if unsafe else "OK"))
    for t in unsafe:
        print(f"    !! wrongly skipped: {t!r}")
    print(f"filler turns:         {want_skip}")
    print(f"correctly skipped:    {saved}/{want_skip}  "
          f"(calls saved {pct(saved, want_skip):.0%}; "
          f"{wasted} filler still extracted — harmless cost)")
    if intent_tot:
        print(f"intent-label accuracy {intent_ok}/{intent_tot}  "
              f"({pct(intent_ok, intent_tot):.0%})")
    return 1 if unsafe else 0


if __name__ == "__main__":
    sys.exit(main())
