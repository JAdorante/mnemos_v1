"""Tests for the one-time onboarding profile (app/services/onboarding.py).

The contract under test:
  * a filled sheet seeds people (+aliases), entities, ASSERTED graph edges, and
    pre-approved ACCEPTED claim facts with per-answer SYSTEM provenance events;
  * ingestion is idempotent and delta-aware (re-runs add nothing; an edited
    sheet adds only the new answers);
  * the flow is asked ONCE: first boot writes a template and records that,
    later boots stay quiet, and a completed ingest silences it forever.

Everything runs against a temp Store + temp profile/state paths (env-driven),
so the live DB and sheet are never touched.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from app.events import Modality
from app.services import onboarding
from app.storage import Store


def _profile() -> dict:
    return {
        "identity": {"name": "Jae", "role": "product engineer",
                     "description": "builds a personal memory assistant"},
        "people": [
            {"name": "Justin Marsh", "aliases": ["Justin"],
             "relationship": "works with", "note": "owns the data pipeline"},
        ],
        "projects": [
            {"name": "Venture Pulse", "kind": "project", "aliases": ["VP"],
             "note": "the flagship dashboard"},
        ],
        "tools": ["Cursor", {"name": "FL Studio"}],
        "schedule": ["standup at 10am", "deep work in the mornings"],
        "priorities": ["ship onboarding this week"],
        "notes": "First paragraph of context.\n\nSecond paragraph.",
    }


class _TempPathsMixin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_onboard_"))
        self._env = {k: os.environ.get(k) for k in
                     ("QUILL_ONBOARDING_PROFILE", "QUILL_ONBOARDING_STATE")}
        os.environ["QUILL_ONBOARDING_PROFILE"] = str(self.tmp / "profile.json")
        os.environ["QUILL_ONBOARDING_STATE"] = str(self.tmp / "state.json")
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "audio")

    def tearDown(self) -> None:
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class TemplateTests(_TempPathsMixin):
    def test_template_created_once_never_clobbered(self) -> None:
        out = onboarding.write_template()
        self.assertTrue(out["created"])
        p = Path(out["path"])
        self.assertTrue(p.is_file())
        p.write_text(json.dumps({"identity": {"name": "Jae"}}), encoding="utf-8")
        again = onboarding.write_template()
        self.assertFalse(again["created"])   # a half-filled sheet survives
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["identity"]["name"],
                         "Jae")

    def test_template_is_valid_json_with_all_sections(self) -> None:
        onboarding.write_template()
        t = json.loads(Path(os.environ["QUILL_ONBOARDING_PROFILE"]).read_text(
            encoding="utf-8"))
        for section in ("identity", "people", "projects", "tools", "schedule",
                        "priorities", "notes"):
            self.assertIn(section, t)


class IngestTests(_TempPathsMixin):
    def test_full_profile_seeds_all_rails(self) -> None:
        res = onboarding.ingest(_profile(), store=self.store)
        self.assertTrue(res["ok"])
        self.assertTrue(res["completed"])
        # People: the user + the named teammate, alias recorded.
        people = {p["name"]: p for p in self.store.list_people_embed()}
        self.assertIn("Jae", people)
        self.assertIn("Justin Marsh", people)
        self.assertIn("Justin", people["Justin Marsh"]["aliases"])
        # Entities: project with alias + both tools.
        ents = {e["name"]: e for e in self.store.recent_entities(20)}
        self.assertEqual(ents["Venture Pulse"]["kind"], "project")
        self.assertEqual(ents["Cursor"]["kind"], "tool")
        self.assertEqual(ents["FL Studio"]["kind"], "tool")
        # Asserted edges: works_with, involved_in, uses — all survive rebuilds.
        rels = self.store.relations_of("person", people["Jae"]["id"])["out"]
        preds = {r["predicate"] for r in rels}
        self.assertLessEqual({"works_with", "involved_in", "uses"}, preds)
        self.assertTrue(all(r["origin"] == "asserted" for r in rels))
        # Claims: pre-approved, provenance-linked, phrased from the answers.
        claims = self.store.list_facts(kind="claim", limit=100)
        texts = " || ".join(c["text"] for c in claims)
        for expected in ("Jae", "product engineer", "standup at 10am",
                         "ship onboarding this week", "owns the data pipeline",
                         "the flagship dashboard", "Second paragraph."):
            self.assertIn(expected, texts)
        self.assertTrue(all(c["review"] == "approved" for c in claims))
        self.assertTrue(all(c["source_event_id"] for c in claims))
        # Provenance events: SYSTEM, onboarding source, ACCEPTED epistemic tier.
        evs = [ev for _, ev in self.store.all_with_ids()
               if ev.source == onboarding.SOURCE]
        self.assertGreaterEqual(len(evs), len(claims))
        self.assertTrue(all(ev.modality == Modality.SYSTEM for ev in evs))
        self.assertTrue(all(ev.epistemic == "accepted" for ev in evs))

    def test_reingest_is_idempotent(self) -> None:
        onboarding.ingest(_profile(), store=self.store)
        before = self.store.fact_count()
        res = onboarding.ingest(_profile(), store=self.store)
        self.assertEqual(self.store.fact_count(), before)
        self.assertEqual(res["claims"] + res["people"] + res["entities"]
                         + res["relations"], 0)
        self.assertGreater(res["skipped"], 0)

    def test_edited_sheet_adds_only_the_delta(self) -> None:
        onboarding.ingest(_profile(), store=self.store)
        before = self.store.fact_count()
        p2 = _profile()
        p2["people"].append({"name": "Abby Nengel", "aliases": ["Abby"],
                             "relationship": "client", "note": ""})
        res = onboarding.ingest(p2, store=self.store)
        self.assertEqual(res["people"], 1)          # just the new person
        self.assertEqual(self.store.fact_count(), before)  # no duplicate claims
        self.assertIsNotNone(self.store.find_person_exact("Abby Nengel"))

    def test_junk_and_empty_fields_are_tolerated(self) -> None:
        res = onboarding.ingest(
            {"identity": {"name": ""}, "people": [{"note": "no name"}, "junk"],
             "projects": [], "tools": ["", None], "schedule": [""],
             "priorities": None, "notes": 42},
            store=self.store)
        self.assertTrue(res["ok"])
        self.assertEqual(self.store.fact_count(), 0)
        self.assertFalse(res["completed"])   # nothing real given -> still pending

    def test_missing_sheet_reports_cleanly(self) -> None:
        res = onboarding.ingest(store=self.store)
        self.assertFalse(res["ok"])
        self.assertIn("no profile sheet", res["error"])


class OnceOnlyTests(_TempPathsMixin):
    def test_first_boot_writes_template_and_records_it_once(self) -> None:
        onboarding.startup_check(store=self.store)
        self.assertTrue(Path(os.environ["QUILL_ONBOARDING_PROFILE"]).is_file())
        state1 = json.loads(Path(os.environ["QUILL_ONBOARDING_STATE"]).read_text(
            encoding="utf-8"))
        self.assertIn("template_created_at", state1)
        self.assertNotIn("completed_at", state1)
        # Second boot with the sheet still blank: no re-ask, state unchanged.
        onboarding.startup_check(store=self.store)
        state2 = json.loads(Path(os.environ["QUILL_ONBOARDING_STATE"]).read_text(
            encoding="utf-8"))
        self.assertEqual(state1["template_created_at"],
                         state2["template_created_at"])

    def test_filled_sheet_ingests_on_next_boot_then_never_again(self) -> None:
        onboarding.startup_check(store=self.store)              # boot 1: template
        Path(os.environ["QUILL_ONBOARDING_PROFILE"]).write_text(
            json.dumps(_profile()), encoding="utf-8")
        onboarding.startup_check(store=self.store)              # boot 2: ingest
        self.assertTrue(onboarding.status()["completed"])
        n = self.store.fact_count()
        onboarding.startup_check(store=self.store)              # boot 3: silent
        self.assertEqual(self.store.fact_count(), n)

    def test_status_shape(self) -> None:
        s = onboarding.status()
        self.assertFalse(s["completed"])
        self.assertFalse(s["profile_exists"])
        onboarding.ingest(_profile(), store=self.store)
        s = onboarding.status()
        self.assertTrue(s["completed"])
        self.assertGreater(s["items_ingested"], 0)


if __name__ == "__main__":
    unittest.main()
