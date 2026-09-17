"""Connector capture & task fulfillment spec — the three "Boston quote"
scenarios, end to end, on three in-process Sparrow instances.

Each instance is its own Store plus its own peer registry / ask / sent /
team files (the peer channel resolves those paths from the environment at
call time), and its own fake chat worker that records the offers and
notices it would have shown. HTTP between instances is replaced by a
router that dispatches /peer/* to the target instance's handler under
that instance's context — the same shape as the existing peer-channel
test harness, extended to three tenants and the slot verbs.

Scenario 1: the data is one browser hop away (Option B).
Scenario 2: a different peer has it (#Team fan-out, merged, no slot).
Scenario 3: the data does not exist yet (slots on every member; the quote
arrives hours later on User 3 by connector sync; first fill delivers and
closes the sibling).
Plus the four negative cases and the fan-out property.
"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))

# Set per test (Harness.start), not at import: other modules' teardowns pop
# these keys, and a full-suite run interleaves modules. Each is restored.
_ENV_FLAGS = {
    "QUILL_SLOT_SIM": "overlap",              # no embedder load
    "QUILL_TEAM_FANOUT_SEQUENTIAL": "1",      # the context switch is process-global
    "QUILL_TEAM_FANOUT_DEADLINE_S": "0.2",
    "QUILL_TASK_COMPLETION_SYNC": "1",        # the insert hook runs inline
    "QUILL_PEER_INGEST": "0",                 # answers stay bus context
}

from app import storage as _storage  # noqa: E402
from app.services import peer_channel as pch  # noqa: E402
from app.services import slots  # noqa: E402
from app.services import task_completion as tc  # noqa: E402
from app.services import team_layer as tl  # noqa: E402
from app.services.connectors import scheduler  # noqa: E402
from app.storage import Store  # noqa: E402

_ENV_KEYS = {
    "QUILL_PEER_REGISTRY": "peers.json", "QUILL_PEER_ASKS": "asks.json",
    "QUILL_PEER_SENT": "sent.json", "QUILL_PEER_MAILBOX": "mailbox.json",
    "QUILL_PEER_TEAMS": "teams.json", "QUILL_PEER_LOOPS": "loops.json",
    "QUILL_PEER_TELEMETRY_PATH": "telemetry.jsonl",
    "QUILL_PEER_CLIP_GRANTS": "clip_grants.json",
}

QUOTE = ("Quote Q-1042 for the Boston deal: $42,000, valid until Oct 15. "
         "Contact Dana Whitfield, Acme.")


class FakeWorker:
    """Records what the chat surface would have shown."""

    def __init__(self):
        self.offers: list[dict] = []
        self.emits: list[tuple[str, str, dict | None]] = []
        self.sent: list[dict] = []
        self.lock = threading.RLock()

    def _emit(self, kind, text, **kw):
        self.emits.append((kind, text, kw.get("stream")))

    def send(self, text, **kw):
        self.sent.append({"goal": text, **kw})

    def expire_stale_offers(self):
        return 0

    def pending_offer(self):
        return None

    def __getattr__(self, name):
        if name.startswith("propose_"):
            def _p(cand=None, *a, **k):
                self.offers.append({"kind": name[len("propose_"):],
                                    **(cand if isinstance(cand, dict) else
                                       {"args": a, "kw": k})})
                return True
            return _p
        raise AttributeError(name)

    def offers_of(self, kind):
        return [o for o in self.offers if o["kind"] == kind]

    def texts(self, kind=None):
        return [t for k, t, _ in self.emits if kind is None or k == kind]


class Instance:
    def __init__(self, name: str, root: Path):
        self.name = name
        self.dir = root / name.replace(" ", "_")
        self.dir.mkdir()
        self.url = f"http://{name.lower().replace(' ', '')}.test"
        self.store = Store(self.dir / "quill.db")
        self.worker = FakeWorker()
        self.env = {k: str(self.dir / v) for k, v in _ENV_KEYS.items()}
        self.answer: dict | None = None   # what compose_answer returns here

    def close(self):
        self.store.close()


class WorkerProxy:
    def __init__(self, harness):
        self._h = harness

    def __getattr__(self, name):
        return getattr(self._h.current.worker, name)


class Harness:
    def __init__(self, names: list[str]):
        self.root = Path(tempfile.mkdtemp(prefix="boston_"))
        self.instances = {n: Instance(n, self.root) for n in names}
        self.by_url = {i.url: i for i in self.instances.values()}
        self.current: Instance | None = None
        self.lock = threading.RLock()
        self._patches = [
            mock.patch.object(pch, "_post_json", self.post_json),
            mock.patch.object(pch, "instance_name", lambda: self.current.name),
            mock.patch.object(pch, "my_base_url", lambda: self.current.url),
            mock.patch.object(pch, "my_internal_url", lambda: ""),
            mock.patch.object(pch, "enrich_peer_question", lambda q: q),
            mock.patch.object(pch, "classify_question", lambda q: "work"),
            mock.patch.object(pch, "compose_answer", self.compose_answer),
            mock.patch("app.services.agent_bridge.worker", WorkerProxy(self)),
            mock.patch("app.services.salience.watch_phrases", lambda: []),
        ]

    def start(self):
        self._prev_flags = {k: os.environ.get(k) for k in _ENV_FLAGS}
        os.environ.update(_ENV_FLAGS)
        # Process-wide singletons that cache a Store on first use: pin them
        # so nothing in this run can leave one bound to a temp store that is
        # about to be closed (the job worker did exactly that once).
        from app.services.memory import memory
        from app.services.worker import worker as job_worker
        self._prev_singletons = (memory._store, job_worker._store)
        for p in self._patches:
            p.start()
        tc.attach()

    def stop(self):
        tc.detach()
        for p in self._patches:
            p.stop()
        from app.services.memory import memory
        from app.services.worker import worker as job_worker
        memory._store, job_worker._store = self._prev_singletons
        for i in self.instances.values():
            i.close()
        for k, v in self._prev_flags.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    @contextlib.contextmanager
    def use(self, inst: Instance):
        with self.lock:
            prev_env = {k: os.environ.get(k) for k in _ENV_KEYS}
            prev_store = _storage._store
            prev_cur = self.current
            # The in-flight pairing code is module state; keep it per instance.
            prev_pairing = pch._pairing
            if prev_cur is not None and prev_cur is not inst:
                prev_cur.pairing = prev_pairing
            for k, v in inst.env.items():
                os.environ[k] = v
            _storage._store = inst.store
            self.current = inst
            if prev_cur is not inst:
                pch._pairing = getattr(inst, "pairing", None)
            try:
                yield inst
            finally:
                if prev_cur is not inst:
                    inst.pairing = pch._pairing
                    pch._pairing = prev_pairing
                for k, v in prev_env.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
                _storage._store = prev_store
                self.current = prev_cur

    # --- fake transport ---------------------------------------------------
    def post_json(self, url, payload, token=None, timeout=None):
        base, path = url.split("/peer/", 1)
        path = "/peer/" + path
        target = self.by_url[base]
        with self.use(target):
            if path == "/peer/pair/claim":
                return pch.claim_pairing(payload["code"], payload["name"],
                                         payload["base_url"],
                                         payload["token_for_caller"],
                                         payload.get("internal_url") or "",
                                         pack="pilot")
            peer = pch.authenticate(f"Bearer {token}")
            assert peer is not None, f"unauthenticated call to {target.name}"
            if path == "/peer/ask":
                return pch.handle_ask(peer, payload)
            if path == "/peer/answer":
                return pch.handle_answer(peer, payload)
            if path == "/peer/update":
                return pch.handle_update(peer, payload)
            if path == "/peer/slot-resolved":
                return pch.handle_slot_resolved(peer, payload)
            if path == "/peer/ping":
                return tl.handle_ping(peer, payload)
            raise AssertionError(f"unrouted {path}")

    def compose_answer(self, question, *, question_class=None):
        ans = self.current.answer
        if ans:
            return {"text": ans["text"], "claims": ans["claims"], "as_of": 1.0,
                    "near_miss": False, "redacted": []}
        return {"text": "I don't have anything in my memory on that.",
                "claims": [], "as_of": None, "near_miss": False, "redacted": []}

    # --- pairing / teams ----------------------------------------------------
    def pair(self, a: Instance, b: Instance) -> tuple[str, str]:
        """Pair a↔b; returns (a's peer_id for b, b's peer_id for a)."""
        with self.use(a):
            code = pch.start_pairing()["code"]
        with self.use(b):
            res = pch.join(a.url, code, pack="pilot")
            assert res["ok"], res
            b_for_a = next(p["peer_id"] for p in pch.peers() if p["name"] == a.name)
        with self.use(a):
            a_for_b = next(p["peer_id"] for p in pch.peers() if p["name"] == b.name)
        return a_for_b, b_for_a

    def peer_id(self, on: Instance, of: Instance) -> str:
        with self.use(on):
            return next(p["peer_id"] for p in pch.peers() if p["name"] == of.name)


class StubMail:
    id = "stubmail"
    label = "Stub Mail"
    tool_names = ("Stub Mail",)
    availability = "ready"
    kind = "directory"
    category = "calendar"
    source_prefixes = ("stubmail.mail",)
    sync_interval_s = 60

    def __init__(self, items):
        self.items = items

    def configured(self): return True
    def connected(self): return True
    def status(self): return {"id": self.id}
    def begin_connect(self, **kw): return {"ok": True}
    def complete_connect(self, code, state, *, redirect_uri): return {"ok": True}
    def sync(self): return {"ok": True}
    def disconnect(self): return {"ok": True}

    def fetch_items(self, *, cursor=None, now=None):
        return list(self.items), {**(cursor or {}), "since": now}


class BostonBase(unittest.TestCase):
    def setUp(self):
        self.h = Harness(["User 1", "User 2", "User 3"])
        self.h.start()
        self.u1 = self.h.instances["User 1"]
        self.u2 = self.h.instances["User 2"]
        self.u3 = self.h.instances["User 3"]
        self.h.pair(self.u1, self.u2)
        self.h.pair(self.u1, self.u3)
        self.h.pair(self.u2, self.u3)
        # User 1's team = Users 2 and 3; everyone's team registry knows the others.
        for inst in (self.u1, self.u2, self.u3):
            with self.h.use(inst):
                others = [self.h.peer_id(inst, o) for o in (self.u1, self.u2, self.u3)
                          if o is not inst]
                tl.upsert_team("Team", others)
        from app.services import model_log
        self._ml = mock.patch.object(model_log.model_log, "_path",
                                     self.h.root / "model_calls.jsonl")
        self._ml.start()

    def tearDown(self):
        self._ml.stop()
        self.h.stop()

    # helpers
    def ask_u2(self, text="What's the status of the Boston quote? Ask User 2."):
        with self.h.use(self.u1):
            pid = self.h.peer_id(self.u1, self.u2)
            return pch.ask(pid, slots.normalize_need(text) and
                           "What's the status of the Boston quote?")

    def waiting_rows(self, inst, statuses=("open",)):
        with self.h.use(inst):
            return [t for t in inst.store.list_tasks(statuses)
                    if t.get("task_kind") == "peer_ask"]


class Scenario1Tests(BostonBase):
    """The data is one browser hop away."""

    def test_scenario_1(self):
        # 1. User 1 asks; hop=0; board shows waiting on User 2.
        res = self.ask_u2()
        self.assertEqual(res["status"], "null")
        self.assertEqual(res["null_result"], {"reason": "no_memory", "slot_offered": True})
        with self.h.use(self.u1):
            sent = pch.answers(res["ask_id"])[0]
        self.assertEqual((sent["hop"], sent["waiting_on"]), (0, "User 2"))
        rows = self.waiting_rows(self.u1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["counterparty_name"], "User 2")
        self.assertIn("Waiting on User 2", rows[0]["text"])
        # 2. User 2's Sparrow found nothing: typed null + the four options.
        offers = self.u2.worker.offers_of("peer_null_options")
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0]["options"], ["search", "source", "team", "slot"])
        self.assertEqual(offers[0]["need"], "Boston quote")
        # 3. User 2 taps Option B → a fetch goal, read-only, under the gate.
        with self.h.use(self.u2):
            out = pch.resolve_null_option(offers[0], "source")
        self.assertTrue(out["ok"])
        self.assertEqual(len(self.u2.worker.sent), 1)
        fetch = self.u2.worker.sent[0]["fetch"]
        self.assertEqual(fetch["goal"], "fetch")
        self.assertEqual(fetch["app_hint"], "salesforce")
        self.assertIn("Read only", self.u2.worker.sent[0]["goal"])
        sid = out["slot_id"]
        with self.h.use(self.u2):
            self.assertEqual(self.u2.store.get_task(sid)["status"], "awaiting_data")
        # 4/5. User 2 approves; the agent's read-back lands as agent.fetch and
        #      the slot fill offer appears: Deliver to User 1 / Not it.
        with self.h.use(self.u2):
            eid = slots.land_fetch_result(fetch, QUOTE, "done", store=self.u2.store)
            ev = self.u2.store.get_event(eid)
        self.assertEqual(ev["source"], "agent.fetch")
        self.assertEqual(ev["modality"], "document")
        fills = self.u2.worker.offers_of("slot_fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fact_id"], sid)
        self.assertEqual(fills[0]["event_id"], eid)
        self.assertEqual(fills[0]["requester"]["name"], "User 1")
        self.assertIn("send it?", fills[0]["message"])
        # 6. User 2 taps Deliver: redacted answer reaches User 1; slot done
        #    with evidence_id; egress view shows the send.
        with self.h.use(self.u2):
            out = slots.deliver(self.u2.store, sid, eid)
            self.assertEqual(out["status"], "delivered")
            row = self.u2.store.get_task(sid)
            self.assertEqual(row["status"], "done")
            self.assertEqual(self.u2.store.last_transition(sid)["evidence_id"], eid)
        with self.h.use(self.u1):
            got = pch.answers(res["ask_id"])[0]
            self.assertEqual((got["status"], got["response_kind"]), ("answered", "fill"))
            self.assertIn("Q-1042", got["answer"])
            self.assertEqual(self.waiting_rows(self.u1), [])
            done = self.waiting_rows(self.u1, ("done",))
            self.assertEqual(len(done), 1)
        self.assertTrue(any("found the Boston quote" in t
                            for t in self.u1.worker.texts("result")))
        from app.services.model_log import model_log
        inv = model_log.egress_inventory(recent=10)
        peer_rows = [r for r in inv["recent"] if r["provider"] == "peer"]
        self.assertEqual(len(peer_rows), 1)
        self.assertEqual(peer_rows[0]["destination"], "User 1")
        self.assertEqual(peer_rows[0]["approving_action"], "user_tap")
        # Audit: every ask / null / delivery wrote to agent_log with peer ids.
        with self.h.use(self.u2):
            goals = [r["goal"] for r in self.u2.store._conn.execute(
                "SELECT goal FROM agent_runs").fetchall()]
        self.assertTrue(any(g.startswith("peer:ask_received") for g in goals))
        self.assertTrue(any(g.startswith("peer:null_result") for g in goals))
        self.assertTrue(any(g.startswith("peer:fill_delivered") for g in goals))


class Scenario2Tests(BostonBase):
    """A different peer has it."""

    def test_scenario_2(self):
        res = self.ask_u2()
        self.assertEqual(res["status"], "null")
        offers = self.u2.worker.offers_of("peer_null_options")
        # 3. User 2 taps Option A: local memory + connector lookup → null in 10 s.
        t0 = time.time()
        with self.h.use(self.u2), \
                mock.patch("app.services.memory.memory.search", return_value=[]):
            out = pch.resolve_null_option(offers[0], "search")
        self.assertLess(time.time() - t0, 10)
        self.assertEqual(out["hits"], 0)
        # 4. The offer now shows Option C and Leave slot open.
        self.assertEqual(self.u2.worker.offers_of("peer_null_options")[-1]["options"],
                         ["team", "slot"])
        # 5. User 1 types "#Team …": fan-out to Users 2 and 3, 30 s deadline.
        self.u3.answer = {"text": "Quote Q-1042 for the Boston deal is $42k, sent Tuesday.",
                          "claims": [{"text": "Quote Q-1042 is $42k", "fact_id": 1,
                                      "event_id": 1, "ts": 1.0}]}
        with self.h.use(self.u1):
            g = tl.parse_group_ask("#Team what is the status of the Boston deal?")
            self.assertTrue(g and g["fanout"] and not g["unknown"])
            self.assertEqual(len(g["peer_ids"]), 2)
            fan = tl.fanout_ask(g["team_slug"], g["question"])
            self.assertEqual(fan["asked"], 2)
            merged = tl.merge_fanout(fan["team_ask_id"])
        # 6/7. User 3 answered under its policy; merged with attribution; no slot.
        self.assertEqual(merged["nulls"], ["User 2"])
        self.assertEqual(merged["answered"], ["User 3"])
        self.assertFalse(merged["all_null"])
        self.assertIn("- User 2: nothing", merged["text"])
        self.assertIn("- User 3: Quote Q-1042", merged["text"])
        for inst in (self.u2, self.u3):
            self.assertEqual(inst.worker.offers_of("slot_create"), [])
            with self.h.use(inst):
                self.assertEqual([s for s in slots.open_slots(inst.store)
                                  if (s["slot"] or {}).get("origin_id") == fan["origin_id"]],
                                 [])


class Scenario3Tests(BostonBase):
    """The data does not exist yet."""

    def _all_null_fanout(self):
        with self.h.use(self.u1):
            g = tl.parse_group_ask("#Team what is the status of the Boston deal?")
            fan = tl.fanout_ask(g["team_slug"], g["question"])
            merged = tl.merge_fanout(fan["team_ask_id"])
        self.assertTrue(merged["all_null"])
        self.assertEqual(sorted(merged["nulls"]), ["User 2", "User 3"])
        return fan

    def _hold_slots(self, fan):
        # 6. Slot offered to each member: User 3 auto-accepts (team policy),
        #    User 2 accepts the offer. slot.requester = User 1 on both.
        with self.h.use(self.u3):
            tl.set_team_policy("team", {"auto_accept_slots": True})
        with self.h.use(self.u1):
            out = tl.offer_all_null_slots(fan, "what is the status of the Boston deal?")
        self.assertTrue(out["ok"])
        kinds = {r["peer_id"]: r["response_kind"] for r in out["requests"]}
        self.assertEqual(set(kinds.values()), {"slot_offered"})
        u2_offer = self.u2.worker.offers_of("slot_create")[-1]
        self.assertEqual(u2_offer["requester"]["name"], "User 1")
        with self.h.use(self.u2):
            s2 = slots.create(self.u2.store, u2_offer["need"],
                              requester=u2_offer["requester"],
                              origin_id=u2_offer["origin_id"], actor="user")
            pch.note_slot_offered(u2_offer["peer_id"], u2_offer["ask_id"], s2)
        with self.h.use(self.u3):
            held = slots.open_slots(self.u3.store)
        self.assertEqual(len(held), 1)
        s3 = int(held[0]["fact_id"])
        self.assertEqual(held[0]["slot"]["requester"]["name"], "User 1")
        self.assertEqual(held[0]["slot"]["origin_id"], fan["origin_id"])
        # User 1's board: one waiting_on_them row for the team.
        rows = self.waiting_rows(self.u1)
        self.assertTrue(any("Waiting on the Team" in r["text"] for r in rows))
        return s2, s3

    def _quote_arrives_on_u3(self, *, hours_after: float = 2.0 + 4 / 60):
        # Backdate the asks so the provenance line can say "2 h 04 m after ask".
        with self.h.use(self.u1):
            sent = json.loads(Path(self.u1.env["QUILL_PEER_SENT"]).read_text())
            for s in sent:
                s["created_at"] = time.time() - hours_after * 3600
            Path(self.u1.env["QUILL_PEER_SENT"]).write_text(json.dumps(sent))
        stub = StubMail([{"kind": "mail", "external_id": "<q1@acme>", "thread_id": "TQ",
                          "ts": time.time(), "title": "Boston deal quote (Q-1042)",
                          "text": f"From: Dana Whitfield <dana@acme.com>\nSubject: "
                                  f"Boston deal quote (Q-1042)\n\n{QUOTE}",
                          "people": ["Dana Whitfield"], "body": QUOTE}])
        with self.h.use(self.u3):
            res = scheduler.sync_connector(stub, store=self.u3.store)
        self.assertEqual(res["landed"], 1)

    def test_scenario_3(self):
        fan = self._all_null_fanout()
        s2, s3 = self._hold_slots(fan)
        # 7. +2 h: the quote email arrives in User 3's inbox via connector sync.
        self._quote_arrives_on_u3()
        # 8. User 3 sees "User 1 was asking about the Boston quote — send it?"
        fills = self.u3.worker.offers_of("slot_fill")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fact_id"], s3)
        self.assertIn("User 1 was asking about the Boston deal", fills[0]["message"])
        self.assertIn("send it?", fills[0]["message"])
        eid = fills[0]["event_id"]
        # 9. Yes: delivered to User 1; User 3 slot done; User 2 slot cancelled.
        with self.h.use(self.u3):
            out = slots.deliver(self.u3.store, s3, eid)
            self.assertEqual(out["status"], "delivered")
            self.assertEqual(self.u3.store.get_task(s3)["status"], "done")
        with self.h.use(self.u2):
            self.assertEqual(self.u2.store.get_task(s2)["status"], "cancelled")
            tx = self.u2.store.last_transition(s2)
            self.assertIn("filled by User 3", tx["reason"])
        # 10. User 1's board: done with provenance "from User 3, 2 h 04 m after ask".
        with self.h.use(self.u1):
            done = self.waiting_rows(self.u1, ("done",))
            self.assertTrue(done)
            fill_rows = [r for r in pch.answers() if r.get("response_kind") == "fill"]
            self.assertEqual(len(fill_rows), 1)
            self.assertEqual(fill_rows[0]["peer_name"], "User 3")
        line = next(t for t in self.u1.worker.texts("result")
                    if "User 3's Sparrow found" in t)
        self.assertIn("2 h 04 m after the ask", line)
        self.assertIn("Q-1042", line)

    def test_negative_not_it_keeps_slot_open_and_never_reoffers(self):
        fan = self._all_null_fanout()
        s2, s3 = self._hold_slots(fan)
        self._quote_arrives_on_u3()
        eid = self.u3.worker.offers_of("slot_fill")[0]["event_id"]
        with self.h.use(self.u3):
            slots.reject_fill(self.u3.store, s3, eid)
            self.assertEqual(self.u3.store.get_task(s3)["status"], "awaiting_data")
            ev = self.u3.store.get_event(eid)
            self.assertEqual(slots.evaluate_event(self.u3.store, eid, ev), [])
        self.assertEqual(len(self.u3.worker.offers_of("slot_fill")), 1)

    def test_negative_policy_denies_quotes_gives_policy_denied_not_silence(self):
        fan = self._all_null_fanout()
        s2, s3 = self._hold_slots(fan)
        self._quote_arrives_on_u3()
        eid = self.u3.worker.offers_of("slot_fill")[0]["event_id"]
        with self.h.use(self.u3):
            u1_on_u3 = self.h.peer_id(self.u3, self.u1)
            pch.set_policy(u1_on_u3, {"availability": "auto", "work": "deny",
                                      "contact": "offer", "personal": "offer",
                                      "other": "offer"})
            out = slots.deliver(self.u3.store, s3, eid)
            self.assertEqual(out["status"], "policy_denied")
            # Filled locally, never delivered.
            self.assertEqual(self.u3.store.get_task(s3)["status"], "done")
            self.assertEqual(self.u3.store.slot_candidate(s3, eid)["verdict"], "withheld")
        with self.h.use(self.u1):
            rows = [r for r in pch.answers() if r.get("kind") == "slot_request"
                    and r["peer_name"] == "User 3"]
            self.assertEqual(rows[0]["status"], "declined")
            self.assertEqual(rows[0]["null_reason"], "policy_denied")
        self.assertTrue(any("disclosure policy" in t
                            for t in self.u1.worker.texts("result")))

    def test_negative_two_fills_within_a_minute_only_first_delivers(self):
        fan = self._all_null_fanout()
        s2, s3 = self._hold_slots(fan)
        self._quote_arrives_on_u3()
        eid = self.u3.worker.offers_of("slot_fill")[0]["event_id"]
        with self.h.use(self.u3):
            first = slots.deliver(self.u3.store, s3, eid)
            self.assertEqual(first["status"], "delivered")
            eid2 = self.u3.store.insert(__import__("app.events", fromlist=["Event"]).Event(
                time=time.time(), modality=__import__("app.events", fromlist=["Modality"])
                .Modality.DOCUMENT, raw=QUOTE + " (revised)", source="documents.file"))
            second = slots.deliver(self.u3.store, s3, eid2)
            self.assertFalse(second["ok"])
            self.assertTrue(second["superseded"])
        with self.h.use(self.u1):
            fills = [r for r in pch.answers() if r.get("response_kind") == "fill"]
            self.assertEqual(len(fills), 1)
        # And User 2's own late fill for the same origin is a no-op on User 1.
        with self.h.use(self.u2):
            eid3 = self.u2.store.insert(__import__("app.events", fromlist=["Event"]).Event(
                time=time.time(), modality=__import__("app.events", fromlist=["Modality"])
                .Modality.DOCUMENT, raw=QUOTE, source="documents.file"))
            # The slot is already cancelled → deliver refuses.
            out = slots.deliver(self.u2.store, s2, eid3)
            self.assertFalse(out["ok"])

    def test_negative_ask_during_meeting_mode_is_queued_then_offered(self):
        with mock.patch("app.services.meeting_mode.status",
                        return_value={"active": True}):
            res = self.ask_u2()
        self.assertEqual(res["null_result"]["slot_offered"], True)
        self.assertEqual(self.u2.worker.offers_of("peer_null_options"), [])
        with self.h.use(self.u2):
            self.assertEqual(len(pch.deferred_null_offers()), 1)
            with mock.patch("app.services.meeting_mode.status",
                            return_value={"active": False}):
                self.assertEqual(pch.flush_deferred_null_offers(), 1)
        self.assertEqual(len(self.u2.worker.offers_of("peer_null_options")), 1)


class FanoutPropertyTests(unittest.TestCase):
    """Fan-out with N peers: when every member returns null, the first fill
    anywhere yields exactly one delivery and N−1 cancellations, whichever
    member fills; when any member answers, no slot is requested at all."""

    N = 3

    def setUp(self):
        names = ["Asker"] + [f"Member {i}" for i in range(1, self.N + 1)]
        self.h = Harness(names)
        self.h.start()
        self.asker = self.h.instances["Asker"]
        self.members = [self.h.instances[n] for n in names[1:]]
        for m in self.members:
            self.h.pair(self.asker, m)
        with self.h.use(self.asker):
            tl.upsert_team("Team", [self.h.peer_id(self.asker, m) for m in self.members])
        for m in self.members:
            with self.h.use(m):
                tl.upsert_team("Team", [self.h.peer_id(m, self.asker)])
                tl.set_team_policy("team", {"auto_accept_slots": True})
        from app.services import model_log
        self._ml = mock.patch.object(model_log.model_log, "_path",
                                     self.h.root / "model_calls.jsonl")
        self._ml.start()

    def tearDown(self):
        self._ml.stop()
        self.h.stop()

    def _fanout(self):
        with self.h.use(self.asker):
            fan = tl.fanout_ask("team", "what is the status of the Boston deal?")
            merged = tl.merge_fanout(fan["team_ask_id"])
        return fan, merged

    def test_all_null_then_any_member_fills(self):
        for filler_idx in range(self.N):
            with self.subTest(filler=filler_idx):
                fan, merged = self._fanout()
                self.assertTrue(merged["all_null"])
                with self.h.use(self.asker):
                    tl.offer_all_null_slots(fan, "what is the status of the Boston deal?")
                held = {}
                for m in self.members:
                    with self.h.use(m):
                        mine = [s for s in slots.open_slots(m.store)
                                if (s["slot"] or {}).get("origin_id") == fan["origin_id"]]
                        self.assertEqual(len(mine), 1)
                        held[m.name] = int(mine[0]["fact_id"])
                filler = self.members[filler_idx]
                with self.h.use(filler):
                    from app.events import Event, Modality
                    eid = filler.store.insert(Event(
                        time=time.time(), modality=Modality.DOCUMENT, raw=QUOTE,
                        source="documents.file", meta={"title": "Boston deal quote"}))
                    out = slots.deliver(filler.store, held[filler.name], eid)
                    self.assertEqual(out["status"], "delivered")
                deliveries, cancellations = 0, 0
                for m in self.members:
                    with self.h.use(m):
                        st = m.store.get_task(held[m.name])["status"]
                    if st == "done":
                        deliveries += 1
                    elif st == "cancelled":
                        cancellations += 1
                self.assertEqual((deliveries, cancellations), (1, self.N - 1))
                with self.h.use(self.asker):
                    fills = [r for r in pch.answers()
                             if r.get("response_kind") == "fill"
                             and r.get("origin_id") == fan["origin_id"]]
                    self.assertEqual(len(fills), 1)
                    self.assertEqual(fills[0]["peer_name"], filler.name)

    def test_any_answer_means_no_slot_requests(self):
        for mask in range(1, 2 ** self.N):
            with self.subTest(mask=mask):
                for i, m in enumerate(self.members):
                    m.answer = ({"text": "Quote is $42k.",
                                 "claims": [{"text": "Quote is $42k", "fact_id": 1,
                                             "event_id": 1, "ts": 1.0}]}
                                if mask & (1 << i) else None)
                fan, merged = self._fanout()
                self.assertFalse(merged["all_null"])
                self.assertEqual(len(merged["answered"]), bin(mask).count("1"))
                self.assertEqual(len(merged["nulls"]), self.N - bin(mask).count("1"))
                for m in self.members:
                    with self.h.use(m):
                        self.assertEqual(
                            [s for s in slots.open_slots(m.store)
                             if (s["slot"] or {}).get("origin_id") == fan["origin_id"]],
                            [])


if __name__ == "__main__":
    unittest.main()
