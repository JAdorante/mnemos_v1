"""Mnemos — laptop-based multimodal memory prototype.

Architecture:  Multimodal Capture -> Memory Engine -> Agent Layer

Milestone status:
  [x] M1  Live audio pipeline   (mic -> VAD -> Whisper -> transcript)
  [ ] M2  Live vision pipeline  (webcam -> frame selection -> vision model)
  [ ] M3  Persistent memory     (transcripts/images/embeddings/entities/tasks)
  [ ] M4  Voice conversation    (TTS + spoken Q&A over memory)
  [ ] M5  Knowledge graph       (people/orgs/meetings/commitments over time)
  [ ] M6  Agent layer           (draft emails, schedule, CRM — with approval)
"""

import os as _os

# Quiet noisy first-run warnings from the HF hub cache on Windows.
_os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
_os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

__version__ = "0.1.0"
