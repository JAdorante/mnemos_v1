"""Entity model, stage G4: a seat's own name vocabulary lives in its store.

`name_quality`'s word lists hold generic words only. A word that is one
seat's own (a brand on its user's desktop, a coined project name) is a row in
that seat's `seat_vocab` table, read by every name gate through a cached
accessor (`Store.seat_vocab`, `name_quality._seat_vocab`). The exact names a
user said are no person are learned from their corrections (turning a person
into an entity, "Not a person"); single words never are.
`scripts/seed_seat_vocab.py` moves a seat's words into its store so the seat
behaves exactly as when the words were in code.

Every word and name here is invented: no seat's vocabulary is in the repo.
"""
from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.api.routes  # noqa: F401  (before any get_store patch)
import app.storage as storage_mod
from app.services import name_quality as nq
from app.storage import Store

NOW = 1_700_000_000.0
ROOT = Path(__file__).resolve().parents[1]
WORD = "zorblat"        # an invented brand
NAMES = ("Zorblat Prime", "Zorblat", "Ada Zorblat", "Zorblat,",
         "Prime Zorblat.", "zorblat hughes", "ZORBLAT Ventures",
         "Zorblat Labs", "Ada Lovelace", "Velloria Hub")


def _mk(tmp, name="t.db") -> Store:
    return Store(db_path=Path(tmp) / name, audio_dir=Path(tmp) / "audio")


def _decisions(names, store=None) -> dict:
    return {n: (nq.is_plausible_person(n, store=store),
                nq.is_person_shaped_entity_name(n, store=store),
                nq.should_mint_as_entity(n, "project", store=store),
                nq.should_mint_as_entity(n, "org", store=store))
            for n in names}


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_seat_vocab_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        # No shared store: a call without `store=` judges by code alone.
        shared = patch.object(storage_mod, "_store", None)
        shared.start()
        self.addCleanup(shared.stop)


# ------------------------------------------------------------ the table --
class SeatVocabStoreTests(_Case):
    def test_add_list_remove(self):
        s = self.store
        self.assertEqual(s.seat_vocab(), {r: frozenset()
                                          for r in Store.SEAT_VOCAB_ROLES})
        self.assertTrue(s.add_seat_vocab(" Zorblat ", "never_person_token",
                                         source="migration"))
        self.assertFalse(s.add_seat_vocab("zorblat", "never_person_token",
                                          source="user"))     # already there
        self.assertTrue(s.add_seat_vocab("zorblat", "nonperson_token",
                                         source="user"))
        self.assertEqual(s.seat_vocab()["never_person_token"],
                         frozenset({"zorblat"}))
        rows = s.list_seat_vocab()
        self.assertEqual([(r["term"], r["role"], r["source"]) for r in rows],
                         [("zorblat", "never_person_token", "migration"),
                          ("zorblat", "nonperson_token", "user")])
        self.assertTrue(s.remove_seat_vocab("ZORBLAT", "never_person_token"))
        self.assertFalse(s.remove_seat_vocab("zorblat", "never_person_token"))
        self.assertEqual(s.seat_vocab()["never_person_token"], frozenset())

    def test_roles_and_sources_are_checked(self):
        s = self.store
        with self.assertRaises(ValueError):
            s.add_seat_vocab("zorblat", "surname", source="user")
        # One correction never teaches a word; a migration never teaches a
        # name.
        with self.assertRaises(ValueError):
            s.add_seat_vocab("zorblat", "never_person_token",
                             source="correction")
        with self.assertRaises(ValueError):
            s.add_seat_vocab("Velloria Hub", "nonperson_name",
                             source="migration")
        # A token role holds one word.
        self.assertFalse(s.add_seat_vocab("zorblat prime", "corp_word",
                                          source="user"))
        self.assertFalse(s.add_seat_vocab("  ", "corp_word", source="user"))
        self.assertEqual(s.list_seat_vocab(), [])

    def test_a_learned_name_is_kept_as_a_hash(self):
        self.assertTrue(self.store.record_not_person("Velloria Hub"))
        rows = self.store.list_seat_vocab()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["role"], "nonperson_name")
        self.assertEqual(rows[0]["term"],
                         Store.entity_tombstone_hash("Velloria Hub"))
        raw = sqlite3.connect(self.store.db_path)
        self.addCleanup(raw.close)
        dump = " ".join(str(c) for row in raw.execute(
            "SELECT * FROM seat_vocab") for c in row).lower()
        self.assertNotIn("velloria", dump)
        # The exact name, folded like every name key.
        self.assertTrue(self.store.is_seat_nonperson_name("velloria  hub"))
        self.assertFalse(self.store.is_seat_nonperson_name("Velloria"))
        self.assertTrue(self.store.forget_not_person("VELLORIA HUB"))
        self.assertFalse(self.store.is_seat_nonperson_name("Velloria Hub"))

    def test_a_read_only_store_without_the_table(self):
        path = Path(self.tmp) / "old.db"
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE people (id INTEGER PRIMARY KEY, "
                    "canonical_name TEXT)")
        con.commit()
        con.close()
        ro = Store(db_path=path, readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.seat_vocab()["never_person_token"], frozenset())
        self.assertEqual(ro.list_seat_vocab(), [])
        self.assertFalse(ro.add_seat_vocab(WORD, "never_person_token",
                                           source="user"))
        self.assertFalse(ro.record_not_person("Velloria Hub"))
        self.assertFalse(ro.is_seat_nonperson_name("Velloria Hub"))
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=ro))

    def test_a_read_only_store_reads_and_never_writes(self):
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        ro = Store(db_path=self.store.db_path, readonly=True)
        self.addCleanup(ro.close)
        self.assertIn(WORD, ro.seat_vocab()["never_person_token"])
        self.assertFalse(nq.is_plausible_person("Zorblat Prime", store=ro))
        self.assertFalse(ro.add_seat_vocab("quenblat", "never_person_token",
                                           source="user"))
        self.assertFalse(ro.remove_seat_vocab(WORD, "never_person_token"))
        self.assertIn(WORD, self.store.seat_vocab()["never_person_token"])

    def test_one_read_per_ttl_and_writes_drop_the_cache(self):
        s = self.store
        s.add_seat_vocab(WORD, "never_person_token", source="user")
        reads = []
        s._conn.set_trace_callback(
            lambda sql: reads.append(sql) if "FROM seat_vocab" in sql
            else None)
        self.addCleanup(s._conn.set_trace_callback, None)
        for _ in range(500):
            nq.is_plausible_person("Ada Lovelace", store=s)
            nq.is_person_shaped_entity_name("Ada Lovelace", store=s)
        self.assertEqual(len(reads), 1)
        # This store's own write is seen at once.
        s.add_seat_vocab("lovelace", "never_person_token", source="user")
        self.assertFalse(nq.is_plausible_person("Ada Lovelace", store=s))
        self.assertEqual(len(reads), 2)

    def test_another_store_on_the_file_sees_a_write_after_the_ttl(self):
        other = Store(db_path=self.store.db_path,
                      audio_dir=Path(self.tmp) / "audio")
        self.addCleanup(other.close)
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=other))
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        # Cached on `other` until the TTL runs out.
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=other))
        with patch.object(Store, "SEAT_VOCAB_TTL_S", 0.0):
            self.assertFalse(nq.is_plausible_person("Zorblat Prime",
                                                    store=other))


# ------------------------------------------------------------ the gates --
class SeatVocabGateTests(_Case):
    def test_each_role_reaches_its_gate(self):
        s = self.store
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=s))
        self.assertTrue(nq.is_person_shaped_entity_name("Zorblat Hughes",
                                                        store=s))
        self.assertFalse(nq.should_mint_as_entity("Zorblat Hughes", "project",
                                                  store=s))
        s.add_seat_vocab(WORD, "nonperson_token", source="user")
        # A nonperson word only stops a two-word name reading as a person's.
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=s))
        self.assertFalse(nq.is_person_shaped_entity_name("Zorblat Hughes",
                                                         store=s))
        self.assertTrue(nq.should_mint_as_entity("Zorblat Hughes", "project",
                                                 store=s))
        s.remove_seat_vocab(WORD, "nonperson_token")
        s.add_seat_vocab("quenby", "corp_word", source="user")
        self.assertFalse(nq.is_person_shaped_entity_name("Quenby Marsh",
                                                         store=s))
        s.add_seat_vocab(WORD, "never_person_token", source="user")
        self.assertFalse(nq.is_plausible_person("Zorblat Prime", store=s))
        self.assertFalse(nq.is_plausible_person("Ada Zorblat.", store=s))
        self.assertFalse(nq.is_person_shaped_entity_name("Zorblat Hughes",
                                                         store=s))
        self.assertTrue(nq.is_plausible_person("Ada Lovelace", store=s))

    def test_several_stores_in_one_process(self):
        other = _mk(self.tmp, "other.db")
        self.addCleanup(other.close)
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        self.assertFalse(nq.is_plausible_person("Zorblat Prime",
                                                store=self.store))
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=other))
        # No store: code alone (no shared store is open in this test).
        self.assertTrue(nq.is_plausible_person("Zorblat Prime"))

    def test_without_a_store_the_shared_one_is_read_never_opened(self):
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        self.assertIsNone(storage_mod._store)
        self.assertTrue(nq.is_plausible_person("Zorblat Prime"))
        self.assertIsNone(storage_mod._store)   # nothing was opened
        with patch.object(storage_mod, "_store", self.store):
            self.assertFalse(nq.is_plausible_person("Zorblat Prime"))
            self.assertFalse(nq.is_person_shaped_entity_name("Zorblat Hughes"))

    def test_a_broken_store_reads_as_no_vocabulary(self):
        class Broken:
            def seat_vocab(self):
                raise sqlite3.ProgrammingError("closed")

        class Odd:
            def seat_vocab(self):
                return ["not", "a", "dict"]
        for bad in (Broken(), Odd(), object()):
            self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=bad))
            self.assertTrue(nq.is_person_shaped_entity_name("Zorblat Hughes",
                                                            store=bad))

    def test_a_seeded_word_behaves_exactly_as_the_word_in_code(self):
        """The migration property: moving a word from the code lists into a
        seat's store leaves every decision on that seat unchanged."""
        with patch.object(nq, "_NEVER_PERSON_TOKENS",
                          nq._NEVER_PERSON_TOKENS | {WORD}), \
                patch.object(nq, "_NONPERSON_TOKENS",
                             nq._NONPERSON_TOKENS | {WORD}), \
                patch.object(nq, "_CORP_SUFFIXES",
                             nq._CORP_SUFFIXES | {"quenby"}):
            in_code = _decisions(NAMES + ("Quenby Marsh",))
        self.store.add_seat_vocab(WORD, "never_person_token",
                                  source="migration")
        self.store.add_seat_vocab(WORD, "nonperson_token", source="migration")
        self.store.add_seat_vocab("quenby", "corp_word", source="migration")
        seeded = _decisions(NAMES + ("Quenby Marsh",), store=self.store)
        self.assertEqual(seeded, in_code)
        # And the word really mattered.
        self.assertNotEqual(_decisions(NAMES + ("Quenby Marsh",)), in_code)

    def test_the_resolver_and_the_people_pipeline_read_their_store(self):
        from app.services import people_pipeline as pp
        from app.services.resolution import Resolver
        other = _mk(self.tmp, "other.db")
        self.addCleanup(other.close)
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        self.assertIsNone(Resolver(store=self.store)
                          .resolve_person("Zorblat Prime", ts=NOW))
        self.assertIsNotNone(Resolver(store=other)
                             .resolve_person("Zorblat Prime", ts=NOW))
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
            res = pp.resolve_person_mention(
                "Zorblat Prime", store=self.store, event_id=1,
                event_source="audio.whisper",
                text="Zorblat Prime shipped the update", now=NOW)
        self.assertEqual(res.decision, "reject")
        self.assertEqual(self.store.find_person_exact("Zorblat Prime"), None)
        # The person-shaped reroute of an entity reads the store too (it
        # needs a person the seat knows: fix pass B).
        self.store.add_seat_vocab("hughes", "nonperson_token", source="user")
        other.insert_person("Zorblat Hughes", ts=NOW)
        self.assertGreater(Resolver(store=self.store).resolve_entity(
            "Zorblat Hughes", "project", ts=NOW), 0)
        self.assertEqual(Resolver(store=other).resolve_entity(
            "Zorblat Hughes", "project", ts=NOW), 0)

    def test_typed_chat_names_and_filing_targets_read_the_seat(self):
        from app.services import projects
        from app.services.extractor import _chat_people_names
        text = "Lunch with Zorblat Prime and Ada Lovelace on Friday."
        self.assertEqual(_chat_people_names(text, store=self.store),
                         ["Zorblat Prime", "Ada Lovelace"])
        self.assertFalse(projects._looks_like_project(self.store,
                                                      "Zorblat Hughes"))
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        self.store.add_seat_vocab(WORD, "nonperson_token", source="user")
        self.assertEqual(_chat_people_names(text, store=self.store),
                         ["Ada Lovelace"])
        self.assertTrue(projects._looks_like_project(self.store,
                                                     "Zorblat Hughes"))

    def test_cleanup_reads_the_seat(self):
        from app.services import ambient_cleanup as ac
        pid = self.store.insert_person("Zorblat Prime", ts=NOW)
        self.assertNotIn(pid, [p["id"] for p in ac.plan_people(self.store)])
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        flagged = {p["id"]: p["reason"] for p in ac.plan_people(self.store)}
        self.assertEqual(flagged.get(pid), "implausible_name")


# ---------------------------------------------------- learned from the user --
class LearnedNameTests(_Case):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        super().setUp()
        import app.api.routes as routes_mod
        p = patch.object(routes_mod.memory, "_ensure_store",
                         return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_turning_a_person_into_an_entity_teaches_the_exact_name(self):
        from app.services import graph
        pid = self.store.insert_person("Velloria Hub", ts=NOW)
        self.assertTrue(nq.is_plausible_person("Velloria Hub",
                                               store=self.store))
        res = graph.reclassify_constellation_node(
            self.store, f"person:{pid}", "tool")
        self.assertTrue(res["ok"], res)
        self.assertFalse(nq.is_plausible_person("Velloria Hub",
                                                store=self.store))
        self.assertFalse(nq.is_plausible_person("velloria hub",
                                                store=self.store))
        # Only the exact name: its words are no rule.
        self.assertTrue(nq.is_plausible_person("Velloria Smith",
                                                store=self.store))
        self.assertTrue(nq.is_plausible_person("Ada Hub", store=self.store))
        self.assertEqual(self.store.seat_vocab()["never_person_token"],
                         frozenset())
        # The name is free to mint as the entity it is now.
        self.assertFalse(nq.is_person_shaped_entity_name("Velloria Hub",
                                                         store=self.store))
        # And never proposed as a person again.
        from app.services import people_pipeline as pp
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
            res = pp.resolve_person_mention(
                "Velloria Hub", store=self.store, event_id=7,
                event_source="audio.whisper",
                text="Velloria Hub is down again", now=NOW + 1)
        self.assertEqual(res.decision, "reject")

    def test_saying_it_is_a_person_after_all_unlearns_it(self):
        from app.services import graph
        pid = self.store.insert_person("Velloria Hub", ts=NOW)
        graph.reclassify_constellation_node(self.store, f"person:{pid}", "org")
        self.assertTrue(self.store.is_seat_nonperson_name("Velloria Hub"))
        graph.reclassify_constellation_node(self.store, f"person:{pid}",
                                            "person")
        self.assertFalse(self.store.is_seat_nonperson_name("Velloria Hub"))
        self.assertTrue(nq.is_plausible_person("Velloria Hub",
                                               store=self.store))

    def test_not_a_person_on_forget(self):
        junk = self.store.insert_person("Velloria Hub", ts=NOW)
        r = self.client.post(f"/people/{junk}/forget",
                             json={"not_a_person": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["learned"])
        self.assertIsNone(self.store.get_person(junk))
        self.assertFalse(nq.is_plausible_person("Velloria Hub",
                                                store=self.store))

    def test_a_plain_forget_teaches_nothing(self):
        gone = self.store.insert_person("Ada Lovelace", ts=NOW)
        r = self.client.post(f"/people/{gone}/forget")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["learned"])
        self.assertEqual(self.store.list_seat_vocab(), [])
        self.assertTrue(nq.is_plausible_person("Ada Lovelace",
                                               store=self.store))

    def test_the_person_card_offers_it(self):
        from app.api import profile_page
        src = Path(profile_page.__file__).read_text(encoding="utf-8")
        self.assertIn('data-pact="notperson"', src)
        self.assertIn("not_a_person:true", src)


# --------------------------------------------- one process, several seats --
class CrossStoreTests(_Case):
    """Tests and a multi-seat process hold several stores at once, one of
    them the shared store. A seat's words and learned names decide for that
    seat only, whichever store is the shared one."""

    def setUp(self):
        super().setUp()
        self.other = _mk(self.tmp, "other.db")
        self.addCleanup(self.other.close)
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        self.store.add_seat_vocab(WORD, "nonperson_token", source="user")
        self.store.record_not_person("Velloria Hub")
        # The seeded seat is the process's shared store.
        shared = patch.object(storage_mod, "_store", self.store)
        shared.start()
        self.addCleanup(shared.stop)

    def test_another_seat_judges_by_its_own_store(self):
        from app.services import ambient_cleanup as ac
        from app.services import people_pipeline as pp
        from app.services import projects
        from app.services.extractor import _chat_people_names
        from app.services.resolution import Resolver
        b = self.other
        self.assertTrue(nq.is_plausible_person("Zorblat Prime", store=b))
        self.assertTrue(nq.is_plausible_person("Velloria Hub", store=b))
        self.assertTrue(nq.is_person_shaped_entity_name("Zorblat Hughes",
                                                        store=b))
        self.assertFalse(nq.should_mint_as_entity("Zorblat Hughes",
                                                  "project", store=b))
        self.assertEqual(_chat_people_names(
            "Lunch with Zorblat Prime on Friday.", store=b), ["Zorblat Prime"])
        self.assertFalse(projects._looks_like_project(b, "Zorblat Hughes"))
        self.assertIsNotNone(Resolver(store=b).resolve_person(
            "Zorblat Prime", ts=NOW))
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
            res = pp.resolve_person_mention(
                "Velloria Hub", store=b, event_id=3,
                event_source="audio.whisper",
                text="Velloria Hub joined the call", now=NOW)
        self.assertNotEqual(res.decision, "reject")
        self.assertEqual(ac.plan_people(b), [])
        # ... while the shared seat itself still applies them.
        self.assertFalse(nq.is_plausible_person("Zorblat Prime"))
        self.assertFalse(nq.is_plausible_person("Velloria Hub"))
        self.assertIsNone(Resolver(store=self.store).resolve_person(
            "Zorblat Prime", ts=NOW))

    def test_the_other_seats_words_do_not_reach_the_shared_seat(self):
        self.other.add_seat_vocab("quenby", "never_person_token",
                                  source="user")
        self.other.record_not_person("Ada Lovelace")
        for store in (self.store, None):
            self.assertTrue(nq.is_plausible_person("Quenby Marsh",
                                                   store=store))
            self.assertTrue(nq.is_plausible_person("Ada Lovelace",
                                                   store=store))
        self.assertFalse(nq.is_plausible_person("Quenby Marsh",
                                                store=self.other))
        self.assertFalse(nq.is_plausible_person("Ada Lovelace",
                                                store=self.other))

    def test_close_and_reopen(self):
        path = self.store.db_path
        self.store.close()
        again = Store(db_path=path, audio_dir=Path(self.tmp) / "audio")
        self.addCleanup(again.close)
        self.assertFalse(nq.is_plausible_person("Zorblat Prime", store=again))
        self.assertFalse(nq.is_plausible_person("Velloria Hub", store=again))
        # The closed store (still the shared one here) reads as no
        # vocabulary once its cache runs out: no crash, code lists only.
        with patch.object(Store, "SEAT_VOCAB_TTL_S", 0.0):
            self.assertTrue(nq.is_plausible_person("Zorblat Prime"))
            self.assertTrue(nq.is_plausible_person("Zorblat Prime",
                                                   store=self.store))
            self.assertFalse(nq.is_plausible_person("Zorblat Prime",
                                                    store=again))


# ------------------------------------------- every gate reads its own store --
class EveryCallerReadsItsStoreTests(_Case):
    """The callers the gate tests above do not reach: each passes its own
    store, so a seeded word or a learned name decides there too (and the
    shared store, here none, is never what decides)."""

    def setUp(self):
        super().setUp()
        self.store.add_seat_vocab(WORD, "never_person_token", source="user")
        self.store.add_seat_vocab(WORD, "nonperson_token", source="user")

    def test_cleanup_keeps_what_the_seat_says_is_no_person(self):
        from app.services import ambient_cleanup as ac
        from app.services import graph
        # A two-word project the seat's word marks as no person: neither
        # rerouted to people (person-shaped) nor hidden as orphan junk.
        shaped = self.store.resolve_entity("Zorblat Hughes", "project",
                                           ts=NOW)
        # The user turned a person into a project: the entity keeps its
        # name although the hidden person row shares it.
        pid = self.store.insert_person("Velloria Hub", ts=NOW)
        res = graph.reclassify_constellation_node(
            self.store, f"person:{pid}", "project")
        self.assertTrue(res["ok"], res)
        planned = {e["id"]: e for e in ac.plan_entities(self.store)}
        self.assertNotIn(shaped, planned)
        self.assertNotIn(int(res["id"].split(":")[1]), planned)

    def test_cleanup_never_remints_a_learned_name_as_a_person(self):
        from app.services import ambient_cleanup as ac
        self.store.record_not_person("Velloria Hub")
        eid = self.store.resolve_entity("Velloria Hub", "project", ts=NOW)
        ac.apply(self.store, {"entities": [
            {"id": eid, "name": "Velloria Hub", "kind": "project",
             "action": "hide_person"}]})
        self.assertIsNone(self.store.find_person_exact("Velloria Hub"))

    def test_the_email_header_gate_reads_the_seat(self):
        from app.services import people_pipeline as pp
        text = ("From: Zorblat Prime <zp@quarrystone.com>\n"
                "To: Ana Ortiz <ana@quarrystone.com>\n\nHello")
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1",
                                     "QUILL_DATA_DIR": self.tmp}):
            out = pp.ingest_email_network(
                text, store=self.store, event_id=5,
                event_source="desktop.screen", window="Inbox - Outlook",
                now=NOW)
        self.assertTrue(out.get("ok"), out)
        self.assertIsNone(self.store.find_person_exact("Zorblat Prime"))
        with self.store._lock:
            seen = {r[0] for r in self.store._conn.execute(
                "SELECT raw_text FROM person_mentions")}
        # As when the word was in code: the header gate turns the name away
        # before the resolver, which would have logged a rejected mention.
        self.assertNotIn("Zorblat Prime", seen)
        self.assertIn("Ana Ortiz", seen)

    def test_the_exhaust_fallback_mint_reads_the_seat(self):
        from app.services import exhaust_ingest as ex
        from app.services import people_pipeline as pp
        from app.services.resolution import Resolver
        stats = {"zp@quarrystone.com": {"name": "Zorblat Prime",
                                        "last_seen": NOW},
                 "ana@quarrystone.com": {"name": "Ana Ortiz",
                                         "last_seen": NOW}}
        # The legacy resolver (People v2 off) answers "no person" without a
        # reject verdict, so the fallback mint's own gate decides.
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "0",
                                     "QUILL_DATA_DIR": self.tmp}), \
                patch.object(pp, "resolver", Resolver(store=self.store)), \
                patch.object(ex, "_ledger_path",
                             return_value=Path(self.tmp) / "l.json"):
            ex.apply_stats(self.store, stats, {}, messages=[], events=[],
                           now=NOW)
        self.assertIsNone(self.store.find_person_exact("Zorblat Prime"))
        self.assertIsNotNone(self.store.find_person_exact("Ana Ortiz"))

    def test_a_claim_subject_the_seat_marks_is_an_entity(self):
        from app.events import Event, Modality
        from app.services import chat_ingest, resolution
        from app.services.memory import memory
        import app.services.worker as worker_mod

        class _FakeWorker:
            def enqueue(self, kind, payload=None, *, unique=False):
                return 1
        text = "Zorblat Hughes costs $49 a month"
        facts = {"tasks": [], "commitments": [], "entities": [],
                 "relations": [], "claims": [
                     {"text": text, "confidence": 0.9, "source_span": text,
                      "assertion": "stated_by_other",
                      "subject": "Zorblat Hughes", "predicate": "costs",
                      "object": "$49"}]}
        for p in (
            patch.dict(os.environ, {
                "QUILL_DATA_DIR": self.tmp, "QUILL_HEADS": "off",
                "QUILL_USAGE_LEDGER": "0", "QUILL_ENTITY_CANDIDATES": "0"}),
            patch.object(resolution, "resolver", resolution.Resolver(
                store=self.store)),
            patch("app.storage.get_store", return_value=self.store),
            patch.object(memory, "_ensure_store", return_value=self.store),
            patch.object(worker_mod, "worker", _FakeWorker()),
            patch("app.services.extractor._index_fact", lambda *a, **k: None),
            patch("app.services.fact_gate._similar_active", return_value=[]),
        ):
            p.start()
            self.addCleanup(p.stop)
        eid = self.store.insert(Event(time=NOW, modality=Modality.TEXT,
                                      raw=text, summary=text,
                                      source="chat.user"))
        with patch("app.services.extractor.extractor._extract_text",
                   return_value=facts):
            chat_ingest.run_job({"event_id": eid, "text": text})
        self.assertIsNone(self.store.find_person_exact("Zorblat Hughes"))
        self.assertTrue(self.store.find_entity_exact("Zorblat Hughes"))


# ---------------------------------------------------------- the seed script --
def _script():
    spec = importlib.util.spec_from_file_location(
        "seed_seat_vocab", ROOT / "scripts" / "seed_seat_vocab.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SeedScriptTests(_Case):
    def setUp(self):
        super().setUp()
        self.seed = _script()
        self.terms = Path(self.tmp) / "words.txt"
        self.terms.write_text(
            "# a seat's own words\n"
            f"never_person_token {WORD}\n"
            f"nonperson_token {WORD.upper()}   # case folds\n\n"
            "corp_word quenby\n", encoding="utf-8")
        self.pid = self.store.insert_person("Zorblat Prime", ts=NOW)
        self.store.resolve_entity("Quenby Marsh", "project", ts=NOW)

    def run_main(self, *extra) -> int:
        return self.seed.main(["--db", str(self.store.db_path),
                               "--terms-file", str(self.terms), *extra])

    def test_parse(self):
        self.assertEqual(self.seed.parse_terms(self.terms.read_text()),
                         [("never_person_token", WORD),
                          ("nonperson_token", WORD), ("corp_word", "quenby")])
        for bad in ("surname zorblat", "never_person_token", "nonperson_name x",
                    "corp_word two words"):
            with self.assertRaises(ValueError, msg=bad):
                self.seed.parse_terms(bad)

    def test_a_dry_run_writes_nothing_and_shows_what_would_change(self):
        def dump():
            con = sqlite3.connect(f"file:{self.store.db_path}?mode=ro",
                                  uri=True)
            try:
                return list(con.iterdump())
            finally:
                con.close()
        rows_before = dump()
        before = self.store.db_path.read_bytes()
        rep = self.seed.plan(self.store.db_path,
                             self.seed.parse_terms(self.terms.read_text()))
        self.assertEqual(len(rep["new"]), 3)
        changed = {(c["name"], c["kind"]) for c in rep["changed"]}
        self.assertIn(("Zorblat Prime", "person"), changed)
        self.assertIn(("Quenby Marsh", "project"), changed)
        self.assertNotIn(("Ada Lovelace", "person"), changed)
        with patch("sys.stdout"):
            self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.store.list_seat_vocab(), [])
        self.assertEqual(self.store.db_path.read_bytes(), before)
        self.assertEqual(dump(), rows_before)
        self.assertEqual(list(Path(self.tmp).glob("*.bak-seat-vocab-*")), [])

    def test_apply_backs_up_then_seeds_once(self):
        with patch("sys.stdout"):
            self.assertEqual(self.run_main("--apply"), 0)
        backups = list(Path(self.tmp).glob("t.db.bak-seat-vocab-*"))
        self.assertEqual(len(backups), 1)
        con = sqlite3.connect(backups[0])
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT canonical_name FROM people")
                         .fetchall(), [("Zorblat Prime",)])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM seat_vocab")
                         .fetchone(), (0,))
        rows = {(r["term"], r["role"], r["source"])
                for r in self.store.list_seat_vocab()}
        self.assertEqual(rows, {(WORD, "never_person_token", "migration"),
                                (WORD, "nonperson_token", "migration"),
                                ("quenby", "corp_word", "migration")})
        # This (already open) store sees the rows once its cache expires.
        with patch.object(Store, "SEAT_VOCAB_TTL_S", 0.0):
            self.assertFalse(nq.is_plausible_person("Zorblat Prime",
                                                    store=self.store))
        rep = self.seed.plan(self.store.db_path,
                             self.seed.parse_terms(self.terms.read_text()))
        self.assertEqual((rep["new"], rep["changed"]), ([], []))
        with patch("sys.stdout"):
            self.assertEqual(self.run_main("--apply", "--source", "user"), 0)
        self.assertEqual(len(self.store.list_seat_vocab()), 3)
        # The second run wrote nothing, so it left no backup: the one there
        # is still the state from before seeding.
        self.assertEqual([p for p in Path(self.tmp).glob(
            "t.db.bak-seat-vocab-*") if not p.name.endswith(("-shm", "-wal"))],
            backups)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM seat_vocab")
                         .fetchone(), (0,))

    def test_an_earlier_backup_is_never_overwritten(self):
        with patch.object(self.seed.time, "strftime",
                          return_value="20260101T000000Z"):
            self.seed.apply(self.store.db_path, [("corp_word", "quenby")],
                            source="migration")
            self.seed.apply(self.store.db_path, [("corp_word", "brask")],
                            source="migration")
        names = sorted(p.name for p in
                       Path(self.tmp).glob("t.db.bak-seat-vocab-*"))
        self.assertEqual(names, ["t.db.bak-seat-vocab-20260101T000000Z",
                                 "t.db.bak-seat-vocab-20260101T000000Z-1"])
        first = sqlite3.connect(Path(self.tmp) / names[0])
        self.addCleanup(first.close)
        self.assertEqual(first.execute("SELECT COUNT(*) FROM seat_vocab")
                         .fetchone(), (0,))

    def test_a_read_only_store_is_refused_before_anything(self):
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            self.skipTest("root writes a read-only file")
        ro = Path(self.tmp) / "ro"
        ro.mkdir()
        db = ro / "quill.db"
        src = sqlite3.connect(self.store.db_path)
        dst = sqlite3.connect(db)
        src.backup(dst)
        src.close()
        dst.close()
        os.chmod(db, 0o444)
        self.addCleanup(os.chmod, db, 0o644)
        args = ["--db", str(db), "--terms-file", str(self.terms)]
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(self.seed.main(args), 0)          # dry run reads
            self.assertEqual(self.seed.main(args + ["--apply"]), 2)
        os.chmod(db, 0o644)
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        with patch("sys.stdout"), patch("sys.stderr"):
            self.assertEqual(self.seed.main(args + ["--apply"]), 2)
        os.chmod(ro, 0o755)
        self.assertEqual(sorted(p.name for p in ro.iterdir()
                                if "bak" in p.name or p.name == "audio"), [])
        con = sqlite3.connect(db)
        self.addCleanup(con.close)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM seat_vocab")
                         .fetchone(), (0,))

    def test_comments_blank_lines_tabs_crlf_and_a_bom(self):
        text = ("﻿# exported by an editor\r\n\r\n"
                "never_person_token\tZorblat  # tab, case\r\n"
                "   # indented comment\r\n"
                "corp_word quenby\r\ncorp_word quenby\r\n")
        self.terms.write_bytes(text.encode("utf-8"))
        with patch("sys.stdout"):
            self.assertEqual(self.run_main("--apply"), 0)
        self.assertEqual({(r["term"], r["role"])
                          for r in self.store.list_seat_vocab()},
                         {(WORD, "never_person_token"),
                          ("quenby", "corp_word")})

    def test_a_file_that_is_no_database_is_refused(self):
        junk = Path(self.tmp) / "junk.db"
        junk.write_text("not a database", encoding="utf-8")
        with patch("sys.stderr"):
            self.assertEqual(self.seed.main(
                ["--db", str(junk), "--terms-file", str(self.terms),
                 "--apply"]), 2)
        self.assertEqual(list(Path(self.tmp).glob("junk.db.bak-*")), [])

    def test_a_bad_file_or_database_stops_before_anything(self):
        self.terms.write_text("never_person_token\n", encoding="utf-8")
        with patch("sys.stderr"):
            self.assertEqual(self.run_main("--apply"), 2)
            self.assertEqual(self.seed.main(
                ["--db", str(Path(self.tmp) / "none.db"),
                 "--terms-file", str(self.terms)]), 2)
        self.assertEqual(self.store.list_seat_vocab(), [])
        self.assertEqual(list(Path(self.tmp).glob("*.bak-seat-vocab-*")), [])


if __name__ == "__main__":
    unittest.main()
