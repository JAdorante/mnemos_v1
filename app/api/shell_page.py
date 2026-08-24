"""Today — attention-ordered home with proposals from agent_bridge.

Served at GET /today (GET /shell permanently redirects here).

Stages (cumulative):
  1. read-only world — constellation / field
  2. attention-ordered — WM focus, horizon, at-risk
  3. proposals — renders existing offer pipeline (incl. reasoners); yes/no
     goes through /today/offer → worker.resolve_todo (no new channel)
"""

from app.api.mnemos_theme import apply as _mnemos

SHELL_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;font:15px/1.55 var(--font);color:var(--text);
  background:
    radial-gradient(900px 480px at 8% -8%, var(--acc-06), transparent 55%),
    radial-gradient(700px 400px at 94% 0%, rgba(30,91,79,.04), transparent 50%),
    linear-gradient(180deg,#FBF9F4 0%,var(--paper) 40%,var(--workspace) 100%);
}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 22px}
.page-sub{margin-left:-4px}
.wrap{max-width:1100px;margin:0 auto;padding:4px 22px 56px}
.mast{padding:14px 0 4px;animation:morningPaper .45s var(--ease) both}
.mast .date{
  font-family:var(--display);font-weight:400;font-size:clamp(1.15rem,2.4vw,1.4rem);
  letter-spacing:-.02em;color:var(--navy);margin:0;line-height:1.2;
}
.mast .line{color:var(--mut);font-size:14px;margin:6px 0 0;max-width:40em}
.stack{display:flex;flex-direction:column;gap:14px;margin-top:12px}
.band{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);padding:16px 18px;position:relative;
  animation:fadeUp .35s var(--ease) both;
}
.band.proposal{
  border-color:var(--acc-35);
  background:linear-gradient(180deg,#FFFCF7 0%,var(--surface) 70%);
  box-shadow:var(--shadow-folio);
}
.band h2{
  font-family:var(--display);font-weight:400;font-size:1.35rem;color:var(--navy);
  margin:0 0 6px;letter-spacing:-.02em;
}
.band .lead{color:var(--mut);font-size:13px;margin:0 0 12px}
.row{display:flex;gap:10px;align-items:flex-start;padding:9px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0;padding-top:0}
.row .t{flex:1;min-width:0}
.row .meta{font:11px var(--mono);color:var(--mut);margin-top:3px}
.pill{
  font:11px/1.2 var(--mono);padding:3px 8px;border-radius:999px;border:1px solid var(--line);
  color:var(--mut);white-space:nowrap;
}
.pill.attn{border-color:var(--acc-40);color:var(--acc);background:var(--acc-dim)}
.pill.urgent{border-color:rgba(166,71,71,.35);color:var(--danger)}
.actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.msg{
  white-space:pre-wrap;font-size:14px;line-height:1.5;color:var(--text);
  margin:0 0 4px;
}
#constWrap{
  height:240px;border-radius:var(--radius);border:1px solid var(--line);
  background:var(--bg-elev);box-shadow:var(--shadow-workspace);overflow:hidden;position:relative;
}
#constLink{
  display:block;width:100%;height:100%;cursor:pointer;text-decoration:none;color:inherit;
}
#constWrap:hover{border-color:var(--acc-28)}
#constCanvas{width:100%;height:100%;display:block}
#constEmpty{
  position:absolute;inset:0;display:none;flex-direction:column;align-items:center;justify-content:center;
  font-family:var(--display);font-size:1.05rem;color:var(--mut);padding:24px;text-align:center;gap:10px;
}
#constEmpty .empty-act{
  appearance:none;border:0;background:transparent;cursor:pointer;
  font:500 13px var(--font);color:var(--navy);padding:4px;
}
#constEmpty .empty-act:hover{text-decoration:underline}
#worldCap{font:12px var(--mono);color:var(--mut);margin:8px 0 0}
.notepad-box{
  width:100%;min-height:72px;resize:vertical;border:1px solid var(--line);
  border-radius:10px;padding:10px 12px;font:14px/1.45 var(--font);color:var(--text);
  background:var(--panel);
}
.notepad-box:focus{outline:2px solid var(--acc-35);outline-offset:1px}
.jot-list{margin-top:10px;display:flex;flex-direction:column;gap:6px;max-height:180px;overflow:auto}
.jot-item{
  font-size:13px;line-height:1.4;padding:6px 0;border-top:1px solid var(--line);
  color:var(--text);overflow-wrap:anywhere;min-width:0;
}
.jot-item:first-child{border-top:0;padding-top:0}
.jot-item .when{font:11px var(--mono);color:var(--mut);margin-top:2px}
.hz{display:flex;flex-wrap:wrap;gap:8px}
.hz .chip{
  border:1px solid var(--line);border-radius:12px;padding:8px 12px;font-size:13px;
  background:var(--panel);color:var(--navy);max-width:100%;
}
.hz .chip .when{font:11px var(--mono);color:var(--acc);margin-right:6px}
.hz .chip .meta{font:11px var(--mono);color:var(--mut);margin-top:3px;line-height:1.35}
.wm{display:flex;flex-wrap:wrap;gap:8px}
.wm .slot{
  border:1px solid var(--line);border-radius:12px;padding:8px 12px;font-size:13px;
  background:var(--panel);max-width:100%;
}
.wm .slot .k{font:10px var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:.04em}
.fetch-err{
  margin:0 22px 12px;padding:10px 14px;border-radius:10px;
  background:rgba(154,63,63,.08);border:1px solid rgba(154,63,63,.25);
  color:var(--danger);font-size:13px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;
}
.fetch-err button{font:inherit;padding:4px 12px;border-radius:8px;cursor:pointer;
  border:1px solid rgba(154,63,63,.35);background:var(--panel);color:var(--danger);}
.foot{
  margin-top:28px;display:flex;gap:14px;flex-wrap:wrap;align-items:center;
  color:var(--mut);font-size:12px;
}
.foot a{color:var(--mut)}
#spotlight{
  display:none;position:fixed;inset:0;z-index:var(--z-modal);background:var(--overlay);
  align-items:flex-start;justify-content:center;padding:12vh 16px 16px;
}
#spotlight.open{display:flex}
#spotlight .sheet{
  width:min(560px,100%);max-height:calc(100dvh - 48px);overflow:auto;
  background:var(--folio);border:1px solid var(--line);
  border-radius:var(--radius);box-shadow:var(--shadow-float);padding:18px;
  animation:fadeUp .28s var(--ease) both;
}
#spotlight .sheet h3{font-family:var(--display);font-weight:400;font-size:1.4rem;
  margin:0 0 8px;color:var(--navy)}
#spotBox{
  width:100%;min-height:72px;resize:vertical;font:inherit;padding:12px 14px;
  border:1px solid var(--line);border-radius:12px;background:var(--bg-elev);color:var(--text);
}
.kbd{font:11px var(--mono);border:1px solid var(--line);border-radius:6px;padding:2px 6px}
@media(max-width:720px){
  .mast{padding-top:10px}
  .top{padding:10px 14px}
  .wrap{padding:0 14px 32px}
  .band{margin:0 14px 12px}
  .fetch-err{margin:0 14px 12px}
  .foot{margin:0 14px 12px}
  #spotlight .sheet{width:100%;margin:0 8px}
  #spotlight{padding:8vh 8px 8px}
}
.band .action-detail{margin:10px 0 4px}
.band .action-detail > summary{
  cursor:pointer;font:500 13px var(--font);color:var(--navy);list-style:none;
}
.band .action-detail > summary::-webkit-details-marker{display:none}
.band .action-detail > summary::before{
  content:"▸ ";color:var(--acc);font-size:11px;
}
.band .action-detail[open] > summary::before{content:"▾ "}
.band .action-detail .detail-card{
  margin-top:8px;padding:12px 14px;border:1px solid var(--acc-22);
  border-radius:12px;background:linear-gradient(180deg,#FFFCF7 0%,var(--surface) 100%);
  border-left:3px solid var(--acc);
}
.band .action-detail .intent{font-size:14px;margin:0 0 10px;color:var(--text)}
.band .action-detail .steps{
  margin:0;padding:0;list-style:none;font:12px/1.55 var(--mono);color:var(--mut);
}
.band .action-detail .steps li{padding:3px 0}
.band .action-detail .payload{
  margin-top:10px;max-height:180px;overflow:auto;white-space:pre-wrap;
  font:13px/1.45 var(--font);color:var(--text);padding:10px 12px;
  background:var(--bg-elev);border:1px solid var(--line);border-radius:10px;
}
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Today</span>
  @@NAV@@
  <span class="spacer"></span>
  <button class="btn" id="spotOpen" type="button">Ask <span class="kbd">⌘K</span></button>
</header>
@@APPROVAL@@
<div id="shellErr" class="fetch-err" hidden role="alert">
  <span id="shellErrMsg">Couldn't reach Mnemos — retrying…</span>
  <button type="button" id="shellRetry">Retry now</button>
</div>

<div class="wrap">
  <header class="mast">
    <div class="date" id="dateLabel">—</div>
    <p class="line" id="tagLine">Nothing needs you right now.</p>
  </header>

  <div class="stack">
    <section class="band proposal" id="secProposal" hidden>
      <h2 id="propTitle">Waiting on you</h2>
      <p class="lead" id="propLead">From the existing offer pipeline — not a separate channel.</p>
      <div class="msg" id="propMsg"></div>
      <div class="row" id="propMeta" style="border:0;padding-top:8px"></div>
      <details class="action-detail" id="propDetail">
        <summary>What will happen</summary>
        <div class="detail-card">
          <p class="intent" id="propIntent"></p>
          <ol class="steps" id="propSteps"></ol>
          <div class="payload" id="propPayload" hidden></div>
        </div>
      </details>
      <div class="actions" id="propActions">
        <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
          <input type="hidden" name="accept" value="1">
          <input type="hidden" name="next" value="/today">
          <button type="submit" class="go" id="propYes">Yes — proceed</button>
        </form>
        <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
          <input type="hidden" name="accept" value="0">
          <input type="hidden" name="next" value="/today">
          <button type="submit" class="quiet" id="propNo">Not now</button>
        </form>
        <div id="propChoices" hidden></div>
        <a class="btnish" href="/chat">Open chat</a>
      </div>
    </section>

    <section class="band" id="secFocus">
      <h2>In focus</h2>
      <p class="lead">Working memory — the same attention field, chat, and planner share.</p>
      <div class="wm" id="wmList"><div class="skel" aria-hidden="true"><span class="bone"></span><span class="bone"></span><span class="bone"></span></div></div>
    </section>

    <section class="band" id="secNotepad">
      <h2>Meeting notes</h2>
      <p class="lead" id="noteLead">Rough notes during a call become importance anchors — not fabricated quotes.</p>
      <textarea class="notepad-box" id="noteBox" placeholder="pricing — pushback&#10;follow up with Sarah on deck" rows="3"></textarea>
      <div class="actions" style="margin-top:10px">
        <button type="button" class="go" id="noteSave">Save note</button>
        <span class="pill" id="noteStatus" hidden role="status" aria-live="polite"></span>
        <a class="btnish" id="noteOpenEnhanced" href="/meetings" hidden>Open enhanced note</a>
      </div>
      <div class="jot-list" id="jotList"></div>
    </section>

    <section class="band" id="secHorizon">
      <h2>Coming up</h2>
      <p class="lead">Horizon strip — what you are likely to need next.</p>
      <div class="hz" id="hzList"><div class="skel" aria-hidden="true"><span class="bone"></span><span class="bone"></span></div></div>
    </section>

    <section class="band" id="secRisk">
      <h2>At risk</h2>
      <p class="lead">Open commitments that look overdue or neglected.</p>
      <div id="riskList"><div class="skel rows" aria-hidden="true"><span class="bone"></span><span class="bone"></span></div></div>
    </section>

    <section class="band" id="secWorld">
      <h2>World</h2>
      <p class="lead">Your field — open Memory to explore.</p>
      <div id="constWrap">
        <a id="constLink" href="/memory?mode=constellation" aria-label="Open constellation in Memory">
          <canvas id="constCanvas"></canvas>
        </a>
        <div id="constEmpty">
          <span>Memories will gather here as you capture, chat, and work. Each node is something Mnemos can show its evidence for.</span>
          <button type="button" class="empty-act" id="constStartCapture">Start capturing</button>
        </div>
      </div>
      <div id="worldCap" hidden></div>
    </section>

    <section class="band" id="secForgot" hidden>
      <h2>Quietly archived</h2>
      <p class="lead">Compacted this month — restore anytime from Memory console.</p>
      <div id="forgotList"></div>
      <div class="actions">
        <a class="btnish" href="/memory">Review in Memory</a>
      </div>
    </section>
  </div>

  <footer class="foot">
    <span id="stageNote">Today · field + WM + offers</span>
    <a href="/chat">Chat is secondary — full thread</a>
    <a href="/memory">Memory console</a>
  </footer>
</div>

<div id="spotlight" aria-hidden="true">
  <div class="sheet" role="dialog" aria-modal="true" aria-label="Ask">
    <h3>Ask @@BRAND@@</h3>
    <p style="color:var(--mut);font-size:13px;margin:0 0 10px">Orientation only. Full thread lives in Chat.</p>
    <textarea id="spotBox" placeholder="A question, a task, a follow-up…"></textarea>
    <div class="actions" style="justify-content:flex-end">
      <button type="button" class="quiet" id="spotCancel">Dismiss</button>
      <a class="btnish" href="/chat">Full chat</a>
      <button type="button" class="go" id="spotGo">Send</button>
    </div>
  </div>
</div>

@@UI_JS@@
<script>
MnemosMemory.set('lastRoute', '/today');
let constCtl = null;
let pollTimer = null;
let _shellSig = null;
let _shellFails = 0;
let _shellRetry = null;

/* The banner means one thing only: the server is unreachable. A render bug is
   not an outage, and must not claim to be one — nor is a refusal to answer. */
function shellOffline(on, reason) {
  const errEl = document.getElementById('shellErr');
  if (!errEl) return;
  errEl.hidden = !on;
  const btn = document.getElementById('shellRetry');
  if (btn) { btn.textContent = 'Retry now'; btn.dataset.mode = 'retry'; }
  const msg = document.getElementById('shellErrMsg');
  if (msg) {
    msg.textContent = on
      ? "Couldn't reach Mnemos — retrying…" + (reason ? ' (' + reason + ')' : '')
      : "Couldn't reach Mnemos — retrying…";
  }
}

/* 401/403 is the opposite of an outage: the server answered, and answered
   "no". Retrying the identical request can never change that, so say what is
   actually wrong and point at the one action that fixes it. */
function shellLocked(status) {
  const errEl = document.getElementById('shellErr');
  if (!errEl) return;
  errEl.hidden = false;
  const msg = document.getElementById('shellErrMsg');
  if (msg) {
    msg.textContent = 'Mnemos is locked for network browsers (HTTP ' + status
      + ') — unlock this browser to load your data.';
  }
  const btn = document.getElementById('shellRetry');
  if (btn) { btn.textContent = 'Unlock'; btn.dataset.mode = 'unlock'; }
}

async function loadShell() {
  let data;
  try {
    const r = await fetch('/today/state?limit=28', {cache: 'no-store'});
    if (!r.ok) { const err = new Error('HTTP ' + r.status); err.status = r.status; throw err; }
    data = await r.json();
  } catch (e) {
    if (e && (e.status === 401 || e.status === 403)) {
      /* Gated, not down. Stop the backoff loop — it would poll a wall. */
      if (_shellRetry) { clearTimeout(_shellRetry); _shellRetry = null; }
      _shellFails = 0;
      console.error('[shell] /today/state refused (HTTP ' + e.status + ') — browser not unlocked');
      shellLocked(e.status);
      return;
    }
    /* Transport failure. Actually retry — the banner has always promised it. */
    _shellFails += 1;
    console.error('[shell] /today/state failed (attempt ' + _shellFails + '):', e);
    if (_shellFails >= 2) shellOffline(true, (e && e.message) ? e.message : String(e));
    if (_shellRetry) clearTimeout(_shellRetry);
    const wait = Math.min(1000 * Math.pow(2, _shellFails - 1), 15000);
    _shellRetry = setTimeout(() => { if (!document.hidden) loadShell(); }, wait);
    return;
  }
  _shellFails = 0;
  if (_shellRetry) { clearTimeout(_shellRetry); _shellRetry = null; }
  shellOffline(false);

  const sig = JSON.stringify(data);
  if (sig === _shellSig) return;
  try {
    document.getElementById('dateLabel').textContent = data.date_label || '';
    if (data.awaiting_approval) document.getElementById('navChat').classList.add('attn');
    else document.getElementById('navChat').classList.remove('attn');

    renderProposal(data.proposal, data.queued_offers || 0, data.waiting_on, data.approval_packet);
    renderWm((data.attention && data.attention.wm) || []);
    renderNotepad(data.notepad || {}, data.latest_meeting_note || null);
    renderHorizon((data.attention && data.attention.horizon) || []);
    renderRisk((data.attention && data.attention.at_risk) || []);
    renderWorld(data.world || {});
    renderForgot(data.forgotten || []);
    updateMastLine(data);
    _shellSig = sig;  /* commit only after a clean render, or it never repaints */
  } catch (e) {
    _shellSig = null;
    console.error('[shell] render failed:', e);
  }
}
document.getElementById('shellRetry')?.addEventListener('click', (ev) => {
  if (ev.currentTarget.dataset.mode === 'unlock') {
    location.href = '/auth?next=' + encodeURIComponent(location.pathname);
    return;
  }
  loadShell();
});

let _noteSessionId = null;
function renderNotepad(np, latestNote) {
  _noteSessionId = np.session_id || null;
  const lead = document.getElementById('noteLead');
  if (np.title) {
    lead.textContent = (np.calendar_linked ? 'Live notes for ' : 'Notes · ')
      + np.title + ' — jots lift what matters in the transcript.';
  } else {
    lead.textContent = 'Rough notes during a call become importance anchors — not fabricated quotes.';
  }
  const enh = document.getElementById('noteOpenEnhanced');
  if (latestNote && latestNote.href) {
    enh.hidden = false;
    enh.href = latestNote.href;
    enh.textContent = 'Open: ' + (latestNote.title || 'enhanced note');
  } else {
    enh.hidden = true;
  }
  const list = document.getElementById('jotList');
  const jots = np.jots || [];
  if (!jots.length) {
    list.innerHTML = '<div class="jot-item" style="color:var(--mut)">No notes yet this session.</div>';
    return;
  }
  list.innerHTML = jots.map(j => {
    const when = j.time ? new Date(j.time * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'}) : '';
    return '<div class="jot-item">' + MnemosEsc(j.text || '')
      + (when ? '<div class="when">' + MnemosEsc(when) + '</div>' : '')
      + '</div>';
  }).join('');
}

async function saveNote() {
  const box = document.getElementById('noteBox');
  const status = document.getElementById('noteStatus');
  const text = (box.value || '').trim();
  if (!text) return;
  const btn = document.getElementById('noteSave');
  btn.disabled = true;
  status.hidden = false;
  status.textContent = 'Saving…';
  try {
    const body = {text: text};
    if (_noteSessionId) body.session_id = _noteSessionId;
    const r = await fetch('/session/note', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      status.textContent = err.detail || 'Could not save';
      return;
    }
    box.value = '';
    status.textContent = 'Saved';
    setTimeout(() => { status.hidden = true; }, 1600);
    loadShell();
  } catch (e) {
    status.textContent = 'Could not save';
  } finally {
    btn.disabled = false;
  }
}
document.getElementById('noteSave').onclick = saveNote;
document.getElementById('noteBox').addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
    e.preventDefault();
    saveNote();
  }
});

function updateMastLine(data) {
  const el = document.getElementById('tagLine');
  const p = data.proposal;
  const queued = data.queued_offers || 0;
  const wm = ((data.attention && data.attention.wm) || []).length;
  const hz = ((data.attention && data.attention.horizon) || []).length;
  if (p || data.awaiting_approval) {
    const n = 1 + (p && queued ? queued : 0);
    el.textContent = n === 1
      ? '1 decision waiting on you.'
      : (n + ' decisions waiting on you.');
    return;
  }
  const parts = [];
  if (wm) parts.push(wm + (wm === 1 ? ' thing in focus' : ' things in focus'));
  if (hz) parts.push(hz + ' coming up');
  if (parts.length) el.textContent = parts.join(' · ') + '.';
  else el.textContent = 'Nothing needs you right now.';
}

function fillActionDetail(p, packet) {
  const det = document.getElementById('propDetail');
  const intent = document.getElementById('propIntent');
  const steps = document.getElementById('propSteps');
  const payload = document.getElementById('propPayload');
  const fields = (packet && packet.fields) || {};
  const intentText = (fields.action || (packet && packet.summary)
    || (p && (p.message || (p.items && p.items[0]))) || '').trim();
  intent.textContent = intentText || 'Mnemos is waiting for your decision.';
  const stepItems = [];
  if (fields.to) stepItems.push('Compose to ' + fields.to);
  if (fields.subject) stepItems.push('Subject: ' + fields.subject);
  if (p && p.items && p.items.length) {
    p.items.slice(0, 6).forEach((it, i) => stepItems.push((i + 1) + '. ' + it));
  } else if (fields.action && !fields.to) {
    stepItems.push(fields.action);
  }
  if (!stepItems.length && intentText) stepItems.push(intentText);
  steps.innerHTML = stepItems.map(s => '<li>' + MnemosEsc(String(s)) + '</li>').join('');
  const body = (fields.body || fields.details || '').trim();
  const outbound = !!(fields.body || fields.to || /email|message|send|post|sms|text/i.test(intentText));
  if (body) {
    payload.hidden = false;
    payload.textContent = body;
  } else {
    payload.hidden = true;
    payload.textContent = '';
  }
  det.open = outbound;
}

function renderProposal(p, queued, waitingOn, packet) {
  const sec = document.getElementById('secProposal');
  if (!p && !(packet && packet.kind === 'approval')) {
    sec.hidden = true;
    return;
  }
  sec.hidden = false;
  if (p) {
    const kind = (p.kind || 'offer').replace(/^reasoner_/, '');
    const title = p.title || (p.reasoner ? (p.reasoner + ' suggestion') : 'Waiting on you');
    document.getElementById('propTitle').textContent = title;
    document.getElementById('propLead').textContent =
      (p.reasoner ? ('Reasoner · ' + p.reasoner) : ('Offer · ' + kind))
      + (queued ? (' · ' + queued + ' more queued') : '');
    document.getElementById('propMsg').textContent = p.message || (p.items && p.items[0]) || '';
    let meta = '';
    if (p.confidence != null) meta += '<span class="pill attn">' + Math.round(p.confidence * 100) + '%</span>';
    (p.why || []).slice(0, 3).forEach(w => {
      meta += '<span class="pill">' + MnemosEsc(String(w)) + '</span>';
    });
    document.getElementById('propMeta').innerHTML = meta;
  } else {
    document.getElementById('propTitle').textContent = 'Waiting on you';
    document.getElementById('propLead').textContent = 'Agent approval — review before it acts.';
    document.getElementById('propMsg').textContent = (packet && packet.summary) || waitingOn || '';
    document.getElementById('propMeta').innerHTML = '';
  }
  fillActionDetail(p, packet);
  const yesForm = document.getElementById('propYes') && document.getElementById('propYes').closest('form');
  const noForm = document.getElementById('propNo') && document.getElementById('propNo').closest('form');
  const choiceBox = document.getElementById('propChoices');
  const choices = (p && p.choices) || [];
  if (choiceBox) {
    if (choices.length) {
      choiceBox.hidden = false;
      choiceBox.innerHTML = choices.map((c, i) =>
        '<button type="button" class="' + (c.id === 'skip' ? 'quiet' : 'go')
        + '" data-choice="' + MnemosEsc(c.id) + '">'
        + MnemosEsc(c.label || c.id) + '</button>'
      ).join(' ');
      choiceBox.querySelectorAll('button[data-choice]').forEach(btn => {
        btn.onclick = () => answerOfferChoice(btn.getAttribute('data-choice'));
      });
      if (yesForm) yesForm.hidden = true;
      if (noForm) noForm.hidden = true;
    } else {
      choiceBox.hidden = true;
      choiceBox.innerHTML = '';
      if (yesForm) yesForm.hidden = false;
      if (noForm) noForm.hidden = false;
    }
  }
}

function renderWm(slots) {
  const el = document.getElementById('wmList');
  if (!slots.length) {
    el.innerHTML = MnemosRender.empty(
      'Nothing in working memory yet. Focus builds from what you capture — start a chat or turn on capture.',
      { link: { href: '/chat', label: 'Open Chat' } });
    return;
  }
  el.innerHTML = slots.map(s =>
    '<div class="slot"><div class="k">' + MnemosEsc(s.node_type || 'node')
    + (s.cluster_n > 1 ? (' · ×' + s.cluster_n) : '') + '</div>'
    + MnemosEsc(s.label || s.node_key || '') + '</div>'
  ).join('');
}

function renderHorizon(items) {
  const el = document.getElementById('hzList');
  if (!items.length) {
    el.innerHTML = MnemosRender.empty(
      'Nothing on the horizon yet. Open loops and upcoming calendar items appear here.',
      { link: { href: '/onboarding?step=rhythm', label: 'Open rhythm setup' } });
    return;
  }
  el.innerHTML = items.map(i => {
    const why = (i.reason || []).slice(0, 2).join(' · ');
    const kind = i.loop_kind || i.source || '';
    const meta = [i.when_label || '', kind === 'open_loop' ? '' : kind,
      why].filter(Boolean).join(' · ');
    return '<div class="chip"><span class="when">' + MnemosEsc(i.when_label || '')
      + '</span>' + MnemosEsc(i.label || '')
      + (meta ? ('<div class="meta">' + MnemosEsc(meta) + '</div>') : '')
      + '</div>';
  }).join('');
}

function renderRisk(items) {
  const el = document.getElementById('riskList');
  if (!items.length) {
    el.innerHTML = MnemosRender.empty('No open commitments at risk.');
    return;
  }
  el.innerHTML = items.map(r =>
    '<div class="row"><div class="t">' + MnemosEsc(r.text || '')
    + '<div class="meta">' + MnemosEsc((r.why || []).join(' · ') || 'at risk')
    + (r.subject ? (' · ' + MnemosEsc(r.subject)) : '') + '</div></div>'
    + '<span class="pill urgent">' + (r.risk != null ? Math.round(r.risk * 100) + '%' : 'risk')
    + '</span></div>'
  ).join('');
}

function renderWorld(world) {
  const nodes = world.nodes || [];
  const empty = document.getElementById('constEmpty');
  const cap = document.getElementById('worldCap');
  empty.style.display = nodes.length ? 'none' : 'flex';
  if (nodes.length) {
    const active = nodes.filter(n => (n.layer || '') !== 'periphery').length;
    cap.hidden = false;
    cap.textContent = nodes.length + ' memories · ' + active + ' active today';
  } else {
    cap.hidden = true;
  }
  if (!nodes.length) {
    if (constCtl) { try { constCtl.destroy(); } catch (e) {} constCtl = null; }
    return;
  }
  const payload = {
    nodes: nodes,
    edges: world.edges || [],
    selection: world.selection,
    mode: world.mode,
    context: world.context,
  };
  if (constCtl) constCtl.update(payload);
  else {
    constCtl = MnemosConstellation.mount(
      document.getElementById('constCanvas'), payload, {
        mode: 'thumbnail',
        href: '/memory?mode=constellation',
      });
  }
}

function renderForgot(items) {
  const sec = document.getElementById('secForgot');
  const el = document.getElementById('forgotList');
  if (!items.length) { sec.hidden = true; return; }
  sec.hidden = false;
  el.innerHTML = items.map(f =>
    '<div class="row"><div class="t">' + MnemosEsc(f.summary || ('event ' + f.event_id))
    + '<div class="meta">event ' + MnemosEsc(String(f.event_id || '')) + '</div></div>'
    + '<button type="button" class="quiet" data-eid="' + MnemosEsc(String(f.event_id || ''))
    + '">Restore</button></div>'
  ).join('');
  el.querySelectorAll('button[data-eid]').forEach(btn => {
    btn.onclick = () => restoreForgotten(Number(btn.getAttribute('data-eid')), btn);
  });
}

async function restoreForgotten(eventId, btn) {
  if (!eventId) return;
  if (btn) btn.disabled = true;
  try {
    await fetch('/today/restore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({event_id: eventId}),
    });
  } catch (e) {}
  loadShell();
}

async function answerOffer(accept) {
  try {
    await fetch('/approvals/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
      body: 'accept=' + (accept ? '1' : '0') + '&as_json=1',
    });
  } catch (e) {}
  loadShell();
  if (window.MnemosApprovals) window.MnemosApprovals.refresh();
}
async function answerOfferChoice(choice) {
  try {
    await fetch('/today/offer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({choice: choice}),
    });
  } catch (e) {}
  loadShell();
  if (window.MnemosApprovals) window.MnemosApprovals.refresh();
}
document.querySelectorAll('#secProposal form.approval-form').forEach(form => {
  form.addEventListener('submit', (ev) => {
    if (!window.fetch) return;
    ev.preventDefault();
    const fd = new FormData(form);
    answerOffer(fd.get('accept') === '1');
  });
});

const startCap = document.getElementById('constStartCapture');
if (startCap) {
  startCap.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (window.MnemosCapture && MnemosCapture.openPrivacy) MnemosCapture.openPrivacy();
    else location.href = '/chat';
  };
}

/* §7 review fixtures: ?empty=focus|horizon|risk|world|all */
(function applyEmptyFixture() {
  try {
    const which = new URLSearchParams(location.search).get('empty');
    if (!which) return;
    const orig = loadShell;
    loadShell = async function () {
      await orig();
      const data = { attention: { wm: [], horizon: [], at_risk: [] }, world: { nodes: [], edges: [] },
        proposal: null, queued_offers: 0, awaiting_approval: false, forgotten: [] };
      if (which === 'focus' || which === 'all') renderWm([]);
      if (which === 'horizon' || which === 'all') renderHorizon([]);
      if (which === 'risk' || which === 'all') renderRisk([]);
      if (which === 'world' || which === 'all') renderWorld({ nodes: [], edges: [] });
      if (which === 'all') updateMastLine(data);
    };
  } catch (e) {}
})();

function openSpot() {
  MnemosDialog.open(document.getElementById('spotlight'), {
    lockScroll: true,
    focus: '#spotBox',
    onEscape: closeSpot,
  });
}
function closeSpot() {
  MnemosDialog.close(document.getElementById('spotlight'));
}
document.getElementById('spotOpen').onclick = openSpot;
document.getElementById('spotCancel').onclick = closeSpot;
document.getElementById('spotlight').addEventListener('click', e => {
  if (e.target.id === 'spotlight') closeSpot();
});
async function sendSpot() {
  const msg = (document.getElementById('spotBox').value || '').trim();
  if (!msg) return;
  closeSpot();
  document.getElementById('spotBox').value = '';
  try {
    await fetch('/chat', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({message: msg}),
    });
  } catch (e) {}
  window.location.href = '/chat';
}
document.getElementById('spotGo').onclick = sendSpot;
document.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
    e.preventDefault(); openSpot();
  }
});

loadShell();
pollTimer = setInterval(() => { if (!document.hidden) loadShell(); }, 12000);
if (window.MnemosFieldStream) {
  const live = MnemosFieldStream.connect(() => loadShell());
  if (live) clearInterval(pollTimer), pollTimer = setInterval(() => { if (!document.hidden) loadShell(); }, 45000);
}
</script>
</body>
</html>
""")
