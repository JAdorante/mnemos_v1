"""Escalation distillation log — durable local→parent handoff records.

When the local VLM cannot handle a frame and Claude is invoked, we append one
JSONL row with both outputs (structured, no image bytes). That trail is the
substrate for later idle distillation / local improvement — separate from
`model_calls.jsonl` (cost/latency telemetry).

    escalate_log.record(
        task="vision.describe",
        reason="low_confidence",
        local={...}, parent={...},
        local_model="minicpm-v", parent_model="claude-opus-4-8",
        capture_quality=0.7, frame_path="data/frames/....jpg",
        source="desktop.screen", modality="vision",
    )
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import settings
from app.perception import redaction as _redaction

# Keys stamped by VLMRouter._tag — strip so distill rows stay clean payloads.
_INTERNAL = frozenset({"_provider", "_route"})

# Human verdicts a row may carry. "unknown" is the record()-time default and is
# never SET via set_user_outcome — labeling only moves a row forward.
_OUTCOMES = frozenset({"accepted", "rejected", "edited"})


def _clean_payload(res: dict | None) -> dict | None:
    if not res:
        return None
    # Redact secrets AND PII (email/phone) at the write boundary: this trail
    # feeds later LoRA training and console views, so neither a credential
    # nor a contact detail the models saw may persist here — whichever caller
    # (vision or text) recorded it. TIER_LOG = the durable-log tier
    # of app/perception/redaction.py.
    cleaned, _hits = _redaction.redact(
        {k: v for k, v in res.items() if k not in _INTERNAL},
        _redaction.TIER_LOG)
    return cleaned


class EscalateLog:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path = Path(settings.escalate_log.path)
        self._counts: Counter[str] = Counter()
        self._total = 0
        self._load_counts()

    @property
    def path(self) -> Path:
        return self._path

    def _load_counts(self) -> None:
        """Best-effort reason histogram from an existing trail (survives restart)."""
        if not self._path.is_file():
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    reason = str(row.get("reason") or "unknown")
                    self._counts[reason] += 1
                    self._total += 1
        except Exception as exc:
            print(f"[escalate_log] count reload skipped ({exc}).")

    def enabled(self) -> bool:
        return bool(settings.escalate_log.enabled)

    def record(
        self,
        *,
        task: str,
        reason: str,
        parent: dict[str, Any],
        local: dict[str, Any] | None = None,
        local_model: str | None = None,
        parent_model: str | None = None,
        capture_quality: float | None = None,
        frame_path: str | None = None,
        source: str | None = None,
        modality: str | None = None,
        local_error: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> dict | None:
        """Append one distill row. Never raises — logging must not break VLM."""
        if not self.enabled():
            return None
        row: dict[str, Any] = {
            # Stable row id so a later human verdict can target THIS row exactly
            # (older trails predate the id — matching falls back to frame_path/time).
            "id": uuid.uuid4().hex,
            "time": time.time(),
            "task": task,
            "reason": reason,
            "modality": modality or "vision",
            "source": source or "",
            "frame_path": frame_path or "",
            "capture_quality": capture_quality,
            "local_model": local_model or settings.vision.local_model,
            "parent_model": parent_model or settings.vision.model,
            "local": _clean_payload(local),
            "parent": _clean_payload(parent),
            # Filled later when a human accepts/rejects an offer (Part 1 leave open).
            "user_outcome": "unknown",
        }
        if local_error:
            row["local_error"], _ = _redaction.redact_text(
                str(local_error)[:500], _redaction.TIER_LOG)
        if meta:
            row["meta"], _ = _redaction.redact(meta, _redaction.TIER_LOG)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
            with self._lock:
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line)
                self._counts[reason] += 1
                self._total += 1
        except Exception as exc:
            print(f"[escalate_log] write skipped ({exc}).")
            return None
        return row

    def row_by_id(self, row_id: str) -> dict | None:
        """Fetch one distill row by its stable id — the join key between a chat
        verdict and the attention impressions recorded when the answer was
        grounded. Linear scan under the lock; the trail is small by design."""
        rid = (row_id or "").strip()
        if not rid or not self.enabled():
            return None
        try:
            with self._lock:
                if not self._path.is_file():
                    return None
                for ln in self._path.read_text(encoding="utf-8").splitlines():
                    if not ln.strip() or rid not in ln:
                        continue
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    if row.get("id") == rid:
                        return row
        except Exception as exc:
            print(f"[escalate_log] row_by_id skipped ({exc}).")
        return None

    def set_user_outcome(
        self,
        outcome: str,
        *,
        frame_path: str | None = None,
        source: str | None = None,
        time: float | None = None,
        window_s: float = 120.0,
        row_id: str | None = None,
        edited_text: str | None = None,
    ) -> bool:
        """Label a distill row with the human verdict (accepted|rejected|edited).

        `edited_text` (only meaningful with outcome="edited") stores the human's
        corrected text on the row as `edited` — the strongest training target we
        have, beating the parent's raw output. Stored untruncated on purpose.

        Matching, strongest key first:
          1. `row_id`   — the stable id record() stamps on every new row.
          2. `frame_path` — exact match; the frame is the natural join key between
             a VLM escalation and the offer/fact the user later judged.
          3. `source` + `time` — same source, |time - row.time| < window_s. Covers
             rows/callers that predate frame paths.
        When several rows match, only the MOST RECENT one is updated: repeated
        escalations of the same frame/source mean the newest parent output is the
        one the user actually saw and judged, so it's the one the verdict grounds.

        Update strategy: safe in-place rewrite under self._lock — read every line,
        patch the matching row's user_outcome, write a temp file in the same
        directory, then os.replace onto the trail. The file is small at prototype
        scale, and ONE canonical file (rather than a companion outcomes file to be
        joined later) keeps the training export trivial. Crash-safe: the original
        is only replaced after the temp file is fully written; any failure warns
        and leaves the trail intact.

        Returns True when a row was updated; False on no-match or any I/O failure
        (never raises for those — outcome labeling must not break the offer flow).
        Raises ValueError only for an outcome outside accepted|rejected|edited,
        which is a caller bug, not runtime data.
        """
        if outcome not in _OUTCOMES:
            raise ValueError(
                f"user_outcome must be one of {sorted(_OUTCOMES)}, got {outcome!r}")
        if not self.enabled():
            return False
        if not (row_id or frame_path or (source and time is not None)):
            return False   # nothing to match on
        try:
            with self._lock:
                if not self._path.is_file():
                    return False
                lines = self._path.read_text(encoding="utf-8").splitlines()
                rows: list[tuple[int, dict]] = []
                for i, ln in enumerate(lines):
                    if not ln.strip():
                        continue
                    try:
                        rows.append((i, json.loads(ln)))
                    except Exception:
                        continue

                def _last(pred) -> int | None:
                    # Rows are append-ordered, so the last hit is the most recent.
                    best = None
                    for i, row in rows:
                        try:
                            if pred(row):
                                best = i
                        except Exception:
                            continue
                    return best

                best: int | None = None
                if row_id:
                    best = _last(lambda r: r.get("id") == row_id)
                if best is None and frame_path:
                    best = _last(lambda r: bool(r.get("frame_path"))
                                 and r.get("frame_path") == frame_path)
                if best is None and source and time is not None:
                    best = _last(lambda r: r.get("source") == source
                                 and abs(float(r.get("time")) - float(time)) < window_s)
                if best is None:
                    return False
                patched = json.loads(lines[best])
                patched["user_outcome"] = outcome
                if outcome == "edited" and edited_text:
                    # Human-corrected text is a training target too — same
                    # redaction as the model payloads before it persists.
                    patched["edited"], _ = _redaction.redact_text(
                        edited_text, _redaction.TIER_LOG)
                lines[best] = json.dumps(patched, ensure_ascii=False, default=str)
                fd, tmp_name = tempfile.mkstemp(
                    dir=str(self._path.parent), suffix=".jsonl.tmp")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                        f.write("\n".join(lines) + "\n")
                    os.replace(tmp_name, self._path)
                except Exception:
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
                    raise
            return True
        except Exception as exc:
            print(f"[escalate_log] outcome update skipped ({exc}).")
            return False

    def stats(self, *, recent: int = 20) -> dict[str, Any]:
        """Aggregate for /console/escalate — counts by reason/outcome + recent rows.

        by_outcome is recomputed by scanning the file rather than kept as live
        instance state: set_user_outcome rewrites rows in place, so a cached
        counter would need careful sync. The trail is small at prototype scale —
        a lazy scan is cheap and can't drift.
        """
        recent_rows: list[dict] = []
        by_outcome: Counter[str] = Counter()
        if self._path.is_file():
            try:
                text = self._path.read_text(encoding="utf-8")
                lines = [ln for ln in text.splitlines() if ln.strip()]
                for i, ln in enumerate(lines):
                    try:
                        row = json.loads(ln)
                    except Exception:
                        continue
                    by_outcome[str(row.get("user_outcome") or "unknown")] += 1
                    if recent > 0 and i >= len(lines) - recent:
                        recent_rows.append(row)
            except Exception as exc:
                print(f"[escalate_log] stats read skipped ({exc}).")
        with self._lock:
            by_reason = dict(self._counts)
            total = self._total
        return {
            "enabled": self.enabled(),
            "path": str(self._path),
            "total": total,
            "by_reason": by_reason,
            "by_outcome": dict(by_outcome),
            "recent": recent_rows,
        }


escalate_log = EscalateLog()
