"""Plan 2.4 — contact-attribution fixtures + write-path + article mint-deny."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "goldens" / "contact_attribution.jsonl"
GEN = ROOT / "scripts" / "gen_contact_attribution_golden.py"
EVAL = ROOT / "scripts" / "eval_contact_attribution.py"


def _ensure_golden() -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, str(GEN)], cwd=str(ROOT))


def test_golden_fixture_shape():
    _ensure_golden()
    rows = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    assert len(rows) >= 50, f"expected ≥50 cases, got {len(rows)}"
    mandates = [r for r in rows if r.get("mandate")]
    assert len(mandates) >= 5, f"expected ≥5 mandate sentences, got {len(mandates)}"
    cats = {r.get("category") for r in rows}
    for required in ("mandate", "article_mint_deny", "co_mention_theft", "weak_review"):
        assert required in cats, f"missing category {required}"


def test_offline_eval_thresholds():
    _ensure_golden()
    proc = subprocess.run(
        [sys.executable, str(EVAL)],
        cwd=str(ROOT), capture_output=True, text=True)
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
    assert proc.returncode == 0, "eval_contact_attribution thresholds failed"


class ContactAttributionWritePathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_attr_"))
        from app.storage import Store
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.env = patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"})
        self.env.start()
        self.now = 1_700_000_000.0

    def tearDown(self):
        self.env.stop()

    def test_mandate_sentences(self):
        from app.services import people_pipeline as pp
        # 1 possessive
        marc = self.store.insert_person("Marc", ts=self.now, promotion_state="active")
        ids = pp.attribute_contacts_from_text(
            "Marc's email is marc@acme.com.",
            store=self.store, person_id=marc, person_name="Marc",
            event_id=1, now=self.now, event_source="audio.whisper")
        self.assertTrue(ids)
        # 3 co-mention: Justin must not steal
        justin = self.store.insert_person(
            "Justin", ts=self.now, promotion_state="active")
        bad = pp.attribute_contacts_from_text(
            "Justin will email Marc at marc@acme.com.",
            store=self.store, person_id=justin, person_name="Justin",
            event_id=2, now=self.now, event_source="audio.whisper")
        self.assertEqual(bad, [])
        # Marc still gets it
        good = pp.attribute_contacts_from_text(
            "Justin will email Marc at marc@acme.com.",
            store=self.store, person_id=marc, person_name="Marc",
            event_id=3, now=self.now, event_source="audio.whisper")
        self.assertTrue(good)

    def test_weak_score_routes_to_review_not_write(self):
        from app.services import people_pipeline as pp
        marc = self.store.insert_person("Marc", ts=self.now, promotion_state="active")
        details = pp.attribute_contacts_detailed(
            "Catching up with Marc later — also marc@acme.com is on the thread.",
            store=self.store, person_id=marc, person_name="Marc",
            event_id=4, now=self.now, event_source="audio.whisper")
        self.assertTrue(any(d.action == "review" for d in details))
        self.assertFalse(any(d.action == "write" for d in details))
        pts = self.store.list_contact_points(marc, type_="email")
        self.assertEqual(pts, [])
        revs = self.store.list_adjudications(kind="contact_review", limit=10)
        self.assertTrue(revs)

    def test_article_mentioned_mint_deny(self):
        from app.services import people_pipeline as pp
        from app.services import source_policy as sp
        text = "The article mentioned Bill Clinton at clinton@example.com"
        pol = sp.policy_for_event(
            event_source="desktop.screen", window="Chrome", text=text)
        self.assertEqual(pol.source_class, "news_page")
        self.assertFalse(pol.create_person_candidates)
        self.assertFalse(pol.extract_contacts)
        # Resolve must not mint
        res = pp.resolve_person_mention(
            "Bill Clinton", store=self.store, event_id=5,
            event_source="desktop.screen", window="Chrome", text=text,
            now=self.now)
        self.assertEqual(res.decision, "reject")
        self.assertIsNone(res.person_id)
        # Contact write denied
        pid = self.store.insert_person(
            "Bill", ts=self.now, promotion_state="active")
        ids = pp.attribute_contacts_from_text(
            text, store=self.store, person_id=pid, person_name="Bill",
            event_id=6, now=self.now,
            event_source="desktop.screen", window="Chrome")
        self.assertEqual(ids, [])
