"""WS2 privacy gating — identifiers respect the privacy_class taxonomy:
mail-derived identifiers are personal and escalate the event's class; a
never-send frame yields no identifiers at all; the router's egress gate
redacts personal-classed content before any cloud call."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from app.events import Event, Modality
from app.perception import identifiers as idn
from app.services import privacy_class as pc

NOW = 1_700_000_000.0


def _screen_event(raw: str, window: str) -> Event:
    return Event(time=NOW, modality=Modality.VISION, raw=raw,
                 source="desktop.screen", meta={"window": window})


class StampPrivacyTests(unittest.TestCase):
    def test_mail_identifiers_are_personal_and_escalate_event(self):
        ev = _screen_event(
            "Subject: Pricing follow-up\nHi Justin,", "Inbox - Outlook")
        idn.stamp_event(ev)
        idents = ev.meta.get("identifiers") or []
        subj = [i for i in idents if i["kind"] == "email_subject"]
        self.assertTrue(subj)
        self.assertEqual(subj[0]["privacy"], "personal")
        self.assertEqual(ev.meta.get("privacy_class"), "personal")

    def test_never_send_frame_yields_no_identifiers(self):
        ev = _screen_event(
            "github.com/JAdorante/mnemos_v1", "repo - Cursor")
        with patch.object(pc, "classify_text",
                          return_value=pc.NEVER_SEND):
            idn.stamp_event(ev)
        self.assertNotIn("identifiers", ev.meta)
        self.assertEqual(ev.entities, [])

    def test_internal_frame_stamps_without_escalation(self):
        ev = _screen_event(
            "working in github.com/JAdorante/mnemos_v1 now",
            "main.py - nexus_v1 - Cursor")
        idn.stamp_event(ev)
        self.assertTrue(ev.meta.get("identifiers"))
        # No personal/sensitive identifier → no class escalation stamped here.
        self.assertNotEqual(ev.meta.get("privacy_class"), "personal")
        self.assertIn("nexus_v1", ev.entities)
        self.assertIn("mnemos_v1", ev.entities)

    def test_kill_switch(self):
        ev = _screen_event("github.com/JAdorante/mnemos_v1", "x - Cursor")
        import os
        with patch.dict(os.environ, {"QUILL_IDENTIFIERS": "0"}):
            # settings is frozen at import; patch the module's cfg lookup.
            from types import SimpleNamespace
            with patch.object(idn, "_cfg",
                              return_value=SimpleNamespace(enabled=False)):
                idn.stamp_event(ev)
        self.assertNotIn("identifiers", ev.meta)


class RouterEgressTests(unittest.TestCase):
    def test_personal_class_is_redacted_before_cloud(self):
        system, messages, cls, action = pc.gate_cloud(
            "summarize", [{"role": "user",
                           "content": "subject: pricing for jadorant@x.com"}],
            declared_class="personal")
        self.assertEqual(action, "redact")
        self.assertEqual(cls, "personal")

    def test_never_send_class_refuses_cloud(self):
        with self.assertRaises(pc.PrivacyRefuse):
            pc.gate_cloud("s", [{"role": "user", "content": "hello"}],
                          declared_class="never-send")


if __name__ == "__main__":
    unittest.main()
