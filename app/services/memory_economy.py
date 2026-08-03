"""Memory economy (Track C) — event lifecycle, retention scoring, compaction.

The roadmap's C1/C2 mechanical layer, shipped observe-first:

  lifecycle   fresh -> absorbed -> compacted (tombstoned reserved; nothing is
              deleted in v1). 'absorbed' = the extractor has represented the
              event in a derived layer (facts/turns) AND it has aged past the
              absorb window. NULL lifecycle reads as 'fresh' — no backfill.

  retention   nightly score per event: how much keeping the raw text still
              buys us. Importance (source class x confidence) sets the base;
              derived-fact footprint, ledger recall, and linked-node value
              extend the half-life; age decays it. Metadata only — retrieval
              never filters on it in v1.

  compaction  QUILL_COMPACTION (default OFF) replaces an absorbed event's raw
              with a SPAN-PRESERVING stub (I-1: every citing fact's verbatim
              source_span is embedded) after archiving the full original row —
              fully reversible via restore(). Events with open citing facts
              are never compacted. Per-run churn cap.

  growth      storage-growth snapshots (db + lance bytes, row counts) — the
              track's first-class metric (exit: sublinear over 4 weeks).

Retention scoring already feeds ledger recall / V / open-work / contradiction
fraction into `retention_score`; learned-forget aggressiveness still waits on
months of ledger signal.
"""
from __future__ import annotations

import json
import math
import threading
import time
from pathlib import Path
from typing import Any

# Source-class importance: how much a raw event of this provenance tends to
# matter once its facts are extracted. Perception floods (screen/click) decay
# fastest; things the user typed or filed decay slowest.
SOURCE_W = {
    "chat": 1.0,
    "documents": 0.95,
    "audio.mic": 0.90,
    "phone": 0.90,
    "calendar": 0.90,
    "notebook": 0.90,
    "audio.system": 0.75,
    "desktop.screen": 0.55,
    "desktop.click": 0.45,
}
SOURCE_W_DEFAULT = 0.80

# Base half-life (days) for an absorbed event nobody engages with again.
HALF_LIFE_DAYS = 120.0
RETENTION_MIN, RETENTION_MAX = 0.02, 1.0
# Score floor for events backing open work — never compaction candidates.
OPEN_WORK_FLOOR = 0.90

SWEEP_BATCH = 5000


def _cfg():
    from app.config import settings
    return settings.economy


def _source_weight(source: str | None) -> float:
    s = (source or "").lower()
    for prefix, w in SOURCE_W.items():
        if s.startswith(prefix):
            return w
    return SOURCE_W_DEFAULT


def retention_score(*, age_days: float, confidence: float | None,
                    source: str | None, absorbed: bool, n_facts: int = 0,
                    recall_n: int = 0, v_max: float = 0.0,
                    has_open: bool = False,
                    contradiction: float = 0.0) -> float:
    """How much keeping this raw event still buys us, in [0.02, 1].

    Roadmap C1 formula (importance x recall x relationship x novelty −
    contradiction) with the shape adapted to what exists today: the
    multiplicative terms extend the HALF-LIFE rather than the base, so
    "things that mattered decay slower" instead of "everything old is zero".
    recall_n / v_max default to 0 — the ledger feeds them as it matures.
    """
    conf = max(0.05, min(1.0, float(confidence if confidence is not None else 0.6)))
    importance = _source_weight(source) * (0.35 + 0.65 * conf)

    # Un-absorbed events are still the ONLY copy of their content — hold high.
    if not absorbed:
        base = max(importance, 0.75)
    else:
        base = importance

    boost = 1.0
    boost += 0.25 * min(4, max(0, int(n_facts)))          # derived footprint
    boost += 0.15 * min(6, max(0, int(recall_n)))         # ledger recall
    boost += 0.50 * max(0.0, min(1.0, float(v_max)))      # linked-node value
    half_life = HALF_LIFE_DAYS * boost

    score = base * math.pow(0.5, max(0.0, float(age_days)) / half_life)
    score -= 0.10 * max(0.0, min(1.0, float(contradiction)))
    if has_open:
        score = max(score, OPEN_WORK_FLOOR)
    return max(RETENTION_MIN, min(RETENTION_MAX, score))


def build_stub(event: dict, spans: list[dict], *, now: float) -> str:
    """The span-preserving replacement text (I-1): a dated marker, the gist,
    and every citing fact's VERBATIM source_span. Facts also carry their own
    span copy — embedding them here is belt-and-braces, so the raw event
    remains greppable evidence even after compaction."""
    day = time.strftime("%Y-%m-%d", time.localtime(now))
    gist = (event.get("summary") or "").strip()
    if not gist:
        gist = (event.get("raw") or "").strip()[:160]
    lines = [f"[compacted {day}] {gist}".rstrip()]
    seen: set[str] = set()
    for f in spans:
        span = (f.get("source_span") or "").strip()
        if not span or span in seen:
            continue
        seen.add(span)
        lines.append(f"  span: \"{span}\"")
    return "\n".join(lines)


def compact_one(store, event_id: int, *, now: float | None = None) -> dict:
    """Compact a single event, with protections. Returns {ok|skipped, reason}."""
    now = float(now if now is not None else time.time())
    spans = store.fact_spans_for_event(int(event_id))
    for f in spans:
        open_work = ((f.get("status") or "") == "open"
                     and (f.get("state") or "active") == "active")
        if open_work:
            return {"ok": False, "skipped": True, "reason": "open_facts",
                    "event_id": int(event_id)}
    event = store.get_event(int(event_id))
    if event is None:
        return {"ok": False, "skipped": True, "reason": "missing",
                "event_id": int(event_id)}
    if (event.get("lifecycle") or "fresh") == "compacted":
        return {"ok": False, "skipped": True, "reason": "already_compacted",
                "event_id": int(event_id)}
    stub = build_stub(event, spans, now=now)
    ok = store.compact_event(int(event_id), stub, now)
    return {"ok": bool(ok), "event_id": int(event_id),
            "n_spans": len({(f.get("source_span") or "").strip()
                            for f in spans if (f.get("source_span") or "").strip()})}


def restore(store, event_id: int) -> bool:
    """Reverse a compaction — the original raw comes back verbatim."""
    return bool(store.restore_event(int(event_id)))


def _dir_bytes(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        return total
    return total


def growth_snapshot(store, *, now: float | None = None,
                    force: bool = False) -> dict | None:
    """Record one point on the storage-growth curve (throttled)."""
    from app.config import settings
    now = float(now if now is not None else time.time())
    last = store.last_storage_growth()
    if not force and last and now - float(last["ts"]) < _cfg().growth_every_s:
        return None
    db_path = Path(getattr(store, "db_path", settings.storage.db_path))
    db_bytes = 0
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        try:
            if p.exists():
                db_bytes += p.stat().st_size
        except OSError:
            pass
    lance = Path(settings.memory.lance_dir)
    lance_bytes = _dir_bytes(lance) if lance.exists() else 0
    counts = store.table_counts()
    snap = {
        "ts": now, "db_bytes": int(db_bytes), "lance_bytes": int(lance_bytes),
        "n_events": counts["events"], "n_facts": counts["facts"],
        "n_turns": counts["turns"], "n_compacted": counts["events_archive"],
    }
    store.add_storage_growth(snap)
    return snap


def sweep(store=None, *, now: float | None = None) -> dict:
    """The nightly pass: score retention, advance lifecycles, list compaction
    candidates, compact only when QUILL_COMPACTION is on (capped), snapshot
    growth. Never raises — worker-job contract."""
    cfg = _cfg()
    result: dict[str, Any] = {
        "enabled": cfg.enabled, "compaction": cfg.compaction,
        "scored": 0, "absorbed": 0, "candidates": 0, "compacted": 0,
        "skipped_open": 0,
    }
    if not cfg.enabled:
        result["reason"] = "disabled"
        return result
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            result["reason"] = f"no_store:{exc}"
            return result
    now = float(now if now is not None else time.time())
    result["ts"] = now

    try:
        events = store.events_for_economy(limit=SWEEP_BATCH)
    except Exception as exc:
        print(f"[memory_economy] sweep read failed ({exc}).")
        result["reason"] = str(exc)
        return result

    updates: list[tuple[int, str | None, float]] = []
    candidates: list[dict] = []
    signals: dict[int, dict] = {}
    try:
        signals = store.economy_signals_for_events(
            [int(e["id"]) for e in events])
    except Exception as exc:
        print(f"[memory_economy] ledger signals skipped ({exc}).")

    for e in events:
        age_days = max(0.0, (now - float(e["time"])) / 86400.0)
        absorbed_marker = e.get("extracted_at") is not None
        lifecycle = e.get("lifecycle") or "fresh"
        new_lifecycle: str | None = None
        if (lifecycle == "fresh" and absorbed_marker
                and age_days > cfg.absorb_after_days):
            new_lifecycle = "absorbed"
            lifecycle = "absorbed"
        sig = signals.get(int(e["id"])) or {}
        score = retention_score(
            age_days=age_days, confidence=e.get("confidence"),
            source=e.get("source"), absorbed=(lifecycle != "fresh"),
            n_facts=int(e.get("n_facts") or 0),
            recall_n=int(sig.get("recall_n") or 0),
            v_max=float(sig.get("v_max") or 0),
            has_open=bool(sig.get("has_open")),
            contradiction=float(sig.get("contradiction") or 0),
        )
        updates.append((int(e["id"]), new_lifecycle, score))
        if new_lifecycle:
            result["absorbed"] += 1
        if (lifecycle == "absorbed" and age_days > cfg.compact_after_days
                and score < cfg.retention_threshold
                and not sig.get("has_open")):
            candidates.append({"id": int(e["id"]), "retention": round(score, 3),
                               "time": e["time"], "source": e.get("source"),
                               "recall_n": int(sig.get("recall_n") or 0)})

    try:
        result["scored"] = store.apply_event_retention(updates, now)
    except Exception as exc:
        print(f"[memory_economy] retention persist failed ({exc}).")
    result["candidates"] = len(candidates)
    result["candidate_preview"] = candidates[:20]

    if cfg.compaction and candidates:
        done = 0
        for c in candidates:
            if done >= cfg.compact_max_per_run:
                break
            try:
                r = compact_one(store, c["id"], now=now)
            except Exception as exc:
                print(f"[memory_economy] compact {c['id']} failed ({exc}).")
                continue
            if r.get("ok"):
                done += 1
            elif r.get("reason") == "open_facts":
                result["skipped_open"] += 1
        result["compacted"] = done

    try:
        growth_snapshot(store, now=now)
    except Exception as exc:
        print(f"[memory_economy] growth snapshot failed ({exc}).")
    try:
        store.add_economy_run(result)
    except Exception as exc:
        print(f"[memory_economy] run persist failed ({exc}).")
    return result


def due_for(store=None) -> bool:
    if not _cfg().enabled:
        return False
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception:
            return False
    try:
        last = store.last_economy_run()
    except Exception:
        return False
    if not last:
        return True
    return (time.time() - float(last["ts"])) > _cfg().due_after_s


def status(store=None) -> dict:
    """Console payload: lifecycle counts, last sweep, candidates, growth curve,
    forgotten-this-month review list, policy flags."""
    cfg = _cfg()
    out: dict[str, Any] = {
        "enabled": cfg.enabled,
        "compaction": cfg.compaction,
        "thresholds": {
            "absorb_after_days": cfg.absorb_after_days,
            "compact_after_days": cfg.compact_after_days,
            "retention_threshold": cfg.retention_threshold,
            "compact_max_per_run": cfg.compact_max_per_run,
        },
    }
    if store is None:
        try:
            from app.storage import get_store
            store = get_store()
        except Exception as exc:
            out["error"] = str(exc)
            return out
    try:
        out["lifecycle"] = store.events_lifecycle_stats()
    except Exception as exc:
        out["lifecycle"] = {"error": str(exc)}
    try:
        last = store.last_economy_run()
        if last:
            try:
                last["detail"] = json.loads(last.get("detail") or "{}")
            except Exception:
                pass
        out["last_sweep"] = last
        out["due"] = due_for(store)
    except Exception:
        out["last_sweep"] = None
    try:
        out["growth"] = store.list_storage_growth(limit=60)
    except Exception:
        out["growth"] = []
    try:
        month_ago = time.time() - 30 * 86400
        out["forgotten_this_month"] = store.compacted_events(
            since=month_ago, limit=100)
    except Exception:
        out["forgotten_this_month"] = []
    try:
        from app.vectorstore import get_vectorstore
        out["lance"] = get_vectorstore().lance_status()
    except Exception as exc:
        out["lance"] = {"ok": False, "error": str(exc)}
    return out


# Mid-uptime due-check — boot enqueues once; this covers processes up >due_after_s.
_attach_lock = threading.Lock()
_timer: threading.Timer | None = None
_CHECK_EVERY_S = 3600.0


def attach() -> None:
    """Hourly due_for() → enqueue memory_economy (unique). No-op if disabled."""
    if not _cfg().enabled:
        return
    with _attach_lock:
        _schedule_next(immediate=False)
    print(f"[memory_economy] due-check attached (every {int(_CHECK_EVERY_S)}s).")


def _schedule_next(*, immediate: bool = False) -> None:
    global _timer
    delay = 30.0 if immediate else _CHECK_EVERY_S

    def _tick() -> None:
        try:
            if due_for():
                from app.services.worker import worker
                worker.enqueue("memory_economy", unique=True)
        except Exception as exc:
            print(f"[memory_economy] due-check skipped ({exc}).")
        with _attach_lock:
            _schedule_next(immediate=False)

    t = threading.Timer(delay, _tick)
    t.daemon = True
    t.start()
    _timer = t
