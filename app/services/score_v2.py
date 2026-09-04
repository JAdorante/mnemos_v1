"""People v3 WS-B — connection score v2 + nightly v1-vs-v2 shadow diff.

v1 (`home_intelligence.person_score`) is linear in evidence counts, so one
chatty source (a podcast, a names-heavy document) buys rank with volume.
v2 changes three things, all weight-driven from data/score_config.json:

  * volume damping — every evidence mass goes through log1p (normalized so
    one unit of evidence scores exactly like v1's one unit);
  * dialogue-partner term — labeled speaker turns are evidence the user
    actually converses WITH someone, which mere mentions can never earn;
  * provenance weights — each evidence item is multiplied by where it came
    from (user-asserted > document > ASR) before damping, and the mention
    term is capped at `mention_cap_ratio` of the relationship evidence
    (typed + asserted + dialogue + co-occurrence): mentions corroborate a
    relationship, they cannot BE one. A pure mention-only profile scores 0.

Config loading is fail-closed (source_policy pattern): if
data/score_config.json is missing or corrupt, `config_loaded()` is False,
/health flags it, v2 scoring raises instead of guessing weights, and the
shadow job records nothing.

Rollout is the kg_parity pattern: QUILL_SCORE_SHADOW (default off) runs a
nightly job that scores everyone both ways and logs a comparison row into
`score_shadow_reports`; `cutover_ready()` demands 7 consecutive clean
nightlies (clean = v2's board passes the <=30% mention-share spec gate).
QUILL_SCORE_V2 (default off) switches the live /people/list ranking to v2
only once that gate holds.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[2] / "data" / "score_config.json"

_REQUIRED_WEIGHTS = ("typed", "mention", "cooccur", "asserted", "dialogue")
_REQUIRED_PROVENANCE = ("asserted", "document", "asr", "unknown")

# Speaker labels that are diarization bookkeeping, never a dialogue partner.
_NON_PARTNER_SPEAKERS = {"user", "you", "me", "self", "unknown", "speaker",
                         "speaker 1", "speaker 2", "assistant",
                         "mnemos", "sparrow", "ravenry"}

_attach_lock = threading.Lock()
_timer: threading.Timer | None = None
_INTERVAL_S = float(os.environ.get("QUILL_SCORE_SHADOW_INTERVAL_S", "86400")
                    or "86400")

CLEAN_STREAK_REQUIRED = 7


class ScoreV2NotReady(RuntimeError):
    """Raised when v2 scoring is asked for weights that failed to load."""


# --- flags (settings.score may be absent under SimpleNamespace patches) ------

def shadow_enabled() -> bool:
    return bool(getattr(getattr(settings, "score", None), "shadow", False))


def live_flag() -> bool:
    return bool(getattr(getattr(settings, "score", None), "live_v2", False))


# --- config (fail-closed, source_policy pattern) -----------------------------

@lru_cache(maxsize=1)
def _raw_config() -> dict:
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        weights = data.get("weights") or {}
        prov = data.get("provenance") or {}
        missing = ([k for k in _REQUIRED_WEIGHTS if k not in weights]
                   + [k for k in _REQUIRED_PROVENANCE if k not in prov])
        if missing:
            log.error(
                "score_config.json at %s is missing keys %s — v2 scoring is "
                "NOT ready (v1 stays live, shadow records nothing). Ship a "
                "complete data/score_config.json to restore it.",
                _CONFIG_PATH, missing)
            return {}
        return data
    except Exception as exc:
        log.error(
            "Could not load score config %s (%s) — v2 scoring is NOT ready "
            "(v1 stays live, shadow records nothing). Ship "
            "data/score_config.json to restore it.", _CONFIG_PATH, exc)
        return {}


def config_loaded() -> bool:
    """True when the shipped weight table parsed and validated."""
    return bool(_raw_config())


def config_version() -> str:
    cfg = _raw_config()
    return str(cfg.get("version") or "1") if cfg else "not-loaded"


def health() -> dict[str, Any]:
    """/health block — store-free on purpose (the endpoint must stay cheap)."""
    return {
        "config_loaded": config_loaded(),
        "version": config_version(),
        "shadow": shadow_enabled(),
        "live_flag": live_flag(),
    }


# --- score v2 (pure over edge dicts, like person_score_terms) ----------------

_LOG2 = math.log1p(1.0)


def _damp(mass: float) -> float:
    """Sublinear evidence growth, normalized so _damp(1) == 1.0 — one unit of
    evidence scores like v1; a hundred score ~6.7, not 100."""
    return math.log1p(max(float(mass), 0.0)) / _LOG2


def provenance_class(edge: dict) -> str:
    """Where an edge's evidence came from. Edges may carry a `source`
    annotation (events.source, attached by annotate_edge_sources / the eval);
    asserted origin always wins — the user said so."""
    if edge.get("origin") == "asserted":
        return "asserted"
    src = (edge.get("source") or "").lower()
    if src.startswith("audio") or src.startswith("phone"):
        return "asr"
    if src.startswith(("documents", "docs", "desktop")):
        return "document"
    if src.startswith(("onboarding", "user", "chat")):
        return "asserted"
    return "unknown"


def person_score_v2_terms(out_edges: list[dict], last_seen: float | None,
                          now: float, *, dialogue_turns: float = 0.0,
                          cfg: dict | None = None) -> dict:
    """v2 decomposition, same shape as person_score_terms (terms / base /
    recency_weight / score / mention_share) plus `mention_raw` (pre-cap)."""
    cfg = cfg if cfg is not None else _raw_config()
    if not cfg:
        raise ScoreV2NotReady(
            "data/score_config.json not loaded — refusing to score with "
            "junk weights")
    w = cfg["weights"]
    prov = cfg["provenance"]

    def _pw(edge: dict) -> float:
        return float(prov.get(provenance_class(edge), prov["unknown"]))

    # Per fact: predicate (typed wins over mentioned_in, as in v1) + the best
    # provenance weight seen for it.
    fact_pred: dict = {}
    fact_pw: dict = {}
    co_mass = 0.0
    asserted_ent = 0
    for e in out_edges or []:
        if e.get("obj_type") == "fact":
            fid = e["obj_id"]
            cur = fact_pred.get(fid)
            if cur is None or cur == "mentioned_in":
                fact_pred[fid] = e.get("predicate")
            fact_pw[fid] = max(fact_pw.get(fid, 0.0), _pw(e))
        elif (e.get("obj_type") == "person"
              and e.get("predicate") == "co_occurs"):
            co_mass += _pw(e) * float(e.get("weight") or 1)
        elif e.get("obj_type") == "entity" and e.get("origin") == "asserted":
            asserted_ent += 1
    typed_mass = sum(fact_pw[f] for f, p in fact_pred.items()
                     if p != "mentioned_in")
    mention_mass = sum(fact_pw[f] for f, p in fact_pred.items()
                       if p == "mentioned_in")

    terms = {
        "typed": float(w["typed"]) * _damp(typed_mass),
        "cooccur": float(w["cooccur"]) * _damp(co_mass),
        "asserted": float(w["asserted"]) * _damp(asserted_ent),
        "dialogue": float(w["dialogue"]) * _damp(dialogue_turns),
    }
    relationship = sum(terms.values())
    mention_raw = float(w["mention"]) * _damp(mention_mass)
    cap = float(cfg.get("mention_cap_ratio", 0.4)) * relationship
    terms["mentions"] = min(mention_raw, cap)
    base = relationship + terms["mentions"]

    half_life = float(cfg.get("recency_half_life_days", 30.0))
    floor = float(cfg.get("recency_floor", 0.35))
    age_d = (now - last_seen) / 86400.0 if last_seen else 90.0
    rec_w = floor + (1.0 - floor) * (0.5 ** (max(age_d, 0.0) / half_life))
    return {"terms": terms, "base": base, "recency_weight": rec_w,
            "score": base * rec_w, "mention_raw": mention_raw,
            "mention_share": (terms["mentions"] / base) if base > 0 else 0.0}


def person_score_v2(out_edges: list[dict], last_seen: float | None,
                    now: float, *, dialogue_turns: float = 0.0,
                    cfg: dict | None = None) -> float:
    return person_score_v2_terms(out_edges, last_seen, now,
                                 dialogue_turns=dialogue_turns,
                                 cfg=cfg)["score"]


# --- evidence collection (store-side) ----------------------------------------

def dialogue_turn_counts(store) -> dict[int, int]:
    """person_id -> labeled speaker-turn count. A person whose voice shows up
    as a turn speaker is someone the user converses with; being named in
    someone else's speech never lands here."""
    with store._lock:
        rows = store._conn.execute(
            "SELECT LOWER(TRIM(speaker)) AS s, COUNT(*) AS n FROM turns "
            "WHERE speaker IS NOT NULL AND TRIM(speaker) != '' "
            "GROUP BY LOWER(TRIM(speaker))").fetchall()
    by_label = {r["s"]: int(r["n"]) for r in rows
                if r["s"] not in _NON_PARTNER_SPEAKERS}
    counts: dict[int, int] = {}
    for p in store.all_people():
        keys = {(p.get("name") or "").strip().lower()}
        keys |= {(a or "").strip().lower() for a in (p.get("aliases") or [])}
        keys.discard("")
        counts[p["id"]] = sum(by_label.get(k, 0) for k in keys)
    return counts


def annotate_edge_sources(store, edges: list[dict]) -> list[dict]:
    """Attach events.source as edge['source'] so provenance_class can see it
    (edges only carry source_event_id)."""
    ids = sorted({int(e["source_event_id"]) for e in edges or []
                  if e.get("source_event_id")})
    if not ids:
        return edges
    src: dict[int, str] = {}
    with store._lock:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = ",".join("?" * len(chunk))
            for r in store._conn.execute(
                    f"SELECT id, source FROM events WHERE id IN ({ph})", chunk):
                src[int(r["id"])] = r["source"] or ""
    for e in edges or []:
        sev = e.get("source_event_id")
        if sev and int(sev) in src and "source" not in e:
            e["source"] = src[int(sev)]
    return edges


def score_people_v2(store, *, now: float | None = None) -> dict[int, dict]:
    """v2 term dicts for every person row (no floor filtering — callers
    decide board membership). Raises ScoreV2NotReady when config is absent."""
    cfg = _raw_config()
    if not cfg:
        raise ScoreV2NotReady("data/score_config.json not loaded")
    now = float(now if now is not None else time.time())
    dialogue = dialogue_turn_counts(store)
    out: dict[int, dict] = {}
    for p in store.all_people():
        rel = store.relations_of("person", p["id"])
        edges = annotate_edge_sources(store, rel.get("out") or [])
        out[p["id"]] = person_score_v2_terms(
            edges, p.get("last_seen"), now,
            dialogue_turns=dialogue.get(p["id"], 0), cfg=cfg)
    return out


# --- live read switch (routes.py /people/list) -------------------------------

def live_v2_enabled(store) -> bool:
    """v2 ranks live only when the flag is on AND weights loaded AND the
    shadow soak proved 7 clean nightlies. Any doubt -> v1."""
    if not live_flag() or not config_loaded():
        return False
    try:
        return bool(cutover_ready(store).get("ready"))
    except Exception:
        return False


def live_scores(store, now: float) -> dict[int, float] | None:
    """None -> caller stays on v1. Never raises: a scoring failure mid-request
    must degrade to v1, not 500 the People tab."""
    try:
        if not live_v2_enabled(store):
            return None
        return {pid: t["score"]
                for pid, t in score_people_v2(store, now=now).items()}
    except Exception as exc:
        log.error("score v2 live path failed (%s) — serving v1.", exc)
        return None


# --- shadow harness (kg_parity pattern) --------------------------------------

def run_shadow(store, *, now: float | None = None) -> dict[str, Any]:
    """Score everyone with v1 and v2, log one comparison row. Report-only —
    never changes a rank. When config failed to load, records NOTHING (a
    nightly built on junk weights must not count toward cutover)."""
    now = float(now if now is not None else time.time())
    if not config_loaded():
        print("[score_shadow] score_config.json not loaded — nightly skipped, "
              "nothing recorded.")
        return {"skipped": "config_not_loaded", "ts": now}

    from app.services.graph import _real_people
    from app.services.home_intelligence import person_score_terms, _SCORE_FLOOR
    from app.services.people_noise_metrics import MENTION_SHARE_MAX

    cfg = _raw_config()
    top_n = int(cfg.get("top_n", 12))
    v2_floor = float(cfg.get("score_floor", 1.0))

    self_pid = None
    try:
        from app.services.self_profile import self_person_id
        self_pid = self_person_id(store)
    except Exception:
        pass

    dialogue = dialogue_turn_counts(store)
    rows = []
    for p in _real_people(store):
        if p["id"] == self_pid:
            continue
        rel = store.relations_of("person", p["id"])
        edges = annotate_edge_sources(store, rel.get("out") or [])
        v1 = person_score_terms(edges, p.get("last_seen"), now)
        v2 = person_score_v2_terms(edges, p.get("last_seen"), now,
                                   dialogue_turns=dialogue.get(p["id"], 0),
                                   cfg=cfg)
        rows.append({"id": p["id"], "name": p["name"],
                     "v1": round(v1["score"], 3), "v2": round(v2["score"], 3),
                     "v1_share": round(v1["mention_share"], 3),
                     "v2_share": round(v2["mention_share"], 3),
                     "dialogue_turns": dialogue.get(p["id"], 0)})

    v1_board = [r for r in sorted(rows, key=lambda r: -r["v1"])
                if r["v1"] >= _SCORE_FLOOR][:top_n]
    v2_board = [r for r in sorted(rows, key=lambda r: -r["v2"])
                if r["v2"] >= v2_floor][:top_n]
    v1_ids = [r["id"] for r in v1_board]
    v2_ids = [r["id"] for r in v2_board]
    overlap = (len(set(v1_ids) & set(v2_ids)) / max(1, len(v1_ids))
               if v1_ids else 1.0)
    displacements = [abs(v1_ids.index(i) - v2_ids.index(i))
                     for i in set(v1_ids) & set(v2_ids)]
    worst_v2_share = max((r["v2_share"] for r in v2_board), default=0.0)
    worst_v1_share = max((r["v1_share"] for r in v1_board), default=0.0)
    # Clean = the thing v2 exists to fix, holding on real data: no one on the
    # v2 board rides on mentions past the spec gate.
    clean = worst_v2_share <= MENTION_SHARE_MAX

    deltas = sorted(rows, key=lambda r: -abs(r["v2"] - r["v1"]))[:50]
    report = {
        "ts": now, "people_scored": len(rows), "clean": clean,
        "top_n": top_n, "top_overlap": round(overlap, 3),
        "max_displacement": max(displacements, default=0),
        "worst_mention_share_v1": round(worst_v1_share, 3),
        "worst_mention_share_v2": round(worst_v2_share, 3),
        "v1_board": v1_ids, "v2_board": v2_ids,
        "deltas": deltas,
        "config_version": config_version(),
    }
    with store._lock:
        store._conn.execute(
            "INSERT INTO score_shadow_reports (ts, clean, report_json) "
            "VALUES (?, ?, ?)", (now, int(clean), json.dumps(report)))
        store._conn.commit()
    if not clean:
        print(f"[score_shadow] DIRTY nightly: worst v2 mention share "
              f"{worst_v2_share:.1%} > {MENTION_SHARE_MAX:.0%} — streak reset.")
    return report


def latest_reports(store, *, limit: int = CLEAN_STREAK_REQUIRED) -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM score_shadow_reports ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["report"] = json.loads(d.pop("report_json"))
        except Exception:
            pass
        out.append(d)
    return out


def cutover_ready(store) -> dict[str, Any]:
    """Read-cutover gate: 7 consecutive clean nightlies. A dirty nightly in
    the window resets the streak by construction (latest-7 must ALL be clean)."""
    reports = latest_reports(store, limit=CLEAN_STREAK_REQUIRED)
    ok = (len(reports) >= CLEAN_STREAK_REQUIRED
          and all(int(r["clean"]) for r in reports))
    return {"ready": ok, "reports": len(reports),
            "clean_in_window": sum(int(r["clean"]) for r in reports),
            "needed": CLEAN_STREAK_REQUIRED}


def status(store=None) -> dict[str, Any]:
    gate = {"ready": False, "reports": 0, "clean_in_window": 0,
            "needed": CLEAN_STREAK_REQUIRED}
    if store is not None:
        try:
            gate = cutover_ready(store)
        except Exception:
            pass
    return {**health(), "interval_s": _INTERVAL_S, "gate": gate}


# --- worker wiring (queue_ttl pattern) ---------------------------------------

def _store():
    from app.services.memory import memory
    return memory._ensure_store()


def run_job(_payload=None) -> None:
    """Worker handler for job kind `score_shadow`."""
    if not shadow_enabled():
        return
    run_shadow(_store())


def attach() -> None:
    """Nightly shadow enqueue while the flag is on (kg_parity pattern)."""
    if not shadow_enabled():
        return
    with _attach_lock:
        _schedule_next()
    print(f"[score_shadow] attached (every {int(max(60.0, _INTERVAL_S))}s).")


def _schedule_next() -> None:
    global _timer
    if not shadow_enabled():
        return
    delay = max(60.0, _INTERVAL_S)

    def _tick() -> None:
        try:
            from app.services.worker import worker
            worker.enqueue("score_shadow", unique=True)
        except Exception as exc:
            print(f"[score_shadow] schedule tick skipped ({exc}).")
        with _attach_lock:
            _schedule_next()

    t = threading.Timer(delay, _tick)
    t.daemon = True
    t.start()
    _timer = t
