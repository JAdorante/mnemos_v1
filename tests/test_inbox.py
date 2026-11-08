"""Inbox — one queue over the existing decision queues (frontend spec, step 2).

Pins: the aggregate's shape and chip contract; every action goes through its
existing verb; undo for 10 s where a clean reverse exists; reads are pure;
the page and the JSON come from the same payload; acting from the page hits
the same route as acting from anywhere else.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.events import Event, Modality
from app.services import inbox
from app.storage import Store


class _NoLog:
    def open_offers(self, *a, **k):
        return []


def _isolate() -> list:
    """The queues that are not store-scoped (chat offers, peer asks, assistant
    notes, connector consent) read process-wide state; pin them empty so the
    tests see only the seeded store."""
    return [
        patch("app.services.chat_log.get_log", return_value=_NoLog()),
        patch("app.services.peer_channel.pending_asks", return_value=[]),
        patch("app.services.assistant_inbox.items", return_value=[]),
        patch.object(inbox, "_consent_items", return_value=[]),
        patch.object(inbox, "_claim_items", return_value=[]),
    ]


def _seed_dispute(store: Store) -> tuple[int, int]:
    """Two simultaneous values for one field → a flagged dispute (kg_beliefs).
    record_from_claim accepts the claim predicates (costs, priced_at, due_on)."""
    from app.services import kg_beliefs
    now = time.time()
    subj = store.resolve_entity("pilot plan", "other", ts=now)
    a = store.resolve_entity("$49", "other", ts=now)
    b = store.resolve_entity("$55", "other", ts=now)
    kg_beliefs.record_from_claim(
        store, subj_type="entity", subj_id=subj, predicate="costs",
        obj_type="entity", obj_id=a, fact_id=1, confidence=0.8, ts=now - 120,
        quote="the pilot plan is $49 a month", source_class="private_conversation",
        speaker="David", speaker_is_source=False)
    kg_beliefs.record_from_claim(
        store, subj_type="entity", subj_id=subj, predicate="costs",
        obj_type="entity", obj_id=b, fact_id=2, confidence=0.8, ts=now - 60,
        quote="we moved the pilot plan to $55", source_class="private_conversation",
        speaker="David", speaker_is_source=False)
    pa = store.find_kg_predicate(subj_type="entity", subj_id=subj, predicate="costs",
                                 obj_type="entity", obj_id=a)
    pb = store.find_kg_predicate(subj_type="entity", subj_id=subj, predicate="costs",
                                 obj_type="entity", obj_id=b)
    return int(pa["id"]), int(pb["id"])


def _seed(store: Store) -> dict:
    now = time.time()
    e1 = store.insert(Event(time=now - 3600, modality=Modality.AUDIO,
                            raw="Dana said Atlas moves to December twelfth",
                            summary="Atlas date", source="audio.whisper", people=["Dana"]))
    f1 = store.add_claim("Atlas moves to December 12.", source_event_id=e1,
                         source_span="Atlas moves to December twelfth",
                         confidence=0.8, extracted_at=now - 3600)
    f2 = store.add_claim("Dana likes the 9:30 standup.", source_event_id=e1,
                         source_span="", confidence=0.6, extracted_at=now - 7200)
    store.review_fact(f2, "approved")
    eid = store.resolve_entity("Atlas", "project")
    alias = store.upsert_entity_alias(eid, "Atlas rollout", "atlas rollout",
                                      source="email", day="2026-11-05")
    return {"event": e1, "fact": f1, "approved": f2, "entity": eid,
            "alias": int(alias["id"])}


class InboxCollectTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ids = _seed(self.store)
        self._patches = _isolate()
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.store.close()
        inbox._undo.clear()

    def test_aggregate_shape_and_chip_contract(self) -> None:
        out = inbox.collect(self.store, worker=None)
        for key in ("needs_you", "can_wait", "handled_7d"):
            self.assertIn(key, out)
        self.assertNotIn("errors", out, out.get("errors"))
        self.assertEqual(out["needs_you"], [])           # nothing blocking
        kinds = {i["kind"] for i in out["can_wait"]}
        self.assertEqual(kinds, {"review", "confirm"})
        for item in out["can_wait"]:
            for key in ("id", "kind", "kind_label", "title", "entity", "evidence", "actions"):
                self.assertIn(key, item)
            self.assertTrue(item["actions"])
            self.assertEqual(sum(1 for a in item["actions"] if a["primary"]), 1)
            for ev in item["evidence"]:
                chip = ev["chip"]
                self.assertIn(chip["kind"], ("observed", "extracted", "confirmed", "peer", "review"))
                self.assertIn("source", chip)
                self.assertIn("event_id", chip)
        # Only the unreviewed fact is listed; the approved one is settled.
        review = [i for i in out["can_wait"] if i["kind"] == "review"]
        self.assertEqual([i["id"] for i in review], [f"review:{self.ids['fact']}"])
        self.assertEqual(review[0]["evidence"][0]["chip"]["kind"], "extracted")
        self.assertEqual(review[0]["evidence"][0]["chip"]["event_id"], self.ids["event"])
        self.assertIn("Atlas moves to December twelfth", review[0]["evidence"][0]["quote"])
        confirm = [i for i in out["can_wait"] if i["kind"] == "confirm"][0]
        self.assertEqual(confirm["entity"]["id"], self.ids["entity"])
        self.assertEqual([a["key"] for a in confirm["actions"]], ["yes", "no", "later"])
        self.assertTrue(confirm["actions"][2].get("local"))

    def test_user_vocabulary_only(self) -> None:
        out = inbox.collect(self.store, worker=None)
        text = " ".join(f"{i['kind_label']} {i['title']} {i['meta']} " +
                        " ".join(a["label"] for a in i["actions"])
                        for i in out["needs_you"] + out["can_wait"]).lower()
        for word in ("claim", "candidate", "predicate", "slot", "lane", " record", "kg"):
            self.assertNotIn(word, text, word)

    def test_collect_is_a_pure_read(self) -> None:
        before = self.store.memory_version()
        a = inbox.collect(self.store, worker=None)
        b = inbox.collect(self.store, worker=None)
        self.assertEqual(self.store.memory_version(), before)
        self.assertEqual([i["id"] for i in a["can_wait"]], [i["id"] for i in b["can_wait"]])

    def test_fact_chip_rules(self) -> None:
        self.assertEqual(inbox.fact_chip({"review": "approved"})["kind"], "confirmed")
        self.assertEqual(inbox.fact_chip({"source_modality": "peer:dana"})["kind"], "peer")
        self.assertEqual(inbox.fact_chip({"source_modality": "user"})["kind"], "observed")
        self.assertEqual(inbox.fact_chip({"source_modality": "audio"})["kind"], "extracted")

    def test_alias_yes_then_undo(self) -> None:
        aid = self.ids["alias"]
        out = inbox.act(self.store, f"alias:{aid}", "yes", worker=None)
        self.assertTrue(out["ok"], out)
        self.assertIn("undo_token", out)
        row = next(r for r in self.store.list_entity_aliases(entity_id=self.ids["entity"]) if r["id"] == aid)
        self.assertTrue(row["confirmed"])
        self.assertNotIn(f"alias:{aid}", [i["id"] for i in inbox.collect(self.store)["can_wait"]])
        back = inbox.undo(out["undo_token"])
        self.assertTrue(back["ok"], back)
        row = next(r for r in self.store.list_entity_aliases(entity_id=self.ids["entity"]) if r["id"] == aid)
        self.assertFalse(row["confirmed"])
        # A token is single-use.
        self.assertFalse(inbox.undo(out["undo_token"])["ok"])

    def test_alias_no_then_undo_restores_the_proposal(self) -> None:
        aid = self.ids["alias"]
        out = inbox.act(self.store, f"alias:{aid}", "no", worker=None)
        self.assertTrue(out["ok"], out)
        self.assertEqual(self.store.list_entity_aliases(confirmed=False), [])
        self.assertTrue(inbox.undo(out["undo_token"])["ok"])
        rows = self.store.list_entity_aliases(confirmed=False)
        self.assertEqual([r["alias"] for r in rows], ["Atlas rollout"])

    def test_undo_window_closes(self) -> None:
        out = inbox.act(self.store, f"alias:{self.ids['alias']}", "yes", worker=None)
        tok = out["undo_token"]
        exp, item, fn = inbox._undo[tok]
        inbox._undo[tok] = (time.time() - 1, item, fn)
        self.assertFalse(inbox.undo(tok)["ok"])

    def test_review_keep_goes_through_the_facts_verb_and_undoes(self) -> None:
        fid = self.ids["fact"]
        # The verdict trail (usage, distill label, learning pair) is the
        # routes' own; it is pinned here so the test never touches the
        # process-wide memory singleton (see test-suite notes on store leaks).
        with patch.object(inbox, "_fact_verdict_side_effects") as fx:
            out = inbox.act(self.store, f"review:{fid}", "keep", worker=None)
            fx.assert_called_once()
            self.assertEqual(fx.call_args.args[1:], ("accepted", "accepted"))
            self.assertTrue(out["ok"], out)
            self.assertEqual(self.store.get_fact(fid)["review"], "approved")
            self.assertTrue(inbox.undo(out["undo_token"])["ok"])
            self.assertIsNone(self.store.get_fact(fid)["review"])
            out = inbox.act(self.store, f"review:{fid}", "dismiss", worker=None)
            self.assertTrue(out["ok"], out)
            self.assertNotIn("undo_token", out)   # a dismiss is not cleanly reversible
            self.assertEqual(self.store.get_fact(fid)["review"], "dismissed")

    def test_unknown_queue_and_action(self) -> None:
        self.assertFalse(inbox.act(self.store, "nope:1", "x")["ok"])
        self.assertFalse(inbox.act(self.store, f"alias:{self.ids['alias']}", "shrug")["ok"])
        self.assertFalse(inbox.act(self.store, "offer:1", "accept", worker=None)["ok"])

    def test_handled_counts_supersessions(self) -> None:
        now = time.time()
        new = self.store.add_claim("Atlas moves to December 15.", source_event_id=self.ids["event"],
                                   source_span="", confidence=0.9, extracted_at=now)
        self.store.supersede_fact(self.ids["fact"], new, now)
        h = inbox.handled(self.store, since_s=86400, now=now + 1)
        self.assertGreaterEqual(h["count"], 1)
        self.assertIn("Facts updated on their own", [b["kind"] for b in h["breakdown"]])

    def test_dispute_rows_carry_both_quotes_and_resolve(self) -> None:
        pa, pb = _seed_dispute(self.store)
        out = inbox.collect(self.store, worker=None)
        decide = [i for i in out["needs_you"] if i["kind"] == "decide"]
        self.assertEqual(len(decide), 1, out.get("errors"))
        item = decide[0]
        self.assertIn("costs", item["title"])
        self.assertEqual([e["value"] for e in item["evidence"]], ["$49", "$55"])
        self.assertIn("$49 a month", item["evidence"][0]["quote"])
        self.assertIn("moved the pilot plan to $55", item["evidence"][1]["quote"])
        for e in item["evidence"]:
            self.assertEqual(e["chip"]["kind"], "extracted")
            self.assertTrue(e["chip"]["source"].startswith("heard"))
        look = [a for a in item["actions"] if a["key"] == "inspect"][0]
        self.assertEqual(look["href"], f"/inspect/belief/{min(pa, pb)}-{max(pa, pb)}")
        # Both are true clears the flags; undo puts them back.
        res = inbox.act(self.store, item["id"], "both_true", worker=None)
        self.assertTrue(res["ok"], res)
        self.assertFalse(self.store.get_kg_predicate(pa)["conflict"])
        self.assertEqual([i for i in inbox.collect(self.store)["needs_you"] if i["kind"] == "decide"], [])
        self.assertTrue(inbox.undo(res["undo_token"])["ok"])
        self.assertTrue(self.store.get_kg_predicate(pa)["conflict"])

    def test_rule_in_force_reads_settings(self) -> None:
        r = inbox.rule_in_force()
        self.assertIn(str(r["alias_days"]), r["text"])


class InboxRouteTests(unittest.TestCase):
    """The page and the JSON are the same payload; acting is one route."""

    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        from app.main import app
        # No lifespan on purpose: these routes need no worker, and starting
        # the app's background threads from a test that swaps stores is how
        # a closed temp store ends up under a live thread.
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()

    def setUp(self) -> None:
        tmp = Path(tempfile.mkdtemp())
        self.store = Store(db_path=tmp / "t.db", audio_dir=tmp / "audio")
        self.ids = _seed(self.store)
        # Patch the route's store seam, not the process-wide memory store: the
        # app's background threads keep using the real store, so closing the
        # temp store in tearDown cannot pull a live sqlite connection from
        # under them (that crashed a full suite run natively).
        self._patches = [patch("app.api.inbox_routes._store", return_value=self.store),
                         patch("app.api.inbox_routes._worker", return_value=None),
                         patch("app.api.ui._inbox_count",
                               side_effect=lambda: inbox.needs_you_count(self.store, worker=None))] + _isolate()
        for p in self._patches:
            p.start()
        inbox._count_cache["at"] = 0.0

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.store.close()
        inbox._undo.clear()

    def _headers(self) -> dict:
        tok = self.client.cookies.get("quill_csrf") or ""
        return {"Origin": "http://testserver", "X-CSRF-Token": tok}

    def test_json_and_html_from_one_payload(self) -> None:
        j = self.client.get("/inbox").json()
        self.assertEqual(j["needs_you"], [])
        self.assertEqual({i["kind"] for i in j["can_wait"]}, {"review", "confirm"})
        r = self.client.get("/inbox", headers={"Accept": "text/html"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-store", r.headers.get("cache-control", ""))
        html = r.text
        self.assertIn('id="sparrowRail"', html)
        self.assertIn('data-nav="inbox" class="on"', html)
        self.assertIn("Needs you", html)
        self.assertIn("Can wait", html)
        for item in j["can_wait"]:
            self.assertIn(f'data-id="{item["id"]}"', html)
        self.assertIn("Handled without asking", html)
        self.assertIn("Why these ask", html)
        # The chip is the server's: a hard-coded chip kind never appears in a template.
        for name in ("inbox.html", "partials/inbox_queue.html"):
            src = (Path(__file__).resolve().parents[1] / "app/templates" / name).read_text()
            self.assertNotIn("chip-observed", src)
            self.assertNotIn("chip-extracted", src)

    def test_count_and_partials(self) -> None:
        c = self.client.get("/inbox/count").json()
        self.assertEqual(c, {"needs_you": 0, "can_wait": 2})
        r = self.client.get("/inbox/partials/queue")
        self.assertEqual(r.status_code, 200)
        self.assertIn('data-inbox-group="can_wait"', r.text)
        self.assertEqual(self.client.get("/inbox/partials/nope").status_code, 404)
        h = self.client.get("/inbox/handled?since=7d").json()
        self.assertIn("breakdown", h)

    def test_act_and_undo_over_http(self) -> None:
        aid = self.ids["alias"]
        self.client.get("/inbox")          # obtain the CSRF cookie
        r = self.client.post(f"/inbox/alias:{aid}/yes", headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertIn("undo_token", body)
        r = self.client.post(f"/inbox/undo/{body['undo_token']}", headers=self._headers())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        r = self.client.post("/inbox/undo/stale", headers=self._headers())
        self.assertEqual(r.status_code, 410)
        r = self.client.post(f"/inbox/alias:{aid}/shrug", headers=self._headers())
        self.assertEqual(r.status_code, 409)

    def test_form_fallback_redirects_to_inbox(self) -> None:
        aid = self.ids["alias"]
        self.client.get("/inbox")
        r = self.client.post(f"/inbox/alias:{aid}/act", data={"action": "yes"},
                             headers=self._headers(), follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertEqual(r.headers["location"], "/inbox")

    def test_look_closer_is_a_page_not_json(self) -> None:
        pa, pb = _seed_dispute(self.store)
        with patch("app.api.shell_routes._store", return_value=self.store):
            r = self.client.get(f"/inspect/belief/{pa}-{pb}", headers={"Accept": "text/html"})
            self.assertEqual(r.status_code, 200, r.text[:300])
            self.assertIn("$49 a month", r.text)
            self.assertIn("moved the pilot plan to $55", r.text)
            self.assertIn("Both are true", r.text)
            self.assertIn('id="sparrowRail"', r.text)
            self.assertNotIn("predicate_id", r.text)       # internal names stay out of the copy
            self.assertEqual(self.client.get("/inspect/belief/999999").status_code, 404)
            self.assertEqual(self.client.get("/inspect/belief/x").status_code, 404)

    def test_the_rail_pill_counts_needs_you_only(self) -> None:
        r = self.client.get("/today", headers={"Accept": "text/html"})
        self.assertIn('data-count="inbox" hidden>0<', r.text)


if __name__ == "__main__":
    unittest.main()
