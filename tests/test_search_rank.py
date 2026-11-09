"""Chat-grounding search: hit passages, the search-section budget, the
semantic query, and the reranker (app/services/search_rank.py). All behind
flags that default off, so the default path is pinned unchanged too."""
from __future__ import annotations

import os
import unittest
from contextlib import contextmanager

from app.services import grounding, search_rank


@contextmanager
def env(**kv):
    prev = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: str(v) for k, v in kv.items()})
    try:
        yield
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


MAIL = {"modality": "document", "summary": "[google.mail] Pricing - approved",
        "raw": ("Hi all, quick recap from the deal review. "
                "We agreed to offer Core plus the free intake add-on. "
                "The reason is that intake is what drives renewals for clinics. "
                "Separately, the offsite moved to the 14th. " * 3)}


class PassageTests(unittest.TestCase):
    def test_default_renders_the_title_as_before(self):
        with env(QUILL_GROUND_PASSAGES="0"):
            self.assertEqual(grounding._hit_text(MAIL, "why intake add-on"),
                             MAIL["summary"])

    def test_passage_is_the_best_matching_stretch(self):
        with env(QUILL_GROUND_PASSAGES="1"):
            t = grounding._hit_text(MAIL, "why did we pick the intake add-on?")
        self.assertTrue(t.startswith("[google.mail] Pricing - approved — "))
        self.assertIn("free intake add-on", t)
        self.assertLessEqual(len(t), len(MAIL["summary"]) + 3 +
                             grounding._PASSAGE_CHARS + 4)

    def test_speech_and_facts_keep_their_summary(self):
        audio = {"modality": "audio", "summary": "call about pricing",
                 "raw": "um so yeah the intake add on " * 30}
        fact = {"modality": "fact:claim", "summary": "Core is $1,350",
                "raw": "Core is $1,350", "is_fact": True}
        with env(QUILL_GROUND_PASSAGES="1"):
            self.assertEqual(grounding._hit_text(audio, "intake"), "call about pricing")
            self.assertEqual(grounding._hit_text(fact, "core"), "Core is $1,350")

    def test_chunk_text_wins_over_raw(self):
        hit = {**MAIL, "chunk_text": "Appendix B: intake pricing table."}
        with env(QUILL_GROUND_PASSAGES="1"):
            self.assertIn("Appendix B", grounding._hit_text(hit, "intake"))

    def test_best_window_marks_elisions(self):
        text = "Alpha one. Beta two. Gamma intake three. Delta four."
        w = grounding.best_window(text, "intake", limit=25)
        self.assertIn("Gamma intake three.", w)
        self.assertTrue(w.startswith("… "))


class QueryAndBudgetTests(unittest.TestCase):
    GOAL = ("RELEVANT CONTEXT (from Sparrow memory — quoted records):\n"
            "- lots of planner context here\n\nCurrent task: who owns GIS?")

    def test_semantic_query_defaults_to_the_goal(self):
        with env(QUILL_GROUND_SEM_QUERY="goal"):
            self.assertEqual(grounding.semantic_query(self.GOAL), self.GOAL)

    def test_semantic_query_task_mode_takes_the_request(self):
        with env(QUILL_GROUND_SEM_QUERY="task"):
            self.assertEqual(grounding.semantic_query(self.GOAL), "who owns GIS?")
            self.assertEqual(grounding.semantic_query("plain question"),
                             "plain question")

    def test_plain_cut_without_reserve(self):
        secs = [["HEAD", "x" * 3000], ["RELEVANT MEMORIES:", "- the answer"]]
        out = grounding.assemble_block(secs, 1, 0)
        self.assertNotIn("the answer", out)
        self.assertEqual(len(out), grounding._MAX_BLOCK_CHARS + 1)

    def test_reserve_keeps_the_search_section(self):
        secs = [["HEAD", "x" * 3000], ["RELEVANT MEMORIES:", "- the answer"],
                ["RECENT ACTIVITY:", "- y"]]
        out = grounding.assemble_block(secs, 1, 1000)
        self.assertIn("- the answer", out)
        self.assertIn("RECENT ACTIVITY", out)
        self.assertTrue(out.startswith("HEAD"))
        self.assertLessEqual(len(out), grounding._MAX_BLOCK_CHARS + 1)

    def test_short_block_is_untouched(self):
        secs = [["A", "b"], ["C", "d"]]
        self.assertEqual(grounding.assemble_block(secs, 1, 1000), "A\nb\n\nC\nd")


class FakeStore:
    def __init__(self):
        self.people = [{"name": "Rachel Osei"}, {"name": "Rachel Kim"},
                       {"name": "Hidden Person", "hide_from_people": True}]
        self.entities = [{"name": "Bayview Clinic Group"}, {"name": "Review"}]

    def all_people(self):
        return self.people

    def all_entities(self):
        return self.entities

    def event_ids_at(self, t, limit=1):
        return [int(t)]

    def fact_spans_for_event(self, eid):
        return [{}] * {300: 6}.get(eid, 0)

    def get_fact(self, fid):
        return {"review": "approved"} if fid == 7 else {"review": None}


class RerankTests(unittest.TestCase):
    def test_query_context_names_people_entities_and_recency(self):
        ctx = search_rank.query_context(
            "what did Rachel Osei's latest email say about Bayview?", FakeStore())
        self.assertEqual({p["name"] for p in ctx["people"]},
                         {"Rachel Osei", "Rachel Kim"})      # first-name match
        self.assertEqual(ctx["entities"], ["bayview clinic group"])
        self.assertTrue(ctx["recency_cue"])
        self.assertFalse(search_rank.query_context("who owns it?", FakeStore())
                         ["recency_cue"])

    def test_participant_tag_and_recency_lift_the_right_item(self):
        hits = [
            {"score": 0.40, "time": 100, "summary": "[google.mail] Lunch",
             "raw": "anyone for lunch", "meta": {"from": "Sam <s@x>"}},
            {"score": 0.38, "time": 200, "summary": "[google.mail] Bayview demo",
             "raw": "please move the demo", "meta": {"from": "Rachel Osei <r@x>"}},
        ]
        out = search_rank.rerank(hits, "Rachel's latest email on Bayview?",
                                 store=FakeStore())
        self.assertIn("Bayview demo", out[0]["summary"])
        self.assertEqual(out[0]["rank_features"],
                         {"person": 1.0, "tag": 1.0, "recency": 1.0})

    def test_mention_counts_less_than_participation(self):
        h = {"score": 0.3, "time": 1, "raw": "Rachel Osei asked for it",
             "meta": {"from": "Sam"}}
        f = search_rank.features(h, search_rank.query_context(
            "Rachel Osei", FakeStore()))
        self.assertEqual((f["person"], f["person_mention"]), (0.0, 1.0))

    def test_references_and_approval_read_the_store(self):
        ev = {"score": 0.2, "time": 300, "raw": "x"}
        fact = {"score": 0.2, "is_fact": True, "fact_id": 7, "raw": "y"}
        ctx = search_rank.query_context("x", FakeStore())
        self.assertGreater(search_rank.features(ev, ctx, store=FakeStore())
                           ["references"], 0.8)
        self.assertEqual(search_rank.features(fact, ctx, store=FakeStore())
                         ["approved"], 1.0)

    def test_no_signal_keeps_memory_search_order(self):
        hits = [{"score": 0.5, "time": 0, "raw": "a"},
                {"score": 0.4, "time": 0, "raw": "b"}]
        out = search_rank.rerank([dict(h) for h in hits], "nothing named",
                                 store=None)
        self.assertEqual([h["raw"] for h in out], ["a", "b"])

    def test_weights_env_override(self):
        with env(QUILL_SEARCH_RERANK_WEIGHTS="person=0,bogus=3,tag=x"):
            self.assertEqual(search_rank.env_weights(), {"person": 0.0})

    def test_flag_defaults_off(self):
        prev = os.environ.pop("QUILL_SEARCH_RERANK", None)
        try:
            self.assertFalse(search_rank.enabled())
        finally:
            if prev is not None:
                os.environ["QUILL_SEARCH_RERANK"] = prev


if __name__ == "__main__":
    unittest.main()
