"""You — the living user profile page.

Everything the system currently believes about its user, as reviewable cards:
the identity core (from onboarding), the self-facts accrued from speech/chat
("what you've told me"), and the open work owned by the user. Every card
carries the same verdicts as the Memory Console — Confirm / Edit / Forget —
because the trust story of a profile is being able to SEE and CORRECT it.

Live: polls /graph/version and re-renders when memory changes, so saying
"I'm switching the deck to Figma" in chat updates this page within seconds.
"""

from app.api.mnemos_theme import apply as _mnemos

PROFILE_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — You</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:15px/1.55 var(--font);color:var(--text);
  background:linear-gradient(180deg,#FBF9F4 0%,var(--paper) 40%,var(--workspace) 100%)}
.top{display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:12px 22px}
.wrap{max-width:860px;margin:0 auto;padding:8px 22px 48px}
.mast h1{font-family:var(--display);font-weight:400;font-size:clamp(2rem,5vw,2.8rem);
  letter-spacing:-.03em;color:var(--navy);margin:14px 0 6px;line-height:1}
.lead{color:var(--mut);font-size:13.5px;margin:0 0 6px}
.section{margin:26px 0 0}
.section h2{font-family:var(--display);font-weight:400;font-size:1.35rem;color:var(--navy);margin:0 0 4px}
.idcard{border:1px solid var(--line);border-radius:14px;background:var(--panel);
  padding:16px 18px;margin-top:10px}
.idcard .nm{font-family:var(--display);font-size:1.5rem;color:var(--navy)}
.idcard .rl{color:var(--mut);margin-top:2px}
.idcard .ds{margin-top:8px;font-size:14px}
.idcard .contact{margin-top:12px;display:flex;flex-direction:column;gap:5px;
  font:13px/1.45 var(--font);color:var(--text)}
.idcard .contact div{display:flex;flex-wrap:wrap;gap:6px 10px;align-items:baseline}
.idcard .contact span{color:var(--mut);font:11px/1.2 var(--mono);
  letter-spacing:.04em;text-transform:uppercase;min-width:7.5rem}
.idcard a{font-size:12.5px}
.card{display:flex;gap:12px;align-items:flex-start;border:1px solid var(--line);
  border-radius:12px;background:var(--panel);padding:12px 14px;margin-top:8px}
.card .t{flex:1;min-width:0}
.card .meta{color:var(--mut);font-size:12px;margin-top:3px}
.card .pill{font:11px var(--mono);border:1px solid var(--line);border-radius:999px;
  padding:2px 8px;color:var(--mut);white-space:nowrap}
.acts{display:flex;gap:6px;flex-shrink:0}
.acts button{border:1px solid var(--line);border-radius:9px;background:var(--panel);
  font:12px var(--font);color:var(--navy);cursor:pointer;padding:5px 9px}
.acts button:hover{background:var(--panel-2)}
.acts .warn:hover{color:var(--danger,#8a2d2d)}
.empty{color:var(--mut);font-size:13.5px;border:1px dashed var(--line);
  border-radius:12px;padding:14px;margin-top:8px}
.confirmed{color:var(--teal,#1E5B4F)}
.tabs{display:flex;gap:6px;margin:14px 0 0}
.tabs button{border:1px solid var(--line);border-radius:999px;background:var(--panel);
  font:600 13px var(--font);color:var(--mut);cursor:pointer;padding:7px 16px}
.tabs button.on{color:var(--navy);background:rgba(11,19,32,.06)}
.psearch{width:100%;margin-top:12px;padding:9px 12px;border:1px solid var(--line);
  border-radius:10px;background:var(--panel);font:14px var(--font);color:var(--text)}
.prow{display:flex;align-items:center;gap:10px;border:1px solid var(--line);
  border-radius:12px;background:var(--panel);padding:10px 14px;margin-top:8px;cursor:pointer}
.prow:hover{background:var(--panel-2,rgba(11,19,32,.03))}
.prow .nm{font-weight:600;color:var(--navy)}
.prow .meta{color:var(--mut);font-size:12px}
.pdetail{border:1px solid var(--line);border-left:3px solid var(--teal,#1E5B4F);
  border-radius:12px;background:var(--panel);padding:14px 16px;margin:6px 0 4px}
.pdetail h3{font-family:var(--display);font-weight:400;font-size:1.2rem;color:var(--navy);margin:0}
.chip{display:inline-block;font:12px var(--mono);border:1px solid var(--line);
  border-radius:999px;padding:2px 9px;color:var(--mut);margin:3px 4px 0 0}
.pfield{display:flex;gap:6px;margin-top:10px}
.pfield input,.pfield textarea{flex:1;padding:7px 10px;border:1px solid var(--line);
  border-radius:9px;background:var(--panel);font:13.5px var(--font);color:var(--text)}
.pfield button{border:1px solid var(--line);border-radius:9px;background:var(--panel);
  font:12.5px var(--font);color:var(--navy);cursor:pointer;padding:6px 11px;white-space:nowrap}
.pfield button:hover{background:var(--panel-2)}
.plabel{font:11px var(--mono);letter-spacing:.05em;color:var(--mut);
  text-transform:uppercase;margin-top:14px}
.forget-person{margin-top:14px;border:1px solid var(--line);border-radius:9px;
  background:var(--panel);font:12.5px var(--font);color:var(--danger,#8a2d2d);
  cursor:pointer;padding:6px 11px}
.work-bar{display:none;position:sticky;bottom:12px;z-index:5;margin-top:14px;
  padding:10px 12px;border:1px solid var(--line);border-radius:12px;
  background:var(--panel);box-shadow:0 -4px 24px rgba(11,19,32,.06);
  align-items:center;gap:8px;flex-wrap:wrap}
.work-bar.on{display:flex}
.work-bar .count{font:12.5px var(--mono);color:var(--mut);margin-right:4px}
.work-bar button.warn{color:var(--danger,#8a2d2d)}
.card .wcheck{accent-color:var(--navy,#0B1320);margin-right:2px;cursor:pointer}
.work-toolbar{display:flex;align-items:center;gap:10px;margin-top:8px;flex-wrap:wrap}
.work-toolbar label{font:12.5px var(--font);color:var(--mut);cursor:pointer;
  display:inline-flex;align-items:center;gap:6px}
</style>
</head>
<body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">You</span>
  @@NAV@@
  <span class="spacer"></span>
</header>

<div class="wrap">
  <header class="mast">
    <h1>You</h1>
    <div class="ink-rule" style="margin:10px 0 12px"></div>
    <p class="lead">Everything @@BRAND@@ currently believes about you — confirm it,
    correct it, or make it forget. It updates itself as you speak and type.</p>
    <div class="tabs">
      <button type="button" id="tabBtnProfile" class="on">Profile</button>
      <button type="button" id="tabBtnPeople">People</button>
      <button type="button" id="tabBtnEntities">Orgs &amp; tools</button>
      <button type="button" id="tabBtnWork">Tasks</button>
    </div>
  </header>

  <div id="tabProfile">
    <section class="section" id="secId">
      <h2>Identity</h2>
      <div id="idCard" class="idcard"><div class="lead">Loading…</div></div>
    </section>

    <section class="section">
      <h2>What you've told me</h2>
      <p class="lead">Preferences, context, and self-facts gathered from your speech and chat.</p>
      <div id="aboutList"><div class="empty">Nothing yet.</div></div>
    </section>

    <section class="section">
      <h2>Your open work</h2>
      <p class="lead">Tasks and promises that belong to you.</p>
      <div id="workList"><div class="empty">Nothing open.</div></div>
    </section>
  </div>

  <div id="tabPeople" hidden>
    <section class="section">
      <h2>People you know</h2>
      <p class="lead">Your network — people you've named or that have enough
      evidence to matter. Click a person to correct their name, add aliases and
      notes, review their facts — or remove noise the ears misheard.</p>
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
        <input class="psearch" id="pSearch" placeholder="filter by name…" style="flex:1;min-width:180px;margin:0">
        <label style="font-size:12.5px;color:var(--mut);display:flex;gap:6px;align-items:center;white-space:nowrap">
          <input type="checkbox" id="pShowCandidates"> Show candidates
        </label>
      </div>
      <div id="exhaustSeed" class="empty" hidden style="margin-top:8px"></div>
      <div id="peopleList"><div class="empty">Loading…</div></div>
    </section>
  </div>

  <div id="tabWork" hidden>
    <section class="section">
      <h2>Tasks &amp; commitments</h2>
      <p class="lead">The full working board — add your own, or manage what the ears
      and chat picked up. A commitment is a promise to someone; a task is just work.</p>
      <div class="pdetail" style="border-left-color:var(--acc,#B87333)">
        <div class="plabel">Add one</div>
        <div class="pfield">
          <select id="wKind" style="border:1px solid var(--line);border-radius:9px;background:var(--panel);font:13.5px var(--font);color:var(--text);padding:7px 10px">
            <option value="task">task</option>
            <option value="commitment">commitment</option>
          </select>
          <input id="wText" placeholder="what needs doing…" style="flex:2">
          <input id="wDue" placeholder="due (optional)" style="max-width:130px">
          <input id="wOwner" placeholder="who (optional)" style="max-width:130px">
          <button data-wact="add">Add</button>
        </div>
      </div>
      <div class="plabel">Open</div>
      <div class="work-toolbar">
        <label><input type="checkbox" id="wSelectOpen"> Select all open</label>
        <label><input type="checkbox" id="wSelectClosed"> Select all closed</label>
      </div>
      <div id="workOpen"><div class="empty">Loading…</div></div>
      <div class="plabel">Recently closed</div>
      <div id="workClosed"><div class="empty">Nothing closed lately.</div></div>
      <div class="work-bar" id="workBar">
        <span class="count" id="workSelCount">0 selected</span>
        <button type="button" data-wbulk="done">Mark done</button>
        <button type="button" data-wbulk="reopen">Reopen</button>
        <button type="button" data-wbulk="due">Set due…</button>
        <button type="button" data-wbulk="edit">Rewrite all…</button>
        <button type="button" class="warn" data-wbulk="dismiss">Delete…</button>
        <button type="button" data-wbulk="clear">Clear</button>
      </div>
    </section>
  </div>

  <div id="tabEntities" hidden>
    <section class="section">
      <h2>Orgs, projects, tools &amp; places</h2>
      <p class="lead">Everything else in your world. Click one to fix its name or
      category, add aliases and notes, review its facts — or remove noise.</p>
      <input class="psearch" id="eSearch" placeholder="filter by name…">
      <div id="entityList"><div class="empty">Loading…</div></div>
    </section>
  </div>
</div>

@@UI_JS@@
<script>
MnemosMemory.set('lastRoute', '/profile');

function fmtWhen(ts){
  if(!ts) return '';
  const d=new Date(ts*1000);
  return d.toLocaleDateString(undefined,{month:'short',day:'numeric'});
}

function card(f, isWork){
  const conf=f.confidence!=null?Math.round(f.confidence*100)+'%':'';
  const rev=f.review==='approved'?'<span class="confirmed">confirmed</span>'
    :(f.review==='edited'?'edited by you':'');
  const meta=[f.kind,fmtWhen(f.updated_at),conf,rev].filter(Boolean).join(' · ');
  return '<div class="card" data-id="'+f.fact_id+'">'
    +'<div class="t">'+MnemosEsc(f.text||'')+'<div class="meta">'+meta+'</div></div>'
    +'<span class="pill">'+MnemosEsc(f.kind||'')+'</span>'
    +'<span class="acts">'
    +(f.review!=='approved'?'<button data-act="approve" title="Yes, this is right">Confirm</button>':'')
    +'<button data-act="edit" title="Correct the wording">Edit</button>'
    +(isWork?'<button data-act="done" title="Mark done">Done</button>':'')
    +'<button data-act="dismiss" class="warn" title="Forget this">Forget</button>'
    +'</span></div>';
}

async function act(id, action){
  if(action==='edit'){
    const cardEl=document.querySelector('.card[data-id="'+id+'"] .t');
    const cur=cardEl?cardEl.childNodes[0].textContent:'';
    const next=prompt('Correct it — what should this say?', cur);
    if(next==null||!next.trim()||next===cur) return;
    await fetch('/facts/'+id+'/edit',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:next.trim()})});
  }else{
    await fetch('/facts/'+id+'/'+action,{method:'POST'});
  }
  load();
  if(!document.getElementById('tabPeople').hidden){
    if(openPersonId!==null) loadPersonDetail(openPersonId);
    else loadPeople(true);
  }
}

document.addEventListener('click', e=>{
  const b=e.target.closest('button[data-act]'); if(!b) return;
  const cardEl=b.closest('.card'); if(!cardEl) return;
  act(cardEl.dataset.id, b.dataset.act);
});

async function load(){
  const d=await (await fetch('/profile/data')).json();
  const id=document.getElementById('idCard');
  if(d.identity && d.identity.name){
    const c=[];
    if(d.identity.primary_email) c.push('<div><span>Primary email</span>'
      +MnemosEsc(d.identity.primary_email)+'</div>');
    if(d.identity.secondary_email) c.push('<div><span>Secondary email</span>'
      +MnemosEsc(d.identity.secondary_email)+'</div>');
    if(d.identity.phone) c.push('<div><span>Phone</span>'
      +MnemosEsc(d.identity.phone)+'</div>');
    id.innerHTML='<div class="nm">'+MnemosEsc(d.identity.name)+'</div>'
      +(d.identity.role?'<div class="rl">'+MnemosEsc(d.identity.role)+'</div>':'')
      +(d.identity.description?'<div class="ds">'+MnemosEsc(d.identity.description)+'</div>':'')
      +(c.length?'<div class="contact">'+c.join('')+'</div>':'')
      +'<div class="meta" style="margin-top:10px"><a href="/onboarding">Edit identity in Setup →</a></div>';
  }else{
    id.innerHTML='<div class="lead">I don\'t know who you are yet.</div>'
      +'<div style="margin-top:8px"><a class="btn" href="/onboarding">Introduce yourself in Setup →</a></div>';
  }
  const about=document.getElementById('aboutList');
  about.innerHTML=(d.about&&d.about.length)
    ? d.about.map(f=>card(f,false)).join('')
    : '<div class="empty">Nothing yet — tell me things in chat ("I prefer…", "I\'m working on…") and they\'ll appear here.</div>';
  const work=document.getElementById('workList');
  work.innerHTML=(d.work&&d.work.length)
    ? d.work.map(f=>card(f,true)).join('')
    : '<div class="empty">Nothing open that belongs to you.</div>';
}

// ---- People tab -----------------------------------------------------------
let peopleCache=[], openPersonId=null;

function setTab(name){
  const tabs={profile:'tabProfile',people:'tabPeople',entities:'tabEntities',work:'tabWork'};
  const btns={profile:'tabBtnProfile',people:'tabBtnPeople',entities:'tabBtnEntities',work:'tabBtnWork'};
  Object.keys(tabs).forEach(k=>{
    document.getElementById(tabs[k]).hidden = name!==k;
    document.getElementById(btns[k]).classList.toggle('on', name===k);
  });
  MnemosMemory.set('profile.tab', name);
  if(name==='people') loadPeople();
  if(name==='entities') loadEntities();
  if(name==='work') loadWork();
}
document.getElementById('tabBtnProfile').onclick=()=>setTab('profile');
document.getElementById('tabBtnPeople').onclick=()=>setTab('people');
document.getElementById('tabBtnEntities').onclick=()=>setTab('entities');
document.getElementById('tabBtnWork').onclick=()=>setTab('work');

function fmtSeen(ts){
  if(!ts) return 'never seen';
  const d=(Date.now()/1000-ts)/86400;
  if(d<1) return 'seen today';
  if(d<2) return 'seen yesterday';
  return 'seen '+Math.round(d)+'d ago';
}

// Live refresh must never fight the user: pause while a card is open or a
// field is focused, and skip the re-render entirely when nothing changed
// (the version token moves for ALL memory, not just people).
function peopleBusy(){
  if(openPersonId!==null) return true;
  const ae=document.activeElement;
  return !!(ae && ae.closest && ae.closest('#tabPeople')
    && (ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'));
}
let peopleSig='';
async function loadPeople(force){
  const showCand=!!(document.getElementById('pShowCandidates')||{}).checked;
  const d=await (await fetch('/people/list?include_candidates='+(showCand?'1':'0'))).json();
  let rows=d.people||[];
  // Main network view: hide zero-evidence rows (same floor as home).
  if(!showCand) rows=rows.filter(p=>(p.weight||0)>=1.0 || p.is_self);
  const sig=JSON.stringify({c:showCand,p:rows});
  if(!force && sig===peopleSig) return;   // no visible change — don't touch the DOM
  peopleSig=sig;
  peopleCache=rows;
  renderPeopleList();
}

function renderPeopleList(){
  const q=(document.getElementById('pSearch').value||'').toLowerCase();
  const el=document.getElementById('peopleList');
  const seed=document.getElementById('exhaustSeed');
  const rows=peopleCache.filter(p=>!q||p.name.toLowerCase().includes(q));
  const seeded=rows.filter(p=>p.from_calendar || (p.interaction_strength||0)>0);
  if(seed){
    seed.hidden=!seeded.length;
    if(seeded.length){
      seed.className='';
      seed.style.cssText='margin-top:8px;border:1px dashed var(--line);border-radius:12px;padding:12px 14px';
      seed.innerHTML='<div class="plabel" style="margin:0 0 6px">Seeded from your email/calendar</div>'
        +'<p class="lead" style="margin:0 0 8px">Confirm people you know, merge duplicates, or forget noise. Nothing here authorizes an action.</p>'
        +seeded.slice(0,12).map(p=>'<div class="prow" data-pid="'+p.id+'">'
          +'<div style="flex:1"><span class="nm">'+MnemosEsc(p.name)+'</span>'
          +(p.from_calendar?' <span class="chip">calendar</span>':' <span class="chip">email</span>')
          +'</div></div>').join('');
    }
  }
  if(!rows.length){el.innerHTML='<div class="empty">No one in your network yet — name people in setup, chat, or speech and they\'ll appear. Turn on “Show candidates” to review unresolved mentions.</div>';return;}
  el.innerHTML=rows.map(p=>{
    const row='<div class="prow" data-pid="'+p.id+'">'
      +'<div style="flex:1"><span class="nm">'+MnemosEsc(p.name)+'</span>'
      +(p.is_self?' <span class="chip">you</span>':'')
      +(p.promotion_state==='candidate'?' <span class="chip">candidate</span>':'')
      +(p.from_calendar?' <span class="chip">calendar</span>':'')
      +'<div class="meta">connection '+p.weight.toFixed(1)+' · '+fmtSeen(p.last_seen)+'</div></div>'
      +'<span class="meta">'+(openPersonId===p.id?'▾':'▸')+'</span></div>';
    return row+(openPersonId===p.id?'<div class="pdetail" id="pDetail"><div class="lead">Loading…</div></div>':'');
  }).join('');
  if(openPersonId!==null) loadPersonDetail(openPersonId);
}

document.getElementById('pSearch').addEventListener('input', renderPeopleList);
document.getElementById('pShowCandidates').addEventListener('change', ()=>loadPeople(true));

document.addEventListener('click', e=>{
  if(e.target.closest('button')) return;
  const row=e.target.closest('.prow[data-pid]'); if(!row) return;
  const pid=parseInt(row.dataset.pid,10);
  openPersonId = (openPersonId===pid)?null:pid;
  renderPeopleList();
});

const DETAIL_FIELDS=[['phone','Phone'],['email','Email'],['role','Job / role'],
                     ['org','Organization'],['team','Team'],['location','Location']];
const MULTI_DETAIL=new Set(['phone','email','org','team']);

function detailSrcChip(d){
  if(d.source==='you')
    return '<span class="chip" style="align-self:center" title="You set this">you</span>';
  if(d.source==='graph')
    return '<span class="chip" style="align-self:center" title="'+MnemosEsc(d.quote||'graph')+'">connected</span>';
  if(d.source==='attributed')
    return '<span class="chip" style="align-self:center" title="'+MnemosEsc(d.quote||'attributed')+'">attributed</span>';
  if(d.value)
    return '<span class="chip" style="align-self:center" title="'+MnemosEsc(d.quote||'mined from memory')+'">from memory</span>';
  return '';
}

async function loadPersonDetail(pid){
  const host=document.getElementById('pDetail'); if(!host) return;
  const p=await (await fetch('/people/'+pid)).json();
  let h='<h3>'+MnemosEsc(p.name)+'</h3>';
  if(p.aliases&&p.aliases.length)
    h+='<div>'+p.aliases.map(a=>'<span class="chip">'+MnemosEsc(a)+'</span>').join('')+'</div>';
  h+='<div class="plabel">Details — add phones, orgs, and teams; × removes one value</div>';
  const lists=p.detail_lists||{};
  DETAIL_FIELDS.forEach(([k,label])=>{
    const rows=lists[k]||[];
    const multi=MULTI_DETAIL.has(k);
    if(multi){
      h+='<div class="plabel" style="margin-top:14px">'+label+(rows.length!==1?'s':'')+'</div>';
      rows.forEach(d=>{
        const canRemove=d.ref && !String(d.ref).startsWith('merged:');
        h+='<div class="pfield">'
          +'<input readonly value="'+MnemosEsc(d.value||'')+'">'
          +detailSrcChip(d)
          +(canRemove
            ?'<button data-pact="detail-remove" data-key="'+k+'" data-ref="'+MnemosEsc(d.ref)+'" data-value="'+MnemosEsc(d.value||'')+'" title="Remove">×</button>'
            :'')
          +'</div>';
      });
      h+='<div class="pfield"><input id="pd_add_'+k+'" placeholder="Add '+label.toLowerCase()+'…">'
        +'<button data-pact="detail-add" data-key="'+k+'">Add</button></div>';
    }else{
      const d=(p.details||{})[k]||{};
      h+='<div class="pfield"><span style="min-width:96px;align-self:center;font:11px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--mut)">'+label+'</span>'
        +'<input id="pd_'+k+'" value="'+MnemosEsc(d.value||'')+'" placeholder="unknown — type to set">'
        +detailSrcChip(d)
        +'<button data-pact="detail" data-key="'+k+'">Save</button></div>';
    }
  });
  if(p.affiliations&&p.affiliations.length)
    h+='<div class="plabel">Connected to</div><div>'
      +p.affiliations.map(a=>'<span class="chip">'+MnemosEsc(a.name)+' · '+MnemosEsc(a.predicate)+'</span>').join('')+'</div>';
  if(p.discussed_with&&p.discussed_with.length)
    h+='<div class="plabel">Comes up with</div><div>'
      +p.discussed_with.map(x=>'<span class="chip">'+MnemosEsc(x.name)+'</span>').join('')+'</div>';
  h+='<div class="plabel">Correct the name</div>'
    +'<div class="pfield"><input id="pRename" value="'+MnemosEsc(p.name)+'">'
    +'<button data-pact="rename">Rename</button></div>'
    +'<div class="plabel">Add an alias (other spellings the ears hear)</div>'
    +'<div class="pfield"><input id="pAlias" placeholder="a nickname or other spelling">'
    +'<button data-pact="alias">Add</button></div>'
    +'<div class="plabel">Tell me something about them</div>'
    +'<div class="pfield"><input id="pNote" placeholder="e.g. runs platform at Foundry; met at the Nova demo day">'
    +'<button data-pact="note">Save</button></div>';
  if(p.facts&&p.facts.length){
    h+='<div class="plabel">What I know</div>'+p.facts.map(f=>card(f,f.kind!=='claim')).join('');
  }else{
    h+='<div class="empty">No facts yet — the note box above is the fastest way to teach me.</div>';
  }
  if(!p.is_self){
    h+='<div class="pfield" style="margin-top:14px">'
      +'<button data-pact="confirm">Confirm</button>'
      +'<button data-pact="merge">Merge…</button>'
      +'<button class="forget-person" data-pact="forget">Forget this person…</button>'
      +'</div>';
  }
  host.innerHTML=h;
  host.dataset.pid=pid;
}

async function personAct(pid, act, key, ref, value){
  if(act==='detail'){
    const el=document.getElementById('pd_'+key);
    const v=(el&&el.value||'').trim();
    const r=await fetch('/people/'+pid+'/detail',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({key,value:v,op:'set'})});
    if(!r.ok){alert((await r.json()).detail||'save failed');return;}
  }else if(act==='detail-add'){
    const el=document.getElementById('pd_add_'+key);
    const v=(el&&el.value||'').trim(); if(!v) return;
    const r=await fetch('/people/'+pid+'/detail',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key,value:v,op:'add'})});
    if(!r.ok){alert((await r.json()).detail||'add failed');return;}
  }else if(act==='detail-remove'){
    const r=await fetch('/people/'+pid+'/detail',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({key,value:value||'',op:'remove',ref})});
    if(!r.ok){alert((await r.json()).detail||'remove failed');return;}
  }else if(act==='rename'){
    const name=document.getElementById('pRename').value.trim(); if(!name) return;
    const r=await fetch('/people/'+pid+'/rename',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    if(!r.ok){alert((await r.json()).detail||'rename failed');return;}
  }else if(act==='alias'){
    const alias=document.getElementById('pAlias').value.trim(); if(!alias) return;
    await fetch('/people/'+pid+'/alias',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({alias})});
  }else if(act==='note'){
    const text=document.getElementById('pNote').value.trim(); if(!text) return;
    await fetch('/people/'+pid+'/note',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  }else if(act==='confirm'){
    await fetch('/people/'+pid+'/confirm',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({state:'contact'})});
  }else if(act==='merge'){
    const other=prompt('Id of the person to merge INTO this one (absorbed id):');
    if(!other) return;
    const r=await fetch('/people/'+pid+'/soft-merge',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({absorbed_id:parseInt(other,10),reason:'exhaust merge'})});
    if(!r.ok){alert((await r.json()).detail||'merge failed');return;}
  }else if(act==='forget'){
    const p=peopleCache.find(x=>x.id===pid);
    if(!confirm('Forget '+(p?p.name:'this person')+'? Their node and connections are removed; facts stay but are detached.')) return;
    await fetch('/people/'+pid+'/forget',{method:'POST'});
    openPersonId=null;
  }
  loadPeople(true);   // re-render roster; an open card refetches its detail
}

document.addEventListener('click', e=>{
  const b=e.target.closest('button[data-pact]'); if(!b) return;
  e.stopPropagation();
  const host=b.closest('.pdetail');
  personAct(parseInt((host&&host.dataset.pid)||openPersonId,10), b.dataset.pact,
            b.dataset.key, b.dataset.ref, b.dataset.value);
});

// ---- Orgs & tools tab -----------------------------------------------------
let entityCache=[], openEntityId=null, entitySig='';
const ENTITY_KINDS=['org','project','tool','place','idea','thing'];
const E_DETAIL_FIELDS=[['status','Status'],['owner','Owner'],
                       ['url','Website'],['location','Location']];

function entityBusy(){
  if(openEntityId!==null) return true;
  const ae=document.activeElement;
  return !!(ae && ae.closest && ae.closest('#tabEntities')
    && (ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'));
}

async function loadEntities(force){
  const d=await (await fetch('/entities/list')).json();
  const sig=JSON.stringify(d.entities||[]);
  if(!force && sig===entitySig) return;
  entitySig=sig;
  entityCache=d.entities||[];
  renderEntityList();
}

function entityRowHtml(x){
  const brief=(x.kind==='org'||x.kind==='company'||x.kind==='organization')
    ?' <a class="chip" href="/org/'+x.id+'" onclick="event.stopPropagation()">brief</a>':'';
  const row='<div class="prow" data-eid="'+x.id+'">'
    +'<div style="flex:1"><span class="nm">'+MnemosEsc(x.name)+'</span>'
    +' <span class="chip">'+MnemosEsc(x.kind)+'</span>'+brief
    +'<div class="meta">connection '+x.weight.toFixed(1)+' · '+fmtSeen(x.last_seen)+'</div></div>'
    +'<span class="meta">'+(openEntityId===x.id?'▾':'▸')+'</span></div>';
  return row+(openEntityId===x.id?'<div class="pdetail" id="eDetail"><div class="lead">Loading…</div></div>':'');
}

function renderEntityList(){
  const q=(document.getElementById('eSearch').value||'').toLowerCase();
  const el=document.getElementById('entityList');
  const rows=entityCache.filter(x=>!q||x.name.toLowerCase().includes(q));
  if(!rows.length){el.innerHTML='<div class="empty">Nothing yet — mention orgs, projects, and tools in chat or speech.</div>';return;}
  // Group by home project (the rollup's associated_project edge). A project's
  // own row leads its section; entities with no dominant project land in
  // "Everything else" at the bottom.
  const groups=new Map(), loose=[];
  rows.forEach(x=>{
    const p=x.project&&x.project.name;
    if(p){ if(!groups.has(p)) groups.set(p,{rows:[],w:0});
      const g=groups.get(p); g.rows.push(x); g.w+=x.weight; }
    else loose.push(x);
  });
  // Pull each section's project row out of the loose list so it heads its group.
  const looseRest=[];
  loose.forEach(x=>{
    const g=groups.get(x.name);
    if(g){ g.rows.unshift(x); g.w+=x.weight; } else looseRest.push(x);
  });
  const names=[...groups.keys()].sort((a,b)=>groups.get(b).w-groups.get(a).w);
  let h='';
  names.forEach(n=>{
    h+='<div class="plabel">'+MnemosEsc(n)+'</div>'
      +groups.get(n).rows.map(entityRowHtml).join('');
  });
  if(looseRest.length)
    h+=(names.length?'<div class="plabel">Everything else</div>':'')
      +looseRest.map(entityRowHtml).join('');
  el.innerHTML=h;
  if(openEntityId!==null) loadEntityDetail(openEntityId);
}

document.getElementById('eSearch').addEventListener('input', renderEntityList);

document.addEventListener('click', e=>{
  if(e.target.closest('button')) return;
  const row=e.target.closest('.prow[data-eid]'); if(!row) return;
  const eid=parseInt(row.dataset.eid,10);
  openEntityId = (openEntityId===eid)?null:eid;
  renderEntityList();
});

async function loadEntityDetail(eid){
  const host=document.getElementById('eDetail'); if(!host) return;
  const x=await (await fetch('/entities/'+eid)).json();
  let h='<h3>'+MnemosEsc(x.name)+'</h3>';
  if(x.kind==='org'||x.kind==='company'||x.kind==='organization')
    h+='<div style="margin:6px 0 10px"><a href="/org/'+eid+'">Open living brief →</a></div>';
  if(x.aliases&&x.aliases.length)
    h+='<div>'+x.aliases.map(a=>'<span class="chip">'+MnemosEsc(a)+'</span>').join('')+'</div>';
  h+='<div class="plabel">Category</div><div>'
    +ENTITY_KINDS.map(k=>'<button class="chip" style="cursor:pointer'
      +(x.kind===k?';color:var(--navy);border-color:var(--navy)':'')
      +'" data-eact="kind" data-kind="'+k+'">'+k+'</button>').join(' ')+'</div>';
  h+='<div class="plabel">Details — from memory; edit anything, Save with an empty box to forget your edit</div>';
  E_DETAIL_FIELDS.forEach(([k,label])=>{
    const d=(x.details||{})[k]||{};
    let src=d.source==='you'
      ? '<span class="chip" style="align-self:center" title="You set this — it wins over memory">you</span>'
      : (d.value?'<span class="chip" style="align-self:center" title="'+MnemosEsc(d.quote||'mined from memory')+'">from memory</span>':'');
    if(d.stale) src+='<span class="chip" style="align-self:center" title="Nothing has confirmed this in '
      +Math.round(d.age_days)+' days (its freshness window is '+Math.round(d.freshness_days)+')">stale</span>';
    h+='<div class="pfield"><span style="min-width:96px;align-self:center;font:11px var(--mono);letter-spacing:.05em;text-transform:uppercase;color:var(--mut)">'+label+'</span>'
      +'<input id="ed_'+k+'" value="'+MnemosEsc(d.value||'')+'" placeholder="unknown — type to set">'
      +src
      +'<button data-eact="detail" data-key="'+k+'">Save</button></div>';
  });
  if(x.people&&x.people.length)
    h+='<div class="plabel">People involved</div><div>'
      +x.people.map(p=>'<span class="chip">'+MnemosEsc(p.name)+'</span>').join('')+'</div>';
  h+='<div class="plabel">Correct the name</div>'
    +'<div class="pfield"><input id="eRename" value="'+MnemosEsc(x.name)+'">'
    +'<button data-eact="rename">Rename</button></div>'
    +'<div class="plabel">Add an alias</div>'
    +'<div class="pfield"><input id="eAlias" placeholder="a shorthand or other spelling">'
    +'<button data-eact="alias">Add</button></div>'
    +'<div class="plabel">Tell me something about it</div>'
    +'<div class="pfield"><input id="eNote" placeholder="what it is, why it matters">'
    +'<button data-eact="note">Save</button></div>';
  if(x.facts&&x.facts.length){
    h+='<div class="plabel">What I know</div>'+x.facts.map(f=>card(f,f.kind!=='claim')).join('');
  }else{
    h+='<div class="empty">No facts yet — the note box above is the fastest way to teach me.</div>';
  }
  h+='<button class="forget-person" data-eact="forget">Forget this…</button>';
  host.innerHTML=h;
  host.dataset.eid=eid;
}

async function entityAct(eid, act, kind, key){
  if(act==='detail'){
    const v=document.getElementById('ed_'+key).value.trim();
    const r=await fetch('/entities/'+eid+'/detail',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({key,value:v})});
    if(!r.ok){alert((await r.json()).detail||'save failed');return;}
  }else if(act==='rename'){
    const name=document.getElementById('eRename').value.trim(); if(!name) return;
    const r=await fetch('/entities/'+eid+'/rename',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
    if(!r.ok){alert((await r.json()).detail||'rename failed');return;}
  }else if(act==='alias'){
    const alias=document.getElementById('eAlias').value.trim(); if(!alias) return;
    await fetch('/entities/'+eid+'/alias',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({alias})});
  }else if(act==='note'){
    const text=document.getElementById('eNote').value.trim(); if(!text) return;
    await fetch('/entities/'+eid+'/note',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({text})});
  }else if(act==='kind'){
    await fetch('/entities/'+eid+'/kind',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify({kind})});
  }else if(act==='forget'){
    const x=entityCache.find(v=>v.id===eid);
    if(!confirm('Forget '+(x?x.name:'this')+'? Its node and connections are removed; facts stay but are detached.')) return;
    await fetch('/entities/'+eid+'/forget',{method:'POST'});
    openEntityId=null;
  }
  loadEntities(true);
}

document.addEventListener('click', e=>{
  const b=e.target.closest('button[data-eact]'); if(!b) return;
  e.stopPropagation();
  const host=b.closest('.pdetail');
  entityAct(parseInt((host&&host.dataset.eid)||openEntityId,10),
            b.dataset.eact, b.dataset.kind, b.dataset.key);
});

// ---- Tasks tab ------------------------------------------------------------
let workSig='';

function workBusy(){
  const ae=document.activeElement;
  return !!(ae && ae.closest && ae.closest('#tabWork')
    && (ae.tagName==='INPUT'||ae.tagName==='TEXTAREA'||ae.tagName==='SELECT'));
}

function workCard(f, open){
  const who=f.owner||f.to_person||'';
  const meta=[f.kind, who?('for '+who):'', f.due?('due '+f.due):'',
              fmtWhen(f.updated_at),
              f.review==='approved'?'<span class="confirmed">confirmed</span>':'']
    .filter(Boolean).join(' · ');
  return '<div class="card" data-wid="'+f.fact_id+'">'
    +'<input type="checkbox" class="wcheck" data-wsel value="'+f.fact_id+'" title="Select">'
    +'<div class="t">'+MnemosEsc(f.text||'')+'<div class="meta">'+meta+'</div></div>'
    +'<span class="pill">'+MnemosEsc(f.kind||'')+'</span>'
    +'<span class="acts">'
    +(open
      ? '<button data-wcard="done" title="Mark done">Done</button>'
        +'<button data-wcard="edit" title="Rewrite it">Edit</button>'
        +'<button data-wcard="due" title="Set the due date">Due</button>'
        +'<button data-wcard="dismiss" class="warn" title="Delete">Delete</button>'
      : '<button data-wcard="reopen" title="Back to the board">Reopen</button>')
    +'</span></div>';
}

function selectedWorkIds(){
  return [...document.querySelectorAll('#tabWork input.wcheck:checked')]
    .map(el=>parseInt(el.value,10)).filter(Boolean);
}

function syncWorkBar(){
  const ids=selectedWorkIds();
  const bar=document.getElementById('workBar');
  bar.classList.toggle('on', ids.length>0);
  document.getElementById('workSelCount').textContent=
    ids.length+' selected';
}

async function loadWork(force){
  const d=await (await fetch('/work/list')).json();
  const sig=JSON.stringify(d);
  if(!force && sig===workSig) return;
  workSig=sig;
  const op=document.getElementById('workOpen');
  let opHtml=(d.open&&d.open.length)
    ? d.open.map(f=>workCard(f,true)).join('')
    : '<div class="empty">Nothing open — add one above, or just say it out loud.</div>';
  if(d.screen_pending)
    opHtml='<div class="empty" style="border-style:solid">👁 '+d.screen_pending
      +' possible task'+(d.screen_pending===1?'':'s')+' spotted on your screen await '
      +'your confirmation — <a href="/memory">review in Memory Console</a>. '
      +'Confirmed ones join this board.</div>'+opHtml;
  op.innerHTML=opHtml;
  const cl=document.getElementById('workClosed');
  cl.innerHTML=(d.closed&&d.closed.length)
    ? d.closed.map(f=>workCard(f,false)).join('')
    : '<div class="empty">Nothing closed lately.</div>';
  document.getElementById('wSelectOpen').checked=false;
  document.getElementById('wSelectClosed').checked=false;
  syncWorkBar();
}

async function workAdd(){
  const text=document.getElementById('wText').value.trim();
  if(!text) return;
  const body={kind:document.getElementById('wKind').value, text,
              due:document.getElementById('wDue').value.trim()||null,
              owner:document.getElementById('wOwner').value.trim()||null};
  const r=await fetch('/work/add',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  if(!r.ok){alert((await r.json()).detail||'add failed');return;}
  document.getElementById('wText').value='';
  document.getElementById('wDue').value='';
  document.getElementById('wOwner').value='';
  loadWork(true);
}

async function workBulk(action){
  const ids=selectedWorkIds();
  if(!ids.length) return;
  const body={ids, action};
  if(action==='clear'){
    document.querySelectorAll('#tabWork input.wcheck:checked')
      .forEach(el=>{el.checked=false;});
    document.getElementById('wSelectOpen').checked=false;
    document.getElementById('wSelectClosed').checked=false;
    syncWorkBar();
    return;
  }
  if(action==='dismiss'){
    if(!confirm('Delete '+ids.length+' item'+(ids.length===1?'':'s')
      +'? They leave the board (kept in the archive as dismissed).')) return;
  }else if(action==='due'){
    const next=prompt('Due for all selected (free text; empty clears):','');
    if(next==null) return;
    body.due=next.trim()||null;
  }else if(action==='edit'){
    const next=prompt('Rewrite all '+ids.length+' selected to the same text:','');
    if(next==null||!next.trim()) return;
    body.text=next.trim();
  }
  const r=await fetch('/work/bulk',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json().catch(()=>({}));
  if(!r.ok){alert(j.detail||'bulk action failed');return;}
  if(j.updated!==ids.length)
    alert('Updated '+j.updated+' of '+ids.length
      +(j.results?(' — some failed'):''));
  loadWork(true);
}

document.addEventListener('change', e=>{
  if(e.target.id==='wSelectOpen'){
    document.querySelectorAll('#workOpen input.wcheck')
      .forEach(el=>{el.checked=e.target.checked;});
    syncWorkBar(); return;
  }
  if(e.target.id==='wSelectClosed'){
    document.querySelectorAll('#workClosed input.wcheck')
      .forEach(el=>{el.checked=e.target.checked;});
    syncWorkBar(); return;
  }
  if(e.target.matches&&e.target.matches('#tabWork input.wcheck')) syncWorkBar();
});

document.addEventListener('click', async e=>{
  const bulk=e.target.closest('button[data-wbulk]');
  if(bulk){ workBulk(bulk.dataset.wbulk); return; }
  const add=e.target.closest('button[data-wact="add"]');
  if(add){ workAdd(); return; }
  const b=e.target.closest('button[data-wcard]'); if(!b) return;
  const cardEl=b.closest('.card[data-wid]'); if(!cardEl) return;
  const id=cardEl.dataset.wid, act=b.dataset.wcard;
  if(act==='edit'){
    const cur=cardEl.querySelector('.t').childNodes[0].textContent;
    const next=prompt('Rewrite it:', cur);
    if(next==null||!next.trim()) return;
    await fetch('/facts/'+id+'/edit',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({text:next.trim()})});
  }else if(act==='due'){
    const next=prompt('Due (free text — a date, "Friday", "end of month"; empty clears):','');
    if(next==null) return;
    await fetch('/facts/'+id+'/due',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({due:next.trim()||null})});
  }else if(act==='dismiss'){
    if(!confirm('Delete this? It leaves the board and retrieval (kept in the archive as dismissed).')) return;
    await fetch('/facts/'+id+'/dismiss',{method:'POST'});
  }else{
    await fetch('/facts/'+id+'/'+act,{method:'POST'});
  }
  loadWork(true);
});

document.getElementById('wText').addEventListener('keydown', e=>{
  if(e.key==='Enter') workAdd();
});

// ---------------------------------------------------------------------------
let memVersion=null;
async function memCheck(){
  try{
    const v=(await (await fetch('/graph/version')).json()).version;
    if(memVersion!==null&&v!==memVersion){
      if(!document.getElementById('tabPeople').hidden){
        if(!peopleBusy()) loadPeople();     // quiet-change-aware, never mid-edit
      }else if(!document.getElementById('tabEntities').hidden){
        if(!entityBusy()) loadEntities();
      }else if(!document.getElementById('tabWork').hidden){
        if(!workBusy()) loadWork();
      }else{
        load();
      }
    }
    memVersion=v;
  }catch(e){}
}
load();
setTab(MnemosMemory.get('profile.tab','profile')||'profile');
setInterval(memCheck,4000);
</script>
</body>
</html>
""")
