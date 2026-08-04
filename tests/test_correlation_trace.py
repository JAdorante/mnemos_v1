"""correlation_id plumbing + trace_chain + /console/trace (plan task 1.5/1.6).

Every event is minted a correlation_id on insert (if it doesn't already carry
one); fact_candidates and agent_runs can be tagged with it; and
Store.trace_chain follows one id back through events -> candidates -> facts
-> agent_runs. The console endpoint exposes the same chain as JSON, or as a
simple HTML page on request.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services.consolidation import Turn
from app.services.extractor import Extractor, turn_hash
from app.storage import Store

NOW = 1_000_000_000.0


def _audio(text: str, t: float = NOW, meta: dict | None = None) -> Event:
    return Event(time=t, modality=Modality.AUDIO, raw=text,
                 source="audio.whisper", confidence=0.9, meta=meta or {})


class InsertMintsCorrelationIdTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_missing_correlation_id_is_minted_and_persisted(self):
        ev = _audio("hello there")
        self.assertNotIn("correlation_id", ev.meta)
        eid = self.store.insert(ev)
        # Mutated in place so the caller sees the minted id too.
        self.assertIn("correlation_id", ev.meta)
        self.assertTrue(ev.meta["correlation_id"])
        stored = self.store.get_event(eid)
        import json
        meta = json.loads(stored["meta"] or "{}")
        self.assertEqual(meta["correlation_id"], ev.meta["correlation_id"])

    def test_existing_correlation_id_is_preserved(self):
        ev = _audio("hello there", meta={"correlation_id": "fixed-id-123"})
        self.store.insert(ev)
        self.assertEqual(ev.meta["correlation_id"], "fixed-id-123")

    def test_two_events_get_distinct_ids(self):
        ev1, ev2 = _audio("first"), _audio("second")
        self.store.insert(ev1)
        self.store.insert(ev2)
        self.assertNotEqual(ev1.meta["correlation_id"], ev2.meta["correlation_id"])


class FactCandidateCorrelationTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_candidate_inherits_source_event_correlation_id(self):
        eid = self.store.insert(_audio("we need to book the venue"))
        cid = self.store.get_event(eid)
        import json
        correlation_id = json.loads(cid["meta"])["correlation_id"]

        turn = Turn(start=NOW, end=NOW + 1, speaker="Hugh",
                    text="we need to book the venue", event_ids=[eid],
                    n_utterances=1)
        facts = {
            "tasks": [{
                "text": "Book the venue", "owner": "me", "due": "",
                "confidence": 0.9, "source_span": "we need to book the venue",
                "assertion": "stated_by_user",
            }],
            "commitments": [], "claims": [], "entities": [], "relations": [],
        }
        self.ex._write_fact_candidates(turn, facts, time.time())
        rows = self.store.list_fact_candidates(turn_hash=turn_hash(turn))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["correlation_id"], correlation_id)


class TraceChainTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ex = Extractor(store=self.store)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _build_chain(self):
        eid = self.store.insert(_audio("we need to book the venue"))
        import json
        correlation_id = json.loads(self.store.get_event(eid)["meta"])["correlation_id"]

        turn = Turn(start=NOW, end=NOW + 1, speaker="Hugh",
                    text="we need to book the venue", event_ids=[eid],
                    n_utterances=1)
        facts = {
            "tasks": [{
                "text": "Book the venue", "owner": "me", "due": "",
                "confidence": 0.9, "source_span": "we need to book the venue",
                "assertion": "stated_by_user",
            }],
            "commitments": [], "claims": [], "entities": [], "relations": [],
        }
        with patch.object(self.ex, "_resolve_person_id", return_value=None), \
             patch.object(self.ex, "_persist_entities", return_value=(0, 0)), \
             patch.object(self.ex, "_record_faithfulness", return_value=None), \
             patch("app.services.extractor._index_fact", lambda *a, **k: None), \
             patch("app.services.task_offer.offer_task", lambda *a, **k: None), \
             patch("app.services.fact_gate._similar_active", return_value=[]):
            self.ex._persist(turn, facts, time.time())

        self.store.start_agent_run(
            "book the venue", agent_type="browser", correlation_id=correlation_id)
        return correlation_id

    def test_chain_links_event_candidate_fact_and_run(self):
        correlation_id = self._build_chain()
        chain = self.store.trace_chain(correlation_id)
        self.assertEqual(chain["correlation_id"], correlation_id)
        self.assertEqual(len(chain["events"]), 1)
        self.assertEqual(chain["events"][0]["raw"], "we need to book the venue")
        self.assertEqual(len(chain["candidates"]), 1)
        self.assertEqual(chain["candidates"][0]["kind"], "task")
        self.assertEqual(chain["candidates"][0]["status"], "accepted")
        self.assertEqual(len(chain["facts"]), 1)
        self.assertEqual(chain["facts"][0]["kind"], "task")
        self.assertEqual(len(chain["agent_runs"]), 1)
        self.assertEqual(chain["agent_runs"][0]["goal"], "book the venue")

    def test_unknown_id_returns_empty_chain(self):
        chain = self.store.trace_chain("does-not-exist")
        self.assertEqual(chain["events"], [])
        self.assertEqual(chain["candidates"], [])
        self.assertEqual(chain["facts"], [])
        self.assertEqual(chain["agent_runs"], [])

    def test_empty_id_returns_empty_chain_without_querying(self):
        chain = self.store.trace_chain("")
        self.assertEqual(chain["events"], [])


class AgentRunCorrelationWhitelistTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def test_start_agent_run_accepts_correlation_id(self):
        rid = self.store.start_agent_run("goal", correlation_id="corr-1")
        run = self.store.agent_run(rid)
        self.assertEqual(run["correlation_id"], "corr-1")

    def test_annotate_agent_run_accepts_correlation_id(self):
        rid = self.store.start_agent_run("goal")
        self.store.annotate_agent_run(rid, correlation_id="corr-2")
        run = self.store.agent_run(rid)
        self.assertEqual(run["correlation_id"], "corr-2")

    def test_recorder_start_run_passes_correlation_id(self):
        from app.services.agent_log import Recorder
        rec = Recorder(store=self.store)
        rid = rec.start_run("goal", correlation_id="corr-3")
        run = self.store.agent_run(rid)
        self.assertEqual(run["correlation_id"], "corr-3")


class ConsoleTraceEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.routes import router
        app = FastAPI()
        app.include_router(router)
        cls.client = TestClient(app)

    def setUp(self):
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.addCleanup(self.store.close)
        import app.api.routes as routes_mod
        patcher = patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_json_by_default(self):
        eid = self.store.insert(_audio("we need to book the venue"))
        import json
        correlation_id = json.loads(self.store.get_event(eid)["meta"])["correlation_id"]
        r = self.client.get(f"/console/trace/{correlation_id}")
        self.assertEqual(r.status_code, 200)
        self.assertIn("application/json", r.headers["content-type"])
        body = r.json()
        self.assertEqual(body["correlation_id"], correlation_id)
        self.assertEqual(len(body["events"]), 1)

    def test_html_with_format_query_param(self):
        eid = self.store.insert(_audio("we need to book the venue"))
        import json
        correlation_id = json.loads(self.store.get_event(eid)["meta"])["correlation_id"]
        r = self.client.get(f"/console/trace/{correlation_id}",
                           params={"format": "html"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn(correlation_id, r.text)

    def test_html_with_accept_header(self):
        eid = self.store.insert(_audio("we need to book the venue"))
        import json
        correlation_id = json.loads(self.store.get_event(eid)["meta"])["correlation_id"]
        r = self.client.get(f"/console/trace/{correlation_id}",
                           headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_unknown_id_returns_empty_chain_not_404(self):
        r = self.client.get("/console/trace/nope")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["events"], [])


if __name__ == "__main__":
    unittest.main()
