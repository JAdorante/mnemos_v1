"""Entity model, Phase 1: candidates before nodes.

A name the extractor reads for the first time is a sighting in the ledger
(`entity_mentions`), not an entity. It becomes one when it recurs (two
sessions, or two kinds of source), or at once when the user typed it. Names
that read as ids ("c4") always need recurrence; lowercase noun phrases ("ski
resort") are only ever promoted on what the user typed, twice. A forgotten
name is never written down. Relations asserted about a name that is still
waiting are pooled and written when it becomes a node.

Every test runs against a real Store on a temp db and the real extractor
persist path; nothing under test is mocked.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`: a module
# first imported under that patch would keep the stand-in for good.
import app.api.routes  # noqa: F401
import app.services.extractor  # noqa: F401
import app.services.people_pipeline  # noqa: F401
from app.events import Event, Modality
from app.services import entity_candidates as cand
from app.services import entity_typing
from app.services.kg_keys import entity_name_key
from app.services.resolution import EntityMatch, Resolver
from app.storage import Store

NOW = 1_700_000_000.0
HOUR = 3600.0
DAY = 86400.0

CHAT = "chat.user"              # direct_message: the user's own typed turn
MAIL = "google.mail"            # connector_mail
DOC = "chat.attach"             # user_authored_document
NOTE = "assistant.claude"       # assistant_capture: may bind, never mint
SCREEN = "desktop.screen"       # screen OCR: may bind, never mint


def _mk(tmp: str) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_cand_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)

    def pending(self, name: str) -> list[dict]:
        return self.store.entity_mentions(name_key=entity_name_key(name),
                                          status="pending")

    def bound(self, name: str) -> list[dict]:
        return self.store.entity_mentions(name_key=entity_name_key(name),
                                          status="bound")

    def edges(self) -> set[tuple]:
        with self.store._lock:
            return {tuple(r) for r in self.store._conn.execute(
                "SELECT subj_type, subj_id, predicate, obj_type, obj_id "
                "FROM relations").fetchall()}


class _ExtractCase(_StoreCase):
    """The extractor's own persist path, against the temp store."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {
            "QUILL_HEADS": "off", "QUILL_USAGE_LEDGER": "0",
            "QUILL_PEOPLE_V2": "1", "QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_CANDIDATE_SESSIONS",
                    "QUILL_ENTITY_CANDIDATE_TTL_DAYS",
                    "QUILL_ENTITY_CANDIDATE_SESSION_GAP_S",
                    "QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS",
                    "QUILL_ENTITY_CODE_KIND_MIN_CONF"):
            os.environ.pop(var, None)
        from app.services import resolution
        for p in (patch.object(resolution, "resolver",
                               Resolver(store=self.store)),
                  patch("app.storage.get_store", return_value=self.store),
                  patch("app.services.extractor._index_fact",
                        lambda *a, **k: None),
                  patch("app.services.fact_gate._similar_active",
                        return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        from app.services.extractor import Extractor
        self.ex = Extractor(store=self.store)

    def event(self, source: str, text: str, ts: float, *,
              meta: dict | None = None) -> int:
        modality = (Modality.AUDIO if source.startswith("audio")
                    else Modality.TEXT)
        return int(self.store.insert(Event(
            time=ts, modality=modality, raw=text, source=source,
            meta=dict(meta or {}))))

    def see(self, name: str, kind: str | None = "org", *, source: str,
            ts: float, text: str | None = None, relations=(),
            event_id: int | None = None, now: float | None = None,
            meta: dict | None = None) -> int:
        """One event from `source` in which the model read `name`."""
        text = text if text is not None else f"A note that mentions {name}."
        ev = event_id if event_id is not None \
            else self.event(source, text, ts, meta=meta)
        facts = {"entities": [{"name": name, "kind": kind, "confidence": 0.8,
                               "source_span": text}],
                 "relations": list(relations)}
        self.ex._persist_entities(facts, ev, now if now is not None else ts,
                                  event_source=source, text=text)
        return ev

    def hear(self, name: str, kind: str | None = "org", *, ts: float,
             source: str = "audio.whisper") -> int:
        """Speech: the extractor passes no event source for it."""
        text = f"We talked about {name} for a while."
        ev = self.event(source, text, ts)
        facts = {"entities": [{"name": name, "kind": kind, "confidence": 0.8,
                               "source_span": text}], "relations": []}
        self.ex._persist_entities(facts, ev, ts, turn_text=text)
        return ev

    def entity(self, name: str) -> dict | None:
        eid = self.store.find_entity_exact(name)
        return self.store.get_entity(eid) if eid else None


# --------------------------------------------------------------- shapes --
class NameShapeTests(unittest.TestCase):
    def test_code_like_names(self):
        for name in ("c4", "r2", "v2", "9bc12da", "893453a", "Phase 1",
                     "Stage 0", "2026-11-14", "Nov 14", "November 2026",
                     "ENG-482", "v1.2.3", "42", "wf_079a6cb8-b9e",
                     "6f1c2d3e-0a1b-4c2d-8e9f-0123456789ab",
                     # series ids with a letter after the hyphen
                     "WS-A", "WS-G", "QA-B1", "PH-2C", "OPT-AB"):
            self.assertTrue(entity_typing.code_like(name), name)

    def test_names_are_not_code_like(self):
        for name in ("Northwind", "Dell Capital", "Postgres", "Windows 11",
                     "GPT-4", "defaced", "mint_recurrence", "app.config",
                     "ski resort", "Atlas", "", None,
                     # hyphenated names, initials, a model number
                     "X-ray", "Coca-Cola", "J-P", "A-L", "Rolls-Royce",
                     "ws-a", "WS-Alpha", "MI-6", "Wal-Mart", "T-Mobile"):
            self.assertFalse(entity_typing.code_like(name), name)

    def test_common_noun_like_names(self):
        for name in ("funeral homes", "ski resort", "industrial laundry",
                     "follow-up", "pilot"):
            self.assertTrue(entity_typing.common_noun_like(name), name)

    def test_names_with_a_capital_digit_dot_or_underscore_are_not_nouns(self):
        for name in ("Ski Resort", "Northwind", "mint_recurrence",
                     "app.config", "qwen2.5", "c4", "iPhone", "東京", "",
                     None):
            self.assertFalse(entity_typing.common_noun_like(name), name)

    def test_plausibility_gate_is_unchanged_for_lowercase_names(self):
        # The candidate gate owns the common-noun rule; the plausibility
        # gate still lets lowercase and snake_case names through.
        from app.services.name_quality import is_plausible_entity
        for name in ("ski resort", "mint_recurrence", "postgres"):
            self.assertTrue(is_plausible_entity(name), name)


# ---------------------------------------------------------------- store --
class LedgerStoreTests(_StoreCase):
    def test_tables_columns_and_indexes(self):
        with self.store._lock:
            cols = {r["name"] for r in self.store._conn.execute(
                "PRAGMA table_info(entity_mentions)").fetchall()}
            rel_cols = {r["name"] for r in self.store._conn.execute(
                "PRAGMA table_info(entity_pending_relations)").fetchall()}
            idx = {r["name"]: r for r in self.store._conn.execute(
                "PRAGMA index_list(entity_mentions)").fetchall()}
        for col in ("id", "name", "name_key", "proposed_kind", "confidence",
                    "source_class", "event_source", "event_id", "span",
                    "filing_project", "parent", "observed_at", "entity_id",
                    "status", "weak"):
            self.assertIn(col, cols)
        for col in ("subj_kind", "subj_name", "subj_key", "predicate",
                    "obj_kind", "obj_name", "obj_key", "confidence", "quote",
                    "source_class", "source_event_id", "ts", "status"):
            self.assertIn(col, rel_cols)
        self.assertTrue(idx["idx_entity_mentions_event"]["unique"])
        for name in ("idx_entity_mentions_key", "idx_entity_mentions_entity",
                     "idx_entity_mentions_status"):
            self.assertIn(name, idx)

    def test_one_row_per_name_and_event(self):
        a = self.store.record_entity_mention(
            "Northwind", proposed_kind="org", confidence=0.8,
            source_class="connector_mail", event_source=MAIL, event_id=7,
            span="Northwind sent the invoice", observed_at=NOW)
        # The same event again, as a relation endpoint: no kind, no span.
        b = self.store.record_entity_mention(
            "NORTHWIND", source_class="connector_mail", event_source=MAIL,
            event_id=7, observed_at=NOW + 5)
        self.assertEqual(a, b)
        rows = self.store.entity_mentions()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["name_key"], row["proposed_kind"], row["span"],
                          row["status"], row["entity_id"], row["observed_at"]),
                         ("northwind", "org", "Northwind sent the invoice",
                          "pending", None, NOW))
        # Another event is another row; a different name in the same event
        # is another row.
        self.store.record_entity_mention("Northwind", event_id=8,
                                         observed_at=NOW)
        self.store.record_entity_mention("Atlas", event_id=7, observed_at=NOW)
        self.assertEqual(len(self.store.entity_mentions()), 3)
        self.assertEqual(self.store.entity_mention_counts(), {"pending": 3})

    def test_unique_index_refuses_a_second_row_for_one_event(self):
        self.store.record_entity_mention("Northwind", event_id=7,
                                         observed_at=NOW)
        with self.store._lock:
            with self.assertRaises(sqlite3.IntegrityError):
                self.store._conn.execute(
                    "INSERT INTO entity_mentions (name, name_key, event_id, "
                    "status) VALUES ('northwind', 'northwind', 7, 'pending')")
            self.store._conn.rollback()

    def test_a_bound_row_never_goes_back_to_pending(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW)
        self.store.record_entity_mention("Northwind", event_id=7,
                                         entity_id=eid, observed_at=NOW)
        self.store.record_entity_mention("Northwind", event_id=7,
                                         observed_at=NOW)
        row = self.store.entity_mentions()[0]
        self.assertEqual((row["status"], row["entity_id"]), ("bound", eid))

    def test_sighting_without_an_event_always_adds_a_row(self):
        for _ in range(2):
            self.store.record_entity_mention("Northwind", observed_at=NOW)
        self.assertEqual(len(self.store.entity_mentions()), 2)

    def test_names_without_a_key_are_not_recorded(self):
        self.assertEqual(self.store.record_entity_mention(""), 0)
        self.assertEqual(self.store.record_entity_mention("   "), 0)
        self.assertEqual(self.store.entity_mentions(), [])

    def test_pooled_relation_is_stored_once_per_event(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        kw = dict(subj_kind="entity", subj_id=atlas, subj_name="Atlas",
                  predicate="uses", obj_kind="entity", obj_name="Northwind",
                  source_event_id=7, ts=NOW)
        first = self.store.add_pending_entity_relation(**kw)
        self.assertTrue(first)
        self.assertEqual(self.store.add_pending_entity_relation(**kw), first)
        self.assertEqual(len(self.store.pending_entity_relations()), 1)
        # Both endpoints known: nothing to pool.
        self.assertEqual(self.store.add_pending_entity_relation(
            subj_kind="entity", subj_id=atlas, subj_name="Atlas",
            predicate="uses", obj_kind="entity", obj_id=atlas,
            obj_name="Atlas"), 0)

    def test_ledger_survives_a_reopen_and_migration_is_idempotent(self):
        self.store.record_entity_mention("Northwind", event_id=7,
                                         proposed_kind="org", observed_at=NOW)
        self.store.close()
        again = _mk(self.tmp)
        try:
            again._migrate_entity_model()
            rows = again.entity_mentions()
            self.assertEqual([(r["name"], r["proposed_kind"]) for r in rows],
                             [("Northwind", "org")])
        finally:
            again.close()

    def test_readonly_store_from_before_the_tables_reads_as_empty(self):
        self.store.close()
        raw = sqlite3.connect(Path(self.tmp) / "t.db")
        raw.execute("DROP TABLE entity_mentions")
        raw.execute("DROP TABLE entity_pending_relations")
        raw.commit()
        raw.close()
        ro = Store(db_path=Path(self.tmp) / "t.db",
                   audio_dir=Path(self.tmp) / "audio", readonly=True)
        try:
            self.assertEqual(ro.entity_mentions(), [])
            self.assertEqual(ro.entity_mention_counts(), {})
            self.assertEqual(ro.pending_entity_relations(), [])
            self.assertEqual(cand.list_candidates(ro), [])
        finally:
            ro.close()


# -------------------------------------------------------------- resolver --
class ResolverSplitTests(_StoreCase):
    def setUp(self):
        super().setUp()
        self.resolver = Resolver(store=self.store)

    def test_match_is_read_only(self):
        m = self.resolver.match_entity("Northwind", "org")
        self.assertIsInstance(m, EntityMatch)
        self.assertEqual((m.status, m.entity_id, m.forgotten),
                         ("new", 0, False))
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_match_finds_an_existing_row(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW)
        m = self.resolver.match_entity("northwind")
        self.assertEqual((m.status, m.entity_id), ("existing", eid))

    def test_match_rejects_what_can_never_be_an_entity(self):
        for name in ("", "   ", "x", "app/config.py", "QUILL_HEADS"):
            self.assertEqual(self.resolver.match_entity(name).status,
                             "rejected", name)
        # A person-shaped name under a project label belongs in people when
        # the seat knows that person (fix pass B: the shape alone is no
        # evidence; see test_entity_fixpass_b).
        self.store.insert_person("Jordan Lee", ts=NOW)
        m = self.resolver.match_entity("Jordan Lee", "project")
        self.assertEqual((m.status, m.via, m.person_shaped),
                         ("rejected", "not_entity", True))
        self.assertEqual(self.resolver.mint_entity(m, "project", ts=NOW), 0)
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_match_marks_a_forgotten_name(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        m = self.resolver.match_entity("Dell Capital", "org")
        self.assertEqual((m.status, m.forgotten), ("new", True))
        self.assertEqual(self.resolver.mint_entity(m, "org", ts=NOW), 0)

    def test_mint_creates_the_row_the_match_found_missing(self):
        m = self.resolver.match_entity("Northwind", "org")
        eid = self.resolver.mint_entity(m, "org", ts=NOW, kind_source="model",
                                        kind_confidence=0.7)
        row = self.store.get_entity(eid)
        self.assertEqual((row["name"], row["kind"], row["kind_source"]),
                         ("Northwind", "org", "model"))

    def test_mint_on_an_existing_match_touches_it(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW,
                                        kind_source="model")
        m = self.resolver.match_entity("Northwind")
        self.assertEqual(self.resolver.mint_entity(
            m, "tool", ts=NOW + 50, kind_source="rule"), eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"], row["last_seen"]),
                         ("tool", "rule", NOW + 50))

    def test_resolve_entity_is_still_match_then_mint(self):
        a = self.resolver.resolve_entity("Northwind", "org", ts=NOW,
                                         kind_source="model")
        self.assertTrue(a)
        self.assertEqual(self.resolver.resolve_entity("northwind", ts=NOW), a)
        self.assertEqual(
            self.resolver.resolve_entity("app/config.py", ts=NOW), 0)
        self.assertEqual(len(self.store.all_entities()), 1)


# ------------------------------------------------------------- promotion --
class RecurrenceTests(_ExtractCase):
    def test_one_sighting_is_a_candidate_not_an_entity(self):
        ev = self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.all_entities(include_hidden=True), [])
        rows = self.pending("Northwind")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(
            (row["name"], row["proposed_kind"], row["source_class"],
             row["event_source"], row["event_id"], row["observed_at"],
             row["entity_id"], row["weak"], row["typed"]),
            ("Northwind", "org", "connector_mail", MAIL, ev, NOW, None, 0, 0))
        self.assertIn("Northwind", row["span"])
        v = cand.evaluate(self.store, "northwind")
        self.assertEqual((v.promote, v.reason, v.mentions, v.sessions,
                          v.source_classes), (False, "waiting", 1, 1, 1))

    def test_two_sessions_promote(self):
        first = self.see("Northwind", "org", source=MAIL, ts=NOW)
        second = self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        row = self.entity("Northwind")
        self.assertIsNotNone(row)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        # Both sightings now belong to the entity; nothing is left pending.
        self.assertEqual(self.pending("Northwind"), [])
        self.assertEqual(
            sorted((m["event_id"], m["entity_id"])
                   for m in self.bound("Northwind")),
            sorted([(first, row["id"]), (second, row["id"])]))
        self.assertEqual([e["name"] for e in self.store.all_entities()],
                         ["Northwind"])

    def test_same_session_twice_does_not_promote(self):
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 30)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        v = cand.evaluate(self.store, "northwind")
        self.assertEqual((v.mentions, v.sessions, v.source_classes, v.promote),
                         (2, 1, 1, False))

    def test_a_sessions_row_makes_two_distant_sightings_one_session(self):
        # One long meeting: its session row covers both sightings, so they
        # are one session however far apart they are.
        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO sessions (start, end, text) VALUES (?, ?, ?)",
                (NOW - 60, NOW + 3 * HOUR, "long meeting"))
            self.store._conn.commit()
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(cand.evaluate(self.store, "northwind").sessions, 1)

    def test_two_source_classes_promote_within_one_session(self):
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.see("Northwind", "org", source=DOC, ts=NOW + 30)
        row = self.entity("Northwind")
        self.assertIsNotNone(row)
        self.assertEqual(
            {m["source_class"] for m in self.bound("Northwind")},
            {"connector_mail", "user_authored_document"})

    def test_speech_is_recorded_under_its_own_source_class(self):
        # The extractor passes no event source for speech; the ledger reads
        # it off the event, so speech plus mail is two source classes.
        self.hear("Northwind", ts=NOW)
        row = self.pending("Northwind")[0]
        self.assertEqual((row["source_class"], row["event_source"]),
                         ("private_conversation", "audio.whisper"))
        self.see("Northwind", "org", source=MAIL, ts=NOW + 30)
        self.assertIsNotNone(self.entity("Northwind"))

    def test_the_same_event_extracted_twice_counts_once(self):
        ev = self.see("Northwind", "org", source=MAIL, ts=NOW)
        # Re-extraction, hours later by the clock: same event, same row.
        self.see("Northwind", "org", source=MAIL, ts=NOW, event_id=ev,
                 now=NOW + 5 * HOUR)
        self.see("northwind", "tool", source=MAIL, ts=NOW, event_id=ev,
                 now=NOW + 9 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        rows = self.pending("Northwind")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["observed_at"], NOW)
        self.assertEqual(cand.evaluate(self.store, "northwind").sessions, 1)

    def test_the_sessions_threshold_is_read_from_the_environment(self):
        with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATE_SESSIONS": "3"}):
            self.see("Northwind", "org", source=MAIL, ts=NOW)
            self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
            self.assertIsNone(self.store.find_entity_exact("Northwind"))
            self.see("Northwind", "org", source=MAIL, ts=NOW + 4 * HOUR)
            self.assertIsNotNone(self.store.find_entity_exact("Northwind"))

    def test_kind_comes_from_the_sightings(self):
        self.see("Northwind", "tool", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Zzz"))
        # One vote each at equal confidence: the later one wins the tie.
        self.assertEqual(self.entity("Northwind")["kind"], "org")
        self.see("Halcyon", "tool", source=MAIL, ts=NOW)
        self.see("Halcyon", "tool", source=MAIL, ts=NOW + 2 * HOUR)
        self.see("Halcyon", "org", source=MAIL, ts=NOW + 4 * HOUR)
        self.assertEqual(self.entity("Halcyon")["kind"], "tool")

    def test_a_project_read_off_a_document_is_an_idea(self):
        self.see("EU CBAM", "project", source=DOC, ts=NOW)
        self.see("EU CBAM", "project", source=DOC, ts=NOW + 2 * HOUR)
        row = self.entity("EU CBAM")
        self.assertEqual(row["kind"], "idea")
        # The ledger keeps what the model said.
        self.assertEqual({m["proposed_kind"] for m in self.bound("EU CBAM")},
                         {"project"})

    def test_other_spellings_become_aliases(self):
        self.see("Dell Capital", "org", source=MAIL, ts=NOW)
        self.see("Dell-Capital", "org", source=MAIL, ts=NOW + 2 * HOUR)
        rows = self.store.all_entities()
        self.assertEqual(len(rows), 1)
        self.assertEqual(
            sorted(a.lower() for a in rows[0]["aliases"]),
            ["dell capital", "dell-capital"])

    def test_an_existing_entity_is_bound_and_touched(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW,
                                        kind_source="filing")
        ev = self.see("Northwind", "tool", source=MAIL, ts=NOW + HOUR)
        row = self.store.get_entity(eid)
        # Touched; a model kind never outranks a filing.
        self.assertEqual((row["last_seen"], row["kind"], row["kind_source"]),
                         (NOW + HOUR, "org", "filing"))
        self.assertEqual(self.pending("Northwind"), [])
        m = self.bound("Northwind")
        self.assertEqual([(x["event_id"], x["entity_id"], x["proposed_kind"])
                          for x in m], [(ev, eid, "tool")])

    def test_a_rejected_name_writes_nothing(self):
        for name in ("app/config.py", "QUILL_HEADS", "x"):
            self.see(name, "org", source=MAIL, ts=NOW)
            self.see(name, "org", source=CHAT, ts=NOW + 2 * HOUR,
                     text=f"look at {name}")
        self.assertEqual(self.store.entity_mentions(), [])
        self.assertEqual(self.store.all_entities(include_hidden=True), [])


class BindOnlyTests(_ExtractCase):
    def test_bind_only_sources_never_promote_alone(self):
        self.see("Northwind", "org", source=NOTE, ts=NOW)
        self.see("Northwind", "org", source=SCREEN, ts=NOW + 2 * HOUR)
        self.see("Northwind", "org", source=NOTE, ts=NOW + 4 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.all_entities(include_hidden=True), [])
        rows = self.pending("Northwind")
        self.assertEqual(len(rows), 3)
        self.assertFalse(any(cand.mint_eligible(r) for r in rows))
        v = cand.evaluate(self.store, "northwind")
        self.assertEqual((v.promote, v.reason), (False, "bind_only"))
        # Decision D5: bind-only sightings count as no session and no
        # source class at all.
        self.assertEqual((v.mentions, v.sessions, v.source_classes),
                         (3, 0, 0))

    def test_bind_only_sightings_never_count_toward_promotion(self):
        # Decision D5 (was: a note plus a mail sighting was two source
        # classes and promoted). The note is evidence for the kind, not for
        # existence; the mail alone is one session of one class.
        note = self.see("Northwind", "org", source=NOTE, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        mail = self.see("Northwind", "org", source=MAIL, ts=NOW + 30)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        v = cand.evaluate(self.store, "northwind")
        self.assertEqual((v.mentions, v.sessions, v.source_classes,
                          v.reason), (2, 1, 1, "waiting"))
        # A second eligible session promotes, and the note's sighting is
        # adopted with the others.
        later = self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        row = self.entity("Northwind")
        self.assertIsNotNone(row)
        self.assertEqual(
            sorted(m["event_id"] for m in self.bound("Northwind")),
            sorted([note, mail, later]))

    def test_bind_only_source_binds_an_existing_entity(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW)
        ev = self.see("Northwind", "tool", source=NOTE, ts=NOW + HOUR)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["last_seen"]), ("org", NOW + HOUR))
        self.assertEqual([(m["event_id"], m["entity_id"], m["source_class"])
                          for m in self.bound("Northwind")],
                         [(ev, eid, "assistant_capture")])

    def test_screen_text_is_not_mint_eligible_whatever_its_class(self):
        self.see("Northwind", "org", source=SCREEN, ts=NOW)
        row = self.pending("Northwind")[0]
        self.assertEqual(row["event_source"], SCREEN)
        self.assertFalse(cand.mint_eligible(row))


class TypedTests(_ExtractCase):
    def test_a_name_the_user_typed_is_minted_at_first_sight(self):
        ev = self.see("Northwind", "org", source=CHAT, ts=NOW,
                      text="We signed Northwind this morning")
        row = self.entity("Northwind")
        self.assertIsNotNone(row)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        m = self.bound("Northwind")
        self.assertEqual([(x["event_id"], x["entity_id"], x["typed"],
                           x["source_class"]) for x in m],
                         [(ev, row["id"], 1, "direct_message")])

    def test_a_name_the_model_made_up_in_a_typed_turn_is_not_typed(self):
        # The model returned a name that is nowhere in what the user wrote.
        self.see("Northwind", "org", source=CHAT, ts=NOW,
                 text="remind me to call the bank tomorrow")
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        row = self.pending("Northwind")[0]
        self.assertEqual(row["typed"], 0)
        self.assertEqual(cand.evaluate(self.store, "northwind").reason,
                         "waiting")

    def test_a_chat_reply_is_not_the_user_typing(self):
        self.see("Northwind", "org", source="chat.assistant", ts=NOW,
                 text="Northwind is a vendor you mentioned")
        self.assertIsNone(self.store.find_entity_exact("Northwind"))

    def test_code_like_name_needs_recurrence_even_typed(self):
        for name in ("c4", "9bc12da", "Phase 1"):
            self.see(name, "project", source=CHAT, ts=NOW,
                     text=f"I pushed {name} today")
            self.assertIsNone(self.store.find_entity_exact(name), name)
            v = cand.evaluate(self.store, entity_name_key(name))
            self.assertEqual((v.promote, v.reason, v.typed, v.code_like),
                             (False, "code_like", True, True), name)
        # The same session again changes nothing; another session does.
        self.see("c4", "project", source=CHAT, ts=NOW + 60,
                 text="c4 is nearly done")
        self.assertIsNone(self.store.find_entity_exact("c4"))
        self.see("c4", "project", source=CHAT, ts=NOW + 2 * HOUR,
                 text="c4 is done")
        self.assertIsNotNone(self.store.find_entity_exact("c4"))
        self.assertIsNone(self.store.find_entity_exact("9bc12da"))

    def test_common_noun_is_never_promoted_from_what_was_not_typed(self):
        self.see("ski resort", "place", source=MAIL, ts=NOW)
        self.see("ski resort", "place", source=MAIL, ts=NOW + 2 * HOUR)
        self.see("ski resort", "place", source=DOC, ts=NOW + 4 * HOUR)
        self.hear("ski resort", "place", ts=NOW + 6 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("ski resort"))
        rows = self.pending("ski resort")
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(r["weak"] for r in rows))
        v = cand.evaluate(self.store, "ski resort")
        self.assertEqual((v.promote, v.reason, v.weak), (False, "weak", True))
        self.assertGreaterEqual(v.sessions, 2)
        self.assertGreaterEqual(v.source_classes, 2)

    def test_common_noun_typed_once_waits_and_typed_twice_promotes(self):
        self.see("ski resort", "place", source=CHAT, ts=NOW,
                 text="the ski resort deal is still open")
        self.assertIsNone(self.store.find_entity_exact("ski resort"))
        # Same session: still one.
        self.see("ski resort", "place", source=CHAT, ts=NOW + 60,
                 text="call the ski resort back")
        self.assertIsNone(self.store.find_entity_exact("ski resort"))
        # A non-typed sighting in another session does not make it two.
        self.see("ski resort", "place", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("ski resort"))
        self.see("ski resort", "place", source=CHAT, ts=NOW + 4 * HOUR,
                 text="the ski resort signed")
        row = self.entity("ski resort")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "place")
        self.assertEqual(self.pending("ski resort"), [])

    def test_a_name_written_as_a_name_is_not_weak(self):
        self.see("ski resort", "place", source=MAIL, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("ski resort"))
        self.see("Ski Resort", "place", source=MAIL, ts=NOW + 2 * HOUR)
        row = self.entity("Ski Resort")
        self.assertIsNotNone(row)
        self.assertEqual(row["name"], "Ski Resort")

    def test_snake_case_and_dotted_names_are_ordinary_candidates(self):
        for name in ("mint_recurrence", "app.config"):
            self.see(name, "tool", source=MAIL, ts=NOW)
            self.assertFalse(self.pending(name)[0]["weak"], name)
            self.see(name, "tool", source=MAIL, ts=NOW + 2 * HOUR)
            self.assertIsNotNone(self.store.find_entity_exact(name), name)


class ForgottenNameTests(_ExtractCase):
    def test_a_forgotten_name_is_never_written_to_the_ledger(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        for i, source in enumerate((MAIL, DOC, CHAT, NOTE)):
            self.see("Dell Capital", "org", source=source,
                     ts=NOW + i * 2 * HOUR,
                     text="Dell Capital sent the term sheet")
        self.hear("Dell Capital", ts=NOW + 10 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))
        self.assertEqual(self.store.entity_mentions(), [])
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_forgetting_an_entity_removes_its_ledger_rows(self):
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Northwind")
        self.assertEqual(len(self.bound("Northwind")), 2)
        # An alias the user tied to it (only those are forgotten with it).
        self.store.upsert_entity_alias(eid, "NW Trading", "nw trading",
                                       source="user", confirmed=True, ts=NOW)
        # A sighting still waiting under an alias of the entity.
        self.store.record_entity_mention("NW Trading", event_id=991,
                                         observed_at=NOW)
        self.store.add_pending_entity_relation(
            subj_kind="entity", subj_name="NW Trading", predicate="uses",
            obj_kind="entity", obj_name="Postgres", source_event_id=991,
            ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertEqual(self.store.entity_mentions(), [])
        self.assertEqual(self.store.pending_entity_relations(), [])
        # And it does not start again.
        self.see("Northwind", "org", source=MAIL, ts=NOW + 4 * HOUR)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 6 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.entity_mentions(), [])

    def test_a_plain_delete_removes_the_rows_but_lets_the_name_return(self):
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Northwind")
        self.store.delete_entity(eid)
        self.assertEqual(self.store.entity_mentions(), [])
        # Free to come back, but from nothing: one sighting is one sighting.
        self.see("Northwind", "org", source=MAIL, ts=NOW + 4 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(len(self.pending("Northwind")), 1)

    def test_dismissing_a_candidate_forgets_it(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        rel = {"subject": "Atlas", "subject_kind": "project",
               "predicate": "uses", "object": "Northwind",
               "object_kind": "org", "confidence": 0.9,
               "source_span": "Atlas uses Northwind"}
        self.see("Northwind", "org", source=MAIL, ts=NOW, relations=[rel])
        self.assertEqual(len(self.store.pending_entity_relations()), 1)
        out = cand.dismiss(self.store, "northwind")
        self.assertEqual(out, {"ok": True, "dismissed": 1})
        self.assertTrue(self.store.is_entity_tombstoned("Northwind"))
        tomb = self.store.entity_tombstone("Northwind")
        self.assertEqual((tomb["kind"], tomb["reason"], tomb["actor"]),
                         ("org", "candidate_dismiss", "user"))
        self.assertEqual(self.store.entity_mentions(name_key="northwind"), [])
        self.assertEqual(self.store.pending_entity_relations(), [])
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR,
                 relations=[rel])
        self.see("Northwind", "org", source=CHAT, ts=NOW + 4 * HOUR,
                 text="Northwind again")
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.entity_mentions(name_key="northwind"), [])
        self.assertEqual(self.store.relations_of("entity", atlas)["out"], [])

    def test_dismiss_refuses_an_entity_and_an_unknown_name(self):
        self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.assertEqual(cand.dismiss(self.store, "Atlas"),
                         {"ok": False, "error": "is_entity"})
        self.assertEqual(cand.dismiss(self.store, "Nobody Inc"),
                         {"ok": False, "error": "not_pending"})
        self.assertEqual(cand.dismiss(self.store, "  "),
                         {"ok": False, "error": "empty_name"})
        self.assertEqual(self.store.entity_tombstone_count(), 0)


# ---------------------------------------------------- pooled relations --
class PooledRelationTests(_ExtractCase):
    REL = {"subject": "Atlas", "subject_kind": "project",
           "predicate": "uses", "object": "Northwind", "object_kind": "org",
           "confidence": 0.9, "source_span": "Atlas uses Northwind"}

    def test_relation_to_a_candidate_is_pooled_then_replayed(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        first = self.see("Northwind", "org", source=MAIL, ts=NOW,
                         relations=[self.REL])
        # No node, no edge, one pooled relation.
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.relations_of("entity", atlas)["out"], [])
        pooled = self.store.pending_entity_relations(name_key="northwind")
        self.assertEqual(len(pooled), 1)
        p = pooled[0]
        self.assertEqual(
            (p["subj_kind"], p["subj_id"], p["predicate"], p["obj_kind"],
             p["obj_id"], p["obj_name"], p["obj_key"], p["quote"],
             p["source_class"], p["source_event_id"], p["confidence"]),
            ("entity", atlas, "uses", "entity", None, "Northwind",
             "northwind", "Atlas uses Northwind", "connector_mail", first,
             0.9))

        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        north = self.store.find_entity_exact("Northwind")
        self.assertTrue(north)
        out = self.store.relations_of("entity", atlas)["out"]
        self.assertEqual(
            [(e["predicate"], e["obj_type"], int(e["obj_id"]), e["origin"],
              e["source_event_id"]) for e in out],
            [("uses", "entity", north, "asserted", first)])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])

    def test_relation_between_two_candidates_waits_for_both(self):
        rel = {"subject": "Halcyon", "subject_kind": "org",
               "predicate": "uses", "object": "Northwind",
               "object_kind": "org", "confidence": 0.9,
               "source_span": "Halcyon uses Northwind"}
        facts = {"entities": [{"name": "Halcyon", "kind": "org"},
                              {"name": "Northwind", "kind": "org"}],
                 "relations": [rel]}
        ev = self.event(MAIL, "Halcyon uses Northwind", NOW)
        self.ex._persist_entities(facts, ev, NOW, event_source=MAIL,
                                  text="Halcyon uses Northwind")
        self.assertEqual(self.edges(), set())
        self.assertEqual(len(self.store.pending_entity_relations()), 1)

        self.see("Halcyon", "org", source=MAIL, ts=NOW + 2 * HOUR)
        halcyon = self.store.find_entity_exact("Halcyon")
        self.assertTrue(halcyon)
        # One end is a node now; the edge still waits for the other.
        self.assertEqual(self.edges(), set())
        p = self.store.pending_entity_relations()[0]
        self.assertEqual((p["subj_id"], p["obj_id"]), (halcyon, None))

        self.see("Northwind", "org", source=MAIL, ts=NOW + 4 * HOUR)
        north = self.store.find_entity_exact("Northwind")
        self.assertEqual(self.edges(),
                         {("entity", halcyon, "uses", "entity", north)})
        self.assertEqual(self.store.pending_entity_relations(status=None), [])

    def test_extracting_the_same_event_again_pools_one_relation(self):
        self.store.resolve_entity("Atlas", "project", ts=NOW)
        ev = self.see("Northwind", "org", source=MAIL, ts=NOW,
                      relations=[self.REL])
        self.see("Northwind", "org", source=MAIL, ts=NOW, event_id=ev,
                 relations=[self.REL])
        self.assertEqual(len(self.store.pending_entity_relations()), 1)

    def test_relation_from_a_bind_only_source_never_mints_its_endpoint(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Atlas", "project", source=NOTE, ts=NOW,
                 relations=[self.REL])
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.relations_of("entity", atlas)["out"], [])
        self.assertEqual(len(self.pending("Northwind")), 1)

    def test_a_relation_endpoint_alone_is_a_sighting(self):
        # The model named Northwind only as an endpoint, never in entities.
        self.store.resolve_entity("Atlas", "project", ts=NOW)
        for i in range(2):
            self.see("Atlas", "project", source=MAIL,
                     ts=NOW + i * 2 * HOUR, relations=[self.REL])
        self.assertIsNotNone(self.store.find_entity_exact("Northwind"))

    def test_relation_is_dropped_when_its_other_end_was_deleted(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW,
                 relations=[self.REL])
        self.store.delete_entity(atlas)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertTrue(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.edges(), set())
        self.assertEqual(self.store.pending_entity_relations(status=None), [])

    def test_relation_from_a_person_to_a_candidate_is_pooled_then_replayed(
            self):
        dana = self.store.resolve_person("Dana Whitfield", ts=NOW)
        rel = {"subject": "Dana Whitfield", "subject_kind": "person",
               "predicate": "uses", "object": "Northwind",
               "object_kind": "org", "confidence": 0.9,
               "source_span": "Dana Whitfield uses Northwind"}
        # Person resolution is its own pipeline; here it only has to hand
        # back the row that exists.
        with patch.object(self.ex, "_resolve_person_id", return_value=dana):
            first = self.see("Northwind", "org", source=MAIL, ts=NOW,
                             relations=[rel])
        self.assertEqual(self.edges(), set())
        p = self.store.pending_entity_relations(name_key="northwind")
        self.assertEqual(
            [(x["subj_kind"], x["subj_id"], x["obj_kind"], x["obj_id"])
             for x in p], [("person", dana, "entity", None)])
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        north = self.store.find_entity_exact("Northwind")
        self.assertTrue(north)
        self.assertEqual(self.edges(),
                         {("person", dana, "uses", "entity", north)})
        out = self.store.relations_of("person", dana)["out"]
        self.assertEqual([e["source_event_id"] for e in out], [first])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])

    def test_a_pooled_relation_is_dropped_when_its_person_was_deleted(self):
        dana = self.store.resolve_person("Dana Whitfield", ts=NOW)
        self.store.add_pending_entity_relation(
            subj_kind="person", subj_id=dana, subj_name="Dana Whitfield",
            predicate="works_at", obj_kind="entity", obj_name="Northwind",
            source_event_id=7, ts=NOW)
        self.store.delete_person(dana)
        self.assertEqual(self.store.pending_entity_relations(status=None), [])


# ---------------------------------------------------------------- expiry --
class ExpiryTests(_ExtractCase):
    REL = PooledRelationTests.REL

    def test_pending_sightings_expire_with_their_pooled_relations(self):
        self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW,
                 relations=[self.REL])
        bound_before = self.bound("Atlas")
        self.assertEqual(len(bound_before), 1)
        wall = time.time()
        # Inside the window nothing goes.
        self.assertEqual(cand.sweep_expired(self.store, now=wall + 29 * DAY),
                         {"mentions": 0, "relations": 0})
        self.assertEqual(len(self.pending("Northwind")), 1)
        gone = cand.sweep_expired(self.store, now=wall + 31 * DAY)
        self.assertEqual(gone, {"mentions": 1, "relations": 1})
        self.assertEqual(self.pending("Northwind"), [])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])
        # Bound rows are evidence for a live entity and stay.
        self.assertEqual(self.bound("Atlas"), bound_before)

    def test_an_expired_sighting_still_counts_through_its_summary(self):
        # Check A (was: an expired sighting no longer counted, so this was a
        # first sighting again). The lazy sweep on record folds the old
        # sighting into a text-free summary and deletes it; the summary is
        # the first session, this sighting the second.
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 31 * DAY,
                 now=time.time() + 31 * DAY)
        row = self.entity("Northwind")
        self.assertIsNotNone(row)
        # Only the new sighting is in the ledger; the old one is a count in
        # the summary, which stays as kind evidence.
        self.assertEqual([r["observed_at"] for r in self.bound("Northwind")],
                         [NOW + 31 * DAY])
        s = self.store.entity_candidate_summary("northwind")
        self.assertEqual((s["mentions"], s["sessions"]), (1, 1))

    def test_a_summary_ttl_of_zero_keeps_nothing(self):
        # The Phase 1 behaviour, by configuration: expiry deletes, so the
        # next sighting is a first sighting again.
        with patch.dict(os.environ,
                        {"QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS": "0"}):
            self.see("Northwind", "org", source=MAIL, ts=NOW)
            self.see("Northwind", "org", source=MAIL, ts=NOW + 31 * DAY,
                     now=time.time() + 31 * DAY)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        rows = self.pending("Northwind")
        self.assertEqual([r["observed_at"] for r in rows], [NOW + 31 * DAY])
        self.assertIsNone(self.store.entity_candidate_summary("northwind"))

    def test_the_ttl_is_read_from_the_environment(self):
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATE_TTL_DAYS": "2"}):
            self.assertEqual(cand.ttl_days(), 2.0)
            gone = cand.sweep_expired(self.store, now=time.time() + 3 * DAY)
        self.assertEqual(gone["mentions"], 1)

    def test_old_events_imported_today_are_not_expired_on_arrival(self):
        # A mail import: the events are a year old, the ledger rows are new.
        old = NOW - 365 * DAY
        self.see("Northwind", "org", source=MAIL, ts=old, now=time.time())
        self.assertEqual(len(self.pending("Northwind")), 1)
        self.see("Northwind", "org", source=MAIL, ts=old + 2 * HOUR,
                 now=time.time())
        self.assertIsNotNone(self.store.find_entity_exact("Northwind"))

    def test_a_pooled_relation_goes_when_its_candidate_has_no_sighting(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.store.add_pending_entity_relation(
            subj_kind="entity", subj_id=atlas, subj_name="Atlas",
            predicate="uses", obj_kind="entity", obj_name="Northwind",
            source_event_id=5, ts=NOW)
        gone = self.store.expire_entity_candidates(0.0)
        self.assertEqual(gone, {"mentions": 0, "relations": 1})

    def test_a_sweep_that_finds_nothing_leaves_no_transaction_open(self):
        # The sweep runs on every extracted event. A DELETE that matches
        # nothing still opens a write transaction: left uncommitted it holds
        # the write lock, and every other connection to the file fails.
        self.assertEqual(cand.sweep_expired(self.store, now=time.time()),
                         {"mentions": 0, "relations": 0})
        self.assertFalse(self.store._conn.in_transaction)
        other = sqlite3.connect(str(Path(self.tmp) / "t.db"), timeout=0)
        self.addCleanup(other.close)
        other.execute("CREATE TABLE IF NOT EXISTS probe_other_writer (x)")
        other.commit()
        # The same through the extractor's own path.
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.assertFalse(self.store._conn.in_transaction)


# -------------------------------------------------------------- flag off --
class FlagOffTests(_ExtractCase):
    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "0"})
        env.start()
        self.addCleanup(env.stop)

    def test_flag_is_read_at_call_time(self):
        self.assertFalse(cand.enabled())
        for off in ("false", "False"):
            with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": off}):
                self.assertFalse(cand.enabled())
        with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "1"}):
            self.assertTrue(cand.enabled())

    def test_new_name_is_minted_at_first_sight_and_the_ledger_is_written(self):
        ev = self.see("Northwind", "org", source=MAIL, ts=NOW)
        row = self.entity("Northwind")
        self.assertIsNotNone(row)
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        self.assertEqual(self.pending("Northwind"), [])
        self.assertEqual([(m["event_id"], m["entity_id"])
                          for m in self.bound("Northwind")],
                         [(ev, row["id"])])

    def test_weak_and_code_like_names_mint_at_first_sight_too(self):
        self.see("ski resort", "place", source=MAIL, ts=NOW)
        self.see("c4", "project", source=MAIL, ts=NOW)
        self.assertIsNotNone(self.store.find_entity_exact("ski resort"))
        self.assertIsNotNone(self.store.find_entity_exact("c4"))

    def test_relation_endpoint_is_minted_and_linked_at_first_sight(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Atlas", "project", source=MAIL, ts=NOW,
                 relations=[PooledRelationTests.REL])
        north = self.store.find_entity_exact("Northwind")
        self.assertTrue(north)
        self.assertEqual(self.edges(),
                         {("entity", atlas, "uses", "entity", north)})
        self.assertEqual(self.store.pending_entity_relations(), [])

    def test_bind_only_source_still_mints_nothing(self):
        self.see("Northwind", "org", source=NOTE, ts=NOW)
        self.see("Northwind", "org", source=NOTE, ts=NOW + 2 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        # The sightings are remembered all the same.
        self.assertEqual(len(self.pending("Northwind")), 2)

    def test_bind_only_relation_to_a_new_name_is_dropped_as_before(self):
        # Before the ledger a bind-only source could not mint an endpoint,
        # so its relation to a new name wrote nothing. With the gate off
        # that edge must not come back later through the pool.
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Atlas", "project", source=NOTE, ts=NOW,
                 relations=[PooledRelationTests.REL])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])
        self.assertEqual(len(self.pending("Northwind")), 1)
        # An eligible source now mints the name at first sight.
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertTrue(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.relations_of("entity", atlas)["out"], [])
        self.assertEqual(self.edges(), set())

    def test_forgotten_name_is_still_refused(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.see("Dell Capital", "org", source=MAIL, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))
        self.assertEqual(self.store.entity_mentions(), [])


# ------------------------------------------------------------ edge cases --
class EdgeCaseTests(_ExtractCase):
    def test_blank_names_write_nothing(self):
        ev = self.event(MAIL, "nothing to see", NOW)
        facts = {"entities": [{"name": "", "kind": "org"},
                              {"name": "   ", "kind": "org"},
                              {"name": None, "kind": "org"},
                              {"kind": "org"}],
                 "relations": [{"subject": "", "subject_kind": "org",
                                "predicate": "uses", "object": "   ",
                                "object_kind": "org"}]}
        self.assertEqual(
            self.ex._persist_entities(facts, ev, NOW, event_source=MAIL,
                                      text="nothing to see"), (0, 0))
        self.assertEqual(self.store.entity_mentions(), [])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_case_and_accents_are_one_candidate(self):
        self.see("Café Roma", "place", source=MAIL, ts=NOW)
        self.assertEqual(len(self.pending("CAFE ROMA")), 1)
        self.see("CAFE ROMA", "place", source=MAIL, ts=NOW + 2 * HOUR)
        rows = self.store.all_entities()
        self.assertEqual(len(rows), 1)
        eid = rows[0]["id"]
        self.assertEqual(sorted(a.lower() for a in rows[0]["aliases"]),
                         ["cafe roma", "café roma"])
        # Either spelling, in any case, is that row from now on.
        for i, spelling in enumerate(("Café Roma", "cafe roma", "CAFÉ ROMA")):
            self.see(spelling, "place", source=MAIL,
                     ts=NOW + (i + 2) * 2 * HOUR)
        self.assertEqual([r["id"] for r in self.store.all_entities()], [eid])
        self.assertEqual({m["entity_id"] for m in self.bound("cafe roma")},
                         {eid})
        self.assertEqual(len(self.bound("cafe roma")), 5)
        self.assertEqual(self.pending("cafe roma"), [])

    def test_a_name_with_no_latin_letters_is_a_candidate_like_any_other(self):
        self.see("東京電力", "org", source=MAIL, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("東京電力"))
        self.assertEqual(len(self.pending("東京電力")), 1)
        self.see("東京電力", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertIsNotNone(self.store.find_entity_exact("東京電力"))
        # Typed by the user: at first sight, and found in the typed text.
        self.see("三菱商事", "org", source=CHAT, ts=NOW,
                 text="明日 三菱商事 と会う")
        self.assertIsNotNone(self.store.find_entity_exact("三菱商事"))

    def test_an_alias_of_an_existing_entity_binds_and_never_waits(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW)
        self.store.touch_entity(eid, NOW, alias="NW Trading")
        ev = self.see("NW Trading", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertEqual([(m["event_id"], m["entity_id"])
                          for m in self.bound("NW Trading")], [(ev, eid)])
        # A bind-only source binds an exact canonical name only, as before:
        # an alias is neither bound nor left waiting as a new name.
        self.see("NW Trading", "org", source=NOTE, ts=NOW + 4 * HOUR)
        self.assertEqual(len(self.bound("NW Trading")), 1)
        self.assertEqual(self.pending("NW Trading"), [])
        self.assertEqual([r["id"] for r in self.store.all_entities()], [eid])

    def test_a_hidden_row_takes_the_sightings_and_is_never_a_candidate(self):
        eid = self.store.resolve_entity("Friday", "thing", ts=NOW,
                                        kind_source="rule", hidden=True)
        self.see("Friday", "idea", source=MAIL, ts=NOW)
        self.see("Friday", "idea", source=MAIL, ts=NOW + 2 * HOUR)
        self.see("Friday", "idea", source=CHAT, ts=NOW + 4 * HOUR,
                 text="see you Friday")
        # Still hidden, still one row, and nothing is waiting to exist.
        self.assertEqual(self.store.all_entities(), [])
        self.assertEqual(
            [r["id"] for r in self.store.all_entities(include_hidden=True)],
            [eid])
        self.assertEqual(self.pending("Friday"), [])
        self.assertEqual({m["entity_id"] for m in self.bound("Friday")},
                         {eid})
        self.assertEqual(cand.list_candidates(self.store), [])

    def test_one_relation_naming_a_candidate_on_both_ends_is_not_pooled(self):
        rel = {"subject": "Northwind", "subject_kind": "org",
               "predicate": "part_of", "object": "northwind",
               "object_kind": "org", "confidence": 0.9,
               "source_span": "Northwind is part of Northwind"}
        self.see("Northwind", "org", source=MAIL, ts=NOW, relations=[rel])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR,
                 relations=[rel])
        self.assertTrue(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.edges(), set())

    def test_sightings_from_several_threads_make_one_entity(self):
        import threading
        errors: list[BaseException] = []
        gate = threading.Barrier(6)

        def one(i: int) -> None:
            try:
                gate.wait(timeout=10)
                self.see("Northwind", "org", source=MAIL,
                         ts=NOW + i * 2 * HOUR)
            except BaseException as exc:   # noqa: BLE001 - reported below
                errors.append(exc)

        threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        rows = self.store.all_entities(include_hidden=True)
        self.assertEqual(len(rows), 1)
        eid = rows[0]["id"]
        every = self.store.entity_mentions(name_key="northwind")
        self.assertEqual(len(every), 6)
        self.assertEqual(len({m["event_id"] for m in every}), 6)
        self.assertEqual({m["entity_id"] for m in every
                          if m["status"] == "bound"}, {eid})
        # A sighting that lost the race to the mint may still be pending; it
        # is never listed as a candidate and the next sighting takes it.
        self.assertEqual(cand.list_candidates(self.store), [])
        self.see("Northwind", "org", source=MAIL, ts=NOW + 20 * HOUR)
        self.assertEqual(self.pending("Northwind"), [])
        self.assertEqual(len(self.bound("Northwind")), 7)
        self.assertFalse(self.store._conn.in_transaction)


# ------------------------------------------------- direct minters, claims --
class DirectMinterTests(_ExtractCase):
    def test_filing_mints_at_first_sight_and_adopts_what_was_pending(self):
        from app.services import projects
        tool = self.store.resolve_entity("Postgres", "tool", ts=NOW)
        rel = {"subject": "Atlas", "subject_kind": "project",
               "predicate": "uses", "object": "Postgres",
               "object_kind": "tool", "confidence": 0.9,
               "source_span": "Atlas uses Postgres"}
        ev = self.see("Atlas", "tool", source=MAIL, ts=NOW, relations=[rel])
        self.assertIsNone(self.store.find_entity_exact("Atlas"))

        eid = projects.ensure_filed("Atlas", store=self.store, now=NOW + 60)
        self.assertTrue(eid)
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]),
                         ("project", "filing"))
        self.assertEqual(self.pending("Atlas"), [])
        self.assertEqual([(m["event_id"], m["entity_id"])
                          for m in self.bound("Atlas")], [(ev, eid)])
        self.assertEqual(self.edges(),
                         {("entity", eid, "uses", "entity", tool)})

    def test_manual_add_mints_at_user_rank_and_adopts(self):
        ev = self.see("ski resort", "place", source=MAIL, ts=NOW)
        out = cand.add_manual(self.store, "  Ski   Resort ", "org", now=NOW)
        self.assertTrue(out["ok"])
        self.assertTrue(out["created"])
        row = self.store.get_entity(out["id"])
        self.assertEqual((row["name"], row["kind"], row["kind_source"]),
                         ("Ski Resort", "org", "user"))
        self.assertEqual(self.pending("ski resort"), [])
        self.assertEqual([(m["event_id"], m["entity_id"])
                          for m in self.bound("ski resort")],
                         [(ev, out["id"])])

    def test_manual_add_lifts_a_tombstone(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        out = cand.add_manual(self.store, "Dell Capital", "org", now=NOW)
        self.assertTrue(out["ok"] and out["created"])
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        # And extraction binds it again.
        ev = self.see("Dell Capital", "org", source=MAIL, ts=NOW + HOUR)
        self.assertEqual([(m["event_id"], m["entity_id"])
                          for m in self.bound("Dell Capital")],
                         [(ev, out["id"])])

    def test_manual_add_of_an_existing_name_is_not_a_second_row(self):
        eid = self.store.resolve_entity("Northwind", "org", ts=NOW,
                                        kind_source="model")
        out = cand.add_manual(self.store, "northwind", "tool", now=NOW)
        self.assertEqual((out["ok"], out["id"], out["created"]),
                         (True, eid, False))
        row = self.store.get_entity(eid)
        self.assertEqual((row["kind"], row["kind_source"]), ("tool", "user"))
        self.assertEqual(cand.add_manual(self.store, "  ", "org"),
                         {"ok": False, "error": "empty_name"})

    def test_a_hidden_value_literal_adopts_nothing(self):
        self.see("Friday", "idea", source=MAIL, ts=NOW)
        eid = self.store.resolve_entity("Friday", "thing", ts=NOW,
                                        kind_source="rule", hidden=True)
        self.assertTrue(eid)
        self.assertEqual(len(self.pending("Friday")), 1)
        self.assertEqual(self.store.get_entity(eid)["aliases"], ["Friday"])


class ClaimSubjectTests(_ExtractCase):
    def claim(self, ts: float, source: str = MAIL) -> None:
        from app.services.consolidation import Turn
        text = "Brightline costs $49 a month"
        ev = self.event(source, text, ts)
        turn = Turn(start=ts, end=ts, speaker="Hugh", text=text,
                    event_ids=[ev], n_utterances=1)
        cl = {"text": text, "confidence": 0.9, "source_span": text,
              "assertion": "stated_by_other", "subject": "Brightline",
              "predicate": "costs", "object": "$49"}
        self.ex._persist_claim_belief(cl, fact_id=0, turn=turn, anchor=ev,
                                      now=ts, event_source=source)

    def test_claim_subject_waits_like_any_other_new_name(self):
        self.claim(NOW)
        self.assertIsNone(self.store.find_entity_exact("Brightline"))
        self.assertEqual(
            self.store.list_kg_predicates(predicate="costs", limit=20), [])
        self.assertEqual(len(self.pending("Brightline")), 1)
        # Seen again in another session: the subject is a node and the
        # claim that names it carries a belief.
        self.claim(NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Brightline")
        self.assertTrue(eid)
        beliefs = self.store.list_kg_predicates(predicate="costs", limit=20)
        self.assertEqual([(b["subj_type"], int(b["subj_id"]))
                          for b in beliefs], [("entity", eid)])

    def test_flag_off_mints_the_claim_subject_at_first_sight(self):
        with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "0"}):
            self.claim(NOW)
        eid = self.store.find_entity_exact("Brightline")
        self.assertTrue(eid)
        self.assertEqual([m["entity_id"]
                          for m in self.bound("Brightline")], [eid])


# ------------------------------------------ carried over from Phase 0 --
class HiddenRowTests(_StoreCase):
    def hidden(self, name: str = "Friday") -> int:
        eid = self.store.resolve_entity(name, "thing", ts=NOW,
                                        kind_source="rule", hidden=True)
        self.assertEqual(self.store.all_entities(), [])
        return eid

    def test_filing_under_a_hidden_name_brings_the_row_back(self):
        from app.services import projects
        eid = self.hidden()
        self.assertEqual(
            projects.ensure_filed("Friday", store=self.store, now=NOW + 5),
            eid)
        rows = self.store.all_entities()
        self.assertEqual([(r["id"], r["name"], r["kind"], r["kind_source"])
                          for r in rows],
                         [(eid, "Friday", "project", "filing")])

    def test_user_rank_brings_a_hidden_row_back_and_rekinds_it(self):
        eid = self.hidden()
        self.assertEqual(self.store.resolve_entity(
            "Friday", "org", ts=NOW, kind_source="user"), eid)
        row = self.store.all_entities()[0]
        self.assertEqual((row["id"], row["kind"], row["kind_source"]),
                         (eid, "org", "user"))

    def test_extraction_never_unhides(self):
        eid = self.hidden()
        for source in (None, "model", "rule"):
            self.assertEqual(self.store.resolve_entity(
                "Friday", "org", ts=NOW, kind_source=source), eid, source)
            self.assertEqual(self.store.all_entities(), [], source)
        self.assertEqual(
            Resolver(store=self.store).resolve_entity(
                "Friday", "org", ts=NOW, kind_source="model"), eid)
        self.assertEqual(self.store.all_entities(), [])

    def test_unhide_keeps_a_kind_no_one_restated(self):
        eid = self.hidden()
        self.store.resolve_entity("Friday", None, ts=NOW, kind_source="user")
        row = self.store.all_entities()[0]
        self.assertEqual((row["id"], row["kind"]), (eid, "thing"))


class PeopleGuardTests(_StoreCase):
    TEXT = "Boost Run is giving us free compute this month"

    def resolve(self, event_id: int):
        from app.services import people_pipeline as pp
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
            return pp.resolve_person_mention(
                "Boost Run", store=self.store, event_id=event_id,
                event_source=CHAT, text=self.TEXT, now=NOW + 1)

    def org_rejections(self) -> int:
        with self.store._lock:
            rows = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM person_mentions "
                "WHERE resolution_status = 'rejected' "
                "AND person_probability <= 0.05").fetchone()
        return int(rows["n"])

    def test_a_forgotten_org_name_does_not_come_back_as_a_person(self):
        eid = self.store.resolve_entity("Boost Run", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        res = self.resolve(10)
        self.assertEqual(res.decision, "reject")
        self.assertIsNone(res.person_id)
        self.assertIsNone(self.store.find_person_exact("Boost Run"))
        self.assertEqual(self.org_rejections(), 1)

    def test_a_dismissed_org_candidate_does_not_block_a_person(self):
        # Decision D7 (was: guarded too). Dismissing says "not a thing";
        # it never was an org row, so the name is free to be a person.
        self.store.record_entity_mention("Boost Run", proposed_kind="org",
                                         event_id=3, observed_at=NOW)
        self.assertTrue(cand.dismiss(self.store, "Boost Run")["ok"])
        self.assertEqual(self.store.entity_tombstone("Boost Run")["kind"],
                         "org")
        self.resolve(11)
        self.assertEqual(self.org_rejections(), 0)

    def test_a_plain_delete_or_a_forgotten_tool_is_not_an_org_name(self):
        # The guard covers what it covered while the row was live: orgs.
        eid = self.store.resolve_entity("Boost Run", "org", ts=NOW)
        self.store.delete_entity(eid)                    # no tombstone
        self.resolve(12)
        self.assertEqual(self.org_rejections(), 0)
        tool = self.store.resolve_entity("Boost Run", "tool", ts=NOW)
        self.store.delete_entity(tool, tombstone=True, reason="forget")
        self.resolve(13)
        self.assertEqual(self.org_rejections(), 0)

    def test_an_existing_person_with_the_name_still_binds(self):
        pid = self.store.insert_person("Boost Run", ts=NOW,
                                       promotion_state="active")
        eid = self.store.resolve_entity("Boost Run", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        res = self.resolve(14)
        self.assertEqual(res.person_id, pid)
        self.assertEqual(self.org_rejections(), 0)


class ManualSplitTests(_StoreCase):
    def test_only_the_user_lifts_a_tombstone_through_a_split(self):
        from app.services import kg_beliefs
        # The old node's kind was set by the user: its source is inherited.
        src = self.store.resolve_entity("Dell", "org", ts=NOW,
                                        kind_source="user")
        gone = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.store.delete_entity(gone, tombstone=True, reason="forget")

        out = kg_beliefs.manual_split(
            self.store, node_type="entity", node_id=src,
            new_name="Dell Capital", predicate_ids=[], decided_by="auto",
            now=NOW)
        self.assertEqual(out, {"ok": False, "error": "name_forgotten"})
        self.assertIsNone(self.store.find_entity_exact("Dell Capital"))
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capital"))

        out = kg_beliefs.manual_split(
            self.store, node_type="entity", node_id=src,
            new_name="Dell Capital", predicate_ids=[], decided_by="user",
            now=NOW)
        self.assertTrue(out.get("ok"), out)
        row = self.store.get_entity(out["new_node_id"])
        self.assertEqual((row["name"], row["kind"], row["kind_source"]),
                         ("Dell Capital", "org", "user"))
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))

    def test_an_automatic_split_onto_a_free_name_still_works(self):
        from app.services import kg_beliefs
        src = self.store.resolve_entity("Dell", "org", ts=NOW,
                                        kind_source="user")
        out = kg_beliefs.manual_split(
            self.store, node_type="entity", node_id=src,
            new_name="Dell Capital", predicate_ids=[], decided_by="auto",
            now=NOW)
        self.assertTrue(out.get("ok"), out)
        self.assertTrue(self.store.find_entity_exact("Dell Capital"))


# ---------------------------------------------------------------- routes --
class CandidateRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_cand_route_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)

    def sight(self, name, kind, cls, event_id, ts, **kw):
        return self.store.record_entity_mention(
            name, proposed_kind=kind, confidence=0.8, source_class=cls,
            event_source=kw.pop("event_source", MAIL), event_id=event_id,
            observed_at=ts, weak=entity_typing.common_noun_like(name), **kw)

    def test_candidates_route_lists_pending_names(self):
        self.sight("Northwind", "org", "connector_mail", 1, NOW)
        self.sight("Northwind", "tool", "connector_mail", 2, NOW + 30)
        self.sight("northwind", "org", "user_authored_document", 3,
                   NOW + 2 * HOUR)
        self.sight("ski resort", "place", "connector_mail", 4, NOW + HOUR)
        eid = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.store.record_entity_mention("Atlas", event_id=5, entity_id=eid,
                                         observed_at=NOW)
        r = self.client.get("/entities/candidates")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["enabled"], body["min_sessions"],
                          body["ttl_days"]), (True, 2, 30.0))
        by_name = {c["name"]: c for c in body["candidates"]}
        # Bound names are entities, not candidates.
        self.assertEqual(sorted(by_name), ["Northwind", "ski resort"])
        n = by_name["Northwind"]
        self.assertEqual(
            (n["proposed_kind"], n["proposed_kinds"], n["mentions"],
             n["sessions"], n["source_classes"], n["first_seen"],
             n["last_seen"], n["weak"]),
            ("org", {"org": 2, "tool": 1}, 3, 2, 2, NOW, NOW + 2 * HOUR,
             False))
        s = by_name["ski resort"]
        self.assertEqual((s["mentions"], s["sessions"], s["source_classes"],
                          s["weak"]), (1, 1, 1, True))
        # Most recently seen first.
        self.assertEqual([c["name"] for c in body["candidates"]],
                         ["Northwind", "ski resort"])

    def test_candidates_route_is_not_read_as_an_entity_id(self):
        r = self.client.get("/entities/candidates")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["candidates"], [])
        # The id route still answers for ids.
        self.assertEqual(self.client.get("/entities/424242").status_code, 404)

    def test_candidates_never_reach_the_entities_tab_list(self):
        self.sight("Northwind", "org", "connector_mail", 1, NOW)
        self.assertEqual(self.client.get("/entities/list").json()["entities"],
                         [])

    def test_manual_add_route(self):
        self.sight("Northwind", "tool", "connector_mail", 1, NOW)
        r = self.client.post("/entities",
                             json={"name": " Northwind ", "kind": "company"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual((body["ok"], body["created"], body["name"],
                          body["kind"], body["kind_source"]),
                         (True, True, "Northwind", "org", "user"))
        eid = body["id"]
        self.assertEqual(self.store.find_entity_exact("Northwind"), eid)
        self.assertEqual(
            [m["entity_id"] for m in self.store.entity_mentions(
                name_key="northwind")], [eid])
        self.assertEqual(self.client.get(
            "/entities/candidates").json()["candidates"], [])
        names = [e["name"] for e in
                 self.client.get("/entities/list").json()["entities"]]
        self.assertEqual(names, ["Northwind"])
        # Adding it again is the same row.
        again = self.client.post("/entities",
                                 json={"name": "northwind", "kind": "org"})
        self.assertEqual((again.status_code, again.json()["id"],
                          again.json()["created"]), (200, eid, False))

    def test_manual_add_route_brings_back_a_forgotten_name(self):
        eid = self.store.resolve_entity("Dell Capital", "org", ts=NOW)
        self.client.post(f"/entities/{eid}/forget")
        self.assertTrue(self.store.is_entity_tombstoned("Dell Capital"))
        r = self.client.post("/entities",
                             json={"name": "Dell Capital", "kind": "org"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.store.is_entity_tombstoned("Dell Capital"))
        self.assertTrue(self.store.find_entity_exact("Dell Capital"))

    def test_manual_add_route_validates_its_input(self):
        for body in ({"name": "   ", "kind": "org"},
                     {"name": "Northwind", "kind": "wizard"},
                     {"name": "x" * 201, "kind": "org"}):
            r = self.client.post("/entities", json=body)
            self.assertEqual(r.status_code, 400, body)
        self.assertEqual(
            self.client.post("/entities", json={"name": "X"}).status_code,
            422)
        self.assertEqual(self.store.all_entities(include_hidden=True), [])

    def test_dismiss_route(self):
        self.sight("Northwind", "org", "connector_mail", 1, NOW)
        self.sight("Northwind", "org", "connector_mail", 2, NOW + 30)
        self.store.add_pending_entity_relation(
            subj_kind="entity", subj_name="Northwind", predicate="uses",
            obj_kind="entity", obj_name="Postgres", source_event_id=1,
            ts=NOW)
        r = self.client.post("/entities/candidates/dismiss",
                             json={"name": "Northwind"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json(), {"ok": True, "dismissed": 2})
        self.assertTrue(self.store.is_entity_tombstoned("Northwind"))
        self.assertEqual(self.store.entity_mentions(), [])
        self.assertEqual(self.store.pending_entity_relations(), [])
        self.assertEqual(self.client.get(
            "/entities/candidates").json()["candidates"], [])
        # Extraction cannot bring it back.
        self.assertEqual(Resolver(store=self.store).resolve_entity(
            "Northwind", "org", ts=NOW, kind_source="model"), 0)

    def test_dismiss_route_errors(self):
        self.store.resolve_entity("Atlas", "project", ts=NOW)
        cases = (({"name": "Atlas"}, 409), ({"name": "Nobody Inc"}, 404),
                 ({"name": "  "}, 400))
        for body, status in cases:
            r = self.client.post("/entities/candidates/dismiss", json=body)
            self.assertEqual(r.status_code, status, body)
        self.assertEqual(self.store.entity_tombstone_count(), 0)
        self.assertTrue(self.store.find_entity_exact("Atlas"))


class SurfaceTests(unittest.TestCase):
    def test_inspect_links_to_the_candidates(self):
        from app.api.shell_routes import INSPECT_GROUPS
        hrefs = [link["href"] for g in INSPECT_GROUPS for link in g["links"]]
        self.assertIn("/entities/candidates", hrefs)

    def test_orgs_tab_has_an_add_control_and_never_loads_candidates(self):
        src = (Path(__file__).resolve().parent.parent / "app" / "api"
               / "profile_page.py").read_text(encoding="utf-8")
        self.assertIn('id="eAdd"', src)
        self.assertIn('id="eAddName"', src)
        self.assertIn('id="eAddKind"', src)
        self.assertIn("fetch('/entities',{method:'POST'", src)
        self.assertNotIn("/entities/candidates", src)

    def test_graph_job_sweeps_expired_candidates(self):
        src = (Path(__file__).resolve().parent.parent / "app"
               / "main.py").read_text(encoding="utf-8")
        job = src.split("def _graph_job(_payload)")[1].split(
            'worker.register("graph", _graph_job)')[0]
        self.assertIn("entity_candidates.sweep_expired(", job)


if __name__ == "__main__":
    unittest.main()
