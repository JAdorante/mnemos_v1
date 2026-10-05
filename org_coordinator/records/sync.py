"""Write-back sync: preview at proposal, jobs at record time, worker, drift.

  preview      POST /packets/preview — for every active connector with a
               mapping for (kind, predicate) and a resolvable target, the
               plan plus a field diff against the external system's current
               value. The node embeds the list in payload["preview"] BEFORE
               hashing, so the approver signs the exact external change.
  submission   the service recomputes every plan (no network) and refuses
               `preview_stale` when the signed preview no longer matches what
               would be written (mapping edited, connector added/removed);
               the node re-mints for re-approval.
  jobs         one sync_job per preview entry, key payload_hash:version.
  worker       claims a due job (SKIP LOCKED), writes, reads back, verifies.
               A field that moved since the preview -> `conflict`, never an
               overwrite. Transient errors back off over ~24h (6 attempts),
               then `failed` with an admin alert; permanent errors fail now.
  drift        nightly read-back of every field Sparrow verified in the last
               90 days; an external change Sparrow did not make opens a drift
               notice for the scope's approvers and marks the record
               externally_modified. Nothing is ever written back over it.
"""
from __future__ import annotations

import json
import time
from typing import Any

from org_coordinator import connectors as conn_mod
from org_coordinator.connectors.base import DRIFTABLE_OPS, plan_core
from org_coordinator.connectors.mapping import find_mapping
from org_coordinator.records import audit, tokens

# Six attempts over roughly a day: 1m, 5m, 30m, 2h, 6h, 14h (~22.6h total).
BACKOFF_S = (60.0, 300.0, 1800.0, 7200.0, 21600.0, 50400.0)
MAX_ATTEMPTS = len(BACKOFF_S)
DRIFT_WINDOW_S = 90 * 86400.0


def version_view(payload: dict, *, record_version_id: str | None = None) -> dict:
    """What a connector maps from: the approved record body."""
    return {"kind": payload.get("kind"), "predicate": payload.get("predicate"),
            "value": payload.get("value"),
            "subject_ref": payload.get("subject_ref"),
            "subject_label": payload.get("subject_label"),
            "valid_from": payload.get("valid_from"),
            "scope_id": payload.get("scope_id"),
            "packet_id": payload.get("packet_id"),
            "record_version_id": record_version_id}


def _plans(repo, payload: dict) -> list[tuple[dict, Any, dict]]:
    """(connector row, live connector, plan) for every connector this record
    maps to. No network: building and mapping only."""
    scope = repo.get_scope(payload.get("scope_id") or "") or {}
    mappings = repo.mappings()
    out = []
    for row in repo.list_connectors(active_only=True):
        m = find_mapping([x for x in mappings if x["connector_id"] == row["id"]],
                         payload.get("kind") or "", payload.get("predicate") or "")
        if m is None:
            continue
        live = conn_mod.build(row)
        if not live.supports(payload.get("kind"), payload.get("predicate"),
                             m["op"]):
            continue
        target = live.resolve_target(scope, payload.get("subject_ref") or "",
                                     payload.get("target_ref"))
        if not target:
            continue
        out.append((row, live, live.map_record(version_view(payload),
                                               target, m)))
    return sorted(out, key=lambda t: t[0]["id"])


def previews(repo, payload: dict) -> list[dict]:
    """Plans + current external values, for the approver to see and sign."""
    out = []
    for _row, live, plan in _plans(repo, payload):
        out.append({**plan_core(plan), "diff": live.preview(plan)})
    return out


def check_preview(repo, payload: dict) -> None:
    """Refuse a payload whose signed preview no longer matches the writes the
    service would make now."""
    from org_coordinator.records.service import ServiceError
    signed = payload.get("preview") or []
    if not isinstance(signed, list):
        raise ServiceError(422, "invalid_payload", "preview must be a list")
    want = [plan_core(p) for _r, _l, p in _plans(repo, payload)]
    got = [plan_core(p) for p in signed]
    if want != got:
        raise ServiceError(409, "preview_stale",
                           "write-back changed since the preview was signed")


def enqueue(repo, *, version_id: str, payload: dict, payload_hash: str,
            version_no: int, proposed_by: str | None, now: float) -> list[str]:
    ids = []
    key = f"{payload_hash}:{version_no}"
    for entry in payload.get("preview") or []:
        job_id = tokens.new_id("sj")
        inserted = repo.insert_sync_job(
            id=job_id, record_version_id=version_id,
            connector_id=entry["connector_id"], target_ref=entry["target"],
            plan_json=json.dumps(plan_core(entry)),
            preview_json=json.dumps(entry), idempotency_key=key,
            proposed_by=proposed_by, created_at=now)
        if inserted:
            repo.queue_put(job_id, now)
            ids.append(job_id)
    return ids


def _expected(job: dict) -> dict | None:
    preview = job.get("preview_json") or {}
    diff = preview.get("diff") or []
    if (job.get("plan_json") or {}).get("op") not in DRIFTABLE_OPS:
        return None
    return {d["field"]: d.get("before") for d in diff}


def _alert(repo, kind: str, ref: str, message: str, now: float) -> None:
    repo.insert_alert(id=tokens.new_id("al"), kind=kind, object_ref=ref,
                      message=message[:500], created_at=now)


def run_one(db, *, now: float | None = None) -> dict | None:
    """Process one due job. None when the queue has nothing due."""
    now = float(now if now is not None else time.time())
    with db.claim_sync_job(now) as claimed:
        if claimed is None:
            return None
        repo, job_id = claimed
        job = repo.get_sync_job(job_id)
        if job is None or job["state"] not in ("pending", "writing"):
            repo.queue_delete(job_id)
            return {"job_id": job_id, "state": "gone"}
        row = repo.get_connector(job["connector_id"])
        plan = job["plan_json"]
        attempts = int(job["attempts"]) + 1
        try:
            if row is None or row["status"] != "active":
                raise conn_mod.PermanentError("connector disabled or removed")
            live = conn_mod.build(row)
            result = live.write(plan, job["idempotency_key"],
                                expected=_expected(job))
            back = live.read_back(plan)
            if not live.verified(plan, back):
                raise conn_mod.TransientError("read-back does not match the write")
        except conn_mod.ConflictError as exc:
            repo.update_sync_job(job_id, state="conflict", attempts=attempts,
                                 last_error=str(exc)[:500], updated_at=now)
            repo.queue_delete(job_id)
            audit.append(repo, "sync", "sync.conflict", job_id,
                         {"field": exc.field}, at=now)
            return {"job_id": job_id, "state": "conflict"}
        except conn_mod.TransientError as exc:
            if attempts >= MAX_ATTEMPTS:
                repo.update_sync_job(job_id, state="failed", attempts=attempts,
                                     last_error=str(exc)[:500], updated_at=now)
                repo.queue_delete(job_id)
                _alert(repo, "sync_failed", job_id,
                       f"write-back failed after {attempts} attempts: {exc}", now)
                audit.append(repo, "sync", "sync.failed", job_id,
                             {"attempts": attempts}, at=now)
                return {"job_id": job_id, "state": "failed"}
            repo.update_sync_job(job_id, state="pending", attempts=attempts,
                                 last_error=str(exc)[:500], updated_at=now)
            repo.queue_put(job_id, now + BACKOFF_S[attempts - 1])
            return {"job_id": job_id, "state": "pending", "retry": attempts}
        except conn_mod.ConnectorError as exc:
            repo.update_sync_job(job_id, state="failed", attempts=attempts,
                                 last_error=str(exc)[:500], updated_at=now)
            repo.queue_delete(job_id)
            _alert(repo, "sync_failed", job_id, f"write-back refused: {exc}", now)
            audit.append(repo, "sync", "sync.failed", job_id,
                         {"attempts": attempts, "permanent": True}, at=now)
            return {"job_id": job_id, "state": "failed"}
        repo.update_sync_job(
            job_id, state="verified", attempts=attempts, last_error=None,
            external_version=str(result.get("external_version") or "") or None,
            written_json=result.get("written"), updated_at=now,
            verified_at=now)
        repo.queue_delete(job_id)
        audit.append(repo, "sync", "sync.write", job_id,
                     {"skipped": result.get("skipped") or []}, at=now)
        audit.append(repo, "sync", "sync.verify", job_id, {}, at=now)
        return {"job_id": job_id, "state": "verified"}


def drain(db, *, now: float | None = None, limit: int = 500) -> dict:
    counts: dict[str, int] = {}
    for _ in range(limit):
        out = run_one(db, now=now)
        if out is None:
            break
        counts[out["state"]] = counts.get(out["state"], 0) + 1
    return counts


def drift_sweep(db, org_id: str, *, now: float | None = None) -> dict:
    """Nightly: read back every field Sparrow verified in the window. Only the
    LATEST verified write per (target, field) counts — an older write that a
    newer Sparrow write replaced is not drift."""
    now = float(now if now is not None else time.time())
    out = {"checked": 0, "drift": 0, "resolved": 0, "errors": 0}
    with db.tenant(org_id) as repo:
        jobs = repo.verified_jobs_since(now - DRIFT_WINDOW_S)
        latest: dict[tuple[str, str], dict] = {}
        for j in jobs:
            plan = j["plan_json"] or {}
            if plan.get("op") not in DRIFTABLE_OPS:
                continue
            for field in (plan.get("fields") or {}):
                latest[(j["target_ref"], field)] = j
        by_job: dict[str, list[str]] = {}
        for (_t, field), j in latest.items():
            by_job.setdefault(j["id"], []).append(field)
        for job_id, fields in by_job.items():
            job = repo.get_sync_job(job_id)
            row = repo.get_connector(job["connector_id"])
            if row is None or row["status"] != "active":
                continue
            try:
                back = conn_mod.build(row).read_back(job["plan_json"])
            except conn_mod.ConnectorError:
                out["errors"] += 1
                continue
            version = repo.get_version(job["record_version_id"])
            record = repo.get_record(version["record_id"])
            open_by_field = {d["field"]: d for d in repo.drift_for_job(job_id)
                             if d["resolved_at"] is None}
            for field in fields:
                out["checked"] += 1
                wrote = (job["plan_json"].get("fields") or {}).get(field)
                now_val = back.get(field)
                if now_val == wrote:
                    if field in open_by_field:
                        repo.resolve_drift(open_by_field[field]["id"], now)
                        out["resolved"] += 1
                    continue
                if field in open_by_field:
                    continue
                if repo.insert_drift(
                        id=tokens.new_id("dr"), record_id=record["id"],
                        sync_job_id=job_id, scope_id=record["scope_id"],
                        field=field, written_value=json.dumps(wrote),
                        external_value=json.dumps(now_val), detected_at=now):
                    repo.set_record_drift(record["id"], "externally_modified")
                    audit.append(repo, "sync", "drift.detected", record["id"],
                                 {"field": field, "job": job_id}, at=now)
                    out["drift"] += 1
        still_open = {d["record_id"] for d in repo.open_drift()}
        for job_id in by_job:
            job = repo.get_sync_job(job_id)
            rid = repo.get_version(job["record_version_id"])["record_id"]
            if rid not in still_open:
                repo.set_record_drift(rid, None)
    return out
