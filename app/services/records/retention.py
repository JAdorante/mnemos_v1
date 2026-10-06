"""Tier 1 retention — the policy, the expiry stamp, and the enforcing sweep.

Raw capture expires by default; holds are the only thing that stops expiry.
Every event gets `expires_at` at insert (Store.insert -> capture_expires_at).
The nightly sweep then removes what has passed it, in dependency order:

  LanceDB event vectors -> audio files -> turns -> kg_evidence (a predicate
  left with no evidence becomes status='unsupported') -> the events row,
  replaced by an event_tombstones row (id, time, modality, source,
  privacy_class, content hash) so provenance reads "expired", not "missing".

Facts and claims derived from an expired event are NOT deleted — they are
Tier 2 with their own TTL. Claims citing the event get that evidence item
marked expired, and the org service is told the event refs (never content).

Mode (`QUILL_CAPTURE_EXPIRY`): `dry_run` (default) computes and receipts what
WOULD go and deletes nothing; `enforce` deletes; `off` skips. Rows captured
before the migration have NULL expires_at and never expire — enforcement
cannot sweep a seat's existing history in one night.

Not covered yet: desktop perception.db rows and CAS frames (desktop nodes
only; hosted seats have neither). The perception erasure job owns those.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from app.services.records.canonical import canonical_hash

DAY = 86400.0

# setting -> (default, lo, hi). hi None = unbounded. record_ttl_days is null
# (indefinite) by default and enforced by the org service, not here.
_POLICY_SPEC: dict[str, tuple[Any, Any, Any]] = {
    "capture_ttl_days": (30, 7, 365),
    "claim_ttl_days": (90, 30, 365),
    "audio_ttl_days": (7, 0, None),           # hi is capture_ttl_days
    "ambient_audio_ttl_days": (1, 0, None),   # matrix: ambient audio 24h
    "quote_in_records": (False, None, None),
}
_ENV = {
    "capture_ttl_days": "QUILL_CAPTURE_TTL_DAYS",
    "claim_ttl_days": "QUILL_CLAIM_TTL_DAYS",
    "audio_ttl_days": "QUILL_AUDIO_TTL_DAYS",
    "ambient_audio_ttl_days": "QUILL_AMBIENT_AUDIO_TTL_DAYS",
}
MODES = ("off", "dry_run", "enforce")
SWEEP_BATCH = 500

# Restrictiveness order for mixed-evidence claims: first match wins.
CAPTURE_SOURCES = ("ambient", "screen", "web", "external", "document",
                   "meeting")


def mode() -> str:
    raw = (os.environ.get("QUILL_CAPTURE_EXPIRY") or "dry_run").strip().lower()
    return raw if raw in MODES else "dry_run"


def _org_policy_path() -> Path:
    from app.config import settings
    return Path(settings.storage.data_dir) / "org_policy.json"


def org_policy() -> dict[str, Any]:
    """The last policy the Org Record Service pushed (heartbeat), or {}."""
    try:
        data = json.loads(_org_policy_path().read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_org_policy(policy: dict[str, Any]) -> None:
    p = _org_policy_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(policy, indent=2, sort_keys=True), "utf-8")
    os.replace(tmp, p)


def _clamp(name: str, value: Any, capture_ttl: float | None = None) -> Any:
    default, lo, hi = _POLICY_SPEC[name]
    if isinstance(default, bool):
        if isinstance(value, str):
            return value.strip().lower() in ("1", "on", "true", "yes")
        return bool(value)
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if name in ("audio_ttl_days", "ambient_audio_ttl_days"):
        hi = capture_ttl
    if lo is not None:
        v = max(float(lo), v)
    if hi is not None:
        v = min(float(hi), v)
    return v


def policy() -> dict[str, Any]:
    """Effective retention policy: defaults < env < org policy, clamped to the
    allowed ranges. `version` names it on receipts."""
    raw: dict[str, Any] = {k: spec[0] for k, spec in _POLICY_SPEC.items()}
    for k, env in _ENV.items():
        if os.environ.get(env):
            raw[k] = os.environ[env]
    retention = org_policy().get("retention") or {}
    for k in _POLICY_SPEC:
        if k in retention:
            raw[k] = retention[k]
    out: dict[str, Any] = {}
    out["capture_ttl_days"] = _clamp("capture_ttl_days", raw["capture_ttl_days"])
    for k in _POLICY_SPEC:
        if k != "capture_ttl_days":
            out[k] = _clamp(k, raw[k], out["capture_ttl_days"])
    out["version"] = canonical_hash(out)[:12]
    out["source"] = "org" if retention else "local"
    return out


def capture_source_of(source: str | None, modality: str | None = None,
                      meta: dict | None = None) -> str:
    """Which row of the per-source policy matrix an event falls under."""
    meta = meta or {}
    src = (source or "").lower()
    mod = (modality or "").lower()
    if meta.get("meeting_session_id") or src.startswith("meeting"):
        return "meeting"
    if src.startswith("audio") or mod == "audio":
        return "ambient"
    if src.startswith("desktop") or src.startswith("screen") or mod == "vision":
        return "screen"
    if src.startswith(("web", "browser", "research")):
        return "web"
    if src.startswith(("documents", "document", "notebook", "chat")):
        return "document"
    return "external"


def most_restrictive(sources: list[str]) -> str:
    for s in CAPTURE_SOURCES:
        if s in sources:
            return s
    return "external"


def capture_expires_at(event, *, pol: dict | None = None) -> float | None:
    """The deadline stamped on a new event: event time + capture TTL."""
    pol = pol or policy()
    try:
        t = float(event.time)
    except (TypeError, ValueError, AttributeError):
        t = time.time()
    return t + float(pol["capture_ttl_days"]) * DAY


# ------------------------------------------------------------------ sweep --
def _held(hold_ids: str | None) -> bool:
    if not hold_ids:
        return False
    try:
        return bool(json.loads(hold_ids))
    except ValueError:
        return True   # unparseable hold column: fail safe, keep the row


def _content_hash(row) -> str:
    h = hashlib.sha256()
    for k in ("raw", "summary", "meta"):
        h.update(str(row[k] or "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _due(store, now: float, limit: int) -> list:
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM events WHERE expires_at IS NOT NULL "
            "AND expires_at < ? AND (hold_ids IS NULL OR hold_ids IN ('', '[]')) "
            "ORDER BY expires_at LIMIT ?",
            (now, int(limit))).fetchall()
    # Belt and braces: a hold column SQL did not recognise as empty is kept.
    return [r for r in rows if not _held(r["hold_ids"])]


def _audio_paths(row) -> list[str]:
    paths = []
    if row["audio_path"]:
        paths.append(str(row["audio_path"]))
    try:
        meta = json.loads(row["meta"] or "{}")
    except ValueError:
        meta = {}
    for k in ("audio_path", "enhanced_audio_path", "frame_path"):
        if meta.get(k):
            paths.append(str(meta[k]))
    return sorted(set(paths))


def _inside(path: Path, roots: list[Path]) -> bool:
    try:
        rp = path.resolve()
    except OSError:
        return False
    return any(rp.is_relative_to(r) for r in roots)


def _default_vectors(store):
    """The real LanceDB only for the process store — a temp store in a test
    must never delete ids from the live index."""
    try:
        from app import storage as _st
        if getattr(_st, "_store", None) is store:
            from app.vectorstore import get_vectorstore
            return get_vectorstore()
    except Exception:
        pass
    return None


def _summarize(rows) -> dict[str, Any]:
    by_mod: dict[str, int] = {}
    for r in rows:
        by_mod[r["modality"]] = by_mod.get(r["modality"], 0) + 1
    times = [float(r["time"]) for r in rows]
    return {"n_events": len(rows), "by_modality": by_mod,
            "from": min(times) if times else None,
            "to": max(times) if times else None}


def _expire_batch(store, rows, *, now: float, pol: dict, vectors) -> dict:
    from app.services.records import node_store

    ids = [int(r["id"]) for r in rows]
    marks = ",".join("?" for _ in ids)
    out: dict[str, Any] = {"vectors": 0, "files": 0, "turns": 0,
                           "kg_evidence": 0, "unsupported_predicates": 0}
    # 1. vectors
    if vectors is not None:
        try:
            out["vectors"] = int(vectors.delete_ids(ids) or 0)
        except Exception as exc:
            out["vectors_error"] = str(exc)
    # 2. audio + frame files, only inside the store's own directories
    roots = [store.audio_dir.resolve(), store.db_path.parent.resolve()]
    for r in rows:
        for p in _audio_paths(r):
            path = Path(p)
            if path.is_file() and _inside(path, roots):
                try:
                    path.unlink()
                    out["files"] += 1
                except OSError:
                    pass
    expired = set(ids)
    with store._lock:
        # 3. turns whose text includes any expired utterance
        turn_ids = []
        for t in store._conn.execute(
                "SELECT id, event_ids FROM turns").fetchall():
            try:
                eids = {int(x) for x in json.loads(t["event_ids"] or "[]")}
            except (ValueError, TypeError):
                continue
            if eids & expired:
                turn_ids.append(int(t["id"]))
        for i in range(0, len(turn_ids), 500):
            chunk = turn_ids[i:i + 500]
            store._conn.execute(
                f"DELETE FROM turns WHERE id IN ({','.join('?' for _ in chunk)})",
                chunk)
        out["turns"] = len(turn_ids)
        # 4. kg_evidence; predicates left with none become unsupported
        out.update(store._forget_event_evidence_unlocked(ids, now))
        # 5. tombstone, then the row
        for r in rows:
            store._conn.execute(
                "INSERT OR REPLACE INTO event_tombstones (id, time, modality, "
                "source, privacy_class, content_hash, expired_at, "
                "policy_version) VALUES (?,?,?,?,?,?,?,?)",
                (int(r["id"]), float(r["time"]), r["modality"], r["source"],
                 r["privacy_class"], _content_hash(r), now, pol["version"]))
        store._conn.execute(f"DELETE FROM events WHERE id IN ({marks})", ids)
        store._conn.commit()
    # Timeline mirror + index: the same hook every other delete path reports to.
    from app.storage import _run_delete_hooks
    _run_delete_hooks(store, deleted_events=[(int(r["id"]), float(r["time"]))
                                             for r in rows])
    # 6. claims citing these events keep the pointer, marked expired
    _mark_claim_evidence_expired(store, expired)
    node_store.enqueue(store, "evidence_expired", f"expiry:{now:.3f}:{ids[0]}",
                       {"event_refs": ids, "expired_at": now})
    return out


def _mark_claim_evidence_expired(store, expired: set[int]) -> int:
    from app.services.records import node_store
    n = 0
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, evidence_json FROM claims").fetchall()
    for r in rows:
        try:
            ev = json.loads(r["evidence_json"] or "[]")
        except ValueError:
            continue
        changed = False
        for item in ev:
            if int(item.get("event_id") or -1) in expired and \
                    item.get("status") != "expired":
                item["status"] = "expired"
                changed = True
        if changed:
            node_store.update_claim(store, r["id"], evidence=ev)
            n += 1
    return n


def _audio_due(store, now: float, pol: dict, limit: int) -> list[int]:
    """Events still holding audio past their source's audio TTL."""
    meet_cut = now - float(pol["audio_ttl_days"]) * DAY
    amb_cut = now - float(pol["ambient_audio_ttl_days"]) * DAY
    with store._lock:
        rows = store._conn.execute(
            "SELECT id, time, source, modality, meta, hold_ids FROM events "
            "WHERE audio_path IS NOT NULL AND time < ? "
            "AND (hold_ids IS NULL OR hold_ids IN ('', '[]')) "
            "ORDER BY time LIMIT ?",
            (max(meet_cut, amb_cut), int(limit) * 4)).fetchall()
    out = []
    for r in rows:
        if _held(r["hold_ids"]):
            continue
        try:
            meta = json.loads(r["meta"] or "{}")
        except ValueError:
            meta = {}
        src = capture_source_of(r["source"], r["modality"], meta)
        cut = meet_cut if src == "meeting" else amb_cut
        if float(r["time"]) < cut:
            out.append(int(r["id"]))
    return out[:limit]


def sweep(store=None, *, now: float | None = None, vectors=None,
          run_mode: str | None = None, max_batches: int = 20) -> dict[str, Any]:
    """One expiry pass. Writes a receipt to the node audit log either way."""
    from app.services.records import node_store
    if store is None:
        from app.storage import get_store
        store = get_store()
    run_mode = run_mode or mode()
    now = float(now if now is not None else time.time())
    pol = policy()
    result: dict[str, Any] = {"ok": True, "mode": run_mode,
                              "policy_version": pol["version"], "at": now}
    if run_mode == "off":
        result["skipped"] = "off"
        return result
    if vectors is None:
        vectors = _default_vectors(store)

    if run_mode == "dry_run":
        rows = _due(store, now, SWEEP_BATCH * max_batches)
        audio = _audio_due(store, now, pol, SWEEP_BATCH * max_batches)
        result.update(would_expire=_summarize(rows),
                      would_strip_audio=len(audio))
        node_store.audit(store, "system", "expiry.dry_run", "events", {
            **result["would_expire"], "audio_events": len(audio),
            "policy_version": pol["version"]}, at=now)
        return result

    totals: dict[str, Any] = {"n_events": 0, "by_modality": {}, "from": None,
                              "to": None, "vectors": 0, "files": 0,
                              "turns": 0, "kg_evidence": 0,
                              "unsupported_predicates": 0}
    for _ in range(max_batches):
        rows = _due(store, now, SWEEP_BATCH)
        if not rows:
            break
        summ = _summarize(rows)
        done = _expire_batch(store, rows, now=now, pol=pol, vectors=vectors)
        totals["n_events"] += summ["n_events"]
        for m, n in summ["by_modality"].items():
            totals["by_modality"][m] = totals["by_modality"].get(m, 0) + n
        for edge, pick in (("from", min), ("to", max)):
            vals = [v for v in (totals[edge], summ[edge]) if v is not None]
            totals[edge] = pick(vals) if vals else None
        for k in ("vectors", "files", "turns", "kg_evidence",
                  "unsupported_predicates"):
            totals[k] += int(done.get(k) or 0)
    audio = _audio_due(store, now, pol, SWEEP_BATCH * max_batches)
    stripped = store.strip_event_audio(audio) if audio else {"n_files": 0}
    totals["audio_stripped_events"] = len(audio)
    totals["audio_stripped_files"] = int(stripped.get("n_files") or 0)
    result["expired"] = totals
    node_store.audit(store, "system", "expiry.receipt", "events",
                     {**totals, "policy_version": pol["version"]}, at=now)
    return result


def tombstone(store, event_id: int) -> dict | None:
    with store._lock:
        r = store._conn.execute(
            "SELECT * FROM event_tombstones WHERE id = ?",
            (int(event_id),)).fetchone()
    return dict(r) if r else None


def status(store=None, *, now: float | None = None) -> dict[str, Any]:
    """GET /retention/status: upcoming expiries by source, holds, last receipt."""
    from app.services.records import node_store
    if store is None:
        from app.storage import get_store
        store = get_store()
    now = float(now if now is not None else time.time())
    pol = policy()
    horizons = {"24h": now + DAY, "7d": now + 7 * DAY, "30d": now + 30 * DAY}
    upcoming: dict[str, dict[str, int]] = {}
    with store._lock:
        rows = store._conn.execute(
            "SELECT source, modality, meta, expires_at, hold_ids FROM events "
            "WHERE expires_at IS NOT NULL AND expires_at < ?",
            (horizons["30d"],)).fetchall()
        held = store._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE hold_ids IS NOT NULL "
            "AND hold_ids NOT IN ('', '[]')").fetchone()["n"]
        never = store._conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE expires_at IS NULL"
        ).fetchone()["n"]
        tombs = store._conn.execute(
            "SELECT COUNT(*) AS n FROM event_tombstones").fetchone()["n"]
    for r in rows:
        if _held(r["hold_ids"]):
            continue
        try:
            meta = json.loads(r["meta"] or "{}")
        except ValueError:
            meta = {}
        src = capture_source_of(r["source"], r["modality"], meta)
        bucket = upcoming.setdefault(src, {k: 0 for k in horizons})
        for k, edge in horizons.items():
            if float(r["expires_at"]) < edge:
                bucket[k] += 1
    receipts = (node_store.audit_entries(store, action="expiry.receipt", limit=1)
                or node_store.audit_entries(store, action="expiry.dry_run",
                                            limit=1))
    return {"ok": True, "mode": mode(), "policy": pol,
            "upcoming_by_source": upcoming, "held_events": int(held),
            "never_expire_events": int(never), "tombstones": int(tombs),
            "holds": list((org_policy().get("holds") or [])),
            "last_receipt": receipts[0] if receipts else None}
