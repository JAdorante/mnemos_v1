"""_guess_surface: phone ACTIONS route to Phone Link; questions ABOUT phone
numbers are memory lookups and must fall through to the LLM router (which has
an answer-from-memory surface).

Live failure (July 20 2026): "find me Conor Kane's phone number based on your
memories" keyword-matched bare "phone", force-routed to phone_link, ground the
recipient to a celebrity notification sender, and read the wrong thread.

Live failure (July 22 2026): "…snapchat.com/web and send … this message…"
matched bare "message", forced Phone Link SMS instead of browser Snapchat.
"""
from __future__ import annotations

import unittest

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


if __name__ == "__main__":
    unittest.main()
