"""Meeting notes must not become agent batch jobs; real todos still offer."""
from __future__ import annotations

import unittest
from unittest import mock

from app.events import Event, Modality
from app.services.todo_watcher import (
    _is_actionable,
    _is_notes_document,
    _todo_payload,
    _on_event,
)


def _ev(*, window="", title="", items=None, ctype="todo_list",
        ocr="", source="desktop.screen") -> Event:
    return Event(
        time=1.0,
        modality=Modality.VISION,
        raw=ocr or title,
        summary=title,
        source=source,
        meta={
            "window": window,
            "content_type": ctype,
            "items": items or [],
            "vision": {
                "title": title,
                "content_type": ctype,
                "items": items or [],
                "ocr_text": ocr,
            },
        },
    )


class NotesVsTodosTests(unittest.TestCase):
    def test_notes_title_detected(self) -> None:
        self.assertTrue(_is_notes_document("DTC and Neo Clouds — Notes"))
        self.assertTrue(_is_notes_document("Weekly Meeting Minutes"))
        self.assertFalse(_is_notes_document("To Do List"))
        self.assertFalse(_is_notes_document("Action Items"))

    def test_section_headings_not_actionable(self) -> None:
        self.assertFalse(_is_actionable(
            "Overview: This document captures key discussion points."))
        self.assertFalse(_is_actionable("Access level: Company-wide."))
        self.assertFalse(_is_actionable("Goal: Surface the most relevant updates."))
        self.assertTrue(_is_actionable(
            "Pricing Follow-Up: Send Chris the pricing details."))
        self.assertTrue(_is_actionable(
            "Email alex@example.com a project summary"))

    def test_notes_doc_no_offer(self) -> None:
        ev = _ev(
            window="Meeting Notes - Notepad",
            title="Project Sync — Notes",
            items=[
                "Overview: This document captures discussion points.",
                "Pricing Follow-Up: Send Chris the pricing details.",
                "Access level: Company-wide.",
            ],
            ocr="Overview:\nPricing Follow-Up: Send Chris…",
        )
        p = _todo_payload(ev)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertTrue(p["notes_doc"])
        self.assertFalse(p["offer"])
        self.assertEqual(p["items"], [
            "Pricing Follow-Up: Send Chris the pricing details.",
        ])

    def test_real_todo_still_offers(self) -> None:
        ev = _ev(
            window="*To Do List - Notepad",
            title="To Do List",
            items=["Email alex@example.com a summary of the project"],
            ocr="To Do List\n1. Email alex@…",
        )
        p = _todo_payload(ev)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertTrue(p["offer"])
        self.assertFalse(p["notes_doc"])

    def test_on_event_skips_propose_for_notes(self) -> None:
        ev = _ev(
            window="Notes - Notepad",
            title="Standup Notes",
            items=["Send Chris the pricing follow-up"],
            ocr="Standup Notes",
        )
        with mock.patch("app.services.todo_watcher.extractor", create=True):
            with mock.patch("app.services.extractor.extractor") as ex:
                ex.ingest_todo_items.return_value = [1]
                with mock.patch("app.services.agent_bridge.worker") as w:
                    w.propose_todo = mock.Mock(return_value=True)
                    _on_event(ev)
                    w.propose_todo.assert_not_called()


if __name__ == "__main__":
    unittest.main()
