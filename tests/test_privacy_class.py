"""Plan 6.1 — privacy_class stamp + cloud egress gate."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock


class ClassifyTests(unittest.TestCase):
    def test_secret_is_never_send(self):
        from app.services import privacy_class as pc

        self.assertEqual(
            pc.classify_text("sk-ant-api03-abcdefghijklmnopqrstuvwxyz"),
            pc.NEVER_SEND)
        self.assertEqual(
            pc.classify_text("-----BEGIN RSA PRIVATE KEY-----\nabc\n"
                             "-----END RSA PRIVATE KEY-----"),
            pc.NEVER_SEND)

    def test_health_finance_is_sensitive(self):
        from app.services import privacy_class as pc

        self.assertEqual(
            pc.classify_text("Discussed the medical diagnosis with her doctor"),
            pc.SENSITIVE)
        self.assertEqual(
            pc.classify_text("Please send the wire transfer today"),
            pc.SENSITIVE)

    def test_email_is_personal(self):
        from app.services import privacy_class as pc

        self.assertEqual(
            pc.classify_text("Reach me at marc@example.com tomorrow"),
            pc.PERSONAL)

    def test_default_internal(self):
        from app.services import privacy_class as pc

        self.assertEqual(
            pc.classify_text("Meeting notes about the product launch"),
            pc.INTERNAL)

    def test_sensitive_window_title(self):
        from app.services import privacy_class as pc

        self.assertEqual(
            pc.classify_text("notes", title="1Password — Login"),
            pc.NEVER_SEND)


class StampEventTests(unittest.TestCase):
    def test_insert_stamps_privacy_class(self):
        from app.events import Event, Modality
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                ev = Event(
                    time=time.time(), modality=Modality.TEXT,
                    raw="sk-ant-api03-abcdefghijklmnopqrstuvwxyz",
                    summary="key paste", source="chat.user",
                )
                store.insert(ev)
                self.assertEqual(ev.privacy_class, "never-send")
                # Round-trip via recent_events (parses meta JSON).
                rows = store.recent_events(limit=5)
                hit = next(
                    (r for r in rows
                     if "sk-ant-" in (r.get("raw") or "")),
                    None)
                self.assertIsNotNone(hit)
                meta = hit.get("meta") or {}
                if isinstance(meta, str):
                    import json
                    meta = json.loads(meta)
                self.assertEqual(meta.get("privacy_class"), "never-send")
            finally:
                store.close()

    def test_keeps_higher_existing_class(self):
        from app.events import Event, Modality
        from app.services.privacy_class import stamp_event

        ev = Event(
            time=time.time(), modality=Modality.TEXT,
            raw="hello world", source="test",
            meta={"privacy_class": "sensitive"},
        )
        stamp_event(ev)
        self.assertEqual(ev.privacy_class, "sensitive")


class GateCloudTests(unittest.TestCase):
    def test_never_send_refuses(self):
        from app.services.privacy_class import PrivacyRefuse, gate_cloud

        with self.assertRaises(PrivacyRefuse):
            gate_cloud(
                "You are helpful.",
                [{"role": "user",
                  "content": "my key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}],
            )

    def test_sensitive_goes_through_redact_action(self):
        from app.services import privacy_class as pc

        system, messages, cls, action = pc.gate_cloud(
            "System",
            [{"role": "user",
              "content": "Discussed medical diagnosis and next steps"}],
        )
        self.assertEqual(cls, pc.SENSITIVE)
        self.assertEqual(action, "redact")
        self.assertIn("medical", messages[0]["content"].lower())

    def test_sensitive_plus_secret_refuses(self):
        from app.services.privacy_class import PrivacyRefuse, gate_cloud

        with self.assertRaises(PrivacyRefuse):
            gate_cloud(
                "System",
                [{"role": "user",
                  "content": ("medical file plus "
                              "sk-ant-api03-abcdefghijklmnopqrstuvwxyz")}],
            )

    def test_complete_claude_never_sends_secret(self):
        from app.services.model_router import ModelRouter
        from app.services.privacy_class import PrivacyRefuse

        r = ModelRouter()
        client = mock.Mock()
        r._client = client
        with self.assertRaises(PrivacyRefuse):
            r._complete_claude(
                "chat",
                system="s",
                messages=[{"role": "user",
                           "content": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}],
            )
        client.messages.create.assert_not_called()

    def test_complete_claude_redacts_then_calls(self):
        from app.services.model_router import ModelRouter

        r = ModelRouter()
        block = mock.Mock(type="text", text="ok")
        resp = mock.Mock(content=[block], usage=mock.Mock(
            input_tokens=1, output_tokens=1))
        client = mock.Mock()
        client.messages.create.return_value = resp
        r._client = client

        out = r._complete_claude(
            "chat",
            system="s",
            messages=[{"role": "user",
                       "content": "Discussed medical diagnosis today"}],
        )
        self.assertEqual(out, "ok")
        client.messages.create.assert_called_once()
        kwargs = client.messages.create.call_args.kwargs
        # No raw secret shapes in what was sent
        sent = str(kwargs.get("messages"))
        self.assertNotIn("sk-ant-", sent)


if __name__ == "__main__":
    unittest.main()
