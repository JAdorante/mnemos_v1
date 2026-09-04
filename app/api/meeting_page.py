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
    linear-gradient(180deg,#FBF9F4 0%,var(--paper) 40%,var(--workspace) 100%);
}
.wrap{max-width:780px;margin:0 auto;padding:8px 22px 64px}
.mast{padding:18px 0 8px}
.mast .kicker{font:12px var(--mono);color:var(--mut);letter-spacing:.04em;text-transform:uppercase}
.mast h1{
  font-family:var(--display);font-weight:400;font-size:clamp(1.6rem,3.2vw,2.1rem);
  color:var(--navy);margin:6px 0 0;letter-spacing:-.02em;line-height:1.15;
}
.mast .summary{color:var(--text);font-size:15px;margin:12px 0 0;max-width:42em;line-height:1.55}
.mast .meta{font:12px var(--mono);color:var(--mut);margin-top:10px}
.privacy{
  margin-top:14px;padding:12px 14px;border:1px solid var(--line);border-radius:var(--radius);
  background:rgba(248,246,241,.7);font-size:13px;line-height:1.45;
}
.privacy .row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:8px}
.privacy .pill{
  font:11px var(--mono);padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--mut);
}
.privacy button{
  border-radius:8px;padding:7px 12px;font:500 12px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
}
.privacy button.on{background:var(--navy);color:#F8F6F1;border:none}
.stack{display:flex;flex-direction:column;gap:10px;margin-top:18px}
.item{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);padding:14px 16px;
}
.item .kind{
  font:11px/1.2 var(--mono);color:var(--acc);text-transform:uppercase;letter-spacing:.04em;
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
.receipt button.play{background:var(--navy);color:#F8F6F1;border:none}
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
  border:none;background:var(--navy);color:#F8F6F1;
}
.draft-btn{background:var(--acc);margin-top:4px}
.ask-out{
  margin-top:12px;font-size:14px;line-height:1.5;color:var(--text);
  white-space:pre-wrap;
}
.ask-out .err{color:#8B3A2A}
.foot{margin-top:28px;display:flex;gap:14px;flex-wrap:wrap;font-size:13px}
.foot a{color:var(--navy)}
#player{display:none}
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
  <section class="panel" id="askPanel" hidden>
    <h2>Ask this meeting</h2>
    <p class="hint">Answers stay scoped to this note’s transcript, facts, and attendees.</p>
    <div class="ask-row">
      <input id="askQ" type="text" placeholder="What did we decide about…" autocomplete="off">
      <button type="button" id="askBtn">Ask</button>
    </div>
    <div class="ask-out" id="askOut"></div>
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
<audio id="player"></audio>
@@UI_JS@@
<script>
const noteId = @@NOTE_ID@@;

function art(p) {
  if (!p) return '';
  return '/artifact?path=' + encodeURIComponent(p);
}

function playPath(path) {
  if (!path) return;
  const a = document.getElementById('player');
  a.src = art(path);
  a.play().catch(() => {});
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
  window.__meetingSessionId = note.subject_id || null;
  renderPrivacy(note.privacy || {});

  stack.innerHTML = (note.items || []).map(it => {
    const dismissed = it.review === 'dismissed' ? ' dismissed' : '';
    const pill = it.review
      ? ('<span class="pill">' + MnemosEsc(it.review) + '</span>') : '';
    const receipts = (it.evidence || []).map(ev => {
      const quote = spanHtml(ev.span_highlight, ev.source_span || ev.text || '');
      const play = ev.playable && ev.play_path
        ? ('<button type="button" class="play" data-path="'
           + MnemosEsc(ev.play_path) + '">Play the moment</button>')
        : '<span class="pill">no audio</span>';
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
    btn.onclick = () => playPath(btn.getAttribute('data-path'));
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
  const retLabel = ret.retention === 'keep_receipts'
    ? 'Keep receipts (audio retained for playback)'
    : 'Transcript-only (WAVs deleted; note stays)';
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
  const sid = window.__meetingSessionId;
  try {
    await fetch('/meeting/retention', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        retention: choice,
        session_id: sid || null,
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
  background:linear-gradient(180deg,#FBF9F4 0%,var(--paper) 50%,var(--workspace) 100%);
}
.wrap{max-width:780px;margin:0 auto;padding:8px 22px 64px}
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
    el.innerHTML = '<p class="lead">No enhanced meeting notes yet.</p>';
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
