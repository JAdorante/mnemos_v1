"""CAL Stage 2 — episodes and replay.

An episode is a root frame plus everything nested under it. The properties that
matter are structural: interruptions fold in rather than splitting, episodes on
a timeline never overlap, and an episode with no anchor says so instead of
borrowing a label.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path

from app.events import Event, Modality
from app.services.context import episodes as ep
from app.services.context import replay as rp
from app.services.context.frames import Anchor, Segmenter, StreamEvent
from app.storage import Store


def A(n, strength=0.8):
    return Anchor("entity", n, f"E{n}", strength, "medium")


def run(spec):
    evs, t, i = [], 0.0, 0
    for key, n, step in spec:
        for _ in range(n):
            evs.append(StreamEvent(i, t, (A(key),) if key else (), "Cursor"))
            i += 1
            t += step
    seg, places = Segmenter(), []
    for e in evs:
        places.append(seg.feed(e))
    seg.close()
    return ep.build(seg, places, run_id="t")


class EpisodeShapeTests(unittest.TestCase):
    def test_an_interruption_folds_into_its_episode(self) -> None:
        """A glance at mail during an hour of work is part of that hour."""
        eps = run([(1, 10, 20), (2, 5, 20), (1, 10, 20)])
        self.assertEqual(len(eps), 1)
        self.assertEqual(eps[0]["n_events"], 25, "including the excursion")

    def test_episodes_never_overlap(self) -> None:
        """A promoted excursion used to start its replacement frame before the
        parent closed, which reads on a timeline as being in two places."""
        eps = run([(1, 10, 20), (2, 40, 20), (3, 40, 20)])
        self.assertGreater(len(eps), 1)
        for a, b in zip(eps, eps[1:]):
            self.assertLessEqual(a["ended_at"], b["started_at"],
                                 f"{a['title']} overlaps {b['title']}")

    def test_every_event_lands_in_exactly_one_episode(self) -> None:
        eps = run([(1, 10, 20), (2, 40, 20), (None, 10, 20)])
        seen = [e for x in eps for e, _i in x["_event_ids"]]
        self.assertEqual(len(seen), len(set(seen)))

    def test_a_stray_event_is_not_an_episode(self) -> None:
        eps = run([(1, 1, 0)])
        self.assertEqual(eps, [])

    def test_coherence_is_measured_over_the_folded_stretch(self) -> None:
        """The frame-level ratio ignores child events, so an episode could
        report forty anchored events beside a coherence of zero."""
        eps = run([(1, 10, 20), (2, 5, 20), (1, 10, 20)])
        e = eps[0]
        anchored = e["n_events"] - e["n_inherited"]
        self.assertAlmostEqual(e["coherence"], anchored / e["n_events"])


class LabellingTests(unittest.TestCase):
    def test_unbound_episodes_say_so(self) -> None:
        eps = run([(None, 20, 20)])
        self.assertIsNone(eps[0]["node_type"])
        self.assertEqual(eps[0]["title"], "Cursor", "falls back to the app")

    def test_kind_needs_a_majority_not_a_plurality(self) -> None:
        """If the day was half mail and half code, 'comms' is worse than
        saying nothing."""
        self.assertEqual(ep.kind_for({"Cursor": 10}), "build")
        self.assertEqual(ep.kind_for({"Outlook": 10}), "comms")
        self.assertIsNone(ep.kind_for({"Cursor": 5, "Outlook": 5}))
        self.assertIsNone(ep.kind_for({}))
        self.assertIsNone(ep.kind_for({"SomeUnknownApp": 9}))


class ReplayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_ep_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.eid = self.store.resolve_entity("Boostrun", "org", ts=time.time())
        self.t0 = 1_700_000_000.0
        for i in range(20):
            ev = Event(time=self.t0 + i * 20, modality=Modality.INPUT,
                       raw="click", summary="click", source="desktop.click",
                       meta={"window": "Boostrun plan - Google Docs - Chromium"})
            self.store.insert(ev)

    def test_stream_carries_anchors_from_titles(self) -> None:
        evs = rp.stream_for(self.store, self.t0 - 1, self.t0 + 10_000)
        self.assertEqual(len(evs), 20)
        self.assertTrue(any(a.node_id == self.eid
                            for e in evs for a in e.anchors))

    def test_replay_persists_and_is_idempotent(self) -> None:
        for _ in range(2):
            res = rp.replay(self.store, t0=self.t0 - 1, t1=self.t0 + 10_000,
                            run_id="r1", persist=True)
        eps = self.store.list_episodes(run_id="r1")
        self.assertEqual(len(eps), len(res["episodes"]),
                         "a re-run replaces, never duplicates")
        self.assertEqual(eps[0]["title"], "Boostrun")
        self.assertEqual(len(self.store.episode_event_ids(eps[0]["id"])), 20)

    def test_clear_run_removes_everything(self) -> None:
        rp.replay(self.store, t0=self.t0 - 1, t1=self.t0 + 10_000,
                  run_id="r1", persist=True)
        self.store.clear_context_run("r1")
        self.assertEqual(self.store.list_episodes(run_id="r1"), [])

    def test_timeline_marks_unbound_without_inventing_a_label(self) -> None:
        out = rp.timeline([{"started_at": self.t0, "ended_at": self.t0 + 600,
                            "title": "Firefox", "node_type": None, "kind": None,
                            "n_events": 10, "n_inherited": 10,
                            "coherence": 0.0}])
        self.assertIn("— (Firefox)", out)


class SelfExclusionTests(unittest.TestCase):
    """The user's own name is in half their window titles and identifies
    nothing they are working on."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="quill_self_"))
        self.store = Store(db_path=self.tmp / "t.db", audio_dir=self.tmp / "a")
        self.me = self.store.resolve_person("Dana Okafor", ts=time.time())

    def test_the_self_person_never_anchors(self) -> None:
        from unittest import mock
        from app.services.context import surfaces as sf
        import app.services.identity as ident
        with mock.patch.object(ident, "user_identity",
                               lambda *a, **k: {"name": "Dana Okafor"}):
            idx = sf.SurfaceIndex(self.store)
            self.assertEqual(
                idx.from_title("Mail - Dana Okafor - Outlook — Firefox"), [])

    def test_other_people_still_anchor(self) -> None:
        other = self.store.resolve_person("Rui Tanaka", ts=time.time())
        from app.services.context import surfaces as sf
        idx = sf.SurfaceIndex(self.store)
        hits = idx.from_title("Rui Tanaka - Google Search - Chromium")
        self.assertEqual([h.node_id for h in hits], [other])


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
