"""Entity model, final-review fix pass B: resolver, rules, prompt, general
use, CI.

One test class per fix (B1-B10 of the fix list): a name an entity row
already carries is that row before its shape is looked at, and two Title
Case words go to People only when the seat knows that person (never a filed
project, never a forgotten name; the same guard for typed chat, the people
pipeline, the person adjudicator and the cleanup); a legal form decides an
unfiled name's kind in the live vote; a domain-named company survives the
repair by the public suffix list; the extraction schema's examples are
invented; one seat's words live in its seat vocabulary; a rule's kind is
stored as the rule's; code-shaped places, durations and terms, and more
legal forms; the CI workflow; the hosted seats' extraction flag.

Every name here is invented. Every test runs against a real Store on a temp
db, through the real extractor persist path where a sighting is involved.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes  # noqa: F401
import app.services.extractor  # noqa: F401
import app.services.people_pipeline  # noqa: F401
from app.services import entity_candidates as cand
from app.services import entity_repair as er
from app.services import entity_typing as et
from app.services import name_quality as nq
from app.services import projects
from app.services.kg_keys import entity_name_key
from app.services.resolution import Resolver
from tests.test_entity_decisions import (CHAT, DAY, HOUR, MAIL, NOW,
                                         _ExtractCase, _StoreCase)

try:
    import yaml
except ImportError:  # pragma: no cover - PyYAML is not a project dependency
    yaml = None

ROOT = Path(__file__).resolve().parents[1]


def _people(store) -> list[str]:
    return sorted(p["name"] for p in store.all_people())


# ------------------------------------------------------------------- B1 --
class PersonEvidenceTests(_ExtractCase):
    """The lookups come first; the shape alone sends nothing to People."""

    def bound(self, eid: int) -> int:
        return len(self.store.entity_mentions(entity_id=int(eid)))

    def test_a_filed_two_word_project_binds_and_mints_no_person(self):
        eid = projects.ensure_filed("Copperhill Refit", store=self.store,
                                    now=NOW, user_accept=True)
        for d in (1, 3):
            self.see("Copperhill Refit", "project", source=MAIL,
                     ts=NOW + d * DAY)
        self.assertEqual(_people(self.store), [])
        self.assertEqual(self.bound(eid), 2)
        self.assertEqual(self.kind(eid), ("project", "filing"))

    def test_a_shared_name_without_a_row_is_no_person(self):
        # Filed by the team (the filed tier), no row yet, and a person once
        # minted under its name: the project's name stays the project's.
        self.file_note("Copperhill Refit")
        projects.invalidate_filed_keys()
        self.store.insert_person("Copperhill Refit", ts=NOW)
        m = Resolver(store=self.store).match_entity("Copperhill Refit",
                                                    "project")
        self.assertEqual((m.status, m.person_shaped), ("new", False))

    def test_a_relation_end_binds_an_existing_org(self):
        org = self.store.resolve_entity("Velmont Sailworks", "org", ts=NOW,
                                        kind_source="model")
        rel = {"subject": "Larkfen Ledger", "subject_kind": "entity",
               "predicate": "related_to", "object": "Velmont Sailworks",
               "object_kind": "entity", "confidence": 0.8,
               "source_span": "Larkfen Ledger imports Velmont Sailworks"}
        self.see("Larkfen Ledger", "tool", source=MAIL, ts=NOW + DAY,
                 relations=[rel])
        self.assertEqual(_people(self.store), [])
        self.assertEqual(self.bound(org), 1)

    def test_a_forgotten_name_of_any_kind_never_goes_to_people(self):
        for name, kind in (("Velmont Sailworks", "org"),
                           ("Odile Survey", "project")):
            eid = self.store.resolve_entity(name, kind, ts=NOW,
                                            kind_source="model")
            self.store.delete_entity(eid, tombstone=True, reason="forget")
        rel = {"subject": "Larkfen Ledger", "subject_kind": "entity",
               "predicate": "related_to", "object": "Velmont Sailworks",
               "object_kind": "entity", "confidence": 0.8,
               "source_span": "Larkfen Ledger imports Velmont Sailworks"}
        self.see("Larkfen Ledger", "tool", source=MAIL, ts=NOW + DAY,
                 relations=[rel])
        for d, name in ((2, "Velmont Sailworks"), (3, "Odile Survey")):
            self.see(name, "project", source=MAIL, ts=NOW + d * DAY)
        self.assertEqual(_people(self.store), [])
        self.assertIsNone(self.entity("Velmont Sailworks"))
        self.assertIsNone(self.entity("Odile Survey"))

    def test_two_title_case_words_alone_wait_as_a_candidate(self):
        self.see("Larkfen Rollout", "project", source=MAIL, ts=NOW)
        self.assertEqual(_people(self.store), [])
        self.assertEqual(len(self.pending("Larkfen Rollout")), 1)
        self.see("Larkfen Rollout", "project", source=MAIL, ts=NOW + DAY)
        row = self.entity("Larkfen Rollout")
        self.assertEqual((row["kind"], row["kind_source"]), ("idea", "model"))
        self.assertEqual(_people(self.store), [])

    def test_a_known_person_named_as_a_project_is_that_person(self):
        pid = self.store.insert_person("Odile Varnet", ts=NOW)
        self.see("Odile Varnet", "project", source=MAIL, ts=NOW + DAY)
        self.assertIsNone(self.entity("Odile Varnet"))
        self.assertEqual(self.pending("Odile Varnet"), [])
        people = self.store.all_people()
        self.assertEqual([p["id"] for p in people], [pid])
        self.assertEqual(float(people[0]["last_seen"]), NOW + DAY)

    def test_a_person_judged_not_human_is_no_evidence(self):
        pid = self.store.insert_person("Bramble Fennick", ts=NOW)
        self.store.set_person_adjudication(
            pid, {"kind": "org", "confidence": 0.9, "ts": NOW})
        self.see("Bramble Fennick", "project", source=MAIL, ts=NOW + DAY)
        self.assertEqual(len(self.pending("Bramble Fennick")), 1)

    def test_the_resolver_never_creates_a_person_from_an_entity_name(self):
        r = Resolver(store=self.store)
        eid = r.resolve_entity("Larkfen Rollout", "project", ts=NOW)
        self.assertGreater(eid, 0)
        self.assertEqual(_people(self.store), [])
        self.store.insert_person("Odile Varnet", ts=NOW)
        self.assertEqual(r.resolve_entity("Odile Varnet", "project", ts=NOW),
                         0)
        self.assertEqual(_people(self.store), ["Odile Varnet"])
        self.assertIsNone(self.entity("Odile Varnet"))

    def test_an_exact_hidden_row_wins_over_the_shape(self):
        eid = self.store.resolve_entity("Larkfen Rollout", "project", ts=NOW,
                                        kind_source="model")
        self.store.set_entity_hidden(eid, hidden=True, source="user")
        self.store.insert_person("Larkfen Rollout", ts=NOW)
        m = Resolver(store=self.store).match_entity("Larkfen Rollout",
                                                    "project")
        self.assertEqual(m.status, "rejected")    # the known-person guard
        self.store.set_entity_hidden(eid, hidden=False, source="user")
        m = Resolver(store=self.store).match_entity("Larkfen Rollout",
                                                    "project")
        self.assertEqual((m.status, m.entity_id), ("existing", eid))


class AdjudicatorAndCleanupTests(_StoreCase):
    """The person adjudicator and the hygiene cleanup use the same guard."""

    def test_a_candidate_twin_of_an_org_is_hidden_without_the_model(self):
        from app.services import person_adjudicator as pa
        self.store.resolve_entity("Velmont Sailworks", "org", ts=NOW,
                                  kind_source="model")
        twin = self.store.insert_person("Velmont Sailworks", ts=NOW)
        other = self.store.insert_person("Odile Varnet", ts=NOW)
        calls = []

        def fake(*a, **k):
            calls.append(k.get("messages"))
            return {"kind": "human", "confidence": 0.9}
        with patch.dict(os.environ, {"QUILL_PERSON_ADJUDICATE": "1"}), \
                patch("app.services.model_router.router.complete_json",
                      side_effect=fake):
            out = pa.run_once(self.store)
        rows = {p["id"]: p for p in self.store.all_people()}
        self.assertTrue(rows[twin].get("hide_from_people"))
        self.assertEqual(rows[twin]["adjudication"]["kind"], "entity")
        self.assertFalse(rows[other].get("hide_from_people"))
        self.assertEqual(len(calls), 1)       # only the real candidate
        self.assertEqual((out["hidden"], out["kept"]), (1, 1))

    def test_cleanup_never_sends_a_filed_project_to_people(self):
        from app.services import ambient_cleanup as ac
        filed = projects.ensure_filed("Copperhill Refit", store=self.store,
                                      now=NOW, user_accept=True)
        legacy = self.store.resolve_entity("Odile Varnet", "project", ts=NOW)
        plan = {e["id"]: e for e in ac.plan_entities(self.store)}
        self.assertNotIn(filed, plan)
        self.assertEqual(plan[legacy]["action"], "hide_person")


# ------------------------------------------------------------------- B2 --
class TypedChatGuardTests(_ExtractCase):
    def resolve(self, name, role="relation"):
        from app.events import Event, Modality
        from app.services.people_pipeline import resolve_person_mention
        ev = int(self.store.insert(Event(
            time=NOW, modality=Modality.TEXT, raw=f"ask {name} about it",
            source=CHAT, meta={})))
        return resolve_person_mention(
            name, store=self.store, event_id=ev, event_source=CHAT,
            text=f"ask {name} about it", grammatical_role=role, now=NOW,
            relationship_boost=0.75)

    def test_a_name_the_user_kinded_is_no_new_person(self):
        from app.services.extractor import _chat_people_names
        eid = self.store.resolve_entity("Quarrow Marina", "org", ts=NOW,
                                        kind_source="model")
        self.store.set_entity_kind(eid, "place", source="user")
        text = "please ask Quarrow Marina if they open on Sunday"
        self.assertEqual(_chat_people_names(text, store=self.store), [])
        self.assertEqual(self.resolve("Quarrow Marina").decision, "reject")
        self.assertNotIn("Quarrow Marina", _people(self.store))

    def test_a_filed_name_is_no_new_person(self):
        from app.services.extractor import _chat_people_names
        self.file_note("Copperhill Refit")
        projects.invalidate_filed_keys()
        text = "did Copperhill Refit get the crane booked"
        self.assertEqual(_chat_people_names(text, store=self.store), [])
        self.assertEqual(self.resolve("Copperhill Refit").decision, "reject")

    def test_a_model_kinded_row_blocks_nothing_but_a_claim_subject(self):
        self.store.resolve_entity("Odile Varnet", "idea", ts=NOW,
                                  kind_source="model")
        # A model's topic named like a person: the person may still exist.
        self.assertNotEqual(self.resolve("Odile Varnet").decision, "reject")
        self.store.resolve_entity("Larkfen Rollout", "idea", ts=NOW,
                                  kind_source="model")
        # A value claim about a thing the seat has is about that thing.
        self.assertEqual(
            self.resolve("Larkfen Rollout", role="claim_subject").decision,
            "reject")


# ------------------------------------------------------------------- B3 --
class LegalFormPriorTests(_ExtractCase):
    def test_an_unfiled_company_is_an_org_whatever_the_model_said(self):
        for i, (name, kind) in enumerate((("Brannock Hale LLC", "project"),
                                          ("Kestle Systems GmbH", "tool"))):
            for d in (0, 2):
                self.see(name, kind, source=MAIL, ts=NOW + (10 * i + d) * DAY)
            row = self.entity(name)
            self.assertEqual((row["kind"], row["kind_source"]),
                             ("org", "rule"), name)

    def test_a_legacy_row_is_left_to_the_repair(self):
        eid = self.store.resolve_entity("Ottoline Mills Ltd", "project",
                                        ts=NOW)
        self.sight("Ottoline Mills Ltd", "project", event_id=1, entity_id=eid)
        v = et.retype(self.store, eid)
        self.assertEqual(v.breakdown["reason"], "legacy")
        self.assertEqual(self.kind(eid), ("project", None))


# ------------------------------------------------------------------- B4 --
class DomainNamedCompanyTests(_StoreCase):
    def test_the_public_suffix_list_decides(self):
        for name in ("Bramble.co.uk", "Pemberly.com.br", "Nimbus.co.jp",
                     "Fennick.io"):
            self.assertTrue(er._domain_named(name), name)
        for name in ("notes.md", "build.sh", "app.config", "www.fennick.io",
                     "a.b.fennick.co.uk", "Fennick", "Fennick Inc"):
            self.assertFalse(er._domain_named(name), name)

    def test_the_repair_keeps_a_company_named_by_its_country_domain(self):
        for name in ("Bramble.co.uk", "Pemberly.com.br"):
            self.store.resolve_entity(name, "org", ts=NOW,
                                      kind_source="model")
        acts = {a["name"]: a for a in er.plan(self.store)["actions"]}
        for name in ("Bramble.co.uk", "Pemberly.com.br"):
            self.assertNotEqual(acts[name]["action"], "drop", name)


# ------------------------------------------------------------------- B5 --
class PromptExampleTests(unittest.TestCase):
    OLD = ("Andy at 8:30", "call with Sam'", "Send Sarah the deck",
           "('Sparrow', 'pricing')", "David said")

    def test_the_entity_model_schema_carries_invented_examples(self):
        from app.services import extractor as ex
        v2 = json.dumps(ex._SCHEMA, ensure_ascii=False)
        v1 = json.dumps(ex._SCHEMA_V1, ensure_ascii=False)
        for s in self.OLD:
            self.assertNotIn(s, v2, s)
            self.assertIn(s, v1, s)          # the kill switch keeps them
        for s in ("Odalys at 8:30", "Send Odalys the deck", "'Nimbus'",
                  "Teodor said"):
            self.assertIn(s, v2, s)
        # Only those strings differ, and only in the copy.
        self.assertEqual(ex._SCHEMA["properties"]["tasks"],
                         ex._SCHEMA_V1["properties"]["tasks"])
        with patch.dict(os.environ, {"QUILL_EXTRACT_ENTITY_V2": "0"}):
            self.assertIs(ex.base_schema(), ex._SCHEMA_V1)


# ------------------------------------------------------------------- B6 --
class SeatWordTests(_StoreCase):
    MOVED = ("solitaire", "industrywide", "election", "integrity")

    def test_one_seats_words_are_not_in_code(self):
        for w in self.MOVED:
            self.assertNotIn(w, nq._NEVER_PERSON_TOKENS, w)
            self.assertNotIn(w, nq._NONPERSON_TOKENS, w)
        # Field and product words stay (seats in any industry meet them).
        for w in ("robotics", "computing", "quantum", "pulse", "venture"):
            self.assertIn(w, nq._NEVER_PERSON_TOKENS | nq._NONPERSON_TOKENS,
                          w)

    def test_a_seeded_seat_still_reads_them(self):
        name = "Election Integrity"
        self.assertTrue(nq.is_plausible_person(name, store=self.store))
        for w in ("election", "integrity"):
            self.store.add_seat_vocab(w, "never_person_token",
                                      source="migration")
        self.assertFalse(nq.is_plausible_person(name, store=self.store))

    def test_a_workstream_abbreviation_is_no_milestone_word(self):
        self.assertFalse(et._MILESTONE.match("WS 4"))
        self.assertTrue(et.code_like("Workstream 4"))


# ------------------------------------------------------------------- B7 --
class RuleSourceTests(_ExtractCase):
    def test_a_rule_decided_promotion_is_stored_as_the_rules(self):
        for d in (0, 2):
            self.see("ZX9", "project", source=MAIL, ts=NOW + d * DAY)
        row = self.entity("ZX9")
        self.assertEqual((row["kind"], row["kind_source"]), ("label", "rule"))
        self.add_email("ana@fennick-tools.example", person="Ana Varnet")
        for d in (5, 7):
            self.see("Fennick Tools", "tool", source=MAIL, ts=NOW + d * DAY)
        row = self.entity("Fennick Tools")
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "rule"))
        # The plain vote still stores the model's rank.
        for d in (9, 11):
            self.see("Ottoline Atelier", "org", source=MAIL, ts=NOW + d * DAY)
        row = self.entity("Ottoline Atelier")
        self.assertEqual((row["kind"], row["kind_source"]), ("org", "model"))

    def test_a_rule_row_is_revoted_by_rules_never_by_the_model(self):
        eid = self.store.resolve_entity("ZX9", "label", ts=NOW,
                                        kind_source="rule")
        # The code-shape rule lets two agreeing sessions keep a tool.
        self.sight("ZX9", "tool", event_id=1, ts=NOW, entity_id=eid)
        self.sight("ZX9", "tool", event_id=2, ts=NOW + 3 * HOUR,
                   entity_id=eid)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid), ("tool", "rule"))
        # A rule org no sighting vote moves.
        org = self.store.resolve_entity("Brannock Hale LLC", "org", ts=NOW,
                                        kind_source="rule")
        for i in range(6):
            self.sight("Brannock Hale LLC", "tool", event_id=10 + i,
                       ts=NOW + i * DAY, conf=0.99, entity_id=org)
        et.retype(self.store, org)
        self.assertEqual(self.kind(org), ("org", "rule"))
        held = self.store.resolve_entity("Nimbus Board", "org", ts=NOW,
                                         kind_source="rule")
        for i in range(6):
            self.sight("Nimbus Board", "place", event_id=30 + i,
                       ts=NOW + i * DAY, conf=0.99, entity_id=held)
        self.assertEqual(et.retype(self.store, held).breakdown["reason"],
                         "held")
        self.assertEqual(self.kind(held), ("org", "rule"))
        # Not frozen: a filing and the user still outrank it.
        projects.ensure_filed("Nimbus Board", store=self.store, now=NOW)
        self.assertEqual(self.kind(held), ("project", "filing"))
        self.store.set_entity_kind(eid, "place", source="user")
        self.assertEqual(self.kind(eid), ("place", "user"))


# ------------------------------------------------------------------- B8 --
class ShapeRuleTests(_ExtractCase):
    def test_durations_terms_and_periods_are_values(self):
        for name in ("13 weeks", "90 days", "6-month", "net 60", "Net 30",
                     "6 percent increase", "5% cut", "Q3", "Q3 2026",
                     "Q4 FY26", "FY2026", "FY 26"):
            self.assertTrue(et.value_literal(name), name)
        for name in ("Network 60", "Quarterly", "Qatar", "FY", "Q5",
                     "Weekly Sync", "net"):
            self.assertFalse(et.value_literal(name), name)

    def test_more_legal_forms_and_prefix_forms(self):
        for name in ("Sberbank PJSC", "Krka d.d.", "Bimbo S.A.B. de C.V.",
                     "PJSC Sberbank", "OOO Romashka", "JSC Gazprom",
                     "PT Astra Graphia", "AO Tinkoff Bank"):
            self.assertEqual(et.legal_form(name), "form", name)
        for name in ("PT Astra", "AO Smith", "Volvo AB", "Gaming PC",
                     "Windows SE", "Maria AS"):
            self.assertEqual(et.legal_form(name), "short", name)
        for name in ("OOO", "pt astra", "Plan B AB testing"):
            self.assertIsNone(et.legal_form(name), name)

    def test_a_short_form_after_one_word_needs_the_evidence(self):
        for i, (name, kind) in enumerate((("Gaming PC", "tool"),
                                          ("Windows SE", "tool"),
                                          ("Maria AS", "project"))):
            for d in (0, 2):
                self.see(name, kind, source=MAIL, ts=NOW + (10 * i + d) * DAY)
            self.assertNotEqual(self.entity(name)["kind"], "org", name)
        for d in (40, 42):
            self.see("Volvo AB", "org", source=MAIL, ts=NOW + d * DAY)
        self.assertEqual(self.entity("Volvo AB")["kind"], "org")
        # The evidence: a sighting that read it as an org, or the word
        # before the form is a known person's email domain.
        self.assertIsNone(et.org_by_evidence(self.store, ["Fennick AB"]))
        self.add_email("bo@fennick.example", person="Bo Varnet")
        self.assertEqual(et.org_by_evidence(self.store, ["Fennick AB"]),
                         "company_name")

    def test_a_code_shaped_place_keeps_its_kind_when_two_sightings_agree(self):
        self.see("B7", "place", source=MAIL, ts=NOW, conf=0.9)
        self.see("B7", "place", source=MAIL, ts=NOW + DAY, conf=0.9)
        self.assertEqual(self.entity("B7")["kind"], "place")
        eid = self.store.resolve_entity("K4", "place", ts=NOW,
                                        kind_source="model")
        self.sight("K4", "place", event_id=50, entity_id=eid)
        et.retype(self.store, eid)
        self.assertEqual(self.kind(eid)[0], "label")


# ------------------------------------------------------------------- B9 --
@unittest.skipIf(yaml is None, "PyYAML not installed")
class CiWorkflowTests(unittest.TestCase):
    PATH = ROOT / ".github" / "workflows" / "entity-golden.yml"

    def test_the_golden_gate_runs_on_main_and_on_pull_requests(self):
        doc = yaml.safe_load(self.PATH.read_text(encoding="utf-8"))
        on = doc.get("on") or doc.get(True)       # YAML 1.1 reads `on` as True
        self.assertEqual(on["push"]["branches"], ["main"])
        for trigger in ("push", "pull_request"):
            paths = on[trigger]["paths"]
            for p in ("app/services/entity_*.py", "app/services/resolution.py",
                      "app/services/name_quality.py",
                      "tests/fixtures/goldens/entity_typing.jsonl",
                      "scripts/eval_entity_typing.py"):
                self.assertIn(p, paths, (trigger, p))
        job = doc["jobs"]["entity"]
        steps = job["steps"]
        py = next(s for s in steps if "setup-python" in str(s.get("uses")))
        self.assertEqual(py["with"]["python-version"], "3.12")
        run = next(s["run"] for s in steps if "unittest" in str(s.get("run")))
        mods = [w for w in run.split() if w.startswith("tests.")]
        self.assertIn("tests.test_entity_typing_golden", mods)
        self.assertIn("tests.test_entity_fixpass_b", mods)
        for m in mods:
            self.assertTrue((ROOT / (m.replace(".", "/") + ".py")).is_file(),
                            m)


# ------------------------------------------------------------------ B10 --
@unittest.skipIf(yaml is None, "PyYAML not installed")
class HostedExtractionFlagTests(unittest.TestCase):
    COMPOSE = ROOT / "deploy" / "hosted" / "gb10" / "docker-compose.yml"

    @unittest.skipUnless(COMPOSE.is_file(), "GB10 compose file not present")
    def test_every_seat_keeps_the_trained_extraction_prompt(self):
        doc = yaml.safe_load(self.COMPOSE.read_text(encoding="utf-8"))
        seats = {k: v for k, v in (doc.get("services") or {}).items()
                 if k.startswith("sparrow-user")}
        self.assertTrue(seats)
        for seat, svc in seats.items():
            env = dict(svc.get("environment") or {})
            self.assertEqual(str(env.get("QUILL_EXTRACT_ENTITY_V2")), "0",
                             seat)


if __name__ == "__main__":
    unittest.main()
