"""Titleize chatty task/commitment extracts; meetings log as Meet {person}."""
from __future__ import annotations

import unittest

from app.services.fact_titles import (
    commitment_title,
    looks_like_meeting,
    meeting_title,
    short_label,
    titleize_work_item,
)


class TitleizeTests(unittest.TestCase):
    def test_meeting_chat_becomes_meet_title(self):
        raw = "I have a meeting with Andy Karos today at 8:30 pm about Sparrow"
        self.assertEqual(
            titleize_work_item(raw),
            "Meet Andy Karos about Sparrow",
        )

    def test_make_note_of_meeting(self):
        raw = "make note of the meeting with Michael Saad today at 4:30"
        self.assertEqual(titleize_work_item(raw), "Meet Michael Saad")

    def test_you_asked_to_make_note(self):
        raw = "You asked to make note of a meeting with Michael Saad today at 4:30"
        self.assertEqual(titleize_work_item(raw), "Meet Michael Saad")

    def test_remember_prefix_stripped(self):
        raw = "Can you remember I have a meeting with Andy Karos about Sparrow"
        self.assertEqual(titleize_work_item(raw), "Meet Andy Karos about Sparrow")

    def test_promise_keeps_imperative(self):
        self.assertEqual(
            titleize_work_item("I'll send Sarah the deck by Thursday"),
            "Send Sarah the deck",
        )

    def test_idempotent(self):
        self.assertEqual(
            titleize_work_item("Meet Andy Karos about Sparrow"),
            "Meet Andy Karos about Sparrow",
        )

    def test_short_label_word_boundary(self):
        lab = short_label(
            "make note of the meeting with Michael Saad today at 4:30",
            kind="commitment",
            cap=28,
        )
        self.assertEqual(lab, "Meet Michael Saad")


class MeetingFormTests(unittest.TestCase):
    def test_looks_like_meeting_from_form(self):
        self.assertTrue(looks_like_meeting(form="meeting"))
        self.assertFalse(looks_like_meeting("send the deck", form="promise"))

    def test_looks_like_meeting_from_wording(self):
        self.assertTrue(looks_like_meeting(
            "make note of that",
            source_span="I have a meeting with Michael Saad today at 4:30",
        ))

    def test_meeting_title_from_structured_fields(self):
        self.assertEqual(
            meeting_title(counterparty="Michael Saad", topic=""),
            "Meet Michael Saad",
        )
        self.assertEqual(
            meeting_title(counterparty="Andy Karos", topic="Sparrow"),
            "Meet Andy Karos about Sparrow",
        )

    def test_commitment_title_prefers_to_person_for_meetings(self):
        # Even when the model titles it badly, structured fields win.
        self.assertEqual(
            commitment_title({
                "form": "meeting",
                "text": "make note of the meeting",
                "topic": "",
                "from_person": "me",
                "to_person": "Michael Saad",
                "source_span": "I have a meeting with Michael Saad today at 4:30",
            }),
            "Meet Michael Saad",
        )

    def test_commitment_title_heuristic_without_form(self):
        self.assertEqual(
            commitment_title({
                "text": "make note of the meeting with Michael Saad today at 4:30",
                "from_person": "me",
                "to_person": "Michael Saad",
                "source_span": "I have a meeting with Michael Saad today at 4:30",
            }),
            "Meet Michael Saad",
        )

    def test_promise_untouched_by_meeting_path(self):
        self.assertEqual(
            commitment_title({
                "form": "promise",
                "text": "I'll send Sarah the deck by Thursday",
                "from_person": "me",
                "to_person": "Sarah",
            }),
            "Send Sarah the deck",
        )


class GraphLabelTests(unittest.TestCase):
    def test_short_constellation_uses_titleizer(self):
        from app.services.graph import _short_constellation_label
        lab = _short_constellation_label(
            "make note of the meeting with Michael Saad today at 4:30",
            kind="commitment",
        )
        self.assertEqual(lab, "Meet Michael Saad")


if __name__ == "__main__":
    unittest.main()
