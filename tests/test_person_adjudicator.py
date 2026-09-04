"""Mint-time person adjudication — the model verdict reroutes tool/org names
minted as people, keeps humans, and fails safe (keep) on doubt or model
failure. All model calls mocked; storage is a temp DB."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import person_adjudicator as pa
from app.storage import Store

NOW = 1_000_000_000.0

_ROUTER = "app.services.model_router.router.complete_json"


class PersonAdjudicatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.store = Store(db_path=Path(self.tmp) / "t.db",
                           audio_dir=Path(self.tmp) / "audio")

    def _person(self, name: str) -> int:
        return int(self.store.insert_person(name, ts=NOW))

    def _by_id(self, pid: int) -> dict:
        return next(p for p in self.store.all_people() if p["id"] == pid)

    # --- verdicts -----------------------------------------------------------

    def test_tool_verdict_hides_person_and_mints_entity(self):
        pid = self._person("OpenAI Codex")
        with patch(_ROUTER, return_value={"kind": "tool", "confidence": 0.95,
                                          "reason": "coding agent product"}):
            out = pa.run_once(self.store)
        self.assertEqual(out["hidden"], 1)
        p = self._by_id(pid)
        self.assertTrue(p["hide_from_people"])
        self.assertEqual(p["adjudication"]["kind"], "tool")
        ents = {e["name"]: e for e in self.store.all_entities()}
        self.assertIn("OpenAI Codex", ents)
        self.assertEqual(ents["OpenAI Codex"]["kind"], "tool")

    def test_junk_verdict_hides_without_entity(self):
        pid = self._person("Karmic Satco")
        with patch(_ROUTER, return_value={"kind": "junk", "confidence": 0.9}):
            out = pa.run_once(self.store)
        self.assertEqual(out["hidden"], 1)
        self.assertTrue(self._by_id(pid)["hide_from_people"])
        self.assertEqual(self.store.all_entities(), [])

    def test_human_verdict_keeps_row(self):
        pid = self._person("Calin Draia")
        with patch(_ROUTER, return_value={"kind": "human", "confidence": 0.8}):
            out = pa.run_once(self.store)
        self.assertEqual(out["kept"], 1)
        p = self._by_id(pid)
        self.assertFalse(p["hide_from_people"])
        self.assertEqual(p["adjudication"]["kind"], "human")

    def test_percent_scale_confidence_normalized(self):
        # Live local models answered 95.0 / 80.0 (percent). A percent-scale
        # "30" must land under the 0.6 floor, not sail over it.
        pid = self._person("Lean Layer")
        with patch(_ROUTER, return_value={"kind": "tool", "confidence": 30.0}):
            out = pa.run_once(self.store)
        self.assertEqual(out["kept"], 1)
        p = self._by_id(pid)
        self.assertFalse(p["hide_from_people"])
        self.assertEqual(p["adjudication"]["kind"], "unsure")
        self.assertAlmostEqual(p["adjudication"]["confidence"], 0.3)

    def test_low_confidence_nonhuman_downgrades_to_unsure(self):
        pid = self._person("Lean Layer")
        with patch(_ROUTER, return_value={"kind": "org", "confidence": 0.3}):
            out = pa.run_once(self.store)
        self.assertEqual(out["kept"], 1)
        p = self._by_id(pid)
        self.assertFalse(p["hide_from_people"])
        self.assertEqual(p["adjudication"]["kind"], "unsure")

    # --- fail-safety --------------------------------------------------------

    def test_model_failure_leaves_row_unmarked_for_retry(self):
        pid = self._person("PortCo Blogs")
        with patch(_ROUTER, side_effect=RuntimeError("ollama down")):
            out = pa.run_once(self.store)
        self.assertEqual(out["failed"], 1)
        p = self._by_id(pid)
        self.assertFalse(p["hide_from_people"])
        self.assertIsNone(p["adjudication"])
        # Next pass retries it.
        with patch(_ROUTER, return_value={"kind": "tool", "confidence": 0.9}):
            out = pa.run_once(self.store)
        self.assertEqual(out["hidden"], 1)

    def test_garbage_model_output_counts_as_failure(self):
        self._person("Hugh Salva")
        with patch(_ROUTER, return_value={"kind": "banana"}):
            out = pa.run_once(self.store)
        self.assertEqual(out["failed"], 1)

    def test_disabled_flag_no_ops(self):
        self._person("OpenAI Codex")
        with patch.dict("os.environ", {"QUILL_PERSON_ADJUDICATE": "0"}), \
                patch(_ROUTER) as cj:
            out = pa.run_once(self.store)
        self.assertTrue(out.get("disabled"))
        cj.assert_not_called()

    # --- eligibility --------------------------------------------------------

    def test_judged_rows_are_not_rejudged(self):
        self._person("Calin Draia")
        with patch(_ROUTER, return_value={"kind": "human", "confidence": 0.8}) as cj:
            pa.run_once(self.store)
            pa.run_once(self.store)
        self.assertEqual(cj.call_count, 1)

    def test_unsure_rejudged_only_after_new_evidence(self):
        pid = self._person("Lean Layer")
        with patch(_ROUTER, return_value={"kind": "unsure", "confidence": 0.5}) as cj:
            pa.run_once(self.store)
            pa.run_once(self.store)          # no new evidence -> no second call
            self.assertEqual(cj.call_count, 1)
        # New sighting AFTER the verdict — the verdict is stamped with real
        # wall-clock time, so the touch must land later than that.
        import time as _t
        self.store.touch_person(pid, _t.time() + 60)
        with patch(_ROUTER, return_value={"kind": "tool", "confidence": 0.9}):
            out = pa.run_once(self.store)
        self.assertEqual(out["hidden"], 1)

    def test_promoted_and_protected_rows_skipped(self):
        recognized = self._person("Chris Falloon")
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE people SET promotion_state='recognized' WHERE id=?",
                (recognized,))
            self.store._conn.commit()
        contact = self._person("Abby Nengel")
        self.store.upsert_contact_point(
            person_id=contact, type_="email",
            value_display="abby@example.com",
            value_normalized="abby@example.com", confidence=0.9,
            attribution_method="test", verification_status="unverified",
            source_event_id=None, evidence_quote=None, discourse_role=None,
            ts=NOW, created_by="test", pipeline_version="t")
        with patch(_ROUTER) as cj:
            out = pa.run_once(self.store)
        self.assertEqual(out["checked"], 0)
        cj.assert_not_called()

    def test_evidence_reaches_the_prompt(self):
        pid = self._person("OpenAI Codex")
        eid = self.store.resolve_entity("Django", "tool", ts=NOW)
        self.store.add_relation("person", pid, "associated_with",
                                "entity", eid, ts=NOW)
        seen = {}

        def _capture(task, **kw):
            seen["prompt"] = kw["messages"][0]["content"]
            return {"kind": "tool", "confidence": 0.9}

        with patch(_ROUTER, side_effect=_capture):
            pa.run_once(self.store)
        self.assertIn("OpenAI Codex", seen["prompt"])
        self.assertIn("Django", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
