"""Plan 2.2 — commitments/ownership golden set thresholds.

Regenerates the fixture if missing, then runs the offline eval harness
and asserts precision / ownership / empty-expect gates.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "goldens" / "commitments_ownership.jsonl"
GEN = ROOT / "scripts" / "gen_commitments_ownership_golden.py"
EVAL = ROOT / "scripts" / "eval_commitments_ownership.py"


def _ensure_golden() -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, str(GEN)], cwd=str(ROOT))


def test_golden_fixture_shape():
    _ensure_golden()
    rows = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(rows) >= 150, f"expected ≥150 cases, got {len(rows)}"
    cats = {r.get("category") for r in rows}
    for required in (
        "stated_commitment",
        "quoted_no_insert",
        "negated_no_insert",
        "hypothetical_no_insert",
        "two_speaker_ownership",
        "me_relative_self",
        "me_relative_other",
    ):
        assert required in cats, f"missing category {required}"


def test_offline_eval_thresholds():
    _ensure_golden()
    proc = subprocess.run(
        [sys.executable, str(EVAL)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    print(proc.stdout)
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
    assert proc.returncode == 0, "eval_commitments_ownership thresholds failed"
