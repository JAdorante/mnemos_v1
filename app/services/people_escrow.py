"""People v3 P3 (WS-A) — voice-track escrow + retroactive rebind.

"Evidence must earn identity": an unbound diarization track ("Speaker 3") never
mints a Person, but what that voice said should not be lost either. Behind
QUILL_PEOPLE_ESCROW (default OFF):

- The extractor keeps facts whose subject is the unbound speaker, attributing
  them to a durable `speaker_tracks` row instead of a person. The fact row is
  marked `state='escrowed'` (plus `facts.speaker_track_id` and the
  `tasks.owner_track_id` / `commitments.from_track_id` twins), which keeps it
  out of grounding, retrieval, people scoring and the constellation — every
  default query already filters on the pre-existing `state` column.
- When the track is bound to a named person (`label_speaker`, or a person
  merge re-pointing a bound track), a durable `people_escrow_rebind` job
  (jobs table + JobWorker) rewrites the escrowed rows to the person id and
  flips them back to `state='active'`, entering the SAME review flow any ASR
  fact would (review stays NULL — no tier promotion). Each run is idempotent
  and logs what it did to `escrow_rebind_log`.

Flag OFF = byte-identical behavior: the extractor's unbound-speaker paths are
untouched and none of the new columns are read or written.
"""
from __future__ import annotations

import json
import re
import time

from app.config import settings

# The provisional labels speakers.py mints for anonymous clusters.
_TRACK_LABEL_RE = re.compile(r"^speaker \d+$", re.IGNORECASE)

JOB_KIND = "people_escrow_rebind"


def enabled() -> bool:
    """QUILL_PEOPLE_ESCROW gate. getattr-chained: older suites patch settings
    sub-objects with SimpleNamespace, so never touch attributes directly."""
    cfg = getattr(settings, "people_escrow", None)
    return bool(getattr(cfg, "enabled", False))


def is_provisional_label(label: str | None) -> bool:
    """True for an anonymous diarization label ("Speaker 3") — a voice track
    that has not earned an identity yet."""
    return bool(_TRACK_LABEL_RE.match((label or "").strip()))


def track_for_turn(store, turn, now: float | None = None) -> int | None:
    """The durable track id for this turn's provisional speaker label
    (created on first use), or None when the turn has no provisional label
    or the flag is off. The extractor calls this once per persisted turn."""
    if not enabled():
        return None
    label = (getattr(turn, "speaker", None)
             if not isinstance(turn, dict) else turn.get("speaker")) or ""
    label = label.strip()
    if not is_provisional_label(label):
        return None
    try:
        return store.get_or_create_speaker_track(
            label, ts=now if now is not None else time.time())
    except Exception as exc:
        print(f"[people_escrow] track lookup skipped ({exc}).")
        return None


def label_speaker(store, label: str, name: str, *, actor: str = "user",
                  now: float | None = None) -> dict:
    """The speaker-labeling flow: bind the OPEN track for a provisional label
    ("Speaker 3") to a named person and enqueue the durable rebind job.

    Returns {ok, track_id, person_id, job_id} or {ok: False, error}.
    """
    now = now if now is not None else time.time()
    label = (label or "").strip()
    name = (name or "").strip()
    if not is_provisional_label(label):
        return {"ok": False, "error": f"not a provisional track label: {label!r}"}
    if not name:
        return {"ok": False, "error": "empty person name"}
    track = store.open_speaker_track(label)
    if track is None:
        return {"ok": False, "error": f"no open track for {label!r}"}
    pid = store.resolve_person(name, ts=now)
    if not pid:
        return {"ok": False, "error": f"could not resolve person {name!r}"}
    if not store.bind_speaker_track(int(track["id"]), int(pid), ts=now):
        return {"ok": False, "error": f"track {track['id']} already bound "
                                      "to a different person"}
    job_id = _enqueue_rebind(store, int(track["id"]), actor=actor)
    return {"ok": True, "track_id": int(track["id"]), "person_id": int(pid),
            "job_id": job_id}


def on_person_merged(store, survivor_id: int, absorbed_id: int,
                     ts: float | None = None) -> list[int]:
    """soft_merge_people hook: tracks bound to the absorbed person follow the
    merge to the survivor, and each re-pointed track gets a fresh durable
    rebind job so rows written under the old binding are rewritten too.
    No-op (and no reads on the new schema) while the flag is off."""
    if not enabled():
        return []
    try:
        moved = store.repoint_speaker_tracks(
            int(absorbed_id), int(survivor_id),
            ts=ts if ts is not None else time.time())
    except Exception as exc:
        print(f"[people_escrow] merge repoint skipped ({exc}).")
        return []
    for tid in moved:
        _enqueue_rebind(store, tid, actor="merge",
                        previous_person_id=int(absorbed_id))
    return moved


def _enqueue_rebind(store, track_id: int, *, actor: str = "user",
                    previous_person_id: int | None = None) -> int:
    """Durable enqueue (jobs table). The registered JobWorker handler drains
    it; a crash mid-bind leaves a re-runnable row, and the job itself is
    idempotent so a retry can never double-apply."""
    payload = {"track_id": int(track_id), "actor": actor}
    if previous_person_id is not None:
        payload["previous_person_id"] = int(previous_person_id)
    return store.enqueue_job(JOB_KIND, json.dumps(payload))


def run_rebind_job(payload: dict | None, store=None) -> dict:
    """JobWorker handler for `people_escrow_rebind` (registered in app/main.py).

    Rewrites every escrowed row for the (bound) track to its person id, flips
    the facts back to state='active' so they enter the normal review/retrieval
    flow at their ORIGINAL evidence tier (review stays NULL), indexes the
    reactivated facts, and appends an audit row. Idempotent: a re-run finds
    nothing left to rewrite and records a zero-count audit row.
    """
    if store is None:
        from app.storage import get_store
        store = get_store()
    payload = payload or {}
    track_id = int(payload.get("track_id") or 0)
    if not track_id:
        raise ValueError("people_escrow_rebind: missing track_id")
    track = store.get_speaker_track(track_id)
    if track is None:
        raise ValueError(f"people_escrow_rebind: no track {track_id}")
    pid = track.get("bound_person_id")
    if (track.get("status") or "") != "bound" or not pid:
        # Not bound (yet) — nothing to rewrite. Benign: the bind enqueues again.
        return {"track_id": track_id, "skipped": "track not bound"}
    prev = payload.get("previous_person_id")
    now = time.time()
    res = store.rebind_speaker_track_rows(
        track_id, int(pid),
        previous_person_id=int(prev) if prev else None, ts=now)
    # Reactivated facts enter the semantic index now (escrow skipped it), so
    # they surface in retrieval exactly like any other reviewed-tier fact.
    for f in res.get("activated", []):
        try:
            from app.services.extractor import _index_fact
            _index_fact(store, int(f["id"]), f.get("kind") or "claim",
                        f.get("text") or "", now)
        except Exception as exc:
            print(f"[people_escrow] index skipped for fact {f.get('id')} "
                  f"({exc}).")
    store.log_escrow_rebind(
        track_id=track_id, person_id=int(pid),
        n_facts=int(res.get("facts", 0)), n_tasks=int(res.get("tasks", 0)),
        n_commitments=int(res.get("commitments", 0)),
        actor=str(payload.get("actor") or "system"),
        reason=f"label={track.get('label')!r}", created_at=now)
    out = {"track_id": track_id, "person_id": int(pid),
           "facts": int(res.get("facts", 0)), "tasks": int(res.get("tasks", 0)),
           "commitments": int(res.get("commitments", 0))}
    print(f"[people_escrow] rebind {out}")
    return out


def escrow_status(store) -> dict:
    """Counts per track — observability for tests/console."""
    return store.speaker_track_status()
