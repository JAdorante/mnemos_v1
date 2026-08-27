"""_guess_surface: phone ACTIONS route to Phone Link; questions ABOUT phone
numbers are memory lookups and must fall through to the LLM router (which has
an answer-from-memory surface).

Live failure (July 20 2026): "find me Conor Kane's phone number based on your
memories" keyword-matched bare "phone", force-routed to phone_link, ground the
recipient to a celebrity notification sender, and read the wrong thread.

Live failure (July 22 2026): "…snapchat.com/web and send … this message…"
matched bare "message", forced Phone Link SMS instead of browser Snapchat.

Live failure (Aug 26 2026): "Tell me what is in this file?" with a PDF attached
force-routed to Phone Link. The heuristic ran over the whole agent goal, which
Add context had merged the document into — a bare "text"/"call"/"phone"
anywhere in an attached file hijacked the surface. The fix is the seam, not the
wordlist: send() reads the TYPED message (`display`), never the merged goal.
"""
from __future__ import annotations

import pathlib
import unittest

from unittest import mock

from app.services import agent_bridge
from app.services.agent_bridge import _guess_surface


class GuessSurfaceTests(unittest.TestCase):
    def test_phone_number_questions_are_not_phone_tasks(self):
        for q in (
            "Can you find me Conor Kanes phone number based on your memorys?",
            "what is Abby's phone number",
            "do you have Marc's phone number?",
            "look up Chris Falloon's phone # for me",
        ):
            self.assertIsNone(_guess_surface(q), msg=q)

    def test_phone_actions_still_route_to_phone_link(self):
        for q in (
            "text Abby I'm running late",
            "call Conor Kane",
            "phone Mom",
            "reply to Marc that the deck is done",
            "send a text to Patrick",
        ):
            self.assertEqual(_guess_surface(q), "phone_link", msg=q)

    def test_ambiguous_send_message_goes_to_router(self):
        # "send a message to X" is ambiguous (SMS / web chat / email) — the
        # fast path must NOT force it; the LLM router decides.
        self.assertIsNone(_guess_surface("send a message to Patrick"))
        self.assertIsNone(_guess_surface("message Patrick about dinner"))

    def test_web_chat_message_does_not_force_phone_link(self):
        # Named web chat / URL → LLM router (browser), never forced SMS.
        for q in (
            "Can you go to https://www.snapchat.com/web and send GOBLIN this message "
            '"hello"',
            "open snapchat.com/web and read what they said",
            "send a message on Snapchat Web to them",
            "read my Discord DMs in the browser",
            "whatsapp web — what did they message me",
        ):
            self.assertIsNone(_guess_surface(q), msg=q)

    def test_messages_plural_falls_to_router(self):
        # Plural "messages" is not an SMS force cue — LLM router decides.
        self.assertIsNone(_guess_surface("read my messages from Patrick"))

    def test_bare_open_still_desktop(self):
        self.assertEqual(_guess_surface("open notepad"), "desktop")


class SnapchatProviderTipTests(unittest.TestCase):
    def test_tip_on_snapchat_hosts(self):
        from browser_agent.provider_tips import tips_for_url

        tip = tips_for_url("https://www.snapchat.com/web")
        self.assertIn("Web chat tip", tip)
        self.assertIn("read", tip.lower())
        tip2 = tips_for_url("https://www.snapchat.com/web/abc-123")
        self.assertIn("Web chat tip", tip2)


class AttachedContextDoesNotPickTheSurfaceTests(unittest.TestCase):
    """Add context merges attachments and notes into the agent goal. Only the
    user's typed instruction may choose a fast-lane surface — a document is
    evidence, not intent."""

    # Every one of these words appears in ordinary business documents.
    DOC = ("[ATTACHED DOCUMENT]\nFilename: brief.pdf\n"
           "Phone Link (notification capture, SMS) — excluded from MVP.\n"
           "Jon called on 7/27; see the call notes and the text routing bench.\n"
           "Phone: (215) 779-6380\n")

    def _sent(self, message: str, context: str | None):
        """The command send() enqueues for one chat turn."""
        goal = (f"USER-PROVIDED CONTEXT:\n{context}\n\nUser request: {message}"
                if context else message)
        w = agent_bridge.AgentWorker()
        with mock.patch.object(w, "start"), \
             mock.patch.object(agent_bridge, "_should_plan", return_value=False):
            w.send(goal, display=message)
        if not w.fast_q.empty():
            return w.fast_q.get_nowait()
        return w.cmd_q.get_nowait()

    def test_document_words_cannot_hijack_the_surface(self) -> None:
        cmd = self._sent("Tell me what is in this file?", self.DOC)
        self.assertIsNone(cmd["surface"])

    def test_the_goal_still_carries_the_document(self) -> None:
        # The fix must narrow what is SCANNED, not what is sent to the agent.
        cmd = self._sent("Tell me what is in this file?", self.DOC)
        self.assertIn("brief.pdf", cmd["text"])
        self.assertEqual(cmd["display"], "Tell me what is in this file?")

    def test_a_real_phone_instruction_still_fast_lanes_with_a_file_attached(self) -> None:
        cmd = self._sent("text Marc the summary", self.DOC)
        self.assertEqual(cmd["surface"], "phone_link")

    def test_a_real_desktop_instruction_still_fast_lanes(self) -> None:
        cmd = self._sent("open cursor", self.DOC)
        self.assertEqual(cmd["surface"], "desktop")

    def test_document_does_not_trigger_multi_task_fan_out(self) -> None:
        # looks_multi fires on a semicolon or two list-ish lines — i.e. on
        # almost any document. Left unfixed, correcting the surface bug alone
        # would just move the failure from Phone Link to task decomposition.
        doc = self.DOC + "Goals: ship; recruit; measure.\n- one\n- two\n"
        with mock.patch("app.services.multitask.enabled", return_value=True):
            cmd = self._sent("Tell me what is in this file?", doc)
        self.assertFalse(cmd["multi"])

    def test_a_genuinely_multi_part_instruction_still_fans_out(self) -> None:
        with mock.patch("app.services.multitask.enabled", return_value=True):
            cmd = self._sent("draft the follow-up email; then add it to my tasks",
                             self.DOC)
        self.assertTrue(cmd["multi"])

    def test_no_context_is_unchanged(self) -> None:
        self.assertEqual(self._sent("call Andy", None)["surface"], "phone_link")
        self.assertIsNone(self._sent("what is open with Justin?", None)["surface"])

    def test_the_attach_header_carries_no_fast_lane_trigger_word(self) -> None:
        # The header Mnemos prepends to an upload is itself part of the goal on
        # any path that still scans it — it must not read as an instruction.
        # (Aug 26 2026: "Answer from ITS text below" matched \btext\b.)
        from app.services import attachments
        src = pathlib.Path(attachments.__file__).read_text(encoding="utf-8")
        start = src.index("[ATTACHED DOCUMENT")
        header = src[start:src.index("Filename:", start)]
        self.assertIsNone(_guess_surface(header), msg=header)


if __name__ == "__main__":
    unittest.main()
