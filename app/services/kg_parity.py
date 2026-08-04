"""KG v2 Change 8 — dual-write parity diff (shadow period M0–M3).

v1 (`relations` + people/entities) and v2 (`kg_predicates` + evidence) run
side by side; dual-write systems drift silently. This worker produces a
divergence report BEFORE read cutover, so the user never discovers the drift
as a changed constellation. Report-only: it never repairs anything (I-2).

Cutover gate (M3): 7 consecutive nightly reports with zero critical deltas
(dangling protected/trusted endpoints, edge deltas on user-origin rows) —
encoded as `cutover_ready()`, a check, not a convention.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any

# Derived edges are features only — never in the belief store — so parity is
# defined over asserted/user rows exclusively.
_ORIGINS = ("asserted", "user")


def shadow_mode() -> bool:
    return os.environ.get("QUILL_KG_SHADOW", "1") not in ("0", "false", "False")


def _edge_key(r: dict) -> tuple:
    return (r["subj_type"], int(r["subj_id"]), r["predicate"],
            r["obj_type"], int(r["obj_id"]))


def run(store, *, now: float | None = None) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    report: dict[str, Any] = {"ts": now}
    critical = 0

    # --- edge parity ------------------------------------------------------
    with store._lock:
        v1 = [dict(r) for r in store._conn.execute(
            "SELECT subj_type, subj_id, predicate, obj_type, obj_id, origin "
            f"FROM relations WHERE origin IN ({','.join('?' * len(_ORIGINS))})",
            _ORIGINS).fetchall()]
        v2 = [dict(r) for r in store._conn.execute(
            "SELECT * FROM kg_predicates WHERE status IN "
            "('active','superseded')").fetchall()]
    v2_keys = {_edge_key(r) for r in v2}
    v1_keys = {_edge_key(r) for r in v1}
    missing_in_v2 = [r for r in v1 if _edge_key(r) not in v2_keys]
    # superseded-only beliefs legitimately outlive their relations row after a
    # temporal split, so only ACTIVE v2 edges count as v2-extra.
    extra_in_v2 = [r for r in v2
                   if r["status"] == "active" and _edge_key(r) not in v1_keys]
    report["edges"] = {
        "v1_count": len(v1), "v2_count": len(v2),
        "missing_in_v2": [list(_edge_key(r)) + [r["origin"]]
                          for r in missing_in_v2][:200],
        "extra_in_v2": [list(_edge_key(r)) for r in extra_in_v2][:200],
    }
    critical += sum(1 for r in missing_in_v2 if r["origin"] == "user")
    critical += sum(1 for r in extra_in_v2
                    if r.get("layer") == "user_verified" or r.get("protected"))

    # --- node parity ------------------------------------------------------
    dangling = []
    for r in v2:
        for side in ("subj", "obj"):
            typ, nid = r[f"{side}_type"], int(r[f"{side}_id"])
            node = None
            if typ == "person":
                node = store.get_person(nid)
            elif typ == "entity":
                node = store.get_entity(nid)
            else:
                continue
            if node is None:
                dangling.append([typ, nid, int(r["id"])])
            elif not node.get("canonical_id"):
                dangling.append([typ, nid, int(r["id"]), "no_canonical_id"])
    report["nodes"] = {"dangling": dangling[:200]}
    critical += len(dangling)

    # --- read parity ------------------------------------------------------
    # For trusted subjects, the v1 affiliation view and the v2 active belief
    # view must agree (order-insensitive set diff).
    trusted_subjects = {(r["subj_type"], int(r["subj_id"])) for r in v2
                        if r.get("protected") or r.get("layer") == "user_verified"}
    read_deltas = []
    from app.services.graph import AFFILIATION_PREDS
    for typ, nid in sorted(trusted_subjects)[:50]:
        if typ != "person":
            continue
        edges = store.relations_of("person", nid)
        v1_aff = {(e["predicate"], e["obj_type"], int(e["obj_id"]))
                  for e in edges["out"]
                  if e.get("origin") in _ORIGINS
                  and e["predicate"] in AFFILIATION_PREDS}
        v2_aff = {(r["predicate"], r["obj_type"], int(r["obj_id"]))
                  for r in v2
                  if r["subj_type"] == "person" and int(r["subj_id"]) == nid
                  and r["status"] == "active"
                  and r["predicate"] in AFFILIATION_PREDS}
        v2_all = {(r["predicate"], r["obj_type"], int(r["obj_id"]))
                  for r in v2
                  if r["subj_type"] == "person" and int(r["subj_id"]) == nid
                  and r["predicate"] in AFFILIATION_PREDS}
        # a v1 edge is only a delta if v2 has NO belief (active or superseded)
        delta = sorted(v1_aff - v2_all) + sorted(v2_aff - v1_aff)
        if delta:
            read_deltas.append({"person": nid, "delta": [list(d) for d in delta]})
    report["read"] = {"subjects_checked": len(trusted_subjects),
                      "deltas": read_deltas[:50]}
    critical += len(read_deltas)

    report["critical"] = critical
    with store._lock:
        store._conn.execute(
            "INSERT INTO kg_parity_reports (ts, critical, report_json) "
            "VALUES (?, ?, ?)", (now, critical, json.dumps(report)))
        store._conn.commit()
    if critical:
        print(f"[kg_parity] {critical} critical delta(s) — see /kg/parity.")
    return report


def latest_reports(store, *, limit: int = 7) -> list[dict]:
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM kg_parity_reports ORDER BY id DESC LIMIT ?",
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
    """M3 read-cutover gate: 7 consecutive nightly reports, zero critical."""
    reports = latest_reports(store, limit=7)
    ok = len(reports) >= 7 and all(int(r["critical"]) == 0 for r in reports)
    return {"ready": ok, "reports": len(reports),
            "critical_in_window": sum(int(r["critical"]) for r in reports)}


def read_v2_enabled(store=None) -> bool:
    """Plan 2.6 — whether constellation/grounding primary-read kg_beliefs.

    Env override (dev / rollback / tests):
      QUILL_KG_READ_V2=1 → force ON
      QUILL_KG_READ_V2=0 → force OFF
    Unset → follow cutover_ready (7 consecutive zero-critical parity reports).
    """
    raw = os.environ.get("QUILL_KG_READ_V2")
    if raw is not None and str(raw).strip() != "":
        return str(raw) not in ("0", "false", "False")
    if store is None:
        return False
    try:
        return bool(cutover_ready(store).get("ready"))
    except Exception:
        return False
