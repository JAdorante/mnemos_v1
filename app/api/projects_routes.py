"""Projects page and its API (see services/projects.py).

  GET  /projects                      page
  GET  /projects/list                 every project, plus Miscellaneous
  GET  /projects/detail?name=         one project's shared records and my notes
  GET  /projects/search?q=            shared records across projects
  GET  /projects/records/{id}/history a shared record's earlier versions
  POST /projects/records/{id}/retract take a shared record off its project
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

router = APIRouter()


@router.get("/projects", response_class=HTMLResponse)
def projects_page() -> str:
    from app.api.projects_page import PROJECTS_PAGE
    return PROJECTS_PAGE


@router.get("/projects/list")
def projects_list() -> dict:
    from app.services import projects
    from app.services.records import org_client
    misc = len(projects.filed_notes(projects.MISC, limit=500))
    return {"projects": projects.registry(), "misc": projects.MISC,
            "misc_notes": misc, "in_org": org_client.joined()}


@router.get("/projects/detail")
def projects_detail(name: str, q: str = "") -> dict:
    from app.services import projects
    if not name.strip():
        raise HTTPException(400, "name required")
    return projects.context(name, query=q)


@router.get("/projects/search")
def projects_search(q: str) -> dict:
    from app.services import projects
    return projects.context(None, query=q)


@router.get("/projects/records/{record_id}/history")
def projects_record_history(record_id: str) -> dict:
    from app.services.records import org_client
    try:
        out = org_client.record_versions(record_id)
    except org_client.OrgRefused as exc:
        raise HTTPException(exc.status, exc.code) from exc
    except org_client.OrgUnavailable as exc:
        raise HTTPException(503, "org service unavailable") from exc
    rows = [v for v in out.get("versions") or [] if not v.get("derived_from")]
    return {"ok": True, "versions": [{
        "version": v.get("version"), "value": v.get("value_json"),
        "recorded_at": v.get("recorded_at"),
        "superseded_at": v.get("superseded_at")} for v in rows]}


@router.post("/projects/records/{record_id}/retract")
def projects_record_retract(record_id: str, request: Request) -> dict:
    from app.api.records_routes import live_session
    from app.services.records import org_client
    ok, why = live_session(request)
    if not ok:
        raise HTTPException(403, why)
    try:
        return org_client.retract_record(record_id)
    except org_client.OrgRefused as exc:
        raise HTTPException(exc.status, exc.detail or exc.code) from exc
    except org_client.OrgUnavailable as exc:
        raise HTTPException(503, "org service unavailable") from exc
