"""Atomic JSON file writes — crash-safe replace for small state files."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def write_json(path: Path | str, data: Any, *, indent: int | None = 2,
               sort_keys: bool = False) -> None:
    """Write JSON via temp file + os.replace so readers never see a truncate."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, indent=indent, sort_keys=sort_keys)
    if not payload.endswith("\n"):
        payload += "\n"
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".json.tmp",
                               prefix=p.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(payload)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
