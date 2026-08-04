"""Meeting Layer P4 — session-scoped ask + follow-up draft citations."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

NOW = 1_700_000_000.0


def _store(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db", audio_dir=Path(td) / "audio")


def _seed_meeting(store, *, with_decision: bool = True):
    """Insert a meeting reflection + commitment(+decision) items with facts."""
    from app.events import Event, Modality

    ev = Event(
        time=NOW - 500, modality=Modality.AUDIO,
        raw="I'll send the revised pricing by Thursday",
        summary="turn", source="audio.whisper",
    )
    eid = store.insert(ev)
    fid_c = store.add_commitment(
        "send revised pricing by Thursday",
        source_event_id=eid,
        source_span="I'll send the revised pricing by Thursday",
        confidence=0.9, extracted_at=NOW - 470,
    )
    fid_d = None
    if with_decision:
        # Flat claim used as a decision citation stand-in (kind on the item).
        fid_d = store.add_claim(
            "Use the April pricing deck",
            source_event_id=eid,
            source_span="let's use the April pricing deck",
            confidence=0.85, extracted_at=NOW - 460,
        )
    rid = store.add_reflection(
        scope="meeting",
        summary="Pricing call · Work\nDiscussed timeline.",
        period_start=NOW - 520,
        period_end=NOW - 400,
        subject_type="session",
        subject_id=7,
        model="test",
        confidence=0.8,
        created_at=NOW - 390,
    )
    store.add_reflection_item(
        rid, kind="commitment",
        text="Send revised pricing by Thursday",
        detail="Owner: user", subject="pricing",
        confidence=0.9, source_fact_ids=[fid_c], created_at=NOW - 389,
    )
    if fid_d is not None:
        store.add_reflection_item(
            rid, kind="decision",
            text="Use the April pricing deck",
            detail="", subject="",
            confidence=0.85, source_fact_ids=[fid_d], created_at=NOW - 388,
        )
    # Overlapping session for attendee recovery in resolve_scope.
    from app.services.sessions import Session
    store.replace_sessions([Session(
        start=NOW - 520, end=NOW - 400,
        speakers=["user"], text="pricing",
        turn_ids=[], event_ids=[eid],
        n_turns=1, n_utterances=1,
        calendar_event_id="Work|pricing",
        meeting_meta={
            "title": "Pricing call",
            "attendees": [{"name": "Sarah Chen", "email": "sarah@acme.com"}],
        },
    )])
    return rid, fid_c, fid_d


class ResolveScopeTests(unittest.TestCase):
    def test_resolve_by_reflection_id(self):
        from app.services import meeting_chat as mc
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, fid_c, fid_d = _seed_meeting(store)
                scope = mc.resolve_scope(store, meeting_reflection_id=rid)
                self.assertIsNotNone(scope)
                self.assertEqual(scope["reflection_id"], rid)
                self.assertIn(fid_c, scope["fact_ids"])
                self.assertIn(fid_d, scope["fact_ids"])
                self.assertTrue(any(
                    (a.get("email") or "").startswith("sarah")
                    for a in scope.get("attendees") or []
                ))
            finally:
                store.close()


class SourceFactIdsTests(unittest.TestCase):
    def test_draft_ids_from_commitment_and_decision(self):
        from app.services import meeting_chat as mc
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, fid_c, fid_d = _seed_meeting(store)
                fids = mc.source_fact_ids_for_draft(store, rid)
                self.assertEqual(fids, [fid_c, fid_d])
            finally:
                store.close()

    def test_dismissed_items_skipped(self):
        from app.services import meeting_chat as mc
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, fid_c, fid_d = _seed_meeting(store)
                items = store.reflection_items(rid)
                store.review_reflection_item(items[0]["id"], "dismissed")
                fids = mc.source_fact_ids_for_draft(store, rid)
                self.assertEqual(fids, [fid_d])
            finally:
                store.close()


class ComposeScopeTests(unittest.TestCase):
    def test_compose_includes_this_meeting_section(self):
        from app.services.grounding import compose
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, fid_c, fid_d = _seed_meeting(store)
                with patch("app.services.memory.memory") as mem:
                    mem.search.return_value = []
                    g = compose(
                        "What did we commit to?",
                        store=store,
                        meeting_reflection_id=rid,
                        record_attention=False,
                        allow_llm_route=False,
                    )
                labels = [s["label"] for s in g.get("sources") or []]
                self.assertIn("this meeting", labels)
                block = g.get("block") or ""
                self.assertIn("Pricing call", block)
                self.assertIn("Send revised pricing", block)
            finally:
                store.close()


class AskTests(unittest.TestCase):
    def test_ask_returns_answer(self):
        from app.services import meeting_chat as mc
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, _, _ = _seed_meeting(store)

                class FakeRouter:
                    def complete(self, *a, **k):
                        return "Send revised pricing by Thursday."

                with patch("app.services.model_router.router", FakeRouter()), \
                     patch("app.services.memory.memory") as mem, \
                     patch("app.services.answer_check.check_answer") as chk:
                    mem.search.return_value = []
                    chk.side_effect = lambda text, *a, **k: MagicMock(
                        text=text, to_dict=lambda: {"ok": True})
                    out = mc.ask("What did we commit?",
                                 meeting_reflection_id=rid, store=store)
                self.assertTrue(out["ok"])
                self.assertIn("pricing", out["answer"].lower())
                self.assertEqual(out["meeting"]["reflection_id"], rid)
            finally:
                store.close()


class DraftFollowupTests(unittest.TestCase):
    def test_draft_passes_source_fact_ids_to_send(self):
        from app.services import meeting_chat as mc
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, fid_c, fid_d = _seed_meeting(store)
                mock_worker = MagicMock()
                with patch("app.services.agent_bridge.worker", mock_worker):
                    out = mc.draft_followup(rid, store=store, dry_run="draft")
                self.assertTrue(out["ok"])
                self.assertEqual(out["source_fact_ids"], [fid_c, fid_d])
                self.assertEqual(out["to_hint"], "sarah@acme.com")
                kwargs = mock_worker.send.call_args.kwargs
                self.assertEqual(kwargs.get("source_fact_ids"), [fid_c, fid_d])
                self.assertEqual(kwargs.get("fact_id"), fid_c)
                self.assertEqual(kwargs.get("dry_run"), "draft")
            finally:
                store.close()


class GroundedFieldsUnionTests(unittest.TestCase):
    def test_grounded_fields_unions_prior_ids(self):
        from browser_agent.orchestrator import Agent

        class Tiny(Agent):
            def __init__(self):
                # Bypass heavy Agent.__init__
                self._source_fact_id = 10
                self._grounded_source_ids = [10, 20, 30]
                self._source_provider = lambda fid: {
                    "fact_id": int(fid),
                    "block": f"Fact #{fid}\nquote",
                }

        a = Tiny()
        fields = a._grounded_fields({"body": "hi", "to": "x@y.com"})
        self.assertIn("Fact #10", fields["source"])
        self.assertEqual(a._grounded_source_ids, [10, 20, 30])

    def test_run_goal_seeds_source_fact_ids(self):
        from browser_agent.orchestrator import Agent

        class Tiny(Agent):
            def __init__(self):
                self._source_fact_id = None
                self._grounded_source_ids = None
                self._source_provider = None
                self.last_distill_id = None
                self.last_steps = 0
                self.dry_run = None
                self._autonomous_run = False
                self._recorder = MagicMock()
                self._recorder.start_run = MagicMock()
                self._recorder.record_from_packet = MagicMock()
                self._recorder.finish_run = MagicMock()

            def _run_goal_inner(self, goal, **kw):
                return "ok", "success"

            def cost(self):
                return 0.0

        a = Tiny()
        with patch("browser_agent.orchestrator.cfg") as cfg:
            cfg.DRY_RUN_LEVELS = ("plan", "draft", "full")
            cfg.AUTONOMOUS_LEVELS = ("autonomous",)
            a.run_goal("draft email", source_fact_ids=[5, 6, 7])
        self.assertEqual(a._source_fact_id, 5)
        self.assertEqual(a._grounded_source_ids, [5, 6, 7])


class HydrateSourceIdsTests(unittest.TestCase):
    def test_hydrate_exposes_source_fact_ids(self):
        from app.services import meeting_enhance as me
        with tempfile.TemporaryDirectory() as td:
            store = _store(td)
            try:
                rid, fid_c, _ = _seed_meeting(store, with_decision=False)
                note = me.hydrate_meeting_note(
                    store, store.get_reflection(rid))
                commit = next(i for i in note["items"] if i["kind"] == "commitment")
                self.assertEqual(commit["source_fact_ids"], [fid_c])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
