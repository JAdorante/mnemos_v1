"""Connector capture & task fulfillment spec — Feature 3: open slots.

Need normalisation, the matcher's precision on a 50-event fixture with 5
planted fills (no false fill above threshold), the Not-it memory, delivery
to the user with the fill as evidence, pre-approved delivery with undo,
review_after on the horizon strip, sibling resolution, and Option B's
fetch-to-DOCUMENT landing.
"""
from __future__ import annotations

import os
import random
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))


class _Overlap(unittest.TestCase):
    """Deterministic similarity (no embedder load); restored after each test."""

    def setUp(self):
        self._prev_sim = os.environ.get("QUILL_SLOT_SIM")
        os.environ["QUILL_SLOT_SIM"] = "overlap"

    def tearDown(self):
        if self._prev_sim is None:
            os.environ.pop("QUILL_SLOT_SIM", None)
        else:
            os.environ["QUILL_SLOT_SIM"] = self._prev_sim

from app.events import Event, Modality  # noqa: E402
from app.services import slots  # noqa: E402

NOW = 1_726_600_000.0


def _mk(td: str):
    from app.storage import Store
    return Store(Path(td) / "t.db")


class NeedTests(_Overlap):
    def test_normalize_need(self):
        cases = {
            "What's the status of the Boston quote? Ask User 2.": "Boston quote",
            "get me the Boston deal quote": "Boston deal quote",
            "keep an eye out for the Acme invoice please": "Acme invoice",
            "ask #platform: what is the status of the Boston deal?": "Boston deal",
            "Boston deal quote": "Boston deal quote",
        }
        for raw, want in cases.items():
            self.assertEqual(slots.normalize_need(raw), want, raw)

    def test_need_tokens_and_coverage(self):
        self.assertEqual(slots.need_tokens("the Boston deal quote"),
                         {"boston", "deal", "quote"})
        self.assertEqual(slots.coverage("Boston deal quote",
                                        "Quotes for the Boston deal attached"), 1.0)
        self.assertAlmostEqual(slots.coverage("Boston deal quote",
                                              "Boston weather is nice"), 1 / 3)

    def test_resolve_entities_binds_exact_only(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                store.resolve_entity("Acme Corp", "org", ts=NOW)
                ents = slots.resolve_entities("Acme Corp invoice", store=store)
                self.assertEqual(ents, ["Acme Corp"])
                self.assertEqual(store.all_entities().__len__(), 1)  # never mints
            finally:
                store.close()


def _fixture_events() -> tuple[list[Event], set[int]]:
    """50 events, 5 planted fills for 'Boston deal quote'. Distractors share
    single tokens (Boston weather, a quote about deals in general, a Boston
    calendar block) so the threshold, not luck, keeps them out."""
    rng = random.Random(7)
    planted = {
        3: "Quote Q-1042 for the Boston deal attached: $42,000, valid to Oct 15.",
        11: "Re: Boston deal — here is the quote you asked for, revised pricing.",
        24: "Boston deal quote (final) — see PDF, Dana signed off.",
        37: "Attached the quote for the Boston deal, per Marc's request.",
        48: "FW: Boston deal quote v3",
    }
    distractors = [
        "Boston weather looks great this weekend, bring a jacket.",
        "Quote of the day: deals are made in the follow-up.",
        "Calendar: Boston office all-hands, 3 pm.",
        "Invoice #221 for the Denver deal, net 30.",
        "Lunch with Priya moved to Thursday.",
        "The Acme quote for Austin is still pending legal review.",
        "Deal desk: new discount policy memo.",
        "Team offsite in Boston next quarter?",
        "Marc: can you send the deck for the Chicago deal?",
        "Quote request form updated on the website.",
        "Sprint retro notes — nothing about pricing.",
        "Your parking permit renewal is due.",
    ]
    events = []
    for i in range(50):
        text = planted.get(i) or rng.choice(distractors) + f" (#{i})"
        src = rng.choice(["google.mail", "chat.user", "documents.file",
                          "desktop.screen", "peer.answer"])
        events.append(Event(time=NOW + i, modality=Modality.DOCUMENT
                            if src == "documents.file" else Modality.TEXT,
                            raw=text, summary=text[:80], source=src,
                            meta={"title": text[:40]}))
    return events, set(planted)


class MatcherTests(_Overlap):
    def test_precision_on_fixture(self):
        events, planted = _fixture_events()
        slot = {"need": "Boston deal quote", "entities": ["Boston"],
                "match_threshold": slots.DEFAULT_THRESHOLD}
        hits = set()
        for i, ev in enumerate(events):
            score, _ = slots.score_event(slot, ev)
            if score >= slot["match_threshold"]:
                hits.add(i)
        self.assertEqual(hits, planted)   # all 5 found, zero false fills

    def test_watcher_offers_each_planted_fill_once(self):
        events, planted = _fixture_events()
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            offered = []
            try:
                sid = slots.create(store, "get me the Boston deal quote",
                                   requester={"kind": "user", "id": None}, now=NOW)
                self.assertTrue(sid)
                self.assertEqual(store.get_task(sid)["status"], "awaiting_data")
                with mock.patch.object(slots, "offer_fill",
                                       side_effect=lambda *a, **k:
                                       offered.append(a[2]) or True):
                    ids = {}
                    for i, ev in enumerate(events):
                        eid = store.insert(ev)
                        ids[eid] = i
                        slots.evaluate_event(store, eid, ev, now=NOW + i)
                    # Re-landing the same events never re-offers (Not-it memory).
                    for eid, i in ids.items():
                        slots.evaluate_event(store, eid, events[i], now=NOW + 100)
                self.assertEqual({ids[e] for e in offered}, planted)
                self.assertEqual(len(offered), len(planted))
            finally:
                store.close()

    def test_eligibility_rules(self):
        self.assertFalse(slots.eligible(Event(time=NOW, modality=Modality.AUDIO,
                                              raw="x", source="audio.whisper",
                                              confidence=0.3)))
        self.assertTrue(slots.eligible(Event(time=NOW, modality=Modality.AUDIO,
                                             raw="x", source="audio.whisper",
                                             confidence=0.9)))
        self.assertFalse(slots.eligible(Event(time=NOW, modality=Modality.SYSTEM,
                                              raw="x", source="peer.ask")))
        self.assertFalse(slots.eligible(Event(time=NOW, modality=Modality.TEXT,
                                              raw="x", source="chat.user"),
                                        slot={"created_from": 5}, event_id=5))


class FillTests(_Overlap):
    def setUp(self):
        super().setUp()
        self._td = tempfile.TemporaryDirectory()
        self.store = _mk(self._td.name)
        self.notices = []
        self._p = mock.patch.object(slots, "_notify",
                                    side_effect=lambda text, stream=None:
                                    self.notices.append((text, stream)))
        self._p.start()
        self._p2 = mock.patch.object(slots, "offer_fill", return_value=True)
        self._p2.start()

    def tearDown(self):
        self._p.stop(); self._p2.stop()
        self.store.close(); self._td.cleanup()
        super().tearDown()

    def _slot(self, **kw) -> int:
        return slots.create(self.store, "Boston deal quote",
                            requester={"kind": "user", "id": None}, now=NOW, **kw)

    def _fill_event(self) -> int:
        return self.store.insert(Event(
            time=NOW + 10, modality=Modality.DOCUMENT,
            raw="Quote Q-1042 for the Boston deal: $42,000.", source="documents.file",
            meta={"title": "Boston deal quote"}))

    def test_not_it_keeps_slot_open_and_never_reoffers(self):
        sid = self._slot()
        eid = self._fill_event()
        ev = self.store.get_event(eid)
        self.assertTrue(slots.evaluate_event(self.store, eid, ev, now=NOW + 10))
        out = slots.reject_fill(self.store, sid, eid)
        self.assertEqual(out["verdict"], "rejected")
        self.assertEqual(self.store.get_task(sid)["status"], "awaiting_data")
        cand = self.store.slot_candidate(sid, eid)
        self.assertEqual(cand["verdict"], "rejected")
        self.assertLess(cand["score"], 0.6)
        self.assertEqual(slots.evaluate_event(self.store, eid, ev, now=NOW + 20), [])

    def test_deliver_to_user_closes_with_evidence(self):
        sid = self._slot()
        eid = self._fill_event()
        slots.evaluate_event(self.store, eid, self.store.get_event(eid), now=NOW + 10)
        out = slots.deliver(self.store, sid, eid, now=NOW + 11)
        self.assertTrue(out["ok"])
        self.assertEqual(out["status"], "delivered")
        row = self.store.get_task(sid)
        self.assertEqual(row["status"], "done")
        tx = self.store.last_transition(sid)
        self.assertEqual(tx["evidence_id"], eid)
        self.assertEqual(self.store.slot_candidate(sid, eid)["verdict"], "delivered")
        text, stream = self.notices[-1]
        self.assertIn(f"/memory?event={eid}", text)      # provenance link
        self.assertEqual(stream["type"], "task.completed")
        # A second fill within the minute: logged, never delivered.
        eid2 = self._fill_event()
        out2 = slots.deliver(self.store, sid, eid2, now=NOW + 30)
        self.assertFalse(out2["ok"])
        self.assertTrue(out2["superseded"])
        self.assertEqual(self.store.slot_candidate(sid, eid2)["verdict"], "superseded")

    def test_pre_approved_delivery_has_an_undo_window(self):
        sid = self._slot(deliver_on_fill=True)
        eid = self._fill_event()
        self._p2.stop()   # use the real offer_fill for this one
        try:
            with mock.patch.object(slots, "UNDO_S", 30.0), \
                    mock.patch("app.services.agent_bridge.worker") as w:
                w.propose_slot_undo.return_value = True
                slots.evaluate_event(self.store, eid, self.store.get_event(eid),
                                     now=NOW + 10)
                self.assertEqual(slots.pending_auto(), [sid])
                self.assertTrue(slots.undo(sid))
                self.assertEqual(slots.pending_auto(), [])
            self.assertEqual(self.store.get_task(sid)["status"], "awaiting_data")
            self.assertTrue(any("undo" in t.lower() for t, _ in self.notices))
            # delay 0 → immediate (auto-approved, never silent: a notice lands).
            slots.schedule_auto_delivery(self.store, sid, eid, delay_s=0, now=NOW + 12)
            self.assertEqual(self.store.get_task(sid)["status"], "done")
        finally:
            self._p2.start()

    def test_review_after_puts_stale_slot_on_horizon_keep_drop(self):
        sid = self._slot()
        self.assertEqual(slots.stale(self.store, now=NOW + 86400), [])
        late = NOW + slots.REVIEW_AFTER_S + 1
        items = slots.horizon_items(self.store, now=late)
        self.assertEqual(items[0]["kind"], "slot_review")
        self.assertEqual([a["reply"] for a in items[0]["actions"]], ["keep", "drop"])
        slots.keep(self.store, sid, now=late)
        self.assertEqual(slots.horizon_items(self.store, now=late), [])
        slots.drop(self.store, sid)
        self.assertEqual(self.store.get_task(sid)["status"], "declined")
        self.assertEqual(slots.open_slots(self.store), [])

    def test_resolve_siblings_closes_by_origin(self):
        a = slots.create(self.store, "Boston deal quote",
                         requester={"kind": "peer", "id": "p1", "name": "User 1"},
                         origin_id="o-1", now=NOW)
        b = slots.create(self.store, "Boston deal quote",
                         requester={"kind": "peer", "id": "p1", "name": "User 1"},
                         origin_id="o-1", now=NOW)
        c = slots.create(self.store, "Denver invoice",
                         requester={"kind": "user", "id": None}, now=NOW)
        closed = slots.resolve_siblings(self.store, "o-1", except_fact_id=a,
                                        reason="filled elsewhere")
        self.assertEqual(closed, [b])
        self.assertEqual(self.store.get_task(b)["status"], "cancelled")
        self.assertEqual(self.store.get_task(a)["status"], "awaiting_data")
        self.assertEqual(self.store.get_task(c)["status"], "awaiting_data")

    def test_declined_thread_blocks_slot_creation(self):
        eid = self.store.insert(Event(time=NOW, modality=Modality.SYSTEM, raw="x",
                                      source="google.mail", meta={"thread_id": "T1"}))
        fid = self.store.add_commitment("Find: Boston deal quote", extracted_at=NOW,
                                        source_event_id=eid)
        self.store.transition_commitment(fid, "declined", actor="user")
        self.assertEqual(slots.create(self.store, "Boston deal quote",
                                      created_from=eid, now=NOW), 0)


class OptionBTests(_Overlap):
    def test_fetch_goal_is_read_only_and_lands_as_document(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                sid = slots.create(store, "Boston deal quote",
                                   requester={"kind": "peer", "id": "p1",
                                              "name": "User 1"}, now=NOW)
                sent = {}
                with mock.patch("app.services.agent_bridge.worker") as w:
                    w.send.side_effect = lambda goal, **kw: sent.update(
                        goal=goal, **kw)
                    res = slots.navigate_to_source(store, sid, app_hint="salesforce")
                self.assertTrue(res["ok"])
                self.assertEqual(sent["fetch"]["goal"], "fetch")
                self.assertEqual(sent["fetch"]["app_hint"], "salesforce")
                self.assertEqual(sent["fetch"]["slot_id"], sid)
                self.assertIn("Read only", sent["goal"])
                self.assertIn("Salesforce", sent["goal"])
                # The agent's read-back lands as agent.fetch and fills the slot.
                with mock.patch.object(slots, "offer_fill", return_value=True) as off, \
                        mock.patch("app.services.model_log.model_log.log_egress") as eg:
                    from app.services import task_completion as tc
                    with mock.patch.dict(os.environ,
                                         {"QUILL_TASK_COMPLETION_SYNC": "1"}):
                        tc.attach()
                        try:
                            eid = slots.land_fetch_result(
                                sent["fetch"],
                                "Quote Q-1042 for the Boston deal: $42,000 "
                                "valid until Oct 15 (Salesforce record).", "done",
                                store=store, now=NOW + 5)
                        finally:
                            tc.detach()
                self.assertTrue(eid)
                ev = store.get_event(eid)
                self.assertEqual(ev["source"], "agent.fetch")
                self.assertEqual(ev["modality"], "document")
                import json
                meta = json.loads(ev["meta"])
                self.assertTrue(meta["never_authorizes"])
                self.assertEqual(meta["slot_id"], sid)
                self.assertTrue(off.called)
                self.assertEqual(eg.call_args.kwargs["kind"], "agent_fetch")
                self.assertEqual(eg.call_args.kwargs["approving_action"], "approval_gate")
                # Refused / empty results never become memory.
                self.assertIsNone(slots.land_fetch_result(sent["fetch"], "", "done",
                                                          store=store))
                self.assertIsNone(slots.land_fetch_result(sent["fetch"], "x" * 50,
                                                          "blocked", store=store))
            finally:
                store.close()


class SearchAssistTests(_Overlap):
    def test_memory_hit_short_circuits_and_web_is_never_used(self):
        with tempfile.TemporaryDirectory() as td:
            store = _mk(td)
            try:
                eid = store.insert(Event(time=NOW, modality=Modality.TEXT,
                                         raw="Boston deal quote is $42k",
                                         source="chat.user"))
                hit = {"text": "Boston deal quote is $42k", "score": 0.2,
                       "source": "chat.user", "time": NOW}
                with mock.patch("app.services.memory.memory.search",
                                return_value=[hit]):
                    hits = slots.search_assist(store, "the Boston deal quote",
                                               allow_connectors=False)
                self.assertEqual(hits[0]["event_id"], eid)
                self.assertGreaterEqual(hits[0]["score"], 0.78)
                with mock.patch("app.services.memory.memory.search",
                                return_value=[]):
                    self.assertEqual(slots.search_assist(
                        store, "Boston deal quote", allow_connectors=False), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
