"""Heuristic phone goal parsing + clean-message short-circuit."""
from __future__ import annotations

import unittest
from unittest import mock

from browser_agent.phone_parse import (
    is_anaphoric_body,
    message_looks_clean,
    refers_to_prior_reply,
    try_parse_phone_goal,
)


class TryParsePhoneGoalTests(unittest.TestCase):
    def test_text_name_body(self) -> None:
        p = try_parse_phone_goal("text Abby I'm running late")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["action"], "send_sms")
        self.assertEqual(p["recipient"], "Abby")
        self.assertEqual(p["message"], "I'm running late")
        self.assertEqual(p["_parsed"], "heuristic")

    def test_text_full_name_body(self) -> None:
        p = try_parse_phone_goal("text Abby Nengel I love you")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["recipient"], "Abby Nengel")
        self.assertEqual(p["message"], "I love you")

    def test_text_with_that(self) -> None:
        p = try_parse_phone_goal("text Mom that I'll call after the meeting")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["recipient"], "Mom")
        self.assertIn("call after", p["message"])

    def test_text_recipient_only(self) -> None:
        p = try_parse_phone_goal("text Abby")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["recipient"], "Abby")
        self.assertEqual(p["message"], "")

    def test_relative_that_is_not_a_body_separator(self) -> None:
        # Live failure: recipient came out as "Hugh Salva the message of your
        # description" and body as "you gave me above". The "that" here is a
        # relative pronoun; the whole tail refers to the prior reply.
        p = try_parse_phone_goal(
            "Text Hugh Salva the message of your description that you gave me above")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["action"], "send_sms")
        self.assertEqual(p["recipient"], "Hugh Salva")
        self.assertEqual(p["message"], "")      # session fill supplies the body

    def test_prior_reply_phrases_detected(self) -> None:
        self.assertTrue(is_anaphoric_body(
            "the message of your description that you gave me above"))
        self.assertTrue(refers_to_prior_reply(
            "the description that you gave me earlier"))
        self.assertTrue(refers_to_prior_reply(
            "send a text message to Hugh Salva of what you just told me above"))
        self.assertFalse(is_anaphoric_body("I'll call after the meeting"))

    def test_open_phone_link(self) -> None:
        p = try_parse_phone_goal("open Phone Link")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["action"], "open")

    def test_read_messages(self) -> None:
        p = try_parse_phone_goal("read my texts from Justin")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["action"], "read_messages")
        self.assertEqual(p["recipient"], "Justin")

    def test_reply(self) -> None:
        p = try_parse_phone_goal("reply to Abby sounds good")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["action"], "reply")
        self.assertEqual(p["recipient"], "Abby")
        self.assertEqual(p["message"], "sounds good")

    def test_ambiguous_falls_through(self) -> None:
        self.assertIsNone(try_parse_phone_goal("tell everyone on the team I'm late"))
        self.assertIsNone(try_parse_phone_goal("open Chrome"))

    def test_anaphoric_content_falls_through(self) -> None:
        self.assertIsNone(try_parse_phone_goal(
            "Can you text that to Justin Adorante actually?"))
        self.assertIsNone(try_parse_phone_goal("text that to Mom"))
        self.assertIsNone(try_parse_phone_goal("text this to Abby"))
        self.assertIsNone(try_parse_phone_goal("text that"))

    def test_prior_reply_body_clears_message_keeps_recipient(self) -> None:
        # Never send the pointer phrase literally — empty body for session fill.
        p = try_parse_phone_goal("Text Hugh the message you just told me")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["action"], "send_sms")
        self.assertEqual(p["recipient"], "Hugh")
        self.assertEqual(p["message"], "")

        p2 = try_parse_phone_goal(
            "text Hugh Salva that message you just told me")
        self.assertIsNotNone(p2)
        assert p2 is not None
        self.assertEqual(p2["recipient"], "Hugh Salva")
        self.assertEqual(p2["message"], "")

    def test_literal_that_clause_still_parses(self) -> None:
        # "that I'll call…" is content, not a session anaphor.
        p = try_parse_phone_goal("text Mom that I'll call after the meeting")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["recipient"], "Mom")
        self.assertIn("call after", p["message"])

    def test_compose_directive_falls_through(self) -> None:
        self.assertIsNone(try_parse_phone_goal(
            "Can you text Hugh Salva with an introduction of yourself "
            "and a summary of what you can do?"))
        self.assertIsNone(try_parse_phone_goal(
            "text Abby about the party on Saturday"))

    def test_reversed_body_to_name_falls_through(self) -> None:
        self.assertIsNone(try_parse_phone_goal("text happy birthday to Mom"))

    def test_body_containing_to_place_still_parses(self) -> None:
        p = try_parse_phone_goal("text Mom I'm heading to Grand Central")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p["recipient"], "Mom")
        self.assertEqual(p["message"], "I'm heading to Grand Central")


class AnaphorHelpersTests(unittest.TestCase):
    def test_is_anaphoric_body(self) -> None:
        self.assertTrue(is_anaphoric_body("that"))
        self.assertTrue(is_anaphoric_body("the message you just told me"))
        self.assertTrue(is_anaphoric_body("what you just said"))
        self.assertFalse(is_anaphoric_body("I'm running late"))
        self.assertFalse(is_anaphoric_body("I'll call after the meeting"))

    def test_refers_to_prior_reply(self) -> None:
        self.assertTrue(refers_to_prior_reply(
            "Text Hugh the message you just told me"))
        self.assertTrue(refers_to_prior_reply(
            "Can you recall the description of how you work?"))
        self.assertFalse(refers_to_prior_reply(
            "text Mom that I'll call after the meeting"))


class MessageLooksCleanTests(unittest.TestCase):
    def test_typed_clean(self) -> None:
        self.assertTrue(message_looks_clean("I'm running late"))
        self.assertTrue(message_looks_clean("I love you"))

    def test_stt_artifacts_need_llm(self) -> None:
        self.assertFalse(message_looks_clean("meet at for thirty"))
        self.assertFalse(message_looks_clean("um yeah see you at too"))


class CorrectPhonePlanFastPathTests(unittest.TestCase):
    def test_clean_message_skips_llm(self) -> None:
        from browser_agent.llm import LLM

        llm = LLM.__new__(LLM)
        llm._json_call = mock.Mock(side_effect=AssertionError("should not call LLM"))
        out = LLM.clean_message(llm, "See you at 5")
        self.assertFalse(out["changed"])
        self.assertEqual(out["text"], "See you at 5")
        llm._json_call.assert_not_called()


class OrchestratorHeuristicParseTests(unittest.TestCase):
    def test_run_phone_skips_parse_llm(self) -> None:
        from browser_agent.orchestrator import Agent

        agent = Agent.__new__(Agent)
        agent._log = mock.Mock()
        agent._autonomous_run = True
        agent.transcript = []
        agent.last_steps = 0
        agent.last_replans = 0
        agent.llm = mock.Mock()
        agent.llm.parse_phone_goal = mock.Mock(
            side_effect=AssertionError("parse_phone_goal should not run"))
        agent.llm.resolve_recipient = mock.Mock(
            return_value={"name": "Abby", "changed": False})
        agent.llm.clean_message = mock.Mock(
            side_effect=AssertionError("clean_message should not run"))
        agent._correct_phone_plan = mock.Mock()
        agent._record_phone_corrections = mock.Mock()

        with mock.patch("app.services.phone_link.execute_goal",
                        return_value=("ok", "success")) as ex:
            result, status = Agent._run_phone_link_goal(
                agent, "text Abby I'm running late", "")

        self.assertEqual(status, "success")
        agent.llm.parse_phone_goal.assert_not_called()
        agent._correct_phone_plan.assert_called_once()
        parsed = agent._correct_phone_plan.call_args[0][0]
        self.assertEqual(parsed.get("_parsed"), "heuristic")
        self.assertEqual(parsed["recipient"], "Abby")
        ex.assert_called_once()


if __name__ == "__main__":
    unittest.main()
