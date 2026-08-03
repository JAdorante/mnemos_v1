"""Milestone 1 standalone demo — live transcription in your terminal.

    python run_audio.py

Speak into your mic; finalized utterances print as [transcript] lines and are
stored in the in-memory timeline. Ctrl+C to stop and print a session summary.
"""
from __future__ import annotations

from app.services.audio import AudioPipeline
from app.services.memory import memory


def main() -> None:
    memory.attach()  # capture transcripts into the timeline
    pipe = AudioPipeline()
    pipe.run_forever()
    events = memory.all()
    print(f"\n=== session over: {len(events)} utterance(s) captured ===")
    for e in events:
        print(f"  {e['time']:.0f}  {e['raw']}")


if __name__ == "__main__":
    main()
