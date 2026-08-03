"""KG v2 post-review revisions (Changes 1-8) — acceptance tests."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.storage import Store

NOW = 1_700_000_000.0


def _mk(td: str) -> Store:
    return Store(db_path=Path(td) / "t.db", audio_dir=Path(td) / "audio")


class Change1OpaqueIdentityTests(unittest.TestCase):
    def test_opaque_ids_and_phonetic_blocking_overlap(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                a = store.resolve_entity("0penAI", "org", ts=NOW)
                b = store.resolve_entity("OpenAI", "org", ts=NOW)
                self.assertNotEqual(a, b)
                ea, eb = store.get_entity(a), store.get_entity(b)
                ca, cb = ea["canonical_id"], eb["canonical_id"]
                self.assertTrue(ca and cb and ca != cb)
                # opaque: 32 hex chars, not derived from the name
                for cid in (ca, cb):
                    self.assertEqual(len(cid), 32)
                    int(cid, 16)
                    self.assertNotIn("open", cid)
                pa = {k["key_value"] for k in store.list_node_keys("entity", a)
                      if k["key_type"] == "phonetic"}
                pb = {k["key_value"] for k in store.list_node_keys("entity", b)
                      if k["key_type"] == "phonetic"}
                self.assertTrue(pa & pb, f"no phonetic overlap: {pa} vs {pb}")
            finally:
                store.close()

    def test_merge_preserves_both_key_sets_on_winner(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                w = store.resolve_person("Patrick Adorante", ts=NOW)
                l = store.resolve_person("Pat Adorante", ts=NOW)
                store.soft_merge_people(w, l, reason="same person")
                winner_keys = {(k["key_type"], k["key_value"])
                               for k in store.list_node_keys("person", w)}
                loser_keys = {(k["key_type"], k["key_value"])
                              for k in store.list_node_keys("person", l)}
                self.assertTrue(loser_keys)
                self.assertTrue(loser_keys <= winner_keys)
                # loser's own rows never deleted
                self.assertTrue(store.list_node_keys("person", l))
            finally:
                store.close()

    def test_backfill_gives_existing_rows_opaque_ids(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                # simulate a pre-migration row
                with store._lock:
                    store._conn.execute(
                        "UPDATE people SET canonical_id=NULL WHERE id=?", (pid,))
                    store._conn.commit()
                store._migrate()
                self.assertTrue(store.get_person(pid).get("canonical_id"))
            finally:
                store.close()

    def test_no_code_parses_canonical_id(self):
        import re
        root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for py in root.rglob("*.py"):
            src = py.read_text(encoding="utf-8", errors="replace")
            # canonical_id may be selected/compared, never split/normalized/parsed
            for m in re.finditer(
                    r"canonical_id[\"'\]]*\s*(?:\.split|\.lower\(\)\.split|\[\d)",
                    src):
                offenders.append(f"{py.name}: {m.group(0)}")
        self.assertFalse(offenders, offenders)


class Change2AttrsPkTests(unittest.TestCase):
    def test_atemporal_attr_cannot_duplicate(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                store.set_node_attr("person", 1, "role", "engineer", ts=NOW)
                # raw duplicate insert with NULL valid_from must hit the index
                with self.assertRaises(sqlite3.IntegrityError):
                    with store._lock:
                        store._conn.execute(
                            "INSERT INTO kg_node_attrs (node_type, node_id, "
                            "key, value, valid_from, created_at, updated_at) "
                            "VALUES ('person', 1, 'role', 'dup', NULL, ?, ?)",
                            (NOW, NOW))
                # upsert path updates in place instead
                store.set_node_attr("person", 1, "role", "manager", ts=NOW + 1)
                rows = store.list_node_attrs("person", 1)
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["value"], "manager")
                # distinct valid_from intervals still coexist
                store.set_node_attr("person", 1, "role", "vp",
                                    valid_from=NOW + 5, ts=NOW + 5)
                self.assertEqual(len(store.list_node_attrs("person", 1)), 2)
            finally:
                store.close()

    def test_evidence_dupe_migration_collapses_and_hardens(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.upsert_kg_predicate(
                    subj_type="person", subj_id=1, predicate="works_at",
                    obj_type="entity", obj_id=2, ts=NOW)
                with store._lock:
                    # recreate the legacy NULL-vulnerable index + seed dupes
                    store._conn.execute("DROP INDEX uq_kg_ev_dedupe")
                    store._conn.execute(
                        "CREATE UNIQUE INDEX idx_kg_ev_dedupe ON "
                        "kg_evidence(predicate_id, event_id, quote_hash)")
                    for w in (1.0, 3.0, 2.0):
                        store._conn.execute(
                            "INSERT INTO kg_evidence (predicate_id, event_id, "
                            "quote_hash, observed_at, weight) "
                            "VALUES (?, NULL, NULL, ?, ?)", (pid, NOW, w))
                    store._conn.commit()
                store._migrate()
                rows = store.list_kg_evidence(pid)
                self.assertEqual(len(rows), 1)
                self.assertEqual(float(rows[0]["weight"]), 3.0)  # kept best
                # hardened index now blocks the NULL dupe
                import sqlite3
                with self.assertRaises(sqlite3.IntegrityError):
                    with store._lock:
                        store._conn.execute(
                            "INSERT INTO kg_evidence (predicate_id, event_id, "
                            "quote_hash, observed_at, weight) "
                            "VALUES (?, NULL, NULL, ?, 1.0)", (pid, NOW))
            finally:
                store.close()


DAY = 86400.0


def _works_at(store, pid, eid, *, ts, quote, source_class="meeting_transcript",
              n=1):
    from app.services import kg_beliefs
    out = None
    for i in range(n):
        out = kg_beliefs.record_from_relation(
            store, subj_type="person", subj_id=pid, predicate="works_at",
            obj_type="entity", obj_id=eid, origin="asserted",
            ts=ts + i, quote=f"{quote} #{i}", source_class=source_class)
    return out


class Change3ConflictTests(unittest.TestCase):
    def test_sequential_job_change_splits_without_penalty(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Patrick", ts=NOW)
                dell = store.resolve_entity("Dell", "org", ts=NOW)
                acme = store.resolve_entity("Acme", "org", ts=NOW)
                _works_at(store, pid, dell, ts=NOW - 60 * DAY,
                          quote="Patrick from Dell", n=5)
                old = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=dell)
                from app.services import kg_beliefs
                dell_conf_before = kg_beliefs.recompute_confidence(
                    store, int(old["id"]), now=NOW - 60 * DAY + 10)
                res = _works_at(store, pid, acme, ts=NOW,
                                quote="Patrick Adorante, Acme — sig",
                                source_class="email")
                self.assertTrue(res["ok"])
                # split auto-applied: Dell closed at Acme's valid_from
                old2 = store.get_kg_predicate(int(old["id"]))
                self.assertEqual(old2["status"], "superseded")
                self.assertIsNotNone(old2["valid_to"])
                self.assertEqual(int(old2["superseded_by"]),
                                 int(res["predicate_id"]))
                # neither side penalized
                self.assertAlmostEqual(float(old2["confidence"]),
                                       dell_conf_before, places=6)
                self.assertEqual(int(old2["conflict"] or 0), 0)
                new = store.get_kg_predicate(int(res["predicate_id"]))
                self.assertEqual(int(new["conflict"] or 0), 0)
                self.assertGreaterEqual(float(new["confidence"]), 0.6)
                # adjudication logged
                adj = store.list_adjudications(kind="split_accept")
                self.assertTrue(adj)
                self.assertEqual(adj[0]["decided_by"], "auto")
            finally:
                store.close()

    def test_simultaneous_conflict_penalizes_and_both_true_restores(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Sarah", ts=NOW)
                a = store.resolve_entity("Figma", "org", ts=NOW)
                b = store.resolve_entity("Linear", "org", ts=NOW)
                _works_at(store, pid, a, ts=NOW - 3 * DAY, quote="at Figma")
                pa = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=a)
                conf_unpenalized = kg_beliefs.recompute_confidence(
                    store, int(pa["id"]), now=NOW)
                res = _works_at(store, pid, b, ts=NOW, quote="at Linear")
                pa2 = store.get_kg_predicate(int(pa["id"]))
                pb = store.get_kg_predicate(int(res["predicate_id"]))
                self.assertEqual(int(pa2["conflict"]), 1)
                self.assertEqual(int(pb["conflict"]), 1)
                self.assertLess(float(pa2["confidence"]), conf_unpenalized)
                self.assertEqual(pa2["status"], "active")  # no split
                self.assertTrue(store.list_adjudications(kind="conflict_flag"))
                # both-true adjudication clears + restores
                out = kg_beliefs.resolve_conflict_both_true(
                    store, int(pa["id"]), int(pb["id"]), now=NOW)
                pa3 = store.get_kg_predicate(int(pa["id"]))
                self.assertEqual(int(pa3["conflict"]), 0)
                self.assertAlmostEqual(float(pa3["confidence"]),
                                       conf_unpenalized, delta=0.02)
                self.assertTrue(
                    store.list_adjudications(kind="conflict_both_true"))
            finally:
                store.close()

    def test_trusted_old_belief_gets_proposal_not_auto_split(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Kai", ts=NOW)
                a = store.resolve_entity("OldCo", "org", ts=NOW)
                b = store.resolve_entity("NewCo", "org", ts=NOW)
                from app.services import kg_beliefs
                kg_beliefs.record_from_relation(
                    store, subj_type="person", subj_id=pid,
                    predicate="works_at", obj_type="entity", obj_id=a,
                    origin="user", ts=NOW - 90 * DAY, quote="I said so",
                    source_class="user")
                old = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=a)
                _works_at(store, pid, b, ts=NOW, quote="sig",
                          source_class="email")
                old2 = store.get_kg_predicate(int(old["id"]))
                self.assertEqual(old2["status"], "active")  # untouched
                adj = store.list_adjudications(kind="split_accept")
                self.assertTrue(adj)
                self.assertEqual(adj[0]["decision"], "defer")
                import json as _json
                feats = _json.loads(adj[0]["features_json"])
                self.assertIn("proposal", feats)
            finally:
                store.close()


class Change4FlywheelTests(unittest.TestCase):
    def test_evidence_verdicts_log_frozen_features(self):
        import json as _json
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                _works_at(store, pid, eid, ts=NOW, quote="works at acme", n=2)
                pred = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=eid)
                evs = store.list_kg_evidence(int(pred["id"]))
                conf_before = kg_beliefs.recompute_confidence(
                    store, int(pred["id"]), now=NOW)
                r = kg_beliefs.evidence_verdict(
                    store, int(evs[0]["id"]), "reject", now=NOW)
                self.assertTrue(r["ok"])
                self.assertLess(r["confidence"], conf_before)
                # rejected row survives with weight 0
                self.assertEqual(
                    float(store.get_kg_evidence(int(evs[0]["id"]))["weight"]), 0)
                kg_beliefs.evidence_verdict(store, int(evs[1]["id"]),
                                            "confirm", now=NOW)
                for kind in ("evidence_reject", "evidence_confirm"):
                    rows = store.list_adjudications(kind=kind)
                    self.assertEqual(len(rows), 1)
                    feats = _json.loads(rows[0]["features_json"])
                    self.assertTrue(feats)
                    self.assertIn("posterior_before", feats)
                    self.assertIn("source_class", feats["evidence"])
            finally:
                store.close()

    def test_weights_live_in_config_and_change_posteriors(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                _works_at(store, pid, eid, ts=NOW, quote="sig",
                          source_class="email")
                pred = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=eid)
                c1 = kg_beliefs.recompute_confidence(store, int(pred["id"]),
                                                     now=NOW)
                v1, w = kg_beliefs.source_weights(store)
                self.assertGreater(v1, 0)
                w["email"] = 0.2
                v2 = store.set_kg_config("source_weights", w)
                self.assertEqual(v2, v1 + 1)
                c2 = kg_beliefs.recompute_confidence(store, int(pred["id"]),
                                                     now=NOW)
                self.assertLess(c2, c1)  # no redeploy needed
            finally:
                store.close()

    def test_merge_logs_adjudication(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                w = store.resolve_person("Chris Long", ts=NOW)
                l = store.resolve_person("Christopher Long", ts=NOW)
                store.soft_merge_people(w, l, reason="same", confidence=0.97)
                rows = store.list_adjudications(kind="merge_accept")
                self.assertEqual(len(rows), 1)
                self.assertEqual(int(rows[0]["node_a"]), w)
                self.assertEqual(int(rows[0]["node_b"]), l)
            finally:
                store.close()


class Change5LazyPosteriorTests(unittest.TestCase):
    def test_intake_path_never_scans_the_bag(self):
        from unittest.mock import patch as _patch
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                _works_at(store, pid, eid, ts=NOW, quote="seed")
                # further sightings of the SAME belief must not read evidence
                def _boom(*a, **k):
                    raise AssertionError("intake path scanned the evidence bag")
                with _patch.object(store, "list_kg_evidence", _boom), \
                        _patch.object(store, "list_kg_evidence_times", _boom):
                    res = _works_at(store, pid, eid, ts=NOW + 60,
                                    quote="another sighting")
                self.assertTrue(res["ok"])
                pred = store.get_kg_predicate(int(res["predicate_id"]))
                self.assertEqual(int(pred["posterior_stale"]), 1)
            finally:
                store.close()

    def test_read_and_batch_agree_and_sweep_clears_flags(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                res = _works_at(store, pid, eid, ts=NOW, quote="x", n=3)
                prid = int(res["predicate_id"])
                c_read = kg_beliefs.posterior(store, prid, now=NOW + 100)
                self.assertEqual(
                    int(store.get_kg_predicate(prid)["posterior_stale"]), 0)
                # re-stale and sweep in batch — identical posterior
                with store._lock:
                    store._conn.execute(
                        "UPDATE kg_predicates SET posterior_stale=1 WHERE id=?",
                        (prid,))
                    store._conn.commit()
                out = kg_beliefs.recal_sweep(store, now=NOW + 100)
                self.assertGreaterEqual(out["recomputed"], 1)
                self.assertEqual(out["remaining"], 0)
                c_batch = float(store.get_kg_predicate(prid)["confidence"])
                self.assertAlmostEqual(c_read, c_batch, places=9)
            finally:
                store.close()

    def test_decay_only_read_matches_full_recompute(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                res = _works_at(store, pid, eid, ts=NOW, quote="x", n=2)
                prid = int(res["predicate_id"])
                kg_beliefs.posterior(store, prid, now=NOW)  # builds cache
                later = NOW + 200 * DAY
                c_cached = kg_beliefs.posterior(store, prid, now=later)
                c_full = kg_beliefs.recompute_confidence(store, prid, now=later)
                self.assertAlmostEqual(c_cached, c_full, places=9)
            finally:
                store.close()

    def test_weight_version_bump_forces_full_rescan(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                res = _works_at(store, pid, eid, ts=NOW, quote="sig",
                                source_class="email")
                prid = int(res["predicate_id"])
                c1 = kg_beliefs.posterior(store, prid, now=NOW)
                _, w = kg_beliefs.source_weights(store)
                w["email"] = 0.2
                store.set_kg_config("source_weights", w)
                c2 = kg_beliefs.posterior(store, prid, now=NOW)  # not stale, but version bumped
                self.assertLess(c2, c1)
            finally:
                store.close()


class Change6TemporalRetrievalTests(unittest.TestCase):
    def _job_change(self, store):
        pid = store.resolve_person("Sarah Kim", ts=NOW - 400 * DAY)
        figma = store.resolve_entity("Figma", "org", ts=NOW - 400 * DAY)
        linear = store.resolve_entity("Linear", "org", ts=NOW)
        _works_at(store, pid, figma, ts=NOW - 400 * DAY, quote="at Figma", n=3)
        _works_at(store, pid, linear, ts=NOW, quote="Sarah · Linear — sig",
                  source_class="email")
        return pid, figma, linear

    def test_org_people_returns_former_affiliates_labeled(self):
        from app.services import graph
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                self._job_change(store)
                res = graph.people_for_entity(store, "Figma")
                self.assertTrue(res["found"])
                self.assertEqual(len(res["people"]), 1)
                row = res["people"][0]
                self.assertTrue(row["former"])
                self.assertIn("at Figma", row["label"])
                self.assertIsNotNone(row["valid_to"])
                self.assertIsNotNone(row["superseded_by"])
                # current employer listed as current, ordered first
                res2 = graph.people_for_entity(store, "Linear")
                self.assertFalse(res2["people"][0]["former"])
            finally:
                store.close()

    def test_person_context_keeps_former_affiliation(self):
        from app.services import graph
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                self._job_change(store)
                ctx = graph.context_for_person("Sarah Kim", store)
                self.assertTrue(ctx["found"])
                by_name = {a["name"]: a for a in ctx["affiliations"]}
                self.assertIn("Figma", by_name)
                self.assertTrue(by_name["Figma"]["former"])
                self.assertFalse(by_name["Linear"].get("former"))
                # current ranks above former
                names = [a["name"] for a in ctx["affiliations"]
                         if a["name"] in ("Figma", "Linear")]
                self.assertEqual(names[0], "Linear")
            finally:
                store.close()

    def test_non_people_predicates_stay_strict_current(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                proj = store.resolve_entity("mnemos", "project", ts=NOW)
                tool = store.resolve_entity("SQLite", "tool", ts=NOW)
                from app.services import kg_beliefs
                r = kg_beliefs.record_from_relation(
                    store, subj_type="entity", subj_id=proj, predicate="uses",
                    obj_type="entity", obj_id=tool, origin="asserted",
                    ts=NOW - 100 * DAY, quote="uses sqlite",
                    source_class="meeting_transcript")
                store.supersede_kg_predicate(int(r["predicate_id"]), 0,
                                            valid_to=NOW - 10 * DAY)
                # default lookup (strict current) must not surface it
                self.assertIsNone(store.find_kg_predicate(
                    subj_type="entity", subj_id=proj, predicate="uses",
                    obj_type="entity", obj_id=tool))
            finally:
                store.close()

    def test_superseded_explain_carries_interval(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid, figma, _ = self._job_change(store)
                old = store.list_kg_predicates(
                    subj_type="person", subj_id=pid, obj_type="entity",
                    obj_id=figma, statuses=("superseded",))[0]
                exp = kg_beliefs.explain_predicate(store, int(old["id"]))
                self.assertIn("Valid", exp["explanation"])
                self.assertIn("superseded by belief", exp["explanation"])
            finally:
                store.close()


class Change7ManualSplitTests(unittest.TestCase):
    def test_manual_split_reassigns_beliefs_and_bags(self):
        from app.services import kg_beliefs
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                merc = store.resolve_entity("Mercury", "org", ts=NOW)
                pids = [store.resolve_person(n, ts=NOW)
                        for n in ("Ana", "Ben", "Cal")]
                # 3 beliefs × 2 evidences = 6 evidences on one contaminated node
                for p in pids:
                    _works_at(store, p, merc, ts=NOW, quote=f"works {p}", n=2)
                preds = {p: store.find_kg_predicate(
                    subj_type="person", subj_id=p, predicate="works_at",
                    obj_type="entity", obj_id=merc) for p in pids}
                # Ana + Ben actually work at Mercury Bank — split them out
                out = kg_beliefs.manual_split(
                    store, node_type="entity", node_id=merc,
                    new_name="Mercury Bank",
                    predicate_ids=[int(preds[pids[0]]["id"]),
                                   int(preds[pids[1]]["id"])], now=NOW)
                self.assertTrue(out["ok"])
                bank = out["new_node_id"]
                self.assertNotEqual(bank, merc)
                self.assertTrue(store.get_entity(bank)["canonical_id"])
                # moved beliefs point at the bank, each with its own bag
                for p in pids[:2]:
                    pr = store.get_kg_predicate(int(preds[p]["id"]))
                    self.assertEqual(int(pr["obj_id"]), bank)
                    self.assertEqual(
                        len(store.list_kg_evidence(int(pr["id"]))), 2)
                    exp = kg_beliefs.explain_predicate(store, int(pr["id"]))
                    self.assertIn("Mercury Bank", exp["explanation"])
                # Cal stays; no orphaned predicates on either node
                pr_cal = store.get_kg_predicate(int(preds[pids[2]]["id"]))
                self.assertEqual(int(pr_cal["obj_id"]), merc)
                self.assertEqual(len(store.list_kg_evidence(int(pr_cal["id"]))), 2)
                adj = store.list_adjudications(kind="split_accept")
                self.assertTrue(any(a["decided_by"] == "user" for a in adj))
            finally:
                store.close()


class Change8ParityTests(unittest.TestCase):
    def test_seeded_drift_is_reported_and_gate_fails(self):
        from app.services import kg_parity
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                _works_at(store, pid, eid, ts=NOW, quote="clean edge")
                # drift 1: v1 user relation with no v2 predicate
                with store._lock:
                    store._conn.execute(
                        "INSERT INTO relations (subj_type, subj_id, predicate, "
                        "obj_type, obj_id, weight, origin, created_at) "
                        "VALUES ('person', ?, 'member_of', 'entity', ?, 1, "
                        "'user', ?)", (pid, eid, NOW))
                    store._conn.commit()
                # drift 2: v2 predicate pointing at a nonexistent node
                store.upsert_kg_predicate(
                    subj_type="person", subj_id=999999,
                    predicate="works_at", obj_type="entity", obj_id=eid,
                    ts=NOW)
                rep = kg_parity.run(store, now=NOW)
                self.assertGreater(rep["critical"], 0)
                missing = rep["edges"]["missing_in_v2"]
                self.assertTrue(any(m[2] == "member_of" for m in missing))
                self.assertTrue(any(d[1] == 999999
                                    for d in rep["nodes"]["dangling"]))
                self.assertFalse(kg_parity.cutover_ready(store)["ready"])
            finally:
                store.close()

    def test_clean_fixture_passes_gate_after_seven_reports(self):
        from app.services import kg_parity
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                _works_at(store, pid, eid, ts=NOW, quote="clean edge")
                for i in range(6):
                    rep = kg_parity.run(store, now=NOW + i * DAY)
                    self.assertEqual(rep["critical"], 0)
                self.assertFalse(kg_parity.cutover_ready(store)["ready"])
                kg_parity.run(store, now=NOW + 6 * DAY)
                gate = kg_parity.cutover_ready(store)
                self.assertTrue(gate["ready"])
                self.assertEqual(gate["critical_in_window"], 0)
            finally:
                store.close()


class M1BackfillTests(unittest.TestCase):
    def test_backfill_is_idempotent_and_clears_edge_parity(self):
        from app.services import kg_backfill, kg_parity
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                pid = store.resolve_person("Ada", ts=NOW)
                eid = store.resolve_entity("Acme", "org", ts=NOW)
                # legacy pre-KG-A rows: relations only, no belief store
                with store._lock:
                    for pred, origin in (("works_at", "asserted"),
                                         ("member_of", "user")):
                        store._conn.execute(
                            "INSERT INTO relations (subj_type, subj_id, "
                            "predicate, obj_type, obj_id, weight, origin, "
                            "created_at) VALUES ('person', ?, ?, 'entity', "
                            "?, 1, ?, ?)", (pid, pred, eid, origin, NOW))
                    store._conn.commit()
                res1 = kg_backfill.run(store)
                self.assertEqual(res1["recorded"], 2)
                self.assertEqual(res1["errors"], 0)
                rep = kg_parity.run(store, now=NOW)
                self.assertEqual(rep["edges"]["missing_in_v2"], [])
                # idempotent: re-run adds no evidence
                pred = store.find_kg_predicate(
                    subj_type="person", subj_id=pid, predicate="works_at",
                    obj_type="entity", obj_id=eid)
                n1 = len(store.list_kg_evidence(int(pred["id"])))
                kg_backfill.run(store)
                self.assertEqual(
                    len(store.list_kg_evidence(int(pred["id"]))), n1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
