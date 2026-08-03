"""Typed, schema-versioned records for the perception subsystem.

Every table row carries `schema_version` so the Parquet export (Phase D) and
any later training pipeline can interpret old rows without guessing. Bump
SCHEMA_VERSION only with a matching migration step in store.py.

Timestamps are UTC **milliseconds** (int), monotonic-corrected at capture
time; `utc_offset_minutes` is recorded per session so local-time rendering is
possible without ever storing local time.
"""
from __future__ import annotations

import os
import time
from typing import Literal, Optional

from pydantic import BaseModel, Field

SCHEMA_VERSION = 1

# Crockford base32, as ULID uses (no I, L, O, U).
_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(ts_ms: int | None = None) -> str:
    """A ULID (26 chars, time-sortable). Local implementation — the repo has
    no ulid dependency and this needs os.urandom only."""
    t = int(ts_ms if ts_ms is not None else time.time() * 1000) & ((1 << 48) - 1)
    out = []
    for shift in range(45, -1, -5):
        out.append(_B32[(t >> shift) & 31])
    rand = int.from_bytes(os.urandom(10), "big")
    for shift in range(75, -1, -5):
        out.append(_B32[(rand >> shift) & 31])
    return "".join(out)


GapReason = Literal["process_down", "sleep", "user_pause", "privacy_excluded",
                    "crash"]
CaptureKind = Literal["full", "scroll_delta", "excluded", "vlm_only"]


class MetaEvent(BaseModel):
    """One L0 state-change record (or heartbeat). Input is COUNTS ONLY —
    key/mouse contents are never captured anywhere in this subsystem."""
    session_id: str
    seq: int
    ts_utc: int                          # UTC ms, monotonic-corrected
    utc_offset_minutes: int = 0
    app_name: str = ""
    app_exe_hash: str = ""
    window_id: str = ""
    window_title: str = ""
    browser_url: Optional[str] = None    # None = url_unavailable (honest)
    url_domain: Optional[str] = None     # registrable domain, stored separately
    doc_path: Optional[str] = None
    key_count: int = 0                   # since previous record
    mouse_count: int = 0
    is_idle: bool = False                # no input >= idle threshold
    display_hash: str = ""
    schema_version: int = SCHEMA_VERSION


class Gap(BaseModel):
    """An explicit hole in capture. Downstream never infers whether silence
    means 'nothing happened' or 'we were not looking' — a gap row says so."""
    ts_start: int
    ts_end: Optional[int] = None         # None = still open (pause in progress)
    reason: GapReason = "process_down"
    schema_version: int = SCHEMA_VERSION


class Capture(BaseModel):
    """One L1/L2 capture decision. Phase A writes kind='excluded'; B fills
    full/scroll_delta; C adds CAS frame/thumb SHAs + promotion/degradation."""
    capture_id: str = Field(default_factory=new_ulid)
    ts_utc: int = 0
    window_id: str = ""
    meta_event_id: Optional[int] = None
    kind: CaptureKind = "full"
    trigger: str = ""
    frame_sha256: Optional[str] = None
    thumb_sha256: Optional[str] = None
    ocr_engine: Optional[str] = None
    ocr_version: Optional[str] = None
    ocr_mean_conf: Optional[float] = None
    dropped_low_conf: int = 0
    redaction_hits: int = 0
    exclusion_rule: Optional[str] = None
    novel_line_count: int = 0
    total_line_count: int = 0
    promoted: bool = False
    degradation: str = "full"   # full | thumb | text | meta
    schema_version: int = SCHEMA_VERSION


class OcrLine(BaseModel):
    """One durable OCR line (content-addressed by line_hash per window)."""
    line_hash: str
    window_id: str
    first_capture_id: str
    text: str
    bbox_x: float = 0.0
    bbox_y: float = 0.0
    bbox_w: float = 0.0
    bbox_h: float = 0.0
    conf: float = 0.0
    schema_version: int = SCHEMA_VERSION


class ActivityBlock(BaseModel):
    """L3 contiguous activity segment (supersedes screen-side activities)."""
    block_id: str = Field(default_factory=new_ulid)
    ts_start: int
    ts_end: int
    dominant_app: str = ""
    dominant_domain: str = ""
    dominant_doc: str = ""
    input_intensity: float = 0.0
    capture_ids: list[str] = Field(default_factory=list)
    summary: str = ""
    schema_version: int = SCHEMA_VERSION


class Extraction(BaseModel):
    """One L3 typed candidate anchored to a capture (deduped)."""
    extraction_id: str = Field(default_factory=new_ulid)
    block_id: Optional[str] = None
    capture_id: str = ""
    type: str = ""
    payload_json: str = "{}"
    confidence: float = 0.0
    source_span: str = ""
    norm_span_key: str = ""
    model: str = ""
    model_version: str = ""
    egress: str = "local"
    ts_utc: int = 0
    schema_version: int = SCHEMA_VERSION


class SupervisionEvent(BaseModel):
    """Append-only supervision signal (training corpus, first-class)."""
    ts_utc: int
    kind: Literal["query", "query_click", "extraction_confirm",
                  "extraction_reject", "extraction_edit", "action_approved",
                  "action_rejected", "pin", "unpin", "exclusion_added",
                  "erasure"]
    target_type: str = ""
    target_id: str = ""
    payload_json: str = "{}"
    schema_version: int = SCHEMA_VERSION


def norm_span_key(span: str) -> str:
    """Normalize a verbatim span for L3 idempotency (not a raw hash)."""
    import re
    s = (span or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s[:500]


def now_ms() -> int:
    return int(time.time() * 1000)


def utc_offset_minutes() -> int:
    """Current UTC offset of the machine, minutes east of UTC."""
    lt = time.localtime()
    return int(lt.tm_gmtoff // 60) if hasattr(lt, "tm_gmtoff") else \
        -int((time.altzone if lt.tm_isdst else time.timezone) // 60)
