"""Mnemos Defining Interface — packet emit, constellation, home intelligence."""
from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


class ParseApprovalPacketTests(unittest.TestCase):
    def test_parses_structured_fields(self):
        from app.services.agent_bridge import _parse_approval_packet

        text = (
            "APPROVAL NEEDED — email Justin\n\n"
            "Action: send email\n"
            "To: justin@example.com\n"
            "Subject: Pricing follow-up\n"
            "Body:\nHi Justin,\n\nForty-nine a month.\n\n"
            "Why: you promised a follow-up\n"
            "Source: audio · yesterday\n\n"
            "Reply 'approve' to proceed, 'cancel' to stop, or tell me what to change"
        )
        pkt = _parse_approval_packet(text)
        self.assertIsNotNone(pkt)
        self.assertEqual(pkt["kind"], "approval")
        self.assertEqual(pkt["summary"], "email Justin")
        self.assertEqual(pkt["fields"]["action"], "send email")
        self.assertEqual(pkt["fields"]["to"], "justin@example.com")
        self.assertEqual(pkt["fields"]["subject"], "Pricing follow-up")
        self.assertIn("Forty-nine", pkt["fields"]["body"])
        self.assertIn("promised", pkt["fields"]["why"])

    def test_non_approval_returns_none(self):
        from app.services.agent_bridge import _parse_approval_packet

        self.assertIsNone(_parse_approval_packet("Want me to run these to-dos?"))
        self.assertIsNone(_parse_approval_packet(""))

    def test_emit_attaches_packet(self):
        from app.services.agent_bridge import AgentWorker

        w = AgentWorker.__new__(AgentWorker)
        w.lock = threading.Lock()
        w.events = []
        w.next_id = 1
        with mock.patch("app.services.voice.maybe_speak_reply"):
            AgentWorker._emit(
                w, "ask",
                "APPROVAL NEEDED — launch cursor\n\nAction: launch_app\n"
                "Reply 'approve' to proceed")
        self.assertEqual(len(w.events), 1)
        self.assertIn("packet", w.events[0])
        self.assertEqual(w.events[0]["packet"]["kind"], "approval")
        self.assertEqual(w.events[0]["packet"]["summary"], "launch cursor")

    def test_emit_merges_pending_desktop_fields(self):
        from app.services.agent_bridge import AgentWorker

        w = AgentWorker.__new__(AgentWorker)
        w.lock = threading.Lock()
        w.events = []
        w.next_id = 1
        w.agent = mock.Mock()
        w.fast_agent = None
        w.agent._pending_approval_packet = {
            "packet_id": 42,
            "payload_hash": "abc",
            "summary": "write 10 bytes to index.html (new)",
            "fields": {"action": "write_file", "path": r"C:\jail\index.html",
                       "content": "<h1>hi</h1>"},
        }
        with mock.patch("app.services.voice.maybe_speak_reply"):
            AgentWorker._emit(
                w, "ask",
                "APPROVAL NEEDED — write 10 bytes to index.html (new)\n"
                "Reply 'approve' to proceed")
        pkt = w.events[0]["packet"]
        self.assertEqual(pkt["packet_id"], 42)
        self.assertEqual(pkt["payload_hash"], "abc")
        self.assertEqual(pkt["fields"]["path"], r"C:\jail\index.html")
        self.assertEqual(pkt["fields"]["action"], "write_file")


class ConstellationTests(unittest.TestCase):
    def test_constellation_shape(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Justin")
                store.resolve_person("Marc")
                store.add_task(
                    "Follow up with Justin on pricing",
                    confidence=0.9, extracted_at=time.time())
                data = graph.constellation(store, limit=24)
                self.assertIn("nodes", data)
                self.assertIn("edges", data)
                self.assertIn("count", data)
                self.assertGreaterEqual(len(data["nodes"]), 1)
                self.assertTrue(data.get("field"))
            finally:
                store.close()

    def test_tools_are_not_ideas(self):
        from app.services import graph
        from app.storage import Store

        self.assertEqual(graph.entity_constellation_kind("tool"), "tool")
        self.assertEqual(graph.entity_constellation_kind("software"), "tool")
        self.assertEqual(graph.entity_constellation_kind("org"), "org")
        self.assertEqual(graph.entity_constellation_kind("company"), "org")
        self.assertEqual(graph.entity_constellation_kind("project"), "project")
        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_entity("GitHub", kind="tool")
                store.resolve_entity("AWS", kind="service")
                data = graph.constellation(store, limit=24)
                kinds = {n["label"]: n["kind"] for n in data["nodes"]}
                self.assertEqual(kinds.get("GitHub"), "tool")
                self.assertEqual(kinds.get("AWS"), "tool")
            finally:
                store.close()

    def test_anchor_angles_are_well_distributed(self):
        # The renderer places people on their stable `anchor` angle; a weak hash
        # (the old sum-of-char-codes) put sequential ids within ~2° of each other,
        # collapsing everyone onto one spoke. Assert real spread instead.
        import math

        from app.services.graph import _anchor_angle

        angles = sorted(_anchor_angle(f"person:{i}") for i in range(1, 13))
        self.assertTrue(all(0 <= a <= 2 * math.pi for a in angles))
        span = angles[-1] - angles[0]
        self.assertGreater(span, math.pi)          # covers over half the circle
        # No two sequential-id people share a spoke (old hash failed this hard).
        gaps = [angles[i + 1] - angles[i] for i in range(len(angles) - 1)]
        self.assertGreater(max(gaps), 0.3)
        self.assertEqual(len({round(a, 3) for a in angles}), len(angles))


class GravityGoldenTests(unittest.TestCase):
    """Behavioral contracts for Memory Gravity — safe to retune against these."""

    def test_pinned_outranks_unpinned_peer(self):
        from app.services.graph import score_gravity

        base = dict(kind="person", confidence=0.9, age_days=10.0,
                    prospective=0.2, relationship=0.3)
        pinned = score_gravity(**base, pinned=True)["gravity"]
        plain = score_gravity(**base, pinned=False)["gravity"]
        self.assertGreater(pinned, plain)

    def test_overdue_commitment_outranks_stale_idea(self):
        from app.services.graph import score_gravity

        overdue = score_gravity(
            kind="commitment", confidence=0.85, age_days=5.0,
            prospective=0.9, future=0.8, unresolved=0.7,
        )["gravity"]
        stale_idea = score_gravity(
            kind="idea", confidence=0.8, age_days=40.0,
            prospective=0.0, semantic=0.2,
        )["gravity"]
        self.assertGreater(overdue, stale_idea)

    def test_low_confidence_is_soft_suppressed(self):
        from app.services.graph import GRAVITY, score_gravity, trust_gate

        self.assertLess(trust_gate(0.15), 0.05)
        self.assertGreater(trust_gate(0.34), 0.9)
        self.assertEqual(trust_gate(0.1, pinned=True), 1.0)
        low = score_gravity(
            kind="commitment", confidence=0.15, age_days=2.0,
            prospective=0.9, unresolved=0.7,
        )["gravity"]
        mid = score_gravity(
            kind="commitment", confidence=0.32, age_days=2.0,
            prospective=0.9, unresolved=0.7,
        )["gravity"]
        self.assertLess(low, mid * 0.35)
        self.assertLess(low, 0.08)
        # No hard cliff at the old 0.28 boundary.
        a = trust_gate(GRAVITY["trust_lo"] + 0.01)
        b = trust_gate(GRAVITY["trust_hi"] - 0.01)
        self.assertLess(a, b)

    def test_pinned_always_in_focus(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                pid = store.resolve_person("QuietPeer")
                # Flood with louder open work so QuietPeer would otherwise sink.
                for i in range(8):
                    store.add_commitment(
                        f"Loud open item {i}",
                        confidence=0.95, extracted_at=time.time())
                nid = f"person:{pid}"
                graph.pin_constellation_node(store, nid, True)
                data = graph.constellation(store, limit=20)
                node = next(n for n in data["nodes"] if n["id"] == nid)
                self.assertTrue(node["pinned"])
                self.assertEqual(node["layer"], "focus")
            finally:
                store.close()

    def test_diversity_keeps_at_least_two_people(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.resolve_person("Bea")
                for i in range(20):
                    store.add_task(
                        f"Noise task {i}",
                        confidence=0.99, extracted_at=time.time())
                data = graph.constellation(store, limit=24)
                focus_people = [n for n in data["nodes"]
                                if n["layer"] == "focus" and n["kind"] == "person"]
                self.assertGreaterEqual(len(focus_people), 2)
            finally:
                store.close()

    def test_entities_are_not_flooded_out_by_open_work(self):
        # A wall of open tasks must not crowd every project/tool out of the field
        # — focus reserves slots for entities (the constellation's flood bug).
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Ada")
                store.resolve_person("Bea")
                for name in ("GitHub", "AWS", "Cursor", "DTC Venture Pulse"):
                    store.resolve_entity(name, "tool")
                for i in range(20):
                    store.add_task(f"Noise task {i}",
                                   confidence=0.99, extracted_at=time.time())
                data = graph.constellation(store, limit=24)
                focus_entities = [n for n in data["nodes"]
                                  if n["layer"] == "focus"
                                  and n["kind"] in graph._ENTITY_FOCUS_KINDS]
                self.assertGreaterEqual(
                    len(focus_entities),
                    min(graph.GRAVITY["min_entities_in_focus"], 4))
            finally:
                store.close()

    def test_temporal_is_single_channel(self):
        from app.services.graph import GRAVITY, score_gravity

        self.assertIn("temp", GRAVITY["w"])
        self.assertNotIn("freq", GRAVITY["w"])
        self.assertNotIn("long", GRAVITY["w"])
        self.assertNotIn("nov", GRAVITY["w"])
        young = score_gravity(kind="person", confidence=0.9, age_days=1.0)["gravity"]
        old = score_gravity(kind="person", confidence=0.9, age_days=60.0)["gravity"]
        self.assertGreater(young, old)


class HomeIntelligenceTests(unittest.TestCase):
    def test_home_intelligence_shape(self):
        from app.services.home_intelligence import build
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                store.resolve_person("Abby")
                store.add_commitment(
                    "Call Abby tomorrow", confidence=0.8, extracted_at=time.time())
                out = build(
                    store,
                    agent_state={"awaiting": True, "todo_pending": False},
                    recent_events=[{"id": 1, "text": "hello", "modality": "audio"}],
                )
                self.assertIn("commitments", out)
                self.assertIn("ambient", out)
                self.assertIn("follow_ups", out)
                self.assertTrue(out["awaiting_approval"])
                self.assertTrue(any(n.get("attention") for n in out["ambient"]))
                self.assertTrue(out["highlights"])
            finally:
                store.close()


class ConstellationEditTests(unittest.TestCase):
    def test_link_and_unlink_persist_hide(self):
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                a = store.resolve_person("Justin")
                b = store.resolve_person("Marc")
                sa, sb = f"person:{a}", f"person:{b}"
                store.add_relation("person", a, "co_occurs", "person", b,
                                   origin="derived", weight=3)
                data = graph.constellation(store, limit=24)
                ids = {e["source"] + "|" + e["target"] for e in data["edges"]}
                self.assertTrue(
                    (sa + "|" + sb) in ids or (sb + "|" + sa) in ids)

                graph.unlink_constellation_edge(store, sa, sb)
                data2 = graph.constellation(store, limit=24)
                ids2 = {(e["source"], e["target"]) for e in data2["edges"]}
                self.assertNotIn((sa, sb), ids2)
                self.assertNotIn((sb, sa), ids2)

                graph.link_constellation_edge(store, sa, sb)
                data3 = graph.constellation(store, limit=24)
                linked = [e for e in data3["edges"]
                          if {e["source"], e["target"]} == {sa, sb}]
                self.assertTrue(linked)
                self.assertTrue(linked[0].get("manual"))
            finally:
                store.close()


class ThemeApplyTests(unittest.TestCase):
    def test_apply_injects_ink_and_ui(self):
        from app.api.mnemos_theme import apply

        page = apply("<html>@@FONTS@@ @@ROOT@@ @@INK@@ @@CHROME@@ @@UI_JS@@ @@BRAND@@</html>")
        self.assertIn("--paper:", page)
        self.assertIn("/static/css/mnemos-ink.css", page)
        self.assertIn("/static/css/mnemos-chrome.css", page)
        self.assertIn("/static/js/mnemos-ui.js", page)
        self.assertIn("Mnemos", page)


if __name__ == "__main__":
    unittest.main()
