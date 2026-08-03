"""Session conversation awareness — transcript follow-ups and SMS body fill."""
from __future__ import annotations

import unittest
from unittest import mock


class LastAssistantAndResolveTests(unittest.TestCase):
    def _agent(self):
        from browser_agent.orchestrator import Agent
        agent = Agent.__new__(Agent)
        agent._log = mock.Mock()
        agent.transcript = []
        return agent

    def test_last_assistant_skips_cancelled(self) -> None:
        from browser_agent.orchestrator import Agent
        agent = self._agent()
        agent.transcript = [
            {"goal": "summarize yourself", "result": "I am vinceo.ai; I help with…"},
            {"goal": "text Hugh that", "result": "SMS send cancelled."},
        ]
        self.assertIn("vinceo.ai", Agent._last_assistant_text(agent))

    def test_resolve_fills_anaphoric_empty_message(self) -> None:
        from browser_agent.orchestrator import Agent
        agent = self._agent()
        summary = (
            "I assist with tasks, memory questions, and drafting. "
            "I escalate to Claude when unsure."
        )
        agent.transcript = [
            {"goal": "detailed summary of your functionality", "result": summary},
        ]
        parsed = {"action": "send_sms", "recipient": "Hugh Salva", "message": ""}
        Agent._resolve_session_message(
            agent, parsed, "Text Hugh Salva the message you just told me")
        self.assertEqual(parsed["message"], summary)
        self.assertTrue(parsed.get("_session_body"))

    def test_resolve_replaces_literal_anaphor_phrase(self) -> None:
        from browser_agent.orchestrator import Agent
        agent = self._agent()
        agent.transcript = [
            {"goal": "who are you", "result": "I'm vinceo.ai."},
        ]
        parsed = {
            "action": "send_sms",
            "recipient": "Hugh",
            "message": "the message you just told me",
        }
        Agent._resolve_session_message(
            agent, parsed, "Text Hugh the message you just told me")
        self.assertEqual(parsed["message"], "I'm vinceo.ai.")

    def test_resolve_leaves_concrete_body_alone(self) -> None:
        from browser_agent.orchestrator import Agent
        agent = self._agent()
        agent.transcript = [
            {"goal": "hi", "result": "Hello!"},
        ]
        parsed = {
            "action": "send_sms",
            "recipient": "Mom",
            "message": "I'll call after the meeting",
        }
        Agent._resolve_session_message(
            agent, parsed, "text Mom that I'll call after the meeting")
        self.assertEqual(parsed["message"], "I'll call after the meeting")
        self.assertNotIn("_session_body", parsed)


class CrossLaneReplyTests(unittest.TestCase):
    """The live "text Hugh what you just told me" failure: phone goals run on
    the FAST agent, whose own transcript never saw the browser agent's answer
    — and on retry its only prior "reply" was its own clarifying question."""

    def _agent(self, transcript=None, shared=None):
        from browser_agent.orchestrator import Agent
        agent = Agent.__new__(Agent)
        agent._log = mock.Mock()
        agent.transcript = transcript or []
        agent._session_replies = (lambda: shared) if shared is not None else None
        return agent

    def test_empty_transcript_falls_back_to_shared_pool(self) -> None:
        from browser_agent.orchestrator import Agent
        shared = ["Open tasks: send the number.", "I'm vinceo.ai; I help with…"]
        agent = self._agent(transcript=[], shared=shared)
        self.assertIn("vinceo.ai", Agent._last_assistant_text(agent))

    def test_own_clarifying_question_is_never_the_reply(self) -> None:
        # Fast lane's transcript holds only its own ask + a cancel; the real
        # answer lives in the shared pool.
        from browser_agent.orchestrator import Agent
        agent = self._agent(
            transcript=[
                {"goal": "text Hugh …",
                 "result": "What would you like me to text Hugh Salva?"},
                {"goal": "text Hugh …", "result": "SMS send cancelled."},
            ],
            shared=["I'm vinceo.ai, a memory-aware assistant…",
                    "What would you like me to text Hugh Salva?",
                    "SMS send cancelled."])
        self.assertIn("memory-aware", Agent._last_assistant_text(agent))

    def test_approval_prompt_is_never_the_reply(self) -> None:
        from browser_agent.orchestrator import Agent
        agent = self._agent(transcript=[
            {"goal": "x", "result": "Here is the summary you asked for."},
            {"goal": "y", "result": "Send this text message via Phone Link?\n\n"
                                    "Reply 'approve' to proceed, or anything "
                                    "else to cancel."},
        ])
        self.assertEqual(Agent._last_assistant_text(agent),
                         "Here is the summary you asked for.")


class TranscriptContextTests(unittest.TestCase):
    def test_followup_puts_session_before_memory(self) -> None:
        from browser_agent.orchestrator import Agent
        agent = Agent.__new__(Agent)
        agent._log = mock.Mock()
        agent.transcript = [
            {"goal": "how do you work?", "result": "Functionality overview here."},
        ]
        agent._memory_provider = lambda _g: "RELEVANT MEMORIES:\n- Venture Pulse CRM"
        agent._memory_context = lambda g: (
            "RELEVANT MEMORIES FROM vinceo.ai:\n- Venture Pulse CRM\n\n")
        ctx = Agent._build_ctx(
            agent, "Can you recall the description of how you work?")
        self.assertLess(ctx.find("SESSION CONVERSATION"),
                        ctx.find("Venture Pulse"))
        self.assertIn("LAST_ASSISTANT_REPLY", ctx)
        self.assertIn("Functionality overview here.", ctx)

    def test_phone_run_resolves_before_execute(self) -> None:
        from browser_agent.orchestrator import Agent

        agent = Agent.__new__(Agent)
        agent._log = mock.Mock()
        agent._autonomous_run = True
        agent.transcript = [
            {"goal": "summarize functionality",
             "result": "High-level: memory, chat, phone, desktop."},
        ]
        agent.last_steps = 0
        agent.last_replans = 0
        agent.llm = mock.Mock()
        agent.llm.parse_phone_goal = mock.Mock(
            side_effect=AssertionError("should use heuristic"))
        agent._correct_phone_plan = mock.Mock()

        with mock.patch("app.services.phone_link.execute_goal",
                        return_value=("ok", "success")) as ex:
            Agent._run_phone_link_goal(
                agent, "Text Hugh the message you just told me", "")

        parsed = agent._correct_phone_plan.call_args[0][0]
        self.assertEqual(parsed["recipient"], "Hugh")
        self.assertEqual(parsed["message"],
                         "High-level: memory, chat, phone, desktop.")
        self.assertTrue(parsed.get("_session_body"))
        ex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
