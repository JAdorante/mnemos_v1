"""People v3 WS-D part 2 — provisional-bind band + merge-as-training.

Design principle under test: medium-confidence matches must not mint a
twin, and must not stall forever as leave_open. Flag OFF
(QUILL_PROVISIONAL_BIND, default) is byte-identical; flag ON binds
provisionally when the best EXISTING candidate scores in [0.55, 0.80],
recording resolution_status='provisional'. Human soft_merge confirms
those rows onto the survivor and writes conclusive positive alias_rules
(the training signal). Exact / auto_resolve / reject paths untouched.
Deterministic throughout — no models, no vector index.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.events import Event, Modality
from app.services import mint_recurrence as mr
from app.services import people_pipeline as pp
from app.services import provisional_bind as pb
from app.storage import Store

NOW = 1_700_000_000.0
HOUR = 3600.0


def _flag(enabled=True, score_lo=0.55, score_hi=0.80):
    return SimpleNamespace(provisional_bind=SimpleNamespace(
        enabled=enabled, score_lo=score_lo, score_hi=score_hi))


def _recurrence(enabled=True, min_sessions=2, ttl_days=30.0):
    return SimpleNamespace(mint_recurrence=SimpleNamespace(
        enabled=enabled, min_sessions=min_sessions, ttl_days=ttl_days))


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")
        self.addCleanup(self._close)

    def _close(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _event(self, ts, source="audio.whisper", text="x"):
        modality = (Modality.AUDIO if source.startswith("audio")
                    else Modality.VISION)
        return self.store.insert(Event(time=ts, modality=modality,
                                       raw=text, source=source))

    def _resolve(self, name, ts, *, text=None, boost=0.85, event_id=None):
        text = text or f"{name} said the deck is ready"
        if event_id is None:
            event_id = self._event(ts, text=text)
        return pp.resolve_person_mention(
            name, store=self.store, event_id=event_id,
            event_source="audio.whisper", text=text,
            grammatical_role="unknown", now=ts,
            relationship_boost=boost)

    def _mention_rows(self):
        return [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM person_mentions ORDER BY mention_id").fetchall()]


# --------------------------------------------------------------------------
# Flag OFF — byte-identical
# --------------------------------------------------------------------------
class FlagOffTests(_Case):
    def test_medium_match_still_mints_today(self):
        # "Dana Whitfield" vs "Dana Marie Whitfield" scores ~0.69 — inside
        # the band, but with the flag OFF and high relevance the resolver
        # still first-sight mints (pre-band behavior).
        self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        with patch.object(pb, "settings", _flag(enabled=False)):
            res = self._resolve("Dana Whitfield", NOW, boost=0.85)
        self.assertEqual(res.decision, "create_new")
        self.assertIsNotNone(res.person_id)
        people = [p for p in self.store.list_people_embed()
                  if not p.get("canonical_person_id")]
        self.assertEqual(len(people), 2)

    def test_legacy_settings_namespace_means_off(self):
        with patch.object(pb, "settings", SimpleNamespace()):
            self.assertFalse(pb.enabled())
            self.assertIsNone(pb.maybe_bind(
                [({"id": 1}, 0.69, {})], "create_new"))


# --------------------------------------------------------------------------
# Flag ON — provisional-bind band
# --------------------------------------------------------------------------
class BandTests(_Case):
    def test_create_new_intercepted_by_band(self):
        pid = self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        with patch.object(pb, "settings", _flag()):
            res = self._resolve("Dana Whitfield", NOW, boost=0.85)
        self.assertEqual(res.decision, "provisional_bind")
        self.assertEqual(res.person_id, pid)
        rows = self._mention_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_status"], "provisional")
        self.assertEqual(rows[0]["resolved_person_id"], pid)
        # No twin minted.
        people = [p for p in self.store.list_people_embed()
                  if not p.get("canonical_person_id")]
        self.assertEqual(len(people), 1)

    def test_leave_open_upgraded_when_in_band(self):
        # Low relevance → decide_from_scores would leave_open; the band
        # still provisionally binds the medium match.
        pid = self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        with patch.object(pb, "settings", _flag()):
            res = self._resolve("Dana Whitfield", NOW, boost=0.3)
        self.assertEqual(res.decision, "provisional_bind")
        self.assertEqual(res.person_id, pid)

    def test_score_below_band_does_not_bind(self):
        # Force a tiny band so the ~0.69 jaccard score falls below lo.
        self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        with patch.object(pb, "settings",
                          _flag(score_lo=0.75, score_hi=0.80)):
            res = self._resolve("Dana Whitfield", NOW, boost=0.85)
        self.assertEqual(res.decision, "create_new")

    def test_prefix_above_band_stays_leave_open(self):
        # "Kevin" ↔ "Kevin Doyle" is prefix (~0.86) — above the band's
        # 0.80 ceiling, so the band does not fire; leave_open stands.
        self.store.insert_person("Kevin Doyle", ts=NOW)
        with patch.object(pb, "settings", _flag()):
            res = self._resolve("Kevin", NOW, boost=0.3)
        self.assertEqual(res.decision, "leave_open")
        self.assertIsNone(res.person_id)

    def test_exact_match_still_auto_resolves(self):
        pid = self.store.insert_person("Sarah Chen", ts=NOW)
        with patch.object(pb, "settings", _flag()):
            res = self._resolve("Sarah Chen", NOW)
        self.assertEqual(res.decision, "auto_resolve")
        self.assertEqual(res.person_id, pid)
        self.assertEqual(self._mention_rows()[0]["resolution_status"],
                         "resolved")

    def test_no_conclusive_alias_rule_on_provisional(self):
        pid = self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        with patch.object(pb, "settings", _flag()):
            self._resolve("Dana Whitfield", NOW)
        # Provisional must NOT write a conclusive alias_rule — merges do.
        self.assertEqual(self.store.alias_rules_for("Dana Whitfield"), [])
        # But the spelling is soft-learned via touch_person aliases.
        row = next(p for p in self.store.list_people_embed()
                   if int(p["id"]) == pid)
        aliases = [a.lower() for a in (row.get("aliases") or [])]
        self.assertIn("dana whitfield", aliases)


# --------------------------------------------------------------------------
# Compose with WS-C recurrence
# --------------------------------------------------------------------------
class RecurrenceComposeTests(_Case):
    def test_band_overrides_pending_mint(self):
        # Recurrence would pool a first-sight mint; a medium existing match
        # must provisional-bind instead of parking a twin in the pool.
        pid = self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        with patch.object(pb, "settings", _flag()), \
                patch.object(mr, "settings", _recurrence()):
            res = self._resolve("Dana Whitfield", NOW, boost=0.85)
        self.assertEqual(res.decision, "provisional_bind")
        self.assertEqual(res.person_id, pid)
        self.assertFalse(self.store.pending_mint_mentions("Dana Whitfield"))


# --------------------------------------------------------------------------
# Merge-as-training
# --------------------------------------------------------------------------
class MergeTrainTests(_Case):
    def test_merge_promotes_provisional_and_writes_alias(self):
        survivor = self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        # Mint a twin deliberately (flag off), then provisional-bind a
        # spelling onto the twin, then merge twin → survivor.
        twin = self.store.insert_person("Dana Whitfield", ts=NOW)
        eid = self._event(NOW, text="Dana Whitfield will review")
        mid = self.store.insert_person_mention(
            event_id=eid, raw_text="Dana Whitfield",
            normalized_text="dana whitfield",
            discourse_role="unknown", grammatical_role="unknown",
            observed_at=NOW, extractor_version="t", pipeline_version="t",
            resolution_status="provisional", resolved_person_id=twin,
            resolution_confidence=0.69, relationship_relevance=0.85)
        out = pb.on_person_merged(self.store, survivor, twin, NOW)
        self.assertEqual(out["promoted"], 1)
        self.assertGreaterEqual(out["aliases"], 1)
        row = dict(self.store._conn.execute(
            "SELECT resolution_status, resolved_person_id "
            "FROM person_mentions WHERE mention_id = ?", (mid,)).fetchone())
        self.assertEqual(row["resolution_status"], "resolved")
        self.assertEqual(row["resolved_person_id"], survivor)
        rules = self.store.alias_rules_for("Dana Whitfield")
        self.assertIn((survivor, "positive"),
                      [(r["person_id"], r["kind"]) for r in rules])

    def test_soft_merge_hook_confirms_provisional(self):
        survivor = self.store.insert_person("Dana Marie Whitfield", ts=NOW)
        twin = self.store.insert_person("Other Dana", ts=NOW)
        with patch.object(pb, "settings", _flag()):
            # Bind a medium match onto the twin via the live path, then
            # merge — the hook must confirm without an extra call.
            # Use a person whose jaccard to the mention sits in-band.
            self.store._conn.execute(
                "UPDATE people SET canonical_name = ? WHERE id = ?",
                ("Dana Marie Twin", twin))
            self.store._conn.commit()
        # Direct provisional row on twin (avoids score-flake on rename).
        eid = self._event(NOW)
        self.store.insert_person_mention(
            event_id=eid, raw_text="Dana Whitfield",
            normalized_text="dana whitfield",
            discourse_role="unknown", grammatical_role="unknown",
            observed_at=NOW, extractor_version="t", pipeline_version="t",
            resolution_status="provisional", resolved_person_id=twin,
            resolution_confidence=0.69, relationship_relevance=0.85)
        self.store.soft_merge_people(survivor, twin, reason="dup", ts=NOW)
        rows = self._mention_rows()
        self.assertEqual(rows[0]["resolution_status"], "resolved")
        self.assertEqual(rows[0]["resolved_person_id"], survivor)


if __name__ == "__main__":
    unittest.main()
