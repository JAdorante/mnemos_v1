"""Entity model, Phase 2: type by evidence.

What kind of thing a name is comes from every sighting of it, not from the
first one. Each sighting votes with its model confidence times how much its
source is worth (what the user typed, then mail, speech, documents,
assistant notes, screen text). Deterministic priors win outright: a filed
project is a project, an email domain is an org, a value is a thing, an id
is a label. A model "project" is never enough to make a project, from any
source. An entity's kind changes only on clear, repeated evidence, and
never when a rule, a filing or the user set it.

Also here: the `label` kind everywhere a kind is listed, and the extractor's
entity-kind prompt with its kill switch (QUILL_EXTRACT_ENTITY_V2=0 gives back
the earlier prompt, schema and stamps byte for byte).

Every test runs against a real Store on a temp db and the real extractor
persist path; only the model call itself is replaced.
"""
from __future__ import annotations

import json
import os
import tempfile
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
from app.services import projects
from app.services.kg_keys import entity_name_key
from app.services.resolution import Resolver
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
        self.tmp = tempfile.mkdtemp(prefix="quill_typing_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        # The filed tier of the project registry lives in the data dir;
        # org scopes come from the membership file. Neither is the seat's.
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp,
                                      "QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)
        scopes = patch.object(projects, "_scopes", lambda: [])
        scopes.start()
        self.addCleanup(scopes.stop)

    def sight(self, name, kind, *, cls="connector_mail", event_id=None,
              ts=NOW, conf=0.8, entity_id=None, source=MAIL, typed=False,
              filing_project=None):
        return self.store.record_entity_mention(
            name, proposed_kind=kind, confidence=conf, source_class=cls,
            event_source=source, event_id=event_id, observed_at=ts,
            entity_id=entity_id, typed=typed, filing_project=filing_project,
            weak=et.common_noun_like(name))

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


class _ExtractCase(_StoreCase):
    """The extractor's own persist path, against the temp store."""

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {
            "QUILL_HEADS": "off", "QUILL_USAGE_LEDGER": "0",
            "QUILL_PEOPLE_V2": "1"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_CANDIDATE_SESSIONS",
                    "QUILL_ENTITY_CANDIDATE_TTL_DAYS"):
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

    def see(self, name: str, kind: str | None = "org", *, source: str,
            ts: float, text: str | None = None, conf: float = 0.8,
            parent: str | None = None, meta: dict | None = None) -> int:
        """One event from `source` in which the model read `name`."""
        text = text if text is not None else f"A note that mentions {name}."
        modality = Modality.TEXT
        ev = int(self.store.insert(Event(
            time=ts, modality=modality, raw=text, source=source,
            meta=dict(meta or {}))))
        ent = {"name": name, "kind": kind, "confidence": conf,
               "source_span": text}
        if parent is not None:
            ent["parent"] = parent
        self.ex._persist_entities({"entities": [ent], "relations": []},
                                  ev, ts, event_source=source, text=text)
        return ev

    def entity(self, name: str) -> dict | None:
        eid = self.store.find_entity_exact(name)
        return self.store.get_entity(eid) if eid else None


# --------------------------------------------------------------- shapes --
class ValueLiteralTests(unittest.TestCase):
    def test_values(self):
        for name in ("$49", "€1,200.50", "$1.2M", "49 USD", "12 dollars",
                     "12%", "-3%", "12 percent", "2026-11-14", "11/14",
                     "Nov 14", "November 14, 2026", "Friday", "next Friday",
                     "tomorrow", "3pm", "12:30", "42", "1,000"):
            self.assertTrue(et.value_literal(name), name)

    def test_names_are_not_values(self):
        # 3M is a company and Sun a brand; a bare amount with a suffix or a
        # short weekday is not read as a value.
        # A quarter ("Q3 2026") is a value since fix pass B.
        for name in ("3M", "Sun", "Mon", "Acme", "c4", "Phase 1",
                     "Windows 11", "GPT-4", "", None):
            self.assertFalse(et.value_literal(name), name)

    def test_shape_helpers_live_in_one_module(self):
        # Phase 1's helpers moved here; the candidate gate uses these.
        self.assertIs(cand.entity_typing.code_like, et.code_like)
        self.assertIs(cand.entity_typing.common_noun_like,
                      et.common_noun_like)


# ---------------------------------------------------------------- kinds --
class KindVocabularyTests(unittest.TestCase):
    def test_normalizer_reads_both_extractor_vocabularies(self):
        from app.services.name_quality import normalize_entity_kind as n
        for raw, want in (("topic", "idea"), ("concept", "idea"),
                          ("theme", "idea"), ("label", "label"),
                          ("tag", "label"), ("milestone", "label"),
                          ("codename", "label"), ("identifier", "label"),
                          ("product", "tool"), ("other", "idea"),
                          ("tool", "tool"), ("org", "org"),
                          ("place", "place"), ("project", "project")):
            self.assertEqual(n(raw), want, raw)
        self.assertIsNone(n("wizard", unknown=None))

    def test_store_kinds_include_label(self):
        from app.services.name_quality import ENTITY_KINDS
        self.assertEqual(ENTITY_KINDS, ("org", "project", "tool", "place",
                                        "idea", "label", "thing"))
        self.assertEqual(Store.canonical_entity_kind("label"), "label")
        self.assertEqual(Store.canonical_entity_kind("milestone"), "label")
        self.assertEqual(Store.canonical_entity_kind("topic"), "idea")
        self.assertIsNone(Store.canonical_entity_kind("wizard"))

    def test_consumers_of_kind(self):
        # A label may never name a stretch of work, never anchors a fact,
        # is a category the map offers, and draws as an idea.
        from app.services import fact_gate, graph, mmr
        from app.services.context import naming, surfaces
        self.assertIn("label", surfaces._UNNAMEABLE_KINDS)
        self.assertIn("label", naming._UNNAMEABLE)
        self.assertNotIn("label", fact_gate._ANCHOR_KINDS)
        self.assertIn("label", graph._ALLOWED["entity"])
        self.assertEqual(graph.entity_constellation_kind("label"), "idea")
        self.assertIn("label", mmr._ENTITY)

    def test_tab_has_a_labels_group(self):
        from app.api import profile_page
        html = profile_page.PROFILE_PAGE
        self.assertIn("const ENTITY_KINDS=['org','project','tool','place',"
                      "'idea','label','thing'];", html)
        self.assertIn("['label','Labels']", html)
        self.assertIn('<option value="label">Label</option>', html)
        self.assertIn("['org','project','tool','place','idea','label']"
                      ".includes(k)", html)


class LabelStoreTests(_StoreCase):
    def test_set_entity_kind_accepts_label(self):
        eid = self.store.resolve_entity("c4", "project", ts=NOW)
        self.assertTrue(self.store.set_entity_kind(eid, "label"))
        self.assertEqual(self.kind(eid), ("label", "user"))
        self.assertTrue(self.store.set_entity_kind(eid, "milestone"))
        self.assertEqual(self.kind(eid), ("label", "user"))

    def test_convert_person_to_label_and_topic(self):
        pid = self.store.insert_person("Halcyon", ts=NOW)
        out = self.store.convert_person_to_entity(pid, "label")
        self.assertTrue(out)
        self.assertEqual(self.kind(out["entity_id"]), ("label", "user"))
        pid2 = self.store.insert_person("Monsoon", ts=NOW)
        out2 = self.store.convert_person_to_entity(pid2, "topic")
        self.assertEqual(self.kind(out2["entity_id"]), ("idea", "user"))
        pid3 = self.store.insert_person("Zephyr", ts=NOW)
        self.assertIsNone(self.store.convert_person_to_entity(pid3, "other"))
        self.assertIsNone(self.store.convert_person_to_entity(pid3, "wizard"))

    def test_map_reclassify_to_label(self):
        from app.services import graph
        eid = self.store.resolve_entity("r2", "project", ts=NOW)
        out = graph.reclassify_constellation_node(
            self.store, f"entity:{eid}", "label")
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.kind(eid), ("label", "user"))

    def test_labels_are_no_asr_spelling_hint(self):
        from app.services import speakers, vocabulary
        self.store.resolve_entity("Atlas Rebuild", "project", ts=NOW)
        self.store.resolve_entity("Northwind", "org", ts=NOW)
        eid = self.store.resolve_entity("Kestrel", "project", ts=NOW)
        self.store.set_entity_kind(eid, "label")
        with patch("app.storage.get_store", return_value=self.store), \
                patch.object(speakers.speakers, "enrolled_names",
                             return_value=[]):
            terms = vocabulary.VocabularyProvider()._build()
        self.assertIn("Atlas Rebuild", terms["projects"])
        self.assertIn("Northwind", terms["orgs"])
        self.assertNotIn("Kestrel", terms["projects"] + terms["orgs"])

    def test_ambient_cleanup_hides_a_person_shaped_label(self):
        from app.services import ambient_cleanup as ac
        eid = self.store.resolve_entity("Bill Clinton", "org", ts=NOW)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entities SET kind = 'label' WHERE id = ?", (eid,))
            self.store._conn.commit()
        plan = {e["name"]: e for e in ac.plan_entities(self.store, limit=50)}
        self.assertEqual(plan["Bill Clinton"]["action"], "hide_person")


# ----------------------------------------------------------------- vote --
class SourceWeightTests(unittest.TestCase):
    def test_typed_over_mail_over_meeting_over_document_over_assistant(self):
        w = et.source_weight
        typed = w({"typed": 1, "source_class": "direct_message"})
        mail = w({"source_class": "connector_mail"})
        meeting = w({"source_class": "meeting_transcript"})
        doc = w({"source_class": "user_authored_document"})
        assistant = w({"source_class": "assistant_capture"})
        screen = w({"source_class": "email", "event_source": SCREEN})
        self.assertGreater(typed, mail)
        self.assertGreater(mail, meeting)
        self.assertGreater(meeting, doc)
        self.assertGreater(doc, assistant)
        self.assertGreater(assistant, screen)


class CandidateVoteTests(_StoreCase):
    def vote(self, name):
        return et.vote(self.store, name_key=entity_name_key(name), name=name)

    def test_weighted_by_confidence_and_source(self):
        # Two screen sightings say org; one typed sighting says tool.
        self.sight("Halcyon", "org", cls="email", source=SCREEN, event_id=1)
        self.sight("Halcyon", "org", cls="email", source=SCREEN, event_id=2)
        self.sight("Halcyon", "tool", cls="direct_message", source=CHAT,
                   event_id=3, typed=True)
        v = self.vote("Halcyon")
        self.assertEqual((v.kind, v.source), ("tool", "model"))
        self.assertEqual(v.breakdown["counts"], {"org": 2, "tool": 1})
        # Confidence counts too: one sure sighting beats a shaky one.
        self.sight("Monsoon", "org", event_id=4, conf=0.9)
        self.sight("Monsoon", "tool", event_id=5, conf=0.3)
        self.assertEqual(self.vote("Monsoon").kind, "org")

    def test_a_model_project_is_never_a_project(self):
        for i, cls in enumerate(("connector_mail", "user_authored_document",
                                 "meeting_transcript", "direct_message")):
            self.sight("EU CBAM", "project", cls=cls, event_id=10 + i,
                       conf=0.95, typed=(cls == "direct_message"))
        v = self.vote("EU CBAM")
        self.assertEqual((v.kind, v.source), ("idea", "model"))
        self.assertNotIn("project", v.breakdown["scores"])

    def test_a_project_inside_one_filed_project_is_a_label(self):
        self.sight("Option B", "project", event_id=1,
                   filing_project="Atlas")
        self.sight("Option B", "project", event_id=2,
                   filing_project="the Atlas project")
        v = self.vote("Option B")
        self.assertEqual(v.kind, "label")
        self.assertEqual(v.breakdown["filing_project"], "atlas")
        # Sightings outside it, or under two projects: a topic.
        self.sight("Option C", "project", event_id=3, filing_project="Atlas")
        self.sight("Option C", "project", event_id=4)
        self.assertEqual(self.vote("Option C").kind, "idea")
        self.sight("Option D", "project", event_id=5, filing_project="Atlas")
        self.sight("Option D", "project", event_id=6, filing_project="Orbit")
        self.assertEqual(self.vote("Option D").kind, "idea")

    def test_code_like_name_is_a_label_whatever_the_model_said(self):
        # One sighting, whatever kind it proposes: a label. (G1: an org or
        # tool kind needs a second, independent sighting to agree; see
        # tests/test_entity_decisions.py.)
        for i, name in enumerate(("c4", "9bc12da", "Phase 1", "ENG-482")):
            self.sight(name, "org", event_id=20 + i)
            v = self.vote(name)
            self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                             ("label", "rule", "code_like"), name)

    def test_a_value_is_a_thing(self):
        self.sight("$49", "project", event_id=1)
        v = self.vote("$49")
        self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                         ("thing", "rule", "value_literal"))

    def test_filed_project_is_a_project(self):
        self.file_note("Atlas")
        self.sight("Atlas", "tool", event_id=1, conf=0.95)
        self.sight("Atlas", "tool", event_id=2, conf=0.95)
        v = self.vote("Atlas")
        self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                         ("project", "filing", "filed_project"))
        # "The Atlas project" is the same filed name.
        self.sight("The Atlas project", "idea", event_id=3)
        self.assertEqual(self.vote("The Atlas project").kind, "project")

    def test_shared_scope_is_a_filed_project_but_a_known_one_is_not(self):
        with patch.object(projects, "_scopes", lambda: [
                {"kind": "project", "name": "Orbit", "id": "s1"}]):
            self.sight("Orbit", "idea", event_id=1)
            self.assertEqual(self.vote("Orbit").kind, "project")
        # A project-kind entity the extractor made is the registry's `known`
        # tier: it is no evidence that the name is a project.
        self.store.resolve_entity("Nimbus", "project", ts=NOW)
        self.assertIn("nimbus", {projects.norm(p["name"])
                                 for p in projects.registry(self.store)})
        self.assertNotIn("nimbus", projects.filed_keys())
        self.sight("Nimbus Two", "project", event_id=2)
        self.assertEqual(self.vote("Nimbus Two").kind, "idea")

    def test_filed_keys_never_creates_the_assistant_database(self):
        from app.services.remote_mcp import db_path
        self.assertFalse(db_path().exists())
        self.assertEqual(projects.filed_keys(), frozenset())
        self.assertFalse(db_path().exists())

    def test_email_domain_is_an_org(self):
        pid = self.store.insert_person("Jane Roe", ts=NOW)
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display="jane@acme-robotics.com",
            value_normalized="jane@acme-robotics.com", confidence=0.9,
            attribution_method="email_header", verification_status="attributed",
            source_event_id=None, evidence_quote=None, discourse_role=None,
            ts=NOW, created_by="system", pipeline_version="t")
        self.sight("Acme Robotics", "project", event_id=1)
        v = self.vote("Acme Robotics")
        self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                         ("org", "rule", "email_domain"))
        # A free-mail domain, or a name that is only part of the local part,
        # says nothing.
        self.store.upsert_contact_point(
            person_id=pid, type_="email", value_display="jane.gmailer@gmail.com",
            value_normalized="jane.gmailer@gmail.com", confidence=0.9,
            attribution_method="email_header", verification_status="attributed",
            source_event_id=None, evidence_quote=None, discourse_role=None,
            ts=NOW, created_by="system", pipeline_version="t")
        self.sight("Gmail", "tool", event_id=2)
        self.assertEqual(self.vote("Gmail").kind, "tool")

    def test_no_kind_stated_is_an_idea(self):
        self.sight("Monsoon", None, event_id=1)
        self.sight("Monsoon", "other", event_id=2)
        v = self.vote("Monsoon")
        self.assertEqual((v.kind, v.breakdown["reason"]), ("idea", "no_votes"))


class IncumbentTests(_StoreCase):
    def entity(self, name, kind, *, source=None):
        eid = self.store.resolve_entity(name, kind, ts=NOW,
                                        kind_source=source)
        return eid

    def test_one_dissenting_sighting_never_flips(self):
        # Even the strongest sighting there is (typed, near certain), whose
        # weight alone clears the incumbent's prior and the margin.
        eid = self.entity("Halcyon", "tool", source="model")
        self.sight("Halcyon", "org", event_id=1, conf=0.99, entity_id=eid,
                   typed=True, cls="direct_message", source=CHAT)
        v = et.retype(self.store, eid)
        self.assertEqual(v.breakdown["reason"], "incumbent")
        self.assertEqual(self.kind(eid), ("tool", "model"))

    def test_clear_repeated_evidence_flips_at_model_rank(self):
        eid = self.entity("Halcyon", "tool", source="model")
        self.sight("Halcyon", "org", event_id=1, conf=0.9, entity_id=eid)
        self.sight("Halcyon", "org", event_id=2, conf=0.9, entity_id=eid)
        v = et.retype(self.store, eid)
        self.assertEqual((v.kind, v.breakdown["reason"]), ("org", "flipped"))
        self.assertEqual(self.kind(eid), ("org", "model"))

    def test_weak_sources_need_more_than_two(self):
        # Two screen sightings are worth less than the incumbent's prior.
        eid = self.entity("Halcyon", "tool", source="model")
        for i in (1, 2):
            self.sight("Halcyon", "org", cls="email", source=SCREEN,
                       event_id=i, conf=0.9, entity_id=eid)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid), ("tool", "model"))

    def test_no_flapping_between_two_kinds(self):
        eid = self.entity("Halcyon", "tool", source="model")
        kinds = []
        for i in range(8):
            self.sight("Halcyon", "org" if i % 2 else "tool", event_id=i,
                       conf=0.8, entity_id=eid)
            et.retype(self.store, eid)
            kinds.append(self.kind(eid)[0])
        self.assertEqual(set(kinds), {"tool"})

    def test_a_legacy_project_is_not_flipped_by_the_vote(self):
        # Decision D4 (was: became a topic after two sightings). From
        # before kind provenance, the user may have chosen the kind: the
        # vote never changes it, only a prior or the reviewed repair does.
        eid = self.entity("Pilot Plan", "project")
        self.assertEqual(self.kind(eid), ("project", None))
        for i in range(4):
            self.sight("Pilot Plan", "project", event_id=1 + i,
                       entity_id=eid)
            v = et.retype(self.store, eid)
        self.assertEqual(v.breakdown["reason"], "legacy")
        self.assertEqual(self.kind(eid), ("project", None))

    def test_a_prior_applies_at_once(self):
        # A model-typed row: since D4 was tightened (stage G4) the code-shape
        # prior no longer changes a legacy row (test_entity_decisions,
        # LegacyRowTests).
        eid = self.entity("c4", "project", source="model")
        self.sight("c4", "project", event_id=1, entity_id=eid)
        v = et.retype(self.store, eid)
        self.assertEqual(v.breakdown["reason"], "code_like")
        # Stored at rule rank: a rule decided (fix pass B).
        self.assertEqual(self.kind(eid), ("label", "rule"))

    def test_never_overrides_rule_filing_or_user(self):
        for src, kind in (("rule", "org"), ("filing", "project"),
                          ("user", "tool")):
            name = f"Held {src}"
            eid = self.entity(name, kind, source=src)
            for i in range(6):
                self.sight(name, "place", event_id=100 * len(src) + i,
                           conf=0.99, entity_id=eid, typed=True,
                           cls="direct_message", source=CHAT)
            v = et.retype(self.store, eid)
            self.assertEqual((v.kind, v.source, v.breakdown["reason"]),
                             (kind, src, "held"), src)
            self.assertEqual(self.kind(eid), (kind, src))
            # The vote itself says so too.
            self.assertEqual(et.vote(self.store, entity_id=eid).kind, kind)

    def test_retype_unknown_entity(self):
        self.assertIsNone(et.retype(self.store, 9999))
        v = et.vote(self.store, entity_id=9999)
        self.assertIsNone(v.kind)


# ---------------------------------------------- the extractor end to end --
class ExtractorTypingTests(_ExtractCase):
    def test_a_project_named_in_mail_is_a_topic_not_a_project(self):
        self.see("Northwind Pilot Program", "project", source=MAIL, ts=NOW)
        self.see("Northwind Pilot Program", "project", source=MAIL, ts=NOW + 2 * HOUR)
        row = self.entity("Northwind Pilot Program")
        self.assertEqual((row["kind"], row["kind_source"]), ("idea", "model"))

    def test_the_document_demotion_is_the_one_rule(self):
        # Before: only a document's "project" was demoted. Now every source
        # is, and a document's tool stays a tool.
        self.see("EU CBAM", "project", source=DOC, ts=NOW)
        self.see("EU CBAM", "project", source=DOC, ts=NOW + 2 * HOUR)
        self.assertEqual(self.entity("EU CBAM")["kind"], "idea")
        self.see("Snowflake", "tool", source=DOC, ts=NOW)
        self.see("Snowflake", "tool", source=DOC, ts=NOW + 2 * HOUR)
        self.assertEqual(self.entity("Snowflake")["kind"], "tool")
        from app.services import entity_candidates
        self.assertFalse(hasattr(entity_candidates, "effective_kind"))

    def test_typed_project_is_minted_as_a_topic(self):
        self.see("Atlas Rebuild Plan", "project", source=CHAT, ts=NOW,
                 text="I am starting Atlas Rebuild Plan this week")
        row = self.entity("Atlas Rebuild Plan")
        self.assertEqual((row["kind"], row["kind_source"]), ("idea", "model"))

    def test_filed_project_keeps_its_kind(self):
        eid = projects.ensure_filed("Atlas", store=self.store, now=NOW,
                                    user_accept=True)
        self.assertEqual(self.store.get_entity(eid)["kind_source"], "filing")
        for i in range(4):
            self.see("Atlas", "tool", source=MAIL, ts=NOW + i * HOUR,
                     conf=0.99)
        self.assertEqual(self.kind(eid), ("project", "filing"))

    def test_code_like_name_promotes_as_a_label_with_its_parent(self):
        self.see("c4", "project", source=CHAT, ts=NOW,
                 text="c4 of Atlas is nearly done", parent="Atlas")
        self.assertIsNone(self.store.find_entity_exact("c4"))
        self.see("c4", "project", source=CHAT, ts=NOW + 2 * HOUR,
                 text="c4 is done", parent="Atlas")
        row = self.entity("c4")
        self.assertEqual(row["kind"], "label")
        self.assertEqual({m["parent"] for m in self.store.entity_mentions(
            entity_id=row["id"])}, {"Atlas"})

    def test_a_value_is_never_promoted(self):
        for i in range(4):
            self.see("49 USD", "project", source=MAIL, ts=NOW + i * DAY,
                     text="It costs 49 USD")
        self.assertIsNone(self.store.find_entity_exact("49 USD"))
        self.assertEqual(len(self.store.entity_mentions(
            name_key=entity_name_key("49 USD"))), 4)
        v = cand.evaluate(self.store, entity_name_key("49 USD"))
        self.assertEqual((v.promote, v.reason), (False, "value_literal"))

    def test_kind_flips_on_bind_only_with_clear_evidence(self):
        self.see("Halcyon", "org", source=MAIL, ts=NOW)
        self.see("Halcyon", "org", source=MAIL, ts=NOW + 2 * HOUR)
        row = self.entity("Halcyon")
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))
        self.see("Halcyon", "tool", source=MAIL, ts=NOW + 3 * HOUR, conf=0.9)
        self.see("Halcyon", "tool", source=MAIL, ts=NOW + 4 * HOUR, conf=0.9)
        # 2 x 0.9 x 0.8 = 1.44 against org's 2 x 0.8 x 0.8 + prior 0.5:
        # not a clear lead yet.
        self.assertEqual(self.entity("Halcyon")["kind"], "org")
        self.see("Halcyon", "tool", source=MAIL, ts=NOW + 5 * HOUR, conf=0.9)
        self.see("Halcyon", "tool", source=MAIL, ts=NOW + 6 * HOUR, conf=0.9)
        self.assertEqual(self.entity("Halcyon")["kind"], "tool")

    def test_a_kind_the_user_set_survives_extraction(self):
        self.see("Halcyon", "org", source=MAIL, ts=NOW)
        self.see("Halcyon", "org", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Halcyon")
        self.assertTrue(self.store.set_entity_kind(eid, "place"))
        for i in range(6):
            self.see("Halcyon", "org", source=CHAT, ts=NOW + (3 + i) * HOUR,
                     text="Halcyon again", conf=0.99)
        self.assertEqual(self.kind(eid), ("place", "user"))

    def test_screen_sightings_bind_and_vote_but_weigh_little(self):
        self.see("Halcyon", "tool", source=MAIL, ts=NOW)
        self.see("Halcyon", "tool", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Halcyon")
        for i in range(4):
            self.see("Halcyon", "org", source=SCREEN, ts=NOW + (3 + i) * HOUR,
                     conf=0.9)
        self.assertEqual(len(self.store.entity_mentions(entity_id=eid)), 6)
        self.assertEqual(self.kind(eid)[0], "tool")

    def test_flag_off_mints_at_first_sight_with_the_vote_kind(self):
        with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "0"}):
            self.see("Orbitron", "project", source=MAIL, ts=NOW)
            self.see("r2", "org", source=MAIL, ts=NOW)
            self.see("Postgres", "tool", source=MAIL, ts=NOW)
        self.assertEqual(self.entity("Orbitron")["kind"], "idea")
        self.assertEqual(self.entity("r2")["kind"], "label")
        self.assertEqual(self.entity("Postgres")["kind"], "tool")
        self.assertEqual([m["status"] for m in self.store.entity_mentions(
            name_key="postgres")], ["bound"])

    def test_candidate_listing_and_dismiss_use_the_vote(self):
        self.see("Northwind Pilot Program", "project", source=MAIL, ts=NOW)
        listed = {c["name"]: c for c in cand.list_candidates(self.store)}
        c = listed["Northwind Pilot Program"]
        self.assertEqual((c["proposed_kind"], c["proposed_kinds"]),
                         ("idea", {"project": 1}))
        self.see("Brightline", "org", source=MAIL, ts=NOW)
        self.assertTrue(cand.dismiss(self.store, "Brightline")["ok"])
        with self.store._lock:
            row = self.store._conn.execute(
                "SELECT kind FROM entity_tombstones").fetchone()
        self.assertEqual(row["kind"], "org")


class TypingEdgeCaseTests(_ExtractCase):
    """Added by the Phase 2 verification: what the vote does with the same
    event read twice, spellings that share a key, aliases, hidden rows,
    empty names, a malformed `parent`, a failing ledger and a user write
    racing the vote."""

    def persist(self, ev: int, name: str, kind: str, *, ts: float,
                conf: float = 0.8, parent=None, text: str = "") -> None:
        ent = {"name": name, "kind": kind, "confidence": conf,
               "source_span": text or name}
        if parent is not None:
            ent["parent"] = parent
        self.ex._persist_entities({"entities": [ent], "relations": []},
                                  ev, ts, event_source=MAIL,
                                  text=text or f"A note about {name}.")

    def test_reextracting_one_event_is_one_sighting(self):
        self.see("Halcyon", "org", source=MAIL, ts=NOW)
        self.see("Halcyon", "org", source=MAIL, ts=NOW + 2 * HOUR)
        eid = self.store.find_entity_exact("Halcyon")
        ev = int(self.store.insert(Event(
            time=NOW + 3 * HOUR, modality=Modality.TEXT,
            raw="A note about Halcyon.", source=MAIL, meta={})))
        # Three passes over one event: three rows would be worth 2.38, past
        # org's 1.28 + prior + margin; one row is not.
        for _ in range(3):
            self.persist(ev, "Halcyon", "tool", ts=NOW + 3 * HOUR, conf=0.99)
        self.assertEqual(len([m for m in self.store.entity_mentions(
            entity_id=eid) if m["event_id"] == ev]), 1)
        self.assertEqual(self.kind(eid), ("org", "model"))

    def test_spellings_that_share_a_key_vote_together(self):
        self.assertEqual(entity_name_key("Café Nimbus"),
                         entity_name_key("CAFE NIMBUS"))
        self.sight("Café Nimbus", "org", event_id=1)
        self.sight("CAFE NIMBUS", "org", event_id=2)
        self.sight("cafe nimbus", "tool", event_id=3)
        v = et.vote(self.store, name_key=entity_name_key("Cafe Nimbus"))
        self.assertEqual(v.kind, "org")
        self.assertEqual(v.breakdown["counts"], {"org": 2, "tool": 1})

    def test_sightings_under_an_alias_count_for_the_entity(self):
        eid = self.store.resolve_entity("Halcyon Labs", "tool", ts=NOW,
                                        kind_source="model")
        self.store.touch_entity(eid, NOW, alias="HLabs")
        for i in (1, 2):
            self.sight("HLabs", "org", event_id=i, conf=0.9, entity_id=eid)
        v = et.retype(self.store, eid)
        self.assertEqual(v.breakdown["reason"], "flipped")
        self.assertEqual(self.kind(eid), ("org", "model"))

    def test_a_hidden_value_row_is_never_rekinded(self):
        eid = self.store.resolve_entity("Friday", "thing", ts=NOW,
                                        kind_source="rule", hidden=True)
        for i in range(4):
            self.sight("Friday", "org", event_id=i, conf=0.99,
                       entity_id=eid)
        v = et.retype(self.store, eid)
        # A rule row is voted on by the priors again (fix pass B): the
        # value prior keeps it a thing.
        self.assertEqual(v.breakdown["reason"], "value_literal")
        self.assertEqual(self.kind(eid), ("thing", "rule"))

    def test_empty_and_blank_names(self):
        v = et.vote(self.store, name_key="")
        self.assertEqual((v.kind, v.breakdown["reason"]), ("idea", "no_votes"))
        for fn in (et.code_like, et.common_noun_like, et.value_literal):
            self.assertFalse(fn("   "), fn.__name__)
        ctx = cand.context(self.store, anchor=None, now=NOW,
                           event_source=MAIL, source_class="connector_mail",
                           mint_ok=True)
        from app.services import resolution
        self.assertEqual(cand.observe(self.store, resolution.resolver, "  ",
                                      "org", ctx=ctx), cand.Observed())
        self.assertEqual(self.store.entity_mentions(), [])

    def test_a_malformed_parent_keeps_the_sighting(self):
        # A local model that ignores the schema: the sighting still counts.
        for i, parent in enumerate(({"name": "Atlas"}, ["Atlas"])):
            ev = int(self.store.insert(Event(
                time=NOW + i * HOUR, modality=Modality.TEXT,
                raw="A note about Halcyon.", source=MAIL, meta={})))
            self.persist(ev, "Halcyon", "org", ts=NOW + i * HOUR,
                         parent=parent)
        ev = int(self.store.insert(Event(
            time=NOW + 2 * HOUR, modality=Modality.TEXT,
            raw="A note about Brightline.", source=MAIL, meta={})))
        self.persist(ev, "Brightline", "org", ts=NOW + 2 * HOUR,
                     parent="  the   Atlas\n project " + "x" * 200)
        rows = self.store.entity_mentions(name_key="halcyon")
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["parent"] for r in rows}, {None})
        (b,) = self.store.entity_mentions(name_key="brightline")
        self.assertTrue(b["parent"].startswith("the Atlas project x"))
        self.assertEqual(len(b["parent"]), cand._PARENT_MAX)

    def test_gate_off_still_mints_when_the_ledger_write_fails(self):
        import sqlite3
        boom = sqlite3.OperationalError("database is locked")
        with patch.object(self.store, "record_entity_mention",
                          side_effect=boom):
            self.see("Orbitron", "project", source=MAIL, ts=NOW)
            self.assertIsNone(self.store.find_entity_exact("Orbitron"))
            with patch.dict(os.environ, {"QUILL_ENTITY_CANDIDATES": "0"}):
                self.see("Orbitron", "project", source=MAIL, ts=NOW + HOUR)
        # Minted at first sight, and a model "project" is still no project.
        self.assertEqual(self.kind(self.store.find_entity_exact("Orbitron")),
                         ("idea", "model"))

    def test_a_user_kind_set_while_the_vote_runs_stands(self):
        eid = self.store.resolve_entity("Halcyon", "tool", ts=NOW,
                                        kind_source="model")
        for i in (1, 2):
            self.sight("Halcyon", "org", event_id=i, conf=0.9, entity_id=eid)
        real = et.vote

        def racing(store, **kw):
            v = real(store, **kw)
            store.set_entity_kind(eid, "place")        # the user, meanwhile
            return v
        with patch.object(et, "vote", racing):
            v = et.retype(self.store, eid)
        self.assertEqual((v.kind, v.breakdown["written"]), ("org", False))
        self.assertEqual(self.kind(eid), ("place", "user"))


# --------------------------------------------------- the extraction call --
class ExtractorPromptTests(unittest.TestCase):
    def setUp(self):
        # patch.dict restores the whole environment on stop, so the flags
        # popped below come back as they were.
        env = patch.dict(os.environ, {"QUILL_HEADS": "off",
                                      "QUILL_USAGE_LEDGER": "0"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_EXTRACT_ENTITY_V2", "QUILL_EXTRACT_PROMPT_VERSION",
                    "QUILL_EXTRACT_SCHEMA_VERSION", "QUILL_EXTRACT_CONTEXT",
                    "QUILL_EXTRACT_IDEAS", "QUILL_EXTRACT_SECTION_HINT",
                    "QUILL_EXTRACT_VOCAB"):
            os.environ.pop(var, None)

    def capture(self, fn, *args):
        seen = {}

        def fake(task, *, system, messages, schema, **kw):
            seen.update(system=system, schema=schema)
            return {}
        from app.services.model_router import router
        with patch.object(router, "complete_json", side_effect=fake):
            fn(*args)
        return seen

    def call_extractor(self):
        from app.services.extractor import Extractor
        with patch("app.services.feedback_learning.extraction_negatives_block",
                   return_value=""):
            return self.capture(Extractor(store=None)._extract_text,
                                "We use Postgres for Atlas.")

    def test_new_prompt_and_schema_by_default(self):
        from app.services import extractor as ex
        seen = self.call_extractor()
        items = seen["schema"]["properties"]["entities"]["items"]
        self.assertEqual(items["properties"]["kind"]["enum"],
                         ["org", "project", "tool", "place", "topic", "label"])
        self.assertIn("parent", items["properties"])
        self.assertNotIn("parent", items["required"])
        self.assertIn("label: a short name", seen["system"])
        self.assertIn("A milestone id is a label, not a project", seen["system"])
        self.assertIn("When unsure whether something is an entity",
                      seen["system"])
        self.assertIn("a named effort with a goal and an owner",
                      seen["system"])
        self.assertEqual(ex.effective_prompt_version(), "extract-v4")
        self.assertEqual(ex.effective_schema_version(), "facts-schema-v5")

    def test_kill_switch_restores_the_earlier_prompt_schema_and_stamps(self):
        from app.services import extractor as ex
        with patch.dict(os.environ, {"QUILL_EXTRACT_ENTITY_V2": "0"}):
            seen = self.call_extractor()
            self.assertEqual(ex.effective_prompt_version(), "extract-v3")
            self.assertEqual(ex.effective_schema_version(), "facts-schema-v4")
            with patch.dict(os.environ, {"QUILL_EXTRACT_CONTEXT": "1"}):
                self.assertEqual(ex.effective_prompt_version(),
                                 "extract-v3-ctx")
        self.assertIs(seen["schema"], ex._SCHEMA_V1)
        self.assertTrue(seen["system"].startswith(ex._SYSTEM_V1))
        self.assertEqual(
            seen["schema"]["properties"]["entities"]["items"]["properties"]
            ["kind"]["enum"], ["org", "project", "place", "product", "other"])
        self.assertNotIn("label:", seen["system"])
        self.assertIn("a company/org, a project/product, or a place",
                      seen["system"])

    def test_the_earlier_prompt_is_kept_byte_for_byte(self):
        # The v2 prompt is the v1 prompt with one block swapped, so the kill
        # switch cannot drift: every other line is shared.
        from app.services import extractor as ex
        self.assertIn(ex._ENTITY_RULES_V1, ex._SYSTEM_V1)
        self.assertNotIn(ex._ENTITY_RULES_V1, ex._SYSTEM)
        self.assertEqual(ex._SYSTEM.replace(ex._ENTITY_RULES, ""),
                         ex._SYSTEM_V1.replace(ex._ENTITY_RULES_V1, ""))
        v1 = json.loads(json.dumps(ex._SCHEMA_V1))
        v2 = json.loads(json.dumps(ex._SCHEMA))
        v1["properties"].pop("entities")
        v2["properties"].pop("entities")
        # Fix pass B: the entity-model schema carries invented example names
        # (`_EXAMPLES_V2`); put the earlier ones back and nothing else differs.
        for path, field, old, new in ex._EXAMPLES_V2:
            node = v2["properties"][path[0]]
            if len(path) > 1:
                node = node["items"]["properties"][path[1]]
            self.assertIn(new, node[field])
            node[field] = node[field].replace(new, old)
        self.assertEqual(v1, v2)

    def test_operator_pinned_stamp_wins_under_the_kill_switch(self):
        from app.services import extractor as ex
        with patch.dict(os.environ, {"QUILL_EXTRACT_ENTITY_V2": "0",
                                     "QUILL_EXTRACT_PROMPT_VERSION": "lora-7"}):
            self.assertEqual(ex.effective_prompt_version(), "lora-7")

    def test_eval_gated_additions_build_on_the_selected_schema(self):
        from app.services import extractor as ex
        with patch.dict(os.environ, {"QUILL_EXTRACT_IDEAS": "1"}):
            s = ex._schema()
            self.assertIn("ideas", s["properties"])
            self.assertIn("label", s["properties"]["entities"]["items"]
                          ["properties"]["kind"]["enum"])
            with patch.dict(os.environ, {"QUILL_EXTRACT_ENTITY_V2": "0"}):
                s = ex._schema()
                self.assertIn("ideas", s["properties"])
                self.assertNotIn("label", s["properties"]["entities"]["items"]
                                 ["properties"]["kind"]["enum"])
        self.assertNotIn("ideas", ex._SCHEMA["properties"])

    def test_screen_extractors_follow_the_switch(self):
        from app.perception import l3_workers
        from app.services import extractor as ex
        from app.services import screen_extract
        seen = self.capture(screen_extract._extract, "Postgres on screen")
        self.assertIs(seen["schema"], ex._SCHEMA)
        self.assertIn("label: a short name", seen["system"])
        self.assertTrue(seen["system"].startswith(screen_extract._SYSTEM))
        seen = self.capture(l3_workers._run_llm_extract, "Postgres on screen")
        self.assertIs(seen["schema"], ex._SCHEMA)
        self.assertIn("label: a short name", seen["system"])
        with patch.dict(os.environ, {"QUILL_EXTRACT_ENTITY_V2": "0"}):
            seen = self.capture(screen_extract._extract, "Postgres")
            self.assertIs(seen["schema"], ex._SCHEMA_V1)
            self.assertEqual(seen["system"], screen_extract._SYSTEM)
            seen = self.capture(l3_workers._run_llm_extract, "Postgres")
            self.assertEqual(seen["system"], screen_extract._SYSTEM)


# ---------------------------------------------------------------- routes --
class KindRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_typing_route_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        p = patch.object(routes_mod.memory, "_ensure_store",
                         return_value=self.store)
        p.start()
        self.addCleanup(p.stop)

    def test_kind_route_and_manual_add_take_label(self):
        eid = self.store.resolve_entity("c4", "project", ts=NOW)
        r = self.client.post(f"/entities/{eid}/kind", json={"kind": "label"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["kind"], "label")
        r = self.client.post(f"/entities/{eid}/kind",
                             json={"kind": "milestone"})
        self.assertEqual(r.json()["kind"], "label")
        r = self.client.post("/entities", json={"name": "Phase 2",
                                                "kind": "label"})
        self.assertEqual(r.status_code, 200, r.text)
        row = self.store.get_entity(r.json()["id"])
        self.assertEqual((row["kind"], row["kind_source"]), ("label", "user"))
        listed = {e["name"]: e["kind"] for e in
                  self.client.get("/entities/list").json()["entities"]}
        self.assertEqual(listed, {"c4": "label", "Phase 2": "label"})


if __name__ == "__main__":
    unittest.main()
