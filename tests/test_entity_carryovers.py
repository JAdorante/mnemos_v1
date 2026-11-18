"""Entity model, Phase 3 part A: carry-overs from the stage G4 check.

A1  the entity-kind prompt carries invented examples only (none from one
    seat's data); the kill-switch prompt and schema stay the base commit's,
    byte for byte.
A2  cleanup's whole-name junk words: placeholders hide an orphan row, screen
    chrome words only when nothing supports the row, and one seat's own
    junk names live in that seat's vocabulary (`junk_entity_name`).
A3  a learned "not a person" is lifted by the user naming the person
    (speaker label, task owner, onboarding, manual split, peer link, rename,
    alias, confirm, debrief card, their own identity), never by automation;
    "Not a person…" can be undone.
A4  an organisation's own mail is not a person: a display name that begins
    with the org label of its own sending domain and is that one word, or
    comes from a system mailbox, or has no given name in its name part. Real
    people at a domain named for them stay people. No vendor list.

Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes  # noqa: F401
import app.services.extractor  # noqa: F401
from app import storage as storage_mod
from app.services import ambient_cleanup as ac
from app.services import name_quality as nq
from app.services import people_pipeline as pp
from app.storage import Store

NOW = 1_700_000_000.0
ROOT = Path(__file__).resolve().parents[1]


def _mk(tmp: str) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_carry_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp,
                                      "QUILL_PEOPLE_V2": "1"})
        env.start()
        self.addCleanup(env.stop)
        # No shared store: a call without `store=` judges by code alone.
        shared = patch.object(storage_mod, "_store", None)
        shared.start()
        self.addCleanup(shared.stop)


# -------------------------------------------------------------------- A1 --
class PromptExampleTests(unittest.TestCase):
    # sha256 of the base commit's (c6ec1aa) prompt and schema: the kill
    # switch must give back exactly what the hosted LoRA seat was trained on.
    BASE_SYSTEM = ("cf3d762ed9c68d3d59ffecf4e68989a3"
                   "3e9ae4dec2e50176c6fbf19a9a091de1")
    BASE_SCHEMA = ("e32ae80e0b92b768d0ef74bf51066248"
                   "1482e08f3d1972bcfa6028a836d37dba")
    # The plan's own example names: one seat's milestone id and junk phrases.
    SEAT_EXAMPLES = ("'r2'", "funeral homes", "ski resort",
                     "industrial laundry", "'option B'", "'M2'", "'Phase 1'")

    def test_the_kill_switch_prompt_and_schema_are_the_base_commits(self):
        from app.services import extractor as ex
        self.assertEqual(hashlib.sha256(ex._SYSTEM_V1.encode()).hexdigest(),
                         self.BASE_SYSTEM)
        self.assertEqual(hashlib.sha256(json.dumps(
            ex._SCHEMA_V1, sort_keys=True).encode()).hexdigest(),
            self.BASE_SCHEMA)
        with patch.dict(os.environ, {"QUILL_EXTRACT_ENTITY_V2": "0"}):
            self.assertIs(ex.base_system(), ex._SYSTEM_V1)
            self.assertEqual(ex.entity_kind_rules(), "")

    def test_the_kind_lines_carry_invented_examples_only(self):
        from app.services import extractor as ex
        from app.services import screen_extract
        prompts = (ex.ENTITY_KIND_LINES, ex._SYSTEM,
                   screen_extract.system_prompt())
        for text in prompts:
            for seat in self.SEAT_EXAMPLES:
                self.assertNotIn(seat, text)
        for invented in ("'Wave 4'", "'Sprint 9'", "'option F'", "'row 27'",
                         "'the pet groomers'", "'a bowling", "alley'",
                         "'commercial beekeeping'"):
            self.assertIn(invented, ex.ENTITY_KIND_LINES)
        self.assertIn(ex.ENTITY_KIND_LINES, screen_extract.system_prompt())
        # Not deployed yet: the new prompt keeps its version stamp.
        self.assertEqual(ex.EXTRACT_PROMPT_VERSION, "extract-v4")


# -------------------------------------------------------------------- A2 --
class JunkWordTests(_Case):
    def orphan(self, name: str, kind: str = "idea", source=None) -> int:
        return self.store.resolve_entity(name, kind, ts=NOW,
                                         kind_source=source)

    def fact(self, text: str) -> int:
        with self.store._lock:
            cur = self.store._conn.execute(
                "INSERT INTO facts (kind, text, extracted_at) "
                "VALUES ('claim', ?, ?)", (text, NOW))
            self.store._conn.commit()
        return int(cur.lastrowid)

    def planned(self) -> dict:
        return {e["name"]: e["reason"] for e in ac.plan_entities(self.store)}

    def test_a_placeholder_hides_an_orphan_row(self):
        self.orphan("Unknown organization")
        self.orphan("not specified")
        self.assertEqual(self.planned(), {"Unknown organization": "orphan_junk",
                                          "not specified": "orphan_junk"})

    def test_a_chrome_word_hides_only_a_row_nothing_supports(self):
        self.orphan("Messages")
        self.orphan("Login")
        self.fact("The login page timed out twice on Monday.")
        self.orphan("Capture", source="user")    # the user added it
        planned = self.planned()
        self.assertEqual(planned.get("Messages"), "orphan_junk")
        self.assertNotIn("Login", planned)        # a fact names it
        self.assertNotIn("Capture", planned)      # the user made it
        # A sighting bound to the row supports it too.
        eid = self.store.find_entity_exact("Messages")
        self.store.record_entity_mention(
            "Messages", proposed_kind="tool", confidence=0.8,
            source_class="connector_mail", event_source="google.mail",
            event_id=7, observed_at=NOW, entity_id=eid)
        self.assertNotIn("Messages", self.planned())

    def test_a_row_the_user_or_a_filing_made_is_never_junk_by_name(self):
        self.assertTrue(self.store.add_seat_vocab(
            "renewal", "junk_entity_name", source="migration"))
        self.orphan("Renewal", source="user")
        self.orphan("Unknown", source="filing")
        self.orphan("not specified")
        self.assertEqual(self.planned(), {"not specified": "orphan_junk"})

    def test_support_is_found_past_many_partial_matches(self):
        self.orphan("Capture")
        for i in range(250):
            self.fact(f"Captured frame {i} from the yard camera.")
        self.assertEqual(self.planned().get("Capture"), "orphan_junk")
        self.fact("Capture is where the intake notes start.")
        self.assertNotIn("Capture", self.planned())

    def test_a_generic_word_that_names_things_elsewhere_hides_nothing(self):
        # "Renewal" on an insurance seat, "Flight" on an airline's: on a seat
        # that never said so, an orphan row with that name stays.
        self.orphan("Renewal")
        self.orphan("Flight")
        self.assertEqual(self.planned(), {})

    def test_a_seats_own_junk_names_hide_as_before(self):
        self.orphan("Renewal")
        self.orphan("Q3 Renewal plan", "project")
        self.assertTrue(self.store.add_seat_vocab(
            "  Renewal ", "junk_entity_name", source="migration"))
        self.assertEqual(self.store.seat_vocab()["junk_entity_name"],
                         frozenset({"renewal"}))
        planned = self.planned()
        self.assertEqual(planned.get("Renewal"), "orphan_junk")
        self.assertNotIn("Q3 Renewal plan", planned)     # whole names only
        # A junk name may be several words, never a sentence.
        self.assertEqual(Store.seat_vocab_term("Old  Pier Notes",
                                               "junk_entity_name"),
                         "old pier notes")
        self.assertIsNone(Store.seat_vocab_term(
            "a b c d e f g", "junk_entity_name"))
        with self.assertRaises(ValueError):
            self.store.add_seat_vocab("x", "junk_entity_name",
                                      source="correction")

    def test_the_seed_script_takes_junk_names(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "seed_seat_vocab_carry", ROOT / "scripts" / "seed_seat_vocab.py")
        seed = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(seed)
        self.assertEqual(seed.parse_terms(
            "junk_entity_name Renewal\njunk_entity_name old pier notes\n"),
            [("junk_entity_name", "renewal"),
             ("junk_entity_name", "old pier notes")])
        self.orphan("Renewal")
        rep = seed.plan(self.store.db_path,
                        [("junk_entity_name", "renewal")])
        changed = {(c["name"], tuple(c["before"]), tuple(c["after"]))
                   for c in rep["changed"]}
        # person / person-shaped / mints / junk / heading junk / contained
        # (the last two are `context.surfaces`, Phase 4 B1).
        self.assertIn(("Renewal", (True, False, True, False, False, True),
                       (True, False, True, True, False, True)), changed)
        self.assertEqual(self.store.list_seat_vocab(), [])   # dry run


# -------------------------------------------------------------------- A3 --
class NotAPersonLiftTests(_Case):
    NAME = "Quenby Marsh"

    def learn(self) -> None:
        self.assertTrue(self.store.record_not_person(self.NAME))
        self.assertFalse(nq.is_plausible_person(self.NAME, store=self.store))

    def lifted(self) -> bool:
        return nq.is_plausible_person(self.NAME, store=self.store)

    def test_automated_proposals_stay_blocked(self):
        self.learn()
        res = pp.resolve_person_mention(self.NAME, store=self.store,
                                        event_source="google.mail",
                                        text=f"{self.NAME} wrote", now=NOW)
        self.assertIsNone(res.person_id)
        from app.services.resolution import Resolver
        self.assertIsNone(Resolver(store=self.store).resolve_person(self.NAME))
        self.store.resolve_person(self.NAME, ts=NOW)     # no user: no lift
        self.assertFalse(self.lifted())
        self.assertFalse(self.store.user_named_person("Someone Else"))

    def test_a_user_mint_lifts_it_and_a_second_lift_writes_nothing(self):
        self.learn()
        pid = self.store.resolve_person(self.NAME, ts=NOW, by_user=True)
        self.assertTrue(pid)
        self.assertTrue(self.lifted())
        n = self.store._conn.total_changes
        self.assertFalse(self.store.user_named_person(self.NAME))
        self.assertEqual(self.store._conn.total_changes, n)

    def test_the_speaker_label_lifts_it_for_the_user_only(self):
        from app.services import people_escrow
        self.learn()
        self.store.get_or_create_speaker_track("Speaker 3", ts=NOW)
        people_escrow.label_speaker(self.store, "Speaker 3", self.NAME,
                                    actor="system", now=NOW)
        self.assertFalse(self.lifted())
        self.store.get_or_create_speaker_track("Speaker 4", ts=NOW)
        res = people_escrow.label_speaker(self.store, "Speaker 4", self.NAME,
                                          now=NOW)
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.lifted())

    def test_onboarding_answers_lift_it(self):
        from app.services import onboarding
        self.learn()
        onboarding._Ingestor(self.store, set(), NOW).person(self.NAME, [])
        self.assertTrue(self.lifted())

    def test_onboarding_identity_lifts_it(self):
        from app.services import onboarding
        self.learn()
        with patch.object(onboarding, "save_profile"), \
                patch.object(onboarding, "_load_state", return_value={}), \
                patch.object(onboarding, "_save_state"):
            onboarding.ingest({"identity": {"name": self.NAME}},
                              store=self.store)
        self.assertTrue(self.lifted())

    def test_the_users_own_identity_lifts_it(self):
        from app.services import self_profile
        self.learn()
        self_profile.reset()
        self.addCleanup(self_profile.reset)
        with patch("app.services.identity.user_identity",
                   return_value={"name": self.NAME}):
            self.assertTrue(self_profile.self_person_id(self.store))
        self.assertTrue(self.lifted())

    def test_a_manual_split_lifts_it_for_the_user_only(self):
        from app.services import kg_beliefs
        old = self.store.resolve_person("Ines Marlow", ts=NOW)
        self.learn()
        kg_beliefs.manual_split(self.store, node_type="person", node_id=old,
                                new_name=self.NAME, predicate_ids=[],
                                decided_by="adjudicator", now=NOW)
        self.assertFalse(self.lifted())
        kg_beliefs.manual_split(self.store, node_type="person", node_id=old,
                                new_name=self.NAME, predicate_ids=[],
                                decided_by="user", now=NOW)
        self.assertTrue(self.lifted())

    def test_the_debrief_card_lifts_it(self):
        from app.services import meeting_debrief
        self.learn()
        out = meeting_debrief.set_relationship(self.store, name=self.NAME,
                                               email="", relationship="")
        self.assertTrue(out["ok"], out)
        self.assertTrue(self.lifted())

    def test_a_peer_link_create_lifts_it_and_passes_the_gate(self):
        from app.services import peer_channel
        from app.services.memory import memory
        peers = Path(self.tmp) / "peers.json"
        peers.write_text(json.dumps({"p1": {"name": self.NAME}}))
        self.learn()
        with patch.object(peer_channel, "_peers_path", return_value=peers), \
                patch.object(memory, "_ensure_store", return_value=self.store):
            res = peer_channel.create_and_link_person("p1")
        self.assertTrue(res["ok"], res)
        self.assertTrue(self.lifted())


class NotAPersonRouteTests(_Case):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.adoption import router as adoption_router
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        app.include_router(adoption_router)
        cls.client = TestClient(app)

    def setUp(self):
        super().setUp()
        import app.api.routes as routes_mod
        from app.services.memory import memory
        for p in (patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store),
                  patch.object(memory, "_ensure_store",
                               return_value=self.store),
                  patch.object(routes_mod.memory, "index_fact"),
                  patch("app.services.worker.worker.enqueue")):
            p.start()
            self.addCleanup(p.stop)

    def learned(self, name: str) -> bool:
        return self.store.is_seat_nonperson_name(name)

    def test_not_a_person_then_undo(self):
        pid = self.store.insert_person("Quenby Marsh", ts=NOW)
        r = self.client.post(f"/people/{pid}/forget",
                             json={"not_a_person": True})
        self.assertTrue(r.json()["learned"])
        self.assertTrue(self.learned("Quenby Marsh"))
        r = self.client.post("/people/not-a-person/undo",
                             json={"name": " quenby  marsh "})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["unlearned"])
        self.assertFalse(self.learned("Quenby Marsh"))
        self.assertFalse(self.client.post(
            "/people/not-a-person/undo",
            json={"name": "Quenby Marsh"}).json()["unlearned"])
        self.assertEqual(self.client.post(
            "/people/not-a-person/undo", json={"name": " "}).status_code, 400)

    def test_the_routes_where_the_user_names_a_person_lift_it(self):
        for name in ("Ines Marlow", "Odile Brack", "Tamsin Rowe",
                     "Pell Avery"):
            self.store.record_not_person(name)
        # A task owner the user typed.
        r = self.client.post("/work/add", json={
            "kind": "task", "text": "Send the deck", "owner": "Ines Marlow"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.learned("Ines Marlow"))
        # Renaming a person to the name, adding it as an alias, confirming.
        pid = self.store.insert_person("Odile B", ts=NOW)
        self.client.post(f"/people/{pid}/rename", json={"name": "Odile Brack"})
        self.assertFalse(self.learned("Odile Brack"))
        self.client.post(f"/people/{pid}/alias", json={"alias": "Tamsin Rowe"})
        self.assertFalse(self.learned("Tamsin Rowe"))
        pid2 = self.store.insert_person("Pell Avery", ts=NOW)
        r = self.client.post(f"/people/{pid2}/confirm",
                             json={"state": "contact"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.learned("Pell Avery"))

    def test_enrolling_a_voice_lifts_it(self):
        import wave
        from types import SimpleNamespace

        import numpy as np
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api.web_ingest import router as web_router
        from app.config import settings
        web = FastAPI()
        web.include_router(web_router)
        sr = settings.audio.sample_rate
        stub = SimpleNamespace(enroll=lambda *a: None,
                               enrolled_names=lambda: ["Wren Okafor"])
        self.store.record_not_person("Wren Okafor")
        with patch("app.services.capture_consent.allows", return_value=True), \
                patch("app.services.speakers.speakers", stub), \
                patch.object(storage_mod, "_store", self.store):
            r = TestClient(web).post(
                "/speakers/enroll/web?name=Wren%20Okafor",
                content=np.zeros(sr * 4, "<i2").tobytes())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.learned("Wren Okafor"))
        # The file route as well.
        self.store.record_not_person("Wren Okafor")
        wav = Path(self.tmp) / "voice.wav"
        with wave.open(str(wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(b"\0\0" * sr)
        with patch("app.services.speakers.speakers", stub):
            r = self.client.post("/speakers/enroll", json={
                "name": "Wren Okafor", "wav_path": str(wav)})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(self.learned("Wren Okafor"))

    def test_the_people_tab_offers_the_undo(self):
        from app.api import profile_page
        src = profile_page.PROFILE_PAGE
        self.assertIn('id="pNotice"', src)
        self.assertIn("fetch('/people/not-a-person/undo'", src)
        self.assertIn('data-pundo="1"', src)


# -------------------------------------------------------------------- A4 --
class OrgOwnMailTests(_Case):
    def test_the_rule(self):
        org = [("Vendrix", "hello@vendrix.com"),
               ("Vendrix Cloud", "cloud-noreply@vendrix.com"),
               ("Vendrix Payments", "billing@vendrix.com"),
               ("Vendrix account team", "acct@vendrix.com"),
               ("Vendrix Workspace Alerts", "ws@vendrix.com"),
               ("Acme Robotics Support", "jo@acme-robotics.com"),
               ("Acme Robotics", "jo@acme-robotics.com")]
        person = [("Ana Brightfield", "ana@brightfield.example"),
                  ("Brightfield Ana", "ana@brightfield.example"),
                  ("Grace Chen", "grace.chen@grace.org"),
                  ("Luca de Rossi", "luca.derossi@luca.example"),
                  ("Acme Jo", "jo@acme-robotics.com"),
                  ("Jo Park", "jo@vendrix.com"),
                  # A free-mail domain names no org.
                  ("Gmailo Rivers", "gmailo@gmail.com")]
        for name, addr in org:
            self.assertTrue(pp.org_own_mail(addr, name), (name, addr))
            self.assertTrue(pp.is_automated_sender(addr, name), (name, addr))
        for name, addr in person:
            self.assertFalse(pp.org_own_mail(addr, name), (name, addr))
            self.assertFalse(pp.is_automated_sender(addr, name), (name, addr))

    def test_real_people_the_rule_must_not_take(self):
        person = [
            # A full name that is the whole domain label (a sole trader's
            # own domain), from a personal or a staffed mailbox.
            ("Ana Brightfield", "ana@anabrightfield.example"),
            ("Ana Brightfield", "contact@anabrightfield.example"),
            # Surname first at a family firm named for the family, from the
            # firm's staffed mailboxes too.
            ("Rossi Marco", "marco@rossi.example"),
            ("Rossi Marco", "office@rossi.example"),
            ("Morgan Ellis", "press@morgan.example"),
            ("Brightfield, Ana", "ana@brightfield.example"),
            # A title after the name is not part of it.
            ("Rossi Marco | Sales Manager", "marco@rossi.example"),
            ("Brightfield Ana, Project Lead", "ana@brightfield.example"),
            ("Rossi Marco (Owner)", "marco@rossi.example"),
            # An all-lowercase display name says nothing by its case.
            ("grace chen", "grace@grace.example"),
            # Non-Latin names and surname-first capitals.
            ("张伟", "wei@zhang.example"),
            ("ZHANG Wei", "wei@zhang.example"),
        ]
        for name, addr in person:
            self.assertFalse(pp.org_own_mail(addr, name), (name, addr))
            self.assertFalse(pp.is_automated_sender(addr, name), (name, addr))
        org = [("Vendrix", "vendrix@vendrix.com"),
               ("Vendrix - Billing", "ar@vendrix.com"),
               ("Vendrix (Payments)", "ar@vendrix.com"),
               ("Acme Robotics", "noreply@acmerobotics.com"),
               ("Vendrix Cloud", "cloud-alerts@vendrix.com"),
               ("vendrix account team", "acct@vendrix.com")]
        for name, addr in org:
            self.assertTrue(pp.org_own_mail(addr, name), (name, addr))

    def test_another_mailbox_under_the_same_name_decides_nothing(self):
        # The org's own system sending under a person's display name (a web
        # shop mailing as its owner) must not take the owner's own address
        # with it: each sender is read on its own.
        text = ("From: Rossi Marco <noreply@rossi.example>\n"
                "To: Ana Brightfield <ana@brightfield.example>\n"
                "Cc: Rossi Marco <marco@rossi.example>\n\nOrder 12 shipped")
        out = pp.ingest_email_network(
            text, store=self.store, event_id=98,
            event_source="desktop.screen", window="Inbox - Ana - Outlook",
            now=NOW)
        self.assertTrue(out.get("ok"), out)
        self.assertFalse(hasattr(pp, "org_sender_names"))
        self.assertTrue(pp.org_own_mail("noreply@rossi.example",
                                        "Rossi Marco"))
        self.assertFalse(pp.org_own_mail("marco@rossi.example",
                                         "Rossi Marco"))
        people = {p["name"] for p in self.store.all_people()}
        self.assertIn("Rossi Marco", people)

    def test_no_vendor_subdomain_list(self):
        # Only generic mail-system labels; a vendor's own subdomain is read
        # by the rule, not listed.
        for label in pp._AUTOMATED_SUBDOMAINS:
            self.assertLessEqual(len(label), 13, label)
        self.assertNotIn("accountprotection", pp._AUTOMATED_SUBDOMAINS)
        self.assertTrue(pp.is_automated_sender(
            "account-security-noreply@security.vendrix.com",
            "Vendrix account team"))

    def test_the_email_header_ingest_mints_no_org_mail(self):
        text = ("From: Vendrix Cloud <cloud-noreply@vendrix.com>\n"
                "To: Ana Brightfield <ana@brightfield.example>\n"
                "Cc: Vendrix Cloud <cloud.alerts@vendrix.com>, "
                "Vendrix Payments <payments@vendrix.com>\n\nHello")
        out = pp.ingest_email_network(
            text, store=self.store, event_id=99,
            event_source="desktop.screen", window="Inbox - Ana - Outlook",
            now=NOW)
        self.assertTrue(out.get("ok"), out)
        people = {p["name"] for p in self.store.all_people()}
        self.assertIn("Ana Brightfield", people)
        self.assertFalse({"Vendrix Cloud", "Vendrix Payments"} & people,
                         people)

    def test_the_exhaust_ingest_mints_no_org_mail(self):
        from app.services import exhaust_ingest as ex
        stats = {"cloud-noreply@vendrix.com": {"name": "Vendrix Cloud",
                                               "last_seen": NOW},
                 "cloud.alerts@vendrix.com": {"name": "Vendrix Cloud",
                                              "last_seen": NOW},
                 "ar@vendrix.com": {"name": "Vendrix (Billing)",
                                    "last_seen": NOW},
                 "ana@brightfield.example": {"name": "Ana Brightfield",
                                             "last_seen": NOW}}
        with patch.object(ex, "_ledger_path",
                          return_value=Path(self.tmp) / "l.json"):
            ex.apply_stats(self.store, stats, {}, messages=[], events=[],
                           now=NOW)
        people = {p["name"] for p in self.store.all_people()}
        self.assertIn("Ana Brightfield", people)
        self.assertNotIn("Vendrix Cloud", people)
        self.assertNotIn("Vendrix (Billing)", people)


if __name__ == "__main__":
    unittest.main()
