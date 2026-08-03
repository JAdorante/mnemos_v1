"""Desktop perception subsystem (v1) — layered capture with honest gaps.

Layers (see PERCEPTION.md and MIGRATION.md):
  L0 metadata stream (always on)  -> perception.db, append-only
  L1 text (QUILL_PERCEPTION_L1)   -> OCR deltas + FTS5 + ocr_blocks
  L2 frames (QUILL_PERCEPTION_L2) -> CAS WebP full+thumb + compactor
  L3 semantics -> Phase D

Phase A is the safety floor. Phase B adds change-triggered OCR behind the
L1 flag. Phase C writes content-addressed frames beside L1 and compacts by
age/budget (pixels first, never text).
"""
