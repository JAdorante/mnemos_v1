"""Offline faithfulness audit — the hallucination rate on REAL past facts.

The golden eval (scripts/eval_extraction.py) proves the extractor is faithful on
a dozen curated transcripts. This runs the SAME check over every fact already in
the DB: does each fact's stored `source_span` actually appear, verbatim, in the
speech it was extracted from? A span that quotes words never said is a
hallucinated provenance pointer — and this is how many of them slipped in before
the live check (app/services/cog_telemetry) existed.

    python scripts/audit_faithfulness.py            # audit all audio-origin facts
    python scripts/audit_faithfulness.py --show 40  # + list up to 40 unfaithful

Source text is the fact's *turn* (the merged utterances the extractor saw), not
just the anchor event — so multi-utterance turns score exactly as they did live.
Vision-origin facts (whose source_span is a constructed 'title: item', not a
speech quote) are excluded; they aren't verbatim quotes by design.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # import app.*

from app.services.cog_telemetry import span_is_faithful


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", type=int, default=15,
                    help="max unfaithful facts to list")
    args = ap.parse_args()

    from app.storage import get_store
    from app.services import consolidation

    store = get_store()
    if store.turn_count() == 0:
        print("[audit] no turns yet; rebuilding from the audio timeline...")
        consolidation.rebuild(store)

    # event_id -> the full turn text the extractor actually saw.
    ev2turn: dict[int, str] = {}
    for t in store.recent_turns(1_000_000):
        for eid in t.get("event_ids", []):
            ev2turn[int(eid)] = t.get("text") or ""

    facts = store.list_facts(limit=1_000_000)
    audio = [f for f in facts if (f.get("source_modality") == "audio"
                                  and (f.get("source_span") or "").strip())]

    ok = 0
    unfaithful: list[dict] = []
    no_turn = 0
    for f in audio:
        sev = f.get("source_event_id")
        src = ev2turn.get(int(sev)) if sev is not None else None
        if not src:                       # anchor not in any turn (rare) — skip
            no_turn += 1
            continue
        if span_is_faithful(f.get("source_span", ""), src):
            ok += 1
        else:
            unfaithful.append(f)

    scored = ok + len(unfaithful)
    print("\n=== faithfulness audit (past facts) ===")
    print(f"audio-origin facts:   {len(audio)}")
    print(f"scored (turn found):  {scored}   (skipped, no turn: {no_turn})")
    if scored:
        rate = len(unfaithful) / scored
        print(f"faithful spans:       {ok}/{scored}")
        print(f"hallucinated-span rate {rate:.3f}")
    if unfaithful:
        print(f"\n-- unfaithful (showing up to {args.show}) --")
        for f in unfaithful[: args.show]:
            print(f"  fact #{f['fact_id']} ({f.get('kind')}): "
                  f"span={f.get('source_span','')[:80]!r}")
            print(f"      text={f.get('text','')[:80]!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
