"""CAL Stages 3–5 — resolver, escalation and the ratchet, propagation,
compounding, and the evaluation harness.

The properties under test are the ones that keep an attribution layer honest
rather than merely confident: supporting evidence cannot carry a candidate,
belief is absolute while margin is relative, the model picks an index and never
a name, one escalation is paid for once, and inferred edges never feed another
inference.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from app.services.context import compounding as comp
from app.services.context import escalate as esc
from app.services.context import evaluate as ev
from app.services.context import keys as k
from app.services.context import propagate as prop
from app.services.context import resolver as rs
from app.storage import Store


def C(nid, name, feats, keys=()):
    return rs.Candidate("entity", nid, name, dict(feats), frozenset(keys))


class ClampTests(unittest.TestCase):
    """§5.1 — supporting evidence may reorder, never carry."""

    def test_supporting_alone_is_scaled_to_nothing(self) -> None:
        """Uncapped, the model learns to predict 'whatever they were doing five
        minutes ago' — right 80% of the time and catastrophic the rest."""
        feats, cut = rs.clamp_supporting(
            {"f_frame": 1.0, "f_embed": 1.0, "f_person": 1.0, "f_prior": 1.0})
        self.assertTrue(cut)
        self.assertEqual(sum(feats.values()), 0.0)

    def test_a_candidate_with_real_keys_is_untouched(self) -> None:
        base = {"f_key": 0.95, "f_key_n": 0.7, "f_frame": 0.2}
        feats, cut = rs.clamp_supporting(base)
        self.assertFalse(cut)
        self.assertEqual(feats, base)

    def test_the_cap_is_a_share_not_a_ceiling(self) -> None:
        feats, _ = rs.clamp_supporting({"f_key": 1.0, "f_frame": 1.0})
        w = rs.WEIGHTS
        sup = w["f_frame"] * feats["f_frame"]
        total = sup + w["f_key"] * feats["f_key"]
        self.assertLessEqual(sup / total, rs.SUPPORTING_CAP + 1e-9)

    def test_every_feature_is_classified(self) -> None:
        """An unclassified feature is a silent hole in the cap."""
        self.assertEqual(set(rs.WEIGHTS), set(rs.FEATURE_TIER))


class BandTests(unittest.TestCase):
    def test_a_strong_key_binds_deterministically(self) -> None:
        d = rs.decide(rs.score([C(17, "Ravenry", {"f_key": 0.97, "f_key_n": 0.69}, ["repo:x"]),
                                C(31, "Tea Leaf", {"f_embed": 0.19})]))
        self.assertEqual(d.band, "deterministic")
        self.assertEqual(d.chosen[0].node_id, 17)

    def test_belief_is_absolute_not_a_softmax_share(self) -> None:
        """With ONE candidate the softmax share is always 1.0 however thin the
        evidence; conflating it with belief binds a lone guess at 1.00."""
        d = rs.decide(rs.score([C(44, "Yesterday", {"f_frame": 1.0, "f_embed": 1.0})]))
        self.assertEqual(d.scored[0].p, 1.0)
        self.assertLess(d.scored[0].strength, rs.ESCALATE_P)
        self.assertEqual(d.band, "provisional")
        self.assertFalse(d.is_bound)

    def test_disjoint_strong_keys_bind_both(self) -> None:
        """A monorepo, or a meeting covering two initiatives."""
        d = rs.decide(rs.score([C(1, "A", {"f_key": 0.95}, ["repo:a"]),
                                C(2, "B", {"f_key": 0.93}, ["repo:b"])]))
        self.assertEqual(d.band, "scored_multi")
        self.assertEqual({c.node_id for c in d.chosen}, {1, 2})

    def test_one_ambiguous_alias_escalates_instead(self) -> None:
        """Same evidence pointing two ways is ambiguity, not multiplicity."""
        d = rs.decide(rs.score([C(1, "A", {"f_key": 0.80}, ["alias:z"]),
                                C(2, "B", {"f_key": 0.78}, ["alias:z"])]))
        self.assertEqual(d.band, "pending")
        self.assertFalse(d.is_bound)
        self.assertLessEqual(len(d.escalate), rs.MAX_ESCALATION_OPTIONS)

    def test_no_candidates_is_unbound_not_an_error(self) -> None:
        d = rs.decide(rs.score([]))
        self.assertEqual(d.band, "unbound")

    def test_explain_names_the_rejected(self) -> None:
        d = rs.decide(rs.score([C(17, "Ravenry", {"f_key": 0.97}, ["repo:x"]),
                                C(31, "Tea Leaf", {"f_embed": 0.19})]))
        text = rs.explain(d)
        self.assertIn("Ravenry", text)
        self.assertIn("Tea Leaf", text)
        self.assertIn("rejected", text)

    def test_train_refuses_to_fake_a_fit(self) -> None:
        with self.assertRaises(NotImplementedError):
            rs.train()


class EscalationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_esc_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.eid = self.store.resolve_entity("Ravenry", "project", ts=time.time())
        self.cands = [C(self.eid, "Ravenry", {"f_key": 0.80}, ["alias:z"]),
                      C(99, "Tea Leaf", {"f_key": 0.78}, ["alias:z"])]
        self.decision = rs.decide(rs.score(self.cands))

    def test_the_model_returns_an_index_never_a_name(self) -> None:
        seen = {}

        def ask(system, messages, tier):
            seen["schema_choice"] = esc.CHOICE_SCHEMA["properties"]["choice"]
            seen["prompt"] = messages[0]["content"]
            return {"choice": 0, "confidence": 0.9}

        got = esc.choose("editing the resolver", self.cands, ask=ask)
        self.assertEqual(got.chosen.node_id, self.eid)
        self.assertIn("integer", seen["schema_choice"]["type"])
        self.assertIn("0. Ravenry", seen["prompt"])

    def test_an_out_of_range_index_is_refused_not_repaired(self) -> None:
        """A repaired answer to a constrained question is a guess in a schema."""
        got = esc.choose("x", self.cands, ask=lambda s, m, t: {"choice": 7})
        self.assertIsNone(got.chosen)
        self.assertIn("bad_index", got.error)

    def test_null_is_a_legitimate_answer(self) -> None:
        got = esc.choose("x", self.cands, ask=lambda s, m, t: {"choice": None})
        self.assertIsNone(got.chosen)
        self.assertEqual(got.error, "")

    def test_a_model_failure_does_not_raise(self) -> None:
        def boom(*_a):
            raise RuntimeError("ollama down")
        got = esc.choose("x", self.cands, ask=boom)
        self.assertIsNone(got.chosen)
        self.assertIn("model_error", got.error)

    def test_the_ratchet_means_paying_once(self) -> None:
        """The whole economic thesis: cost tracks novel identifiers, not events."""
        sk = [k.remote("git@github.com:x/rav.git"),
              k.path("/home/x/dev/ravenry", resolve=False)]
        self.assertEqual(self.store.lookup_binding("repo", "github.com/x/rav"), [])
        out = esc.resolve(self.store, self.decision, summary="editing",
                          signal_keys=sk, ask=lambda s, m, t: {"choice": 0})
        self.assertEqual(out.chosen.node_id, self.eid)
        rows = self.store.lookup_binding("repo", "github.com/x/rav")
        self.assertEqual([r["node_id"] for r in rows], [self.eid])
        self.assertEqual(rows[0]["origin"], "inferred",
                         "weaker than a user confirmation, on purpose")

    def test_a_refused_escalation_mints_nothing(self) -> None:
        sk = [k.remote("git@github.com:x/rav.git")]
        esc.resolve(self.store, self.decision, summary="x", signal_keys=sk,
                    ask=lambda s, m, t: {"choice": None})
        self.assertEqual(self.store.lookup_binding("repo", "github.com/x/rav"), [])

    def test_only_escalatable_decisions_escalate(self) -> None:
        bound = rs.decide(rs.score([C(1, "A", {"f_key": 0.97}, ["repo:a"])]))
        out = esc.resolve(self.store, bound, summary="x")
        self.assertEqual(out.error, "not_escalatable")


class PropagationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_prop_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        now = time.time()
        for oid, conf, layer in ((17, 0.90, "asserted"), (31, 0.80, "derived"),
                                 (99, 0.95, "inferred"), (44, 0.30, "asserted")):
            self.store._conn.execute(
                "INSERT INTO kg_predicates (subj_type,subj_id,predicate,obj_type,"
                "obj_id,layer,confidence,first_seen,last_seen,status,created_at,"
                "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("person", 6, "works_on", "entity", oid, layer, conf, now, now,
                 "active", now, now))
        self.store._conn.commit()

    def test_inferred_edges_never_propagate(self) -> None:
        """The system's own guesses must not become its own evidence, or it
        drifts into a self-consistent fiction nothing inside it can detect."""
        got = prop.propagate(self.store, {("person", 6): 1.0})
        self.assertNotIn(("entity", 99), got)

    def test_weak_edges_are_below_the_floor(self) -> None:
        got = prop.propagate(self.store, {("person", 6): 1.0})
        self.assertNotIn(("entity", 44), got)

    def test_damping_is_lambda_times_edge_confidence(self) -> None:
        got = prop.propagate(self.store, {("person", 6): 1.0})
        self.assertAlmostEqual(got[("entity", 17)], 0.9 * prop.DAMPING, places=6)

    def test_one_hop_only(self) -> None:
        """Seeds are absent from the result, so a caller cannot loop this into
        a transitive closure by feeding output back as input."""
        got = prop.propagate(self.store, {("person", 6): 1.0})
        self.assertNotIn(("person", 6), got)

    def test_fan_out_is_capped_by_strength(self) -> None:
        got = prop.neighbors(self.store, "person", 6, fan_out=1)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["n_id"], 17, "keeps the strongest, not the first")

    def test_propagation_cannot_propose_a_candidate(self) -> None:
        cands = [C(17, "Ravenry", {"f_key": 0.9}, ["r"])]
        out = prop.reweight(cands, prop.propagate(self.store, {("person", 6): 1.0}))
        self.assertEqual([c.node_id for c in out], [17])
        self.assertIn("f_graph", out[0].features)

    def test_graph_evidence_is_supporting(self) -> None:
        self.assertEqual(rs.FEATURE_TIER["f_graph"], rs.SUPPORTING)


class CompoundingTests(unittest.TestCase):
    def test_spread_measures_discrimination(self) -> None:
        self.assertEqual(comp.spread([100]), 0.0)
        self.assertEqual(comp.spread([50, 50]), 1.0)
        self.assertLess(comp.spread([95, 3, 2]), 0.5)

    def test_a_bleeding_key_is_demoted_but_a_confirmed_one_is_not(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="quill_spread_"))
        store = Store(db_path=tmp / "t.db", audio_dir=tmp / "a")
        for nid in (1, 2, 3, 4):
            store.bind_node_key("entity", nid, "path", "/downloads",
                                strength=0.6)
            for _ in range(9):
                store.bind_node_key("entity", nid, "path", "/downloads",
                                    strength=0.6)
        store.bind_node_key("entity", 9, "repo", "github.com/a/b", strength=0.95,
                            key_class="identity", confirmed=True)
        out = comp.recompute_spread(store)
        self.assertGreaterEqual(out["keys_demoted"], 1)
        bled = store.lookup_binding("path", "/downloads")[0]
        self.assertLessEqual(bled["strength"], comp.DEMOTED_STRENGTH)
        self.assertGreater(bled["spread"], comp.SPREAD_DEMOTE_ABOVE)
        kept = store.lookup_binding("repo", "github.com/a/b")[0]
        self.assertEqual(kept["strength"], 0.95, "a confirmed key is not a statistic")

    def test_evidence_saturates_within_a_bucket_and_adds_across(self) -> None:
        """Seeing one file four hundred times in a sitting is ONE observation."""
        one_bucket = comp.independent_total([(("fs", 1), "fs")] * 400)
        four_buckets = comp.independent_total(
            [((f"b{i}", 1), "fs") for i in range(4)])
        self.assertLessEqual(one_bucket, comp.BUCKET_CAP + 1e-9)
        self.assertGreater(four_buckets, one_bucket)

    def test_a_new_project_needs_keys_events_and_days(self) -> None:
        obs = [(i, f"day{i % 3}", {"repo:a", "path:b", "domain:c", "issue:D-1"})
               for i in range(30)]
        self.assertTrue(comp.detect_new_project(obs))
        self.assertEqual(comp.detect_new_project(obs[:5]), [],
                         "too few events")
        one_day = [(i, "day0", {"repo:a", "path:b", "domain:c", "issue:D-1"})
                   for i in range(30)]
        self.assertEqual(comp.detect_new_project(one_day), [],
                         "one day is a session, not a project")

    def test_detection_proposes_but_never_mints(self) -> None:
        obs = [(i, f"d{i % 2}", {"repo:a", "path:b", "domain:c", "issue:D-1"})
               for i in range(30)]
        for c in comp.detect_new_project(obs):
            self.assertEqual(c["state"], "provisional")
            self.assertNotIn("entity_id", c)


class EvaluationTests(unittest.TestCase):
    def test_boundary_f1_tolerates_human_imprecision(self) -> None:
        exact = ev.boundary_f1([100.0, 200.0], [100.0, 200.0])
        self.assertEqual(exact["f1"], 1.0)
        near = ev.boundary_f1([130.0, 200.0], [100.0, 200.0], tolerance_s=60)
        self.assertEqual(near["f1"], 1.0)
        far = ev.boundary_f1([400.0, 200.0], [100.0, 200.0], tolerance_s=60)
        self.assertLess(far["f1"], 1.0)

    def test_a_duplicated_boundary_cannot_inflate_recall(self) -> None:
        got = ev.boundary_f1([100.0, 101.0, 102.0], [100.0], tolerance_s=30)
        self.assertEqual(got["tp"], 1)
        self.assertEqual(got["fp"], 2)

    def test_recall_is_over_attributable_events_only(self) -> None:
        got = ev.attribution({1: "A", 2: None}, {1: "A", 2: None})
        self.assertEqual(got["recall"], 1.0)
        self.assertEqual(got["n_attributable"], 1)

    def test_zero_unbound_is_flagged_unhealthy(self) -> None:
        """A system that always produces a label is overconfident, and its
        precision is measuring its own nerve."""
        got = ev.attribution({1: "A", 2: "B"}, {1: "A", 2: "A"})
        self.assertEqual(got["unbound_rate"], 0.0)
        self.assertFalse(got["unbound_healthy"])

    def test_correction_persistence_catches_an_edge_written_as_a_binding(self) -> None:
        got = ev.correction_persistence([("p", 1, None), ("p", 2, 99.0)])
        self.assertEqual(got["persistence"], 0.5)
        self.assertEqual(got["recurred"], 1)

    def test_the_compounding_metric(self) -> None:
        self.assertEqual(ev.model_calls_per_1000(12, 3000), 4.0)

    def test_labelling_sheet_is_ready_to_fill_in(self) -> None:
        from app.events import Event, Modality
        tmp = Path(tempfile.mkdtemp(prefix="quill_sheet_"))
        store = Store(db_path=tmp / "t.db", audio_dir=tmp / "a")
        t0 = 1_700_000_000.0
        for i in range(3):
            store.insert(Event(time=t0 + i, modality=Modality.INPUT, raw="click",
                               summary="click", source="desktop.click",
                               meta={"window": "Docs - Chromium"}))
        sheet = ev.labelling_sheet(store, t0=t0 - 1, t1=t0 + 100)
        self.assertEqual(len(sheet), 3)
        self.assertEqual(sheet[0]["label_project"], "")
        self.assertEqual(sheet[0]["window"], "Docs - Chromium")
        truth, bounds = ev.load_labels([
            {**sheet[0], "label_project": "Ravenry", "label_boundary": "1"},
            {**sheet[1], "label_project": "Ravenry"},
            {**sheet[2], "label_project": ""}])
        self.assertEqual(truth[sheet[0]["event_id"]], "Ravenry")
        self.assertIsNone(truth[sheet[2]["event_id"]])
        self.assertEqual(bounds, [sheet[0]["time"]])

    def test_score_run_grades_a_replay(self) -> None:
        eps = [{"started_at": 100.0, "ended_at": 200.0, "node_type": "entity",
                "title": "Ravenry", "_event_ids": [(1, False), (2, True)]},
               {"started_at": 200.0, "ended_at": 300.0, "node_type": None,
                "title": "Firefox", "_event_ids": [(3, True)]}]
        got = ev.score_run(eps, {1: "Ravenry", 2: "Ravenry", 3: None},
                           [100.0, 200.0])
        self.assertEqual(got["boundaries"]["f1"], 1.0)
        self.assertEqual(got["attribution"]["precision"], 1.0)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
