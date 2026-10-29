"""Meeting note page — enhanced session note with evidence playback (P3)."""

from app.api.mnemos_theme import apply as _mnemos

MEETING_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meeting · @@BRAND@@</title>
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
    linear-gradient(180deg,#131318 0%,var(--paper) 40%,var(--workspace) 100%);
}
.wrap{max-width:1120px;margin:0 auto;padding:8px 24px 64px}
.mast{padding:18px 0 8px}
.mast .kicker{font:12px var(--sans);color:var(--mut);}
.mast h1{
  font-family:var(--display);font-weight:400;font-size:clamp(1.6rem,3.2vw,2.1rem);
  color:var(--navy);margin:6px 0 0;letter-spacing:-.02em;line-height:1.15;
}
.mast .summary{color:var(--text);font-size:15px;margin:12px 0 0;max-width:42em;line-height:1.55}
.mast .meta{font:12px var(--mono);color:var(--mut);margin-top:10px}
.privacy{
  margin-top:14px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--radius);
  background:rgba(19,19,24,.7);font-size:13px;line-height:1.45;
}
.privacy .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.privacy .pill{
  font:11px var(--mono);padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mut);
}
.privacy button{
  border-radius:8px;padding:7px 12px;font:500 12px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
}
.privacy button.on{background:var(--acc);color:var(--acc-fg);border:none}
.stack{display:flex;flex-direction:column;gap:10px;margin-top:18px}
.item{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);padding:14px 16px;
}
.item .kind{
  font:11px/1.2 var(--sans);color:var(--acc);
}
.item .text{margin:6px 0 0;font-size:15px;color:var(--navy);line-height:1.45}
.item .detail{margin:6px 0 0;font-size:13px;color:var(--mut)}
.item .subject{font:12px var(--mono);color:var(--mut);margin-top:4px}
.receipts{margin-top:10px;display:flex;flex-direction:column;gap:8px}
.receipt{
  border-top:1px solid var(--line);padding-top:8px;font-size:13px;
}
.receipt .quote{color:var(--text);line-height:1.45}
.receipt mark{
  background:var(--acc-18);color:inherit;padding:0 2px;border-radius:2px;
}
.receipt .actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:6px;align-items:center}
.receipt button,.item button,.actions button,.actions a.btnish{
  border-radius:8px;padding:6px 12px;font:500 12px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
  text-decoration:none;display:inline-flex;align-items:center;
}
.receipt button.play{background:var(--acc);color:var(--acc-fg);border:none}
.receipt.playing{
  border-left:3px solid var(--acc);padding-left:10px;
  background:var(--acc-06);border-radius:0 8px 8px 0;
}
.receipt .pill.removed{color:var(--mut);border-style:dashed}
.playerbar{
  position:sticky;bottom:0;z-index:var(--z-raised);margin-top:18px;padding:10px 14px;
  display:flex;gap:12px;align-items:center;flex-wrap:wrap;
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);
}
.playerbar .now{flex:1;min-width:200px;font-size:13px;color:var(--text);line-height:1.4}
.playerbar .now .lbl{font:11px var(--sans);color:var(--acc);display:block}
.playerbar audio{width:min(360px,100%);height:36px}
.playerbar button.close{
  border-radius:8px;padding:6px 10px;font:500 12px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
}
.item .row-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.item.dismissed{opacity:.55}
.item .pill{
  font:11px var(--mono);padding:2px 7px;border-radius:999px;border:1px solid var(--line);
  color:var(--mut);
}
.empty{color:var(--mut);padding:24px 0;font-family:var(--display);font-size:1.1rem}
.panel{
  margin-top:22px;padding:16px 18px;background:var(--surface);
  border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);
}
.panel h2{
  font-family:var(--display);font-weight:400;font-size:1.25rem;
  color:var(--navy);margin:0 0 6px;
}
.panel .hint{font-size:13px;color:var(--mut);margin:0 0 12px}
.ask-row{display:flex;gap:8px;flex-wrap:wrap}
.ask-row input{
  flex:1;min-width:180px;padding:10px 12px;border:1px solid var(--line);
  border-radius:8px;font:15px var(--font);background:var(--panel);color:var(--navy);
}
.ask-row button,.draft-btn{
  border-radius:8px;padding:10px 14px;font:500 13px var(--font);cursor:pointer;
  border:none;background:var(--acc);color:var(--acc-fg);
}
.draft-btn{background:var(--acc);margin-top:4px}
.ask-out{
  margin-top:12px;font-size:14px;line-height:1.5;color:var(--text);
  white-space:pre-wrap;
}
.ask-out .err{color:#8B3A2A}
.db-grant{
  margin:0 0 12px;padding:10px 12px;border:1px dashed var(--line);border-radius:8px;
  font-size:13px;color:var(--mut);display:flex;gap:8px;flex-wrap:wrap;align-items:center;
}
.db-grant button,.db-row button{
  border-radius:8px;padding:7px 12px;font:500 12px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
}
.db-list{display:flex;flex-direction:column;gap:8px}
.db-row{border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--panel)}
.db-row.sent{opacity:.7}
.db-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.db-head label{display:flex;gap:8px;align-items:center;cursor:pointer;color:var(--navy);font-weight:500}
.db-meta{font:12px var(--mono);color:var(--mut)}
.db-head select,.db-row input[type=email],.db-row input.db-subj,.db-row textarea{
  border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--navy);
  font:13px var(--font);padding:5px 8px;
}
.db-pill{font:11px var(--mono);padding:2px 7px;border-radius:999px;border:1px solid var(--line);color:var(--mut)}
.db-pill.sent{color:var(--acc);border-color:var(--acc)}
.db-pill.failed{color:#8B3A2A;border-color:#8B3A2A}
.db-row details{margin-top:8px}
.db-row summary{cursor:pointer;font-size:13px;color:var(--mut)}
.db-row input.db-subj{width:100%;margin-top:8px}
.db-row textarea{width:100%;min-height:150px;margin-top:6px;line-height:1.45;resize:vertical}
.db-email{margin-top:8px;display:flex;gap:6px;align-items:center;font-size:13px;color:var(--mut)}
.db-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.draft-btn[disabled]{opacity:.45;cursor:default}
.foot{margin-top:28px;display:flex;gap:14px;flex-wrap:wrap;font-size:13px}
.foot a{color:var(--navy)}
@media(max-width:640px){
  .wrap{padding:8px 14px 48px}
  .mast h1{font-size:clamp(1.35rem,5vw,1.85rem)}
  .privacy .row{flex-direction:column;align-items:flex-start}
  .ask-row{flex-direction:column}
  .ask-row input{min-width:0;width:100%}
  .receipt .actions{flex-direction:column;align-items:flex-start}
}
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Meeting</span>
  @@NAV@@
  <span class="spacer"></span>
</header>
@@APPROVAL@@
<div class="wrap">
  <header class="mast">
    <div class="kicker" id="kicker">Meeting note</div>
    <h1 id="title">—</h1>
    <p class="summary" id="summary"></p>
    <div class="meta" id="meta"></div>
    <div class="privacy" id="privacy" hidden>
      <div id="privacyLead"></div>
      <div class="row" id="privacyPills"></div>
      <div class="row" id="retentionBtns">
        <button type="button" data-ret="transcript_only">Transcript-only</button>
        <button type="button" data-ret="keep_receipts">Keep receipts</button>
      </div>
    </div>
  </header>
  <div class="stack" id="items"></div>
  <div class="empty" id="empty" hidden>No enhanced note yet for this meeting.</div>
  <div class="playerbar" id="playerBar" hidden>
    <div class="now"><span class="lbl">Playing the moment</span><span id="nowPlaying"></span></div>
    <audio id="player" controls preload="none"></audio>
    <button type="button" class="close" id="playerClose">Close</button>
  </div>
  <section class="panel" id="askPanel" hidden>
    <h2>Ask this meeting</h2>
    <p class="hint">Answers stay scoped to this note’s transcript, facts, and attendees.</p>
    <div class="ask-row">
      <input id="askQ" type="text" placeholder="What did we decide about…" autocomplete="off">
      <button type="button" id="askBtn">Ask</button>
    </div>
    <div class="ask-out" id="askOut"></div>
  </section>
  <section class="panel" id="debriefPanel" hidden>
    <h2>Debrief</h2>
    <p class="hint">A thank-you to each person on the call with what you covered, written for how you know them. Pick who gets one — nothing sends until you press Send.</p>
    <div class="db-grant" id="dbGrant" hidden></div>
    <div class="db-list" id="dbList"></div>
    <div class="db-actions">
      <button type="button" class="draft-btn" id="dbDraft">Draft selected</button>
      <button type="button" class="draft-btn" id="dbSend" disabled>Send selected</button>
    </div>
    <div class="ask-out" id="dbOut"></div>
  </section>
  <section class="panel" id="draftPanel" hidden>
    <h2>Follow-up</h2>
    <p class="hint">Drafts a short email citing this meeting’s commitments and decisions — approval before send.</p>
    <button type="button" class="draft-btn" id="draftBtn">Draft follow-up</button>
    <div class="ask-out" id="draftOut"></div>
  </section>
  <footer class="foot">
    <a href="/today">← Today</a>
    <a href="/memory">Memory</a>
    <a href="/meetings">All meeting notes</a>
    <a href="/chat">Chat</a>
  </footer>
</div>
@@UI_JS@@
<script>
const noteId = @@NOTE_ID@@;

function art(p) {
  if (!p) return '';
  return '/artifact?path=' + encodeURIComponent(p);
}

// One visible player for the page. A clip is one utterance; the quoted words
// sit somewhere inside it, so playback starts at their offset when the
// server could locate them and from the top when it could not. The receipt
// being played is marked, and the bar stays until closed so the listener can
// scrub back for context.
function clearPlaying() {
  document.querySelectorAll('.receipt.playing').forEach(el => el.classList.remove('playing'));
}
function playMoment(btn) {
  const path = btn.getAttribute('data-path');
  if (!path) return;
  const start = parseFloat(btn.getAttribute('data-start') || '0') || 0;
  const label = btn.getAttribute('data-label') || '';
  const a = document.getElementById('player');
  const bar = document.getElementById('playerBar');
  const src = art(path);
  clearPlaying();
  const receipt = btn.closest('.receipt');
  if (receipt) receipt.classList.add('playing');
  document.getElementById('nowPlaying').textContent = label;
  bar.hidden = false;
  const seekAndPlay = () => {
    try { if (start > 0) a.currentTime = start; } catch (e) {}
    a.play().catch(() => {});
  };
  if (a.getAttribute('data-src') !== src) {
    a.setAttribute('data-src', src);
    a.src = src;
    a.addEventListener('loadedmetadata', seekAndPlay, {once: true});
    a.load();
  } else {
    seekAndPlay();
  }
}
function playPath(path) {
  // Kept for callers that only have a path.
  const fake = document.createElement('button');
  fake.setAttribute('data-path', path);
  playMoment(fake);
}
document.getElementById('playerClose').onclick = () => {
  const a = document.getElementById('player');
  try { a.pause(); } catch (e) {}
  clearPlaying();
  document.getElementById('playerBar').hidden = true;
};
document.getElementById('player').addEventListener('ended', clearPlaying);

function audioPill(ev) {
  if (ev.audio_state === 'removed') {
    return '<span class="pill removed" title="The audio was deleted when this meeting was kept transcript-only. The words stay.">audio removed · transcript-only</span>';
  }
  return '<span class="pill">no audio captured</span>';
}

function spanHtml(hl, fallback) {
  if (!hl) return MnemosEsc(fallback || '');
  return MnemosEsc(hl.before || '')
    + '<mark>' + MnemosEsc(hl.match || '') + '</mark>'
    + MnemosEsc(hl.after || '');
}

function fmtWhen(ts) {
  if (!ts) return '';
  try {
    return new Date(ts * 1000).toLocaleString([], {
      weekday: 'short', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (e) { return ''; }
}

async function reviewItem(id, verb) {
  await fetch('/reflection_items/' + id + '/' + verb, {method: 'POST'});
  load();
}

async function load() {
  const url = noteId
    ? ('/meeting/note/' + noteId + '?format=json')
    : '/meeting/note/latest?format=json';
  const data = await (await fetch(url)).json();
  const note = data.note;
  const empty = document.getElementById('empty');
  const stack = document.getElementById('items');
  if (!note) {
    empty.hidden = false;
    stack.innerHTML = '';
    document.getElementById('title').textContent = 'No meeting note yet';
    return;
  }
  empty.hidden = true;
  document.getElementById('title').textContent = note.title || 'Meeting note';
  document.getElementById('summary').textContent = note.summary || '';
  document.getElementById('meta').textContent = [
    fmtWhen(note.period_start),
    note.period_end ? ('– ' + fmtWhen(note.period_end)) : '',
    note.model ? ('· ' + note.model) : '',
  ].filter(Boolean).join(' ');
  document.title = (note.title || 'Meeting') + ' · Sparrow';
  const rid = note.id;
  document.getElementById('askPanel').hidden = !rid;
  document.getElementById('draftPanel').hidden = !rid;
  window.__meetingNoteId = rid || null;
  if (rid) loadDebrief();
  // The meeting's own id when the note has one (stable); a derived session
  // id only for legacy notes.
  window.__meetingSessionId = note.meeting_session_id || null;
  window.__legacySessionId = (note.subject_type === 'session') ? (note.subject_id || null) : null;
  renderPrivacy(note.privacy || {});

  stack.innerHTML = (note.items || []).map(it => {
    const dismissed = it.review === 'dismissed' ? ' dismissed' : '';
    const pill = it.review
      ? ('<span class="pill">' + MnemosEsc(it.review) + '</span>') : '';
    const receipts = (it.evidence || []).map(ev => {
      const quote = spanHtml(ev.span_highlight, ev.source_span || ev.text || '');
      const start = (ev.clip_start_s != null) ? ev.clip_start_s : 0;
      const label = (ev.source_span || ev.text || '').slice(0, 140);
      const play = ev.playable && ev.play_path
        ? ('<button type="button" class="play" data-path="'
           + MnemosEsc(ev.play_path) + '" data-start="' + MnemosEsc(String(start))
           + '" data-label="' + MnemosEsc(label) + '">Play the moment'
           + (ev.clip_start_s != null ? '' : ' (from start)') + '</button>')
        : audioPill(ev);
      return '<div class="receipt"><div class="quote">' + quote + '</div>'
        + '<div class="actions">' + play
        + '<span class="pill">fact ' + MnemosEsc(String(ev.fact_id || '')) + '</span>'
        + '</div></div>';
    }).join('');
    return '<article class="item' + dismissed + '" data-id="' + it.id + '">'
      + '<div class="kind">' + MnemosEsc(it.kind || '') + ' ' + pill + '</div>'
      + '<div class="text">' + MnemosEsc(it.text || '') + '</div>'
      + (it.detail ? ('<div class="detail">' + MnemosEsc(it.detail) + '</div>') : '')
      + (it.subject ? ('<div class="subject">' + MnemosEsc(it.subject) + '</div>') : '')
      + (receipts ? ('<div class="receipts">' + receipts + '</div>') : '')
      + '<div class="row-actions">'
      + '<button type="button" data-verb="approve">Approve</button>'
      + '<button type="button" data-verb="dismiss">Dismiss</button>'
      + '</div></article>';
  }).join('');

  stack.querySelectorAll('button.play').forEach(btn => {
    btn.onclick = () => playMoment(btn);
  });
  stack.querySelectorAll('.row-actions button').forEach(btn => {
    btn.onclick = () => {
      const art = btn.closest('.item');
      const id = art && art.getAttribute('data-id');
      const verb = btn.getAttribute('data-verb');
      if (id && verb) reviewItem(id, verb);
    };
  });
}
function renderPrivacy(priv) {
  const box = document.getElementById('privacy');
  if (!box) return;
  box.hidden = false;
  const cons = priv.consent || {};
  const ret = priv.retention || {};
  const lead = document.getElementById('privacyLead');
  const retLabel = (ret.retention === 'keep_receipts'
    ? 'Keep receipts (audio retained for playback)'
    : 'Transcript-only (WAVs deleted; note stays)')
    + (ret.source === 'meeting_session' ? ', as chosen when the meeting started' : '');
  lead.textContent = (cons.consented
    ? 'Capture was consented for this workspace. '
    : 'Capture consent was off or unknown for this note. ')
    + 'Retention: ' + retLabel + '. '
    + (priv.tradeoff || '');
  const pills = document.getElementById('privacyPills');
  const src = (cons.sources_on || []).map(s =>
    '<span class="pill">' + MnemosEsc(s) + '</span>').join('');
  pills.innerHTML = src || '<span class="pill">no sources on</span>';
  document.querySelectorAll('#retentionBtns button').forEach(btn => {
    const v = btn.getAttribute('data-ret');
    btn.classList.toggle('on', v === ret.retention);
    btn.onclick = () => setRetention(v);
  });
}

async function setRetention(choice) {
  const msid = window.__meetingSessionId;
  const sid = window.__legacySessionId;
  try {
    await fetch('/meeting/retention', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        retention: choice,
        meeting_session_id: msid || null,
        session_id: msid ? null : (sid || null),
        default: false,
      }),
    });
    load();
  } catch (e) {}
}

async function askMeeting() {
  const rid = window.__meetingNoteId;
  const q = (document.getElementById('askQ').value || '').trim();
  const out = document.getElementById('askOut');
  if (!rid || !q) return;
  out.textContent = 'Thinking…';
  try {
    const r = await fetch('/meeting/note/' + rid + '/ask', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q}),
    });
    const data = await r.json();
    if (!r.ok) {
      out.innerHTML = '<span class="err">' + MnemosEsc(data.detail || 'ask failed') + '</span>';
      return;
    }
    out.textContent = data.answer || '(no answer)';
  } catch (e) {
    out.innerHTML = '<span class="err">' + MnemosEsc(String(e)) + '</span>';
  }
}

async function draftFollowup() {
  const rid = window.__meetingNoteId;
  const out = document.getElementById('draftOut');
  if (!rid) return;
  out.textContent = 'Queueing draft…';
  try {
    const r = await fetch('/meeting/note/' + rid + '/draft', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({dry_run: 'draft'}),
    });
    const data = await r.json();
    if (!r.ok) {
      out.innerHTML = '<span class="err">' + MnemosEsc(data.detail || 'draft failed') + '</span>';
      return;
    }
    const n = (data.source_fact_ids || []).length;
    out.textContent = 'Follow-up queued'
      + (n ? (' · citing ' + n + ' fact' + (n === 1 ? '' : 's')) : '')
      + '. Open Chat to approve the draft.';
  } catch (e) {
    out.innerHTML = '<span class="err">' + MnemosEsc(String(e)) + '</span>';
  }
}

// --- Debrief: one thank-you per person on the call ------------------------
let __db = null;
// The user's ticks survive every re-render (a relationship change reloads the
// list) — a recipient they unticked must never come back ticked.
const __dbPick = {};
// MnemosEsc leaves quotes alone; attribute values here can come from an
// invite a stranger wrote, so they get the full treatment.
function dbAttr(v) {
  return MnemosEsc(v).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function dbRow(a, rels) {
  const sent = a.status === 'sent';
  const opts = ['<option value="">relationship…</option>'].concat(rels.map(r =>
    '<option value="' + dbAttr(r) + '"' + (r === a.relationship ? ' selected' : '') + '>'
    + MnemosEsc(r) + '</option>')).join('');
  const meta = [a.role, a.org, a.email || 'no email yet'].filter(Boolean).join(' · ');
  const relTag = a.relationship_source === 'suggested'
    ? '<span class="db-pill" title="' + dbAttr(a.relationship_why || '') + '">suggested</span>' : '';
  const status = a.status === 'none' ? '' : '<span class="db-pill ' + dbAttr(a.status) + '">'
    + MnemosEsc(a.status) + '</span>';
  const pick = !sent && ((a.key in __dbPick) ? __dbPick[a.key]
                                            : (a.status !== 'none' || !!a.email));
  let html = '<div class="db-row' + (sent ? ' sent' : '') + '" data-key="' + dbAttr(a.key) + '">'
    + '<div class="db-head"><label><input type="checkbox" class="db-pick"'
    + (pick ? ' checked' : '') + (sent ? ' disabled' : '') + '> ' + MnemosEsc(a.name) + '</label>'
    + '<span class="db-meta">' + MnemosEsc(meta) + '</span>'
    + '<select class="db-rel"' + (sent ? ' disabled' : '') + '>' + opts + '</select>' + relTag + status
    + '</div>';
  if (!a.email && !sent) {
    html += '<div class="db-email">Email <input type="email" class="db-addr" placeholder="name@company.com">'
      + '<button type="button" class="db-addr-save">Save</button></div>';
  }
  if (a.error) html += '<div class="ask-out"><span class="err">' + MnemosEsc(a.error) + '</span></div>';
  if (a.body) {
    html += '<details' + (a.status === 'failed' ? ' open' : '') + '><summary>'
      + (sent ? 'Sent' : 'Draft') + ' · ' + MnemosEsc(a.subject) + '</summary>'
      + '<input class="db-subj" value="' + dbAttr(a.subject) + '"' + (sent ? ' disabled' : '') + '>'
      + '<textarea class="db-body"' + (sent ? ' disabled' : '') + '>' + MnemosEsc(a.body) + '</textarea>'
      + '</details>';
  }
  return html + '</div>';
}

function dbGrant(st) {
  const box = document.getElementById('dbGrant');
  const send = st.send || {};
  if (!send.enabled) {
    box.hidden = false;
    box.textContent = 'Sending is off on this install — drafts only.';
    return;
  }
  if (send.provider) {
    const p = (send.providers || []).find(x => x.id === send.provider) || {};
    box.hidden = false;
    box.innerHTML = 'Sends from your ' + MnemosEsc(p.label || send.provider)
      + (p.account ? ' (' + MnemosEsc(p.account) + ')' : '') + '.';
    return;
  }
  const btns = (send.providers || []).filter(p => p.configured).map(p =>
    '<button type="button" data-grant="' + dbAttr(p.id) + '">Allow sending from '
    + MnemosEsc(p.label) + '</button>').join('');
  box.hidden = false;
  box.innerHTML = btns
    ? ('To send, allow Sparrow to send mail as you — a separate permission from reading. ' + btns)
    : 'No mail account is set up for sending on this install.';
  box.querySelectorAll('[data-grant]').forEach(b => b.onclick = async () => {
    try {
      const r = await fetch('/mail/send/grant', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({provider: b.dataset.grant,
                              return_path: location.pathname})});
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'could not start');
      location.href = d.auth_url;
    } catch (e) { dbSay(String(e.message || e), true); }
  });
}

function dbSay(msg, err) {
  const out = document.getElementById('dbOut');
  out.innerHTML = err ? '<span class="err">' + MnemosEsc(msg) + '</span>' : MnemosEsc(msg);
}

function dbRender(st) {
  __db = st;
  const panel = document.getElementById('debriefPanel');
  panel.hidden = !st.enabled || !(st.attendees || []).length;
  dbGrant(st);
  const list = document.getElementById('dbList');
  list.innerHTML = (st.attendees || []).map(a => dbRow(a, st.relationships || [])).join('');
  if (st.withheld) {
    list.insertAdjacentHTML('beforeend', '<div class="db-meta">' + st.withheld
      + ' item' + (st.withheld === 1 ? '' : 's') + ' kept out of every draft as sensitive.</div>');
  }
  list.querySelectorAll('.db-row').forEach(row => {
    const a = st.attendees.find(x => x.key === row.dataset.key);
    const rel = row.querySelector('.db-rel');
    if (rel) rel.onchange = () => dbPerson(a, {relationship: rel.value});
    const save = row.querySelector('.db-addr-save');
    if (save) save.onclick = () => dbPerson(a, {email: row.querySelector('.db-addr').value.trim()});
    row.querySelectorAll('.db-subj,.db-body').forEach(el => el.oninput = () => { row.dataset.dirty = '1'; });
    const pick = row.querySelector('.db-pick');
    if (pick) pick.onchange = () => { __dbPick[row.dataset.key] = pick.checked; dbButtons(); };
  });
  dbButtons();
}

function dbPicked() {
  return Array.from(document.querySelectorAll('.db-row')).filter(r => {
    const c = r.querySelector('.db-pick');
    return c && c.checked && !c.disabled;
  });
}

function dbButtons() {
  const picked = dbPicked();
  const sendable = picked.filter(r => {
    const a = __db.attendees.find(x => x.key === r.dataset.key);
    return a && a.body && a.email && (a.status === 'draft' || a.status === 'failed');
  });
  const btn = document.getElementById('dbSend');
  btn.disabled = !__db.can_send || !sendable.length;
  btn.textContent = sendable.length ? ('Send selected (' + sendable.length + ')') : 'Send selected';
  document.getElementById('dbDraft').disabled = !picked.length;
}

async function loadDebrief() {
  const rid = window.__meetingNoteId;
  if (!rid) return;
  try {
    const r = await fetch('/meeting/note/' + rid + '/debrief');
    if (!r.ok) return;
    dbRender(await r.json());
  } catch (e) {}
}

async function dbPerson(a, change) {
  const rid = window.__meetingNoteId;
  try {
    // Save typed edits first: the reload below re-renders every draft.
    await dbSaveEdits(Array.from(document.querySelectorAll('.db-row')));
    const r = await fetch('/meeting/note/' + rid + '/debrief/person', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({key: a.key, name: a.name, email: a.email || '',
                                          relationship: a.relationship || ''}, change))});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'could not save');
    if (change.email && a.body) {
      await fetch('/meeting/note/' + rid + '/debrief/edit', {method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({key: a.key, subject: a.subject, body: a.body, email: change.email})});
    }
    dbSay(change.relationship !== undefined
      ? 'Saved. Redraft ' + a.name + ' to match the new relationship.' : 'Saved.');
    await loadDebrief();
  } catch (e) { dbSay(String(e.message || e), true); }
}

async function dbSaveEdits(rows) {
  const rid = window.__meetingNoteId;
  for (const row of rows) {
    if (!row.dataset.dirty) continue;
    const r = await fetch('/meeting/note/' + rid + '/debrief/edit', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({key: row.dataset.key,
        subject: row.querySelector('.db-subj').value,
        body: row.querySelector('.db-body').value})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'could not save the edit');
    delete row.dataset.dirty;
  }
}

async function dbDraft() {
  const rid = window.__meetingNoteId;
  const rows = dbPicked();
  const keys = rows.map(r => r.dataset.key);
  const redo = __db.attendees.filter(a => keys.includes(a.key) && a.body);
  if (redo.length && !confirm('Redraft ' + redo.length + ' existing draft'
      + (redo.length === 1 ? '' : 's') + '? Edits you made will be replaced.')) return;
  dbSay('Drafting…');
  try {
    const r = await fetch('/meeting/note/' + rid + '/debrief/draft', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({keys: keys, regenerate: true})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'draft failed');
    dbRender(d);
    dbSay('Drafted ' + d.drafted + '. Open each one to read or edit it before sending.');
  } catch (e) { dbSay(String(e.message || e), true); }
}

async function dbSend() {
  const rid = window.__meetingNoteId;
  const rows = dbPicked();
  try {
    await dbSaveEdits(rows);
    const st = await (await fetch('/meeting/note/' + rid + '/debrief')).json();
    const keys = rows.map(r => r.dataset.key);
    const items = st.attendees.filter(a => keys.includes(a.key) && a.body && a.email
      && (a.status === 'draft' || a.status === 'failed'));
    if (!items.length) return;
    const names = items.map(a => a.name + ' <' + a.email + '>').join('\n');
    if (!confirm('Send ' + items.length + ' email' + (items.length === 1 ? '' : 's')
        + ' as you to:\n\n' + names)) return;
    dbSay('Sending…');
    const r = await fetch('/meeting/note/' + rid + '/debrief/send', {method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({items: items.map(a => ({key: a.key, body_hash: a.body_hash}))})});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'send failed');
    const bad = (d.results || []).filter(x => !x.ok);
    dbSay('Sent ' + d.sent + (bad.length ? (' · ' + bad.length + ' not sent: '
      + bad.map(x => x.error).join('; ')) : '.'), bad.length > 0);
    await loadDebrief();
  } catch (e) { dbSay(String(e.message || e), true); }
}

document.getElementById('dbDraft').onclick = dbDraft;
document.getElementById('dbSend').onclick = dbSend;
document.getElementById('askBtn').onclick = askMeeting;
document.getElementById('askQ').addEventListener('keydown', e => {
  if (e.key === 'Enter') { e.preventDefault(); askMeeting(); }
});
document.getElementById('draftBtn').onclick = draftFollowup;
load();
</script>
</body>
</html>
""")


MEETINGS_LIST_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Meetings · @@BRAND@@</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;font:15px/1.55 var(--font);color:var(--text);
  background:linear-gradient(180deg,#131318 0%,var(--paper) 50%,var(--workspace) 100%);
}
.wrap{max-width:1120px;margin:0 auto;padding:8px 24px 64px}
h1{font-family:var(--display);font-weight:400;font-size:1.8rem;color:var(--navy);margin:18px 0 6px}
.lead{color:var(--mut);margin:0 0 18px}
.row{
  display:block;padding:12px 0;border-top:1px solid var(--line);text-decoration:none;color:inherit;
}
.row .t{font-size:16px;color:var(--navy)}
.row .m{font:12px var(--mono);color:var(--mut);margin-top:3px}
.foot{margin-top:28px}
.foot a{color:var(--navy);font-size:13px}
@media(max-width:640px){
  .wrap{padding:8px 14px 48px}
  .row .t{font-size:15px}
}
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Meetings</span>
  @@NAV@@
  <span class="spacer"></span>
</header>
@@APPROVAL@@
<div class="wrap">
  <h1>Meeting notes</h1>
  <p class="lead">Enhanced notes from settled sessions — every line carries a receipt.</p>
  <div id="list"></div>
  <footer class="foot"><a href="/today">← Today</a></footer>
</div>
@@UI_JS@@
<script>
async function load() {
  const data = await (await fetch('/meetings/list')).json();
  const el = document.getElementById('list');
  const rows = data.meetings || [];
  if (!rows.length) {
    el.innerHTML = '<div class="empty-state">Enhanced notes land here after a meeting is captured and settled — each line carries a receipt back to the audio. <a href="/today">Take rough notes on Today</a> or <a href="/capture">start capture</a>.</div>';
    return;
  }
  el.innerHTML = rows.map(m =>
    '<a class="row" href="/meeting/note/' + m.id + '">'
    + '<div class="t">' + MnemosEsc(m.title || 'Meeting') + '</div>'
    + '<div class="m">' + MnemosEsc(m.when || '') + ' · '
    + MnemosEsc(String(m.n_items || 0)) + ' items</div></a>'
  ).join('');
}
load();
</script>
</body>
</html>
""")
