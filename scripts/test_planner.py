"""Unit test for the Personal Agent Layer vertical slice (Phase 5).

    python scripts/test_planner.py

Offline — the LLM is mocked at the _llm() seam, so no API key is needed. Covers:
  * WritingCompiler drafts into the packet, grounded in the selected context;
  * risk/approval on a 'send' (high -> gated);
  * graceful degradation to passthrough when no LLM is available;
  * ActionStep.to_goal_text carries the draft to the executor;
  * the agent_bridge seam compiles a Plan and enqueues steps + packets.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.services import agent_planner as ap
from app.services.agent_log import ActionPacket


class FakeLLM:
    """Stands in for browser_agent.llm.LLM — records prompts, returns canned JSON."""
    def __init__(self):
        self.seen_user = ""

    def _json_call(self, model, system, user, schema, effort=None):
        self.seen_user = user
        return {"to": "Marc", "subject": "Pricing follow-up",
                "body": "Hi Marc, following up on the $49/mo pricing. — J",
                "why": "You promised this after the meeting.",
                "summary": "Send pricing follow-up to Marc"}

    def route(self, goal, context=""):
        return {"intent": "send_email"}


def _ctx():
    return ap.SelectedContext(
        memory_block="- Marc discussed $49/mo pricing\n- You promised a follow-up",
        source_fact_ids=[11, 12],
        open_commitments=[{"text": "Send Marc the revised pricing deck"}])


def _ctx_with_person():
    return ap.SelectedContext(
        memory_block="- Marc discussed $49/mo pricing",
        source_fact_ids=[11],
        open_commitments=[{"text": "Send Marc the revised pricing deck"}],
        people=[{"found": True, "person": {"name": "Marc"},
                 "items": [{"predicate": "owed", "text": "pricing deck",
                            "status": "open"}],
                 "affiliations": [{"name": "Acme", "kind": "org"}],
                 "discussed_with": [{"name": "Sarah", "weight": 2}]}])


def test_writing_compiler_drafts():
    ap._LLM = FakeLLM()                       # inject mock at the shared seam
    try:
        packet = ap.WritingCompiler().compile("send a follow-up to Marc", _ctx())
    finally:
        ap._LLM = None
    assert packet.fields.get("body"), packet.fields
    assert packet.fields["subject"] == "Pricing follow-up"
    assert packet.risk_level == "high" and packet.approval_required is True
    assert packet.suggested_agent == "writing_agent"
    assert packet.source_fact_ids == [11, 12]           # provenance carried
    print("[test] WritingCompiler drafts into the packet (send -> high/gated) OK")


def test_draft_prompt_is_grounded():
    fake = FakeLLM()
    ap._LLM = fake
    try:
        ap.WritingCompiler().compile("follow up with Marc", _ctx())
    finally:
        ap._LLM = None
    # the draft prompt must carry the memory + the open commitment (grounding)
    assert "revised pricing deck" in fake.seen_user, fake.seen_user
    assert "$49/mo pricing" in fake.seen_user
    print("[test] draft prompt is grounded in memory + commitments OK")


def test_degrades_without_llm():
    ap._LLM = False                           # no LLM available
    try:
        pl = ap.PersonalAgentLayer()
        pl.select_context = lambda goal, person=None: ap.SelectedContext()  # no embedder
        plan = pl.compile("send an email to Marc about pricing")
    finally:
        ap._LLM = None
    assert plan.is_single
    step = plan.steps[0]
    # WritingCompiler._draft raised -> Planner fell back to passthrough, but the
    # risk classification still holds: a 'send' is gated.
    assert step.packet.approval_required is True and step.packet.risk_level == "high"
    assert step.packet.fields == {}           # passthrough didn't draft
    print("[test] degrades to passthrough without an LLM (still gated) OK")


def test_to_goal_text_carries_draft():
    packet = ActionPacket(
        goal="send follow-up",
        fields={"to": "Marc", "subject": "Pricing", "body": "Hi Marc, ..."},
        context=["Marc discussed $49/mo", "You promised a follow-up"])
    step = ap.ActionStep(goal="send follow-up to Marc", packet=packet,
                         agent_type="writing_agent")
    text = step.to_goal_text()
    assert "RELEVANT CONTEXT" in text and "$49/mo" in text
    assert "Subject: Pricing" in text and "Body:\nHi Marc" in text
    assert "Current task: send follow-up to Marc" in text
    print("[test] to_goal_text carries context + draft to the executor OK")


def test_meeting_plain_briefing_no_llm():
    ap._LLM = False
    try:
        packet = ap.MeetingCompiler().compile(
            "prep for my meeting with Marc", _ctx_with_person())
    finally:
        ap._LLM = None
    assert packet.execution_surface == "none"      # pure cognition — no hands
    assert packet.approval_required is False and packet.risk_level == "low"
    assert "Marc" in packet.fields["briefing"]
    assert "revised pricing deck" in packet.fields["briefing"]   # open commitment surfaced
    print("[test] MeetingCompiler plain briefing (no LLM, no browser) OK")


def test_meeting_llm_briefing():
    class FakeMeetLLM:
        def _json_call(self, model, system, user, schema, effort=None):
            assert "revised pricing deck" in user           # grounded in memory
            return {"summary": "Marc / pricing", "briefing": "Marc wants pricing.",
                    "ask": ["Confirm $49/mo pricing"]}

    ap._LLM = FakeMeetLLM()
    try:
        packet = ap.MeetingCompiler().compile("brief me on Marc", _ctx_with_person())
    finally:
        ap._LLM = None
    assert packet.fields["briefing"] == "Marc wants pricing."
    text = ap.render_deliverable(packet)
    assert "Confirm $49/mo pricing" in text             # ask items rendered for chat
    print("[test] MeetingCompiler LLM briefing + render_deliverable OK")


class _FakeAgent:
    """Stands in for the browser Agent: records run_goal calls, no browser."""
    def __init__(self):
        from browser_agent.orchestrator import _NullRecorder
        self.calls: list[dict] = []
        self._recorder = _NullRecorder()

    def run_goal(self, text, dry_run=None, surface=None, packet=None):
        self.calls.append({"text": text, "surface": surface, "packet": packet})
        return ("ran", "success")


def test_worker_dispatches_plan():
    """The worker compiles on its own thread, delivers a briefing with no browser,
    and routes an actionable goal through run_goal."""
    from app.services.agent_bridge import AgentWorker

    ap._LLM = False   # meeting -> plain briefing; writing -> passthrough (browser)
    ap.planner.select_context = lambda goal, person=None: _ctx_with_person()
    try:
        # a meeting goal is pure cognition: delivered directly, no run_goal
        w = AgentWorker()
        w.agent = _FakeAgent()
        w._run_planned({"text": "prep me for my meeting with Marc", "surface": None})
        assert w.agent.calls == [], "a briefing must not open the browser"
        results = [e for e in w.events if e["kind"] == "result"]
        assert results and "Marc" in results[-1]["text"]

        # an actionable goal goes through run_goal with a forced surface
        w2 = AgentWorker()
        w2.agent = _FakeAgent()
        w2._run_planned({"text": "send a follow-up to Marc", "surface": None})
        assert len(w2.agent.calls) == 1 and w2.agent.calls[0]["surface"] == "browser"
    finally:
        ap._LLM = None
        ap.planner.select_context = ap.PersonalAgentLayer.select_context.__get__(ap.planner)
    print("[test] worker dispatches plan: briefing direct, writing -> browser OK")


if __name__ == "__main__":
    test_writing_compiler_drafts()
    test_draft_prompt_is_grounded()
    test_degrades_without_llm()
    test_to_goal_text_carries_draft()
    test_meeting_plain_briefing_no_llm()
    test_meeting_llm_briefing()
    test_worker_dispatches_plan()
    print("\n[test] ALL PASS")
