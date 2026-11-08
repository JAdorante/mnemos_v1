"""Inbox — one queue for every decision (frontend spec, migration step 2).

GET  /inbox                      the page (HTML) or the aggregate (JSON, by Accept)
GET  /inbox/count                {needs_you, can_wait} for the rail pill
GET  /inbox/handled?since=7d     {count, breakdown}
GET  /inbox/partials/{card}      one card's body, for sync refetches
POST /inbox/{item}/{action}      act through the item's existing verb
POST /inbox/{item}/act           no-JS form fallback (field: action)
POST /inbox/undo/{token}         within 10 s of an action that supports it
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.api import ui
from app.services import inbox as _inbox
from app.services.memory import memory

router = APIRouter()


def _worker():
    from app.api.routes import _agent_worker
    return _agent_worker()


def _store():
    return memory._ensure_store()


def _wants_html(request: Request) -> bool:
    if request.query_params.get("format") == "json":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _since_seconds(since: str | None) -> float:
    s = (since or "7d").strip().lower()
    try:
        if s.endswith("d"):
            return float(s[:-1]) * 86400
        if s.endswith("h"):
            return float(s[:-1]) * 3600
        return float(s)
    except ValueError:
        return 7 * 86400


def _crossed_7d(limit: int = 8) -> list[dict]:
    """Last 7 days of egress, newest first, for the "Crossed to the team" card."""
    try:
        from app.services.model_log import model_log
        inv = model_log.egress_inventory(recent=200)
    except Exception:
        return []
    since = time.time() - 7 * 86400
    rows = []
    for r in inv.get("recent") or []:
        try:
            t = float(r.get("time") or 0)
        except (TypeError, ValueError):
            continue
        if t < since:
            continue
        rows.append({
            "when": time.strftime("%b %-d", time.localtime(t)),
            "what": str(r.get("privacy_max") or r.get("task") or "memory").replace("egress.", ""),
            "to": str(r.get("destination") or r.get("provider") or "the team"),
            "by": "human" if r.get("approving_action") == "human" else "policy",
            "t": t,
        })
    rows.sort(key=lambda r: -r["t"])
    return rows[:limit]


@router.get("/inbox")
def inbox_page(request: Request):
    worker = _worker()
    data = _inbox.collect(_store(), worker=worker)
    if not _wants_html(request):
        return JSONResponse(data)
    return ui.render("inbox.html", path="/inbox", agent_worker=worker,
                     next_url="/inbox", inbox=data, rule=_inbox.rule_in_force(),
                     crossed=_crossed_7d())


@router.get("/inbox/count")
def inbox_count() -> dict:
    data = _inbox.collect(_store(), worker=_worker())
    return {"needs_you": len(data["needs_you"]), "can_wait": len(data["can_wait"])}


@router.get("/inbox/handled")
def inbox_handled(since: str = "7d") -> dict:
    return _inbox.handled(_store(), since_s=_since_seconds(since))


@router.get("/inbox/partials/{card}")
def inbox_partial(card: str) -> HTMLResponse:
    data = _inbox.collect(_store(), worker=_worker())
    if card == "queue":
        return ui.render_partial("partials/inbox_queue.html", inbox=data)
    if card == "handled":
        return ui.render_partial("partials/inbox_handled.html", inbox=data)
    raise HTTPException(status_code=404, detail="no such card")


@router.post("/inbox/undo/{token}")
def inbox_undo(token: str) -> JSONResponse:
    out = _inbox.undo(token)
    return JSONResponse(out, status_code=200 if out.get("ok") else 410)


@router.post("/inbox/{item_id}/act")
def inbox_act_form(item_id: str, action: str = Form(...)) -> RedirectResponse:
    """No-JS path: a real form post, then back to the inbox."""
    _inbox.act(_store(), item_id, action, worker=_worker())
    return RedirectResponse(url="/inbox", status_code=303)


@router.post("/inbox/{item_id}/{action}")
def inbox_act(item_id: str, action: str) -> JSONResponse:
    try:
        out = _inbox.act(_store(), item_id, action, worker=_worker())
    except HTTPException as exc:
        return JSONResponse({"ok": False, "error": str(exc.detail)}, status_code=exc.status_code)
    return JSONResponse(out, status_code=200 if out.get("ok") else 409)
