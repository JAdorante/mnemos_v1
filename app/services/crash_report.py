"""Local crash/feedback zip for the tester cohort (Workstream 4.4).

No telemetry phones home. The zip is written under data/logs/ for the tester
to send. API keys and personal-class strings are redacted first.
"""
from __future__ import annotations

import io
import json
import os
import re
import time
import zipfile
from pathlib import Path
from typing import Any

_KEY_RE = re.compile(r"(sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,})")
_PERSONAL_RE = re.compile(
    r"\b(health|diagnos\w*|medic\w*|family|spouse|ssn|password|salary|therap\w*)\b",
    re.I,
)


def logs_dir() -> Path:
    data = os.environ.get("QUILL_DATA_DIR") or "data"
    p = Path(data) / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _redact(text: str) -> str:
    from app.services import redact
    out = _KEY_RE.sub("[REDACTED_KEY]", text or "")
    try:
        out = redact.redact_text(out)
    except Exception:
        pass
    # Drop lines that look personal-classed so the zip is safe to email.
    kept = []
    for line in out.splitlines():
        if _PERSONAL_RE.search(line):
            kept.append("[redacted personal-class line]")
        else:
            kept.append(line)
    return "\n".join(kept)


def write_report(*, note: str = "") -> dict[str, Any]:
    root = Path(os.environ.get("QUILL_DATA_DIR") or "data")
    buf = io.BytesIO()
    included: list[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("note.txt", _redact(note or "(no note)"))
        # WS-C: stamp the build so a tester report is attributable to one.
        from app.version import __version__
        import platform as _platform
        zf.writestr("manifest.json", json.dumps({
            "app_version": __version__,
            "os": _platform.system(),
            "created_at": time.time(),
        }, indent=2))
        for name in ("model_calls.jsonl", "escalate_distill.jsonl",
                     "first_run.json", "capture_consent.json"):
            p = root / name
            if p.is_file():
                zf.writestr(name, _redact(p.read_text(encoding="utf-8", errors="replace")[-400_000:]))
                included.append(name)
        log_root = logs_dir()
        for p in sorted(log_root.glob("*.log"))[-8:]:
            zf.writestr(f"logs/{p.name}", _redact(
                p.read_text(encoding="utf-8", errors="replace")[-200_000:]))
            included.append(p.name)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = logs_dir() / f"mnemos-report-{stamp}.zip"
    out.write_bytes(buf.getvalue())
    return {"ok": True, "path": str(out), "files": included}
