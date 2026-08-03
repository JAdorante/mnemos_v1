"""Milestone 2 standalone demo — live webcam understanding in your terminal.

    python run_vision.py

Watches your webcam; when the scene changes (or every ~30s) it captures a frame,
sends it to Claude vision, and stores a structured VISION event. Ctrl+C to stop
and print a summary. Requires ANTHROPIC_API_KEY (or an `ant auth login` profile)
for descriptions — without it, frames are still captured and saved.
"""
from __future__ import annotations

from app.services.memory import memory
from app.services.vision import VisionPipeline


def main() -> None:
    memory.attach()
    pipe = VisionPipeline()
    pipe.run_forever()
    events = memory.all()
    vis = [e for e in events if e["modality"] == "vision"]
    print(f"\n=== session over: {len(vis)} frame(s) captured ===")
    for e in vis:
        print(f"  {e['time']:.0f}  {e['summary']}")


if __name__ == "__main__":
    main()
