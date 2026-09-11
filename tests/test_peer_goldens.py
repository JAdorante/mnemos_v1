"""The peer golden set itself (Phase 0.2) — keeps the harness honest.

A golden set that can silently stop asserting is worse than none, so this
pins the fixture's shape and the scorer's semantics rather than re-running the
eval (which `make eval-peer` owns).

The load-bearing property: goldens assert on CLAIMS (seeded fact tags), never
on expected prose. That is what lets them survive Phase 1 changing the egress
output shape from a bare string to {claims, as_of, prose}.
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

ROOT = Path(__file__).resolve().parent.parent
GOLDEN = ROOT / "tests" / "fixtures" / "goldens" / "peer_asks.jsonl"


def _eval_mod():
    spec = importlib.util.spec_from_file_location(
        "eval_peer", ROOT / "scripts" / "eval_peer.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class GoldenFixtureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cases = [json.loads(ln) for ln in
                      GOLDEN.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_enough_cases_with_unique_ids(self) -> None:
        self.assertGreaterEqual(len(self.cases), 10)
        ids = [c["id"] for c in self.cases]
        self.assertEqual(len(ids), len(set(ids)))

    def test_every_case_asserts_on_tags_not_prose(self) -> None:
        """The property that makes these survive Phase 1."""
        for c in self.cases:
            self.assertNotIn("expect_text", c, c["id"])
            self.assertNotIn("expected_answer", c, c["id"])
            self.assertIn("expect_tags", c, c["id"])
            seeded = {r["tag"] for r in c.get("seed") or []}
            for tag in (c.get("expect_tags") or []) + (c.get("forbid_tags") or []):
                self.assertIn(tag, seeded,
                              f"{c['id']}: {tag} is asserted but never seeded")

    def test_every_case_has_a_question_and_a_shape(self) -> None:
        for c in self.cases:
            self.assertTrue(c.get("question"), c["id"])
            self.assertIn(c.get("shape"), ("status", "lookup"), c["id"])

    def test_status_cases_seed_a_newer_and_an_older_fact(self) -> None:
        """A recency assertion needs something to be wrong about."""
        for c in self.cases:
            if c.get("shape") != "status":
                continue
            ages = [r.get("days_ago", 0) for r in c.get("seed") or []]
            self.assertTrue(ages, c["id"])
            if len(ages) > 1:
                self.assertNotEqual(min(ages), max(ages), c["id"])

    def test_freshness_bounds_are_present_where_claimed(self) -> None:
        for c in self.cases:
            if c.get("expect_empty"):
                continue
            self.assertIn("freshness_max_age_days", c, c["id"])

    def test_known_gaps_name_the_phase_that_closes_them(self) -> None:
        """A known-fail marker with no closing phase is a permanent excuse."""
        for c in self.cases:
            marker = c.get("known_fail_until")
            if marker is None:
                continue
            self.assertRegex(marker, r"^phase-\d$", c["id"])
            self.assertIn("note", c,
                          f"{c['id']}: a known gap must explain itself")

    def test_the_suite_covers_both_leak_and_recency_axes(self) -> None:
        self.assertTrue(any(c.get("forbid_tags") for c in self.cases))
        self.assertTrue(any(c.get("shape") == "status" for c in self.cases))
        self.assertTrue(any(c.get("expect_empty") for c in self.cases))


class ScorerTests(unittest.TestCase):
    """The scorer must read Phase 1 structure when it appears, and fall back
    to prose until then — otherwise the goldens quietly stop measuring."""

    def setUp(self) -> None:
        self.mod = _eval_mod()
        self.seeded = {
            "a": {"fact_id": 11, "text": "Boost Run gave us free compute",
                  "ts": self.mod.NOW, "speaker": "Andy",
                  "tokens": self.mod._tokens("Boost Run gave us free compute")},
            "b": {"fact_id": 12, "text": "Dave's salary is 220k",
                  "ts": self.mod.NOW, "speaker": "Justin",
                  "tokens": self.mod._tokens("Dave's salary is 220k")},
        }

    def test_prose_fallback_finds_the_supporting_fact(self) -> None:
        out = {"text": "- Boost Run gave us free compute"}
        self.assertEqual(self.mod._cited(out, self.seeded), {"a"})

    def test_structured_claims_are_matched_by_fact_id(self) -> None:
        out = {"claims": [{"text": "paraphrased entirely differently",
                           "fact_id": 11}]}
        self.assertEqual(self.mod._cited(out, self.seeded), {"a"})

    def test_structured_claims_win_over_prose(self) -> None:
        """Phase 1 ships both; the ids are the ground truth."""
        out = {"claims": [{"text": "x", "fact_id": 12}],
               "text": "Boost Run gave us free compute"}
        self.assertEqual(self.mod._cited(out, self.seeded), {"b"})

    def test_empty_answer_cites_nothing(self) -> None:
        self.assertEqual(self.mod._cited({"text": ""}, self.seeded), set())

    def test_stopwords_alone_do_not_count_as_a_citation(self) -> None:
        out = {"text": "I know what you said about the update."}
        self.assertEqual(self.mod._cited(out, self.seeded), set())

    def test_fake_retriever_ranks_overlap_then_recency(self) -> None:
        search = self.mod._fake_search(self.seeded)
        hits = search("what about the free compute from Boost Run?")
        self.assertTrue(hits)
        self.assertEqual(hits[0]["fact_id"], 11)

    def test_fake_retriever_returns_nothing_on_no_overlap(self) -> None:
        search = self.mod._fake_search(self.seeded)
        self.assertEqual(search("what did legal say about Trillium?"), [])


if __name__ == "__main__":
    unittest.main()
