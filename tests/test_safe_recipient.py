"""Recipient grounding must not remap to a different person."""
from __future__ import annotations

import unittest
from unittest import mock

from browser_agent.voice_correct import safe_to_remap
from browser_agent.llm import LLM


class SafeRemapTests(unittest.TestCase):
    def test_allows_first_name_expansion(self) -> None:
        self.assertTrue(safe_to_remap("Abby", "Abby Nengel"))
        self.assertTrue(safe_to_remap("pat", "Pat Smith"))

    def test_blocks_different_person(self) -> None:
        self.assertFalse(safe_to_remap("Conor Kane", "Courtney"))
        self.assertFalse(safe_to_remap("Ann Smith", "Bob Jones"))
        self.assertFalse(safe_to_remap("Jordan Lee", "Alex"))

    def test_blocks_shared_given_name_different_surname(self) -> None:
        # Live failure: "Conor Kane" silently became "Conor McGregor" because
        # a shared first name alone counted as the same person.
        self.assertFalse(safe_to_remap("Conor Kane", "Conor McGregor"))
        self.assertFalse(safe_to_remap("Chris Falloon", "Chris Rock"))

    def test_allows_surname_stt_typo_and_bare_given(self) -> None:
        self.assertTrue(safe_to_remap("Abby Nagle", "Abby Nengel"))
        self.assertTrue(safe_to_remap("Conor Kane", "Conor"))

    def test_allows_reversed_contact_format(self) -> None:
        # Phone Link lists contacts as "Last, First".
        self.assertTrue(safe_to_remap("Chris Falloon", "Falloon, Chris"))

    def test_allows_same_surname_close_typo(self) -> None:
        self.assertTrue(safe_to_remap("Jon Smith", "John Smith"))

    def test_resolve_keeps_spoken_when_unsafe(self) -> None:
        llm = LLM.__new__(LLM)
        llm._json_call = mock.Mock(
            return_value={"name": "Courtney", "confidence": "high",
                          "reason": "should not matter"})
        contacts = ["Dan", "Courtney", "Chris", "Abby Nengel"]
        out = LLM.resolve_recipient(llm, "Conor Kane", contacts, "")
        self.assertEqual(out["name"], "Conor Kane")
        self.assertFalse(out["changed"])
        llm._json_call.assert_not_called()

    def test_resolve_never_swaps_surname_for_celebrity_contact(self) -> None:
        # The live Phone Link contact list is polluted with notification
        # senders; "Conor Kane" must survive "Conor McGregor" being present
        # even if the tiebreak model would happily pick it.
        llm = LLM.__new__(LLM)
        llm._json_call = mock.Mock(
            return_value={"name": "Conor McGregor", "confidence": "high",
                          "reason": "closest first name"})
        contacts = ["Phone", "Conor McGregor", "Patrick Adorante",
                    "Falloon, Chris", "Abby Nengel"]
        out = LLM.resolve_recipient(llm, "Conor Kane", contacts, "")
        self.assertEqual(out["name"], "Conor Kane")
        self.assertFalse(out["changed"])

    def test_resolve_expands_first_name(self) -> None:
        llm = LLM.__new__(LLM)
        llm._json_call = mock.Mock(side_effect=AssertionError("no LLM"))
        out = LLM.resolve_recipient(
            llm, "Abby", ["Dan", "Abby Nengel", "Chris"], "")
        self.assertEqual(out["name"], "Abby Nengel")
        self.assertTrue(out["changed"])


if __name__ == "__main__":
    unittest.main()
