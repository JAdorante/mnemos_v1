"""People v3 WS-G — the three noise metrics, as one shared definition.

Noise stops being a feeling and becomes three numbers. These are pure
functions: `scripts/eval_people_noise.py` (CI) and the WS-B nightly shadow
report compute their inputs and call the same code here, so the gate and the
report can never drift apart.

Gates (spec v3 §7):
  junk-mint      <= 0.5 per audio-hour on the noisy corpus; 0 from documents
  wrong-owner    <= 2% vs golden labels
  mention share  <= 30% of every top-10 person_score
"""
from __future__ import annotations

JUNK_MINT_PER_AUDIO_HOUR_MAX = 0.5
WRONG_OWNER_RATE_MAX = 0.02
MENTION_SHARE_MAX = 0.30


def junk_mint_rate(junk_minted: int, audio_hours: float) -> float:
    """Person nodes created per audio-hour that end up merged, hidden, or
    archived within the scenario — the cost of minting too eagerly."""
    if audio_hours <= 0:
        return 0.0 if junk_minted == 0 else float("inf")
    return junk_minted / audio_hours


def wrong_owner_rate(assignments: list[tuple[object, object]]) -> float:
    """Fraction of golden-owned items bound to the WRONG person.

    `assignments` is [(golden_owner_key, resolved_owner_key), ...]; items
    whose golden owner is None are skipped (nothing to be wrong about), and a
    resolved None counts as a miss, not a wrong owner — leaving a commitment
    unowned is recoverable; handing it to the wrong person is the failure the
    gate exists for."""
    owned = [(g, r) for g, r in assignments if g is not None]
    if not owned:
        return 0.0
    wrong = sum(1 for g, r in owned if r is not None and r != g)
    return wrong / len(owned)


def mention_share(out_edges: list[dict], last_seen: float | None,
                  now: float) -> float:
    """Fraction of one person's score derived from mention evidence."""
    from app.services.home_intelligence import person_score_terms
    return person_score_terms(out_edges, last_seen, now)["mention_share"]


def top10_mention_shares(
        people: list[tuple[str, list[dict], float | None]],
        now: float) -> list[tuple[str, float, float]]:
    """Rank people by person_score and return (name, score, mention_share)
    for the top 10. `people` is [(name, out_edges, last_seen), ...]."""
    from app.services.home_intelligence import person_score_terms
    scored = []
    for name, edges, last_seen in people:
        t = person_score_terms(edges, last_seen, now)
        scored.append((name, t["score"], t["mention_share"]))
    scored.sort(key=lambda x: -x[1])
    return scored[:10]


def gate_report(*, junk_rate: float, doc_mints: int, wrong_rate: float,
                shares: list[tuple[str, float, float]]) -> dict:
    """Evaluate all three gates. `ok` is the CI verdict; per-gate booleans
    let the report say which knob failed."""
    share_ok = all(s <= MENTION_SHARE_MAX for _, _, s in shares)
    checks = {
        "junk_mint_ok": junk_rate <= JUNK_MINT_PER_AUDIO_HOUR_MAX,
        "doc_mint_ok": doc_mints == 0,
        "wrong_owner_ok": wrong_rate <= WRONG_OWNER_RATE_MAX,
        "mention_share_ok": share_ok,
    }
    return {**checks, "ok": all(checks.values())}
