"""Plan 4.2 — completion candidates: offer-only speech/screen; verified send completes."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

NOW = 1_700_000_000.0


def _mk(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db")


class CommitmentCompleteUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        from app.services import commitment_complete as cc
        cc.clear_cooldowns_for_tests()

    def test_resolve_and_sent_detectors(self):
        from app.services import commitment_complete as cc

        self.assertTrue(cc.looks_like_resolve("I just sent Marc the deck"))
        self.assertTrue(cc.looks_like_resolve("It's done — I finished the memo"))
        self.assertFalse(cc.looks_like_resolve("I'll send Marc the deck tomorrow"))
        self.assertTrue(cc.looks_like_sent_toast("Message sent"))
        self.assertTrue(cc.looks_like_sent_toast("Moved to Sent Items"))
        self.assertFalse(cc.looks_like_sent_toast("Compose new message"))

    def test_speech_offers_never_auto_completes(self):
        from app.services import commitment_complete as cc

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "Send Marc the deck", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "test"})
                offered = []

                def fake_offer(cand):
                    offered.append(cand)
                    return True

                with mock.patch(
                    "app.services.agent_bridge.worker.propose_commitment_resolve",
                    side_effect=fake_offer,
                ), mock.patch(
                    "app.services.memory.memory.similar_facts",
                    return_value=[],
                ):
                    hits = cc.offer_matches_for_text(
                        "I just sent Marc the deck",
                        source="speech_resolve",
                        store=store, force=True)
                self.assertTrue(hits)
                self.assertTrue(offered)
                self.assertEqual(offered[0]["fact_id"], fid)
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "open")
                self.assertEqual(row["commitment_state"], "active")
            finally:
                store.close()

    def test_offer_accept_completes(self):
        from app.services import commitment_complete as cc

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "Email the contract", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "test"})
                with mock.patch("app.storage.get_store", return_value=store):
                    out = cc.accept_resolve_offer({
                        "fact_id": fid,
                        "text": "Email the contract",
                        "source": "speech_resolve",
                        "quote": "I sent the contract",
                        "event_id": 7,
                    })
                self.assertTrue(out.get("ok"))
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "done")
                self.assertEqual(row["commitment_state"], "completed")
                self.assertIn("speech_resolve",
                              row.get("completion_evidence_json") or "")
            finally:
                store.close()

    def test_verified_send_completes_cited_commitment(self):
        from app.services import commitment_complete as cc

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "Send the invoice", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "test"})
                done = cc.complete_from_verified_send(
                    [fid], packet_id=42, evidence_event_id=9,
                    dry_run=None, store=store)
                self.assertEqual(len(done), 1)
                row = store.get_fact(fid)
                self.assertEqual(row["commitment_state"], "completed")
                self.assertIn("verified_send",
                              row.get("completion_evidence_json") or "")
            finally:
                store.close()

    def test_plan_only_never_completes(self):
        from app.services import commitment_complete as cc

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "Send the invoice", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "test"})
                done = cc.complete_from_verified_send(
                    [fid], packet_id=1, dry_run="plan", store=store)
                self.assertEqual(done, [])
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "open")
                self.assertEqual(row["commitment_state"], "active")
                self.assertEqual(
                    store.list_commitment_transitions(fid)[-1]["to_state"],
                    "active")
            finally:
                store.close()

    def test_verified_send_without_provenance_noop(self):
        from app.services import commitment_complete as cc

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment("Orphan", extracted_at=NOW)
                done = cc.complete_from_verified_send(
                    [], packet_id=1, store=store)
                self.assertEqual(done, [])
                self.assertEqual(store.get_fact(fid)["status"], "open")
            finally:
                store.close()

    def test_screen_sent_is_candidate_not_complete(self):
        from app.services import commitment_complete as cc

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "Send follow-up to Justin", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "test"})
                offered = []
                with mock.patch(
                    "app.services.agent_bridge.worker.propose_commitment_resolve",
                    side_effect=lambda c: offered.append(c) or True,
                ), mock.patch(
                    "app.services.memory.memory.similar_facts",
                    return_value=[],
                ):
                    # Toast text overlaps commitment tokens (Justin / follow-up).
                    self.assertTrue(cc.looks_like_sent_toast(
                        "Gmail — Message sent to Justin (follow-up)"))
                    cc.offer_matches_for_text(
                        "Gmail — Message sent to Justin (follow-up)",
                        source="screen_sent", store=store, force=True)
                self.assertTrue(offered)
                self.assertEqual(offered[0]["fact_id"], fid)
                self.assertEqual(store.get_fact(fid)["status"], "open")
            finally:
                store.close()

    def test_planner_compile_never_completes(self):
        """compile() must not transition commitments (plan-only AC)."""
        from app.services.agent_planner import PersonalAgentLayer

        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                fid = store.add_commitment(
                    "Email Justin the notes", extracted_at=NOW)
                store.transition_commitment(
                    fid, "active", reason="approve",
                    evidence={"source": "test"})
                layer = PersonalAgentLayer(store=store)
                # _llm -> None keeps the test hermetic (no Anthropic call):
                # route_intent + multitask fall back to heuristics and the
                # writing draft degrades to the passthrough compiler, which
                # still cites source_fact_ids.
                with mock.patch("app.services.agent_planner._llm",
                                return_value=None), \
                     mock.patch("app.services.memory.memory.search",
                                return_value=[]), \
                     mock.patch("app.services.working_memory.ensure_fresh"), \
                     mock.patch("app.services.working_memory.snapshot",
                                return_value=[]), \
                     mock.patch("app.services.working_memory.render_lines",
                                return_value=[]):
                    plan = layer.compile("email Justin the notes")
                self.assertTrue(plan.steps)
                row = store.get_fact(fid)
                self.assertEqual(row["status"], "open")
                # Packet should cite the open commitment for later verified send.
                fids = plan.steps[0].packet.source_fact_ids or []
                self.assertIn(fid, fids)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
