"""Golden eval for #10 — the unified action-readiness score + risk-aware bands.

Checks the two things that matter: (1) the pre-#10 baseline is preserved — an
ordinary low/medium-risk task offers/suppresses at the same effective confidence
as before, and a missing confidence stays silent; (2) risk now bends the bar — a
high-risk action (buy/pay/send) needs a higher score to be offered, and a
human-ACCEPTED fact clears a bar its unreviewed twin doesn't. Deterministic and
LLM-free, so it runs in milliseconds.

    python scripts/eval_readiness.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import app.*

# (text, confidence, review, expect_band)
CASES = [
    # --- baseline preserved: low-risk keeps the old ~0.60 readiness floor ---
    ("call the vet back", 0.9, None, "offer"),
    ("call the vet back", 0.7, None, "offer"),        # 0.63 >= 0.60
    ("text Abby about tomorrow", 0.6, None, "review"),  # 0.54 < 0.60 (was suppressed)
    ("jot down the idea", 0.3, None, "hold"),          # 0.27 < review floor
    ("do the thing", None, None, "hold"),              # no confidence -> silent
    # --- medium risk: same floor ---
    ("book the venue for Friday", 0.7, None, "offer"),  # 0.63 >= 0.60
    # --- high risk: needs a HIGHER score even to offer ---
    ("buy the concert tickets", 0.7, None, "review"),   # 0.63 < 0.75
    ("buy the concert tickets", 0.9, None, "offer"),    # 0.81 >= 0.75
    ("email Justin the deck", 0.8, None, "review"),     # send=high, 0.72 < 0.75
    ("email Justin the deck", 0.85, None, "offer"),     # 0.765 >= 0.75
    ("pay the invoice", 0.8, None, "review"),           # 0.72 < 0.75
    # --- human ACCEPTED clears a bar the unreviewed twin can't ---
    ("pay the invoice", 0.8, "approved", "offer"),      # accepted tier -> 0.80 >= 0.75
]


def main() -> int:
    from app.services.readiness import for_task, band, score

    ok = 0
    for text, conf, review, exp in CASES:
        v = for_task(text, conf, review=review)
        hit = v.band == exp
        ok += hit
        mark = "" if hit else f"   <-- MISMATCH (got {v.band})"
        print(f"  {v.band:7} score={v.score:.2f} risk={v.risk:7} "
              f"(want {exp:7}) :: {text[:34]!r} conf={conf} rev={review}{mark}")

    # auto is opt-in: even a near-perfect low-risk score stays 'offer' by default
    hi = score(model=0.99)
    default_auto = band(hi, "low")
    import os
    os.environ["QUILL_AUTO_ACT"] = "1"
    opted_auto = band(hi, "low")
    os.environ["QUILL_AUTO_ACT"] = "0"
    auto_ok = default_auto == "offer" and opted_auto == "auto"

    n = len(CASES)
    print(f"\n=== readiness eval ===")
    print(f"band decisions correct: {ok}/{n}")
    print(f"auto opt-in gate:       default={default_auto} opted-in={opted_auto}  "
          + ("OK" if auto_ok else "<-- FAIL"))
    return 0 if (ok == n and auto_ok) else 1


if __name__ == "__main__":
    sys.exit(main())
