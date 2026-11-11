"""Team search API (see services/team_search.py and services/team_index.py).

  GET  /team/search?q=          the central index's answer (Connected)
  POST /team/search/ask         {q}: ask each teammate's Sparrow (any rung;
                                a person sends it; approvals may be needed)
  GET  /team/index              this seat's pointers: setting, mode, counts
  POST /team/index/sync         rebuild / publish / withdraw now
  POST /team/index/projects     {project, shared}: stop or resume sharing my
                                items in one shared project
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()


class AskIn(BaseModel):
    q: str


class ShareIn(BaseModel):
    project: str
    shared: bool


@router.get("/team/search")
def team_search(q: str = "") -> dict:
    from app.services import team_search as ts
    if not q.strip():
        raise HTTPException(400, "q required")
    return ts.search(q)


@router.post("/team/search/ask")
def team_search_ask(body: AskIn) -> dict:
    from app.services import team_search as ts
    return ts.ask_teammates(body.q)


@router.get("/team/index")
def team_index_status() -> dict:
    from app.services import team_index
    return team_index.status()


@router.post("/team/index/sync")
def team_index_sync() -> dict:
    from app.services import team_index
    return team_index.sync()


@router.post("/team/index/projects")
def team_index_projects(body: ShareIn) -> dict:
    from app.services import team_index
    out = team_index.set_project_shared(body.project, body.shared)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error") or "not found")
    return out
