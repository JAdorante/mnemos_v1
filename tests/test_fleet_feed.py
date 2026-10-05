"""Fleet subscriptions (Phase 2): fan-out by topic, SSE, and catch-up.

Two local agents on one Sparrow see each other's signals within a second, and
a reconnecting agent catches up without duplicates.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time

from fastapi.testclient import TestClient

from app.services.fleet import feed, registry
from tests.fleet_support import FleetTestCase, agent_body, fleet_app, full_signal


def _sse_items(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


class FeedBase(FleetTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(fleet_app())
        self.a = registry.register_agent("rates", ["eng.status"], "both")
        self.b = registry.register_agent("fx", ["eng.status", "eng.infra"],
                                         "both")
        self.reader = registry.register_agent("eq", ["sales.leads"],
                                              "subscriber")

    def h(self, rec) -> dict:
        return {"Authorization": f"Bearer {rec['token']}"}

    def publish(self, rec, **over):
        r = self.client.post("/fleet/publish", json=agent_body(**over),
                             headers=self.h(rec))
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class FanOutTests(FeedBase):
    def test_sibling_sees_the_signal_within_a_second(self) -> None:
        async def run() -> float:
            sid, q = feed.subscribe(asyncio.get_running_loop())
            try:
                t0 = time.monotonic()
                await asyncio.get_running_loop().run_in_executor(
                    None, lambda: self.publish(self.a))
                while True:
                    item = await asyncio.wait_for(q.get(), timeout=1.0)
                    if feed.deliverable(item, registry.get_agent("fx")):
                        return time.monotonic() - t0
            finally:
                feed.unsubscribe(sid)
        self.assertLess(asyncio.run(run()), 1.0)

    def test_sse_stream_delivers_to_the_sibling_not_the_publisher(self) -> None:
        results = {}

        def listen(name, rec):
            r = self.client.get("/fleet/stream?max_s=1.5", headers=self.h(rec))
            results[name] = (r.status_code, r.headers.get("content-type"),
                             _sse_items(r.text))

        threads = [threading.Thread(target=listen, args=("fx", self.b)),
                   threading.Thread(target=listen, args=("rates", self.a))]
        for t in threads:
            t.start()
        time.sleep(0.4)
        sig = self.publish(self.a)["signal"]
        for t in threads:
            t.join(10)
        status, ctype, items = results["fx"]
        self.assertEqual(status, 200)
        self.assertTrue(ctype.startswith("text/event-stream"))
        self.assertEqual([i["origin_id"] for i in items], [sig["origin_id"]])
        item = items[0]
        self.assertEqual(item["provenance"], "local")
        self.assertEqual(item["hops"], 0)
        self.assertEqual(item["producer"], "agent:rates")
        self.assertEqual(results["rates"][2], [])

    def test_registered_topics_bound_the_feed_not_the_query(self) -> None:
        self.publish(self.a)
        r = self.client.get("/fleet/signals?topic=eng.status",
                            headers=self.h(self.reader))
        self.assertEqual(r.json()["signals"], [])
        r = self.client.get("/fleet/stream?topics=eng.status&max_s=0.3",
                            headers=self.h(self.reader))
        self.assertEqual(_sse_items(r.text), [])

    def test_publisher_only_agent_cannot_read(self) -> None:
        pub = registry.register_agent("pub", ["eng.status"], "publisher")
        self.assertEqual(self.client.get("/fleet/signals",
                                         headers=self.h(pub)).status_code, 403)
        self.assertEqual(self.client.get("/fleet/signals").status_code, 401)

    def test_expired_signals_are_dropped_at_delivery(self) -> None:
        now = time.time()
        stale = full_signal(ts=now - 120, expires_at=now - 1,
                            producer="agent:rates")
        feed._deliver({"seq": feed.next_seq(), "provenance": "local",
                       "origin_id": stale["origin_id"], "producer": stale["producer"],
                       "hops": 0, "topic": "eng.status", "signal": stale})
        r = self.client.get("/fleet/signals", headers=self.h(self.b))
        self.assertEqual(r.json()["signals"], [])


class CatchUpTests(FeedBase):
    def test_reconnect_catches_up_without_duplicates(self) -> None:
        first = self.publish(self.a, summary="one")
        r = self.client.get("/fleet/signals", headers=self.h(self.b)).json()
        self.assertEqual(len(r["signals"]), 1)
        cursor = r["cursor"]
        self.assertEqual(cursor, first["seq"])
        self.publish(self.a, summary="two")
        self.publish(self.a, summary="three")
        again = self.client.get(f"/fleet/signals?since={cursor}",
                                headers=self.h(self.b)).json()
        self.assertEqual([s["signal"]["summary"] for s in again["signals"]],
                         ["two", "three"])
        tail = self.client.get(f"/fleet/signals?since={again['cursor']}",
                               headers=self.h(self.b)).json()
        self.assertEqual(tail["signals"], [])

    def test_stream_replays_after_last_event_id(self) -> None:
        one = self.publish(self.a, summary="one")
        self.publish(self.a, summary="two")
        r = self.client.get("/fleet/stream?max_s=0.3",
                            headers={**self.h(self.b),
                                     "Last-Event-ID": str(one["seq"])})
        self.assertEqual([i["signal"]["summary"] for i in _sse_items(r.text)],
                         ["two"])

    def test_catch_up_past_the_ring_reads_the_store_once(self) -> None:
        """Seqs survive a restart because they ride in the persisted event."""
        now = time.time()
        old = full_signal(ts=now - 30, expires_at=now + 600,
                          producer="agent:rates", origin_id="s1:old")
        meta = {"signal": old, "fleet_seq": 1_000, "provenance": "peer",
                "peer_node": "node-b"}

        class FakeStore:
            def events_in_window(self, t0, t1, source=None, limit=100):
                return [{"id": 1, "meta": meta}] if source == "peer.signal" else []

        self.publish(self.a, summary="live")
        items = feed.catch_up(registry.get_agent("fx"), None, 0,
                              store=FakeStore())
        self.assertEqual([i["origin_id"] for i in items][0], "s1:old")
        self.assertEqual(items[0]["provenance"], "peer")
        self.assertEqual(items[0]["peer_node"], "node-b")
        self.assertEqual(len(items), 2)
        self.assertEqual(feed.catch_up(registry.get_agent("fx"), None, 1_000,
                                       store=FakeStore())[0]["signal"]["summary"],
                         "live")

    def test_ring_is_bounded(self) -> None:
        import os
        os.environ["QUILL_FLEET_RING"] = "3"
        try:
            os.environ["QUILL_FLEET_RATE"] = "100"
            for i in range(6):
                self.publish(self.a, summary=f"t{i}")
            self.assertEqual(len(feed._ring), 3)
        finally:
            os.environ.pop("QUILL_FLEET_RING", None)


class McpSignalsToolTests(FeedBase):
    def test_signals_is_a_read_tool_with_provenance(self) -> None:
        from app.services import mcp_tools
        self.assertIn("signals", mcp_tools.READ_TOOLS)
        self.publish(self.a)
        out = mcp_tools.call_tool("signals", {"topic": "eng.status"})
        self.assertTrue(out["ok"])
        res = out["results"][0]
        self.assertEqual(res["subject"], "Atlas migration")
        self.assertEqual(res["kind"], "status_update")
        self.assertEqual(res["body"]["status"], "at_risk")
        self.assertEqual(res["provenance"]["kind"], "local")
        self.assertEqual(res["provenance"]["producer"], "agent:rates")
        self.assertIn("disclosure_class", res)
