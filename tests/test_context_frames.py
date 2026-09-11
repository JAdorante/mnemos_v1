"""CAL Stage 2 — frame segmentation, driven by synthetic golden streams.

Measured against one real 11-hour day, this design's rules fire 0 times (forced
split), 1 time (switch) and 2 times (freeze-on-idle). Real capture cannot test
this state machine; it can only tell you whether the thresholds feel right. So
the streams below are built to hit each rule deliberately, with shapes seeded
from that day's measured statistics — p50 gap 4 s, p90 88 s, median run one
event — rather than from a tidy imagined day of 40-minute work blocks.
"""
from __future__ import annotations

import unittest

from app.services.context.frames import (
    Anchor, Segmenter, StreamEvent, FREEZE_S, IDLE_SUSPEND_S, MAX_FRAME_S,
)


def A(n, strength=0.8, tier="medium"):
    return Anchor("entity", n, f"E{n}", strength, tier)


def stream(spec, start=0.0):
    """[(anchor|None, count, step_seconds), ...] -> ordered StreamEvents."""
    evs, t, i = [], start, 0
    for key, n, step in spec:
        for _ in range(n):
            evs.append(StreamEvent(i, t, (A(key),) if key else (), "app"))
            i += 1
            t += step
    return evs


def run(evs, **kw):
    seg = Segmenter(**kw)
    places, fevs = [], []
    for e in evs:
        p = seg.feed(e)
        places.append(p)
        fevs.extend(p.frame_events)
    fevs.extend(seg.close())
    return seg, places, fevs


def roots(seg):
    return [f for f in seg.frames if f.parent_id is None]


class SteadyWorkTests(unittest.TestCase):
    def test_one_stretch_is_one_frame(self) -> None:
        seg, _, _ = run(stream([(1, 40, 20)]))
        self.assertEqual(len(roots(seg)), 1)
        self.assertEqual(roots(seg)[0].n_events, 40)

    def test_empty_and_single_event_streams(self) -> None:
        seg, _, _ = run([])
        self.assertEqual(seg.frames, [])
        seg, _, _ = run(stream([(1, 1, 0)]))
        self.assertEqual(len(seg.frames), 1)


class InterruptVsSwitchTests(unittest.TestCase):
    """The distinction that decides whether a timeline is readable.

    Most ambient systems shatter an hour of work into thirty fragments because
    the user glanced at Slack.
    """

    def test_a_glance_is_absorbed_entirely(self) -> None:
        """One foreign event does not even disturb the frame — it just costs
        coherence, which is the honest record of it."""
        seg, _, _ = run(stream([(1, 10, 20), (2, 1, 30), (1, 10, 20)]))
        self.assertEqual(len(seg.frames), 1)
        self.assertLess(seg.frames[0].coherence, 1.0)

    def test_an_excursion_that_returns_does_not_cut_the_episode(self) -> None:
        seg, _, fevs = run(stream([(1, 10, 20), (2, 5, 20), (1, 10, 20)]))
        self.assertEqual(len(roots(seg)), 1, "one continuous piece of work")
        kinds = [f.kind for f in fevs]
        self.assertIn("interrupt", kinds)
        self.assertNotIn("switch", kinds)

    def test_the_interruption_nests_under_its_parent(self) -> None:
        seg, _, _ = run(stream([(1, 10, 20), (2, 5, 20), (1, 10, 20)]))
        child = [f for f in seg.frames if f.parent_id is not None]
        self.assertEqual(len(child), 1)
        self.assertEqual(child[0].parent_id, roots(seg)[0].id)

    def test_returning_collapses_rather_than_nesting_deeper(self) -> None:
        """A morning of glancing at mail must not build a tower of frames."""
        spec = []
        for _ in range(6):
            spec += [(1, 8, 20), (2, 4, 20)]
        seg, _, _ = run(stream(spec))
        self.assertLessEqual(max(len(seg.frames), 1), 14)
        for f in seg.frames:
            self.assertLessEqual(f.n_events, 200)

    def test_sustained_dwell_elsewhere_is_a_switch(self) -> None:
        seg, _, fevs = run(stream([(1, 10, 20), (2, 25, 20)]))
        self.assertEqual(len(roots(seg)), 2)
        self.assertIn("switch", [f.kind for f in fevs])

    def test_an_interruption_that_never_ends_becomes_a_switch(self) -> None:
        """Once a child frame is active its anchor stops being a competitor, so
        without promotion it would quietly absorb the rest of the day."""
        seg, _, fevs = run(stream([(1, 10, 20), (2, 40, 20)]))
        switched = [f for f in fevs if f.kind == "switch"]
        self.assertTrue(switched)
        self.assertIsNone(switched[-1].frame.parent_id, "promoted to a root")


class HysteresisTests(unittest.TestCase):
    def test_alternating_evidence_does_not_flap(self) -> None:
        """A single threshold oscillates; two thresholds with a dwell bar do
        not. Flapping is what shreds an hour into fragments."""
        spec = []
        for _ in range(20):
            spec += [(1, 2, 15), (2, 2, 15)]
        seg, _, _ = run(stream(spec))
        self.assertLessEqual(len(roots(seg)), 4,
                             "80 alternating events must not make 40 frames")


class IdleTests(unittest.TestCase):
    """Lunch is not a context switch. Working on something else is."""

    def test_idle_then_the_same_work_resumes_the_same_frame(self) -> None:
        evs = stream([(1, 10, 20)])
        t = evs[-1].t + IDLE_SUSPEND_S + 300      # ~25 min away
        evs += stream([(1, 10, 20)], start=t)
        seg, _, fevs = run(evs)
        kinds = [f.kind for f in fevs]
        self.assertIn("suspend", kinds)
        self.assertIn("resume", kinds)
        self.assertEqual(len(roots(seg)), 1, "the same work, continued")

    def test_evidence_is_frozen_not_decayed_while_idle(self) -> None:
        """Forty minutes away must not cost the frame its hold; decaying on
        wall-clock makes a system that forgets what you were doing at lunch."""
        evs = stream([(1, 10, 20)])
        seg = Segmenter()
        for e in evs:
            seg.feed(e)
        before = seg._share(("entity", 1))
        seg.feed(StreamEvent(999, evs[-1].t + 2400, (A(1),), "app"))
        self.assertAlmostEqual(before, seg._share(("entity", 1)), places=3)

    def test_a_long_absence_closes_the_frame(self) -> None:
        evs = stream([(1, 10, 20)])
        evs += stream([(1, 10, 20)], start=evs[-1].t + FREEZE_S + 600)
        seg, _, fevs = run(evs)
        self.assertEqual(len(roots(seg)), 2)
        self.assertIn("idle_gap", [f.reason for f in fevs])

    def test_suspended_frames_expire(self) -> None:
        evs = stream([(1, 5, 20)])
        evs += stream([(2, 5, 20)], start=evs[-1].t + IDLE_SUSPEND_S + 60)
        evs += stream([(2, 5, 20)], start=evs[-1].t + 7800)
        seg, _, fevs = run(evs)
        self.assertIn("suspend_expiry", [f.reason for f in fevs])


class ForcedSplitTests(unittest.TestCase):
    def test_ninety_minutes_splits(self) -> None:
        """Defensive only — on real capture the longest continuous run measured
        was 19 minutes, so this never fires outside a test."""
        seg, _, fevs = run(stream([(1, 400, 20)]))
        self.assertGreaterEqual(len(roots(seg)), 2)
        self.assertIn("max_duration", [f.reason for f in fevs])
        for f in roots(seg):
            if f.ended_at:
                self.assertLessEqual(f.ended_at - f.started_at, MAX_FRAME_S + 60)


class InheritanceTests(unittest.TestCase):
    """The correction that makes per-event coverage the wrong measure.

    A click carries no identifier and never will. It belongs to whatever the
    user was doing; demanding that each event resolve on its own is what made
    ">85% binding coverage per event" unsatisfiable by construction.
    """

    def test_unanchored_events_inherit_the_open_frame(self) -> None:
        evs = stream([(1, 5, 20), (None, 20, 10), (1, 5, 20)])
        seg, places, _ = run(evs)
        self.assertEqual(len(seg.frames), 1)
        fid = seg.frames[0].id
        self.assertTrue(all(p.frame_id == fid for p in places))
        self.assertEqual(sum(1 for p in places if p.inherited), 20)

    def test_a_stream_with_no_anchors_at_all_is_one_honest_unknown(self) -> None:
        seg, places, _ = run(stream([(None, 30, 20)]))
        self.assertEqual(len(seg.frames), 1)
        self.assertIsNone(seg.frames[0].key, "unbound is a real state")

    def test_the_first_anchor_adopts_an_unbound_frame(self) -> None:
        """It must not open a second frame beside the placeholder."""
        seg, _, _ = run(stream([(None, 5, 20), (1, 10, 20)]))
        self.assertEqual(len(seg.frames), 1)
        self.assertEqual(seg.frames[0].key, ("entity", 1))


class SaturationTests(unittest.TestCase):
    def test_an_incumbent_cannot_become_undisplaceable(self) -> None:
        """Additive evidence makes a long incumbency arithmetically impossible
        to beat, and the segmenter then produces one frame per day and calls it
        a success."""
        seg, _, _ = run(stream([(1, 300, 20), (2, 30, 20)]))
        self.assertGreaterEqual(len(roots(seg)), 2)


if __name__ == "__main__":       # pragma: no cover
    unittest.main()
