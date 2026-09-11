"""Peer round-trip telemetry (Phase 0.1) — the trail that makes peer evaluable.

Pins three things:
  * every leg of a round trip lands as its own event, joined by ask_id;
  * the trail records METADATA ONLY — question/answer text never reaches it,
    including when a future call site passes it by accident;
  * rollup() computes the three pilot metrics (completion rate, median
    time-to-answer, repeat use) and the gate breakdown that says which of
    consent / retrieval / composition is actually failing.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

from app.services import peer_channel as pch  # noqa: E402
from app.services import peer_telemetry as tel  # noqa: E402
from tests.test_peer_channel import PeerChannelBase  # noqa: E402


class TelemetryBase(PeerChannelBase):
    def setUp(self) -> None:
        super().setUp()
        os.environ["QUILL_PEER_TELEMETRY_PATH"] = str(
            Path(self._tmp) / "peer_telemetry.jsonl")
        os.environ["QUILL_PEER_TELEMETRY"] = "1"

    def tearDown(self) -> None:
        os.environ.pop("QUILL_PEER_TELEMETRY_PATH", None)
        os.environ.pop("QUILL_PEER_TELEMETRY", None)
        super().tearDown()

    def _events(self, name: str) -> list[dict]:
        return [r for r in tel.read_rows() if r.get("event") == name]

    def _authed_peer(self) -> dict:
        """The peer dict handle_ask actually receives — authenticate() merges
        the peer_id in, and decide_ask needs it to find the asker again."""
        reg = self._registry()
        peer_id = next(iter(reg))
        return {"peer_id": peer_id, **reg[peer_id]}


class RecordTests(TelemetryBase):
    def test_metadata_only_content_keys_are_dropped(self) -> None:
        """A call site that passes text must not be able to leak it."""
        tel.record("gate", ask_id="a1", peer_id="p1",
                   question="who is covering our compute costs?",
                   answer="Andy Karos at Boost Run",
                   text="t", body="b", prose="p", claims=[{"text": "x"}],
                   action="offer", topic="work", question_chars=41)
        rows = tel.read_rows()
        self.assertEqual(len(rows), 1)
        blob = str(rows[0])
        for leaked in ("Andy", "Boost Run", "compute costs"):
            self.assertNotIn(leaked, blob)
        # The metadata we DO want survived.
        self.assertEqual(rows[0]["action"], "offer")
        self.assertEqual(rows[0]["question_chars"], 41)

    def test_disabled_writes_nothing(self) -> None:
        os.environ["QUILL_PEER_TELEMETRY"] = "0"
        self.assertIsNone(tel.record("gate", ask_id="a1"))
        self.assertEqual(tel.read_rows(), [])

    def test_record_never_raises_on_bad_path(self) -> None:
        os.environ["QUILL_PEER_TELEMETRY_PATH"] = "/proc/nope/cannot.jsonl"
        self.assertIsNone(tel.record("gate", ask_id="a1"))


class RoundTripTests(TelemetryBase):
    """Drive the real peer paths and assert each leg was recorded."""

    def test_outbound_ask_records_ask_sent_with_status(self) -> None:
        self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(pch, "_post_peer",
                               return_value={"ok": True, "status": "pending"}):
            res = pch.ask(peer_id, "where are we on the compute deal?")
        self.assertEqual(res["status"], "pending")
        sent = self._events("ask_sent")
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["status"], "pending")
        self.assertEqual(sent[0]["peer_id"], peer_id)
        self.assertFalse(sent[0]["followup"])
        self.assertNotIn("compute deal", str(sent[0]))

    def test_unreachable_peer_records_queued_not_a_silent_drop(self) -> None:
        self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(pch, "_post_peer",
                               side_effect=OSError("connection refused")):
            res = pch.ask(peer_id, "any update?")
        self.assertEqual(res["status"], "queued")
        self.assertEqual(self._events("ask_sent")[0]["status"], "queued")

    def test_gate_records_the_action_the_policy_took(self) -> None:
        claim = self._claimed_peer()
        pch.handle_ask(self._authed_peer(),
                       {"ask_id": "x1", "question": "free Friday?"})
        gate = self._events("gate")
        self.assertEqual(len(gate), 1)
        # Default posture is all-offer: every ask waits for the human.
        self.assertEqual(gate[0]["action"], "offer")
        self.assertEqual(gate[0]["ask_id"], "x1")
        self.assertNotIn("Friday", str(gate[0]))
        self.assertTrue(claim["ok"])

    def test_verdict_records_how_long_the_asker_waited(self) -> None:
        reg_peer = self._claimed_peer()
        pch.handle_ask(self._authed_peer(),
                       {"ask_id": "x2", "question": "status?"})
        pending = pch.pending_asks()
        self.assertEqual(len(pending), 1)
        with mock.patch.object(pch, "_deliver", return_value=True), \
                mock.patch.object(pch, "compose_answer",
                                  return_value={"text": "All good.",
                                                "redacted": []}):
            pch.decide_ask(pending[0]["id"], True)
        verdicts = self._events("verdict")
        self.assertEqual(len(verdicts), 1)
        self.assertEqual(verdicts[0]["verdict"], "approved")
        self.assertGreaterEqual(verdicts[0]["waited_s"], 0.0)
        self.assertTrue(reg_peer["ok"])

    def test_answer_records_length_and_usability(self) -> None:
        self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "We have compute through November."}):
            pch.ask(peer_id, "what's the latest?")
        answers = self._events("answer")
        self.assertEqual(len(answers), 1)
        self.assertEqual(answers[0]["status"], "answered")
        self.assertTrue(answers[0]["usable"])
        self.assertEqual(answers[0]["answer_chars"], 33)
        self.assertNotIn("November", str(answers[0]))

    def test_unusable_answer_is_recorded_as_unusable(self) -> None:
        """A refusal/identity dump completes the trip but is not an answer —
        the completion metric must not count it."""
        self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "You are Sparrow, the user's..."}):
            pch.ask(peer_id, "what's the latest?")
        self.assertFalse(self._events("answer")[0]["usable"])
        self.assertEqual(tel.rollup()["answered_usable"], 0)

    def test_second_ask_to_same_peer_is_flagged_a_followup(self) -> None:
        self._claimed_peer()
        peer_id = next(iter(self._registry()))
        with mock.patch.object(
                pch, "_post_peer",
                return_value={"ok": True, "status": "answered",
                              "answer": "Through November, confirmed."}):
            pch.ask(peer_id, "first question")
            pch.ask(peer_id, "second question")
        sent = self._events("ask_sent")
        self.assertEqual([s["followup"] for s in sent], [False, True])


class RollupTests(TelemetryBase):
    def _trip(self, ask_id: str, *, peer: str, status: str = "answered",
              usable: bool = True, t0: float = 100.0, t1: float = 104.0,
              gate: str = "offer") -> None:
        rows = [
            ("ask_sent", {"status": "pending", "time": t0}),
            ("gate", {"action": gate, "time": t0}),
            ("answer", {"status": status, "usable": usable, "time": t1}),
        ]
        for ev, extra in rows:
            r = tel.record(ev, ask_id=ask_id, peer_id=peer, **extra)
            # record() stamps its own time; rewrite for deterministic latency.
            self.assertIsNotNone(r)
        self._rewrite_times(ask_id, t0, t1)

    def _rewrite_times(self, ask_id: str, t0: float, t1: float) -> None:
        p = Path(os.environ["QUILL_PEER_TELEMETRY_PATH"])
        import json
        out = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            row = json.loads(ln)
            if row.get("ask_id") == ask_id:
                row["time"] = t1 if row.get("event") == "answer" else t0
            out.append(json.dumps(row))
        p.write_text("\n".join(out) + "\n", encoding="utf-8")

    def test_completion_rate_counts_only_usable_answers(self) -> None:
        self._trip("a1", peer="p1")
        self._trip("a2", peer="p1", usable=False)
        self._trip("a3", peer="p2", status="declined", usable=False)
        roll = tel.rollup()
        self.assertEqual(roll["asks_sent"], 3)
        self.assertEqual(roll["answered_usable"], 1)
        self.assertAlmostEqual(roll["completion_rate"], 1 / 3)

    def test_median_time_to_answer(self) -> None:
        self._trip("a1", peer="p1", t0=100.0, t1=102.0)
        self._trip("a2", peer="p2", t0=200.0, t1=208.0)
        self.assertAlmostEqual(tel.rollup()["median_answer_s"], 5.0)

    def test_repeat_use_is_pairs_that_asked_more_than_once(self) -> None:
        self._trip("a1", peer="p1")
        self._trip("a2", peer="p1")
        self._trip("a3", peer="p2")
        roll = tel.rollup()
        self.assertEqual(roll["pairs_asked"], 2)
        self.assertEqual(roll["pairs_repeat"], 1)
        self.assertAlmostEqual(roll["repeat_use"], 0.5)

    def test_gate_breakdown_separates_consent_from_retrieval(self) -> None:
        self._trip("a1", peer="p1", gate="auto")
        self._trip("a2", peer="p1", gate="offer")
        self._trip("a3", peer="p2", gate="deny")
        self.assertEqual(tel.rollup()["gate_actions"],
                         {"auto": 1, "offer": 1, "deny": 1})

    def test_empty_trail_reports_none_not_zero(self) -> None:
        """No traffic is not 0% completion — the pilot must tell them apart."""
        roll = tel.rollup()
        self.assertIsNone(roll["completion_rate"])
        self.assertIsNone(roll["median_answer_s"])
        self.assertEqual(roll["asks_sent"], 0)


if __name__ == "__main__":
    unittest.main()
