"""Team page (/peer): pair two Sparrow instances, set each peer's disclosure
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
.top{position:sticky;top:0;display:flex;gap:14px;align-items:center;padding:10px 20px;z-index:var(--z-raised);background:var(--chrome-bg);backdrop-filter:blur(10px)}
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
@media(max-width:640px){
  .top{padding:8px 14px;gap:10px;flex-wrap:wrap}
  .wrap{padding:18px 14px 64px}
  h1{font-size:clamp(1.5rem,6vw,2rem)}
  .panel{padding:16px}
  .code{font-size:1.6rem;letter-spacing:.2em;word-break:break-all}
  .row{flex-direction:column;align-items:stretch}
  .row input{min-width:0;width:100%}
  .row input[style]{max-width:none!important}
  .ask{flex-direction:column;align-items:stretch}
  .ask .q{min-width:0}
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .pol{flex-direction:column;align-items:flex-start}
}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Team</span>
  @@NAV@@
  <span class="spacer"></span></div>
<div class="wrap">
  <div id="peerErr" class="fetch-err" hidden role="alert" style="margin-bottom:18px;padding:10px 14px;border-radius:10px;background:rgba(154,63,63,.08);border:1px solid rgba(154,63,63,.25);color:var(--danger);font-size:13px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <span>Couldn't reach Sparrow — retrying…</span>
    <button type="button" id="peerRetry">Retry now</button>
  </div>
  <h1>Team</h1>
  <p class="lead">Pair with a teammate who also runs @@BRAND@@. Their assistant can then ask
  yours questions — answered from <b>your</b> memory, by <b>your</b> models, only when
  <b>you</b> allow it. Raw memory never leaves your machine; by default every question
  waits for your approval below.</p>
  <div class="note" id="tlsNote" hidden></div>

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

  <div class="panel" id="offersPanel" hidden>
    <h2>People from recent meetings</h2>
    <p class="muted">Attendees who aren't paired yet. Pairing still needs a code — this
    just names who to invite so work doesn't vanish after the call.</p>
    <div id="offersBox" class="muted"></div>
  </div>

  <div class="panel">
    <h2>Your team</h2>
    <p class="muted">What each teammate's assistant may ask without interrupting you.
    Apply a <b>pack</b> (teammate / manager / company / vendor), then tweak one topic if needed.
    <b>Ask me</b> = you approve each one (the default). <b>Answer</b> = share automatically.
    <b>Decline</b> = refuse automatically. Personal topics can never be shared automatically.
    Chat: <code>ask Name: …</code> or <code>ask #team: …</code>.</p>
    <div id="peersBox" class="muted">No teammates paired yet.</div>
  </div>

  <div class="panel">
    <h2>Named teams</h2>
    <p class="muted">Group paired peers so you can ask the whole squad. Each member
    still answers from their own memory, under their own policy.</p>
    <div class="row">
      <input id="teamName" placeholder="Team name, e.g. Platform">
      <button class="btn btn-ghost" id="teamCreateBtn" type="button">Create team</button>
    </div>
    <div id="teamsBox" class="muted">No named teams yet.</div>
  </div>

  <div class="panel">
    <h2>Open loops</h2>
    <p class="muted">Shared work from task handoffs — same id on both sides, evidence stays local.</p>
    <div id="loopsBox" class="muted">No shared loops yet.</div>
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
let _peerSig=null;

async function refresh(){
  if(document.hidden) return;
  const errEl=$('peerErr');
  try{
  if(!PEOPLE.length)await loadPeople();
  const r=await fetch('/peer/status');
  if(!r.ok) throw new Error('HTTP '+r.status);
  const s=await r.json();
  const sig=JSON.stringify(s);
  if(sig===_peerSig){ if(errEl) errEl.hidden=true; return; }
  _peerSig=sig;
  if(errEl) errEl.hidden=true;
  CLASSES=s.classes||[];ACTIONS=s.actions||[];
  PACKS=s.packs||[];TEAMS=s.teams||[];PEERS=s.peers||[];
  $('myUrl').textContent=s.base_url||'';
  const warn=(s.tls&&s.tls.warning)||'';
  $('tlsNote').hidden=!warn;if(warn)$('tlsNote').textContent=warn;
  renderAsks(s.pending_asks||[]);renderPeers(s.peers||[]);renderSent(s.sent||[]);
  renderTeams(s.teams||[],s.peers||[]);
  renderLoops(s.loops||[]);renderOffers(s.pairing_offers||[]);
  }catch(e){ if(errEl) errEl.hidden=false; }
}
$('peerRetry')?.addEventListener('click',()=>refresh());
let PACKS=[],TEAMS=[],PEERS=[];
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
let PEOPLE=[];
async function loadPeople(){
  try{
    const r=await fetch('/people/list?include_candidates=1').then(x=>x.json());
    PEOPLE=r.people||[];
  }catch(e){PEOPLE=[]}
}
function personOpts(selected){
  const opts=['<option value="">— not linked —</option>']
    .concat(PEOPLE.filter(p=>!p.is_self).map(p=>
      `<option value="${p.id}" ${String(selected||'')===String(p.id)?'selected':''}>${esc(p.name)}</option>`));
  opts.push('<option value="__create__">Create new person…</option>');
  return opts.join('');
}
function presenceDot(p){
  const st=p.presence||'unknown';
  const col=st==='online'?'var(--ok)':(st==='offline'?'#c78a2c':'var(--mut)');
  return `<span style="display:inline-block;width:.6em;height:.6em;border-radius:50%;background:${col};margin-right:.35em" title="${esc(st)}"></span>${esc(st)}`;
}
function packOpts(selected){
  const cur=selected||'custom';
  const opts=PACKS.map(pk=>`<option value="${esc(pk.id)}" ${cur===pk.id?'selected':''}>${esc(pk.id)}</option>`);
  opts.unshift(`<option value="custom" ${cur==='custom'?'selected':''}>custom</option>`);
  return opts.join('');
}
function renderPeers(peers){
  if(!peers.length){$('peersBox').textContent='No teammates paired yet.';return}
  $('peersBox').innerHTML='<table><tr><th>Teammate</th><th>Person in memory</th><th>Pack</th><th>Allowed without asking</th><th></th></tr>'+
    peers.map(p=>`<tr><td><b>${esc(p.name)}</b> <span class="muted" style="font-size:.8rem">${presenceDot(p)}</span>
      <div class="muted" style="font-family:var(--mono);font-size:.78rem">${esc(p.base_url)}${p.tls?' · tls':''}</div>
      ${p.person_name?`<div class="muted" style="font-size:.82rem">Linked: ${esc(p.person_name)}</div>`:
        `<div class="muted" style="font-size:.82rem">Not a person in memory yet — pick someone in the next column (or Create new person with their real name). Chat will keep treating this as a machine name until then.</div>`}</td>
    <td><select data-link="${p.peer_id}" onchange="linkPerson('${p.peer_id}',this)">${personOpts(p.person_id)}</select></td>
    <td><select onchange="applyPack('${p.peer_id}',this.value)">${packOpts(p.policy_pack)}</select></td>
    <td><div class="pol">${CLASSES.map(c=>`<label>${esc(TOPIC[c]||c)}
      <br><select data-peer="${p.peer_id}" data-cls="${c}" onchange="savePolicy('${p.peer_id}')"
        ${c==='personal'?'title="Personal topics can never be shared automatically"':''}>
      ${ACTIONS.map(a=>(c==='personal'&&a==='auto')?'':`<option value="${a}" ${((p.policy||{})[c]||'offer')===a?'selected':''}>${LABEL[a]}</option>`).join('')}
      </select></label>`).join('')}</div></td>
    <td><button class="linkish" onclick="revoke('${p.peer_id}')">unpair</button></td></tr>`).join('')+'</table>';
}
async function applyPack(pid,pack){
  if(!pack||pack==='custom')return;
  const r=await post('/peer/policy/pack',{peer_id:pid,pack});
  if(r.detail||r.error)alert(r.detail||r.error);
  refresh();
}
function renderTeams(teams,peers){
  if(!teams.length){$('teamsBox').textContent='No named teams yet.';return}
  $('teamsBox').innerHTML=teams.map(t=>{
    const members=new Set(t.peer_ids||[]);
    const checks=(peers||[]).map(p=>`<label style="margin-right:10px"><input type="checkbox" data-team="${esc(t.slug)}" value="${esc(p.peer_id)}" ${members.has(p.peer_id)?'checked':''} onchange="saveTeam('${esc(t.slug)}')"> ${esc(p.name)}</label>`).join('')
      || '<span class="muted">Pair someone first.</span>';
    return `<div class="ask"><div class="q"><b>#${esc(t.slug)}</b> ${esc(t.name)}
      <div class="muted" style="margin-top:6px">${checks}</div>
      <div class="muted" style="margin-top:4px">Chat: ask #${esc(t.slug)}: what's blocking us?</div></div>
      <button class="linkish" onclick="delTeam('${esc(t.slug)}')">remove</button></div>`;
  }).join('');
}
async function saveTeam(slug){
  const ids=[...document.querySelectorAll(`input[data-team="${slug}"]:checked`)].map(i=>i.value);
  await post('/peer/teams/members',{slug,peer_ids:ids});
  refresh();
}
async function delTeam(slug){
  if(!confirm('Remove this named team? Pairing is unchanged.'))return;
  await post('/peer/teams/delete',{slug});refresh();
}
$('teamCreateBtn').onclick=async()=>{
  const name=$('teamName').value.trim();if(!name)return;
  const r=await post('/peer/teams',{name});
  if(r.detail||r.error)alert(r.detail||r.error);
  $('teamName').value='';refresh();
};
function renderLoops(rows){
  if(!rows.length){$('loopsBox').textContent='No shared loops yet.';return}
  $('loopsBox').innerHTML='<table><tr><th>With</th><th>Task</th><th>Status</th></tr>'+
    rows.map(r=>`<tr><td>${esc(r.peer_name)}</td><td>${esc(r.task)}</td>
    <td>${esc(r.status)}${r.loop_id?` <span class="muted" style="font-family:var(--mono);font-size:.75rem">${esc(r.loop_id)}</span>`:''}</td></tr>`).join('')+'</table>';
}
function renderOffers(rows){
  if(!rows.length){$('offersPanel').hidden=true;return}
  $('offersPanel').hidden=false;
  $('offersBox').innerHTML='<ul style="margin:0;padding-left:1.2rem">'+rows.map(o=>
    `<li><b>${esc(o.name||o.email)}</b>${o.email&&o.name?` <span class="muted">${esc(o.email)}</span>`:''}
     — show a pairing code above and send it to them.</li>`).join('')+'</ul>';
}
async function linkPerson(pid,sel){
  const v=sel.value;
  if(!v){await post('/peer/unlink',{peer_id:pid});refresh();return}
  if(v==='__create__'){
    const name=prompt('Create person name (must look like a real name):');
    if(!name){refresh();return}
    const r=await post('/peer/link',{peer_id:pid,create_name:name.trim()});
    if(r.detail||r.error)alert(r.detail||r.error);
    await loadPeople();refresh();return;
  }
  const r=await post('/peer/link',{peer_id:pid,person_id:Number(v)});
  if(r.detail||r.error)alert(r.detail||r.error);
  refresh();
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
    rows.slice().reverse().map(r=>`<tr><td>${esc(r.peer_name)}${r.team_slug?` <span class="tag">#${esc(r.team_slug)}</span>`:''}</td>
    <td>${esc(r.question)}${r.loop_id?`<div class="muted" style="font-family:var(--mono);font-size:.75rem">loop ${esc(r.loop_id)}</div>`:''}</td>
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
refresh();setInterval(()=>{ if(!document.hidden) refresh(); },5000);
</script>
</body>
</html>""")
