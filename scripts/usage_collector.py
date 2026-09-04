#!/usr/bin/env python
"""Operator-side collector for the opt-in weekly usage ping (WS-A tier 3).

Runs on the *operator's* machine, not a tester's. It appends each received
report to a directory that `scripts/pilot_metrics.py` then reads — one file per
POST, no database, no processing:

    pip install fastapi uvicorn
    QUILL_COLLECTOR_DIR=./pilot_reports \\
    QUILL_COLLECTOR_TOKEN=some-shared-secret \\
    uvicorn scripts.usage_collector:app --host 0.0.0.0 --port 8080

Then point testers' QUILL_USAGE_PING_URL at https://<host>/usage (they still
have to consent in the Privacy controls before anything is sent).

Deliberately dumb: it validates that the body is a `mnemos.usage/1` report,
caps the size, and writes it down. Anything richer belongs in pilot_metrics.py,
where it can be re-run over the whole folder.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Request

MAX_BYTES = 1_000_000            # a year of daily rows is ~100 KB
app = FastAPI(title="Sparrow pilot collector")


def _dir() -> Path:
    p = Path(os.environ.get("QUILL_COLLECTOR_DIR", "pilot_reports"))
    p.mkdir(parents=True, exist_ok=True)
    return p


@app.get("/health")
def health() -> dict:
    return {"ok": True, "dir": str(_dir()), "reports": len(list(_dir().glob("*.json")))}


@app.post("/usage")
async def collect(request: Request,
                  authorization: str | None = Header(default=None)) -> dict:
    want = (os.environ.get("QUILL_COLLECTOR_TOKEN") or "").strip()
    if want and (authorization or "") != f"Bearer {want}":
        raise HTTPException(401, "collector token required")
    body = await request.body()
    if len(body) > MAX_BYTES:
        raise HTTPException(413, "report too large")
    try:
        payload = json.loads(body)
    except Exception as exc:
        raise HTTPException(400, f"not JSON: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != "mnemos.usage/1":
        raise HTTPException(400, "not a mnemos.usage/1 report")
    iid = str(payload.get("install_id") or "unknown")[:64]
    safe = "".join(c for c in iid if c.isalnum() or c in "-_") or "unknown"
    day = str(payload.get("generated_at_day") or time.strftime("%Y-%m-%d"))[:10]
    # uuid suffix so a re-send never clobbers the earlier copy; pilot_metrics
    # merges duplicates by install id anyway.
    out = _dir() / f"usage-{safe}-{day}-{uuid.uuid4().hex[:8]}.json"
    out.write_bytes(body)
    return {"ok": True, "stored": out.name}
