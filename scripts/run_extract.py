"""Run one extraction pass over settled, unextracted turns.

    python scripts/run_extract.py            # extract from real captured audio
    python scripts/run_extract.py --demo     # seed a synthetic turn, then extract

--demo inserts a few fake AUDIO events (an old timestamp so the turn is already
'settled'), runs the extractor, prints the resulting open tasks / facts, then
leaves them in the DB so you can inspect them via /console or open_tasks().
Needs ANTHROPIC_API_KEY.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.events import Event, Modality
from app.services.extractor import extractor, EXTRACTOR_MODEL
from app.storage import get_store


def _seed_demo(store) -> None:
    # Timestamp well in the past so the turn is 'settled' (won't wait for merges).
    base = time.time() - 3600
    lines = [
        "Okay so for the launch I'll send Chris the pricing follow-up by Friday.",
        "We also need to book the venue for the demo day.",
        "Oh and the demo is on Monday, pricing is forty nine a month.",
    ]
    for i, text in enumerate(lines):
        ev = Event(
            time=base + i * 2.0, modality=Modality.AUDIO, raw=text,
            summary="", source="demo", confidence=0.95,
            meta={"speaker": {"name": "me", "is_known": True}},
        )
        store.insert(ev)
    print(f"[demo] seeded {len(lines)} synthetic audio utterance(s).")


def main(argv: list[str]) -> None:
    store = get_store()
    if "--demo" in argv:
        _seed_demo(store)

    print(f"[extract] model = {EXTRACTOR_MODEL}")
    summary = extractor.run_once(verbose=True)
    print(f"[extract] done: {summary}")

    tasks = store.open_tasks()
    print(f"\n=== open tasks ({len(tasks)}) ===")
    for t in tasks:
        owner = t.get("owner") or "(unassigned)"
        due = t.get("due") or "-"
        print(f"  [{owner}] {t['text']}  (due: {due})")
        print(f"        from: {t.get('source_span')!r}")

    print(f"\n[extract] total facts in DB: {store.fact_count()}")


if __name__ == "__main__":
    main(sys.argv)
