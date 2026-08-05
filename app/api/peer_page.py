"""Team page (/peer): pair two Mnemos instances, set each peer's disclosure
policy, and decide the approval queue. The page only renders state and calls
the /peer/* endpoints — every rule (single-use codes, personal-never-auto,
offer-by-default) lives in services/peer_channel.py, not here."""

from app.api.mnemos_theme import apply as _mnemos

PEER_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Team</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 var(--font);color:var(--text);background:var(--paper)}
.top{position:sticky;top:0;display:flex;gap:14px;align-items:center;padding:10px 20px;z-index:5}
.wrap{max-width:860px;margin:0 auto;padding:26px 20px 80px}
h1{font-family:var(--display);font-weight:400;font-size:2rem;letter-spacing:-.02em;color:var(--navy);margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 22px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px;box-shadow:var(--shadow);margin-bottom:18px;animation:fadeUp .35s var(--ease) both}
.panel h2{font-family:var(--display);font-weight:400;font-size:1.35rem;margin:0 0 10px;color:var(--navy)}
.btn{appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;border-radius:12px;
  padding:11px 20px;background:var(--navy);color:#F8F6F1}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn-sm{padding:7px 14px;font-size:.88rem}
.muted{color:var(--mut);font-size:.92rem}
.code{font-family:var(--mono);font-size:2.2rem;letter-spacing:.35em;color:var(--navy);padding:8px 0 2px}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
input,select{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev);color:var(--text)}
input{flex:1;min-width:200px}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);text-align:left;padding:6px 8px}
td{border-top:1px solid var(--line);padding:9px 8px;vertical-align:top}
.linkish{background:none;border:0;color:var(--danger);cursor:pointer;font:inherit;font-size:.85rem;padding:0}
.ok{color:var(--ok);font-weight:600}
.ask{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0;
  display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.ask .q{flex:1;min-width:240px}
.tag{display:inline-block;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;
  border:1px solid var(--line);border-radius:99px;padding:2px 9px;color:var(--mut);margin-left:8px}
.pol{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pol select{padding:5px 8px;font-size:.85rem;border-radius:8px}
.pol label{font-size:.78rem;color:var(--mut)}
.note{border:1px solid rgba(199,138,44,.35);background:rgba(199,138,44,.08);
  border-radius:12px;padding:12px 14px;font-size:.92rem;margin:12px 0}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Team</span><span class="spacer"></span>
  <nav class="nav"><a href="/today">Today</a><a href="/chat">Chat</a><a href="/memory">Memory</a>
  <a href="/profile">You</a>
  <a href="/desktop-access">Desktop</a><a href="/phone">Phone</a><a class="on" href="/peer">Team</a></nav></div>
<div class="wrap">
  <h1>Team</h1>
  <p class="lead">Pair with a teammate who also runs @@BRAND@@. Their assistant can then ask
  yours questions — answered from <b>your</b> memory, by <b>your</b> models, only when
  <b>you</b> allow it. Raw memory never leaves your machine; by default every question
  waits for your approval below.</p>

  <div class="panel">
    <h2>Pair with a teammate</h2>
    <div class="row">
      <button class="btn" id="startBtn" type="button">Show a pairing code</button>
      <span class="muted" style="align-self:center">Tell your teammate the code and your
      address — codes work once and expire in 10 minutes.</span>
    </div>
    <div id="pairBox" hidden>
      <div class="code" id="codeText"></div>
      <div class="muted">Your address: <span id="myUrl" style="font-family:var(--mono)"></span></div>
    </div>
    <hr style="border:0;border-top:1px solid var(--line);margin:16px 0">
    <p class="muted" style="margin:0 0 6px">Or join a teammate showing a code:</p>
    <div class="row">
      <input id="joinUrl" placeholder="Their address, e.g. http://192.168.1.20:8000">
      <input id="joinCode" placeholder="6-digit code" style="max-width:140px">
      <button class="btn btn-ghost" id="joinBtn" type="button">Join</button>
    </div>
    <div id="pairMsg" class="muted"></div>
  </div>

  <div class="panel">
    <h2>Approval queue</h2>
    <p class="muted">Questions from teammates waiting on you. Approving composes the answer
    from your memory, removes anything secret-shaped, and sends only that text.</p>
    <div id="asksBox" class="muted">Nothing waiting.</div>
  </div>

  <div class="panel">
    <h2>Your team</h2>
    <p class="muted">What each teammate's assistant may ask without interrupting you.
    <b>Ask me</b> = you approve each one (the default). <b>Answer</b> = share automatically.
    <b>Decline</b> = refuse automatically. Personal topics can never be shared automatically.</p>
    <div id="peersBox" class="muted">No teammates paired yet.</div>
  </div>

  <div class="panel">
    <h2>Asked &amp; answered</h2>
    <div id="sentBox" class="muted">You haven't asked a teammate anything yet.</div>
  </div>
</div>
<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
const LABEL={auto:'Answer',offer:'Ask me',deny:'Decline'};
const TOPIC={availability:'Schedule & availability',work:'Work & projects',contact:'Contact details',personal:'Personal',other:'Everything else'};
let CLASSES=[],ACTIONS=[];

async function refresh(){
  const s=await fetch('/peer/status').then(r=>r.json());
  CLASSES=s.classes||[];ACTIONS=s.actions||[];
  $('myUrl').textContent=s.base_url||'';
  renderAsks(s.pending_asks||[]);renderPeers(s.peers||[]);renderSent(s.sent||[]);
}
function renderAsks(asks){
  if(!asks.length){$('asksBox').textContent='Nothing waiting.';return}
  $('asksBox').innerHTML=asks.map(a=>{
    const hand=a.kind==='handoff';
    return `<div class="ask">
    <div class="q"><b>${esc(a.peer_name)}</b> ${hand?'hands you a task':'asks'}: “${esc(a.question)}”
      ${hand?'<span class="tag">task handoff</span>':''}
      ${a.topic?`<span class="tag">${esc(TOPIC[a.topic]||a.topic)}</span>`:''}</div>
    <button class="btn btn-sm" onclick="decide('${a.id}',true)">${hand?'Accept task':'Approve &amp; send'}</button>
    <button class="btn btn-ghost btn-sm" onclick="decide('${a.id}',false)">Decline</button>
  </div>`}).join('');
}
async function decide(id,yes){
  const r=await post(yes?'/peer/asks/approve':'/peer/asks/deny',{id});
  if(r.answer!==undefined&&yes)alert('Sent:\n\n'+r.answer);
  refresh();
}
function renderPeers(peers){
  if(!peers.length){$('peersBox').textContent='No teammates paired yet.';return}
  $('peersBox').innerHTML='<table><tr><th>Teammate</th><th>Allowed without asking</th><th></th></tr>'+
    peers.map(p=>`<tr><td><b>${esc(p.name)}</b><div class="muted" style="font-family:var(--mono);font-size:.78rem">${esc(p.base_url)}</div></td>
    <td><div class="pol">${CLASSES.map(c=>`<label>${esc(TOPIC[c]||c)}
      <br><select data-peer="${p.peer_id}" data-cls="${c}" onchange="savePolicy('${p.peer_id}')"
        ${c==='personal'?'title="Personal topics can never be shared automatically"':''}>
      ${ACTIONS.map(a=>(c==='personal'&&a==='auto')?'':`<option value="${a}" ${((p.policy||{})[c]||'offer')===a?'selected':''}>${LABEL[a]}</option>`).join('')}
      </select></label>`).join('')}</div></td>
    <td><button class="linkish" onclick="revoke('${p.peer_id}')">unpair</button></td></tr>`).join('')+'</table>';
}
async function savePolicy(pid){
  const policy={};
  document.querySelectorAll(`select[data-peer="${pid}"]`).forEach(s=>policy[s.dataset.cls]=s.value);
  const r=await post('/peer/policy',{peer_id:pid,policy});
  if(r.detail)alert(r.detail);
}
async function revoke(pid){
  if(!confirm('Unpair this teammate? Tokens die in both directions.'))return;
  await post('/peer/revoke',{peer_id:pid});refresh();
}
function renderSent(rows){
  if(!rows.length){$('sentBox').textContent="You haven't asked a teammate anything yet.";return}
  $('sentBox').innerHTML='<table><tr><th>To</th><th>Question</th><th>Status</th><th>Answer</th></tr>'+
    rows.slice().reverse().map(r=>`<tr><td>${esc(r.peer_name)}</td><td>${esc(r.question)}</td>
    <td>${esc(r.status)}</td><td>${esc(r.answer||'')}</td></tr>`).join('')+'</table>';
}
$('startBtn').onclick=async()=>{
  const r=await post('/peer/pair/start');
  if(!r.ok){$('pairMsg').textContent=r.error||'could not start pairing';return}
  $('pairBox').hidden=false;$('codeText').textContent=r.code;$('myUrl').textContent=r.base_url;
};
$('joinBtn').onclick=async()=>{
  $('pairMsg').textContent='Joining…';
  const r=await post('/peer/pair/join',{url:$('joinUrl').value.trim(),code:$('joinCode').value.trim()});
  $('pairMsg').innerHTML=r.ok?`<span class="ok">✓ Paired with ${esc(r.name)}.</span>`:esc(r.error||'join failed');
  refresh();
};
refresh();setInterval(refresh,5000);
</script>
</body>
</html>""")
