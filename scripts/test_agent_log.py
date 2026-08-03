"""Unit test for the Phase 5 agent-run substrate (Sprint 1 + 2).

    python scripts/test_agent_log.py

Two halves, both offline (no browser, no API, temp DB):

  1. Recorder <-> Store round-trip: a run opens, is annotated once "routed",
     compiles an action packet, takes an `edit` verdict, records steps, and
     closes. We assert the whole trajectory persisted — and specifically that
     the user's edit instruction survives (it used to evaporate when the run
     ended), and that agent_run_stats() derives the edit rate.

  2. Agent._approval_decision writes through the recorder: bound to a spy (like
     test_approval_packet.py binds to a Fake), we prove the packet is recorded
     before asking and the decision — with the edit text — after.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_agent import config as cfg
from browser_agent.orchestrator import Agent, _risk_from_route

from app.storage import Store
from app.services.agent_log import Recorder


def test_recorder_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(db_path=Path(tmp) / "quill_test.db",
                      audio_dir=Path(tmp) / "audio")
        try:
            _run_roundtrip(store)
        finally:
            store.close()  # release the sqlite file so the temp dir can be removed


def _run_roundtrip(store):
        rec = Recorder(store=store)

        rid = rec.start_run("Draft the follow-up to Marc", surface="browser",
                            dry_run="approval")
        assert rid and rec.current_run_id == rid, "start_run should set current run"
        rec.annotate_run(intent="send_email", risk_level="high")

        pid = rec.record_packet(
            summary="Send email to Marc",
            fields={"action": "Send email", "to": "Marc",
                    "subject": "Pricing follow-up"},
            execution_surface="browser", risk_level="high")
        assert pid, "record_packet should return a packet id"

        rec.record_decision(pid, "edit",
                            user_edit="change the subject to Q3 pricing")
        rec.record_steps([
            {"step_index": 1, "action_type": "navigate",
             "input": {"url": "mail.google.com"}, "output": "ok",
             "status": "verified"},
            {"step_index": 2, "action_type": "request_approval",
             "input": {"summary": "Send email to Marc"},
             "output": "edit requested", "status": "verified"},
        ])
        rec.finish_run(status="success", cost=0.1234, steps=2)
        assert rec.current_run_id is None, "finish_run should clear the current run"

        run = store.agent_run(rid)
        assert run is not None
        assert run["status"] == "success", run["status"]
        assert run["intent"] == "send_email" and run["risk_level"] == "high"
        assert run["latency"] is not None and run["latency"] >= 0
        assert run["cost"] == 0.1234 and run["steps"] == 2   # steps = count column

        assert len(run["packets"]) == 1, run["packets"]
        assert run["packets"][0]["decision"] == "edit"
        assert run["packets"][0]["execution_surface"] == "browser"

        assert len(run["step_log"]) == 2, run["step_log"]
        assert [s["action_type"] for s in run["step_log"]] == ["navigate", "request_approval"]

        fb = run["feedback"]
        assert len(fb) == 1 and fb[0]["feedback_type"] == "edited", fb
        # The signal that used to evaporate once the run ended:
        assert fb[0]["user_edit"] == "change the subject to Q3 pricing", fb
        assert fb[0]["packet_id"] == pid

        stats = store.agent_run_stats()
        assert stats["runs"] == 1, stats
        assert stats["success_rate"] == 1.0, stats
        assert stats["edit_rate"] == 1.0 and stats["approval_rate"] == 0.0, stats
        print("[test] recorder <-> store round-trip OK (edit signal persisted)")


def test_recorder_best_effort():
    """A broken store must never raise into the run — errors are swallowed."""
    class Broken:
        def start_agent_run(self, *a, **k):
            raise RuntimeError("db is on fire")

    rec = Recorder(store=Broken())
    assert rec.start_run("x") is None and rec.current_run_id is None
    # These must be no-op-safe with no active run, too.
    rec.annotate_run(intent="y")
    rec.record_decision(None, "approve")
    rec.finish_run(status="error")
    print("[test] recorder best-effort (never raises) OK")


def test_risk_from_route():
    assert _risk_from_route({"intent": "delete_account"}) == "high"
    assert _risk_from_route({"intent": "read_page", "requires_user_approval": True}) == "high"
    assert _risk_from_route({"intent": "send_email"}) == "medium"
    assert _risk_from_route({"intent": "lookup", "requires_browser": False}) == "low"
    assert _risk_from_route({"intent": "search"}) == "low"
    assert _risk_from_route(None) == "low"
    print("[test] _risk_from_route tiers OK")


def test_approval_decision_records():
    """_approval_decision records the packet then the verdict (with edit text)."""
    cfg.REQUIRE_APPROVAL = True

    class Spy:
        def __init__(self):
            self.calls = []
            self.current_run_id = 1

        def record_packet(self, **k):
            self.calls.append(("packet", k))
            return 42

        def record_decision(self, packet_id, decision, user_edit=None):
            self.calls.append(("decision", packet_id, decision, user_edit))

    class Fake:
        _log = staticmethod(lambda s: None)

    fake = Fake()
    fake._recorder = Spy()
    fake.last_route = {"intent": "send_email", "requires_user_approval": True}
    fake._ask_fn = lambda prompt: "change the subject to Q3 pricing"

    decision, feedback = Agent._approval_decision(
        fake, "Send email to Marc", {"action": "Send email", "to": "Marc"})
    assert decision == "edit" and "Q3 pricing" in feedback, (decision, feedback)

    calls = fake._recorder.calls
    assert [c[0] for c in calls] == ["packet", "decision"], calls
    # packet carried the derived risk tier (route required approval -> high)
    assert calls[0][1].get("risk_level") == "high", calls[0]
    # decision recorded the packet id and the exact edit instruction
    assert calls[1] == ("decision", 42, "edit", "change the subject to Q3 pricing"), calls[1]
    print("[test] _approval_decision records packet + edit verdict OK")

    # And with approval off, no recording happens (auto-approve short-circuit).
    cfg.REQUIRE_APPROVAL = False
    fake._recorder.calls.clear()
    decision, _ = Agent._approval_decision(fake, "Send email to Marc", {})
    assert decision == "approve" and fake._recorder.calls == []
    cfg.REQUIRE_APPROVAL = True
    print("[test] approval-off short-circuit records nothing OK")


if __name__ == "__main__":
    test_recorder_roundtrip()
    test_recorder_best_effort()
    test_risk_from_route()
    test_approval_decision_records()
    print("\n[test] ALL PASS")
