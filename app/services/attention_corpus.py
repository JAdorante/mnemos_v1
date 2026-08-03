"""Attention golden corpus — Phase 0 evaluation freeze.

The Cognitive OS P0 exit requires a frozen set of ≥50 annotated
recall / anticipation cases (including misses), layered on top of the
existing GravityGoldenTests. This module owns the schema, load path, and
status report the /console harness shows.

Cases are *contracts* for later ranking cutovers (A2–A4): each says, in a
given context, which node the user needed and whether the field had it.
They do not change live ranking. Real ledger rows can be appended via
`scripts/freeze_attention_corpus.py` without rewriting the frozen core.

Schema (one JSON object per line):
  id            stable case id (recall-001, miss-014, …)
  kind          recall | miss | anticipation | engagement
  query         what the user asked / needed (natural language)
  needed        {type: person|entity|fact, name|text: …}
  field_at_ask  list of node labels present on the field at ask time
  expect        hit | miss | warm | engage
  context       optional {mode, calendar_next, app}
  notes         short why-this-case-matters
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CORPUS_PATH = REPO_ROOT / "data" / "bench" / "attention" / "golden.jsonl"
MANIFEST_PATH = REPO_ROOT / "data" / "bench" / "attention" / "MANIFEST.json"

KINDS = frozenset({"recall", "miss", "anticipation", "engagement"})
EXPECTS = frozenset({"hit", "miss", "warm", "engage"})
NEEDED_TYPES = frozenset({"person", "entity", "fact"})

# P0 exit floor from cognitive_os_v2_roadmap.md Month-1.
MIN_CASES = 50
MIN_MISSES = 10
MIN_ANTICIPATION = 8


def load_cases(path: Path | None = None) -> list[dict[str, Any]]:
    """Load the frozen corpus. Raises on malformed lines."""
    p = path or CORPUS_PATH
    cases: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p.name}:{i}: bad JSON ({exc})") from exc
            cases.append(row)
    return cases


def validate_case(case: dict[str, Any], *, idx: int = 0) -> list[str]:
    """Return a list of schema problems (empty = ok)."""
    errs: list[str] = []
    prefix = f"case[{idx}]"
    if not isinstance(case, dict):
        return [f"{prefix}: not an object"]
    if not (case.get("id") or "").strip():
        errs.append(f"{prefix}: missing id")
    kind = case.get("kind")
    if kind not in KINDS:
        errs.append(f"{prefix}: kind must be one of {sorted(KINDS)}")
    if not (case.get("query") or "").strip():
        errs.append(f"{prefix}: missing query")
    needed = case.get("needed") or {}
    if not isinstance(needed, dict) or needed.get("type") not in NEEDED_TYPES:
        errs.append(f"{prefix}: needed.type must be person|entity|fact")
    elif not (needed.get("name") or needed.get("text") or "").strip():
        errs.append(f"{prefix}: needed needs name or text")
    if not isinstance(case.get("field_at_ask"), list):
        errs.append(f"{prefix}: field_at_ask must be a list")
    expect = case.get("expect")
    if expect not in EXPECTS:
        errs.append(f"{prefix}: expect must be one of {sorted(EXPECTS)}")
    # Consistency: miss cases expect miss; recall with needed in field → hit.
    field = [str(x) for x in (case.get("field_at_ask") or [])]
    label = _needed_label(needed) if isinstance(needed, dict) else ""
    if expect == "miss" and label and any(_label_match(label, f) for f in field):
        errs.append(f"{prefix}: expect=miss but needed appears in field_at_ask")
    if expect == "hit" and label and not any(_label_match(label, f) for f in field):
        errs.append(f"{prefix}: expect=hit but needed absent from field_at_ask")
    return errs


def _needed_label(needed: dict) -> str:
    t = needed.get("type") or ""
    name = (needed.get("name") or needed.get("text") or "").strip()
    return f"{t}:{name}".lower()


def _label_match(needed_label: str, field_label: str) -> bool:
    a = needed_label.lower().strip()
    b = (field_label or "").lower().strip()
    if a == b:
        return True
    # Allow "person:Marc" to match "person:Marc Chen" and vice versa.
    if ":" in a and ":" in b:
        ta, na = a.split(":", 1)
        tb, nb = b.split(":", 1)
        if ta == tb and (na in nb or nb in na):
            return True
    return False


def validate(cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Full corpus gate: schema + floor counts. `ok` is the P0 exit bit."""
    cases = cases if cases is not None else load_cases()
    errors: list[str] = []
    for i, c in enumerate(cases):
        errors.extend(validate_case(c, idx=i))
    ids = [c.get("id") for c in cases]
    if len(ids) != len(set(ids)):
        errors.append("duplicate case ids")
    by_kind: dict[str, int] = {}
    for c in cases:
        by_kind[c.get("kind") or "?"] = by_kind.get(c.get("kind") or "?", 0) + 1
    miss_n = sum(1 for c in cases if c.get("expect") == "miss"
                 or c.get("kind") == "miss")
    ant_n = by_kind.get("anticipation", 0)
    if len(cases) < MIN_CASES:
        errors.append(f"need ≥{MIN_CASES} cases, have {len(cases)}")
    if miss_n < MIN_MISSES:
        errors.append(f"need ≥{MIN_MISSES} miss cases, have {miss_n}")
    if ant_n < MIN_ANTICIPATION:
        errors.append(f"need ≥{MIN_ANTICIPATION} anticipation cases, have {ant_n}")
    return {
        "ok": not errors,
        "n": len(cases),
        "by_kind": by_kind,
        "misses": miss_n,
        "anticipation": ant_n,
        "path": str(CORPUS_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
        "errors": errors,
        "frozen": MANIFEST_PATH.is_file(),
        "manifest": _read_manifest(),
    }


def status() -> dict[str, Any]:
    """Dashboard-facing corpus status (never raises)."""
    try:
        return validate()
    except Exception as exc:
        return {"ok": False, "n": 0, "errors": [str(exc)], "frozen": False}


def _read_manifest() -> dict[str, Any] | None:
    if not MANIFEST_PATH.is_file():
        return None
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def write_manifest(*, n: int, by_kind: dict, notes: str = "") -> dict:
    """Stamp the corpus as frozen for the P0 exit."""
    import time
    manifest = {
        "frozen_at": time.strftime("%Y-%m-%d"),
        "frozen_ts": time.time(),
        "n": n,
        "by_kind": by_kind,
        "min_cases": MIN_CASES,
        "notes": notes or "P0 attention golden corpus — layered on GravityGoldenTests.",
        "path": str(CORPUS_PATH.relative_to(REPO_ROOT)).replace("\\", "/"),
    }
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n",
                             encoding="utf-8")
    return manifest
