"""Tier 1 (Track A) — de-hardcoded user-tailored literals stay data-driven.

A1: the phone_link "who should I text?" hint uses a REAL contact from the user's
    data, and degrades to name-free phrasing on an empty data dir.
A2: few-shot example names render from example_terms() — neutral placeholders by
    default (no real names in the prompt), the user's own vocabulary when opted in.
A3: the Gmail compose recipe left EXECUTOR_SYSTEM and is injected as a per-page
    provider tip only when that provider's page is loaded.
"""
from __future__ import annotations

import unittest


class A1PhoneLinkHintTests(unittest.TestCase):
    def test_neutral_fallback_when_no_data(self) -> None:
        import app.services.phone_link as pl
        import app.services.speakers as sp
        import app.services.vocabulary as vo
        real_spk, real_vocab = sp.speakers.enrolled_names, vo.vocabulary.get_bias_terms
        sp.speakers.enrolled_names = lambda: []
        vo.vocabulary.get_bias_terms = lambda force=False: {
            "people": [], "projects": [], "orgs": [], "aliases": []}
        try:
            self.assertIsNone(pl._example_recipient())
            # exercise the actual send-without-recipient branch (force _enabled true)
            real_name = pl.os.name
            pl.os.name = "nt"
            pl.os.environ["QUILL_PHONE_LINK"] = "1"
            try:
                msg, status = pl.execute_goal(
                    "text", {"action": "send_sms", "recipient": "", "message": ""})
            finally:
                pl.os.name = real_name
            self.assertEqual(status, "needs_details")
            self.assertIn("<name>", msg)          # name-free, generic phrasing
            for banned in ("Justin", "Marc", "Abby"):
                self.assertNotIn(banned, msg)
        finally:
            sp.speakers.enrolled_names = real_spk
            vo.vocabulary.get_bias_terms = real_vocab

    def test_uses_real_contact_when_present(self) -> None:
        import app.services.phone_link as pl
        import app.services.speakers as sp
        real = sp.speakers.enrolled_names
        sp.speakers.enrolled_names = lambda: ["Dana"]
        try:
            self.assertEqual(pl._example_recipient(), "Dana")
        finally:
            sp.speakers.enrolled_names = real


class A2ExampleTermsTests(unittest.TestCase):
    def test_default_is_neutral_placeholders(self) -> None:
        import os
        from app.services import vocabulary as vo
        os.environ.pop("QUILL_PROMPT_EXAMPLES_FROM_DATA", None)
        vo.reset_example_terms()
        ex = vo.example_terms()
        self.assertEqual(ex["person"], "<name>")
        self.assertEqual(ex["company"], "Acme")

    def test_rendered_prompts_have_no_real_names(self) -> None:
        import browser_agent.prompts as P
        import browser_agent.tools as T
        import app.services.extractor as EX
        props = EX._SCHEMA["properties"]
        appr = next(t for t in T.ACTION_TOOLS if t["name"] == "request_approval")
        blob = (P.ROUTER_SYSTEM
                + props["entities"]["items"]["properties"]["name"]["description"]
                + props["relations"]["description"]
                + appr["input_schema"]["properties"]["summary"]["description"])
        for banned in ("Justin", "Marc", "Abby", "Chris", "TechCorp",
                       "Dell Capital", "'Sparrow's"):
            self.assertNotIn(banned, blob)

    def test_opt_in_pulls_user_vocabulary(self) -> None:
        import os
        from app.services import vocabulary as vo
        real = vo.vocabulary.get_bias_terms
        vo.vocabulary.get_bias_terms = lambda force=False: {
            "people": ["Dana", "Erin"], "orgs": ["Northwind"],
            "projects": ["Atlas"], "aliases": []}
        os.environ["QUILL_PROMPT_EXAMPLES_FROM_DATA"] = "1"
        vo.reset_example_terms()
        try:
            ex = vo.example_terms()
            self.assertEqual(ex["person"], "Dana")
            self.assertEqual(ex["teammate"], "Erin")
            self.assertEqual(ex["company"], "Northwind")
            self.assertEqual(ex["project"], "Atlas")
        finally:
            vo.vocabulary.get_bias_terms = real
            os.environ.pop("QUILL_PROMPT_EXAMPLES_FROM_DATA", None)
            vo.reset_example_terms()


class A3ProviderTipsTests(unittest.TestCase):
    def test_gmail_block_removed_from_executor_prompt(self) -> None:
        from browser_agent.prompts import EXECUTOR_SYSTEM
        self.assertNotIn("view=cm&fs=1", EXECUTOR_SYSTEM)
        self.assertIn("PROVIDER TIP", EXECUTOR_SYSTEM)

    def test_tip_only_for_matching_provider(self) -> None:
        from browser_agent.provider_tips import tips_for_url
        self.assertIn("view=cm&fs=1",
                      tips_for_url("https://mail.google.com/mail/u/0/#inbox"))
        self.assertEqual(tips_for_url("https://example.com"), "")
        self.assertEqual(tips_for_url("about:blank"), "")
        self.assertEqual(tips_for_url(""), "")


if __name__ == "__main__":
    unittest.main()
