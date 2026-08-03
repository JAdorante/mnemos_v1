"""Anticipation defaults stay quiet — no bare Open-app spam."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from app.services import anticipation as ant


def _act(app: str, end: float, start: float | None = None) -> dict:
    return {
        "app": app,
        "start": start if start is not None else end - 60,
        "end": end,
    }


class QuietAnticipationTests(unittest.TestCase):
    def _cfg(self, **over) -> SimpleNamespace:
        base = dict(
            enabled=True,
            min_conf=0.75,
            cooldown_s=900,
            consider_cooldown_s=60,
            idle_s=120,
            history=40,
            min_activities=4,
            min_transition_count=3,
            max_offers=1,
            offer_open_app=False,
        )
        base.update(over)
        return SimpleNamespace(**base)

    def test_bare_open_app_suppressed(self) -> None:
        now = time.time()
        # notepad → cursor three times; currently idle in notepad.
        acts = []
        t = now - 1000
        for _ in range(3):
            acts.append(_act("notepad", t + 50, t))
            acts.append(_act("cursor", t + 100, t + 50))
            t += 120
        acts.append(_act("notepad", now - 200, now - 260))  # idle > 120s
        # recent_activities is newest-first
        acts_newest_first = list(reversed(acts))

        store = mock.Mock()
        store.recent_activities.return_value = acts_newest_first
        store.list_facts.return_value = []

        with mock.patch.object(ant, "settings",
                               SimpleNamespace(anticipation=self._cfg())):
            cands = ant.score_candidates(store, now=now)
        self.assertEqual(cands, [])

    def test_open_app_opt_in_offers(self) -> None:
        now = time.time()
        acts = []
        t = now - 1000
        for _ in range(3):
            acts.append(_act("notepad", t + 50, t))
            acts.append(_act("cursor", t + 100, t + 50))
            t += 120
        acts.append(_act("notepad", now - 200, now - 260))
        store = mock.Mock()
        store.recent_activities.return_value = list(reversed(acts))
        store.list_facts.return_value = []

        with mock.patch.object(
            ant, "settings",
            SimpleNamespace(anticipation=self._cfg(offer_open_app=True,
                                                    min_conf=0.5)),
        ):
            cands = ant.score_candidates(store, now=now)
        self.assertTrue(cands)
        self.assertTrue(cands[0]["goal"].lower().startswith("open "))

    def test_task_matched_still_offered(self) -> None:
        now = time.time()
        acts = []
        t = now - 1000
        for _ in range(3):
            acts.append(_act("notepad", t + 50, t))
            acts.append(_act("cursor", t + 100, t + 50))
            t += 120
        acts.append(_act("notepad", now - 200, now - 260))
        store = mock.Mock()
        store.recent_activities.return_value = list(reversed(acts))
        store.list_facts.return_value = [
            {"text": "Finish the Cursor PR review", "fact_id": 42},
        ]

        with mock.patch.object(ant, "settings",
                               SimpleNamespace(anticipation=self._cfg(min_conf=0.5))):
            cands = ant.score_candidates(store, now=now)
        self.assertTrue(cands)
        self.assertIn("Cursor PR", cands[0]["goal"])
        self.assertEqual(cands[0]["fact_id"], 42)

    def test_shipped_defaults_in_source(self) -> None:
        from pathlib import Path
        text = (Path(__file__).resolve().parents[1] / "app" / "config.py"
                ).read_text(encoding="utf-8")
        self.assertIn('QUILL_ANTICIPATE_MIN_CONF", "0.75"', text)
        self.assertIn('QUILL_ANTICIPATE_OPEN_APP", "0"', text)


if __name__ == "__main__":
    unittest.main()
