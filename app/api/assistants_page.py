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
.item .row input{flex:1;min-width:160px}
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
  @@BRAND@@ for your projects. Every saved note waits here for your review. Nothing they send
  becomes memory until you accept it, and nothing they send can make @@BRAND@@ act.</p>

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
      Sparrow under the onboarding project"</i>.</p>
    </div>
  </div>

  <div class="panel" id="inboxPanel" hidden>
    <h2>Review inbox <span class="mut" id="inboxCount"></span></h2>
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
      <button class="btn" id="tokBtn" type="button">Create token</button>
    </div>
    <div id="tokOut" class="note" hidden></div>
  </div>
</div>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const post=(u,b)=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b||{})}).then(r=>r.json());
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
  await loadInbox(); await loadAccepted();
}

async function loadInbox(){
  const r=await (await fetch('/assistants/inbox?status=pending')).json();
  const items=r.items||[];
  document.getElementById('inboxCount').textContent=items.length?'· '+items.length+' waiting':'';
  const box=document.getElementById('inbox');
  if(!items.length){ box.innerHTML='<p class="empty">Nothing waiting. Saved notes from your assistants will appear here.</p>'; return; }
  box.innerHTML=items.map(it=>`
    <div class="item" data-id="${it.id}">
      <div class="meta"><span class="chip">${esc(it.assistant_label)}</span>
        <span class="chip">${esc(it.kind)}</span>
        ${(it.flags||[]).map(f=>`<span class="chip warn" title="Looks like it contains a secret">${esc(f)}</span>`).join('')}
        <span>${esc(when(it.created_at))}</span>
        ${it.conversation?`<span>· ${esc(it.conversation)}</span>`:''}
        ${it.source_url?`<a href="${esc(it.source_url)}" target="_blank" rel="noopener noreferrer">source</a>`:''}</div>
      ${it.title?`<b>${esc(it.title)}</b>`:''}
      <textarea aria-label="Note text">${esc(it.text)}</textarea>
      <div class="row"><input type="text" placeholder="Project" aria-label="Project" value="${esc(it.project)}">
        <button class="btn" data-act="accept" type="button">Accept</button>
        <button class="btn btn-ghost" data-act="reject" type="button">Reject</button></div>
    </div>`).join('');
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
        <span>${esc(when(it.decided_at))}</span></div>
      <p>${esc(it.text)}</p>
      <div class="row"><button class="btn btn-ghost" data-remove type="button">Remove from memory</button></div>
    </div>`).join('');
}

document.getElementById('accepted').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-remove]'); if(!b) return;
  if(!confirm('Remove this note and everything Sparrow learned from it?')) return;
  const el=b.closest('.item'); b.disabled=true;
  const r=await post('/assistants/inbox/'+el.dataset.id+'/remove');
  if(!r.ok){ alert(r.error||'Failed'); b.disabled=false; return; }
  loadAccepted();
});

document.getElementById('inbox').addEventListener('click',async e=>{
  const b=e.target.closest('button[data-act]'); if(!b) return;
  const el=b.closest('.item'); const id=el.dataset.id;
  el.querySelectorAll('button').forEach(x=>x.disabled=true);
  const r=b.dataset.act==='accept'
    ? await post('/assistants/inbox/'+id+'/accept',{text:el.querySelector('textarea').value,project:el.querySelector('input').value})
    : await post('/assistants/inbox/'+id+'/reject');
  if(!r.ok){ alert(r.error||'Failed'); el.querySelectorAll('button').forEach(x=>x.disabled=false); return; }
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
  const scope=[document.getElementById('tokCapture').checked?'capture':'',document.getElementById('tokRead').checked?'read':''].join(' ').trim();
  if(!scope){ alert('Choose save, read, or both.'); return; }
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
