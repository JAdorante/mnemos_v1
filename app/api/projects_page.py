"""Projects page (/projects): every project with its shared records and the
user's own filed notes, plus Miscellaneous. It only renders state; the rules
live in services/projects.py and the Org Record Service."""

from app.api.mnemos_theme import apply as _mnemos

PROJECTS_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Projects</title>
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
h2{font-family:var(--display);font-weight:400;font-size:1.35rem;margin:0 0 10px;color:var(--navy)}
h3{font-size:15px;margin:18px 0 6px}
.lead{color:var(--mut);margin:0 0 18px}
.mut{color:var(--mut);font-size:14px}
.layout{display:grid;grid-template-columns:280px minmax(0,1fr);gap:18px;align-items:start}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);padding:18px;box-shadow:var(--shadow)}
.plist{list-style:none;margin:0;padding:0}
.plist button{appearance:none;width:100%;display:flex;justify-content:space-between;gap:10px;align-items:center;
  text-align:left;font:inherit;font-size:14px;color:var(--text);background:none;border:0;border-radius:10px;padding:8px 10px;cursor:pointer}
.plist button:hover{background:var(--paper)}
.plist button[aria-current=true]{background:var(--paper);font-weight:600}
.group{font-size:12px;font-weight:600;color:var(--mut);margin:12px 0 4px;padding:0 10px}
.chip{font-size:12px;font-weight:600;padding:2px 8px;border-radius:999px;border:1px solid var(--line);color:var(--text)}
.chip.warn{color:var(--danger);border-color:var(--danger)}
.item{border:1px solid var(--line);border-radius:12px;padding:14px;margin:10px 0}
.item .meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;font-size:13px;color:var(--mut);margin-bottom:6px}
.item p{margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere}
.btn{appearance:none;border:1px solid var(--line);cursor:pointer;font:inherit;font-size:14px;font-weight:600;border-radius:12px;padding:6px 12px;background:transparent;color:var(--text)}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-top:8px}
.search{width:100%;font:inherit;font-size:14px;color:var(--text);background:var(--paper);border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-bottom:12px}
.empty{color:var(--mut);font-size:14px;padding:6px 0}
.note{padding:10px 14px;border-radius:10px;font-size:14px;margin:10px 0;background:rgba(180,130,40,.08);border:1px solid rgba(180,130,40,.3)}
@media (max-width:760px){.layout{grid-template-columns:minmax(0,1fr)}.wrap{padding:20px 16px 60px}}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Projects</span>
  @@NAV@@
  <span class="spacer"></span></div>
<div class="wrap">
  <h1>Projects</h1>
  <p class="lead">Notes from your assistants, filed by project. Shared records are visible to your team.
  Your own notes stay on your @@BRAND@@ until you share them.</p>
  <div class="layout">
    <div class="panel"><input class="search" id="q" type="search" placeholder="Search team records" aria-label="Search team records">
      <div id="list"><p class="empty">Loading…</p></div></div>
    <div class="panel" id="detail"><p class="empty">Choose a project.</p></div>
  </div>
</div>
<script>
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const when=t=>t?new Date(t*1000).toLocaleDateString():'—';
const CSRF=(document.cookie.split('; ').find(c=>c.startsWith('quill_csrf='))||'').split('=')[1]||'';
const post=u=>fetch(u,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':CSRF},body:'{}'});
let current='', miscName='Miscellaneous';

function button(name,count,extra){
  return `<li><button type="button" data-name="${esc(name)}" aria-current="${name===current}">
    <span>${esc(name)}</span><span class="mut">${extra||''}${count?esc(count):''}</span></button></li>`;
}
async function loadList(){
  const r=await (await fetch('/projects/list')).json();
  miscName=r.misc||miscName;
  const groups={shared:[],filed:[],known:[]};
  (r.projects||[]).forEach(p=>groups[p.source].push(p));
  const part=(title,rows)=>rows.length?`<div class="group">${title}</div><ul class="plist">${rows.map(p=>button(p.name,p.notes)).join('')}</ul>`:'';
  document.getElementById('list').innerHTML=part('Shared with your team',groups.shared)
    +part('Your notes',groups.filed)+part('Other projects Sparrow noticed',groups.known)
    +`<div class="group">Not about a project</div><ul class="plist">${button(r.misc,r.misc_notes)}</ul>`;
}
document.getElementById('list').addEventListener('click',e=>{
  const b=e.target.closest('button[data-name]'); if(!b) return;
  current=b.dataset.name; loadList(); loadDetail();
});

function recordHtml(r,withProject){
  const status=r.status==='suggested'?'<span class="chip warn">Suggested, not decided</span>':r.status==='decided'?'<span class="chip">Decided</span>':'';
  const facts=[r.decided_by&&('Decided by '+r.decided_by),r.owner&&('Owner: '+r.owner),r.due&&('Due '+r.due)].filter(Boolean).join(' · ');
  return `<div class="item" data-record="${esc(r.record_id)}">
    <div class="meta"><span class="chip">${esc(r.kind)}</span>${status}
      ${withProject&&r.project?`<span class="chip">${esc(r.project)}</span>`:''}
      <span>Approved by ${esc(r.approved_by||'unknown')}, ${esc(when(r.recorded_at))}</span>
      ${r.drafted_by?`<span>· Drafted with ${esc(r.drafted_by)}</span>`:''}
      ${r.version>1?`<span>· Version ${esc(r.version)}</span>`:''}</div>
    ${r.title?`<b>${esc(r.title)}</b>`:''}
    <p>${esc(r.text)}</p>
    ${r.rationale?`<p class="mut">Why: ${esc(r.rationale)}</p>`:''}
    ${facts?`<p class="mut">${esc(facts)}</p>`:''}
    <div class="row">${r.version>1?'<button class="btn" data-history type="button">Earlier versions</button>':''}
      <button class="btn" data-retract type="button">Remove from project</button></div>
    <div class="history"></div></div>`;
}
function noteHtml(n){
  return `<div class="item"><div class="meta"><span class="chip">${esc(n.kind)}</span>
      <span>${esc(n.assistant_label)}, ${esc(when(n.decided_at))}</span>
      ${n.share_state==='recorded'?'<span class="chip">Shared</span>':n.share_state==='forwarded'?'<span class="chip">Waiting for an approver</span>':''}</div>
    ${n.title?`<b>${esc(n.title)}</b>`:''}<p>${esc(n.text)}</p></div>`;
}
function teamNote(r){
  return r.team==='unavailable'?'<div class="note">The org service is not reachable, so team records are missing here.</div>':'';
}
async function loadDetail(){
  const box=document.getElementById('detail');
  const r=await (await fetch('/projects/detail?name='+encodeURIComponent(current))).json();
  const notes=(r.notes||[]).filter(n=>n.share_state!=='recorded');
  if(current===miscName){
    box.innerHTML=`<h2>${esc(miscName)}</h2><p class="mut">Notes that aren't about a project. They stay personal. To share one, remove it and save it under a project.</p>`
      +(notes.map(noteHtml).join('')||'<p class="empty">Nothing here yet.</p>');
    return;
  }
  box.innerHTML=`<h2>${esc(r.project||current)}</h2>${teamNote(r)}
    <h3>Shared with your team</h3>${(r.records||[]).map(x=>recordHtml(x)).join('')||'<p class="empty">Nothing shared yet. Share a note when you accept it on the <a href="/assistants">Assistants</a> page.</p>'}
    <h3>Your notes</h3>${notes.map(noteHtml).join('')||'<p class="empty">No private notes here.</p>'}`;
}
let timer;
document.getElementById('q').addEventListener('input',e=>{
  clearTimeout(timer); const q=e.target.value.trim();
  if(!q){ if(current) loadDetail(); return; }
  timer=setTimeout(async()=>{
    const r=await (await fetch('/projects/search?q='+encodeURIComponent(q))).json();
    document.getElementById('detail').innerHTML=`<h2>Team records</h2>${teamNote(r)}`
      +((r.records||[]).map(x=>recordHtml(x,true)).join('')||'<p class="empty">No shared records match.</p>');
  },250);
});
document.getElementById('detail').addEventListener('click',async e=>{
  const el=e.target.closest('[data-record]'); if(!el) return;
  const id=encodeURIComponent(el.dataset.record);
  if(e.target.closest('[data-retract]')){
    if(!confirm('Remove this record from the project for everyone? Its history is kept.')) return;
    const r=await post('/projects/records/'+id+'/retract');
    if(!r.ok){ alert('Could not remove it. You may not have approval rights on this project.'); return; }
    if(current) loadDetail(); else el.remove();
  }else if(e.target.closest('[data-history]')){
    const r=await (await fetch('/projects/records/'+id+'/history')).json();
    el.querySelector('.history').innerHTML=(r.versions||[]).filter(v=>v.superseded_at).map(v=>
      `<p class="mut">Version ${esc(v.version)}, ${esc(when(v.recorded_at))}: ${esc((v.value||{}).text||'')}</p>`).join('')||'<p class="mut">No earlier versions.</p>';
  }
});
loadList();
</script>
</body>
</html>
""")
