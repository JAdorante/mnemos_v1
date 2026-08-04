"""Plan 2.3 — entity-resolution golden set + merge-error gate."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "goldens" / "entity_resolution.jsonl"
GEN = ROOT / "scripts" / "gen_entity_resolution_golden.py"
EVAL = ROOT / "scripts" / "eval_entity_resolution.py"


def _ensure_golden() -> None:
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    subprocess.check_call([sys.executable, str(GEN)], cwd=str(ROOT))


def test_golden_fixture_shape():
    _ensure_golden()
    rows = [json.loads(l) for l in GOLDEN.read_text(encoding="utf-8").splitlines()
            if l.strip()]
    assert len(rows) >= 80, f"expected ≥80 cases, got {len(rows)}"
    cats = {r.get("category") for r in rows}
    for required in (
        "exact_match",
        "ambiguous_short",
        "create_new",
        "news_no_mint",
        "adversarial_near_miss",
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
    assert proc.returncode == 0, "eval_entity_resolution thresholds failed"


def test_people_v2_code_default_on():
    """Plan 2.3 gate green → code default QUILL_PEOPLE_V2=1."""
    import os
    from unittest.mock import patch
    from app.services import people_pipeline as pp
    env = {k: v for k, v in os.environ.items() if k != "QUILL_PEOPLE_V2"}
    with patch.dict(os.environ, env, clear=True):
        assert pp.enabled() is True
    with patch.dict(os.environ, {"QUILL_PEOPLE_V2": "0"}):
        assert pp.enabled() is False
