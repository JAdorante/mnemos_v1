"""Regression tests for the six systemic patterns found in the 2026-11-11 audit of
User 1's store.  Each test replays the cited source event's text through the real
code path with the LLM mocked.  They are EXPECTED TO FAIL on the current tree; they
pass once the proposed minimal changes land.  Proposed homes are noted per class.
Run: cd <repo> && QUILL_DATA_DIR=<tmp> QUILL_HEADS=off python3 -m unittest <this file>
"""
from __future__ import annotations
import os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from app.events import Event, Modality
from app.storage import Store

NOW = 1_790_000_000.0


def _fresh_store(tmp):
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


# ---------------------------------------------------------------- Pattern A (tests/test_kg_beliefs.py)
class UploadBriefGateTests(unittest.TestCase):
    """ev292 'Comau acquired Invent Smart Intralogistics Solutions' / ev199 'EU CBAM' / 'ycombinator. com'."""
    def test_brief_upload_asserts_no_part_of_no_project_no_domain(self):
        from app.services.extractor import Extractor
        from app.services import resolution
        span = "Comau acquired Invent Smart Intralogistics Solutions (ycombinator. com)"
        facts = {"entities": [
            {"name": "Comau", "kind": "org", "confidence": .9, "source_span": "Comau"},
            {"name": "Invent Smart Intralogistics Solutions", "kind": "project", "confidence": .9,
             "source_span": "Invent Smart Intralogistics Solutions"},
            {"name": "EU CBAM", "kind": "project", "confidence": .9, "source_span": "EU CBAM"}],
            "relations": [
            {"subject": "Comau", "subject_kind": "entity", "predicate": "part_of",
             "object": "Invent Smart Intralogistics Solutions", "object_kind": "entity", "confidence": .8, "source_span": span},
            {"subject": "Comau", "subject_kind": "entity", "predicate": "part_of",
             "object": "ycombinator. com", "object_kind": "entity", "confidence": .8, "source_span": span}]}
        with tempfile.TemporaryDirectory() as td:
            store = _fresh_store(td)
            try:
                import app.storage as storage_mod
                with patch.object(storage_mod, "get_store", return_value=store), \
                     patch.object(resolution, "resolver", resolution.Resolver(store=store)), \
                     patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
                    Extractor(store)._persist_entities(facts, None, NOW, event_source="onboarding.upload",
                                                       window="Executive brief _ Robotics.pdf", text=span)
                comau = store.find_entity_exact("Comau")
                self.assertIsNotNone(comau)
                out = store.relations_of("entity", comau)["out"]
                self.assertFalse(any(r["predicate"] == "part_of" for r in out), out)      # acquirer is never part_of acquiree
                self.assertIsNone(store.find_entity_exact("ycombinator. com"))             # citation domain not minted
                self.assertEqual([e["name"] for e in store.all_entities() if e["kind"] == "project"], [])  # brief text defines no projects
            finally:
                store.close()


# ---------------------------------------------------------------- Pattern B (tests/test_exhaust_ingest.py)
class HeaderSeedHygieneTests(unittest.TestCase):
    """ev277/ev325 'Seeded people from email headers (no bodies)'."""
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ex_"))
        self.env = patch.dict(os.environ, {"QUILL_DATA_DIR": str(self.tmp), "QUILL_EXHAUST_INGEST": "1"}, clear=False)
        self.env.start(); self.addCleanup(self.env.stop)
        self.store = _fresh_store(self.tmp); self.addCleanup(self.store.close)

    def test_header_seed_rejects_bots_local_parts_and_subdomain_orgs(self):
        from app.services import exhaust_ingest as ex
        msgs = [
            {"id": "<a>", "ts": 100.0, "headers": {"from": "Google <no-reply@accounts.google.com>", "to": "me@villanova.edu"}},
            {"id": "<b>", "ts": 101.0, "headers": {"from": "Microsoft Learn <learn@mails.microsoft.com>", "to": "me@villanova.edu"}},
            {"id": "<c>", "ts": 102.0, "headers": {"from": "ckempton@docnetwork.org", "to": "me@villanova.edu"}},
            {"id": "<d>", "ts": 103.0, "headers": {"from": "Hugh Salva <hughsalva@college.harvard.edu>",
                                                   "to": "me@villanova.edu, Pat Lee <plee@villanova.edu>"}},
        ]
        with patch.object(ex, "_self_emails", return_value=["me@villanova.edu"]):
            out = ex.run_ingest(store=self.store, messages=msgs, events=[], fetch=False)
        self.assertTrue(out.get("ok"), out)
        names = {p["name"] for p in self.store.all_people()}
        for bad in ("Google", "Microsoft Learn", "Ckempton"):
            self.assertNotIn(bad, names)
        ents = {e["id"]: e["name"] for e in self.store.all_entities()}
        for bad in ("Accounts", "Mails", "College"):
            self.assertNotIn(bad, ents.values())
        for p in self.store.all_people():
            for r in self.store.relations_of("person", p["id"])["out"]:
                if r["predicate"] == "works_at":
                    self.assertNotIn(ents.get(r["obj_id"]), ("Villanova", "Harvard"))   # students, not employees


# ---------------------------------------------------------------- Pattern C (tests/test_screen_extract.py)
class ScreenEmailOcrHygieneTests(unittest.TestCase):
    """ev310: Outlook OCR with 'To: Adorante, Patrick <patrick.adorante@dell.com>; Justin Adorante; +1 other'."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.store = _fresh_store(self.tmp); self.addCleanup(self.store.close)
        import app.storage as storage_mod
        from app.services import resolution
        for p in (patch.object(storage_mod, "get_store", return_value=self.store),
                  patch.object(resolution, "resolver", resolution.Resolver(store=self.store)),
                  patch("app.services.identity.user_identity", return_value={"name": "Justin Adorante"})):
            p.start(); self.addCleanup(p.stop)
        self.me = self.store.insert_person("Justin Adorante", ts=NOW, promotion_state="active")
        self.store.insert_person("Andrew Karos", ts=NOW, promotion_state="active")
        self.vince = self.store.insert_person("Vince Fong", ts=NOW, promotion_state="active")

    def test_inbox_ocr_does_not_mint_people_as_tools_or_header_works_at(self):
        from app.services import screen_extract
        to_line = "To: Adorante, Patrick <patrick.adorante@dell.com>; Justin Adorante; +1 other"
        text = "From: Vince Fong\nSubject: Re: Intro\n" + to_line + "\nOther Emails (3): SportsLine; The Augustinians; Grubhub\n" + ("x" * 200)
        self.store.insert(Event(time=NOW, modality=Modality.VISION, raw=text, summary=text[:40], source="desktop.screen",
                                meta={"window": "Inbox - Justin Adorante - Outlook"}))
        facts = {"tasks": [], "commitments": [], "claims": [], "questions": [],
                 "entities": [{"name": "Justin Adorante", "kind": "product", "confidence": .9, "source_span": "Justin Adorante"},
                              {"name": "Andrew Karos", "kind": "product", "confidence": .9, "source_span": "Andrew Karos"},
                              {"name": "Grubhub", "kind": "place", "confidence": .9, "source_span": "Grubhub"}],
                 "relations": [{"subject": n, "subject_kind": "person", "predicate": "works_at", "object": "Dell",
                                "object_kind": "entity", "confidence": .8, "source_span": to_line}
                               for n in ("Justin Adorante", "Vince Fong")]}
        import app.services.worker as worker_mod
        with patch.object(screen_extract, "_extract", return_value=facts), \
             patch.object(worker_mod, "worker", type("W", (), {"enqueue": lambda *a, **k: 1})()), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            screen_extract.run_once()
        names = {(e["name"] or "").lower() for e in self.store.all_entities(include_hidden=True)}
        self.assertNotIn("justin adorante", names); self.assertNotIn("andrew karos", names)   # people never re-minted as entities
        self.assertNotIn("grubhub", names)                                                     # inbox sender list is not a place
        dell = self.store.find_entity_exact("Dell")
        for pid in (self.me, self.vince):
            out = self.store.relations_of("person", int(pid)).get("out") or []
            self.assertFalse(any(e["predicate"] == "works_at" and dell and int(e["obj_id"]) == int(dell) for e in out), out)


# ---------------------------------------------------------------- Pattern D (tests/test_meeting_session.py / test_extractor.py)
class SelfChannelVerificationTests(unittest.TestCase):
    """ev257 carries speaker.decision='self_channel', similarity 1.0, with no voiceprint compared."""
    def setUp(self):
        self.td = tempfile.TemporaryDirectory(); self.addCleanup(self.td.cleanup)
        p = patch("app.services.identity.user_identity", return_value={"name": "Justin Adorante"}); p.start(); self.addCleanup(p.stop)

    def test_mic_audio_not_matching_user_voiceprint_is_not_the_user(self):
        from app.services.speakers import SpeakerIdentifier
        spk = SpeakerIdentifier(voiceprint_dir=Path(self.td.name))
        me = np.array([1.0, 0.0, 0.0], dtype=np.float32); lecture = np.array([0.0, 1.0, 0.0], dtype=np.float32)
        spk._voiceprints["Justin Adorante"] = me
        r = spk.identify_embedding(lecture, space="self")
        self.assertFalse(r["is_known"], r); self.assertNotEqual(r.get("name"), "Justin Adorante")
        self.assertEqual(spk.identify_embedding(me, space="self")["name"], "Justin Adorante")


class MicFirstPersonNotSelfTests(unittest.TestCase):
    """ev257 text 'when Slack was going public. I was on the board' from an unidentified speaker."""
    def test_unlabeled_speaker_board_seat_is_not_user_works_at(self):
        from app.services import extractor as ex
        td = tempfile.TemporaryDirectory(); self.addCleanup(td.cleanup)
        store = _fresh_store(td.name); self.addCleanup(store.close)
        with patch("app.services.identity.user_identity", return_value={"name": "Justin Adorante"}), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            from app.services import self_profile
            me = self_profile.self_person_id(store)
            text = "when Slack was going public. I was on the board"
            eid = store.insert(Event(time=NOW, modality=Modality.AUDIO, raw=text, summary=text, source="audio.web_mic", people=["Speaker 1"]))
            ex.persist_facts(store, {"tasks": [], "commitments": [], "questions": [], "entities": [],
                                     "claims": [{"text": "I was on the board of Slack", "confidence": .8, "source_span": "I was on the board", "assertion": "asserted"}],
                                     "relations": [{"subject": "me", "subject_kind": "person", "predicate": "works_at", "object": "Slack",
                                                    "object_kind": "entity", "confidence": .8, "source_span": "I was on the board"}]},
                             eid, text, NOW, event_source="audio.web_mic", speaker="Speaker 1", index=False)
        out = store.relations_of("person", int(me)).get("out") or []
        self.assertFalse(any(e["predicate"] in ("works_at", "about_self") for e in out), out)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- Pattern E (tests/test_name_only_resolution.py)
class NameOnlyResolutionTests(unittest.TestCase):
    """ev325 'Matt <matt@shyftoff.com>', ev324 survey sentence, ev185 two Daves, ev198 'ABB' vs 'Abby'."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.store = _fresh_store(self.tmp); self.addCleanup(self.store.close)

    def _out(self, t, i, pred):
        return [e for e in self.store.relations_of(t, i)["out"] if e["predicate"] == pred]

    def test_exhaust_bare_address_and_bots_do_not_mint(self):
        # Reconciled with tests/test_exhaust_ingest.py, which requires an explicit
        # single-token display name ("Ada <ada@acme.io>") to still mint. Only a
        # parser-invented name (bare address) and automated senders are blocked.
        from app.services import exhaust_ingest as ex
        with patch.object(ex, "_ledger_path", return_value=Path(self.tmp) / "l.json"):
            ex.apply_stats(self.store, {"ckempton@docnetwork.org": {"name": "Ckempton", "name_from_local": True, "last_seen": NOW},
                                        "no-reply@accounts.google.com": {"name": "Google", "last_seen": NOW}},
                           {}, messages=[], events=[], now=NOW)
        names = {p["name"] for p in self.store.list_people_embed()}
        self.assertNotIn("Ckempton", names); self.assertNotIn("Google", names)

    def test_person_token_inside_tool_name_is_not_a_hit(self):
        from app.services import graph
        g = self.store.insert_person("Google", ts=NOW)
        for n in ("Excel", "Google Sheets", "Google Calendar"):
            self.store.resolve_entity(n, "tool", ts=NOW)
        self.store.add_claim("Tools and apps the user uses include: Excel, Google Sheets, ChatGPT, Google Calendar", extracted_at=NOW)
        graph.rebuild(self.store)
        self.assertEqual(self._out("person", g, "associated_with"), [])

    def test_first_name_project_not_matched_inside_full_names(self):
        from app.services import graph
        dave = self.store.resolve_entity("Dave", "project", ts=NOW)
        self.store.insert_person("Dave Cappell", ts=NOW); self.store.insert_person("Dave Randal", ts=NOW)
        f = self.store.add_claim("not with Dave Randal but with Dave Cappell", extracted_at=NOW)
        graph.rebuild(self.store)
        self.assertNotIn(dave, [e["obj_id"] for e in self._out("fact", f, "about")])

    def test_acronym_not_aliased_onto_soundex_twin(self):
        from app.services.resolution import Resolver
        abby = self.store.resolve_entity("Abby", "project", ts=NOW)
        eid = Resolver(store=self.store).resolve_entity("ABB", "org", ts=NOW)
        self.assertNotEqual(eid, abby)
        self.assertNotIn("ABB", self.store.get_entity(abby)["aliases"])


# ---------------------------------------------------------------- Pattern F (tests/test_entities_admin.py EntityEndpointTests)
class EntityNoteIdempotentTests(unittest.TestCase):
    """facts #74/#77/#80/#87: four identical approved claims with no source event."""
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI(); app.include_router(router); cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.store = _fresh_store(self.tmp); self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        p = patch.object(routes_mod.memory, "_ensure_store", return_value=self.store); p.start(); self.addCleanup(p.stop)

    def test_repeated_entity_note_is_idempotent(self):
        eid = self.store.resolve_entity("Project High Noon", kind="project", ts=NOW)
        text = "Project High Noon is blocked on the pilot checklist; Maya owns the runbook artifact in Notion"
        with patch("app.api.routes.memory.index_fact", lambda *a, **k: None):
            ids = {self.client.post(f"/entities/{eid}/note", json={"text": text}).json()["fact_id"] for _ in range(4)}
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.store._conn.execute("SELECT count(*) FROM facts WHERE text = ?", (text,)).fetchone()[0], 1)
