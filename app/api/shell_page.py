"""Today — attention-ordered home with proposals from agent_bridge.

Served at GET /today (GET /shell permanently redirects here).

Layout (UI refactor spec §4): 1120px frame, two-column grid — main flow
(Waiting on you, In focus, Meeting notes, Recently noticed) plus a 320px
rail (Coming up, At risk, In the margin, Quietly archived). The world
graph is gone from Today (§5); its stat line lives in the day header.
The Ask field and worker status live in the shared shell, not here.
"""

from app.api.mnemos_theme import apply as _mnemos

try:
    from app.version import __version__ as _ver
except Exception:
    _ver = ""

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
  margin:0;min-height:100vh;
  font:15px/1.5 var(--sans);color:var(--text);
  background:var(--ink);
  -webkit-font-smoothing:antialiased;
}
.wrap{max-width:1120px;margin:0 auto;padding:44px 24px 80px}
header.day{
  display:flex;align-items:baseline;gap:16px;flex-wrap:wrap;margin-bottom:34px;
  animation:morningPaper var(--dur-slow) var(--ease) both;
}
.day h1{
  font-family:var(--serif);font-weight:500;font-size:34px;letter-spacing:.005em;
  color:var(--text);margin:0;line-height:1.2;
}
.day-sub{color:var(--mut);font-size:15px}
.day-sub b{color:var(--text);font-weight:600}
.grid{display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:24px;align-items:start}
section.card{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--r-lg);
  padding:22px 24px;margin-bottom:24px;position:relative;
  animation:fadeUp var(--dur) var(--ease) both;
}
.card h2{
  font-family:var(--serif);font-weight:500;font-size:19px;margin:0 0 2px;color:var(--text);
}
.card .hint{color:var(--mut);font-size:13.5px;margin:0 0 16px}
.rail section.card{padding:18px 20px}
.rail .card h2{font-size:17px}
.card.proposal{border-color:var(--acc-35)}
/* In focus — entity chips, no eyebrows; type carried by the dot. */
.chips{display:flex;flex-wrap:wrap;gap:8px}
/* Meeting notes */
.notepad-box{
  width:100%;min-height:84px;resize:vertical;border:1px solid var(--line);
  border-radius:var(--r-md);padding:13px 14px;
  font:inherit;color:var(--text);background:var(--ink);
}
.notepad-box::placeholder{color:var(--faint)}
.notepad-box:focus{outline:2px solid var(--violet);outline-offset:-1px;border-color:transparent}
.note-row{display:flex;align-items:center;gap:14px;margin-top:12px;flex-wrap:wrap}
.note-row .hint{margin:0}
.jot-list{margin-top:12px;display:flex;flex-direction:column;gap:0;max-height:180px;overflow:auto}
.jot-item{
  font-size:13.5px;line-height:1.45;padding:8px 0;
  border-top:1px solid var(--line);color:var(--text);overflow-wrap:anywhere;min-width:0;
}
.jot-item:first-child{border-top:0;padding-top:0}
.jot-item .when{font-size:12.5px;color:var(--faint);margin-top:2px}
/* Recently noticed — humanized memory rows */
.mem{border-top:1px solid var(--line);padding:13px 2px;display:flex;gap:12px;align-items:flex-start}
.mem:first-of-type{border-top:0;padding-top:4px}
.mem .d{margin-top:7px;flex:none;width:8px;height:8px;border-radius:50%}
.mem p{font-size:14.5px;margin:0}
.mem .meta{color:var(--faint);font-size:12.5px;margin-top:2px}
.mem details{margin-top:4px}
.see-all{display:inline-block;margin-top:10px;color:var(--mut);font-size:13.5px;text-decoration:none}
.see-all:hover{color:var(--text)}
/* Coming up chips */
.hz{display:flex;flex-direction:column;gap:8px}
.hz .hz-item{
  border:1px solid var(--line);border-radius:var(--r-md);padding:8px 12px;
  font-size:13.5px;background:var(--raised);color:var(--text);max-width:100%;
}
.hz .hz-item .when{font-weight:600;margin-right:4px}
.hz .hz-item .meta{font-size:12.5px;color:var(--faint);margin-top:2px}
/* At risk rows + ok line */
.risk-row{display:flex;gap:12px;align-items:flex-start;padding:8px 0;border-top:1px solid var(--line)}
.risk-row:first-of-type{border-top:0;padding-top:0}
.risk-row .t{flex:1;min-width:0;font-size:13.5px}
.risk-row .meta{font-size:12.5px;color:var(--faint);margin-top:2px}
.pill{
  font-size:12px;padding:3px 8px;border-radius:var(--radius-full);
  border:1px solid var(--line);color:var(--mut);white-space:nowrap;
}
.pill.attn{border-color:var(--acc-40);color:var(--acc);background:var(--acc-dim)}
.pill.urgent{border-color:color-mix(in srgb,var(--danger) 35%,transparent);color:var(--danger)}
.ok-line{display:flex;align-items:center;gap:9px;color:var(--mut);font-size:14px;margin-top:6px}
.ok-line .d{width:8px;height:8px;border-radius:50%;background:var(--green);flex:none}
/* In the margin — serif prose, daily-briefing voice */
.rail .ambient-note{
  font-family:var(--serif);font-style:normal;font-size:15.5px;line-height:1.55;
  color:var(--text);border-left:0;padding:0;margin:0 0 14px;
}
.rail .ambient-note.attention{color:var(--text)}
.rail .ambient-note.actionable{cursor:pointer;background:transparent;border:0;text-align:left;
  width:100%;font:inherit;padding:0;display:block}
.rail .ambient-note.actionable:hover .ambient-text{color:var(--violet)}
.rail .ambient-act{
  display:block;margin-top:6px;color:var(--violet);font:600 13.5px var(--sans);
}
/* Proposal detail + actions */
.msg{white-space:pre-wrap;font-size:14px;line-height:1.55;color:var(--text);margin:0 0 4px}
.prop-meta{display:flex;gap:8px;flex-wrap:wrap;padding:8px 0 0}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
.action-detail{margin:8px 0 4px}
.action-detail > summary{
  cursor:pointer;font:500 13px/1.3 var(--sans);color:var(--text);list-style:none;
}
.action-detail > summary::-webkit-details-marker{display:none}
.action-detail > summary::before{content:"▸ ";color:var(--acc);font-size:11px}
.action-detail[open] > summary::before{content:"▾ "}
.action-detail .detail-card{
  margin-top:8px;padding:12px 14px;border:1px solid var(--acc-22);
  border-radius:var(--r-md);background:var(--raised);
  border-left:3px solid var(--acc);
}
.action-detail .intent{font-size:14px;margin:0 0 8px;color:var(--text)}
.action-detail .steps{
  margin:0;padding:0;list-style:none;
  font:12.5px/1.55 var(--sans);color:var(--mut);
}
.action-detail .steps li{padding:3px 0}
.action-detail .payload{
  margin-top:8px;max-height:180px;overflow:auto;white-space:pre-wrap;
  font:13px/1.45 var(--sans);color:var(--text);padding:8px 12px;
  background:var(--ink);border:1px solid var(--line);border-radius:var(--r-sm);
}
/* Quietly archived rows */
.forgot-row{display:flex;gap:12px;align-items:center;justify-content:space-between;
  padding:8px 0;border-top:1px solid var(--line);font-size:13.5px}
.forgot-row:first-of-type{border-top:0;padding-top:0}
.forgot-row .meta{font-size:12.5px;color:var(--faint)}
.fetch-err{
  max-width:1120px;margin:12px auto 0;padding:8px 16px;border-radius:var(--r-md);
  background:color-mix(in srgb,var(--danger) 8%,transparent);
  border:1px solid color-mix(in srgb,var(--danger) 25%,transparent);
  color:var(--danger);font-size:13px;
  display:flex;gap:12px;align-items:center;flex-wrap:wrap;
}
.fetch-err button{
  font:500 12px/1.2 var(--sans);
  padding:4px 12px;border-radius:var(--r-sm);cursor:pointer;
  border:1px solid color-mix(in srgb,var(--danger) 35%,transparent);
  background:transparent;color:var(--danger);
}
footer.foot{
  max-width:1120px;margin:0 auto;padding:0 24px 40px;
  color:var(--faint);font-size:13px;display:flex;gap:20px;flex-wrap:wrap;
}
.foot a{color:var(--faint);text-decoration:none}
.foot a:hover{color:var(--mut)}
@media(max-width:900px){
  .grid{grid-template-columns:1fr}
  .rail{order:2}
}
@media(max-width:720px){
  .wrap{padding:28px 16px 48px}
  .fetch-err{margin:12px 16px 0}
}
@media(prefers-reduced-motion:reduce){
  header.day,section.card{animation:none}
}
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  @@NAV@@
</header>
@@APPROVAL@@
<div id="shellErr" class="fetch-err" hidden role="alert">
  <span id="shellErrMsg">Couldn't reach Sparrow — retrying…</span>
  <button type="button" id="shellRetry">Retry now</button>
</div>

<div class="wrap">
  <header class="day">
    <h1 id="dateLabel">—</h1>
    <span class="day-sub" id="tagLine">Nothing needs you right now.</span>
  </header>

  <div class="grid">
    <div class="main">
      <section class="card proposal" id="secProposal" hidden>
        <h2 id="propTitle">Waiting on you</h2>
        <p class="hint" id="propLead">From the existing offer pipeline — not a separate channel.</p>
        <div class="msg" id="propMsg"></div>
        <div class="prop-meta" id="propMeta"></div>
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

      <section class="card" id="secFocus">
        <h2>In focus</h2>
        <p class="hint">Your working memory — chat and the planner share this attention field.</p>
        <div class="chips" id="wmList"><div class="skel" aria-hidden="true"><span class="bone"></span><span class="bone"></span><span class="bone"></span></div></div>
        <div class="chip-legend" id="wmLegend" hidden>
          <span><span class="d person"></span>People</span>
          <span><span class="d entity"></span>Topics &amp; entities</span>
          <span>Numbers show memories gathered there</span>
        </div>
      </section>

      <section class="card" id="secNotepad">
        <h2>Meeting notes</h2>
        <p class="hint" id="noteLead">Rough notes during a call become importance anchors — never fabricated quotes.</p>
        <textarea class="notepad-box" id="noteBox" placeholder="pricing — pushback&#10;follow up with Sarah on deck" rows="3"></textarea>
        <div class="note-row">
          <button type="button" class="btn-primary" id="noteSave">Save note</button>
          <span class="pill" id="noteStatus" hidden role="status" aria-live="polite"></span>
          <a class="btn-link" id="noteOpenEnhanced" href="/meetings" hidden>Open enhanced note</a>
          <p class="hint">Notes are anchored to whatever session is live.</p>
        </div>
        <div class="jot-list" id="jotList"></div>
      </section>

      <section class="card" id="secRecent">
        <h2>Recently noticed</h2>
        <p class="hint">What @@BRAND@@ picked up today, in plain language. Raw detail stays one click away.</p>
        <div id="recentList"><div class="skel rows" aria-hidden="true"><span class="bone"></span><span class="bone"></span></div></div>
        <a class="see-all" href="/memory" id="recentSeeAll" hidden></a>
      </section>
    </div>

    <div class="rail">
      <section class="card" id="secHorizon">
        <h2>Coming up</h2>
        <div class="hz" id="hzList"><div class="skel" aria-hidden="true"><span class="bone"></span></div></div>
      </section>

      <section class="card" id="secRisk">
        <h2>At risk</h2>
        <div id="riskList"><div class="skel rows" aria-hidden="true"><span class="bone"></span></div></div>
      </section>

      <section class="card" id="secMargin">
        <h2>In the margin</h2>
        <div id="ambientBox"><p class="ambient-note">Listening…</p></div>
      </section>

      <section class="card" id="secForgot" hidden>
        <h2>Quietly archived</h2>
        <p class="hint">Compacted this month — restore anytime.</p>
        <div id="forgotList"></div>
        <div class="actions" style="margin-top:8px">
          <a class="btn-link" href="/memory">Review in Memory</a>
        </div>
      </section>
    </div>
  </div>
</div>

<footer class="foot">
  <span id="stageNote">@@BRAND@@ @@VER@@ · local-first</span>
  <a href="/chat">Chat — full thread</a>
  <a href="/memory">Memory</a>
</footer>

@@UI_JS@@
<script>
MnemosMemory.set('lastRoute', '/today');
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
      ? "Couldn't reach Sparrow — retrying…" + (reason ? ' (' + reason + ')' : '')
      : "Couldn't reach Sparrow — retrying…";
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
    msg.textContent = 'Sparrow is locked for network browsers (HTTP ' + status
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
  loadRecent();
});

let _noteSessionId = null;
function renderNotepad(np, latestNote) {
  _noteSessionId = np.session_id || null;
  const lead = document.getElementById('noteLead');
  if (np.title) {
    lead.textContent = (np.calendar_linked ? 'Live notes for ' : 'Notes · ')
      + np.title + ' — jots lift what matters in the transcript.';
  } else {
    lead.textContent = 'Rough notes during a call become importance anchors — never fabricated quotes.';
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
    list.innerHTML = '';
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

let _memToday = null; /* from Recently noticed — feeds the header sub-line */
function updateMastLine(data) {
  const el = document.getElementById('tagLine');
  const p = data && data.proposal;
  const queued = (data && data.queued_offers) || 0;
  const wm = ((data && data.attention && data.attention.wm) || []).length;
  if (p || (data && data.awaiting_approval)) {
    const n = 1 + (p && queued ? queued : 0);
    el.textContent = n === 1
      ? '1 decision waiting on you.'
      : (n + ' decisions waiting on you.');
    return;
  }
  const parts = [];
  if (wm) parts.push('<b>' + wm + (wm === 1 ? ' thing' : ' things') + '</b> in focus');
  if (_memToday != null && _memToday > 0) parts.push(_memToday + ' memories');
  if (parts.length) el.innerHTML = parts.join(' · ');
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
  intent.textContent = intentText || 'Sparrow is waiting for your decision.';
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

/* In focus — entity chips. Circle violet = person, amber square = topic;
   trailing faint number = how many memories cluster in the slot. */
function renderWm(slots) {
  const el = document.getElementById('wmList');
  const legend = document.getElementById('wmLegend');
  if (!slots.length) {
    legend.hidden = true;
    el.innerHTML = MnemosRender.empty(
      'Nothing in working memory yet. Focus builds from what you capture — start a chat or turn on capture.',
      { link: { href: '/chat', label: 'Open Chat' } });
    return;
  }
  legend.hidden = false;
  el.innerHTML = slots.map(s => {
    const person = (s.node_type === 'person') || (s.kind === 'person');
    const label = s.label || s.node_key || '';
    const n = (s.cluster_n && s.cluster_n > 1) ? s.cluster_n : null;
    return '<button type="button" class="echip" title="' + MnemosEsc(label) + '">'
      + '<span class="d ' + (person ? 'person' : 'entity') + '"></span>'
      + '<span class="t">' + MnemosEsc(label) + '</span>'
      + (n ? ('<span class="w">' + n + '</span>') : '')
      + '</button>';
  }).join('');
}

function renderHorizon(items) {
  const el = document.getElementById('hzList');
  if (!items.length) {
    el.innerHTML = '<div class="empty">Open loops and calendar items will land here as your day takes shape.'
      + '<br><a href="/onboarding?step=rhythm">Set up your rhythm</a> or <a href="/onboarding">connect a calendar</a>.</div>';
    return;
  }
  el.innerHTML = items.map(i => {
    let why = String((i.reason || [])[0] || '');
    if (why.length > 90) why = why.slice(0, 87) + '…';
    const when = (i.when_label && i.when_label !== (i.label || '')) ? i.when_label : '';
    return '<div class="hz-item" title="' + MnemosEsc((i.reason || []).join(' · ')) + '">'
      + (when ? ('<span class="when">' + MnemosEsc(when) + '</span>') : '')
      + MnemosEsc(i.label || '')
      + (why ? ('<div class="meta">' + MnemosEsc(why) + '</div>') : '')
      + '</div>';
  }).join('');
}

function renderRisk(items) {
  const el = document.getElementById('riskList');
  if (!items.length) {
    el.innerHTML = '<div class="ok-line"><span class="d"></span>No commitments look overdue.</div>';
    return;
  }
  el.innerHTML = items.map(r =>
    '<div class="risk-row"><div class="t">' + MnemosEsc(r.text || '')
    + '<div class="meta">' + MnemosEsc((r.why || []).join(' · ') || 'at risk')
    + (r.subject ? (' · ' + MnemosEsc(r.subject)) : '') + '</div></div>'
    + '<span class="pill urgent">' + (r.risk != null ? Math.round(r.risk * 100) + '%' : 'risk')
    + '</span></div>'
  ).join('');
}

/* Recently noticed — today's memories in plain language (spec §6 contract:
   summary + meta line; raw payload only inside the disclosure). */
function memDotClass(e) {
  if ((e.source || '').indexOf('chat') === 0 || e.modality === 'chat') return 'chat';
  return 'screen';
}
function memMeta(e) {
  const src = (e.source || '').indexOf('desktop.') === 0 ? 'Screen'
    : e.modality === 'vision' ? 'Screen'
    : e.modality === 'audio' ? 'Audio'
    : (e.source || '').indexOf('chat') === 0 ? 'Chat'
    : (e.modality || 'Memory');
  const when = e.time
    ? new Date(e.time * 1000).toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})
    : '';
  return [src, e.window || e.monitor || '', when].filter(Boolean).join(' · ');
}
async function loadRecent() {
  const el = document.getElementById('recentList');
  const seeAll = document.getElementById('recentSeeAll');
  let j;
  try {
    j = await (await fetch('/console/events?limit=6', {cache: 'no-store'})).json();
  } catch (e) { return; }
  const events = (j && j.events) || [];
  _memToday = (j && j.total) != null ? j.total : events.length;
  if (seeAll) {
    seeAll.hidden = !_memToday;
    seeAll.textContent = 'See all ' + _memToday + ' memories in Memory →';
  }
  updateMastLine(_lastShellData || {});
  if (!events.length) {
    el.innerHTML = '<div class="empty">Nothing captured yet today. Memories appear as you '
      + 'work, chat, and record — <a href="/chat">start a chat</a> or turn on capture.</div>';
    return;
  }
  el.innerHTML = events.map(e => {
    const text = e.summary || e.text || '(empty)';
    const raw = e.summary && e.text && e.text !== e.summary ? e.text : null;
    return '<div class="mem"><span class="d ' + memDotClass(e) + '"></span><div>'
      + '<p>' + MnemosEsc(text) + '</p>'
      + '<p class="meta">' + MnemosEsc(memMeta(e)) + '</p>'
      + (raw ? ('<details class="disclosure"><summary>Show raw capture</summary><pre>'
        + MnemosEsc(raw) + '</pre></details>') : '')
      + '</div></div>';
  }).join('');
}
let _lastShellData = null;
const _origLoadShell = loadShell;
loadShell = async function () {
  await _origLoadShell();
  /* keep for mast-line recompute when the memory count arrives */
  try { _lastShellData = JSON.parse(_shellSig); } catch (e) {}
};

async function loadAmbient() {
  try {
    const intel = await (await fetch('/home/intelligence')).json();
    MnemosAmbient.render(document.getElementById('ambientBox'), intel.ambient || []);
  } catch (e) {
    MnemosAmbient.render(document.getElementById('ambientBox'), [{text: 'Listening…'}]);
  }
}

function renderForgot(items) {
  const sec = document.getElementById('secForgot');
  const el = document.getElementById('forgotList');
  if (!items.length) { sec.hidden = true; return; }
  sec.hidden = false;
  el.innerHTML = items.map(f =>
    '<div class="forgot-row"><span>' + MnemosEsc(f.summary || ('event ' + f.event_id))
    + '</span>'
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

/* §7 review fixtures: ?empty=focus|horizon|risk|recent|all */
(function applyEmptyFixture() {
  try {
    const which = new URLSearchParams(location.search).get('empty');
    if (!which) return;
    const orig = loadShell;
    loadShell = async function () {
      await orig();
      const data = { attention: { wm: [], horizon: [], at_risk: [] },
        proposal: null, queued_offers: 0, awaiting_approval: false, forgotten: [] };
      if (which === 'focus' || which === 'all') renderWm([]);
      if (which === 'horizon' || which === 'all') renderHorizon([]);
      if (which === 'risk' || which === 'all') renderRisk([]);
      if (which === 'all') updateMastLine(data);
    };
    if (which === 'recent' || which === 'all') {
      const origRecent = loadRecent;
      loadRecent = async function () {
        await origRecent();
        document.getElementById('recentList').innerHTML =
          '<div class="empty">Nothing captured yet today. Memories appear as you '
          + 'work, chat, and record — <a href="/chat">start a chat</a> or turn on capture.</div>';
      };
    }
  } catch (e) {}
})();

loadShell();
loadRecent();
loadAmbient();
pollTimer = setInterval(() => {
  if (!document.hidden) { loadShell(); loadRecent(); }
}, 12000);
if (window.MnemosFieldStream) {
  const live = MnemosFieldStream.connect(() => loadShell());
  if (live) clearInterval(pollTimer), pollTimer = setInterval(() => {
    if (!document.hidden) { loadShell(); loadRecent(); }
  }, 45000);
}
</script>
</body>
</html>
""").replace("@@VER@@", _ver)
