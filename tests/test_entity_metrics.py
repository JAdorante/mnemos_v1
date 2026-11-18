"""Entity model, Phase 5: the seat's own entity metrics.

`entity_metrics.compute(store, days=7)`: the forget rate, the
kind-correction rate, the share of visible rows never seen twice, and
candidates pending / promoted / expired / dismissed, each read from this
store only. `GET /entities/metrics?days=7` answers for the seat it runs on
(declared before the int-typed entity route); `scripts/entity_metrics.py`
prints one row per store, never pooled. The candidate log keeps counts, no
names.

Every test runs against a real Store on a temp db.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Imported here, before any test patches `app.storage.get_store`.
import app.api.routes as routes_mod
from app import storage as storage_mod
from app.events import Event, Modality
from app.services import entity_candidates as cand
from app.services import entity_metrics as em
from app.storage import Store

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400.0


def _mk(tmp: str, **kw) -> Store:
    return Store(db_path=Path(tmp) / "quill.db",
                 audio_dir=Path(tmp) / "audio", **kw)


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="quill_metrics_")
        self.store = _mk(self.tmp)
        self.addCleanup(self.store.close)
        env = patch.dict(os.environ, {"QUILL_DATA_DIR": self.tmp,
                                      "QUILL_ENTITY_CANDIDATES": "1"})
        env.start()
        self.addCleanup(env.stop)
        for var in ("QUILL_ENTITY_CANDIDATE_TTL_DAYS",
                    "QUILL_ENTITY_CANDIDATE_SUMMARY_TTL_DAYS"):
            os.environ.pop(var, None)
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(routes_mod.router)
        self.client = TestClient(app)
        for p in (patch.object(routes_mod.memory, "_ensure_store",
                               return_value=self.store),
                  patch.object(storage_mod, "_store", None)):
            p.start()
            self.addCleanup(p.stop)
        self.now = time.time()

    def ent(self, name, kind="org", source="model") -> int:
        return self.store.resolve_entity(name, kind, ts=self.now,
                                         kind_source=source)

    def event(self, ts, source="google.mail") -> int:
        return int(self.store.insert(Event(
            time=ts, modality=Modality.TEXT, raw="r", source=source,
            meta={})))

    def sight(self, name, ts, *, entity_id=None, kind="org"):
        return self.store.record_entity_mention(
            name, proposed_kind=kind, confidence=0.8,
            source_class="connector_mail", event_source="google.mail",
            event_id=self.event(ts), observed_at=ts, entity_id=entity_id)

    def m(self, **kw) -> dict:
        return em.compute(self.store, **kw)

    def promote(self, name, ts) -> int:
        """A candidate the gate itself makes a row (a third sighting in a
        second session), as extraction does."""
        ev = self.event(ts)
        ctx = cand.context(self.store, anchor=ev, now=ts,
                           event_source="google.mail",
                           source_class="connector_mail", mint_ok=True)
        return cand.consider_new(
            self.store, name, ctx, kind="org", confidence=0.8,
            mint=lambda n, k, c, src: self.store.resolve_entity(
                n, k, ts=ts, kind_source=src, kind_confidence=c))


class ComputeTests(_Case):
    def test_an_empty_seat_reads_zeros(self):
        m = self.m()
        self.assertEqual(m["entities"], {"visible": 0, "hidden": 0,
                                         "created": 0})
        self.assertEqual((m["forget"]["forgotten"], m["forget"]["rate"]),
                         (0, None))
        self.assertIsNone(m["kind_correction"]["rate"])
        self.assertIsNone(m["never_seen_twice"]["share"])
        self.assertEqual(m["candidates"], {"pending": 0, "promoted": 0,
                                           "expired": 0,
                                           "expired_sightings": 0,
                                           "dismissed": 0})
        self.assertEqual(m["window"]["days"], 7.0)

    def test_the_forget_rate_is_forgets_over_the_rows_shown(self):
        ids = [self.ent(n) for n in ("Vesper Logistics", "Quay Ledger",
                                     "Harbor Line", "Osprey Yard")]
        self.client.post(f"/entities/{ids[0]}/forget")
        m = self.m()
        self.assertEqual(m["forget"]["forgotten"], 1)
        self.assertEqual(m["forget"]["rate"], 0.25)     # 1 / (3 visible + 1)
        self.assertEqual(m["entities"]["visible"], 3)
        self.assertEqual(m["entities"]["created"], 3)   # rows still here
        # A plain delete (cleanup) or a repair drop is not the user's forget.
        self.store.delete_entity(ids[1], tombstone=True, reason="repair:x",
                                 actor="repair")
        m = self.m()
        self.assertEqual((m["forget"]["forgotten"], m["forget"]["repair_drops"]),
                         (1, 1))

    def test_the_kind_correction_rate_counts_rows_the_user_rekinded(self):
        a, b, c = (self.ent(n, "project") for n in
                   ("Wave 4", "Harbor Line", "Quay Ledger"))
        self.client.post(f"/entities/{a}/kind", json={"kind": "label"})
        self.client.post(f"/entities/{a}/kind", json={"kind": "idea"})
        self.client.post(f"/entities/{b}/kind", json={"kind": "project"})
        self.client.post("/graph/constellation/reclassify",
                         json={"id": f"entity:{c}", "kind": "org"})
        m = self.m()
        # a twice and c once changed; b only confirmed its kind.
        self.assertEqual(m["kind_correction"]["corrected"], 2)
        self.assertEqual(m["kind_correction"]["rate"], round(2 / 3, 4))

    def test_rows_never_seen_twice(self):
        once = self.ent("Quay Ledger")
        twice = self.ent("Harbor Line")
        self.sight("Quay Ledger", self.now - DAY, entity_id=once)
        self.sight("Harbor Line", self.now - 2 * DAY, entity_id=twice)
        self.sight("Harbor Line", self.now - DAY, entity_id=twice)
        never = self.ent("Osprey Yard")          # no evidence at all
        hidden = self.ent("Old Pier")
        self.store.set_entity_hidden(hidden, hidden=True, source="user")
        m = self.m()
        self.assertEqual(m["never_seen_twice"], {"nodes": 2, "of": 3,
                                                 "share": round(2 / 3, 4)})
        self.assertTrue(never)

    def test_candidates_pending_promoted_expired_dismissed(self):
        # Pending: two names waiting.
        self.sight("Quillmoor Analytics", self.now - DAY)
        self.sight("Brackwater Mills", self.now - DAY)
        self.assertEqual(self.m()["candidates"]["pending"], 2)
        # Promoted: the gate makes a row of a candidate. A manual add mints
        # without the gate and is no promotion (fix pass A12).
        self.assertTrue(self.promote("Brackwater Mills", self.now))
        self.sight("Harrowgate Tiles", self.now - DAY)
        cand.add_manual(self.store, "Harrowgate Tiles", "org")
        # Dismissed.
        self.client.post("/entities/candidates/dismiss",
                         json={"name": "Quillmoor Analytics"})
        # Expired: a sighting written 40 days ago is swept.
        old = self.store.record_entity_mention(
            "Larkmoor Freight", proposed_kind="org", confidence=0.8,
            source_class="connector_mail", event_source="google.mail",
            event_id=self.event(self.now - 40 * DAY),
            observed_at=self.now - 40 * DAY)
        with self.store._lock:
            self.store._conn.execute(
                "UPDATE entity_mentions SET created_at = ? WHERE id = ?",
                (self.now - 40 * DAY, old))
            self.store._conn.commit()
        cand.sweep_expired(self.store, now=self.now)
        m = self.m()["candidates"]
        self.assertEqual((m["pending"], m["promoted"], m["expired"],
                          m["expired_sightings"], m["dismissed"]),
                         (0, 1, 1, 1, 1))

    def test_the_window(self):
        e = self.ent("Vesper Logistics")
        self.client.post(f"/entities/{e}/forget")
        self.assertEqual(self.m(days=7)["forget"]["forgotten"], 1)
        later = self.now + 8 * DAY
        self.assertEqual(self.m(days=7, now=later)["forget"]["forgotten"], 0)
        self.assertEqual(self.m(days=0, now=later)["forget"]["forgotten"], 1)
        self.assertIsNone(self.m(days=0)["window"]["since"])

    def test_the_candidate_log_keeps_no_name(self):
        self.sight("Brackwater Mills", self.now - DAY)
        self.assertTrue(self.promote("Brackwater Mills", self.now))
        # A manual add is not the gate's: no promotion is logged for it.
        self.sight("Harrowgate Tiles", self.now - DAY)
        cand.add_manual(self.store, "Harrowgate Tiles", "org")
        with self.store._lock:
            cols = [r[1] for r in self.store._conn.execute(
                "PRAGMA table_info(entity_candidate_log)")]
            rows = self.store._conn.execute(
                "SELECT * FROM entity_candidate_log").fetchall()
        self.assertEqual(cols, ["id", "at", "event", "names", "sightings"])
        self.assertEqual([(r["event"], r["names"], r["sightings"])
                          for r in rows], [("promoted", 1, 2)])
        self.store.log_entity_candidates("bogus", names=1, sightings=1)
        self.store.log_entity_candidates("expired", names=0, sightings=0)
        self.assertEqual(len(self.store._conn.execute(
            "SELECT * FROM entity_candidate_log").fetchall()), 1)


class PerSeatTests(_Case):
    def test_two_seats_are_measured_apart(self):
        other = _mk(tempfile.mkdtemp(prefix="quill_metrics_b_"))
        self.addCleanup(other.close)
        e = self.ent("Vesper Logistics")
        self.client.post(f"/entities/{e}/forget")
        other.resolve_entity("Harbor Line", "project", ts=self.now)
        self.assertEqual(em.compute(self.store)["forget"]["forgotten"], 1)
        self.assertEqual(em.compute(other)["forget"]["forgotten"], 0)
        self.assertEqual(em.compute(other)["entities"]["visible"], 1)

    def test_the_route_answers_for_this_seat(self):
        self.ent("Harbor Line", "project")
        r = self.client.get("/entities/metrics", params={"days": 7})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["entities"]["visible"], 1)
        self.assertEqual(self.client.get("/entities/metrics").json()
                         ["window"]["days"], 7.0)
        # Declared before /entities/{entity_id}: not read as an id.
        from app.api import routes
        paths = [getattr(r, "path", "") for r in routes.router.routes]
        self.assertLess(paths.index("/entities/metrics"),
                        paths.index("/entities/{entity_id}"))
        from app.api import shell_routes
        hrefs = [link["href"] for g in shell_routes.INSPECT_GROUPS
                 for link in g["links"]]
        self.assertIn("/entities/metrics?days=7", hrefs)

    def test_the_script_prints_one_row_per_seat(self):
        spec = importlib.util.spec_from_file_location(
            "entity_metrics_script", ROOT / "scripts" / "entity_metrics.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        a = self.ent("Vesper Logistics")
        self.client.post(f"/entities/{a}/forget")
        self.ent("Harbor Line", "project")
        b_dir = Path(tempfile.mkdtemp(prefix="quill_metrics_seat_")) / "seat-b"
        b_dir.mkdir()
        other = _mk(str(b_dir))
        other.resolve_entity("Quay Ledger", "tool", ts=self.now)
        other.close()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = script.main(["--db", str(self.store.db_path),
                              "--db", str(b_dir / "quill.db")])
        self.assertEqual(rc, 0)
        lines = [ln.split() for ln in out.getvalue().splitlines()]
        rows = {ln[0]: ln for ln in lines[1:3]}
        head = lines[0]
        a_row = rows[Path(self.tmp).name]
        self.assertEqual(a_row[head.index("forgotten")], "1")
        self.assertEqual(a_row[head.index("forget_rate")], "0.500")
        self.assertEqual(rows["seat-b"][head.index("forgotten")], "0")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(script.main(["--db", "/nonexistent/quill.db"]),
                             2)

    def test_a_store_from_before_the_tables_reads_zeros_and_is_untouched(self):
        tmp = tempfile.mkdtemp(prefix="quill_metrics_old_")
        st = _mk(tmp)
        st.resolve_entity("Harbor Line", "project", ts=self.now)
        st.close()
        # Take the store back to before the entity model: its tables, and
        # the columns it added to `entities`, are gone.
        db = Path(tmp) / "quill.db"
        con = sqlite3.connect(db)
        for t in ("entity_tombstones", "entity_mentions",
                  "entity_pending_relations", "entity_candidate_summaries",
                  "entity_candidate_log", "seat_vocab", "entity_merges",
                  "entity_repair_runs", "entity_repair_log",
                  "entity_repair_proposals", "decisions"):
            con.execute(f"DROP TABLE IF EXISTS {t}")
        con.execute("DROP INDEX IF EXISTS idx_entities_parent")
        for c in ("kind_source", "kind_confidence", "updated_at", "parent_id",
                  "parent_source", "hidden_source"):
            con.execute(f"ALTER TABLE entities DROP COLUMN {c}")
        con.commit()
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        con.close()
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        spec = importlib.util.spec_from_file_location(
            "entity_metrics_script_old", ROOT / "scripts" / "entity_metrics.py")
        script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(script)
        m = script.measure(db, days=7)
        self.assertEqual(m["entities"]["visible"], 1)
        self.assertEqual(m["forget"]["forgotten"], 0)
        self.assertEqual(m["kind_correction"]["corrected"], 0)
        self.assertEqual(m["candidates"]["pending"], 0)
        self.assertEqual(m["never_seen_twice"]["nodes"], 1)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)

if __name__ == "__main__":       # pragma: no cover
    unittest.main()
