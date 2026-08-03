"""Tests for secret/PII redaction (services/redact.py) and its three wiring
points: the VLMRouter cloud-skip gate, escalate_log write-boundary redaction,
and the sensitive-window capture skip.

Providers are faked; no network, no real model calls.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app.services import redact
from app.services.escalate_log import escalate_log
from app.services.vlm import VLMRouter

# Plausible-shaped, non-live test values.
_ANT_KEY = "sk-ant-api03-" + "a1B2" * 12
_ENV_OCR = ("# Apollo\nAPOLLO_API_KEY=naQtHKb1FPjQQ2wmXYZAB\n"
            "GOOGLE_CLOUD_PROJECT=my-project\n")


def _res(content_type="none", confidence=0.9, **kw) -> dict:
    return {"description": "a scene", "ocr_text": "", "people_count": 0,
            "objects": [], "scene_type": "desk", "content_type": content_type,
            "title": "", "items": [], "item_confidences": [],
            "confidence": confidence, **kw}


class _FakeProvider:
    def __init__(self, res=None, model="fake-model"):
        self.model = model
        self._res = res or _res()
        self.calls = 0

    def describe(self, jpeg_bytes: bytes) -> dict:
        self.calls += 1
        return dict(self._res)


class DetectionTests(unittest.TestCase):
    def test_provider_keys_detected_and_redacted(self) -> None:
        cases = {
            "anthropic_key": f"key is {_ANT_KEY} ok",
            "aws_key_id": "AKIAIOSFODNN7EXAMPLE",
            "github_token": "ghp_" + "x1" * 12,
            "slack_token": "xoxb-1234567890-abcdef",
            "google_key": "AIza" + "B7c" * 12,
            "private_key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQ...",
            "url_credentials": "postgres://admin:hunter22@db.local/x",
        }
        for kind, text in cases.items():
            self.assertIn(kind, redact.scan(text), kind)
            out = redact.redact_text(text)
            self.assertIn(f"[REDACTED:{kind}]", out, kind)

    def test_env_assignment_lines(self) -> None:
        self.assertIn("env_assignment", redact.scan(_ENV_OCR))
        out = redact.redact_text(_ENV_OCR)
        self.assertNotIn("naQtHKb1FPjQQ2wmXYZAB", out)
        # Non-secret env lines on the same page survive.
        self.assertIn("GOOGLE_CLOUD_PROJECT=my-project", out)

    def test_kv_secret_lowercase_lines(self) -> None:
        text = "login info\npassword: hunter22blue\nusername: justin\n"
        self.assertIn("kv_secret", redact.scan(text))
        out = redact.redact_text(text)
        self.assertNotIn("hunter22blue", out)
        self.assertIn("username: justin", out)

    def test_card_and_ssn(self) -> None:
        self.assertEqual(redact.scan("card 4111 1111 1111 1111 exp 12/28"),
                         ["card_number"])
        self.assertIn("ssn", redact.scan("SSN 078-05-1120 on file"))

    def test_clean_text_stays_clean(self) -> None:
        text = ("Meeting notes: discuss Q3 roadmap with Sam.\n"
                "TODO: email the deck to the team.\n")
        self.assertEqual(redact.scan(text), [])
        self.assertEqual(redact.redact_text(text), text)


class FalsePositiveGuardTests(unittest.TestCase):
    """Shapes that burned the first cleanup pass must stay unflagged."""

    def test_float_mantissas_not_cards(self) -> None:
        self.assertEqual(redact.scan(
            '{"cent": 0.7010934059657331, "sem": 0.5510022884511111}'), [])

    def test_id_timestamp_chains_not_cards(self) -> None:
        self.assertEqual(redact.scan(
            "1151-1784749803.7072284-661-1784749812.8831997-102"), [])

    def test_hex_hashes_not_cards(self) -> None:
        self.assertEqual(redact.scan("a007ea20b7ba204de188238574835138"), [])

    def test_monotone_zero_runs_not_cards(self) -> None:
        self.assertEqual(redact.scan("poeple_0000000000000000, 1"), [])

    def test_keyword_is_not_key(self) -> None:
        self.assertEqual(redact.scan("NEWAPI_KEYWORD_LOC: 'body',"), [])

    def test_prose_colon_lines_not_kv_secrets(self) -> None:
        self.assertEqual(redact.scan("monkey: banana-bread recipe"), [])


class PayloadTests(unittest.TestCase):
    def test_scan_and_redact_nested_payload(self) -> None:
        payload = {"ocr_text": _ENV_OCR, "items": [f"key {_ANT_KEY}"],
                   "confidence": 0.9, "n": None}
        kinds = redact.scan_payload(payload)
        self.assertIn("env_assignment", kinds)
        self.assertIn("anthropic_key", kinds)
        out = redact.redact_payload(payload)
        self.assertNotIn(_ANT_KEY, json.dumps(out))
        self.assertEqual(out["confidence"], 0.9)   # non-strings untouched
        self.assertIsNone(out["n"])

    def test_sensitive_windows(self) -> None:
        for title in (".env - nexus_v1 - Visual Studio Code",
                      "id_rsa - Notepad", "secrets.yaml - repo - Code",
                      "KeePassXC", "1Password", "my passwords.txt"):
            self.assertTrue(redact.is_sensitive_window(title), title)
        for title in ("Inbox - Gmail - Chrome", "quarterly report.docx - Word",
                      "The Environment Agency - Chrome", ""):
            self.assertFalse(redact.is_sensitive_window(title), title)


class _TempTrailMixin:
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_redact_"))
        self.trail = self.tmp / "escalate_distill.jsonl"
        self._orig = (escalate_log._path, escalate_log._counts, escalate_log._total)
        escalate_log._path = self.trail
        escalate_log._counts = Counter()
        escalate_log._total = 0

    def tearDown(self) -> None:
        escalate_log._path, escalate_log._counts, escalate_log._total = self._orig


class RouterSecretGateTests(_TempTrailMixin, unittest.TestCase):
    def _router(self, local, claude) -> VLMRouter:
        r = VLMRouter()
        r.local = local
        r.claude = claude
        r.claude_lite = claude
        r._local_ok = True
        return r

    def test_secret_ocr_skips_cloud_and_distill(self) -> None:
        # todo_list would normally force the accurate reader (hard_type).
        local = _FakeProvider(_res("todo_list", 0.9, ocr_text=_ENV_OCR))
        claude = _FakeProvider(_res("todo_list", 0.99))
        out = self._router(local, claude).describe(b"jpg")
        self.assertEqual(claude.calls, 0)            # nothing left the machine
        self.assertFalse(self.trail.exists())        # no distill row either
        self.assertEqual(out["_provider"], "ollama")
        self.assertEqual(out["_route"]["reason"], "secret_detected")
        self.assertIn("env_assignment", out["_route"]["secret_kinds"])
        self.assertNotIn("naQtHKb1FPjQQ2wm", out["ocr_text"])

    def test_parent_results_redacted_before_return_and_log(self) -> None:
        # The local pass can miss a secret the parent's sharper OCR then
        # reads; the parent result must still come back redacted and the
        # distill row must be clean.
        local = _FakeProvider(_res("todo_list", 0.9, ocr_text="fuzzy text"))
        claude = _FakeProvider(_res("todo_list", 0.95,
                                    ocr_text=f"x = '{_ANT_KEY}'"))
        out = self._router(local, claude).describe(b"jpg")
        self.assertEqual(claude.calls, 1)
        self.assertNotIn(_ANT_KEY, json.dumps(out))
        self.assertIn("[REDACTED:anthropic_key]", out["ocr_text"])
        self.assertNotIn(_ANT_KEY, self.trail.read_text(encoding="utf-8"))

    def test_clean_ocr_still_escalates(self) -> None:
        local = _FakeProvider(_res("todo_list", 0.9, ocr_text="buy milk"))
        claude = _FakeProvider(_res("todo_list", 0.99, ocr_text="buy milk"))
        out = self._router(local, claude).describe(b"jpg")
        self.assertEqual(claude.calls, 1)
        self.assertEqual(out["_provider"], "claude")


class EscalateLogRedactionTests(_TempTrailMixin, unittest.TestCase):
    def test_record_redacts_all_payload_fields(self) -> None:
        escalate_log.record(
            task="vision.describe", reason="hard_type",
            local=_res(ocr_text=_ENV_OCR),
            parent=_res(ocr_text=f"token {_ANT_KEY}"),
            local_error=f"failed on {_ANT_KEY}",
            meta={"note": _ENV_OCR},
            frame_path="data/frames/x.jpg")
        text = self.trail.read_text(encoding="utf-8")
        self.assertNotIn(_ANT_KEY, text)
        self.assertNotIn("naQtHKb1FPjQQ2wm", text)
        self.assertIn("[REDACTED:anthropic_key]", text)

    def test_edited_text_redacted(self) -> None:
        row = escalate_log.record(task="vision.describe", reason="hard_type",
                                  local=_res(), parent=_res(),
                                  frame_path="data/frames/x.jpg")
        ok = escalate_log.set_user_outcome(
            "edited", row_id=row["id"], edited_text=f"use {_ANT_KEY} here")
        self.assertTrue(ok)
        self.assertNotIn(_ANT_KEY, self.trail.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
