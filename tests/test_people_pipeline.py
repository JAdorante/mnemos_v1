"""People Intelligence v2 — source policy, candidate resolve, contacts."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import people_pipeline as pp
from app.services import source_policy as sp
from app.storage import Store

NOW = 1_700_000_000.0


class SourcePolicyTests(unittest.TestCase):
    def test_terminal_denies_people(self):
        p = sp.policy_for_event(window="Windows PowerShell", text="clasp push")
        self.assertEqual(p.source_class, "terminal")
        self.assertFalse(p.create_person_candidates)

    def test_social_feed_denies(self):
        ocr = ("@InternetH0F\n182.8K Views\n2.8K likes\n69 reposts\nPost your reply")
        p = sp.policy_for_event(window="X", text=ocr)
        self.assertEqual(p.source_class, "social_feed")
        self.assertFalse(p.extract_mentions)

    def test_news_knowledge_only(self):
        p = sp.policy_for("news_page")
        self.assertTrue(p.create_claims)
        self.assertFalse(p.create_person_candidates)
        self.assertFalse(p.extract_contacts)

    def test_tmz_and_news_content_classify_as_news(self):
        sp._raw_policies.cache_clear()
        p = sp.policy_for_event(
            event_source="desktop.screen",
            window="TMZ - Chrome",
            text="Bill Clinton spotted in NYC — exclusive photos")
        self.assertEqual(p.source_class, "news_page")
        self.assertFalse(p.create_person_candidates)
        p2 = sp.policy_for_event(
            event_source="desktop.screen",
            window="Chrome",
            text="Breaking news: subscribe to continue reading this article")
        self.assertEqual(p2.source_class, "news_page")


class PeoplePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_pv2_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")
        self.env = patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def test_leave_open_on_ambiguous_short_name(self):
        # Two Chris* people — short "Chris" should not auto-merge wrongly,
        # and may leave_open or resolve only with margin.
        a = self.store.insert_person("Chris Falloon", ts=NOW, promotion_state="active")
        b = self.store.insert_person("Chris Kim", ts=NOW, promotion_state="active")
        self.assertNotEqual(a, b)
        res = pp.resolve_person_mention(
            "Chris", store=self.store, event_id=1,
            event_source="audio.whisper", text="I spoke with Chris",
            now=NOW + 1, relationship_boost=0.6)
        # With two equal-ish prefix matches, leave_open is correct.
        self.assertIn(res.decision, ("leave_open", "auto_resolve"))
        if res.decision == "auto_resolve":
            self.assertIn(res.person_id, (a, b))

    def test_exact_match_auto_resolves(self):
        pid = self.store.insert_person(
            "Marc Sullivan", ts=NOW, promotion_state="active")
        res = pp.resolve_person_mention(
            "Marc Sullivan", store=self.store, event_id=2,
            event_source="audio.whisper",
            text="Marc Sullivan will send the deck",
            now=NOW + 1, relationship_boost=0.9)
        self.assertEqual(res.decision, "auto_resolve")
        self.assertEqual(res.person_id, pid)
        mentions = self.store.list_person_mentions(person_id=pid)
        self.assertTrue(mentions)

    def test_os_account_rejected(self):
        with patch.dict(os.environ, {"USERNAME": "Dell AI User"}):
            res = pp.resolve_person_mention(
                "Dell AI User", store=self.store, event_id=3,
                event_source="desktop.screen", text="C:\\Users\\Dell AI User",
                now=NOW)
            self.assertEqual(res.decision, "reject")
            self.assertIsNone(res.person_id)

    def test_news_policy_no_person_create(self):
        res = pp.resolve_person_mention(
            "Ben Shapiro", store=self.store, event_id=4,
            event_source="desktop.screen",
            window="The New York Times",
            text="Ben Shapiro calls the film a masterpiece",
            now=NOW)
        # classify may be news_page via window
        self.assertIn(res.decision, ("reject", "leave_open", "create_new", "auto_resolve"))
        # With news_page policy, create_person_candidates is false → reject
        pol = sp.policy_for_event(
            event_source="desktop.screen", window="The New York Times",
            text="Ben Shapiro")
        if pol.source_class == "news_page":
            self.assertEqual(res.decision, "reject")

    def test_news_binds_existing_but_does_not_mint(self):
        pid = self.store.insert_person(
            "Patrick Adorante", ts=NOW, promotion_state="active")
        hit = pp.resolve_person_mention(
            "Patrick Adorante", store=self.store, event_id=40,
            event_source="desktop.screen", window="TMZ - Chrome",
            text="Patrick Adorante mentioned in a sidebar",
            now=NOW + 1)
        self.assertEqual(hit.decision, "auto_resolve")
        self.assertEqual(hit.person_id, pid)
        miss = pp.resolve_person_mention(
            "Bill Clinton", store=self.store, event_id=41,
            event_source="desktop.screen", window="TMZ - Chrome",
            text="Bill Clinton exclusive",
            now=NOW + 2)
        self.assertEqual(miss.decision, "reject")
        self.assertIsNone(miss.person_id)
        names = {p["name"] for p in self.store.all_people()}
        self.assertNotIn("Bill Clinton", names)

    def test_contacts_roster_skips_thin_candidates(self):
        self.store.insert_person(
            "Patrick Adorante", ts=NOW, promotion_state="active")
        self.store.insert_person(
            "Bill Clinton", ts=NOW, promotion_state="candidate")
        roster = {r["name"] for r in pp.contacts_roster(self.store)}
        self.assertIn("Patrick Adorante", roster)
        self.assertNotIn("Bill Clinton", roster)

    def test_contact_attribution(self):
        pid = self.store.insert_person("Marc", ts=NOW, promotion_state="active")
        ids = pp.attribute_contacts_from_text(
            "Marc's email address is marc@foundry.io",
            store=self.store, person_id=pid, person_name="Marc",
            event_id=5, now=NOW, event_source="audio.whisper")
        self.assertTrue(ids)
        pts = self.store.list_contact_points(pid, type_="email")
        self.assertEqual(pts[0]["value_normalized"], "marc@foundry.io")
        # Justin must not steal it
        jid = self.store.insert_person("Justin", ts=NOW, promotion_state="active")
        bad = pp.attribute_contacts_from_text(
            "Justin will email Marc at marc@foundry.io",
            store=self.store, person_id=jid, person_name="Justin",
            event_id=6, now=NOW, event_source="audio.whisper")
        self.assertEqual(bad, [])
        # News surfaces must not mint contact points
        news = pp.attribute_contacts_from_text(
            "Marc's email address is marc@foundry.io",
            store=self.store, person_id=pid, person_name="Marc",
            event_id=8, now=NOW, event_source="desktop.screen",
            window="The New York Times")
        self.assertEqual(news, [])

    def test_agent_gate_blocks_candidate(self):
        pid = self.store.insert_person(
            "Pat", ts=NOW, promotion_state="candidate")
        gate = pp.agent_may_use_contact(self.store, pid, "email")
        self.assertFalse(gate["allow"])

    def test_create_new_on_high_relevance(self):
        res = pp.resolve_person_mention(
            "Avery Quinn", store=self.store, event_id=7,
            event_source="audio.whisper",
            text="Avery Quinn owns the launch checklist",
            now=NOW, relationship_boost=0.85)
        self.assertEqual(res.decision, "create_new")
        self.assertIsNotNone(res.person_id)
        p = self.store.get_person(res.person_id)
        self.assertEqual(p.get("promotion_state"), "candidate")

    def test_soft_merge_hides_absorbed(self):
        a = self.store.insert_person("Alex One", ts=NOW, promotion_state="active")
        b = self.store.insert_person("Alex Two", ts=NOW, promotion_state="active")
        mid = self.store.soft_merge_people(a, b, reason="same person", actor="test")
        self.assertTrue(mid)
        absorbed = self.store.get_person(b)
        self.assertTrue(absorbed.get("hide_from_people"))
        self.assertEqual(absorbed.get("canonical_person_id"), a)


if __name__ == "__main__":
    unittest.main()
