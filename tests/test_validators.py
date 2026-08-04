"""Deterministic field validators (plan task 1.4).

`validate_fact_fields` never raises; it returns a drop reason string or None.
Wired into `gate_fact` before the insert path, so a malformed email/phone/
price/URL/due date is dropped with a reason instead of ever reaching the
store — regardless of confidence or assertion class.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.services import fact_gate
from app.services.fact_gate import gate_fact
from app.services.validators import validate_fact_fields


class EmailValidatorTests(unittest.TestCase):
    def test_well_formed_email_passes(self):
        self.assertIsNone(validate_fact_fields(
            "claim", "reach me at hugh@example.com", None))

    def test_missing_tld_is_malformed(self):
        reason = validate_fact_fields(
            "claim", "his email is john@company", None)
        self.assertIsNotNone(reason)
        self.assertIn("email", reason)

    def test_bare_handle_mention_is_not_flagged(self):
        # "@john" (a social mention, nothing before the @) is not an email.
        self.assertIsNone(validate_fact_fields(
            "claim", "she tagged @john on the thread", None))


class PhoneValidatorTests(unittest.TestCase):
    def test_well_formed_phone_with_context_passes(self):
        self.assertIsNone(validate_fact_fields(
            "claim", "call me at 555-123-4567", None))

    def test_too_short_digit_run_with_context_is_malformed(self):
        reason = validate_fact_fields(
            "claim", "call me at 12-345", None)
        self.assertIsNotNone(reason)
        self.assertIn("phone", reason)

    def test_number_without_phone_context_is_ignored(self):
        # No phone/call/reach context word — a bare number is not a phone.
        self.assertIsNone(validate_fact_fields(
            "claim", "we sold 12-345 units this year", None))


class PriceValidatorTests(unittest.TestCase):
    def test_price_with_suffix_passes(self):
        self.assertIsNone(validate_fact_fields(
            "claim", "the pilot is $49/seat", None))

    def test_plain_price_passes(self):
        self.assertIsNone(validate_fact_fields(
            "claim", "the invoice was $4000", None))

    def test_too_many_decimal_digits_is_malformed(self):
        reason = validate_fact_fields(
            "claim", "the pilot is $49.999 per seat", None)
        self.assertIsNotNone(reason)
        self.assertIn("price", reason)

    def test_no_digits_after_dollar_is_malformed(self):
        reason = validate_fact_fields("claim", "billed in $USD", None)
        self.assertIsNotNone(reason)
        self.assertIn("price", reason)


class UrlValidatorTests(unittest.TestCase):
    def test_well_formed_url_passes(self):
        self.assertIsNone(validate_fact_fields(
            "claim", "the doc is at https://acme.io/deck", None))

    def test_malformed_url_is_flagged(self):
        reason = validate_fact_fields(
            "claim", "visit http://.com for details", None)
        self.assertIsNotNone(reason)
        self.assertIn("url", reason)


class TemporalValidatorTests(unittest.TestCase):
    def test_resolved_iso_date_passes(self):
        self.assertIsNone(validate_fact_fields(
            "task", "book the venue", {"due": "2026-08-10"}))

    def test_empty_due_passes(self):
        self.assertIsNone(validate_fact_fields(
            "task", "book the venue", {"due": ""}))

    def test_bare_weekday_is_unresolved(self):
        reason = validate_fact_fields(
            "task", "book the venue", {"due": "Friday"})
        self.assertIsNotNone(reason)
        self.assertIn("due", reason)

    def test_invalid_calendar_date_is_malformed(self):
        reason = validate_fact_fields(
            "commitment", "send the deck", {"due": "2026-13-45"})
        self.assertIsNotNone(reason)
        self.assertIn("due", reason)

    def test_claims_are_not_temporal_checked(self):
        # `due` only applies to tasks/commitments — a claim payload with a
        # stray 'due' key must not be validated against it.
        self.assertIsNone(validate_fact_fields(
            "claim", "just a claim", {"due": "Friday"}))

    def test_never_raises_on_bad_payload_shape(self):
        self.assertIsNone(validate_fact_fields("task", "x", "not-a-dict"))


class GateWiringTests(unittest.TestCase):
    """validate_fact_fields is wired into gate_fact ahead of the insert path."""

    def setUp(self):
        patches = [patch.object(fact_gate, "_telemetry", lambda *a, **k: None),
                   patch.object(fact_gate, "_similar_active", return_value=[])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_malformed_email_drops_before_insert(self):
        v = gate_fact("claim", "his email is john@company", 0.9,
                      "his email is john@company",
                      "his email is john@company")
        self.assertEqual(v.action, "drop")
        self.assertIn("email", v.reason)

    def test_malformed_price_drops_before_insert(self):
        v = gate_fact("claim", "the pilot is $49.999", 0.9,
                      "the pilot is $49.999", "the pilot is $49.999")
        self.assertEqual(v.action, "drop")
        self.assertIn("price", v.reason)

    def test_malformed_due_drops_task(self):
        v = gate_fact("task", "book the venue", 0.9,
                      "book the venue", "book the venue",
                      payload={"due": "Friday"})
        self.assertEqual(v.action, "drop")
        self.assertIn("due", v.reason)

    def test_clean_fields_still_insert(self):
        v = gate_fact("task", "book the venue", 0.9,
                      "book the venue", "book the venue",
                      payload={"due": "2026-08-10"})
        self.assertEqual(v.action, "insert")

    def test_validator_beats_review_assertion(self):
        # A malformed field is dropped even for a quoted/hypothetical fact —
        # objective correctness is checked before subjective assertion class.
        v = gate_fact("claim", "his email is john@company", 0.9,
                      "his email is john@company",
                      "his email is john@company", assertion="quoted")
        self.assertEqual(v.action, "drop")
        self.assertIn("email", v.reason)


if __name__ == "__main__":
    unittest.main()
