"""Entity typing golden set: the real pipeline against labelled sightings.

`tests/fixtures/goldens/entity_typing.jsonl` holds seats and their sightings:
most are synthetic companies from unrelated industries, a minority are the
source plan's own examples of the failure (the user's seat, with an invented
user name). `scripts/eval_entity_typing.py` replays each seat on its own
fresh Store through the extractor's entity path (the mint gate, the
candidate ledger and promotion, the kind vote) and the scope pass, then
scores what Sparrow made of every name.

The floors: project precision >= 0.95 overall and for every seat with at
least two project cases (seats under that are gated together, so one case
cannot decide a seat alone, and no seat can hide behind another), and
overall accuracy (kind and parent) >= 0.95, and no miss among the cases one
deterministic rule decides. Breaking the rule that a model "project" is
never a project fails the gate in every seat, and breaking the code-shape,
lowercase-description or scope rule fails it too.

The rules were tuned on the same synthetic worlds these seats come from, so
this is a regression gate, not evidence that the rules generalise.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "eval_entity_typing", ROOT / "scripts" / "eval_entity_typing.py")
golden = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(golden)


class EntityTypingGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = golden.load()
        cls.report = golden.evaluate(cls.data)

    def test_the_set_is_mostly_other_industries(self):
        seats = self.data["seats"]
        worlds = [s for s, v in seats.items() if v["origin"] == "world"]
        self.assertGreaterEqual(len(worlds), 20)
        self.assertGreaterEqual(len({seats[s]["industry"] for s in worlds}),
                                15)
        by_origin = self.report["mentions_by_origin"]
        self.assertGreaterEqual(self.report["mentions"], 150)
        # The user's own seat is a minority of the labelled mentions.
        self.assertLess(by_origin.get("plan", 0),
                        0.2 * self.report["mentions"])
        # Every world seat carries enough project cases to be gated alone,
        # and few enough that one false project takes it under the floor.
        for seat in worlds:
            row = self.report["per_seat"][seat]
            self.assertEqual(row["gated"], "own", seat)
            self.assertLess(row["tp"] / (row["tp"] + 1),
                            golden.PROJECT_FLOOR, seat)

    def test_every_world_seat_can_show_a_false_project(self):
        # A seat's own precision gate means something only when the seat has
        # a name the model called a project that is no project, seen in two
        # sessions from sources that may mint (so it would be promoted).
        from collections import defaultdict
        sightings = defaultdict(list)
        for m in self.data["mentions"]:
            sightings[(m["seat"], golden._key(m["name"]))].append(m)
        chances = defaultdict(int)
        for (seat, _), ms in sightings.items():
            minting = [m for m in ms if m["source"] not in ("note", "screen")]
            if ms[0]["expect"]["kind"] != "project" \
                    and any(m["proposed_kind"] == "project" for m in ms) \
                    and len({m["session"] for m in minting}) >= 2:
                chances[seat] += 1
        for seat, v in self.data["seats"].items():
            if v["origin"] == "world":
                self.assertGreaterEqual(chances[seat], 1, seat)

    def test_every_kind_of_case_is_covered(self):
        cats = set(self.report["by_category"])
        for c in ("filed_project", "filed_project_other_kind",
                  "filed_company", "org_recurring", "org_once",
                  "org_email_domain", "model_project_topic",
                  "model_project_in_project", "code_id_in_project",
                  "code_named_tool", "description", "value", "bind_only",
                  "typed_once", "person_name", "place_recurring"):
            self.assertIn(c, cats)

    def test_project_precision_overall(self):
        p = self.report["project"]
        self.assertGreaterEqual(p["precision"], golden.PROJECT_FLOOR, p)

    def test_project_precision_per_seat(self):
        for seat, row in self.report["per_seat"].items():
            if row["gated"] == "own" and row["precision"] is not None:
                self.assertGreaterEqual(row["precision"],
                                        golden.PROJECT_FLOOR, (seat, row))
        pooled = self.report["pooled_small_seats"]
        if pooled:
            self.assertEqual(pooled["fp"], 0, pooled)

    def test_overall_accuracy(self):
        self.assertGreaterEqual(self.report["accuracy"],
                                golden.ACCURACY_FLOOR,
                                self.report["misses"])
        self.assertTrue(self.report["passed"], self.report["failures"])

    def test_the_gate_exit_code(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with patch.object(golden, "evaluate", return_value=self.report):
                self.assertEqual(golden.main(["--gate"]), 0)
            bad = {**self.report, "passed": False, "failures": ["x"]}
            with patch.object(golden, "evaluate", return_value=bad):
                self.assertEqual(golden.main(["--gate"]), 1)
                self.assertEqual(golden.main([]), 0)       # report only
        self.assertIn("FAIL: x", out.getvalue())

    def test_breaking_the_project_demotion_fails_the_gate(self):
        # A model "project" voting for `project` again (the failure this
        # whole build exists to stop) must fail the gate, overall and in
        # the seats where it happens.
        from app.services import entity_typing
        real = entity_typing.mention_kind

        def broken(proposed, *, code, scoped):
            k = entity_typing._norm_kind(proposed)
            return "project" if k == "project" else real(
                proposed, code=code, scoped=scoped)
        with patch.object(entity_typing, "mention_kind", broken):
            rep = golden.evaluate(self.data)
        self.assertFalse(rep["passed"])
        self.assertLess(rep["project"]["precision"], golden.PROJECT_FLOOR)
        # Every world seat fails on its own: none hides behind another.
        failed = {f.split(":")[0] for f in rep["failures"]}
        worlds = {s for s, v in self.data["seats"].items()
                  if v["origin"] == "world"}
        self.assertEqual(worlds - failed, set(), rep["failures"])

    def test_no_rule_case_is_wrong(self):
        r = self.report["rule_cases"]
        self.assertGreaterEqual(r["cases"], 60)
        self.assertEqual(r["ok"], r["cases"], self.report["misses"])

    def test_breaking_another_rule_fails_the_gate(self):
        # The accuracy floor alone would let each of these pass: the rule
        # cases are what catch them.
        from app.services import entity_scope, entity_typing
        breaks = {
            "code_shape": [patch.object(entity_typing, "code_like",
                                        lambda name: False)],
            "lowercase_description": [
                patch.object(entity_typing, "common_noun_like",
                             lambda name: False),
                patch.object(entity_typing, "weak_name",
                             lambda name, kinds=(): False)],
            "scope": [patch.object(entity_scope, "run",
                                   lambda store, **kw: {})],
        }
        for rule, patches in breaks.items():
            with self.subTest(rule=rule):
                for p in patches:
                    p.start()
                try:
                    rep = golden.evaluate(self.data)
                finally:
                    for p in patches:
                        p.stop()
                self.assertFalse(rep["passed"], rule)
                self.assertLess(rep["rule_cases"]["ok"],
                                rep["rule_cases"]["cases"], rule)

    def test_a_small_seat_is_pooled_and_its_false_project_still_fails(self):
        cases = [{"seat": "a", "origin": "world", "industry": "x",
                  "category": "c", "expected": "idea", "predicted": "project",
                  "ok": False},
                 {"seat": "b", "origin": "world", "industry": "y",
                  "category": "c", "expected": "project",
                  "predicted": "project", "ok": True},
                 {"seat": "b", "origin": "world", "industry": "y",
                  "category": "c", "expected": "project",
                  "predicted": "project", "ok": True}]
        rep = golden.score(cases)
        self.assertEqual(rep["per_seat"]["a"]["gated"], "pooled")
        self.assertEqual(rep["per_seat"]["b"]["gated"], "own")
        # Seat a's one false project is not its own verdict, but it is not
        # hidden either: the pooled small seats fail.
        self.assertTrue(any("pooled" in f for f in rep["failures"]))
        # Not even behind thirty small seats whose one project is right.
        many = cases[:1] + [
            {"seat": f"s{i}", "origin": "world", "industry": "z",
             "category": "c", "expected": "project", "predicted": "project",
             "ok": True} for i in range(30)]
        rep = golden.score(many)
        self.assertGreaterEqual(rep["pooled_small_seats"]["precision"],
                                golden.PROJECT_FLOOR)
        self.assertFalse(rep["passed"])
        self.assertTrue(any("pooled" in f for f in rep["failures"]))

    def test_a_name_folded_into_another_row_makes_no_row_of_its_own(self):
        # The resolver folding a name into a project row as an alias made no
        # project row: an accuracy miss, never a predicted project.
        import tempfile
        from app.storage import Store
        tmp = tempfile.mkdtemp(prefix="golden_fold_")
        st = Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "a")
        try:
            pid = st.resolve_entity("Osprey Rollout", "project", ts=1.0,
                                    kind_source="filing")
            st.touch_entity(pid, 2.0, alias="Osprey Rollout Timeline")
            got = golden._outcomes(st, [{"name": "Osprey Rollout Timeline"},
                                        {"name": "Osprey Rollout"}])
        finally:
            st.close()
        fold = got[golden._key("Osprey Rollout Timeline")]
        self.assertEqual((fold["kind"], fold["folded_into"]),
                         ("alias", "Osprey Rollout"))
        self.assertEqual(got[golden._key("Osprey Rollout")]["kind"], "project")
        rep = golden.score([
            {"seat": "s", "origin": "world", "industry": "x", "category": "c",
             "expected": "idea", "predicted": "alias", "ok": False},
            {"seat": "s", "origin": "world", "industry": "x", "category": "c",
             "expected": "project", "predicted": "project", "ok": True},
            {"seat": "s", "origin": "world", "industry": "x", "category": "c",
             "expected": "project", "predicted": "project", "ok": True}])
        self.assertEqual((rep["project"]["tp"], rep["project"]["fp"]), (2, 0))

    def test_a_label_that_disagrees_with_itself_is_refused(self):
        import json
        import tempfile
        rows = [{"type": "seat", "seat": "s", "origin": "world",
                 "industry": "x", "filed": []},
                {"type": "mention", "seat": "s", "name": "Atlas",
                 "context": "a", "source": "mail", "session": 1,
                 "expect": {"kind": "org", "parent": None}},
                {"type": "mention", "seat": "s", "name": "atlas",
                 "context": "b", "source": "mail", "session": 2,
                 "expect": {"kind": "idea", "parent": None}}]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl",
                                         delete=False) as f:
            f.write("".join(json.dumps(r) + "\n" for r in rows))
        with self.assertRaises(ValueError):
            golden.load(f.name)
        Path(f.name).unlink()


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
