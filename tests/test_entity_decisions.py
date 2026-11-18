"""Entity model, stage G: the lead's decisions after the Phase 0-2 check
(SPEC section 8) under the general-use requirement (section 9).

G1 a code-shaped name keeps the org or tool kind the model gave it only when
   one sighting proposed it at >= QUILL_ENTITY_CODE_KIND_MIN_CONF and a second
   independent sighting (another session or source class) agrees; else it is
   a label and waits for recurrence. Replaces D1.
G2 a filed company stays an org: a legal form from any jurisdiction (the
   short ones only in their usual case, last, after a capitalised word) or a
   known person's email domain beats the filed-project prior, in the vote and
   in `projects.ensure_filed`. Replaces D2's list.
D3 the priors read the canonical name and the confirmed aliases only.
D4 a row from before kind provenance (NULL `kind_source`) is changed by the
   filed, email-domain and value-literal priors only, never by the sighting
   vote nor (tightened after the stage G check) by the code-shape rule.
D5 bind-only sightings count toward no promotion, and a relation such a
   source asserts about a candidate is dropped, not pooled.
D6 entity sightings group into sessions by the `sessions` table spans and a
   gap of QUILL_ENTITY_CANDIDATE_SESSION_GAP_S (3600 s); the people path keeps
   its 300 s.
D7 a dismissed candidate's tombstone does not stop a person.
D8 a single lowercase word is weak only when no sighting called it a tool or
   an org.
D9 the filed-project keys and the email-domain orgs are cached: one
   extraction pass opens the assistant database once.
G5 a seat with one source kind (mail only, typed chat only, meetings only)
   promotes real names through distinct sessions alone.
G6 every threshold is read from the environment at call time.
A  expired sightings leave a text-free summary, so a name seen six weeks
   apart still promotes.
C  an org can be a filing target: its notes are stamped, it is a rollup
   home, and repeated saves never change the kind or write anything.

Every test runs against a real Store on a temp db, through the real
extractor persist path where a sighting is involved. Mocked: the vector
index write and the dedup probe (as in the Phase 1 and 2 tests).
"""
from __future__ import annotations

import json
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
from app.services import entity_typing as et
from app.services import project_rollup
from app.services import projects
from app.services.kg_keys import entity_name_key
from app.services.resolution import Resolver
from app.storage import Store

NOW = 1_700_000_000.0
MIN = 60.0
HOUR = 3600.0
DAY = 86400.0

CHAT = "chat.user"              # direct_message: the user's own typed turn
MAIL = "google.mail"            # connector_mail
TRANSCRIPT = "notion.transcript"  # connector_item: a meeting's transcript
DOC = "chat.attach"             # user_authored_document
NOTE = "assistant.claude"       # assistant_capture: may bind, never mint
SCREEN = "desktop.screen"       # screen OCR: may bind, never mint

_THRESHOLDS = ("QUILL_ENTITY_CANDIDATE_SESSIONS",
               "QUILL_ENTITY_CANDIDATE_TTL_DAYS",
               "QUILL_ENTITY_CANDIDATE_SESSION_GAP_S",
               "QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS",
               "QUILL_ENTITY_CODE_KIND_MIN_CONF",
               "QUILL_ENTITY_KIND_INCUMBENT_PRIOR",
               "QUILL_ENTITY_KIND_FLIP_MARGIN",
               "QUILL_ENTITY_KIND_MIN_DISSENT",
               "QUILL_ROLLUP_FILED_HOMES", "QUILL_ROLLUP_HOME_KINDS",
               "QUILL_ROLLUP_MIN_FACTS")


def _mk(tmp: str) -> Store:
    return Store(db_path=Path(tmp) / "t.db", audio_dir=Path(tmp) / "audio")


class _StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_decisions_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        # The filed tier of the project registry lives in the data dir;
        # org scopes come from the membership file. Neither is the seat's.
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp,
                                      "QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)
        for var in _THRESHOLDS:
            os.environ.pop(var, None)
        scopes = patch.object(projects, "_scopes", lambda: [])
        scopes.start()
        self.addCleanup(scopes.stop)
        projects.invalidate_filed_keys()
        self.addCleanup(projects.invalidate_filed_keys)

    def sight(self, name, kind, *, cls="connector_mail", event_id=None,
              ts=NOW, conf=0.9, entity_id=None, source=MAIL, typed=False,
              filing_project=None):
        return self.store.record_entity_mention(
            name, proposed_kind=kind, confidence=conf, source_class=cls,
            event_source=source, event_id=event_id, observed_at=ts,
            entity_id=entity_id, typed=typed, filing_project=filing_project,
            weak=et.common_noun_like(name))

    def vote(self, name):
        return et.vote(self.store, name_key=entity_name_key(name), name=name)

    def kind(self, eid: int) -> tuple:
        row = self.store.get_entity(eid)
        return row["kind"], row["kind_source"]

    def file_note(self, project: str) -> None:
        """An accepted assistant note filed under `project`: the `filed`
        tier of the registry, as the inbox accept writes it."""
        from app.services.remote_mcp import connect
        with connect() as c:
            c.execute("INSERT INTO captures (created_at, project, kind, text, "
                      "status, decided_at) VALUES (?, ?, 'note', 'x', "
                      "'accepted', ?)", (NOW, project, NOW))
            c.commit()

    def add_email(self, address: str, person: str = "Jane Roe") -> None:
        pid = self.store.find_person_exact(person) \
            or self.store.insert_person(person, ts=NOW)
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display=address,
            value_normalized=address, confidence=0.9,
            attribution_method="email_header",
            verification_status="attributed", source_event_id=None,
            evidence_quote=None, discourse_role=None, ts=NOW,
            created_by="system", pipeline_version="t")

    def confirm_alias(self, eid: int, alias: str) -> None:
        self.store.upsert_entity_alias(eid, alias, entity_name_key(alias),
                                       source="user", confirmed=True, ts=NOW)

    def session_row(self, start: float, end: float) -> None:
        with self.store._lock:
            self.store._conn.execute(
                "INSERT INTO sessions (start, end, text) VALUES (?, ?, ?)",
                (start, end, "session"))
            self.store._conn.commit()


class _ExtractCase(_StoreCase):
    """The extractor's own persist path, against the temp store."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {
            "QUILL_HEADS": "off", "QUILL_USAGE_LEDGER": "0",
            "QUILL_PEOPLE_V2": "1"})
        env.start()
        self.addCleanup(env.stop)
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

    def see(self, name: str, kind: str | None = "org", *, source: str,
            ts: float, text: str | None = None, conf: float = 0.9,
            relations=(), now: float | None = None,
            meta: dict | None = None) -> int:
        """One event from `source` in which the model read `name`."""
        text = text if text is not None else f"A note that mentions {name}."
        modality = (Modality.AUDIO if source.startswith("audio")
                    else Modality.TEXT)
        ev = int(self.store.insert(Event(
            time=ts, modality=modality, raw=text, source=source,
            meta=dict(meta or {}))))
        facts = {"entities": [{"name": name, "kind": kind,
                               "confidence": conf, "source_span": text}],
                 "relations": list(relations)}
        if source.startswith("audio"):
            # Speech reaches the extractor with no event source.
            self.ex._persist_entities(facts, ev,
                                      now if now is not None else ts,
                                      turn_text=text)
        else:
            self.ex._persist_entities(facts, ev,
                                      now if now is not None else ts,
                                      event_source=source, text=text)
        return ev

    def entity(self, name: str) -> dict | None:
        eid = self.store.find_entity_exact(name)
        return self.store.get_entity(eid) if eid else None

    def pending(self, name: str) -> list[dict]:
        return self.store.entity_mentions(name_key=entity_name_key(name),
                                          status="pending")


# ------------------------------------------------------------------- G1 --
class CodeShapedNameTests(_StoreCase):
    """A code-shaped name keeps org or tool only with two independent
    sightings, one of them sure."""

    def test_one_sighting_is_a_label_whatever_it_says(self):
        for i, (name, kind) in enumerate((
                ("a16z", "org"), ("H100", "tool"), ("S3", "tool"),
                ("c4", "project"), ("ENG-482", "org"))):
            self.sight(name, kind, event_id=10 + i, conf=0.99)
            v = self.vote(name)
            self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                             ("label", "rule", "code_like"), name)

    def test_two_sessions_agree(self):
        self.sight("a16z", "org", event_id=1, ts=NOW, conf=0.9)
        self.sight("a16z", "org", event_id=2, ts=NOW + 2 * HOUR, conf=0.7)
        v = self.vote("a16z")
        self.assertEqual((v.kind, v.breakdown.get("code_kind")),
                         ("org", "org"))

    def test_two_source_classes_agree_in_one_session(self):
        self.sight("H100", "tool", event_id=1, ts=NOW, conf=0.9)
        self.sight("H100", "tool", event_id=2, ts=NOW + 5 * MIN, conf=0.85,
                   cls="private_conversation", source="audio.whisper")
        self.assertEqual(self.vote("H100").kind, "tool")

    def test_same_session_and_class_is_one_source(self):
        # Three mails in one sitting: one source said it three times.
        for i in range(3):
            self.sight("H100", "tool", event_id=i + 1, ts=NOW + i * 10 * MIN)
        self.assertEqual(self.vote("H100").kind, "label")

    def test_one_sighting_must_be_sure(self):
        self.sight("S3", "tool", event_id=1, ts=NOW, conf=0.7)
        self.sight("S3", "tool", event_id=2, ts=NOW + 2 * HOUR, conf=0.75)
        self.assertEqual(self.vote("S3").kind, "label")
        # A stated confidence is needed: none stated is not "sure".
        self.sight("R2", "tool", event_id=3, ts=NOW, conf=None)
        self.sight("R2", "tool", event_id=4, ts=NOW + 2 * HOUR, conf=None)
        self.assertEqual(self.vote("R2").kind, "label")

    def test_the_second_sighting_must_name_the_same_kind(self):
        self.sight("M365", "tool", event_id=1, ts=NOW, conf=0.95)
        self.sight("M365", "project", event_id=2, ts=NOW + 2 * HOUR)
        self.assertEqual(self.vote("M365").kind, "label")

    def test_screen_and_notes_do_not_agree_with_anything(self):
        # They repeat what another source said; they are no second source.
        self.sight("EC2", "tool", event_id=1, ts=NOW, conf=0.95)
        self.sight("EC2", "tool", event_id=2, ts=NOW + 2 * HOUR,
                   cls="unknown", source=SCREEN, conf=0.95)
        self.sight("EC2", "tool", event_id=3, ts=NOW + 4 * HOUR,
                   cls="assistant_capture", source=NOTE, conf=0.95)
        self.assertEqual(self.vote("EC2").kind, "label")

    def test_the_floor_is_read_from_the_environment(self):
        self.sight("a16z", "org", event_id=1, ts=NOW, conf=0.9)
        self.sight("a16z", "org", event_id=2, ts=NOW + 2 * HOUR, conf=0.9)
        self.assertEqual(self.vote("a16z").kind, "org")
        with patch.dict(os.environ,
                        {"QUILL_ENTITY_CODE_KIND_MIN_CONF": "0.95"}):
            self.assertEqual(et.code_kind_min_conf(), 0.95)
            self.assertEqual(self.vote("a16z").kind, "label")
        for bad in ("x", ""):
            with patch.dict(os.environ,
                            {"QUILL_ENTITY_CODE_KIND_MIN_CONF": bad}):
                self.assertEqual(et.code_kind_min_conf(), 0.8)

    def test_deterministic_signals_decide_first(self):
        # An email domain: one sighting is enough, it is the domain's org.
        self.add_email("jane@a16z.com")
        self.sight("a16z", "project", event_id=1)
        v = self.vote("a16z")
        self.assertEqual((v.kind, v.breakdown["reason"]),
                         ("org", "email_domain"))
        # A filing: a project.
        self.file_note("qx7")
        self.sight("qx7", "tool", event_id=2)
        self.assertEqual(self.vote("qx7").kind, "project")
        # The user: held.
        eid = self.store.resolve_entity("R2", "tool", ts=NOW,
                                        kind_source="user")
        self.sight("R2", "project", event_id=3, entity_id=eid)
        self.assertEqual(et.retype(self.store, eid).breakdown["reason"],
                         "held")
        self.assertEqual(self.kind(eid), ("tool", "user"))

    def test_a_model_row_follows_the_evidence_without_flapping(self):
        eid = self.store.resolve_entity("H100", "tool", ts=NOW,
                                        kind_source="model")
        # One new sighting: not enough to keep "tool" on a code. The
        # code-shape rule decided, so the row stores `rule` (fix pass B).
        self.sight("H100", "tool", event_id=1, ts=NOW, entity_id=eid)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid), ("label", "rule"))
        # A second session agrees: the sightings vote it back to tool.
        self.sight("H100", "tool", event_id=2, ts=NOW + 3 * HOUR,
                   entity_id=eid)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid), ("tool", "rule"))
        # A stray "project" afterwards changes nothing.
        self.sight("H100", "project", event_id=3, ts=NOW + 6 * HOUR,
                   entity_id=eid)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid), ("tool", "rule"))


class CodeShapedNamePromotionTests(_ExtractCase):
    def test_a_named_code_waits_and_then_keeps_its_kind(self):
        self.see("a16z", "org", source=CHAT, ts=NOW,
                 text="a16z led the round")
        self.assertIsNone(self.store.find_entity_exact("a16z"))
        self.assertEqual(cand.evaluate(self.store, "a16z").reason,
                         "code_like")
        self.see("a16z", "org", source=MAIL, ts=NOW + 2 * HOUR)
        row = self.entity("a16z")
        # The code-shape rule let the kind stand: stored at rule rank (fix
        # pass B).
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "rule"))

    def test_a_code_seen_twice_as_a_project_is_a_label(self):
        self.see("c4", "project", source=MAIL, ts=NOW)
        self.see("c4", "project", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertEqual(self.entity("c4")["kind"], "label")


# ------------------------------------------------------------------- G2 --
class LegalFormTests(unittest.TestCase):
    COMPANIES = (
        "Acme LLC", "Acme, LLC", "Acme L.L.C.", "Zephyr LLP", "Saltmeadow LP",
        "Dunmore Dental PLLC", "Reyes & Kahn PC", "Northwind Inc",
        "Northwind, Inc.", "Northwind Incorporated", "Orbit Corp",
        "Orbit Corp.", "Orbit Corporation", "Kestrel Co.", "Fast Company",
        "Halcyon Ltd", "Halcyon Ltd.", "Tata Motors Limited", "Monsoon PLC",
        "Maple Leaf ULC", "Brightline GmbH", "Lebenshilfe gGmbH",
        "Siemens AG", "Brenner KG", "Henkel KGaA", "Allianz SE",
        "Freunde e.V.", "Volksbank eG", "Vega S.A.", "Banco Rojo SA",
        "Atos S.A.S.", "Mistral SAS", "Boulanger SARL", "Petit S.à r.l.",
        "Lumière SNC", "Ferro S.p.A.", "Ferro SpA", "Verdi S.r.l.",
        "Verdi Srl", "Iberia S.L.", "Iberia SL", "Solana S.A.U.",
        "Philips BV", "Philips B.V.", "Delta NV", "Delta N.V.",
        "Koala Pty", "Koala Pty Ltd", "Sakura K.K.", "Sakura KK",
        "Hikari G.K.", "Midori Y.K.", "Nokia Oy", "Nokia Oyj", "Volvo AB",
        "Equinor AS", "Maersk A/S", "Equinor ASA", "Lego ApS",
        "Brightline Sp. z o.o.", "Bimbo S.A. de C.V.", "Natura Ltda",
        "Natura Ltda.", "Vale S/A", "Infosys Pvt Ltd",
        "Wipro Private Limited", "Petronas Sdn Bhd", "Maybank Bhd",
        "Grab Pte Ltd", "Astra International Tbk", "Indo Mining PT",
        "Gazprom JSC", "Yandex OOO", "Sber PAO", "Krka d.o.o.",
        "Skoda s.r.o.", "Skoda a.s.", "Richter Kft.", "Mol Zrt.",
        "Mol Nyrt.", "Marel ehf.", "Eimskip hf.", "Greenway CIC")
    NOT_COMPANIES = (
        "Plan B AB testing", "Ana Sa", "Volvo ab", "iPhone SE", "Day Spa",
        "the company", "a limited", "the corporation", "LLC", "Inc", "AB",
        "Sparrow 1.0", "Atlas", "Costco", "Pinc", "Bagel Co", "Gaming pc",
        "Acme Group", "Northfield University", "Saltmeadow Partners",
        "Coastal Ventures", "", None, "   ", "& Co.")

    def test_companies(self):
        for name in self.COMPANIES:
            self.assertTrue(et.company_name(name), name)

    def test_not_companies(self):
        for name in self.NOT_COMPANIES:
            self.assertFalse(et.company_name(name), name)

    def test_punctuation_from_running_text_does_not_hide_the_form(self):
        for name in ("Acme LLC.", "Acme LLC,", "(Northwind, Inc.)",
                     "“Volvo AB”", "Volvo AB.", "'Halcyon GmbH'",
                     "Kestrel Co..", "Orbit Corp!"):
            self.assertTrue(et.company_name(name), name)
        # "Co." keeps its dot; a person stays a person with a full stop.
        for name in ("Acme Co", "Ana Sa.", "Li Ag.", "(", " . "):
            self.assertFalse(et.company_name(name), name)

    def test_a_form_after_a_determiner_is_a_noun_not_a_name(self):
        for name in ("Form an LLC", "form an llc", "My PC", "Set up the GmbH",
                     "Our Corp", "The Company", "this SE"):
            self.assertFalse(et.company_name(name), name)
        self.assertTrue(et.company_name("The Walt Disney Company"))

    def test_short_forms_only_in_their_usual_case_and_place(self):
        self.assertTrue(et.company_name("Volvo AB"))
        self.assertFalse(et.company_name("Volvo Ab"))
        self.assertFalse(et.company_name("AB Volvo"))
        # After a capitalised word (or a digit), not after a lowercase one.
        self.assertTrue(et.company_name("3M AG"))
        self.assertFalse(et.company_name("bayer AG"))

    def test_legal_forms_and_organisation_words_are_kept_apart(self):
        from app.services import name_quality as nq
        # Generic organisation words are not legal forms, and the legal
        # forms are their own vocabulary.
        for word in ("group", "partners", "university", "ventures", "team"):
            self.assertIn(word, nq._CORP_SUFFIXES)
            self.assertNotIn(word, et.LEGAL_FORMS)
        for form in et.SHORT_LEGAL_FORMS:
            self.assertNotIn(form.casefold(), et.LEGAL_FORMS, form)


class FiledCompanyTests(_ExtractCase):
    def test_filing_under_a_company_keeps_it_an_org(self):
        # A short form after one word needs the evidence to agree (fix
        # pass B): here the model once read "Volvo AB" as an org.
        self.sight("Volvo AB", "org", event_id=900)
        for name in ("Acme LLC", "Volvo AB", "Brightline Sp. z o.o.",
                     "Tata Motors Limited"):
            eid = projects.ensure_filed(name, store=self.store, now=NOW,
                                        user_accept=True)
            self.assertTrue(eid, name)
            self.assertEqual(self.kind(eid), ("org", "filing"), name)
        # An existing model row that called it a tool becomes the org.
        tool = self.store.resolve_entity("Northwind, Inc.", "tool", ts=NOW,
                                         kind_source="model")
        self.assertEqual(projects.ensure_filed("Northwind, Inc.",
                                               store=self.store, now=NOW),
                         tool)
        self.assertEqual(self.kind(tool), ("org", "filing"))

    def test_an_email_domain_is_an_org_when_filed(self):
        self.add_email("jane@halcyon-labs.io")
        eid = projects.ensure_filed("Halcyon Labs", store=self.store,
                                    now=NOW)
        self.assertEqual(self.kind(eid), ("org", "filing"))

    def test_a_domain_that_spells_the_name_without_spaces(self):
        # Company domains drop the spaces: clearbrookdental.com is
        # "Clearbrook Dental", acme-robotics.io is "Acme Robotics".
        self.add_email("ana@clearbrookdental.com")
        self.add_email("bo@acme-robotics.io", person="Bo Park")
        for name in ("Clearbrook Dental", "Acme Robotics"):
            self.sight(name, "tool", event_id=hash(name) % 1000)
            v = self.vote(name)
            self.assertEqual((v.kind, v.breakdown["reason"]),
                             ("org", "email_domain"), name)
        # A name that only shares a word with the domain is not it.
        self.sight("Clearbrook", "tool", event_id=2000)
        self.assertEqual(self.vote("Clearbrook").kind, "tool")

    def test_a_free_mail_domain_is_no_org(self):
        # G3: a free-mail host from the vendored list names no employer.
        self.add_email("jane@yandex.ru")
        eid = projects.ensure_filed("Yandex", store=self.store, now=NOW)
        self.assertEqual(self.kind(eid), ("project", "filing"))

    def test_everything_else_filed_is_a_project(self):
        tool = self.store.resolve_entity("Sparrow 1.0", "tool", ts=NOW,
                                         kind_source="model")
        self.assertEqual(projects.ensure_filed("Sparrow 1.0",
                                               store=self.store, now=NOW),
                         tool)
        self.assertEqual(self.kind(tool), ("project", "filing"))
        # A short form in the wrong case is no legal form.
        eid = projects.ensure_filed("Plan B AB testing", store=self.store,
                                    now=NOW)
        self.assertEqual(self.kind(eid), ("project", "filing"))

    def test_the_vote_keeps_a_filed_company_an_org(self):
        self.file_note("Volvo AB")
        self.sight("Volvo AB", "org", event_id=900)
        self.sight("Volvo AB", "project", event_id=1)
        v = self.vote("Volvo AB")
        self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                         ("org", "rule", "filed_company_name"))
        self.file_note("Halcyon")
        self.add_email("bob@halcyon.com", person="Bob Poe")
        self.sight("Halcyon", "tool", event_id=2)
        v = self.vote("Halcyon")
        self.assertEqual((v.kind, v.breakdown["reason"]),
                         ("org", "filed_email_domain"))
        self.file_note("Sparrow 1.0")
        self.sight("Sparrow 1.0", "tool", event_id=3)
        self.assertEqual(self.vote("Sparrow 1.0").kind, "project")

    def test_a_model_org_row_that_is_filed_stays_an_org(self):
        self.see("Brightline GmbH", "org", source=MAIL, ts=NOW)
        self.see("Brightline GmbH", "org", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Brightline GmbH")
        self.file_note("Brightline GmbH")
        self.see("Brightline GmbH", "project", source=MAIL, ts=NOW + 4 * HOUR)
        self.assertEqual(self.kind(eid)[0], "org")


# ------------------------------------------------------------------- D3 --
class PriorNamesTests(_StoreCase):
    def test_an_unconfirmed_spelling_is_no_prior(self):
        self.file_note("Orbit")
        eid = self.store.resolve_entity("Orbital Labs", "tool", ts=NOW,
                                        kind_source="model")
        # Folded in under another spelling (an alias on the row and a
        # bound sighting), never confirmed.
        self.store.touch_entity(eid, NOW, alias="Orbit")
        self.sight("Orbit", "tool", event_id=1, entity_id=eid)
        self.assertNotEqual(et.vote(self.store, entity_id=eid).kind,
                            "project")
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid), ("tool", "model"))
        # The user confirms the alias: now it speaks for the row.
        self.confirm_alias(eid, "Orbit")
        v = et.vote(self.store, entity_id=eid)
        self.assertEqual((v.kind, v.breakdown["reason"]),
                         ("project", "filed_project"))

    def test_an_unconfirmed_spelling_is_no_email_domain(self):
        self.add_email("jane@kestrel.com")
        eid = self.store.resolve_entity("Kestrel Analytics Suite", "tool",
                                        ts=NOW, kind_source="model")
        self.sight("Kestrel", "tool", event_id=1, entity_id=eid)
        self.assertEqual(et.vote(self.store, entity_id=eid).kind, "tool")
        self.confirm_alias(eid, "Kestrel")
        self.assertEqual(et.vote(self.store, entity_id=eid)
                         .breakdown["reason"], "email_domain")

    def test_the_canonical_name_alone_still_counts(self):
        self.file_note("Atlas")
        eid = self.store.resolve_entity("Atlas", "tool", ts=NOW,
                                        kind_source="model")
        self.assertEqual(et.vote(self.store, entity_id=eid).kind, "project")


# ------------------------------------------------------------------- D4 --
class LegacyRowTests(_StoreCase):
    def test_the_vote_never_flips_a_legacy_row(self):
        legacy = self.store.resolve_entity("Halcyon", "tool", ts=NOW)
        model = self.store.resolve_entity("Monsoon", "tool", ts=NOW,
                                          kind_source="model")
        for eid, name in ((legacy, "Halcyon"), (model, "Monsoon")):
            for i in range(4):
                self.sight(name, "org", event_id=10 * eid + i, conf=0.95,
                           entity_id=eid)
            et.retype(self.store, eid)
        self.assertEqual(self.kind(legacy), ("tool", None))
        self.assertEqual(et.vote(self.store, entity_id=legacy)
                         .breakdown["reason"], "legacy")
        # The same evidence flips a row the model typed.
        self.assertEqual(self.kind(model), ("org", "model"))

    def test_the_priors_still_apply_to_a_legacy_row(self):
        # The filed, email-domain and value-literal priors (D4 as tightened
        # after the stage G check; the code-shape rule is the next test).
        self.file_note("Atlas")
        cases = (("Atlas", "tool", "project"), ("Nov 14", "idea", "thing"))
        for name, kind, want in cases:
            eid = self.store.resolve_entity(name, kind, ts=NOW)
            self.assertEqual(self.kind(eid), (kind, None))
            self.sight(name, kind, event_id=None, entity_id=eid)
            et.retype(self.store, eid)
            self.assertEqual(self.kind(eid)[0], want, name)
        self.add_email("ann@brightline.com", person="Ann Lee")
        eid = self.store.resolve_entity("Brightline", "tool", ts=NOW)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid)[0], "org")

    def test_the_code_shape_rule_never_changes_a_legacy_row(self):
        # D4 tightened: a legacy code-shaped row may be the user's own kind
        # from before provenance; only the reviewed repair (Phase 4) changes
        # it. Was: G1 relabelled an uncorroborated legacy "ZK42" tool.
        cases = (("ZK42", "tool"), ("c7", "project"), ("9bc12da", "idea"))
        for i, (name, kind) in enumerate(cases):
            eid = self.store.resolve_entity(name, kind, ts=NOW)
            self.sight(name, kind, event_id=10 + i, entity_id=eid)
            v = et.retype(self.store, eid)
            self.assertEqual(v.breakdown["reason"], "legacy", name)
            self.assertTrue(v.breakdown.get("code_like"), name)
            self.assertEqual(self.kind(eid), (kind, None), name)
        kept = self.store.resolve_entity("RTX4090", "tool", ts=NOW)
        self.sight("RTX4090", "tool", event_id=2, ts=NOW, entity_id=kept)
        self.sight("RTX4090", "tool", event_id=3, ts=NOW + 2 * HOUR,
                   entity_id=kept)
        v = et.retype(self.store, kept)
        self.assertEqual(v.breakdown["reason"], "legacy")
        self.assertEqual(self.kind(kept), ("tool", None))
        # The same evidence relabels a row the model typed.
        model = self.store.resolve_entity("ZK43", "tool", ts=NOW,
                                          kind_source="model")
        self.sight("ZK43", "tool", event_id=20, entity_id=model)
        et.retype(self.store, model)
        self.assertEqual(self.kind(model), ("label", "rule"))
        # The value-literal prior still applies to a legacy row whatever
        # its shape ("2026-11-14" is a date, not a code).
        date = self.store.resolve_entity("2026-11-14", "project", ts=NOW)
        self.sight("2026-11-14", "project", event_id=21, entity_id=date)
        et.retype(self.store, date)
        self.assertEqual(self.kind(date)[0], "thing")


# ------------------------------------------------------------------- D5 --
class BindOnlyCountsTests(_ExtractCase):
    REL = {"subject": "Atlas", "subject_kind": "project",
           "predicate": "uses", "object": "Northwind", "object_kind": "org",
           "confidence": 0.9, "source_span": "Atlas uses Northwind"}

    def test_a_bind_only_sighting_is_no_second_session_or_class(self):
        for src in (NOTE, SCREEN):
            name = f"Northwind {src.split('.')[0].title()}"
            self.see(name, "org", source=src, ts=NOW)
            self.see(name, "org", source=MAIL, ts=NOW + 2 * HOUR)
            self.assertIsNone(self.store.find_entity_exact(name), src)
            v = cand.evaluate(self.store, entity_name_key(name))
            self.assertEqual((v.mentions, v.sessions, v.source_classes),
                             (2, 1, 1), src)
            listed = {c["name"]: c for c in cand.list_candidates(self.store)}
            c = listed[name]
            self.assertEqual((c["mentions"], c["bind_only_mentions"],
                              c["sessions"], c["source_classes"]),
                             (2, 1, 1, 1), src)
            self.assertEqual(len(c["source_class_names"]), 2, src)

    def test_a_relation_from_a_bind_only_source_is_dropped(self):
        atlas = self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Atlas", "project", source=NOTE, ts=NOW,
                 relations=[self.REL])
        self.assertEqual(self.store.pending_entity_relations(status=None), [])
        self.assertEqual(len(self.pending("Northwind")), 1)
        # Northwind becomes a node from eligible sightings: no edge comes
        # back from the note.
        self.see("Northwind", "org", source=MAIL, ts=NOW + 2 * HOUR)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 4 * HOUR)
        self.assertTrue(self.store.find_entity_exact("Northwind"))
        self.assertEqual(self.store.relations_of("entity", atlas)["out"], [])

    def test_an_eligible_relation_to_a_candidate_is_still_pooled(self):
        self.store.resolve_entity("Atlas", "project", ts=NOW)
        self.see("Atlas", "project", source=MAIL, ts=NOW,
                 relations=[self.REL])
        self.assertEqual(len(self.store.pending_entity_relations()), 1)

    def test_a_typed_looking_note_is_not_typed(self):
        # The user's own words pasted into an assistant note are still a
        # note: no strong signal, no promotion.
        self.see("Northwind", "org", source=NOTE, ts=NOW)
        v = cand.evaluate(self.store, "northwind")
        self.assertEqual((v.typed, v.reason), (False, "bind_only"))


# ------------------------------------------------------------------- D6 --
class EntitySessionTests(_ExtractCase):
    def test_sightings_within_the_hour_are_one_session(self):
        self.see("Northwind", "org", source=MAIL, ts=NOW)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 20 * MIN)
        self.see("Northwind", "org", source=MAIL, ts=NOW + 50 * MIN)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(cand.evaluate(self.store, "northwind").sessions, 1)
        # A chain of close sightings is one sitting; a gap past the hour
        # starts another.
        self.see("Northwind", "org", source=MAIL,
                 ts=NOW + 50 * MIN + HOUR + 1)
        self.assertIsNotNone(self.store.find_entity_exact("Northwind"))

    def test_the_gap_is_read_from_the_environment(self):
        with patch.dict(os.environ,
                        {"QUILL_ENTITY_CANDIDATE_SESSION_GAP_S": "300"}):
            self.assertEqual(cand.session_gap_s(), 300.0)
            self.see("Northwind", "org", source=MAIL, ts=NOW)
            self.see("Northwind", "org", source=MAIL, ts=NOW + 20 * MIN)
            self.assertIsNotNone(self.store.find_entity_exact("Northwind"))
        for bad in ("x", ""):
            with patch.dict(os.environ,
                            {"QUILL_ENTITY_CANDIDATE_SESSION_GAP_S": bad}):
                self.assertEqual(cand.session_gap_s(), 3600.0)

    def test_a_session_row_is_one_session_however_long(self):
        # A three-hour meeting is one session even though its sightings are
        # more than the gap apart.
        self.session_row(NOW, NOW + 3 * HOUR)
        self.assertEqual(cand.distinct_sessions(
            self.store, [NOW + MIN, NOW + 90 * MIN, NOW + 170 * MIN]), 1)
        # A sighting no row covers, half an hour after it ends, is that
        # sitting; one two hours on is not.
        self.assertEqual(cand.distinct_sessions(
            self.store, [NOW + MIN, NOW + 3 * HOUR + 30 * MIN]), 1)
        self.assertEqual(cand.distinct_sessions(
            self.store, [NOW + MIN, NOW + 5 * HOUR]), 2)

    def test_session_rows_closer_than_the_gap_are_one_sitting(self):
        # The table cuts at the people path's 300 s; two rows ten minutes
        # apart are one sitting for entity recurrence (deviation from the
        # letter of D6: the result must not depend on whether consolidation
        # has run).
        self.session_row(NOW, NOW + 10 * MIN)
        self.session_row(NOW + 20 * MIN, NOW + 30 * MIN)
        stamps = [NOW + MIN, NOW + 25 * MIN]
        self.assertEqual(cand.distinct_sessions(self.store, stamps), 1)
        # Without the rows the same sightings are one session too.
        with self.store._lock:
            self.store._conn.execute("DELETE FROM sessions")
            self.store._conn.commit()
        self.assertEqual(cand.distinct_sessions(self.store, stamps), 1)
        # Rows a day apart are two.
        self.session_row(NOW, NOW + HOUR)
        self.session_row(NOW + DAY, NOW + DAY + HOUR)
        self.assertEqual(cand.distinct_sessions(
            self.store, [NOW + MIN, NOW + DAY + MIN]), 2)

    def test_labels_follow_the_input_order(self):
        labels = cand.session_labels(
            self.store, [NOW + 5 * HOUR, NOW, NOW + 10 * MIN, NOW + 5 * HOUR])
        self.assertEqual(labels[1], labels[2])
        self.assertEqual(labels[0], labels[3])
        self.assertNotEqual(labels[0], labels[1])
        self.assertEqual(cand.session_labels(self.store, []), [])

    def test_the_people_path_keeps_its_300_seconds(self):
        from app.services import mint_recurrence
        self.assertEqual(mint_recurrence.distinct_sessions(
            self.store, [NOW, NOW + 20 * MIN]), 2)
        self.assertEqual(cand.distinct_sessions(
            self.store, [NOW, NOW + 20 * MIN]), 1)


# ------------------------------------------------------------------- D7 --
class DismissAndPeopleTests(_StoreCase):
    TEXT = "Dana Whitcombe joined the call about the renewal"

    def org_rejections(self) -> int:
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT COUNT(*) AS n FROM person_mentions "
                "WHERE resolution_status = 'rejected' "
                "AND person_probability <= 0.05").fetchone()
        return int(row["n"])

    def resolve(self, name: str, event_id: int):
        from app.services import people_pipeline as pp
        with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "1"}):
            return pp.resolve_person_mention(
                name, store=self.store, event_id=event_id,
                event_source=CHAT, text=self.TEXT, now=NOW + 1)

    def test_a_dismissed_candidate_does_not_block_a_person(self):
        self.store.record_entity_mention("Dana Whitcombe",
                                         proposed_kind="org", event_id=3,
                                         observed_at=NOW)
        self.assertTrue(cand.dismiss(self.store, "Dana Whitcombe")["ok"])
        tomb = self.store.entity_tombstone("Dana Whitcombe")
        self.assertEqual((tomb["kind"], tomb["reason"]),
                         ("org", cand.DISMISS_REASON))
        self.resolve("Dana Whitcombe", 11)
        self.assertEqual(self.org_rejections(), 0)

    def test_a_forgotten_org_still_does(self):
        eid = self.store.resolve_entity("Dana Whitcombe", "org", ts=NOW)
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertEqual(self.resolve("Dana Whitcombe", 12).decision,
                         "reject")
        self.assertEqual(self.org_rejections(), 1)


# ------------------------------------------------------------------- D8 --
class WeakWordTests(_ExtractCase):
    def test_the_rule(self):
        self.assertTrue(et.weak_name("ski resort", ["tool"]))
        self.assertTrue(et.weak_name("laundry", ["project", None]))
        self.assertTrue(et.weak_name("laundry", []))
        self.assertFalse(et.weak_name("numpy", ["tool"]))
        self.assertFalse(et.weak_name("pgvector", ["project", "tool"]))
        self.assertFalse(et.weak_name("acme", ["company"]))      # -> org
        self.assertFalse(et.weak_name("Numpy", ["project"]))

    def test_a_lowercase_tool_typed_once_is_a_node(self):
        self.see("numpy", "tool", source=CHAT, ts=NOW,
                 text="I rewrote the loop in numpy")
        row = self.entity("numpy")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "tool")

    def test_a_lowercase_tool_recurs_like_any_name(self):
        self.see("pgvector", "tool", source=MAIL, ts=NOW)
        self.see("pgvector", "tool", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertIsNotNone(self.entity("pgvector"))

    def test_a_lowercase_word_called_a_project_stays_weak(self):
        self.see("laundry", "project", source=CHAT, ts=NOW,
                 text="the laundry is the hard part")
        self.see("laundry", "project", source=MAIL, ts=NOW + 2 * HOUR)
        self.assertIsNone(self.store.find_entity_exact("laundry"))
        v = cand.evaluate(self.store, "laundry")
        self.assertEqual((v.weak, v.reason), (True, "weak"))
        listed = {c["name"]: c for c in cand.list_candidates(self.store)}
        self.assertTrue(listed["laundry"]["weak"])
        # One sighting that calls it a tool lifts it.
        self.see("laundry", "tool", source=MAIL, ts=NOW + 4 * HOUR)
        self.assertIsNotNone(self.store.find_entity_exact("laundry"))


# ------------------------------------------------------------------- D9 --
class VoteCostTests(_ExtractCase):
    def counting_connect(self):
        from app.services import remote_mcp
        real = remote_mcp.connect
        calls = []

        def counted(*a, **k):
            calls.append(1)
            return real(*a, **k)
        p = patch.object(remote_mcp, "connect", counted)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_one_pass_opens_the_assistant_database_once(self):
        self.file_note("Atlas")
        projects.invalidate_filed_keys()
        calls = self.counting_connect()
        scope_calls = []
        with patch.object(projects, "_scopes",
                          lambda: scope_calls.append(1) or []):
            for i in range(30):
                self.see("Atlas", "tool", source=MAIL, ts=NOW + i * 2 * HOUR)
                self.see("Halcyon", "org", source=MAIL,
                         ts=NOW + i * 2 * HOUR)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(scope_calls), 1)
        self.assertEqual(self.entity("Atlas")["kind"], "project")

    def test_filing_drops_the_cache(self):
        self.assertEqual(projects.filed_keys(), frozenset())
        calls = self.counting_connect()
        projects.filed_keys()
        self.assertEqual(len(calls), 0)          # cached, and no db yet
        projects.ensure_filed("Orbit", store=self.store, now=NOW)
        self.file_note("Orbit")
        self.assertIn("orbit", projects.filed_keys())

    def test_a_write_by_another_connection_is_seen(self):
        self.file_note("Atlas")
        self.assertIn("atlas", projects.filed_keys())
        # Not through ensure_filed: a direct write still changes the file.
        from app.services.remote_mcp import db_path
        raw = sqlite3.connect(str(db_path()))
        raw.execute("INSERT INTO captures (created_at, project, kind, text, "
                    "status, decided_at) VALUES (?, 'Kestrel Rebuild', "
                    "'note', 'xyz', 'accepted', ?)", (NOW, NOW))
        raw.commit()
        raw.close()
        self.assertIn("kestrel rebuild", projects.filed_keys())

    def test_the_cache_expires(self):
        self.file_note("Atlas")
        projects.filed_keys()
        calls = self.counting_connect()
        with patch.object(projects, "_FILED_TTL_S", 0.0):
            projects.filed_keys()
            projects.filed_keys()
        self.assertEqual(len(calls), 2)

    def test_the_email_domain_scan_is_cached_per_store(self):
        self.add_email("jane@acme-robotics.com")
        real = self.store.contact_email_domains
        scans = []

        def counted():
            scans.append(1)
            return real()
        with patch.object(self.store, "contact_email_domains", counted):
            for i in range(20):
                self.sight("Acme Robotics", "tool", event_id=i + 1)
                self.assertEqual(self.vote("Acme Robotics").kind, "org")
            self.assertEqual(len(scans), 1)
            # A new contact point is seen at once.
            self.add_email("bob@brightline.com", person="Bob Poe")
            self.sight("Brightline", "tool", event_id=99)
            self.assertEqual(self.vote("Brightline").kind, "org")
            self.assertEqual(len(scans), 2)
        # Another store has its own cache.
        other = _mk(tempfile.mkdtemp(prefix="quill_decisions_other_"))
        self.addCleanup(other.close)
        self.assertEqual(et.email_domain_org_keys(other), frozenset())

    def test_a_store_without_the_contact_table_has_no_domains(self):
        self.store.close()
        raw = sqlite3.connect(Path(self.tmp) / "t.db")
        raw.execute("DROP TABLE person_contact_points")
        raw.commit()
        raw.close()
        ro = Store(db_path=Path(self.tmp) / "t.db",
                   audio_dir=Path(self.tmp) / "audio", readonly=True)
        self.addCleanup(ro.close)
        self.assertEqual(ro.contact_points_mark(), 0)
        self.assertEqual(ro.contact_email_domains(), [])
        self.assertEqual(et.email_domain_org_keys(ro), frozenset())


# ------------------------------------------------------------------- G5 --
class SingleSourceSeatTests(_ExtractCase):
    """A seat with one source kind promotes real names through distinct
    sessions alone (no second source class is ever available to it)."""

    def test_mail_only(self):
        for day in (0, 2):
            self.see("Clearbrook Dental", "org", source=MAIL,
                     ts=NOW + day * DAY)
        self.assertIsNotNone(self.entity("Clearbrook Dental"))
        # Said three times in one sitting it waits.
        for i in range(3):
            self.see("Quarrystone Assurance", "org", source=MAIL,
                     ts=NOW + 5 * DAY + i * 15 * MIN)
        self.assertIsNone(self.store.find_entity_exact("Quarrystone Assurance"))
        self.assertEqual(cand.evaluate(self.store, "quarrystone assurance")
                         .reason, "waiting")

    def test_typed_chat_only(self):
        # A name the user typed is a node at once; a lowercase phrase or a
        # code the user typed needs a second sitting; a name the model
        # read into a typed turn (not in the text) recurs like any other.
        self.see("Ashgrove Hall", "place", source=CHAT, ts=NOW,
                 text="Book Ashgrove Hall for Friday")
        self.assertIsNotNone(self.entity("Ashgrove Hall"))
        for day in (0, 1):
            self.see("loading yard", "place", source=CHAT, ts=NOW + day * DAY,
                     text="check the loading yard")
            self.see("B12", "place", source=CHAT, ts=NOW + day * DAY + MIN,
                     text="dock B12 is blocked")
        self.assertIsNotNone(self.entity("loading yard"))
        # Two sittings agree it is a place: a code-shaped place keeps the
        # kind under G1, as an org or a tool does (fix pass B).
        self.assertEqual(self.entity("B12")["kind"], "place")
        for day in (0, 1):
            self.see("Marrowgate Freight", "org", source=CHAT,
                     ts=NOW + day * DAY + 2 * MIN, text="ship it today")
        self.assertIsNotNone(self.entity("Marrowgate Freight"))

    def test_meetings_only(self):
        # Speech: one consolidated session row per meeting.
        for day in (0, 3):
            start = NOW + day * DAY
            self.session_row(start, start + HOUR)
            for i in range(3):
                self.see("Freezer Audit Program", "project",
                         source="audio.web_tab", ts=start + i * 20 * MIN)
            if day == 0:
                self.assertIsNone(self.store.find_entity_exact(
                    "Freezer Audit Program"))
        self.assertIsNotNone(self.entity("Freezer Audit Program"))
        # Transcripts from a connector, a week apart.
        for day in (0, 7):
            self.see("Pemberly Temp Staffing", "org", source=TRANSCRIPT,
                     ts=NOW + day * DAY)
        self.assertIsNotNone(self.entity("Pemberly Temp Staffing"))


# ------------------------------------------------------------------- G6 --
class ThresholdConfigTests(_StoreCase):
    def test_every_threshold_is_read_at_call_time(self):
        cases = (
            ("QUILL_ENTITY_CANDIDATE_SESSIONS", "3", cand.min_sessions, 3),
            ("QUILL_ENTITY_CANDIDATE_TTL_DAYS", "14", cand.ttl_days, 14.0),
            ("QUILL_ENTITY_CANDIDATE_SESSION_GAP_S", "7200",
             cand.session_gap_s, 7200.0),
            ("QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS", "90",
             cand.summary_ttl_days, 90.0),
            ("QUILL_ENTITY_CODE_KIND_MIN_CONF", "0.7",
             et.code_kind_min_conf, 0.7),
        )
        for var, value, fn, want in cases:
            with patch.dict(os.environ, {var: value}):
                self.assertEqual(fn(), want, var)
        self.assertEqual((cand.min_sessions(), cand.ttl_days(),
                          cand.session_gap_s(), cand.summary_ttl_days(),
                          et.code_kind_min_conf()),
                         (2, 30.0, 3600.0, 365.0, 0.8))

    def test_the_flip_rule_is_read_at_call_time(self):
        self.assertEqual(et._flip_rule(), (0.5, 0.3, 2))
        with patch.dict(os.environ, {
                "QUILL_ENTITY_KIND_INCUMBENT_PRIOR": "1.0",
                "QUILL_ENTITY_KIND_FLIP_MARGIN": "0.5",
                "QUILL_ENTITY_KIND_MIN_DISSENT": "3"}):
            self.assertEqual(et._flip_rule(), (1.0, 0.5, 3))
        with patch.dict(os.environ, {"QUILL_ENTITY_KIND_MIN_DISSENT": "x"}):
            self.assertEqual(et._flip_rule()[2], 2)
        # And the vote uses it: what flips by default holds with three.
        eid = self.store.resolve_entity("Halcyon", "tool", ts=NOW,
                                        kind_source="model")
        for i in range(2):
            self.sight("Halcyon", "org", event_id=i + 1, entity_id=eid)
        with patch.dict(os.environ, {"QUILL_ENTITY_KIND_MIN_DISSENT": "3"}):
            self.assertEqual(et.vote(self.store, entity_id=eid).kind, "tool")
        self.assertEqual(et.vote(self.store, entity_id=eid).kind, "org")

    def test_more_sessions_needed_means_more_sessions(self):
        for i in range(2):
            self.sight("Brightline", "org", event_id=i + 1,
                       ts=NOW + i * DAY)
        self.assertTrue(cand.evaluate(self.store, "brightline").promote)
        with patch.dict(os.environ,
                        {"QUILL_ENTITY_CANDIDATE_SESSIONS": "3"}):
            self.assertFalse(cand.evaluate(self.store, "brightline").promote)


# -------------------------------------------------------------- Check A --
class SummaryTests(_ExtractCase):
    def test_sightings_42_days_apart_promote(self):
        self.see("Clearbrook Dental", "org", source=MAIL, ts=NOW)
        self.assertIsNone(self.store.find_entity_exact("Clearbrook Dental"))
        # The lazy sweep of the next event (42 days on) folds the first
        # sighting into a summary and deletes it; this one is the second
        # session.
        self.see("Clearbrook Dental", "org", source=MAIL, ts=NOW + 42 * DAY,
                 now=time.time() + 42 * DAY)
        row = self.entity("Clearbrook Dental")
        self.assertIsNotNone(row)
        self.assertEqual(row["kind"], "org")
        # The summary outlives the promotion as kind evidence.
        self.assertEqual(self.store.entity_candidate_summary(
            "clearbrook dental")["mentions"], 1)

    def test_a_summary_holds_no_text(self):
        span = "Renewal with Clearbrook Dental is due"
        self.see("Clearbrook Dental", "org", source=MAIL, ts=NOW,
                 text=span, meta={"project": "Quayside Upgrade"})
        self.see("Clearbrook Dental", "tool", source=MAIL, ts=NOW + 5 * MIN,
                 text="Clearbrook Dental intake form",
                 meta={"project": "Quayside Upgrade"})
        wall = time.time()
        gone = cand.sweep_expired(self.store, now=wall + 31 * DAY)
        self.assertEqual(gone["mentions"], 2)
        self.assertEqual(self.pending("Clearbrook Dental"), [])
        with self.store._lock:
            rows = [dict(r) for r in self.store._conn.execute(
                "SELECT * FROM entity_candidate_summaries").fetchall()]
        self.assertEqual(len(rows), 1)
        blob = json.dumps(rows[0]).casefold()
        for text in ("clearbrook", "dental", "renewal", "quayside",
                     "upgrade", "intake"):
            self.assertNotIn(text, blob)
        s = self.store.entity_candidate_summary("clearbrook dental")
        self.assertEqual(rows[0]["name_hash"],
                         Store.entity_key_hash("clearbrook dental"))
        self.assertEqual((s["mentions"], s["sessions"]), (2, 1))
        self.assertEqual(s["source_classes"], ["connector_mail"])
        self.assertEqual(sorted(s["kinds"]), ["org", "tool"])
        self.assertEqual((s["first_seen"], s["last_seen"]),
                         (NOW, NOW + 5 * MIN))
        self.assertFalse(s["weak"])
        self.assertEqual(len(s["scope"]), 64)       # a hash, not the name

    def test_the_summary_votes_and_counts(self):
        # Two sessions of "tool" expired; one "org" sighting now. The
        # summary is two sessions, so the name promotes, and its kind is
        # what all three sightings say.
        for day in (0, 1):
            self.see("Routeline", "tool", source=MAIL, ts=NOW + day * DAY)
            if day == 0:
                self.assertIsNone(self.store.find_entity_exact("Routeline"))
        eid = self.store.find_entity_exact("Routeline")
        self.assertIsNotNone(eid)
        self.store.delete_entity(eid)       # plain delete: no tombstone
        for day in (0, 1):
            self.store.record_entity_mention(
                "Routeline", proposed_kind="tool", confidence=0.9,
                source_class="connector_mail", event_source=MAIL,
                event_id=900 + day, observed_at=NOW + day * DAY)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
        s = self.store.entity_candidate_summary("routeline")
        self.assertEqual((s["sessions"], s["kinds"]["tool"]["n"]), (2, 2))
        self.see("Routeline", "org", source=MAIL, ts=NOW + 40 * DAY,
                 now=time.time() + 40 * DAY)
        self.assertEqual(self.entity("Routeline")["kind"], "tool")

    def test_bind_only_sightings_count_no_session_after_expiry_either(self):
        self.see("Northwind", "org", source=NOTE, ts=NOW)
        self.see("Northwind", "org", source=SCREEN, ts=NOW + DAY)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
        s = self.store.entity_candidate_summary("northwind")
        self.assertEqual((s["mentions"], s["sessions"], s["source_classes"]),
                         (2, 0, []))
        self.see("Northwind", "org", source=MAIL, ts=NOW + 40 * DAY,
                 now=time.time() + 40 * DAY)
        self.assertIsNone(self.store.find_entity_exact("Northwind"))
        self.assertEqual(cand.evaluate(self.store, "northwind").sessions, 1)

    def test_a_weak_name_typed_six_weeks_apart_promotes(self):
        self.see("loading yard", "place", source=CHAT, ts=NOW,
                 text="the loading yard is full")
        self.see("loading yard", "place", source=CHAT, ts=NOW + 42 * DAY,
                 text="the loading yard again", now=time.time() + 42 * DAY)
        self.assertIsNotNone(self.entity("loading yard"))

    def test_a_code_summary_corroborates_a_named_kind(self):
        self.see("a16z", "org", source=MAIL, ts=NOW)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
        self.assertEqual(self.store.entity_candidate_summary("a16z")
                         ["kinds"]["org"]["c"], 0.9)
        self.see("a16z", "org", source=MAIL, ts=NOW + 42 * DAY,
                 now=time.time() + 42 * DAY)
        self.assertEqual(self.entity("a16z")["kind"], "org")

    def test_one_sitting_swept_twice_is_still_one_session(self):
        # The lazy sweep runs on every record, so the sightings of one
        # sitting usually expire in separate sweeps. The second fold is the
        # same sitting: one session overall, for the kind, and typed.
        wall = time.time()
        evs = [self.see("a16z", "org", source=CHAT, ts=NOW + i * 10 * MIN,
                        text="a16z led the round") for i in range(2)]
        with self.store._lock:
            for i, ev in enumerate(evs):
                self.store._conn.execute(
                    "UPDATE entity_mentions SET created_at = ? "
                    "WHERE event_id = ?", (wall + i * 600, ev))
            self.store._conn.commit()
        cand.sweep_expired(self.store, now=wall + 30 * DAY + 300)
        self.assertEqual(len(self.pending("a16z")), 1)
        cand.sweep_expired(self.store, now=wall + 30 * DAY + 900)
        self.assertEqual(self.pending("a16z"), [])
        s = self.store.entity_candidate_summary("a16z")
        self.assertEqual((s["mentions"], s["sessions"], s["typed_sessions"],
                          s["kinds"]["org"]["s"]), (2, 1, 1, 1))
        # One sitting of one source is no corroboration: a later sighting
        # that names no kind makes a label, not an org (G1).
        self.see("a16z", None, source=MAIL, ts=NOW + 40 * DAY,
                 now=wall + 40 * DAY)
        self.assertEqual(self.entity("a16z")["kind"], "label")

    def test_a_weak_name_typed_once_swept_twice_waits(self):
        wall = time.time()
        evs = [self.see("loading yard", "place", source=CHAT,
                        ts=NOW + i * 10 * MIN, text="the loading yard is full")
               for i in range(2)]
        with self.store._lock:
            for i, ev in enumerate(evs):
                self.store._conn.execute(
                    "UPDATE entity_mentions SET created_at = ? "
                    "WHERE event_id = ?", (wall + i * 600, ev))
            self.store._conn.commit()
        for t in (300, 900, 1500):
            cand.sweep_expired(self.store, now=wall + 30 * DAY + t)
        s = self.store.entity_candidate_summary("loading yard")
        self.assertEqual((s["typed_sessions"], s["typed_last"]),
                         (1, NOW + 10 * MIN))
        # Typed again in that same sitting's afterglow (an import of the
        # same chat): still one sitting, so it waits.
        self.see("loading yard", "place", source=CHAT, ts=NOW + 20 * MIN,
                 text="loading yard", now=wall + 31 * DAY)
        self.assertIsNone(self.store.find_entity_exact("loading yard"))
        self.assertEqual(cand.evaluate(self.store, "loading yard").reason,
                         "weak")

    def test_a_proper_spelling_that_expired_still_says_it_is_a_name(self):
        # Seen once written as a name, then once in lowercase six weeks on:
        # the expired sighting keeps the name from being weak.
        self.see("Ski Resort", "place", source=MAIL, ts=NOW)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
        self.assertFalse(self.store.entity_candidate_summary("ski resort")
                         ["weak"])
        self.see("ski resort", "place", source=MAIL, ts=NOW + 40 * DAY,
                 now=time.time() + 40 * DAY)
        self.assertIsNotNone(self.store.find_entity_exact("ski resort"))

    def test_the_filing_project_survives_expiry(self):
        # A model "project" whose every sighting, expired ones included, was
        # filed under one project is a label; one whose expired sightings
        # were filed elsewhere is not scoped, so it is a topic.
        for name, first, then, want in (
                ("Harbor Loop", "Alpha Build", "Alpha Build", "label"),
                ("Pier Loop Rebuild", "Alpha Build", "Beta Build", "idea")):
            self.see(name, "project", source=MAIL, ts=NOW,
                     meta={"project": first})
            cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
            self.see(name, "project", source=MAIL, ts=NOW + 40 * DAY,
                     meta={"project": then}, now=time.time() + 40 * DAY)
            self.assertEqual(self.entity(name)["kind"], want, name)

    def test_the_summary_expires(self):
        self.see("Clearbrook Dental", "org", source=MAIL, ts=NOW)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
        self.assertEqual(self.store.entity_candidate_summary_count(), 1)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY + 364 * DAY)
        self.assertEqual(self.store.entity_candidate_summary_count(), 1)
        cand.sweep_expired(self.store, now=time.time() + 31 * DAY + 366 * DAY)
        self.assertEqual(self.store.entity_candidate_summary_count(), 0)
        with patch.dict(os.environ,
                        {"QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS": "10"}):
            self.see("Quarrystone", "org", source=MAIL, ts=NOW)
            cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
            self.assertEqual(self.store.entity_candidate_summary_count(), 1)
            cand.sweep_expired(self.store, now=time.time() + 42 * DAY)
            self.assertEqual(self.store.entity_candidate_summary_count(), 0)

    def test_dismiss_forget_and_tombstones_delete_the_summary(self):
        def summarised(name):
            self.see(name, "org", source=MAIL, ts=NOW)
            cand.sweep_expired(self.store, now=time.time() + 31 * DAY)
            self.assertIsNotNone(self.store.entity_candidate_summary(
                entity_name_key(name)), name)

        # Dismiss: the pending sighting and the summary go with the
        # tombstone (a note's sighting keeps the name pending, not minted).
        summarised("Halcyon")
        self.see("Halcyon", "org", source=NOTE, ts=NOW + 40 * DAY,
                 now=time.time() + 40 * DAY)
        self.assertIsNone(self.store.find_entity_exact("Halcyon"))
        self.assertTrue(cand.dismiss(self.store, "Halcyon")["ok"])
        self.assertIsNone(self.store.entity_candidate_summary("halcyon"))
        # Forget: an entity whose name (or an alias) has a summary.
        summarised("Kestrel Labs")
        eid = self.store.resolve_entity("Kestrel", "org", ts=NOW,
                                        kind_source="user")
        self.confirm_alias(eid, "Kestrel Labs")
        self.store.delete_entity(eid, tombstone=True, reason="forget")
        self.assertIsNone(self.store.entity_candidate_summary("kestrel labs"))
        # A tombstone written directly.
        summarised("Orbit Freight")
        self.store.tombstone_entity_name("Orbit Freight", reason="x")
        self.assertIsNone(self.store.entity_candidate_summary("orbit freight"))
        # A plain delete of the row the name became.
        summarised("Monsoon")
        eid = cand.add_manual(self.store, "Monsoon", "org", now=NOW)["id"]
        self.assertIsNotNone(self.store.entity_candidate_summary("monsoon"))
        self.store.delete_entity(eid)
        self.assertIsNone(self.store.entity_candidate_summary("monsoon"))

    def test_a_store_without_the_table_reads_none(self):
        self.store.close()
        raw = sqlite3.connect(Path(self.tmp) / "t.db")
        raw.execute("DROP TABLE entity_candidate_summaries")
        raw.commit()
        raw.close()
        ro = Store(db_path=Path(self.tmp) / "t.db",
                   audio_dir=Path(self.tmp) / "audio", readonly=True)
        self.addCleanup(ro.close)
        self.assertIsNone(ro.entity_candidate_summary("halcyon"))
        self.assertEqual(ro.entity_candidate_summary_count(), 0)
        self.assertEqual(ro.fold_entity_candidates({"x": {}}, [1]), 0)


# -------------------------------------------------------------- Check C --
class OrgFilingTargetTests(_ExtractCase):
    def facts_of(self, event_id: int, *texts: str) -> list[int]:
        out = []
        with self.store._lock:
            for t in texts:
                cur = self.store._conn.execute(
                    "INSERT INTO facts (kind, text, source_event_id, "
                    "extracted_at) VALUES ('claim', ?, ?, ?)",
                    (t, event_id, NOW))
                out.append(int(cur.lastrowid))
            self.store._conn.commit()
        return out

    def test_filing_under_an_org_stamps_its_facts(self):
        # The model once read it as an org: a short legal form after one
        # word needs that (fix pass B).
        self.sight("Volvo AB", "org", event_id=900)
        ev = int(self.store.insert(Event(
            time=NOW, modality=Modality.TEXT, raw="note", source=NOTE,
            meta={"project": "Volvo AB"})))
        fids = self.facts_of(ev, "The renewal is signed",
                             "Pricing holds for a year")
        self.assertEqual(projects.stamp_event_facts(self.store, ev,
                                                    "Volvo AB"), 2)
        eid = self.store.find_entity_exact("Volvo AB")
        self.assertEqual(self.kind(eid), ("org", "filing"))
        for fid in fids:
            about = self.store.relations_of("fact", fid)["out"]
            self.assertIn(eid, {int(e["obj_id"]) for e in about
                                if e["obj_type"] == "entity"})
        # The registry lists it as a filing target.
        self.file_note("Volvo AB")
        self.assertIn("Volvo AB",
                      {p["name"] for p in projects.registry(self.store)})

    def rollup_world(self):
        """Two filing targets (an org and a project), a tool that lives in
        the org's notes, an unfiled model project, and an unfiled org."""
        self.sight("Volvo AB", "org", event_id=900)
        volvo = projects.ensure_filed("Volvo AB", store=self.store, now=NOW)
        atlas = projects.ensure_filed("Atlas", store=self.store, now=NOW)
        tool = self.store.resolve_entity("Routeline", "tool", ts=NOW,
                                         kind_source="model")
        loop = self.store.resolve_entity("Eastern loop", "project", ts=NOW,
                                         kind_source="model")
        other = self.store.resolve_entity("Pemberly", "org", ts=NOW,
                                          kind_source="model")
        for i in range(4):
            fid = self.facts_of(0, f"Routeline routes the Volvo AB run {i}")[0]
            for eid in (volvo, tool, loop):
                self.store.add_relation("fact", fid, "about", "entity", eid,
                                        origin="derived", ts=NOW)
        return volvo, atlas, tool, loop, other

    def test_a_filed_org_is_a_rollup_home(self):
        volvo, atlas, tool, loop, other = self.rollup_world()
        entities = {int(e["id"]): e for e in self.store.all_entities()}
        self.assertEqual(project_rollup.homes(self.store, entities),
                         {volvo, atlas})
        homes_of = {a["entity_id"]: a["project_id"]
                    for a in project_rollup.compute(self.store)}
        self.assertEqual(homes_of.get(tool), volvo)
        # An unfiled model "project" is no home once filing targets exist.
        self.assertEqual(homes_of.get(loop), volvo)
        self.assertNotIn(other, homes_of)

    def test_without_filing_targets_the_home_kinds_apply(self):
        tool = self.store.resolve_entity("Routeline", "tool", ts=NOW,
                                         kind_source="model")
        loop = self.store.resolve_entity("Eastern loop", "project", ts=NOW,
                                         kind_source="model")
        org = self.store.resolve_entity("Pemberly", "org", ts=NOW,
                                        kind_source="model")
        entities = {int(e["id"]): e for e in self.store.all_entities()}
        self.assertEqual(project_rollup.homes(self.store, entities), {loop})
        with patch.dict(os.environ,
                        {"QUILL_ROLLUP_HOME_KINDS": "project,org"}):
            self.assertEqual(project_rollup.homes(self.store, entities),
                             {loop, org})
        self.assertNotIn(tool, project_rollup.homes(self.store, entities))

    def test_the_filed_homes_switch_restores_the_old_rule(self):
        volvo, atlas, tool, loop, other = self.rollup_world()
        entities = {int(e["id"]): e for e in self.store.all_entities()}
        with patch.dict(os.environ, {"QUILL_ROLLUP_FILED_HOMES": "0"}):
            self.assertEqual(project_rollup.homes(self.store, entities),
                             {atlas, loop})

    def state(self) -> tuple:
        with self.store._lock:
            rows = [tuple(r) for r in self.store._conn.execute(
                "SELECT id, kind, kind_source, kind_confidence, updated_at, "
                "last_seen, hidden FROM entities ORDER BY id").fetchall()]
        return rows, self.store.memory_version()

    def test_repeated_saves_never_change_the_kind_or_write(self):
        self.sight("Volvo AB", "org", event_id=900)
        first = {}
        for name in ("Volvo AB", "Atlas"):
            eid = projects.ensure_filed(name, store=self.store, now=NOW,
                                        user_accept=True)
            first[name] = self.kind(eid)
        self.assertEqual(first, {"Volvo AB": ("org", "filing"),
                                 "Atlas": ("project", "filing")})
        before = self.state()
        changes = self.store._conn.total_changes
        # Twice each, then alternating, with and without the user's accept.
        for name in ("Volvo AB", "Volvo AB", "Atlas", "Atlas", "Volvo AB",
                     "Atlas", "Volvo AB"):
            for accept in (True, False):
                eid = projects.ensure_filed(name, store=self.store,
                                            now=NOW + 99, user_accept=accept)
                self.assertEqual(self.kind(eid), first[name], name)
        self.assertEqual(self.store._conn.total_changes, changes)
        self.assertEqual(self.state(), before)

    def test_later_evidence_does_not_change_the_first_decision(self):
        atlas = projects.ensure_filed("Atlas", store=self.store, now=NOW)
        self.assertEqual(self.kind(atlas), ("project", "filing"))
        # A contact at atlas.com appears: an email-domain org by now. The
        # filing decided first, and the vote holds a filing row.
        self.add_email("ann@atlas.com", person="Ann Lee")
        self.assertEqual(projects.ensure_filed("Atlas", store=self.store,
                                               now=NOW), atlas)
        self.see("Atlas", "org", source=MAIL, ts=NOW + HOUR)
        self.see("Atlas", "org", source=MAIL, ts=NOW + 3 * HOUR)
        self.assertEqual(self.kind(atlas), ("project", "filing"))
        # A company first seen by the model, then filed: the filing decides
        # once (org), and the model's later "project" changes nothing.
        self.see("Halcyon GmbH", "tool", source=MAIL, ts=NOW)
        self.see("Halcyon GmbH", "tool", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Halcyon GmbH")
        self.assertEqual(projects.ensure_filed("Halcyon GmbH",
                                               store=self.store, now=NOW),
                         eid)
        self.assertEqual(self.kind(eid), ("org", "filing"))
        for i in range(3):
            self.see("Halcyon GmbH", "project", source=MAIL,
                     ts=NOW + (4 + i) * HOUR)
        self.assertEqual(self.kind(eid), ("org", "filing"))

    def test_a_user_kind_is_never_touched_by_a_save(self):
        eid = self.store.resolve_entity("Volvo AB", "idea", ts=NOW,
                                        kind_source="user")
        changes = self.store._conn.total_changes
        self.assertEqual(projects.ensure_filed("Volvo AB", store=self.store,
                                               now=NOW), eid)
        self.assertEqual(self.kind(eid), ("idea", "user"))
        self.assertEqual(self.store._conn.total_changes, changes)


if __name__ == "__main__":
    unittest.main()
