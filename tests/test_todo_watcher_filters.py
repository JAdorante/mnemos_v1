"""Todo watcher must not offer terminal / Flask / schema junk as tasks."""
from __future__ import annotations

import unittest

from app.events import Event, Modality
from app.services.todo_watcher import (
    _is_junk_item,
    _is_log_surface,
    _todo_payload,
)


def _ev(*, window="", title="", items=None, ctype="todo_list",
        ocr="", summary="", source="desktop.screen") -> Event:
    return Event(
        time=1.0,
        modality=Modality.VISION,
        raw=ocr or title,
        summary=summary or title,
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


class LogJunkFilterTests(unittest.TestCase):
    def test_flask_lines_are_junk_items(self) -> None:
        self.assertTrue(_is_junk_item("Serving Flask app 'exec_webapp'"))
        self.assertTrue(_is_junk_item("Debug mode: off"))
        self.assertTrue(_is_junk_item("Running on http://127.0.0.1:5000"))

    def test_smoke_offer_payload_rejected(self) -> None:
        """Exact false-positive from the live smoke test."""
        ev = _ev(
            window="Windows PowerShell",
            title="User-scoped My Contacts (activity ownership)",
            items=[
                "Serving Flask app 'exec_webapp'",
                "Debug mode: off",
                "Running on http://127.0.0.1:5000",
            ],
            ocr="Serving Flask app 'exec_webapp'\nDebug mode: off\nRunning on http://127.0.0.1:5000",
        )
        self.assertTrue(_is_log_surface(
            "Windows PowerShell",
            "User-scoped My Contacts (activity ownership)",
            ev.meta["vision"]["ocr_text"],
            "",
        ))
        self.assertIsNone(_todo_payload(ev))

    def test_console_window_rejected_even_with_clean_items(self) -> None:
        ev = _ev(
            window="Windows Terminal",
            title="To Do List",
            items=["Buy milk today", "Call the dentist"],
            ocr="To Do List\n1. Buy milk today",
        )
        self.assertIsNone(_todo_payload(ev))

    def test_real_notepad_todo_accepted(self) -> None:
        ev = _ev(
            window="*To Do List - Notepad",
            title="To Do List",
            items=[
                "Text Abby Nengel: I Love You",
                "Do research on quantum",
                "Look into flights to SFO",
            ],
            ocr="To Do List\n1. Text Abby\n2. Do research",
        )
        p = _todo_payload(ev)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(len(p["items"]), 3)

    def test_desktop_code_editor_without_todo_header_rejected(self) -> None:
        ev = _ev(
            window="agent_bridge.py - nexus_v1 - Cursor",
            title="Mixed editor view",
            items=["Fix the router", "Add unit tests"],
            ctype="todo_list",
            ocr="def send():\n    pass",
        )
        self.assertIsNone(_todo_payload(ev))

    def test_webcam_todo_without_window_still_ok(self) -> None:
        ev = _ev(
            window="",
            title="To Do List",
            items=["Pick up dry cleaning", "Email the landlord"],
            ocr="To Do List\n- Pick up dry cleaning",
            source="vision.webcam",
        )
        p = _todo_payload(ev)
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(len(p["items"]), 2)


if __name__ == "__main__":
    unittest.main()
