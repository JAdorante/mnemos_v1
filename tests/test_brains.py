"""Brains (memory constellation v2): the compiler, the packet, the gate.

Pins the acceptance list in the Nov 10 2026 functional spec against a
seeded temp store:
  * a subject with fewer than 3 items is no brain; each item lands in one
    section; a user move survives a full rebuild; decision claims and open
    commitments land by rule; an empty required section is one system gap
    that filling resolves; rebuilds are position-stable; one new fact moves
    one brain and the memory version;
  * the packet withholds pending items by default and labels them when
    asked, respects a token budget without dropping headers, never carries
    a verbatim quote, obeys the perimeter and sealed rungs, logs every call
    to the egress trail, gives the MCP tool and the HTTP route identical
    text, and cannot be made to grow a section by an item that says
    "## Decisions".
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))
os.environ.setdefault("QUILL_HEADS", "off")

import app.config  # noqa: E402
from app.events import Event, Modality  # noqa: E402
from app.services import brains, egress  # noqa: E402
from app.services.records import node_store  # noqa: E402
from app.storage import Store  # noqa: E402

NOW = time.time()


def _claim(store, *, kind, subject_ref, label, text, status="proposed",
           record_ref=None, privacy="internal", personal_only=False,
           event_id=None, t=None, predicate=None, extra=None):
    value = {"text": text}
    if kind == "status":
        value = {"state": text}
    if extra:
        value.update(extra)
    cid = node_store.insert_claim(store, {
        "kind": kind, "subject_ref": subject_ref, "subject_label": label,
        "predicate": predicate or {"decision": "decided", "commitment": "owes",
                                   "status": "deal.stage"}.get(kind, "says"),
        "value": value, "schema_version": f"{kind}-v1",
        "canonical_hash": f"h-{kind}-{abs(hash(text))}", "confidence": 0.9,
        "evidence": [{"event_id": event_id, "t": t or NOW - 3000,
                      "source": "meeting", "span": "VERBATIM-SPAN"}],
        "privacy_class": privacy, "capture_source": "meeting",
        "status": status, "personal_only": personal_only,
    })
    if record_ref:
        node_store.update_claim(store, cid, record_ref=record_ref)
    return cid


def _seed_borealis(s, e):
    """A second deal with three rule-placed facts and nobody on it yet."""
    s.resolve_entity("Project Borealis", "project", ts=NOW)
    for i, txt in enumerate(("Project Borealis thesis: upside from roll-ups.",
                             "The opportunity on Project Borealis is margin growth.",
                             "Project Borealis has a risk: key-man exposure.")):
        f = s.add_claim(txt, source_event_id=e, source_span="",
                        confidence=0.8, extracted_at=NOW - 100 - i)
        s.review_fact(f, "approved")


class Seeded(unittest.TestCase):
    rung = "connected"

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="brains-")
        self.store = Store(db_path=Path(self.dir) / "quill.db",
                           audio_dir=Path(self.dir) / "audio")
        from app.services.model_log import model_log
        self._trail = model_log._path
        model_log._path = Path(self.dir) / "model_calls.jsonl"
        self._patches = [
            mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": self.rung,
                                         "QUILL_DATA_DIR": self.dir}),
            mock.patch.object(egress, "_org_rung", (0.0, "")),
        ]
        for p in self._patches:
            p.start()
        self.ids = self.seed(self.store)

    def tearDown(self):
        from app.services.model_log import model_log
        model_log._path = self._trail
        for p in reversed(self._patches):
            p.stop()
        self.store.close()

    @staticmethod
    def seed(store) -> dict:
        s = store
        e1 = s.insert(Event(time=NOW - 3600, modality=Modality.AUDIO,
                            raw="QUOTE-FROM-THE-ROOM about Halcyon",
                            summary="Halcyon sync", source="meeting.audio",
                            people=["Priya", "Dan"]))
        halcyon = s.resolve_entity("Project Halcyon", "project", ts=NOW)
        priya = s.resolve_person("Priya Shah", ts=NOW)
        dan = s.resolve_person("Dan Ortega", ts=NOW)
        thesis = s.add_claim(
            "Project Halcyon thesis: margin expansion from pricing power.",
            source_event_id=e1, source_span="SOURCE-SPAN-ONE",
            confidence=0.85, extracted_at=NOW - 3600)
        s.review_fact(thesis, "approved")
        risk = s.add_claim(
            "A risk on Project Halcyon is customer churn in Q4.",
            source_event_id=e1, source_span="SOURCE-SPAN-TWO",
            confidence=0.7, extracted_at=NOW - 3000)
        commit = s.add_commitment(
            "send the follow-up question list for Project Halcyon",
            source_event_id=e1, from_person_id=priya, to_person_id=dan,
            due="2026-11-12", confidence=0.9, extracted_at=NOW - 2000)
        s.review_fact(commit, "approved")
        task = s.add_task("schedule site visits for Project Halcyon",
                          source_event_id=e1, owner_person_id=dan,
                          due="2026-11-20", confidence=0.7, extracted_at=NOW - 1000)
        decision = _claim(s, kind="decision", subject_ref="entity:project halcyon",
                          label="Project Halcyon",
                          text="Proceed to a second round on Halcyon.",
                          status="recorded", record_ref="rec:1", event_id=e1)
        s.add_relation("person", priya, "works_at", "entity", halcyon,
                       origin="asserted", confidence=0.85, ts=NOW - 3600)
        s.set_person_attr(priya, "role", "CFO", None, NOW)
        return {"event": e1, "halcyon": halcyon, "priya": priya, "dan": dan,
                "thesis": thesis, "risk": risk, "commit": commit, "task": task,
                "decision": decision}

    def brain(self, bid=None):
        return brains.get_brain(self.store, bid or f"entity:{self.ids['halcyon']}")

    def section(self, key, bid=None):
        for s in self.brain(bid)["sections"]:
            if s["key"] == key:
                return s
        raise AssertionError(key)


# ------------------------------------------------------------- compiler --
class CompilerTests(Seeded):
    def test_fewer_than_three_items_is_no_brain(self):
        s = self.store
        s.resolve_entity("Project Lonely", "project", ts=NOW)
        s.add_claim("Project Lonely has one fact.", source_event_id=self.ids["event"],
                    source_span="", confidence=0.8, extracted_at=NOW - 10)
        brains.rebuild(s)
        ids = {b["id"] for b in brains.list_brains(s)["brains"]}
        self.assertIn(f"entity:{self.ids['halcyon']}", ids)
        self.assertNotIn(f"entity:{s.find_entity_exact('Project Lonely')}", ids)

    def test_each_item_lands_in_exactly_one_section(self):
        brains.rebuild(self.store)
        with self.store._lock:
            total, distinct = self.store._conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT subject_type || subject_id || "
                "item_type || item_id) FROM brain_items").fetchone()
        self.assertGreater(total, 0)
        self.assertEqual(total, distinct)
        b = self.brain()
        refs = [i["ref"] for s in b["sections"] for i in s["items"]]
        self.assertEqual(len(refs), len(set(refs)))
        # the unique index is what makes a second section impossible
        with self.assertRaises(sqlite3.IntegrityError), self.store._lock:
            self.store._conn.execute(
                "INSERT INTO brain_items (subject_type, subject_id, section_key, "
                "item_type, item_id, text, review_state, updated_at) "
                "SELECT subject_type, subject_id, 'risks', item_type, item_id, text, "
                "review_state, updated_at FROM brain_items LIMIT 1")

    def test_decision_claim_and_open_commitment_land_by_rule(self):
        brains.rebuild(self.store)
        dec = self.section("decisions")
        self.assertEqual([i["ref"] for i in dec["items"]],
                         [f"claim:{self.ids['decision']}"])
        self.assertEqual(dec["items"][0]["assigned_by"], "rule")
        self.assertEqual(dec["items"][0]["review_state"], "approved")
        com = self.section("commitments")
        self.assertEqual([i["ref"] for i in com["items"]],
                         [f"commitment:{self.ids['commit']}"])
        self.assertIn("Priya Shah owes Dan Ortega", com["items"][0]["text"])
        self.assertEqual(com["items"][0]["assigned_by"], "rule")
        # keyword rules, not a model: the risk sentence is in Risks
        self.assertEqual([i["ref"] for i in self.section("risks")["items"]],
                         [f"fact:{self.ids['risk']}"])
        people = self.section("people")
        self.assertEqual(len(people["items"]), 1)
        self.assertIn("Priya Shah works at Project Halcyon (CFO)", people["items"][0]["text"])

    def test_move_override_survives_a_full_rebuild(self):
        brains.rebuild(self.store)
        bid = f"entity:{self.ids['halcyon']}"
        res = brains.move_item(self.store, bid, "fact", str(self.ids["risk"]), "thesis")
        self.assertEqual(res["item"]["section"], "thesis")
        brains.rebuild(self.store, scope="full")
        self.assertIn(f"fact:{self.ids['risk']}",
                      [i["ref"] for i in self.section("thesis")["items"]])
        self.assertNotIn(f"fact:{self.ids['risk']}",
                         [i["ref"] for i in self.section("risks")["items"]])
        moved = next(i for i in self.section("thesis")["items"]
                     if i["ref"] == f"fact:{self.ids['risk']}")
        self.assertEqual(moved["assigned_by"], "user")
        # hide is the other user word, and it also survives
        brains.hide_item(self.store, bid, "fact", str(self.ids["risk"]))
        brains.rebuild(self.store, scope="full")
        self.assertNotIn(f"fact:{self.ids['risk']}",
                         [i["ref"] for s in self.brain()["sections"] for i in s["items"]])

    def test_empty_required_section_is_one_system_gap_until_filled(self):
        s = self.store
        # a second deal with items but no people yet
        e = self.ids["event"]
        s.resolve_entity("Project Borealis", "project", ts=NOW)
        _seed_borealis(s, e)
        brains.rebuild(s)
        bor = f"entity:{s.find_entity_exact('Project Borealis')}"
        gaps = [i for i in brains.get_brain(s, bor)["sections"][-1]["items"]
                if i["item_type"] == "gap"]
        self.assertEqual([g["text"] for g in gaps if "people" in g["text"]],
                         ["Who are the people on Project Borealis?"])
        brains.rebuild(s)       # a second pass does not add a second gap
        gaps2 = [i for i in brains.get_brain(s, bor)["sections"][-1]["items"]
                 if "people" in i["text"]]
        self.assertEqual(len(gaps2), 1)
        # filling People resolves it
        s.add_relation("person", self.ids["dan"], "leads", "entity",
                       s.find_entity_exact("Project Borealis"),
                       origin="asserted", confidence=0.9, ts=NOW)
        brains.rebuild(s)
        texts = [i["text"] for i in brains.get_brain(s, bor)["sections"][-1]["items"]]
        self.assertNotIn("Who are the people on Project Borealis?", texts)
        with s._lock:
            row = s._conn.execute(
                "SELECT resolved_at, resolved_item FROM brain_gaps WHERE question = ?",
                ("Who are the people on Project Borealis?",)).fetchone()
        self.assertIsNotNone(row["resolved_at"])
        self.assertTrue(str(row["resolved_item"]).startswith("relation:"))

    def test_two_rebuilds_give_identical_positions(self):
        brains.rebuild(self.store)
        first = brains.list_brains(self.store)["brains"]
        leaves1 = {i["ref"]: i["pos"] for s in self.brain()["sections"] for i in s["items"]}
        brains.rebuild(self.store)
        second = brains.list_brains(self.store)["brains"]
        leaves2 = {i["ref"]: i["pos"] for s in self.brain()["sections"] for i in s["items"]}
        self.assertEqual([(b["id"], b["pos"]) for b in first],
                         [(b["id"], b["pos"]) for b in second])
        self.assertEqual(leaves1, leaves2)
        dirs = [s["dir"] for s in self.brain()["sections"]]
        for d in dirs:
            self.assertAlmostEqual(sum(c * c for c in d), 1.0, places=3)

    def test_one_new_fact_moves_one_brain_and_the_version(self):
        s = self.store
        e = self.ids["event"]
        s.resolve_entity("Project Borealis", "project", ts=NOW)
        _seed_borealis(s, e)
        brains.rebuild(s)
        before = {b["id"]: b for b in brains.list_brains(s)["brains"]}
        v0 = s.memory_version()
        hal_leaves = {i["ref"]: i["pos"] for sec in self.brain()["sections"] for i in sec["items"]}
        time.sleep(0.01)
        f = s.add_claim("Project Borealis has a new risk: a competing bid.",
                        source_event_id=e, source_span="", confidence=0.8,
                        extracted_at=time.time())
        s.review_fact(f, "approved")
        res = brains.list_brains(s)          # ensure_compiled runs the dirty pass
        after = {b["id"]: b for b in res["brains"]}
        self.assertNotEqual(v0, s.memory_version())
        bor = f"entity:{s.find_entity_exact('Project Borealis')}"
        hal = f"entity:{self.ids['halcyon']}"
        self.assertGreater(after[bor]["updated_at"], before[bor]["updated_at"])
        self.assertEqual(after[hal]["updated_at"], before[hal]["updated_at"])
        self.assertEqual(after[hal]["pos"], before[hal]["pos"])
        self.assertEqual(
            hal_leaves,
            {i["ref"]: i["pos"] for sec in self.brain()["sections"] for i in sec["items"]})
        # ?since= returns only the brain that moved
        changed = brains.list_brains(s, since=v0)["brains"]
        self.assertEqual([b["id"] for b in changed], [bor])


# --------------------------------------------------------------- packet --
class PacketTests(Seeded):
    def bid(self):
        return f"entity:{self.ids['halcyon']}"

    def test_default_packet_withholds_pending_and_counts_them(self):
        brains.rebuild(self.store)
        pkt = brains.compile_packet(self.store, self.bid())
        self.assertEqual(pkt["verdict"], "allow")
        self.assertNotIn("(unreviewed)", pkt["text"])
        self.assertNotIn("customer churn", pkt["text"])
        self.assertEqual(pkt["withheld"]["pending"], 3)   # risk, task, works_at edge
        self.assertIn("Proceed to a second round on Halcyon.", pkt["text"])
        self.assertIn("## Commitments", pkt["text"])
        self.assertIn("[Meeting · ", pkt["text"])
        self.assertTrue(pkt["text"].startswith("Context for Project Halcyon, a live deal."))
        self.assertEqual(pkt["tokens"], -(-len(pkt["text"]) // 4))

    def test_include_pending_adds_the_prefix(self):
        brains.rebuild(self.store)
        pkt = brains.compile_packet(self.store, self.bid(), include_pending=True)
        self.assertIn("- (unreviewed) A risk on Project Halcyon is customer churn in Q4.",
                      pkt["text"])
        self.assertEqual(pkt["withheld"]["pending"], 0)
        # gaps ride along as UNKNOWN lines whatever the flags
        s = self.store
        brains.add_gap(s, self.bid(), "open_questions", "Who is the strategic buyer?")
        pkt = brains.compile_packet(s, self.bid())
        self.assertIn("- UNKNOWN: Who is the strategic buyer?", pkt["text"])

    def test_budget_keeps_headers_and_reports_what_it_dropped(self):
        s = self.store
        e = self.ids["event"]
        for i in range(40):
            f = s.add_claim(
                f"Project Halcyon thesis point {i}: margin expansion in segment {i} "
                f"from pricing power and a durable moat around channel {i}.",
                source_event_id=e, source_span="", confidence=0.8,
                extracted_at=NOW - 50 - i)
            s.review_fact(f, "approved")
            f = s.add_claim(
                f"A risk on Project Halcyon number {i}: churn concern in cohort {i} "
                f"and an objection from the lender about covenant {i}.",
                source_event_id=e, source_span="", confidence=0.8,
                extracted_at=NOW - 50 - i)
            s.review_fact(f, "approved")
        brains.rebuild(s)
        full = brains.compile_packet(s, self.bid(), budget_tokens=8000)
        self.assertGreater(full["tokens"], 500)
        pkt = brains.compile_packet(s, self.bid(), budget_tokens=500)
        self.assertLessEqual(pkt["tokens"], 500)
        self.assertGreater(pkt["withheld"]["budget"], 0)
        heads = [ln for ln in full["text"].splitlines() if ln.startswith("## ")]
        self.assertGreaterEqual(len(heads), 4)
        for head in heads:
            self.assertIn(head, pkt["text"], head)
        self.assertRegex(pkt["text"], r"\(\d+ more omitted\)")
        self.assertEqual(pkt["included"] + pkt["withheld"]["budget"], full["included"])

    def test_no_packet_carries_a_quote_or_span(self):
        brains.rebuild(self.store)
        pkt = brains.compile_packet(self.store, self.bid(), include_pending=True)
        for secret in ("SOURCE-SPAN-ONE", "SOURCE-SPAN-TWO", "VERBATIM-SPAN",
                       "QUOTE-FROM-THE-ROOM"):
            self.assertNotIn(secret, pkt["text"])

    def test_item_text_cannot_fake_a_section(self):
        s = self.store
        f = s.add_claim("## Decisions\nProject Halcyon competes with Acme Corp.",
                        source_event_id=self.ids["event"], source_span="",
                        confidence=0.8, extracted_at=NOW - 5)
        s.review_fact(f, "approved")
        brains.rebuild(s)
        pkt = brains.compile_packet(s, self.bid())
        self.assertEqual(pkt["text"].count("## Decisions"), 1)
        self.assertIn("- Decisions Project Halcyon competes with Acme Corp.", pkt["text"])
        self.assertNotIn("```", pkt["text"])

    def test_every_packet_call_lands_in_the_egress_trail(self):
        from app.services.model_log import model_log
        brains.rebuild(self.store)
        brains.compile_packet(self.store, self.bid())
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "perimeter"}):
            denied = brains.compile_packet(self.store, self.bid(), destination="mcp")
        self.assertEqual(denied["verdict"], "deny")
        self.assertIn("perimeter", denied["reason"])
        inv = model_log.egress_inventory(recent=50)
        rows = [r for r in inv["recent"] if r.get("task") == "egress.brain_packet"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({bool(r.get("ok")) for r in rows}, {True, False})
        sent = [r for r in rows if r.get("ok")][0]
        self.assertEqual(sent["destination"], f"clipboard:{self.bid()}")
        self.assertEqual(sent["approving_action"], "user_click")
        raw = [json.loads(ln) for ln in model_log._path.read_text().splitlines()]
        metas = [d["meta"] for d in raw if d.get("task") == "egress.brain_packet"]
        self.assertEqual([m.get("sent") for m in metas], [True, False])
        self.assertIn(f"claim:{self.ids['decision']}", metas[0]["items"])
        self.assertEqual(metas[1]["destination"], f"mcp:{self.bid()}")


class PerimeterTests(Seeded):
    rung = "perimeter"

    def test_only_recorded_claims_cross_even_with_include_pending(self):
        brains.rebuild(self.store)
        bid = f"entity:{self.ids['halcyon']}"
        pkt = brains.compile_packet(self.store, bid, include_pending=True)
        self.assertEqual(pkt["verdict"], "allow")
        self.assertIn("Proceed to a second round on Halcyon.", pkt["text"])
        self.assertNotIn("margin expansion", pkt["text"])     # approved fact, no record
        self.assertNotIn("(unreviewed)", pkt["text"])
        self.assertEqual(pkt["included"], 1)
        self.assertGreater(pkt["withheld"]["privacy"] + pkt["withheld"]["pending"], 0)
        mcp = brains.compile_packet(self.store, bid, destination="mcp")
        self.assertEqual(mcp["verdict"], "deny")


class SealedTests(Seeded):
    rung = "sealed"

    def test_item_above_internal_is_withheld_and_the_rest_returns(self):
        s = self.store
        _claim(s, kind="decision", subject_ref="entity:project halcyon",
               label="Project Halcyon", text="Dan decided to tell his doctor first.",
               status="approved", privacy="personal", event_id=self.ids["event"])
        _claim(s, kind="decision", subject_ref="entity:project halcyon",
               label="Project Halcyon", text="Keep this one to myself.",
               status="approved", personal_only=True, event_id=self.ids["event"])
        brains.rebuild(s)
        bid = f"entity:{self.ids['halcyon']}"
        pkt = brains.compile_packet(s, bid)
        self.assertEqual(pkt["verdict"], "allow")
        self.assertIn("Proceed to a second round on Halcyon.", pkt["text"])
        self.assertNotIn("doctor", pkt["text"])
        self.assertNotIn("Keep this one to myself", pkt["text"])
        self.assertEqual(pkt["withheld"]["privacy"], 1)
        self.assertEqual(pkt["withheld"]["personal"], 1)
        self.assertEqual(pkt["rung"], "sealed")
        # over MCP the click is not an approval: sealed needs a human
        mcp = brains.compile_packet(s, bid, destination="mcp")
        self.assertEqual(mcp["verdict"], "deny")

    def test_personal_only_never_leaves_on_connected_either(self):
        s = self.store
        _claim(s, kind="decision", subject_ref="entity:project halcyon",
               label="Project Halcyon", text="Keep this one to myself.",
               status="approved", personal_only=True, event_id=self.ids["event"])
        brains.rebuild(s)
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "connected"}):
            pkt = brains.compile_packet(s, f"entity:{self.ids['halcyon']}")
        self.assertNotIn("Keep this one to myself", pkt["text"])
        self.assertEqual(pkt["withheld"]["personal"], 1)
        # it still renders on the map for its owner
        texts = [i["text"] for sec in brains.get_brain(s, f"entity:{self.ids['halcyon']}")["sections"]
                 for i in sec["items"]]
        self.assertIn("Keep this one to myself.", texts)


# ----------------------------------------------------------------- http --
class RouteTests(Seeded):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def setUp(self):
        super().setUp()
        from app.api import brain_routes
        self._route_patch = mock.patch.object(brain_routes, "_store",
                                              lambda: self.store)
        self._route_patch.start()
        brain_routes._rate.clear()
        brains.rebuild(self.store)
        self.client.get("/brains")          # sets the CSRF cookie
        tok = self.client.cookies.get("quill_csrf") or ""
        self.h = {"Origin": "http://testserver", "X-CSRF-Token": tok}

    def post(self, path, json):
        return self.client.post(path, json=json, headers=self.h)

    def tearDown(self):
        self._route_patch.stop()
        super().tearDown()

    def test_list_and_get(self):
        r = self.client.get("/brains")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertIn("version", body)
        ids = {b["id"] for b in body["brains"]}
        bid = f"entity:{self.ids['halcyon']}"
        self.assertIn(bid, ids)
        one = next(b for b in body["brains"] if b["id"] == bid)
        for key in ("type", "label", "activity", "pos", "sections", "items_total",
                    "sources_total", "updated_at"):
            self.assertIn(key, one)
        self.assertEqual(one["type"], "deal")
        r = self.client.get(f"/brains/{bid}")
        self.assertEqual(r.status_code, 200)
        b = r.json()
        self.assertEqual([s["key"] for s in b["sections"]],
                         ["thesis", "people", "status", "commitments", "risks",
                          "competitors", "decisions", "open_questions"])
        item = b["sections"][6]["items"][0]
        for key in ("ref", "text", "review_state", "source_label", "observed_at",
                    "pos", "size"):
            self.assertIn(key, item)
        self.assertEqual(self.client.get("/brains/entity:999999").status_code, 404)
        self.assertEqual(self.client.get("/brains/nonsense").status_code, 404)
        self.assertEqual(self.client.get("/brains?type=widget").status_code, 422)
        r = self.client.get(f"/brains/{bid.replace(':', '%3A')}")
        self.assertEqual(r.status_code, 200)

    def test_packet_route_status_codes(self):
        bid = f"entity:{self.ids['halcyon']}"
        r = self.post(f"/brains/{bid}/packet", json={})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["verdict"], "allow")
        self.assertIn("egress_id", body)
        self.assertEqual(self.post(f"/brains/{bid}/packet",
                                          json={"scope": "bogus"}).status_code, 422)
        self.assertEqual(self.post(f"/brains/{bid}/packet",
                                          json={"budget_tokens": 99999}).status_code, 422)
        self.assertEqual(self.post(f"/brains/{bid}/packet",
                                          json={"scope": "section", "section": "nope"}).status_code, 404)
        r = self.post(f"/brains/{bid}/packet",
                             json={"scope": "section", "section": "decisions"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("## Decisions", r.json()["text"])
        self.assertNotIn("## Commitments", r.json()["text"])
        with mock.patch.dict(os.environ, {"QUILL_EGRESS_RUNG": "perimeter"}):
            r = self.post(f"/brains/{bid}/packet", json={"destination": "mcp"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["verdict"], "deny")
        self.assertIn("reason", r.json())
        r = self.client.get("/privacy/egress?recent=50")
        tasks = [row.get("task") for row in r.json()["cloud"]["recent"]]
        self.assertIn("egress.brain_packet", tasks)

    def test_packet_rate_limit(self):
        from app.api import brain_routes
        bid = f"entity:{self.ids['halcyon']}"
        with mock.patch.object(brain_routes, "PACKET_RATE_PER_MIN", 3):
            codes = [self.post(f"/brains/{bid}/packet", json={}).status_code
                     for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])

    def test_overrides_and_gaps_over_http(self):
        bid = f"entity:{self.ids['halcyon']}"
        r = self.post(f"/brains/{bid}/items/move",
                             json={"item_type": "fact", "item_id": str(self.ids["risk"]),
                                   "section": "thesis"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["item"]["section"], "thesis")
        r = self.post(f"/brains/{bid}/items/move",
                             json={"item_type": "fact", "item_id": "1", "section": "nope"})
        self.assertEqual(r.status_code, 404)
        r = self.post(f"/brains/{bid}/gaps",
                             json={"section": "open_questions",
                                   "question": "Who is the strategic buyer?"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["gap"]["created_by"], "user")
        r = self.post(f"/brains/{bid}/items/hide",
                             json={"item_type": "fact", "item_id": str(self.ids["risk"])})
        self.assertEqual(r.json(), {"ok": True})
        r = self.post(f"/brains/{bid}/type", json={"brain_type": "portco"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["brain"]["type"], "portco")
        self.assertEqual(self.post(f"/brains/{bid}/type",
                                          json={"brain_type": "person"}).status_code, 422)
        # the move was logged as an adjudication (training data for step 5)
        with self.store._lock:
            kinds = [r[0] for r in self.store._conn.execute(
                "SELECT kind FROM kg_adjudications").fetchall()]
        self.assertIn("brain_move", kinds)
        self.assertIn("brain_hide", kinds)

    def test_mcp_tool_and_http_route_return_identical_text(self):
        from app.services import mcp_tools
        from app.services.memory import memory
        bid = f"entity:{self.ids['halcyon']}"
        http = self.post(f"/brains/{bid}/packet",
                                json={"destination": "mcp"}).json()
        with mock.patch.object(memory, "_ensure_store", lambda: self.store):
            res = mcp_tools.call_tool("brain_context", {"subject": "Project Halcyon"})
            by_id = mcp_tools.call_tool("brain_context", {"subject": bid})
            missing = mcp_tools.call_tool("brain_context", {"subject": "Nobody Here"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["result"]["text"], http["text"])
        self.assertEqual(by_id["result"]["text"], http["text"])
        self.assertEqual(res["result"]["brain"], bid)
        self.assertFalse(missing["result"]["found"])
        self.assertIn("brain_context", {t["name"] for t in mcp_tools.tool_schemas()})


class ShellTests(unittest.TestCase):
    """The mode entry: a Brains chip on /memory, the script on demand, and
    the Inspect index link beside Constellation."""

    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app.main import app
        cls.client = TestClient(app)

    def test_memory_page_has_the_brains_mode(self):
        page = self.client.get("/memory?mode=brains").text
        self.assertIn('data-layer="brains"', page)
        self.assertIn('id="brainsPane"', page)
        self.assertIn("/static/js/sparrow-brains.js?v=", page)
        self.assertNotIn("@@BRAINS_JS@@", page)
        self.assertNotIn("sparrow-brains.js\"></script>", page)   # loaded on demand, not eagerly

    def test_static_assets_are_served_offline(self):
        for path in ("/static/js/sparrow-brains.js", "/static/js/vendor/three.module.min.js"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
        js = self.client.get("/static/js/sparrow-brains.js").text
        self.assertNotIn("cdn.", js)
        self.assertIn("window.SparrowBrains", js)
        for key in ("thesis", "people", "status", "commitments", "risks",
                    "competitors", "decisions", "open_questions"):
            self.assertIn(f"{key}:", js)

    def test_inspect_lists_brains_beside_constellation(self):
        page = self.client.get("/inspect").text
        self.assertIn("/memory?mode=brains", page)
        self.assertIn("/memory?mode=constellation", page)


# ------------------------------------------------------------ classifier --
class _FakeLocal:
    """Stands in for OllamaText: labels by keyword, records the calls."""

    def __init__(self, up=True):
        self.up = up
        self.calls = []

    def available(self):
        return self.up

    def complete(self, task, *, system, messages, max_tokens, schema=None, exemplars=""):
        self.calls.append(messages[0]["content"])
        body = messages[0]["content"].split("Facts:\n", 1)[1]
        labels = []
        for line in body.splitlines():
            low = line.lower()
            labels.append("competitor" if "acme" in low else
                          "decision" if "agreed" in low else "other")
        return {"text": json.dumps({"labels": labels}), "json": {"labels": labels},
                "confidence": 0.9, "parse_ok": True, "truncated": False}


class ClassifierTests(Seeded):
    def test_hint_places_a_fact_after_rules_and_user_moves_outrank_it(self):
        s = self.store
        e = self.ids["event"]
        hinted = s.add_claim("Project Halcyon also fields Acme Corp in every bake-off.",
                             source_event_id=e, source_span="", confidence=0.8,
                             extracted_at=NOW - 20, section_hint="competitor")
        s.review_fact(hinted, "approved")
        keyworded = s.add_claim("The lender raised an objection on Project Halcyon.",
                                source_event_id=e, source_span="", confidence=0.8,
                                extracted_at=NOW - 19, section_hint="thesis")
        s.review_fact(keyworded, "approved")
        self.assertEqual(s.get_fact(hinted)["section_hint"], "competitor")
        brains.rebuild(s)
        comp = self.section("competitors")
        self.assertIn(f"fact:{hinted}", [i["ref"] for i in comp["items"]])
        placed = next(i for i in comp["items"] if i["ref"] == f"fact:{hinted}")
        self.assertEqual(placed["assigned_by"], "model")
        # the deterministic keyword rule had its turn first
        self.assertIn(f"fact:{keyworded}", [i["ref"] for i in self.section("risks")["items"]])
        # and the user's move beats the hint
        brains.move_item(s, f"entity:{self.ids['halcyon']}", "fact", str(hinted), "thesis")
        self.assertIn(f"fact:{hinted}", [i["ref"] for i in self.section("thesis")["items"]])
        self.assertNotIn(f"fact:{hinted}", [i["ref"] for i in self.section("competitors")["items"]])

    def test_backlog_pass_labels_only_what_no_rule_placed(self):
        from app.services import brain_classifier
        s = self.store
        e = self.ids["event"]
        plain = s.add_claim("Project Halcyon keeps losing bids to Acme Corp lately.",
                            source_event_id=e, source_span="", confidence=0.8,
                            extracted_at=NOW - 20)
        s.review_fact(plain, "approved")
        agreed = s.add_claim("Everyone agreed Project Halcyon moves to phase two.",
                             source_event_id=e, source_span="", confidence=0.8,
                             extracted_at=NOW - 18)
        s.review_fact(agreed, "approved")
        brains.rebuild(s)
        refs = [i["ref"] for sec in self.brain()["sections"] for i in sec["items"]]
        self.assertNotIn(f"fact:{plain}", refs)
        self.assertNotIn(f"fact:{agreed}", refs)
        fake = _FakeLocal()
        with mock.patch.object(brain_classifier, "_client", fake):
            res = brain_classifier.backlog_pass(s, limit=50)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["classified"], 2)
        self.assertGreaterEqual(res["subjects"], 1)
        self.assertEqual(s.get_fact(plain)["section_hint"], "competitor")
        self.assertEqual(s.get_fact(agreed)["section_hint"], "decision")
        # the pass recompiled: the facts now sit in their sections
        self.assertIn(f"fact:{plain}", [i["ref"] for i in self.section("competitors")["items"]])
        self.assertIn(f"fact:{agreed}", [i["ref"] for i in self.section("decisions")["items"]])
        # rule-placed facts were never sent to the model
        sent = "\n".join(fake.calls)
        self.assertNotIn("margin expansion", sent)
        self.assertNotIn("Proceed to a second round", sent)
        # nothing left to do on a second pass
        with mock.patch.object(brain_classifier, "_client", _FakeLocal()):
            again = brain_classifier.backlog_pass(s, limit=50)
        self.assertEqual(again["considered"], 0)

    def test_backlog_pass_writes_nothing_when_the_local_model_is_down(self):
        from app.services import brain_classifier
        s = self.store
        plain = s.add_claim("Project Halcyon keeps losing bids to Acme Corp lately.",
                            source_event_id=self.ids["event"], source_span="",
                            confidence=0.8, extracted_at=NOW - 20)
        brains.rebuild(s)
        with mock.patch.object(brain_classifier, "_client", _FakeLocal(up=False)):
            res = brain_classifier.backlog_pass(s, limit=50)
        self.assertTrue(res["unavailable"])
        self.assertEqual(res["classified"], 0)
        self.assertIsNone(s.get_fact(plain)["section_hint"])

    def test_extractor_asks_for_the_hint_only_behind_the_flag(self):
        from app.services import extractor
        with mock.patch.dict(os.environ, {"QUILL_EXTRACT_SECTION_HINT": "0"}):
            props = extractor._schema()["properties"]["claims"]["items"]["properties"]
            self.assertNotIn("section_hint", props)
        with mock.patch.dict(os.environ, {"QUILL_EXTRACT_SECTION_HINT": "1"}):
            items = extractor._schema()["properties"]["claims"]["items"]
            self.assertIn("section_hint", items["properties"])
            self.assertIn("section_hint", items["required"])
            self.assertIn("risk", items["properties"]["section_hint"]["enum"])
            self.assertIn("", items["properties"]["section_hint"]["enum"])
        self.assertNotIn("section_hint", extractor._SCHEMA["properties"]["claims"]["items"]["properties"])


# -------------------------------------------------------------- harness --
class QualityHarnessTests(Seeded):
    """The blind three-way run: same budget per condition, answers
    shuffled and unlabeled, the key kept apart, and a score that names the
    winner the way the spec's bar does."""

    def test_run_blinds_and_score_reads_the_key(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "brains_quality", Path("scripts/brains_quality.py"))
        bq = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(bq)
        brains.rebuild(self.store)
        out = Path(self.dir) / "quality"
        tasks = Path(self.dir) / "tasks.json"
        tasks.write_text(json.dumps([
            {"task": "draft the follow-up email to the CFO", "subject": "Project Halcyon"},
            {"task": "list what is still unknown", "subject": f"entity:{self.ids['halcyon']}"},
        ]), "utf-8")
        seen = []

        def fake_complete(task, *, system, messages, max_tokens=700, **kw):
            seen.append(messages[0]["content"])
            return "ANSWER " + str(len(seen))

        class _Mem:
            def search(self, q, limit=40):
                return [{"text": "Priya mentioned churn in Q4 SOURCE-SPAN-TWO", "source": "meeting"},
                        {"text": "x" * 9000, "source": "mail"}]

            def _ensure_store(self_inner):
                return self.store

        from app.services import memory as memmod
        from app.services.model_router import router
        with mock.patch.object(memmod, "memory", _Mem()), \
                mock.patch.object(router, "complete", fake_complete):
            bq.run(tasks, out, 300, seed=1)
        blind = [json.loads(l) for l in (out / "blind.jsonl").read_text().splitlines()]
        key = json.loads((out / "key.json").read_text())
        self.assertEqual(len(blind), 6)
        self.assertEqual(len(key), 6)
        self.assertTrue(all("condition" not in r for r in blind))
        self.assertEqual(sorted(k["condition"] for k in key.values()),
                         sorted(["none", "search", "packet"] * 2))
        # every context respects the budget; the packet is the real packet
        for k in key.values():
            self.assertLessEqual(k["context_tokens"], 300)
        packets = [c for c in seen if "Context for Project Halcyon, a live deal" in c]
        self.assertEqual(len(packets), 2)
        self.assertTrue(any("Memory search results" in c for c in seen))
        self.assertTrue(any("(no context)" in c for c in seen))
        # score: packet wins both tasks -> clearly better
        rows = []
        for r in blind:
            cond = key[r["id"]]["condition"]
            r["score"] = {"none": 1, "search": 3, "packet": 5}[cond]
            rows.append(json.dumps(r))
        (out / "blind.jsonl").write_text("\n".join(rows) + "\n")
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            bq.score(out)
        self.assertIn("packet beats search on 2/2 tasks", buf.getvalue())
        self.assertIn("clearly better", buf.getvalue())
        self.assertNotIn("NOT clearly", buf.getvalue())


class BackgroundRebuildTests(Seeded):
    def test_large_first_rebuild_runs_off_the_request_path(self):
        brains._state.pop(id(self.store), None)
        with self.store._lock:
            self.store._conn.execute("DELETE FROM brain_items")
            self.store._conn.commit()
        with mock.patch.object(brains, "FULL_REBUILD_INLINE_FACTS", 0):
            res = brains.list_brains(self.store)
            self.assertTrue(res["building"])
            t = brains._building.get(id(self.store))
            self.assertIsNotNone(t)
            t.join(timeout=30)
        self.assertFalse(brains.building(self.store))
        res = brains.list_brains(self.store)
        self.assertFalse(res["building"])
        self.assertIn(f"entity:{self.ids['halcyon']}", [b["id"] for b in res["brains"]])


if __name__ == "__main__":
    unittest.main()
