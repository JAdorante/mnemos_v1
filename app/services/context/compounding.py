"""CAL Stage 4 — the mechanisms that make the layer get better, or stop it
getting worse: key specificity, evidence independence, and new-project
detection.

Each is a guard against a different way a binding graph rots.
"""
from __future__ import annotations

import json
import math
import time

# --- 1. key specificity (IDF) — the anti-bleed guard (§3.4) ------------------
# Some keys are real identifiers that carry no discriminating signal:
# `path:~/Downloads`, `domain:google.com`, `branch:*#main`. The seed stop-list in
# keys.py catches the ones we can name in advance. This measures the rest, from
# how they actually behave: a key that points everywhere points nowhere.
SPREAD_DEMOTE_ABOVE = 0.5
SPREAD_MIN_OBS = 20
DEMOTED_STRENGTH = 0.25
PROMOTE_EVIDENCE_MULTIPLE = 3      # asymmetric on purpose — see below


def spread(distribution) -> float:
    """Normalized entropy of a key's node distribution, in [0, 1].

    0.0 means the key always means one thing. 1.0 means it is spread evenly
    over everything it touches and is worthless for attribution.
    """
    counts = [float(c) for c in distribution if c and c > 0]
    total = sum(counts)
    if total <= 0 or len(counts) < 2:
        return 0.0
    h = -sum((c / total) * math.log(c / total) for c in counts)
    return min(1.0, h / math.log(max(2, len(counts))))


def recompute_spread(store, *, key_types=None, now: float | None = None) -> dict:
    """Nightly sweep: measure every key's spread and demote the bleeders.

    Demotion is STICKY and promotion costs several times the evidence that
    demoted it, because the two errors are not symmetric — a missed binding
    costs one escalation, while a bleeding key silently mis-attributes
    everything it touches and is invisible until someone reads a wrong answer.
    """
    ts = float(now if now is not None else time.time())
    sql = ("SELECT key_type, key_value, node_type, node_id, n_obs, strength, "
           "       confirmed FROM kg_node_keys WHERE valid_to IS NULL")
    args: list = []
    if key_types:
        kt = list(key_types)
        sql += f" AND key_type IN ({','.join('?' * len(kt))})"
        args += kt
    with store._lock:
        rows = [dict(r) for r in store._conn.execute(sql, args).fetchall()]
    by_key: dict = {}
    for r in rows:
        by_key.setdefault((r["key_type"], r["key_value"]), []).append(r)

    measured = demoted = 0
    for (ktype, kvalue), group in by_key.items():
        obs = sum(int(g["n_obs"] or 0) for g in group)
        s = spread([g["n_obs"] or 0 for g in group])
        measured += 1
        bleeding = s > SPREAD_DEMOTE_ABOVE and obs >= SPREAD_MIN_OBS
        for g in group:
            new_strength = g["strength"]
            if bleeding and not g["confirmed"]:
                # A user-confirmed binding is never demoted by a statistic.
                new_strength = min(float(g["strength"] or 0.0), DEMOTED_STRENGTH)
            try:
                with store._lock:
                    store._conn.execute(
                        "UPDATE kg_node_keys SET spread=?, strength=? "
                        "WHERE node_type=? AND node_id=? AND key_type=? "
                        "AND key_value=?",
                        (s, new_strength, g["node_type"], g["node_id"],
                         ktype, kvalue))
                    store._conn.commit()
            except Exception as exc:
                print(f"[cal.compounding] spread update skipped ({exc}).")
        if bleeding:
            demoted += 1
    return {"ok": True, "keys_measured": measured, "keys_demoted": demoted,
            "at": ts}


# --- 2. evidence independence (§8.1) ----------------------------------------
# Seeing `entity_resolver.py` four hundred times in one sitting is ONE
# observation of "I am working on this", not four hundred. Weight therefore
# saturates WITHIN a bucket and adds ACROSS buckets, where a bucket is one
# source class in one episode — a different day, a different modality or a
# different episode is genuinely new evidence.
#
# NOTE: the design states this as `min(1.0, 1 + 0.2·ln(n))`, which is exactly
# 1.0 for every n >= 1 — the cap sits on the wrong side of the growth term and
# would flatten all evidence to its base weight. The intent is clearly
# logarithmic growth under a ceiling, which is what this implements.
BUCKET_GROWTH = 0.2
BUCKET_CAP = 2.0


def bucket_weight(base: float, n: int, *, growth: float = BUCKET_GROWTH,
                  cap: float = BUCKET_CAP) -> float:
    """Effective weight of `n` sightings inside ONE bucket."""
    if n <= 0:
        return 0.0
    return float(base) * min(cap, 1.0 + growth * math.log(max(1, int(n))))


def independent_total(observations, *, base_weights=None) -> float:
    """Total evidence weight across buckets, saturating within each.

    `observations` is an iterable of (bucket_key, source_class). Buckets should
    be (source_class, episode_id): the same file seen all morning is one
    bucket, the same file mentioned in a meeting is another.
    """
    counts: dict = {}
    classes: dict = {}
    for bucket, source_class in observations or []:
        counts[bucket] = counts.get(bucket, 0) + 1
        classes[bucket] = source_class
    bw = base_weights or {}
    return sum(bucket_weight(float(bw.get(classes[b], 1.0)), n)
               for b, n in counts.items())


# --- 3. new-project detection (§5.4) -----------------------------------------
# Never from one event. This is the difference between a graph with forty
# meaningful projects and one with nine hundred pieces of debris.
MIN_DISTINCT_KEYS = 4
MIN_EVENTS = 25
MIN_DAYS = 2


def detect_new_project(observations, *, min_keys: int = MIN_DISTINCT_KEYS,
                       min_events: int = MIN_EVENTS,
                       min_days: int = MIN_DAYS) -> list[dict]:
    """Clusters of unbound strong keys that look like something real.

    `observations` is an iterable of (event_id, day, frozenset_of_strong_keys)
    for events that resolved to NOTHING. Returns candidate clusters; it does
    not mint, name, or surface anything — naming is one model call and the
    result is a PROVISIONAL entity that a human confirms, because an
    auto-created project is a guess with a name on it.

    Clusters are built on key CO-OCCURRENCE, not similarity: keys that keep
    showing up together in unattributed events are evidence of one unnamed
    thing, whereas keys that merely resemble each other are evidence of
    nothing.
    """
    groups: dict = {}
    for event_id, day, keys in observations or []:
        ks = frozenset(keys or ())
        if not ks:
            continue
        hit = None
        for gid, g in groups.items():
            if g["keys"] & ks:
                hit = gid
                break
        if hit is None:
            groups[len(groups)] = {"keys": set(ks), "events": {event_id},
                                   "days": {day}}
        else:
            groups[hit]["keys"] |= ks
            groups[hit]["events"].add(event_id)
            groups[hit]["days"].add(day)

    out = []
    for g in groups.values():
        if (len(g["keys"]) >= min_keys and len(g["events"]) >= min_events
                and len(g["days"]) >= min_days):
            out.append({"keys": sorted(g["keys"]),
                        "n_events": len(g["events"]),
                        "n_days": len(g["days"]),
                        "state": "provisional"})
    out.sort(key=lambda c: (-c["n_events"], c["keys"][0] if c["keys"] else ""))
    return out


def may_mint(name: str, *, event_source: str = "", window: str = "") -> bool:
    """Final gate before a proposed project becomes an entity.

    Routes through the existing mint policy rather than adding a second one —
    `kg_beliefs.allow_entity_mint` already encodes which sources are allowed to
    create nodes, and a parallel rule here would drift from it.
    """
    try:
        from app.services.kg_beliefs import allow_entity_mint
        return bool(allow_entity_mint(event_source=event_source, window=window))
    except Exception as exc:
        print(f"[cal.compounding] mint gate unavailable ({exc}); refusing.")
        return False


__all__ = ["spread", "recompute_spread", "bucket_weight", "independent_total",
           "detect_new_project", "may_mint", "SPREAD_DEMOTE_ABOVE",
           "SPREAD_MIN_OBS", "MIN_DISTINCT_KEYS", "MIN_EVENTS", "MIN_DAYS"]
