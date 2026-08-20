"""Adoption-readiness HTTP surface (meeting-first, exhaust, MCP, tester).

Kept out of routes.py so the September workstreams don't fight a 7k-line file.
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

router = APIRouter()


class AmbientIn(BaseModel):
    mic: bool | None = None
    webcam: bool | None = None
    desktop: bool | None = None


class MeetingListenIn(BaseModel):
    consent: bool = True


class ExhaustRunIn(BaseModel):
    refresh: bool = False


class McpToolIn(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ApiKeyIn(BaseModel):
    key: str
    provider: str = "anthropic"


class ReportIn(BaseModel):
    note: str = ""


class ExternalIn(BaseModel):
    text: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    speaker_hint: str | None = None
    device_id: str | None = None
    kind: str = "omi"
    confidence: float | None = None


@router.get("/first-run/status")
def first_run_status() -> dict:
    from app.services import first_run
    return first_run.status()


@router.post("/first-run/meeting-listen")
def first_run_meeting_listen(body: MeetingListenIn) -> dict:
    from app.services import first_run
    return first_run.save({"meeting_listen_consent": body.consent})


@router.post("/first-run/ambient")
def first_run_ambient(body: AmbientIn) -> dict:
    from app.services import first_run
    sources = {k: v for k, v in body.model_dump().items() if v is not None}
    return first_run.set_ambient_opt_in(sources)


@router.get("/first-run/nudge")
def first_run_nudge() -> dict:
    from app.services import first_run
    pend = first_run.load().get("pending_first_win")
    unlock = first_run.unlock_card()
    return {"first_win": pend, "unlock": unlock}


@router.post("/first-run/nudge/ack")
def first_run_nudge_ack() -> dict:
    from app.services import first_run
    win = first_run.consume_first_win()
    return {"ok": True, "consumed": win}


@router.post("/first-run/unlock/ack")
def first_run_unlock_ack() -> dict:
    from app.services import first_run
    return first_run.mark_unlock_shown()


@router.get("/exhaust/status")
def exhaust_status() -> dict:
    from app.services import exhaust_ingest as ex
    return ex.status()


@router.post("/exhaust/connect")
def exhaust_connect() -> dict:
    from app.services import exhaust_ingest as ex
    return ex.start_oauth_loopback()


def _exhaust_worker():
    from app.services import exhaust_ingest as ex
    ex.run_ingest(fetch=True)


@router.post("/exhaust/refresh")
def exhaust_refresh() -> dict:
    from app.services import exhaust_ingest as ex
    if ex.progress().get("running"):
        return {"ok": True, "running": True, **ex.progress()}
    t = threading.Thread(target=_exhaust_worker, name="exhaust-ingest", daemon=True)
    t.start()
    return {"ok": True, "started": True}


@router.post("/exhaust/purge")
def exhaust_purge() -> dict:
    from app.services import exhaust_ingest as ex
    return ex.purge()


@router.get("/mcp/tools")
def mcp_tools(authorization: str | None = Header(default=None)) -> dict:
    from app.services import mcp_tools
    if not mcp_tools.check_token(authorization):
        # Loopback without a token is allowed for the local stdio server on first run.
        from app.services.api_auth import bind_is_loopback
        if not bind_is_loopback():
            raise HTTPException(401, "mcp token required")
    return {"tools": mcp_tools.tool_schemas(), "write_tools": False,
            "action_tools": False}


@router.post("/mcp/tool")
def mcp_tool(body: McpToolIn, authorization: str | None = Header(default=None)) -> dict:
    from app.services import mcp_tools
    from app.services.api_auth import bind_is_loopback
    if not mcp_tools.check_token(authorization) and not bind_is_loopback():
        raise HTTPException(401, "mcp token required")
    return mcp_tools.call_tool(body.name, body.arguments)


@router.get("/mcp/token")
def mcp_token_info() -> dict:
    """Local-only: path to the token file (never the token itself over LAN)."""
    from app.services import mcp_tools
    from app.services.api_auth import bind_is_loopback
    if not bind_is_loopback():
        raise HTTPException(403, "token path is loopback-only")
    p = mcp_tools.ensure_token()
    return {"ok": True, "path": str(mcp_tools.token_path()), "configured": bool(p)}


@router.get("/onboarding/parent-model")
def onboarding_parent_model() -> dict:
    """Provider roster for the setup picker + which one is connected."""
    from app.services import parent_model
    return parent_model.status()


@router.post("/onboarding/api-key")
def onboarding_api_key(body: ApiKeyIn) -> dict:
    """Connect the parent model account: validate the key live against the
    chosen provider (Anthropic/OpenAI/Gemini/Grok), then persist provider +
    key to .credentials.env — never persist an unproven key."""
    from app.services import parent_model
    pid = (body.provider or "anthropic").strip().lower()
    if pid not in parent_model.PROVIDERS:
        raise HTTPException(400, f"unknown provider {pid!r}")
    key = (body.key or "").strip()
    err = parent_model.validate_key(pid, key)
    if err:
        raise HTTPException(400, err)
    path = parent_model.save(pid, key)
    return {"ok": True, "path": path, "provider": pid}


@router.post("/console/report")
def console_report(body: ReportIn) -> dict:
    from app.services import crash_report
    return crash_report.write_report(note=body.note)


@router.post("/capture/external")
def capture_external(
    body: ExternalIn,
    authorization: str | None = Header(default=None),
) -> dict:
    from app.services import external_capture
    if not external_capture.enabled():
        raise HTTPException(404, "QUILL_EXTERNAL_CAPTURE=0")
    device = external_capture.authenticate(authorization)
    if device is None:
        raise HTTPException(401, "pairing token required")
    payload = body.model_dump()
    return external_capture.ingest_transcript(device, payload)


_PWA = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mnemos capture</title>
<style>
body{font:16px/1.45 system-ui;margin:24px;max-width:420px}
button{font:inherit;padding:12px 18px;border-radius:10px;border:0;background:#0b1320;color:#f8f6f1}
#log{white-space:pre-wrap;color:#555;margin-top:16px;font-size:13px}
</style></head><body>
<h1>Phone as mic</h1>
<p>Records short clips and posts transcripts to this Mnemos. Pair the phone first.</p>
<label>Token <input id="tok" style="width:100%"></label>
<button id="go" type="button">Record 8s</button>
<div id="log"></div>
<script>
const log=t=>document.getElementById('log').textContent+=t+'\\n';
document.getElementById('go').onclick=async()=>{
  const tok=document.getElementById('tok').value.trim();
  if(!tok){log('need pairing token');return;}
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});
  const rec=new MediaRecorder(stream);
  const chunks=[];
  rec.ondataavailable=e=>chunks.push(e.data);
  rec.start();
  log('recording…');
  await new Promise(r=>setTimeout(r,8000));
  rec.stop();
  stream.getTracks().forEach(t=>t.stop());
  await new Promise(r=>rec.onstop=r);
  // v1: browsers can't whisper locally — send a placeholder until we add
  // on-device speech. Testers should dictate into a notes shortcut instead.
  const text=prompt('Transcript of what you just said (v1 types it):','');
  if(!text) return;
  const r=await fetch('/capture/external',{
    method:'POST',
    headers:{'Content-Type':'application/json','Authorization':'Bearer '+tok},
    body:JSON.stringify({text, started_at:Date.now()/1000-8, ended_at:Date.now()/1000, kind:'phone'})
  });
  log(await r.text());
};
</script>
</body></html>
"""


@router.get("/capture/pwa", response_class=HTMLResponse)
def capture_pwa() -> HTMLResponse:
    return HTMLResponse(_PWA)


class ConfirmIn(BaseModel):
    state: str = "contact"


@router.post("/people/{person_id}/confirm")
def people_confirm(person_id: int, body: ConfirmIn | None = None) -> dict:
    """Promote an exhaust-seeded candidate so they stay in the People list."""
    from app.services.memory import memory
    store = memory._ensure_store()
    if store.get_person(person_id) is None:
        raise HTTPException(404, "no such person")
    state = (body.state if body else "contact") or "contact"
    if state not in ("contact", "candidate", "archived"):
        raise HTTPException(400, "state must be contact|candidate|archived")
    store.set_person_promotion(person_id, state)
    return {"ok": True, "person_id": person_id, "promotion_state": state}


@router.get("/help/mcp")
def help_mcp():
    from pathlib import Path
    from fastapi.responses import FileResponse
    p = Path(__file__).resolve().parents[2] / "docs" / "mcp.md"
    if not p.is_file():
        raise HTTPException(404, "docs/mcp.md missing")
    return FileResponse(p, media_type="text/markdown; charset=utf-8")


@router.get("/help/trust")
def help_trust():
    from pathlib import Path
    from fastapi.responses import FileResponse
    p = Path(__file__).resolve().parents[2] / "docs" / "trust-layer.md"
    if not p.is_file():
        raise HTTPException(404, "docs/trust-layer.md missing")
    return FileResponse(p, media_type="text/markdown; charset=utf-8")


_bootstrap_lock = threading.Lock()
_bootstrap_state: dict[str, Any] = {"running": False, "log": [], "ok": None}


def _bootstrap_worker():
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    with _bootstrap_lock:
        _bootstrap_state.update({"running": True, "log": [], "ok": None})
    steps = [
        ([sys.executable, str(root / "scripts" / "download_models.py")], "models"),
        ([sys.executable, "-m", "playwright", "install", "chromium"], "chromium"),
    ]
    ok = True
    for cmd, label in steps:
        with _bootstrap_lock:
            _bootstrap_state["log"].append(f"starting {label}…")
        try:
            r = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True,
                               timeout=3600)
            tail = (r.stdout or r.stderr or "")[-400:]
            with _bootstrap_lock:
                _bootstrap_state["log"].append(f"{label}: exit {r.returncode} {tail}")
            if r.returncode != 0:
                ok = False
        except Exception as exc:
            with _bootstrap_lock:
                _bootstrap_state["log"].append(f"{label} failed: {exc}")
            ok = False
    with _bootstrap_lock:
        _bootstrap_state.update({"running": False, "ok": ok})


@router.get("/bootstrap")
def bootstrap_page() -> HTMLResponse:
    html = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Mnemos — first launch</title>
<style>body{font:16px/1.45 system-ui;margin:32px;max-width:520px}
button{font:inherit;padding:10px 16px;border-radius:10px;border:0;background:#0b1320;color:#f8f6f1}
#log{white-space:pre-wrap;color:#555;margin-top:16px;font-size:13px}</style></head>
<body>
<h1>Download speech models</h1>
<p>Mnemos does not ship the 460MB speech models or Chromium. This runs once, locally, and can be resumed.</p>
<button id="go" type="button">Download now</button>
<p><a href="/onboarding">Skip to setup</a> if models are already cached.</p>
<div id="log"></div>
<script>
const log=t=>document.getElementById('log').textContent=t;
async function poll(){
  const s=await (await fetch('/bootstrap/status')).json();
  log((s.log||[]).join('\\n')||'working…');
  if(s.running){setTimeout(poll,1500);return;}
  if(s.ok) location.href='/onboarding';
}
document.getElementById('go').onclick=async()=>{
  await fetch('/bootstrap/start',{method:'POST'});
  poll();
};
</script></body></html>"""
    return HTMLResponse(html)


@router.get("/bootstrap/status")
def bootstrap_status() -> dict:
    with _bootstrap_lock:
        return dict(_bootstrap_state)


@router.post("/bootstrap/start")
def bootstrap_start() -> dict:
    with _bootstrap_lock:
        if _bootstrap_state.get("running"):
            return {"ok": True, "running": True}
    t = threading.Thread(target=_bootstrap_worker, name="bootstrap", daemon=True)
    t.start()
    return {"ok": True, "started": True}
