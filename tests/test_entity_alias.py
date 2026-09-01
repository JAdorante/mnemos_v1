"""Alias-aware entity resolution (WS1c) — bind-only, never mints.

Resolution precedence (exact > confirmed alias > normalized > embedding
proposal), separator/case normalization, the recurrence gate on cosine
proposals (never auto-confirm below N distinct days), and the wiring that
feeds confirmed aliases into graph._entity_patterns via all_entities().
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.services import entity_alias as ea
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86_400.0


class NormalizeTests(unittest.TestCase):
    def test_separators_collapse(self):
        self.assertEqual(ea.normalize("nexus_v1"), "nexus v1")
        self.assertEqual(ea.normalize("Capital-Connect"), "capital connect")
        self.assertEqual(ea.normalize("  Nexus   V1 "), "nexus v1")
        self.assertEqual(ea.normalize("a/b\\c.d"), "a b c d")

    def test_empty(self):
        self.assertEqual(ea.normalize(""), "")
        self.assertEqual(ea.normalize(None), "")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.nexus = self.store.resolve_entity("Nexus V1", "project", ts=NOW)
        self.cap = self.store.resolve_entity("Capital Connect", "project",
                                             ts=NOW)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_exact_wins(self):
        self.assertEqual(
            ea.resolve("Nexus V1", store=self.store, ts=NOW), self.nexus)

    def test_normalized_match_binds_and_records_alias(self):
        eid = ea.resolve("nexus_v1", store=self.store, ts=NOW)
        self.assertEqual(eid, self.nexus)
        rows = self.store.list_entity_aliases(entity_id=self.nexus)
        self.assertTrue(any(r["alias"] == "nexus_v1" and r["confirmed"]
                            for r in rows))

    def test_confirmed_alias_resolves(self):
        self.store.upsert_entity_alias(
            self.cap, "capconnect", "capconnect", source="user",
            confirmed=True, ts=NOW, day="2026-08-20")
        self.assertEqual(
            ea.resolve("capconnect", store=self.store, ts=NOW), self.cap)

    def test_unconfirmed_alias_never_resolves(self):
        self.store.upsert_entity_alias(
            self.cap, "ccx", "ccx", source="embedding",
            confirmed=False, ts=NOW, day="2026-08-20")
        with patch.object(ea, "_embed", return_value=None):
            self.assertIsNone(ea.resolve("ccx", store=self.store, ts=NOW))

    def test_never_mints(self):
        before = len(self.store.all_entities())
        with patch.object(ea, "_embed", return_value=None):
            self.assertIsNone(
                ea.resolve("totally-new-thing", store=self.store, ts=NOW))
        self.assertEqual(len(self.store.all_entities()), before)

    def test_junk_names_rejected(self):
        with patch.object(ea, "_embed", return_value=None):
            self.assertIsNone(ea.resolve("", store=self.store))
            self.assertIsNone(ea.resolve("x", store=self.store))

    def _fake_embed(self):
        """Deterministic 'embedding': close for capital-connect vs Capital
        Connect, orthogonal for everything else."""
        vecs = {
            "capital connect": np.array([1.0, 0.0]),
            "capconn": np.array([0.99, 0.14]),   # cos ≈ 0.99 vs above
            "nexus v1": np.array([0.0, 1.0]),
        }

        def fake(text):
            return vecs.get(text)
        return patch.object(ea, "_embed", side_effect=fake)

    def test_cosine_proposes_but_does_not_bind(self):
        with self._fake_embed():
            eid = ea.resolve("capconn", store=self.store, ts=NOW)
        self.assertIsNone(eid, "a first-sight cosine match must not bind")
        rows = self.store.list_entity_aliases(entity_id=self.cap,
                                              confirmed=False)
        self.assertTrue(any(r["alias"] == "capconn" for r in rows))

    def test_recurrence_autoconfirms_across_distinct_days(self):
        with self._fake_embed():
            self.assertIsNone(ea.resolve("capconn", store=self.store, ts=NOW))
            # Same day again — still below the distinct-day bar.
            self.assertIsNone(
                ea.resolve("capconn", store=self.store, ts=NOW + 60))
            self.assertIsNone(
                ea.resolve("capconn", store=self.store, ts=NOW + DAY))
            # Third distinct day → auto-confirm → binds.
            eid = ea.resolve("capconn", store=self.store, ts=NOW + 2 * DAY)
        self.assertEqual(eid, self.cap)
        rows = self.store.list_entity_aliases(entity_id=self.cap,
                                              confirmed=True)
        self.assertTrue(any(r["alias"] == "capconn" for r in rows))
        # And now it resolves via the confirmed-alias path, embeddings absent.
        with patch.object(ea, "_embed", return_value=None):
            self.assertEqual(
                ea.resolve("capconn", store=self.store, ts=NOW + 3 * DAY),
                self.cap)

    def test_human_confirm_unlocks_resolution(self):
        with self._fake_embed():
            ea.resolve("capconn", store=self.store, ts=NOW)
        row = self.store.list_entity_aliases(entity_id=self.cap,
                                             confirmed=False)[0]
        self.store.confirm_entity_alias(row["id"], True)
        with patch.object(ea, "_embed", return_value=None):
            self.assertEqual(
                ea.resolve("capconn", store=self.store, ts=NOW), self.cap)

    def test_confirmed_alias_feeds_entity_patterns(self):
        from app.services.graph import _entity_patterns
        self.store.upsert_entity_alias(
            self.nexus, "nexus_v1", "nexus v1", source="user",
            confirmed=True, ts=NOW, day="2026-08-20")
        ent = next(e for e in self.store.all_entities()
                   if e["id"] == self.nexus)
        self.assertIn("nexus_v1", ent["aliases"])
        pats = _entity_patterns(ent)
        self.assertTrue(any(p.search("storage.py - nexus_v1 - Cursor")
                            for p in pats))

    def test_proposals_listing_names_the_entity(self):
        self.store.upsert_entity_alias(
            self.cap, "ccx-proj", "ccx proj", source="embedding",
            confirmed=False, ts=NOW, day="2026-08-20")
        rows = ea.proposals(self.store)
        self.assertEqual(rows[0]["entity_name"], "Capital Connect")
        self.assertFalse(rows[0]["confirmed"])


if __name__ == "__main__":
    unittest.main()
