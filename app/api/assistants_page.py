"""Assistants page (/assistants): connect ChatGPT / Claude / Grok over
remote MCP, review what they saved, revoke connections. The page only
renders state and calls /assistants/* — the rules (inbox-first, scopes,
redaction) live in services/remote_mcp.py and services/assistant_inbox.py."""

from app.api.mnemos_theme import apply as _mnemos

ASSISTANTS_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Assistants</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 var(--font);color:var(--text);background:var(--paper)}
.top{position:sticky;top:0;display:flex;gap:14px;align-items:center;padding:10px 20px;z-index:var(--z-raised);background:var(--chrome-bg);backdrop-filter:blur(10px)}
.wrap{max-width:1120px;margin:0 auto;padding:26px 24px 80px}
h1{font-family:var(--display);font-weight:400;font-size:2rem;letter-spacing:-.02em;color:var(--navy);margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 22px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px;box-shadow:var(--shadow);margin-bottom:18px}
.panel h2{font-family:var(--display);font-weight:400;font-size:1.35rem;margin:0 0 10px;color:var(--navy)}
.btn{appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;border-radius:12px;
  padding:9px 16px;background:var(--acc);color:var(--acc-fg)}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn:disabled{opacity:.5;cursor:default}
.mut{color:var(--mut);font-size:14px}
.url{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.url code{font-family:var(--mono);font-size:14px;padding:8px 12px;border-radius:10px;
  background:var(--paper);border:1px solid var(--line);word-break:break-all}
.note{padding:10px 14px;border-radius:10px;font-size:14px;margin:10px 0;
  background:rgba(180,130,40,.08);border:1px solid rgba(180,130,40,.3)}
.steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin-top:14px}
.steps div{border:1px solid var(--line);border-radius:12px;padding:14px;font-size:14px}
.steps b{display:block;margin-bottom:6px}
.item{border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
.item .meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--mut);margin-bottom:8px}
.chip{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--text)}
.chip.warn{color:var(--danger);border-color:var(--danger)}
.item textarea,.item input{width:100%;font:inherit;font-size:14px;color:var(--text);background:var(--paper);
  border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.item textarea{min-height:84px;resize:vertical}
.item .row{display:flex;gap:10px;margin-top:10px;flex-wrap:wrap;align-items:center}
.item .row input[type=text]{flex:1;min-width:160px}
.item input[type=checkbox],.item input[type=radio]{width:auto;flex:none;margin-top:4px}
.item select{font:inherit;font-size:14px;color:var(--text);background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:8px 10px}
.convo{font-size:13px;font-weight:600;color:var(--mut);margin:16px 0 4px}
.replaces{margin-top:8px;font-size:14px}
.replaces label{display:flex;gap:8px;align-items:flex-start;margin:4px 0}
.err{color:var(--danger);font-size:14px;margin-top:6px}
table{width:100%;border-collapse:collapse;font-size:14px}
td,th{text-align:left;padding:8px 6px;border-bottom:1px solid var(--line)}
th{color:var(--mut);font-weight:600}
.empty{color:var(--mut);font-size:14px;padding:6px 0}
.tokrow{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.tokrow input[type=text]{font:inherit;font-size:14px;padding:8px 10px;border-radius:10px;
  border:1px solid var(--line);background:var(--paper);color:var(--text);min-width:200px}
.tbl{overflow-x:auto}
@media (max-width:640px){.wrap{padding:20px 16px 60px}.tbl .col-when{display:none}}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Assistants</span>
  @@NAV@@
  <span class="spacer"></span></div>
<div class="wrap">
  <h1>Assistants</h1>
  <p class="lead">Connect ChatGPT, Claude, and Grok so what you work out with them comes back to
  @@BRAND@@, filed under your <a href="/projects">projects</a>. Every saved note waits here for your
  review. Nothing they send becomes memory until you accept it, and nothing they send can make
  @@BRAND@@ act.</p>

  <div class="panel" id="connectPanel">
    <h2>Connect an assistant</h2>
    <div id="offNote" class="note" hidden></div>
    <div id="urlBox" hidden>
      <p class="mut" style="margin:0 0 8px">Add this as a custom connector in each assistant:</p>
      <div class="url"><code id="mcpUrl"></code>
        <button class="btn btn-ghost" id="copyBtn" type="button">Copy</button></div>
      <div id="urlNote" class="note" hidden></div>
      <div class="steps">
        <div><b>ChatGPT</b>Settings → Apps &amp; Connectors → Advanced → turn on Developer mode,
          then Create. Paste the URL, choose OAuth, and approve on the @@BRAND@@ screen.</div>
        <div><b>Claude</b>Settings → Connectors → Add custom connector. Paste the URL and
          approve on the @@BRAND@@ screen.</div>
        <div><b>Grok</b>grok.com/connectors → New Connector → Custom. Paste the URL and approve
          on the @@BRAND@@ screen. For the xAI API, use an access token below instead.</div>
      </div>
      <p class="mut" style="margin-top:12px">Then just ask, e.g. <i>"save the decision we just made to
      Sparrow"</i>. It goes under the project it fits, a new project if it clearly is one, or
      Miscellaneous. You can change that when you review it.</p>
    </div>
  </div>

  <div class="panel" id="inboxPanel" hidden>
    <h2>Review inbox <span class="mut" id="inboxCount"></span></h2>
    <datalist id="projectNames"></datalist>
    <div id="inbox"></div>
    <h2 style="margin-top:20px">Recently accepted</h2>
    <div id="accepted"></div>
  </div>

  <div class="panel" id="connPanel" hidden>
    <h2>Connected</h2>
    <div id="conns"></div>
  </div>

  <div class="panel" id="tokPanel" hidden>
    <h2>Access tokens</h2>
    <p class="mut" style="margin-top:0">For clients that take a header instead of signing in, such as
    the xAI API's remote MCP tools or Claude Code (<code>Authorization: Bearer …</code>).</p>
    <div class="tokrow">
      <input type="text" id="tokLabel" placeholder="Label, e.g. Grok API" maxlength="60">
      <label class="mut"><input type="checkbox" id="tokCapture" checked> save</label>
      <label class="mut"><input type="checkbox" id="tokRead" checked> read</label>
      <label class="mut"><input type="checkbox" id="tokTeam"> team records</label>
      <button class="btn" id="tokBtn" type="button">Create token</button>
    </div>
    <div id="tokOut" class="note" hidden></div>
  </div>
</div>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const CSRF=(document.cookie.split('; ').find(c=>c.startsWith('quill_csrf='))||'').split('=')[1]||'';
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},body:JSON.stringify(b||{})}).then(r=>r.json());
let canShare=false, projects=[], miscName='Miscellaneous', inboxKey=null;
const when=t=>t?new Date(t*1000).toLocaleString():'—';

async function load(){
  let st;
  try{ st=await (await fetch('/assistants/status')).json(); }catch(e){ return; }
  const off=document.getElementById('offNote');
  if(st.unavailable){
    off.hidden=false;
    off.textContent='Remote connections are off: '+st.unavailable+'.';
    return;
  }
  off.hidden=true;
  document.getElementById('urlBox').hidden=false;
  document.getElementById('mcpUrl').textContent=st.mcp_url;
  const un=document.getElementById('urlNote');
  const warn=[];
  if(!st.https) warn.push('This address is not HTTPS, so cloud assistants cannot reach it. Open this page through your public URL.');
  if(st.ephemeral && !st.pinned) warn.push('This is a quick-tunnel address that changes when the tunnel restarts, which breaks connected assistants. Use a named tunnel and set QUILL_MCP_PUBLIC_URL.');
  un.hidden=!warn.length; un.textContent=warn.join(' ');
  ['inboxPanel','connPanel','tokPanel'].forEach(id=>document.getElementById(id).hidden=false);
  renderConns(st.connections||[]);
  canShare=!!st.can_share; projects=st.projects||[]; miscName=st.misc||miscName;
  document.getElementById('projectNames').innerHTML=[...projects.map(p=>p.name),miscName].map(n=>`<option value="${esc(n)}">`).join('');
  await loadInbox(); await loadAccepted();
}

function isMisc(name){ return !name.trim() || name.trim().toLowerCase()===miscName.toLowerCase(); }
function routeLabel(name){
  if(isMisc(name)) return 'Not about a project';
  return projects.some(p=>p.name.toLowerCase()===name.trim().toLowerCase())?'Existing project':'New project';
}
function itemHtml(it){
  const d=it.details||{};
  const person=d.decided_by||'';
  return `
    <div class="item" data-id="${it.id}">
      <div class="meta"><span class="chip">${esc(it.assistant_label)}</span>
        <span class="chip">${esc(it.kind)}</span>
        ${(it.flags||[]).map(f=>`<span class="chip warn" title="Looks like it contains a secret">${esc(f)}</span>`).join('')}
        <span>${esc(when(it.created_at))}</span>
        ${it.source_url?`<a href="${esc(it.source_url)}" target="_blank" rel="noopener noreferrer">source</a>`:''}</div>
      ${it.title?`<b>${esc(it.title)}</b>`:''}
      <textarea aria-label="Note text">${esc(it.text)}</textarea>
      ${d.rationale?`<p class="mut">Why: ${esc(d.rationale)}</p>`:''}
      ${d.owner||d.due?`<p class="mut">${esc([d.owner&&('Owner: '+d.owner),d.due&&('Due '+d.due)].filter(Boolean).join(' · '))}</p>`:''}
      ${it.kind==='decision'?`<div class="row">
        <select data-status aria-label="Was this decided?">
          <option value="suggested"${person?'':' selected'}>The assistant only suggested this</option>
          <option value="decided"${person?' selected':''}>A person decided this</option></select>
        <input type="text" data-decider placeholder="Who decided?" aria-label="Who decided" value="${esc(person)}"${person?'':' hidden'}></div>`:''}
      <div class="row"><input type="text" data-project list="projectNames" placeholder="Project" aria-label="Project" value="${esc(it.project)}">
        <span class="mut" data-route title="${esc(it.route_reason||'')}">${it.project?esc(routeLabel(it.project)):'Sorting…'}</span></div>
      ${canShare?`<div class="row" data-share-row${it.project&&isMisc(it.project)?' hidden':''}><label class="mut"><input type="checkbox" data-share> Share with this project's team</label></div>
      <div class="replaces" data-replaces hidden></div>`:''}
      <div class="row"><button class="btn" data-act="accept" type="button">Accept</button>
        <button class="btn btn-ghost" data-act="reject" type="button">Reject</button></div>
    </div>`;
}
async function loadInbox(){
  const r=await (await fetch('/assistants/inbox?status=pending')).json();
  const items=r.items||[];
  document.getElementById('inboxCount').textContent=items.length?'· '+items.length+' waiting':'';
  const box=document.getElementById('inbox');
  // The refresh must not wipe what the reviewer is typing or ticking.
  const key=items.map(i=>[i.id,i.project,i.route_action].join(':')).join('|')+'#'+projects.length;
  if(key===inboxKey) return;
  const busy=box.contains(document.activeElement) || box.querySelector('[data-share]:checked');
  if(busy && items.every(i=>box.querySelector(`.item[data-id="${i.id}"]`))) return;
  inboxKey=key;
  if(!items.length){ box.innerHTML='<p class="empty">Nothing waiting. Saved notes from your assistants will appear here.</p>'; return; }
  const groups=new Map();
  items.forEach(it=>{ const k=it.conversation||''; groups.has(k)||groups.set(k,[]); groups.get(k).push(it); });
  box.innerHTML=[...groups].map(([name,rows])=>(name?`<div class="convo">From “${esc(name)}” · ${rows.length} ${rows.length===1?'note':'notes'}</div>`:'')+rows.map(itemHtml).join('')).join('');
}

function shareChip(it){
  const st=it.share_state;
  if(st==='recorded') return '<span class="chip">Shared</span>';
  if(st==='queued') return '<span class="chip">Sharing when the org is reachable</span>';
  if(st==='forwarded') return '<span class="chip">Waiting for an approver</span>';
  if(st==='failed') return `<span class="chip warn" title="${esc(it.share_error||'')}">Not shared</span>`;
  return '';
}
function canShareItem(it){
  return canShare && it.project!==miscName && !['recorded','queued','forwarded'].includes(it.share_state);
}
async function loadAccepted(){
  const r=await (await fetch('/assistants/inbox?status=accepted')).json();
  const items=(r.items||[]).slice(0,20);
  const box=document.getElementById('accepted');
  if(!items.length){ box.innerHTML='<p class="empty">Nothing accepted yet.</p>'; return; }
  box.innerHTML=items.map(it=>`
    <div class="item" data-id="${it.id}">
      <div class="meta"><span class="chip">${esc(it.assistant_label)}</span>
        <span class="chip">${esc(it.kind)}</span>
        ${it.project?`<span class="chip">${esc(it.project)}</span>`:''}
        ${shareChip(it)}
        <span>${esc(when(it.decided_at))}</span></div>
      <p>${esc(it.text)}</p>
      ${it.share_state==='failed'&&it.share_error?`<p class="err">${esc(it.share_error)}</p>`:''}
      <div class="row">${canShareItem(it)?'<button class="btn btn-ghost" data-share-now type="button">Share with this project\'s team</button>':''}
        <button class="btn btn-ghost" data-remove type="button">Remove from memory</button></div>
    </div>`).join('');
}

document.getElementById('accepted').addEventListener('click',async e=>{
  const sh=e.target.closest('button[data-share-now]');
  if(sh){
    const el=sh.closest('.item'); sh.disabled=true;
    const r=await post('/assistants/inbox/'+el.dataset.id+'/share');
    if(!r.ok) alert(r.error||'Could not share this note.');
    loadAccepted(); return;
  }
  const b=e.target.closest('button[data-remove]'); if(!b) return;
  if(!confirm('Remove this note and everything Sparrow learned from it? If you shared it, it also comes off the project.')) return;
  const el=b.closest('.item'); b.disabled=true;
  const r=await post('/assistants/inbox/'+el.dataset.id+'/remove');
  if(!r.ok){ alert(r.error||'Failed'); b.disabled=false; return; }
  if(r.warning) alert(r.warning);
  loadAccepted();
});

async function showReplaces(el){
  const box=el.querySelector('[data-replaces]'); if(!box) return;
  const on=el.querySelector('[data-share]').checked;
  box.hidden=!on; box.innerHTML='';
  if(!on) return;
  const project=el.querySelector('[data-project]').value;
  const r=await (await fetch('/assistants/inbox/'+el.dataset.id+'/similar?project='+encodeURIComponent(project))).json();
  const recs=r.records||[];
  if(!recs.length) return;
  box.innerHTML='<span class="mut">Does this replace something already shared?</span>'
    +`<label><input type="radio" name="rep${el.dataset.id}" value="" checked><span>No, add it as a new record</span></label>`
    +recs.map(x=>`<label><input type="radio" name="rep${el.dataset.id}" value="${esc(x.record_id)}"><span>Replace: ${esc(x.title||x.text.slice(0,140))}</span></label>`).join('');
}
document.getElementById('inbox').addEventListener('change',e=>{
  const el=e.target.closest('.item'); if(!el) return;
  if(e.target.matches('[data-project]')){
    const out=el.querySelector('[data-route]');
    out.textContent=routeLabel(e.target.value); out.title='';
    const row=el.querySelector('[data-share-row]');
    if(row){
      row.hidden=isMisc(e.target.value);
      if(row.hidden) row.querySelector('[data-share]').checked=false;
    }
  }
  if(e.target.matches('[data-share],[data-project]')) showReplaces(el);
  if(e.target.matches('[data-status]')){
    const who=el.querySelector('[data-decider]');
    who.hidden=e.target.value!=='decided';
  }
});
document.getElementById('inbox').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-act]'); if(!b) return;
  const el=b.closest('.item'); const id=el.dataset.id;
  el.querySelectorAll('button').forEach(x=>x.disabled=true);
  let r;
  if(b.dataset.act==='accept'){
    const status=el.querySelector('[data-status]');
    const share=el.querySelector('[data-share]');
    const rep=el.querySelector('[data-replaces] input:checked');
    r=await post('/assistants/inbox/'+id+'/accept',{text:el.querySelector('textarea').value,
      project:el.querySelector('[data-project]').value,
      share:!!(share&&share.checked), replaces:rep&&rep.value?rep.value:null,
      decision_status:status?status.value:null,
      decided_by:status&&status.value==='decided'?el.querySelector('[data-decider]').value:null});
  }else{
    r=await post('/assistants/inbox/'+id+'/reject');
  }
  if(!r.ok){ alert(r.error||'Failed'); el.querySelectorAll('button').forEach(x=>x.disabled=false); return; }
  if(r.shared&&!r.shared.ok) alert('Saved to your notes, but not shared: '+(r.shared.error||'unknown error'));
  if(r.project&&!isMisc(r.project)&&!projects.some(p=>p.name===r.project)){
    projects.push({name:r.project,shared:false});
    document.getElementById('projectNames').insertAdjacentHTML('beforeend',`<option value="${esc(r.project)}">`);
  }
  el.remove(); inboxKey=null;
  loadInbox(); loadAccepted();
});

function renderConns(rows){
  const box=document.getElementById('conns');
  if(!rows.length){ box.innerHTML='<p class="empty">No assistants connected yet.</p>'; return; }
  box.innerHTML='<div class="tbl"><table><thead><tr><th>Assistant</th><th>Name</th><th>Allowed</th><th class="col-when">Connected</th><th>Last used</th><th></th></tr></thead><tbody>'
    +rows.map(c=>`<tr><td>${esc(c.assistant_label)}</td><td>${esc(c.label||'—')}${c.kind==='static'?' <span class="mut">(token)</span>':''}</td>
      <td>${esc((c.scope||'').replace('capture','save'))}</td><td class="col-when">${esc(when(c.created_at))}</td><td>${esc(when(c.last_used_at))}</td>
      <td><button class="btn btn-ghost" data-revoke="${esc(c.id)}" type="button">Disconnect</button></td></tr>`).join('')
    +'</tbody></table></div>';
}
document.getElementById('conns').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-revoke]'); if(!b) return;
  if(!confirm('Disconnect this assistant? It will need to sign in again.')) return;
  await post('/assistants/connections/'+encodeURIComponent(b.dataset.revoke)+'/revoke');
  load();
});

document.getElementById('copyBtn').addEventListener('click',()=>{
  const t=document.getElementById('mcpUrl').textContent;
  navigator.clipboard&&navigator.clipboard.writeText(t);
});

document.getElementById('tokBtn').addEventListener('click',async ()=>{
  const scope=[document.getElementById('tokCapture').checked?'capture':'',document.getElementById('tokRead').checked?'read':'',document.getElementById('tokTeam').checked?'team':''].join(' ').trim();
  if(!scope){ alert('Choose at least one permission.'); return; }
  const r=await post('/assistants/tokens',{label:document.getElementById('tokLabel').value,scope});
  const out=document.getElementById('tokOut'); out.hidden=false;
  out.innerHTML=r.ok?'Copy it now. It will not be shown again:<br><code style="word-break:break-all">'+esc(r.token)+'</code>':esc(r.error||'Failed');
  load();
});

load();
setInterval(()=>{ if(!document.hidden && !document.getElementById('inboxPanel').hidden) loadInbox().catch(()=>{}); },15000);
</script>
</body>
</html>
""")
