"""People v3 P4 (WS-C) — recurrence-gated person minting.

Design principle under test: people must recur before they exist. Flag OFF
(QUILL_MINT_RECURRENCE, default) is byte-identical first-sight minting; flag
ON parks a would-be NEW mint in the pending pool (person_mentions rows with
resolution_status='pending_mint') until the same identity has been seen in
>= 2 distinct sessions, then mints retroactively — adopting the pooled
mentions and filling typed rows so no signal is lost. Junk/banned names
never pool; binds to EXISTING people are untouched; pending rows that never
recur are archived (never deleted) after the TTL. Deterministic throughout —
no models, no vector index.
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
from app.storage import Store

NOW = 1_700_000_000.0
DAY = 86400.0
HOUR = 3600.0


def _flag(enabled=True, min_sessions=2, ttl_days=30.0):
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

    # -- helpers ----------------------------------------------------------
    def _event(self, ts, source="audio.whisper", text="x"):
        modality = (Modality.AUDIO if source.startswith("audio")
                    else Modality.VISION)
        return self.store.insert(Event(time=ts, modality=modality,
                                       raw=text, source=source))

    def _resolve(self, name, ts, *, text=None, role="unknown",
                 source="audio.whisper", boost=0.85, event_id=None,
                 attendee_priors=None):
        text = text or f"{name} said the deck is ready"
        if event_id is None:
            event_id = self._event(ts, source=source, text=text)
        res = pp.resolve_person_mention(
            name, store=self.store, event_id=event_id, event_source=source,
            text=text, grammatical_role=role, now=ts,
            relationship_boost=boost, attendee_priors=attendee_priors)
        return res, event_id

    def _sessions(self, windows):
        self.store.replace_sessions([
            SimpleNamespace(start=a, end=b, speakers=[], text="",
                            turn_ids=[], event_ids=[], n_turns=1,
                            n_utterances=1)
            for a, b in windows])

    def _pending(self, name):
        return self.store.pending_mint_mentions(name)

    def _people_named(self, name):
        return [p for p in self.store.list_people_embed()
                if p["name"].lower() == name.lower()]

    def _mention_rows(self):
        return [dict(r) for r in self.store._conn.execute(
            "SELECT * FROM person_mentions ORDER BY mention_id").fetchall()]


# --------------------------------------------------------------------------
# Flag OFF — byte-identical first-sight minting
# --------------------------------------------------------------------------
class FlagOffTests(_Case):
    def test_first_sight_mints_exactly_like_today(self):
        with patch.object(mr, "settings", _flag(enabled=False)):
            res, _ = self._resolve("Dana Whitfield", NOW)
        self.assertEqual(res.decision, "create_new")
        self.assertIsNotNone(res.person_id)
        self.assertEqual(len(self._people_named("Dana Whitfield")), 1)
        rows = self._mention_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolution_status"], "resolved")
        self.assertFalse(self._pending("Dana Whitfield"))

    def test_legacy_settings_namespace_means_off(self):
        # Older suites patch settings with namespaces that predate the WS-C
        # fields — the gate must read them as OFF, never crash.
        with patch.object(mr, "settings", SimpleNamespace()):
            self.assertFalse(mr.enabled())
            res, _ = self._resolve("Dana Whitfield", NOW)
        self.assertEqual(res.decision, "create_new")
        self.assertIsNotNone(res.person_id)


# --------------------------------------------------------------------------
# Flag ON — pending pool + recurrence gate
# --------------------------------------------------------------------------
class RecurrenceGateTests(_Case):
    def test_first_sighting_pools_instead_of_minting(self):
        self._sessions([(NOW, NOW + HOUR)])
        with patch.object(mr, "settings", _flag()):
            res, _ = self._resolve("Dana Whitfield", NOW + 60)
        self.assertEqual(res.decision, "pending_mint")
        self.assertIsNone(res.person_id)
        self.assertEqual(self._people_named("Dana Whitfield"), [])
        pool = self._pending("Dana Whitfield")
        self.assertEqual(len(pool), 1)
        self.assertEqual(pool[0]["resolution_status"], "pending_mint")

    def test_same_session_repeat_does_not_mint(self):
        self._sessions([(NOW, NOW + HOUR)])
        with patch.object(mr, "settings", _flag()):
            self._resolve("Dana Whitfield", NOW + 60)
            res, _ = self._resolve("Dana Whitfield", NOW + 600)
        self.assertEqual(res.decision, "pending_mint")
        self.assertIsNone(res.person_id)
        self.assertEqual(self._people_named("Dana Whitfield"), [])
        self.assertEqual(len(self._pending("Dana Whitfield")), 2)

    def test_second_session_mints_retroactively_and_links(self):
        self._sessions([(NOW, NOW + HOUR),
                        (NOW + 5 * HOUR, NOW + 6 * HOUR)])
        with patch.object(mr, "settings", _flag()):
            # Sighting 1: the mention is a task OWNER; the task lands with a
            # NULL owner because the person does not exist yet.
            t1 = NOW + 60
            eid1 = self._event(t1, text="Dana Whitfield will review the deck")
            fid = self.store.add_task(
                "review the deck", source_event_id=eid1,
                source_span="Dana Whitfield will review the deck",
                confidence=0.9, owner_person_id=None, extracted_at=t1)
            res1, _ = self._resolve(
                "Dana Whitfield", t1, role="owner", event_id=eid1,
                text="Dana Whitfield will review the deck")
            self.assertEqual(res1.decision, "pending_mint")
            # Sighting 2, a different session: mint + retro-adopt.
            res2, _ = self._resolve("Dana Whitfield", NOW + 5 * HOUR + 60)
        self.assertEqual(res2.decision, "create_new")
        self.assertIsNotNone(res2.person_id)
        pid = res2.person_id
        self.assertEqual(len(self._people_named("Dana Whitfield")), 1)
        # The pooled mention is adopted, not left dangling.
        self.assertFalse(self._pending("Dana Whitfield"))
        rows = self._mention_rows()
        self.assertEqual([r["resolution_status"] for r in rows],
                         ["resolved", "resolved"])
        self.assertEqual({r["resolved_person_id"] for r in rows}, {pid})
        # The typed row born from the pooled event now belongs to the person.
        task = dict(self.store._conn.execute(
            "SELECT owner_person_id FROM tasks WHERE fact_id = ?",
            (fid,)).fetchone())
        self.assertEqual(task["owner_person_id"], pid)

    def test_gap_grouping_when_no_session_rows_exist(self):
        # Live case: consolidation has not built session rows yet. Two
        # sightings within the session gap are ONE conversation; a sighting
        # hours later is a second one and mints.
        with patch.object(mr, "settings", _flag()):
            self._resolve("Dana Whitfield", NOW)
            res_same, _ = self._resolve("Dana Whitfield", NOW + 60)
            self.assertEqual(res_same.decision, "pending_mint")
            self.assertEqual(self._people_named("Dana Whitfield"), [])
            res_new, _ = self._resolve("Dana Whitfield", NOW + 2 * HOUR)
        self.assertEqual(res_new.decision, "create_new")
        self.assertIsNotNone(res_new.person_id)

    def test_invite_anchored_mint_is_exempt(self):
        # An email-backed calendar invitee IS identity evidence — the
        # recurrence gate does not apply, first sight still mints.
        with patch.object(mr, "settings", _flag()):
            res, _ = self._resolve(
                "Dana Whitfield", NOW,
                attendee_priors=[{"name": "Dana Whitfield",
                                  "email": "dana@acme.com"}])
        self.assertEqual(res.decision, "create_new")
        self.assertIsNotNone(res.person_id)
        self.assertFalse(self._pending("Dana Whitfield"))


# --------------------------------------------------------------------------
# Guards — junk / banned names never pool; existing binds untouched
# --------------------------------------------------------------------------
class GuardTests(_Case):
    def test_banned_canonical_name_never_pools(self):
        pid = self.store.insert_person("Marc Chen", ts=NOW)
        self.store.add_alias_rule(pid, "Marc Chen", "negative",
                                  created_by="test", ts=NOW)
        with patch.object(mr, "settings", _flag()):
            res, _ = self._resolve("Marc Chen", NOW)
        self.assertEqual(res.decision, "leave_open")
        self.assertIsNone(res.person_id)
        self.assertFalse(self._pending("Marc Chen"))
        self.assertEqual(len(self._people_named("Marc Chen")), 1)

    def test_junk_names_never_enter_the_pool(self):
        with patch.object(mr, "settings", _flag()):
            res, _ = self._resolve("Speaker 3", NOW)
        self.assertEqual(res.decision, "reject")
        self.assertFalse(self._pending("Speaker 3"))

    def test_single_token_names_never_enter_the_pool(self):
        with patch.object(mr, "settings", _flag()):
            res, _ = self._resolve("justin", NOW)
        self.assertEqual(res.decision, "leave_open")
        self.assertFalse(self._pending("justin"))
        self.assertFalse(self._mention_rows()[0]["resolution_status"]
                         == "pending_mint")

    def test_existing_person_bind_is_untouched(self):
        pid = self.store.insert_person("Sarah Chen", ts=NOW)
        self.store.touch_person(pid, NOW, alias="sarah")
        with patch.object(mr, "settings", _flag()):
            res, _ = self._resolve("Sarah Chen", NOW + 60)
        self.assertEqual(res.decision, "auto_resolve")
        self.assertEqual(res.person_id, pid)
        self.assertFalse(self._pending("Sarah Chen"))


# --------------------------------------------------------------------------
# TTL expiry — archive, never delete
# --------------------------------------------------------------------------
class ExpiryTests(_Case):
    def test_stale_pending_rows_archive_and_stop_counting(self):
        with patch.object(mr, "settings", _flag(ttl_days=30.0)):
            self._resolve("Dana Whitfield", NOW - 40 * DAY)
            self.assertEqual(len(self._pending("Dana Whitfield")), 1)
            # A sighting 40 days later: the lazy sweep archives the stale
            # row FIRST, so this counts as one fresh session — pool, no mint.
            res, _ = self._resolve("Dana Whitfield", NOW)
        self.assertEqual(res.decision, "pending_mint")
        self.assertEqual(self._people_named("Dana Whitfield"), [])
        rows = self._mention_rows()
        self.assertEqual([r["resolution_status"] for r in rows],
                         ["pending_expired", "pending_mint"])
        # Archived, not deleted: the evidence row is still on the ledger.
        self.assertEqual(len(rows), 2)

    def test_sweep_is_idempotent(self):
        with patch.object(mr, "settings", _flag(ttl_days=30.0)):
            self._resolve("Dana Whitfield", NOW - 40 * DAY)
            self.assertEqual(mr.sweep_expired(self.store, now=NOW), 1)
            self.assertEqual(mr.sweep_expired(self.store, now=NOW), 0)


# --------------------------------------------------------------------------
# Composition with P3 voice-track escrow
# --------------------------------------------------------------------------
class EscrowComposeTests(_Case):
    def test_escrowed_unnamed_speech_never_reaches_the_pool(self):
        # An unbound diarization label used as a party name is escrow's
        # territory: the extractor short-circuits before resolution, so no
        # mention row — pending or otherwise — is ever written.
        from app.services import people_escrow
        from app.services.extractor import Extractor
        ex = Extractor(store=self.store)
        eid = self._event(NOW, text="I'll send the deck")
        esc = SimpleNamespace(people_escrow=SimpleNamespace(enabled=True))
        with patch.object(people_escrow, "settings", esc), \
                patch.object(mr, "settings", _flag()):
            pid = ex._resolve_person_id(
                "Speaker 3", NOW, event_id=eid,
                event_source="audio.whisper", text="I'll send the deck")
        self.assertIsNone(pid)
        self.assertEqual(self._mention_rows(), [])

    def test_labeled_speaker_with_new_name_pools_normally(self):
        # Once a speaker is LABELED with a real (but new) name, that name is
        # a named-but-new mention — exactly what the recurrence gate covers.
        from app.services import people_escrow
        esc = SimpleNamespace(people_escrow=SimpleNamespace(enabled=True))
        with patch.object(people_escrow, "settings", esc), \
                patch.object(mr, "settings", _flag()):
            res, _ = self._resolve("Dana Whitfield", NOW, role="speaker")
        self.assertEqual(res.decision, "pending_mint")
        self.assertEqual(len(self._pending("Dana Whitfield")), 1)


if __name__ == "__main__":
    unittest.main()
