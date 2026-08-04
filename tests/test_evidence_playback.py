"""Plan 3.4 — evidence playback: fact→event→WAV + span highlight."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

NOW = 1_700_000_000.0


class EvidencePlaybackUnitTests(unittest.TestCase):
    def test_clip_prefers_enhanced(self):
        from app.services.evidence_playback import clip_from_meta

        c = clip_from_meta({
            "audio_path": "/data/audio/a.wav",
            "enhanced_audio_path": "/data/audio/a.enhanced.wav",
            "provenance": {"transcript": "hello world"},
        })
        self.assertEqual(c["play_path"], "/data/audio/a.enhanced.wav")
        self.assertEqual(c["audio_path"], "/data/audio/a.wav")
        self.assertEqual(c["transcript"], "hello world")

    def test_find_span_highlight(self):
        from app.services.evidence_playback import find_span

        hit = find_span(
            "David said the pilot plan is $49 a month today.",
            "pilot plan is $49 a month",
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit["match"].lower(), "pilot plan is $49 a month")
        self.assertIn("David said", hit["before"])
        self.assertIn("today", hit["after"])

    def test_find_span_missing(self):
        from app.services.evidence_playback import find_span

        self.assertIsNone(find_span("no money here", "$55"))


class EvidencePlaybackIntegrationTests(unittest.TestCase):
    def test_facts_list_attaches_playable_clip_and_span(self):
        from app.events import Event, Modality
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                wav = str(Path(td) / "clip.wav")
                Path(wav).write_bytes(b"RIFF....")
                eid = store.insert(Event(
                    time=NOW, modality=Modality.AUDIO,
                    raw="David said the pilot plan is $49 a month today.",
                    source="audio.whisper",
                    meta={
                        "audio_path": wav,
                        "enhanced_audio_path": wav,
                        "provenance": {
                            "transcript": (
                                "David said the pilot plan is $49 a month today."
                            ),
                        },
                    },
                ))
                fid = store.add_task(
                    "Follow up on $49 pilot",
                    confidence=0.9, extracted_at=NOW,
                    source_event_id=eid,
                    source_span="pilot plan is $49 a month",
                )
                with mock.patch(
                    "app.services.memory.memory._ensure_store",
                    return_value=store,
                ):
                    from app.api.routes import facts_list
                    out = facts_list(status="open", limit=50)
                facts = {f["fact_id"]: f for f in out["facts"]}
                self.assertIn(fid, facts)
                row = facts[fid]
                self.assertTrue(row["playable"])
                self.assertEqual(row["play_path"], wav)
                self.assertIsNotNone(row["span_highlight"])
                self.assertIn("49", row["span_highlight"]["match"])
            finally:
                store.close()

    def test_constellation_evidence_hydrates_playback(self):
        from app.events import Event, Modality
        from app.services import graph
        from app.storage import Store

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "t.db")
            try:
                wav = str(Path(td) / "moment.wav")
                Path(wav).write_bytes(b"RIFF....")
                eid = store.insert(Event(
                    time=NOW, modality=Modality.AUDIO,
                    raw="Send the demo link by Friday please.",
                    source="audio.whisper",
                    meta={"audio_path": wav},
                ))
                fid = store.add_task(
                    "Send the demo link",
                    confidence=0.95, extracted_at=NOW,
                    source_event_id=eid,
                    source_span="Send the demo link",
                )
                with mock.patch("time.time", return_value=NOW):
                    ev = graph.constellation_evidence(store, f"fact:{fid}")
                self.assertTrue(ev.get("ok"))
                sources = ev.get("sources") or []
                self.assertTrue(sources)
                s0 = sources[0]
                self.assertTrue(s0.get("playable"))
                self.assertEqual(s0.get("play_path"), wav)
                self.assertTrue(s0.get("span_highlight"))
                self.assertIn("demo link", (s0["span_highlight"].get("match") or "").lower())
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
