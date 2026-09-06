"""Org living brief — GET /org/{entity_id}.

Entity details + current/former people + open work mentioning the org.
Polls /graph/version like Today/Profile.
"""

from app.api.mnemos_theme import apply as _mnemos

ORG_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Org</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:15px/1.55 var(--font);color:var(--text);
  background:linear-gradient(180deg,#131318 0%,var(--paper) 40%,var(--workspace) 100%)}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 22px}
.wrap{max-width:1120px;margin:0 auto;padding:8px 24px 48px}
.mast h1{font-family:var(--display);font-weight:400;font-size:clamp(2rem,5vw,2.8rem);
  letter-spacing:-.03em;color:var(--navy);margin:14px 0 6px;line-height:1}
.lead{color:var(--mut);font-size:13.5px;margin:0 0 16px}
.chip{display:inline-block;font-size:.72rem;
  border:1px solid var(--line);border-radius:99px;padding:2px 9px;color:var(--mut);margin:0 6px 6px 0}
.section{margin:22px 0 0}
.section h2{font-family:var(--display);font-weight:400;font-size:1.35rem;color:var(--navy);margin:0 0 8px}
.panel{border:1px solid var(--line);border-radius:14px;background:var(--panel);
  padding:14px 16px;margin-top:8px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:baseline;padding:8px 0;border-top:1px solid var(--line)}
.row:first-child{border-top:0}
.row a{color:var(--navy);font-weight:600;text-decoration:none}
.row a:hover{text-decoration:underline}
.muted{color:var(--mut);font-size:.9rem}
.meta{color:var(--mut);font-size:.78rem;font-family:var(--mono)}
.fact{padding:8px 0;border-top:1px solid var(--line);font-size:.95rem}
.fact:first-child{border-top:0}
.empty{color:var(--mut);padding:8px 0}
.err{color:var(--danger);padding:20px 0}
.fetch-err{
  padding:10px 14px;border-radius:10px;
  background:rgba(154,63,63,.08);border:1px solid rgba(154,63,63,.25);
  color:var(--danger);font-size:13px;display:flex;gap:12px;align-items:center;flex-wrap:wrap;
}
.fetch-err button{font:inherit;padding:4px 12px;border-radius:8px;cursor:pointer;
  border:1px solid rgba(154,63,63,.35);background:var(--panel);color:var(--danger);}
.detail{display:grid;grid-template-columns:120px 1fr;gap:6px 12px;font-size:.92rem}
.detail dt{color:var(--mut);font:11px/1.2 var(--sans);}
@media(max-width:640px){
  .top{padding:10px 14px}
  .wrap{padding:8px 14px 40px}
  .detail{grid-template-columns:1fr;gap:4px}
  .detail dt{margin-top:8px}
  .row{flex-direction:column;align-items:flex-start;gap:4px}
}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Org</span>
  @@NAV@@
  <span class="spacer"></span></div>
<div id="orgErr" class="fetch-err" hidden role="alert" style="max-width:1120px;margin:12px auto 0;padding:0 24px">
  <span style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;padding:10px 14px;border-radius:10px;background:rgba(154,63,63,.08);border:1px solid rgba(154,63,63,.25);color:var(--danger);font-size:13px;width:100%">
    <span>Couldn't reach Sparrow — retrying…</span>
    <button type="button" id="orgRetry">Retry now</button>
  </span>
</div>
<div class="wrap" id="root"><div class="muted">Loading…</div></div>
<script>
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const eid=(()=>{const m=location.pathname.match(/\/org\/(\d+)/);return m?Number(m[1]):null})();
let graphVer=null;
let _orgSig=null;

async function load(force){
  if(document.hidden) return;
  const errEl=document.getElementById('orgErr');
  if(!eid){document.getElementById('root').innerHTML='<div class="err">Missing org id.</div>';return}
  try{
    const v=await fetch('/graph/version').then(r=>r.json());
    if(!force && graphVer!==null && v.version===graphVer) return;
    graphVer=v.version;
  }catch(e){}
  try{
    const resp=await fetch('/org/'+eid+'/data');
    if(!resp.ok) throw new Error('HTTP '+resp.status);
    const d=await resp.json();
    if(d.detail||d.error){
      document.getElementById('root').innerHTML='<div class="err">'+esc(d.detail||d.error)+'</div>';
      if(errEl) errEl.hidden=true;
      return;
    }
    const sig=JSON.stringify(d);
    if(!force && sig===_orgSig){ if(errEl) errEl.hidden=true; return; }
    _orgSig=sig;
    if(errEl) errEl.hidden=true;
    render(d);
  }catch(e){
    if(errEl) errEl.hidden=false;
  }
}
document.getElementById('orgRetry')?.addEventListener('click',()=>load(true));
function render(d){
  const e=d.entity||{};
  let h='<div class="mast"><h1>'+esc(e.name||'Org')+'</h1>';
  h+='<p class="lead">Living brief — people, facts, and open work tied to this org.</p>';
  h+='<span class="chip">'+esc(e.kind||'org')+'</span>';
  (e.aliases||[]).forEach(a=>{h+='<span class="chip">'+esc(a)+'</span>'});
  h+='</div>';

  const det=d.details||{};
  const keys=Object.keys(det).filter(k=>det[k]&&det[k].value);
  if(keys.length){
    h+='<div class="section"><h2>Details</h2><div class="panel"><dl class="detail">';
    keys.forEach(k=>{
      const x=det[k];
      h+='<dt>'+esc(k)+'</dt><dd>'+esc(x.value)
        +(x.source?' <span class="meta">('+esc(x.source)+')</span>':'')+'</dd>';
    });
    h+='</dl></div></div>';
  }

  const people=d.people||[];
  h+='<div class="section"><h2>People</h2><div class="panel">';
  if(!people.length) h+='<div class="empty">No affiliation beliefs yet.</div>';
  else people.forEach(p=>{
    h+='<div class="row"><div style="flex:1"><a href="/profile?tab=people&pid='+p.person_id+'">'
      +esc(p.label||p.name)+'</a>'
      +'<div class="meta">'+esc(p.predicate||'')+(p.former?' · former':' · current')
      +(p.confidence!=null?' · conf '+Number(p.confidence).toFixed(2):'')+'</div></div>';
    if(p.predicate_id){
      h+='<a class="meta" href="/kg/predicates/'+p.predicate_id+'/explain" target="_blank">explain</a>';
    }
    h+='</div>';
  });
  h+='</div></div>';

  const work=d.work||[];
  h+='<div class="section"><h2>Open work</h2><div class="panel">';
  if(!work.length) h+='<div class="empty">No open tasks or commitments mention this org.</div>';
  else work.forEach(w=>{
    h+='<div class="row"><div style="flex:1"><span class="chip">'+esc(w.kind)+'</span> '
      +esc(w.text)
      +'<div class="meta">'+(w.due?'due '+esc(w.due)+' · ':'')+'#'+w.fact_id+'</div></div></div>';
  });
  h+='</div></div>';

  const facts=d.facts||[];
  h+='<div class="section"><h2>What I know</h2><div class="panel">';
  if(!facts.length) h+='<div class="empty">No facts linked yet.</div>';
  else facts.forEach(f=>{
    h+='<div class="fact"><span class="chip">'+esc(f.kind||'fact')+'</span> '+esc(f.text)+'</div>';
  });
  h+='</div></div>';

  h+='<p class="muted" style="margin-top:18px"><a href="/profile?tab=entities">← Orgs on You</a>'
    +' · <a href="/memory?mode=constellation">Constellation</a></p>';
  document.getElementById('root').innerHTML=h;
  document.title=(e.name||'Org')+' — @@BRAND@@';
}
load(true);
let orgStreamOn=false;
let orgPollTimer=null;
function startOrgPoll(){
  if(orgPollTimer) clearInterval(orgPollTimer);
  orgPollTimer=setInterval(()=>load(false), orgStreamOn?30000:4000);
}
if(window.MnemosFieldStream){
  orgStreamOn=!!MnemosFieldStream.connect((d)=>{
    if(d.version!=null) graphVer=null;
    load(false);
  });
}
startOrgPoll();
</script>
</body>
</html>""")
