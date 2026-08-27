"""Adoption-readiness HTTP surface (meeting-first, exhaust, MCP, tester).

Kept out of routes.py so the September workstreams don't fight a 7k-line file.
"""
from __future__ import annotations

import threading
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.api.mnemos_theme import apply_plain as _plain

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
    from app.services import first_run, meeting_enhance
    from app.services.memory import memory
    pend = first_run.load().get("pending_first_win")
    if isinstance(pend, dict) and pend:
        # Re-resolve at serve time so a stale /meeting/note/{id} (e.g. from a
        # test DB that leaked into first_run.json) never 404s the toast.
        try:
            store = memory._ensure_store()
            sid = pend.get("session_id")
            href = meeting_enhance.note_href_for_session(
                store, int(sid) if sid is not None else None)
            pend = {**pend, "href": href}
        except Exception:
            pend = {**pend, "href": pend.get("href") or "/meetings"}
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


class InviteIn(BaseModel):
    code: str


@router.get("/onboarding/invite")
def onboarding_invite_status() -> dict:
    """Whether this build has an invite service, so the UI can offer the path."""
    from app.services import invite
    return {"ok": True, "configured": bool(invite.vending_url())}


@router.post("/onboarding/invite")
def onboarding_invite(body: InviteIn) -> dict:
    """Redeem an invite code into this machine's .credentials.env (WS-D T1).

    The vended key is written exactly where a pasted key goes and is used the
    same way — the BYO path is untouched, and so is the local-first story.
    """
    from app.services.invite import InviteError, redeem_and_save
    try:
        return redeem_and_save(body.code)
    except InviteError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/console/report")
def console_report(body: ReportIn) -> dict:
    from app.services import crash_report
    return crash_report.write_report(note=body.note)


# --- data export & backup (WS-B) -------------------------------------------
# Both endpoints stream: a data directory can be far larger than RAM (the
# 107 GB incident), so the zip is generated into the response and never held.

@router.get("/export/status")
def export_status() -> dict:
    """Last-backup timestamp + whether a backup would fit on this disk."""
    from app.services import export
    return export.status()


def _zip_response(chunks, filename: str):
    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        chunks, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _stream_or_400(make_iter, filename: str):
    """Pull the first chunk eagerly so a refused export is a clean 400.

    Once a StreamingResponse has started there is no way back to an error
    status — the user would get a truncated zip instead of "not enough disk".
    """
    from app.services.export import ExportError
    try:
        it = make_iter()
        first = next(it, b"")
    except ExportError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(500, f"export failed: {exc}") from exc

    def chunks():
        yield first
        yield from it

    return _zip_response(chunks(), filename)


@router.api_route("/export/backup", methods=["GET", "POST"])
def export_backup():
    """Streamed zip of the data directory — restorable with restore_backup.py.

    GET is allowed so the Privacy controls button can be a plain link (a
    fetch() cannot save a multi-GB stream to disk); it reads and copies, and
    changes nothing.
    """
    from app.services import export
    name = export.suggested_name("backup")
    # Stamped when the backup starts, not when the browser finishes saving it:
    # the Privacy controls line is "last backup taken", and a stream we cannot
    # observe to completion is not something to report as unfinished.
    resp = _stream_or_400(lambda: export.backup_stream(), name)
    export.record_backup(name)
    return resp


@router.api_route("/export/takeout", methods=["GET", "POST"])
def export_takeout(redact: bool = False):
    """Portable JSONL export a human can read without Mnemos installed.

    `redact=true` runs text fields through the crash-report redactor for a
    share-safe variant — take an unredacted one for yourself.
    """
    from app.services import export
    name = export.suggested_name("takeout-redacted" if redact else "takeout")
    return _stream_or_400(lambda: export.takeout_stream(redact=redact), name)


# --- version check (WS-C) --------------------------------------------------
# The manifest GET is an unconditional fetch of a static file: no query params,
# no install id, no version header. Notification only — nothing downloads.

class UpdateEnabledIn(BaseModel):
    enabled: bool


class UpdateDismissIn(BaseModel):
    version: str | None = None


@router.get("/update/status")
def update_status() -> dict:
    """Cached manifest verdict. `state` is 'unknown' when we never reached it."""
    from app.services import update_check
    return update_check.status()


@router.post("/update/check")
def update_check_now() -> dict:
    """Force a re-check now (the Console's 'check again'). Never raises."""
    from app.services import update_check
    return update_check.check(force=True)


@router.post("/update/enabled")
def update_set_enabled(body: UpdateEnabledIn) -> dict:
    """Privacy controls toggle. Off means the GET never happens."""
    from app.services import update_check
    return update_check.set_enabled(bool(body.enabled))


@router.post("/update/dismiss")
def update_dismiss(body: UpdateDismissIn) -> dict:
    """Dismiss the banner for one version; a newer release shows it again."""
    from app.services import update_check
    return update_check.dismiss(body.version)


# --- classifier heads (Phase 2) --------------------------------------------
# Same transparency contract as the Learning tab: every head's volume, skip
# rate and shadow disagreement are visible, and each has its own kill switch.

@router.get("/console/heads")
def console_heads(days: float = 7.0) -> dict:
    """Per-head rollout state: is it trained, what would it skip, and how
    often would that have been wrong?

    `ready_to_activate` encodes the gate in one place so the flip is a
    decision the Console can offer rather than a human reading a log:
    enough shadow events, and disagreement under the threshold on the
    population the head would have skipped.
    """
    from app.services import fast_heads
    return fast_heads.status(window_s=max(0.0, days) * 86400.0)


@router.post("/console/heads/train")
def console_heads_train(head: str | None = None) -> dict:
    """Fit now instead of waiting for the idle scheduler. Refuses politely
    when a head has too few labels rather than fitting a silent dropper."""
    from app.services import fast_heads
    if head:
        return {"ok": True, "result": fast_heads.train_head(head)}
    return {"ok": True, "results": fast_heads.train_all()}


# --- latency spans (latency program, Phase 0) ------------------------------
# "You cannot fix what you can't see": model_log records per-call wall time,
# this records where that time went. Read-only and local.

@router.get("/console/latency")
def console_latency(hours: float = 0.0,
                    cold_load_ms: float | None = None) -> dict:
    """p50/p90/p99 per stage per task, plus the cold-start census.

    `hours` limits the window (0 = everything on the trail). Stages are sorted
    by share of total time — the top row of each group is where to optimize.
    `cold_load_ms` re-splits cold vs warm over already-collected data, since
    the right threshold is machine- and model-dependent.
    """
    import time as _time
    from app.services import latency
    since = (_time.time() - hours * 3600.0) if hours and hours > 0 else None
    out = latency.percentiles(since=since, cold_load_ms=cold_load_ms)
    out["window_hours"] = hours or None
    # The audio path reports from audio_telemetry, which already times every
    # utterance — no second set of timers on the capture thread.
    out["capture"] = latency.capture_stages(
        window_s=(hours * 3600.0) if hours and hours > 0 else 86400.0)
    return out


# --- pilot usage ledger (WS-A) --------------------------------------------
# Local-first by construction: /usage/stats and /usage/report only read and
# write this machine. The one network path (/usage/ping/*) needs BOTH a
# configured URL and a consent flag the user set here, and both default off.

class UsageConsentIn(BaseModel):
    consented: bool


@router.get("/usage/stats")
def usage_stats() -> dict:
    """Local usage rows + derived WAU / retention metrics. Never leaves here."""
    from app.services import usage_ledger
    return {
        "ok": True,
        "enabled": usage_ledger.usage.enabled(),
        "metrics": usage_ledger.metrics(),
        "days": usage_ledger.report_payload()["days"],
        "pending": usage_ledger.usage.pending(),
        "ping": usage_ledger.ping_status(),
    }


@router.get("/usage/preview")
def usage_preview() -> dict:
    """The exact payload a share would send — shown BEFORE consent is asked.

    Same bytes as /usage/report writes and as the weekly ping would POST;
    there is no second, richer payload anywhere.
    """
    from app.services import usage_ledger
    import json as _json
    text = usage_ledger.redacted_report_json()
    return {"ok": True, "payload": _json.loads(text), "text": text,
            "bytes": len(text)}


@router.post("/usage/report")
def usage_report() -> dict:
    """Write data/logs/usage-<install_id>-<day>.json for the tester to send.

    Same interaction shape as the crash-report zip: the file lands on disk and
    the human decides whether it goes anywhere.
    """
    from app.services import usage_ledger
    usage_ledger.usage.flush()
    return usage_ledger.write_report()


@router.get("/usage/ping/status")
def usage_ping_status() -> dict:
    from app.services import usage_ledger
    return {"ok": True, **usage_ledger.ping_status()}


@router.post("/usage/ping/consent")
def usage_ping_consent(body: UsageConsentIn) -> dict:
    """Store (or withdraw) standing consent for the weekly stats ping."""
    from app.services import usage_ledger
    usage_ledger.set_ping_consent(bool(body.consented))
    return {"ok": True, **usage_ledger.ping_status()}


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


_PWA = _plain("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@BRAND@@ capture</title>
@@FONTS@@
<style>
@@ROOT@@
body{font:16px/1.45 var(--font);color:var(--text);margin:24px;max-width:420px;background:var(--paper)}
button{font:inherit;padding:12px 18px;border-radius:10px;border:0;background:var(--navy);color:var(--paper)}
#log{white-space:pre-wrap;color:var(--mut);margin-top:16px;font-size:13px}
</style></head><body>
<h1>Phone as mic</h1>
<p>Records short clips and posts transcripts to this @@BRAND@@. Pair the phone first.</p>
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
""")


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
    html = _plain("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — first launch</title>
@@FONTS@@
<style>@@ROOT@@
body{font:16px/1.45 var(--font);color:var(--text);margin:32px;max-width:520px;background:var(--paper)}
button{font:inherit;padding:10px 16px;border-radius:10px;border:0;background:var(--navy);color:var(--paper)}
#log{white-space:pre-wrap;color:var(--mut);margin-top:16px;font-size:13px}</style></head>
<body>
<h1>Download speech models</h1>
<p>@@BRAND@@ does not ship the 460MB speech models or Chromium. This runs once, locally, and can be resumed.</p>
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
</script></body></html>""")
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
