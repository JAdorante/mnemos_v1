"""Tests for structured chat grounding (services/grounding.py).

Policy under test: people questions traverse the knowledge graph, task
questions query the reviewed facts table, semantic timeline search is the
FALLBACK layer (summaries over raw), the email-ish loose-promise guard keeps
its goal-overlap exception, and every layer is individually best-effort.
Store, graph, memory search, and activity are all faked.
"""
from __future__ import annotations

import unittest
from unittest import mock

from app.services import grounding as gr


class _FakeStore:
    def __init__(self, people=None, tasks=None):
        self._people = people or []
        self._tasks = tasks or []

    def all_people(self):
        return self._people

    def list_facts(self, kind=None, status=None, limit=200, actionable=False):
        # Fake rows carry no event_source, so `actionable` filters nothing here
        # — the real gate is exercised in tests/test_screen_work_gate.py.
        return [f for f in self._tasks
                if (kind is None or f.get("kind") == kind)
                and (status is None or f.get("status") == status)][:limit]


def _ctx_found(name="Connor Kane"):
    return {"found": True, "person": {"name": name},
            "items": [{"predicate": "committed", "text": "send the demo link",
                       "status": "open"},
                      {"predicate": "mentioned_in", "text": "stocks chat",
                       "status": None}],
            "discussed_with": [{"name": "Kin", "weight": 2.0}],
            "affiliations": [{"name": "Acme", "kind": "org"}]}


class GroundingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._search = mock.patch("app.services.memory.memory.search",
                                  return_value=[])
        self._search.start()
        self._act = mock.patch("app.services.activity.describe_recent",
                               return_value=[])
        self._act.start()

    def tearDown(self) -> None:
        self._search.stop()
        self._act.stop()

    def test_person_question_traverses_graph(self) -> None:
        store = _FakeStore(people=[{"name": "Connor Kane"}])
        with mock.patch("app.services.graph.context_for_person",
                        return_value=_ctx_found()) as cfp:
            out = gr.compose("What do you know about Connor Kane?", store=store)
        cfp.assert_called_once()
        self.assertIn("KNOWN PERSON: Connor Kane", out["block"])
        self.assertIn("committed: send the demo link [open]", out["block"])
        self.assertIn("often comes up with: Kin", out["block"])
        self.assertIn("affiliated with: Acme", out["block"])

    def test_name_needs_word_boundary(self) -> None:
        store = _FakeStore(people=[{"name": "Kin"}])   # 'Kin' inside 'working'
        with mock.patch("app.services.graph.context_for_person") as cfp:
            gr.compose("What was I working on?", store=store)
        cfp.assert_not_called()

    def test_task_question_queries_facts_table(self) -> None:
        store = _FakeStore(tasks=[
            {"kind": "task", "status": "open", "text": "email the dataset",
             "extracted_at": 2.0, "review": "approved"},
            {"kind": "task", "status": "done", "text": "old thing",
             "extracted_at": 1.0},
        ])
        out = gr.compose("What tasks are still open this week?", store=store)
        self.assertIn("OPEN TASKS", out["block"])
        self.assertIn("email the dataset", out["block"])
        self.assertNotIn("old thing", out["block"])            # done ≠ open

    def test_non_task_question_skips_facts_table(self) -> None:
        store = _FakeStore(tasks=[{"kind": "task", "status": "open",
                                   "text": "email the dataset",
                                   "extracted_at": 1.0}])
        out = gr.compose("What's the capital of France?", store=store)
        self.assertNotIn("OPEN TASKS", out["block"])

    def test_semantic_fallback_prefers_summary(self) -> None:
        hits = [{"modality": "audio", "raw": "raw asr junk",
                 "summary": "clean summary", "score": 0.5}]
        with mock.patch("app.services.memory.memory.search", return_value=hits):
            out = gr.compose("anything", store=_FakeStore())
        self.assertIn("clean summary", out["block"])
        self.assertNotIn("raw asr junk", out["block"])
        self.assertEqual(out["hits"], hits)

    def test_email_guard_keeps_overlapping_promise(self) -> None:
        hits = [
            {"modality": "audio", "raw": "I'll call you after my dentist",
             "score": 0.5},                                     # unrelated
            {"modality": "audio", "raw": "I'll send the dataset to Ramos",
             "score": 0.5},                                     # overlaps goal
        ]
        with mock.patch("app.services.memory.memory.search", return_value=hits):
            out = gr.compose("email Ramos the dataset", store=_FakeStore(),
                             email_guard=True)
        self.assertIn("send the dataset", out["block"])
        self.assertNotIn("dentist", out["block"])

    def test_layer_failure_degrades_not_breaks(self) -> None:
        store = _FakeStore(people=[{"name": "Connor Kane"}])
        hits = [{"modality": "audio", "raw": "still here", "score": 0.5}]
        with mock.patch("app.services.graph.context_for_person",
                        side_effect=RuntimeError("graph down")), \
             mock.patch("app.services.memory.memory.search", return_value=hits):
            out = gr.compose("What about Connor Kane?", store=store)
        self.assertIn("still here", out["block"])
        self.assertNotIn("KNOWN PERSON", out["block"])

    def test_watching_question_searches_vision_modality(self) -> None:
        vision_hits = [{"modality": "vision", "raw": "YouTube: Mao documentary",
                        "score": 0.5}]

        def search(q, limit=20, modality=None):
            return vision_hits if modality == "vision" else []

        with mock.patch("app.services.memory.memory.search", side_effect=search):
            out = gr.compose("What was I watching?", store=_FakeStore())
        self.assertIn("YouTube: Mao documentary", out["block"])
        with mock.patch("app.services.memory.memory.search", side_effect=search):
            out = gr.compose("What was I watching earlier?", store=_FakeStore())
        self.assertIn("SCREEN & CAMERA OBSERVATIONS", out["block"])
        self.assertIn("Mao documentary", out["block"])

    def test_people_list_question_uses_contacts_not_ambient(self) -> None:
        roster = [{"id": 1, "name": "Patrick Adorante", "promotion_state": "active"}]
        with mock.patch("app.services.people_pipeline.contacts_roster",
                        return_value=roster), \
             mock.patch("app.services.working_memory.ensure_fresh"), \
             mock.patch("app.services.working_memory.snapshot",
                        return_value=[{"node_type": "person", "node_id": 99,
                                      "label": "Bill Clinton"}]), \
             mock.patch("app.services.working_memory.render_lines",
                        return_value=["WORKING SET:", "- Bill Clinton"]):
            out = gr.compose(
                "Can you give me a list of all the people I know?",
                store=_FakeStore())
        self.assertIn("PEOPLE YOU KNOW", out["block"])
        self.assertIn("Patrick Adorante", out["block"])
        self.assertNotIn("Bill Clinton", out["block"])
        self.assertNotIn("WORKING SET", out["block"])
        self.assertNotIn("RECENT DESKTOP ACTIVITY", out["block"])
        labels = [s["label"] for s in out["sources"]]
        self.assertIn("people you know", labels)

    def test_non_screen_question_skips_vision_layer(self) -> None:
        with mock.patch("app.services.memory.memory.search",
                        return_value=[]) as search:
            gr.compose("What did I say about stocks?", store=_FakeStore())
        for call in search.call_args_list:
            self.assertIsNone(call.kwargs.get("modality"))

    def test_sources_name_the_sections_used(self) -> None:
        # "Show sources": compose reports one labeled entry per section it
        # actually used, with the content lines for the UI's collapsible view.
        store = _FakeStore(
            people=[{"name": "Connor Kane"}],
            tasks=[{"kind": "task", "status": "open", "text": "email the dataset",
                    "extracted_at": 2.0}])
        hits = [{"modality": "audio", "raw": "stocks chat", "score": 0.5}]
        with mock.patch("app.services.graph.context_for_person",
                        return_value=_ctx_found()), \
             mock.patch("app.services.memory.memory.search", return_value=hits), \
             mock.patch("app.services.onboarding.load_profile", return_value=None):
            out = gr.compose("What tasks did Connor Kane leave open?",
                             store=store)
        labels = [s["label"] for s in out["sources"]]
        # Identity is always the first section (who the assistant / user is);
        # the clock line grounds right after it (July 28: reordered — the
        # clock layer must never displace identity from the front).
        self.assertEqual(labels, ["identity",
                                  "clock",
                                  "person graph: Connor Kane",
                                  "open tasks & commitments",
                                  "timeline memories"])
        person = out["sources"][2]
        self.assertGreaterEqual(person["n"], 2)
        self.assertTrue(any("send the demo link" in it
                            for it in person["items"]))

    def test_identity_is_the_baseline_section(self) -> None:
        # Identity always grounds FIRST, so even an empty store yields the
        # identity section at the front (never nothing) — that's the floor
        # "who am I?" rests on. The always-on clock line follows it.
        with mock.patch("app.services.onboarding.load_profile", return_value=None):
            out = gr.compose("anything", store=_FakeStore())
        labels = [s["label"] for s in out["sources"]]
        self.assertEqual(labels[0], "identity")
        self.assertEqual(labels, ["identity", "clock"])
        self.assertTrue(out["block"].startswith("ABOUT YOU"))

    def test_block_is_capped(self) -> None:
        hits = [{"modality": "audio", "raw": "x" * 1000, "score": 0.5}
                for _ in range(10)]
        with mock.patch("app.services.memory.memory.search", return_value=hits):
            out = gr.compose("anything", store=_FakeStore(), semantic_limit=10)
        self.assertLessEqual(len(out["block"]), gr._MAX_BLOCK_CHARS + 1)

    def test_tell_me_route_uses_speaker_beliefs(self) -> None:
        store = _FakeStore(people=[{"name": "David"}])
        beliefs = [{
            "predicate": {
                "subj_type": "entity", "subj_id": 1,
                "predicate": "costs", "obj_type": "entity", "obj_id": 2,
            },
            "evidence": [{"quote": "pilot plan is $49 a month"}],
            "conflict": False,
        }]
        with mock.patch("app.services.kg_beliefs.beliefs_by_speaker",
                        return_value=beliefs) as bbs, \
             mock.patch("app.services.grounding._node_label",
                        side_effect=lambda s, t, i: (
                            "pilot plan" if i == 1 else "$49")):
            out = gr.compose(
                "What did David tell me about the price?",
                store=store, allow_llm_route=False)
        bbs.assert_called()
        self.assertEqual(out["route"]["route"], "speaker_beliefs")
        self.assertIn("BELIEFS ATTRIBUTED TO", out["block"])
        self.assertIn("$49", out["block"])
        labels = [s["label"] for s in out["sources"]]
        self.assertTrue(any(l.startswith("beliefs from") for l in labels))

    def test_changed_route_uses_field_diff(self) -> None:
        store = _FakeStore()
        diff = {
            "entered_focus": ["person:2"],
            "left_focus": ["fact:1"],
            "rising": [{"id": "person:1", "delta": 0.1}],
            "aging": [],
            "has_prior": True,
        }
        with mock.patch("app.services.field_history.diff",
                        return_value=diff) as fd, \
             mock.patch.object(store, "list_reflections",
                               return_value=[{
                                   "summary": "Pricing talk moved up",
                                   "period_end": 1e12,
                                   "created_at": 1e12,
                               }], create=True):
            out = gr.compose(
                "What changed since last week?",
                store=store, allow_llm_route=False)
        fd.assert_called()
        self.assertEqual(out["route"]["route"], "field_delta")
        self.assertIn("WHAT CHANGED SINCE", out["block"])
        self.assertIn("entered focus", out["block"])
        self.assertIn("Pricing talk moved up", out["block"])
        labels = [s["label"] for s in out["sources"]]
        self.assertTrue(any(l.startswith("changes since") for l in labels))

    def test_regex_route_skips_llm(self) -> None:
        with mock.patch("app.services.model_router.router.complete_json") as cj:
            hit = gr.classify_query_route(
                "What did David tell me?", allow_llm=True)
        self.assertEqual(hit["via"], "regex")
        cj.assert_not_called()

    def test_llm_route_only_on_soft_miss(self) -> None:
        with mock.patch.dict("os.environ", {"QUILL_QUERY_ROUTE_LLM": "1"}), \
             mock.patch("app.services.model_router.router.complete_json",
                        return_value={
                            "route": "speaker_beliefs",
                            "speaker": "David",
                            "since": None,
                        }) as cj:
            # Soft signal but not an exact regex shape.
            hit = gr.classify_query_route(
                "Remind me what David mentioned earlier", allow_llm=True)
        cj.assert_called_once()
        self.assertEqual(hit["route"], "speaker_beliefs")
        self.assertEqual(hit["via"], "llm")

    def test_paired_peer_hostname_grounds_who_is(self) -> None:
        roster = [{
            "id": 1, "name": "User 2", "base_url": "http://192.168.86.42:8000",
            "presence": "offline", "person_id": None, "person_name": None,
        }]
        with mock.patch("app.services.peer_channel.peers", return_value=roster):
            out = gr.compose("Who is User 2?", store=_FakeStore())
        blob = out["block"]
        self.assertIn("PAIRED TEAMMATES", blob)
        self.assertIn("User 2", blob)
        self.assertIn("not linked", blob.lower())
        self.assertIn("192.168.86.42", blob)


if __name__ == "__main__":
    unittest.main()


def setUpModule() -> None:
    # Telemetry sandbox: model_log resolves its trail path once at import, so
    # without this every faked model call in this module appends a bogus row
    # (fake models, 0s latency) to the REAL data/model_calls.jsonl trail.
    global _model_log_orig_path
    import tempfile as _tempfile
    from pathlib import Path as _Path
    from app.services.model_log import model_log as _ml
    _model_log_orig_path = _ml._path
    _ml._path = (_Path(_tempfile.mkdtemp(prefix="mnemos-test-telemetry-"))
                 / "model_calls.jsonl")


def tearDownModule() -> None:
    from app.services.model_log import model_log as _ml
    _ml._path = _model_log_orig_path
