"""Replay ingest: land a historical corpus through the connector path.

Off unless QUILL_REPLAY_INGEST=1. Exists for the two-Sparrow interaction
test (scripts/two_sparrow/): every real text path stamps `now()`, so a
two-week timeline cannot be replayed with its own timestamps over HTTP.
This route takes connector-shaped items (same dicts a connector's
`fetch_items` returns) and lands them through `scheduler.land_unseen`, so
they get the same ledger, source class, salience and extraction nudge a
real sync pass gives them — only the clock is the item's own `ts`.

    POST /replay/items {"connector_id": "google", "items": [ {...}, ... ]}

`connector_id` must be one of REPLAY_CONNECTORS, registered connector ids,
so source_policy classes the rows as connector capture and the text lane
mines them (an `audio.*` source would be left to the audio lane and never
extracted, so transcripts replay as `notion.transcript` documents). Behind the LAN gate like
every other owner route; never on a pilot seat (the gb10 compose does not
set the flag, and tests/test_hosted_posture.py keeps it that way).
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

REPLAY_CONNECTORS = frozenset({"google", "slack", "outlook", "notion"})
MAX_ITEMS = 500


def enabled() -> bool:
    return os.environ.get("QUILL_REPLAY_INGEST", "0") in ("1", "true", "True")


class ReplayIn(BaseModel):
    connector_id: str = Field(..., min_length=1, max_length=40)
    items: list[dict[str, Any]] = Field(..., max_length=MAX_ITEMS)


@router.post("/replay/items")
def replay_items(body: ReplayIn) -> dict:
    if not enabled():
        raise HTTPException(status_code=404, detail="replay ingest is off")
    cid = body.connector_id.strip().lower()
    if cid not in REPLAY_CONNECTORS:
        raise HTTPException(status_code=422, detail=f"connector_id must be one of {sorted(REPLAY_CONNECTORS)}")
    from app.services.connectors import scheduler
    connector = SimpleNamespace(id=cid)
    landed = scheduler.land_unseen(connector, body.items)
    return {"ok": True, "connector_id": cid, "received": len(body.items), "landed": landed}


@router.get("/replay/status")
def replay_status() -> dict:
    return {"ok": True, "enabled": enabled(), "connectors": sorted(REPLAY_CONNECTORS)}
