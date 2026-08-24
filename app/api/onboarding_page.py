"""Guided onboarding wizard HTML — replaces hand-editing the JSON profile sheet."""

from app.api.mnemos_theme import apply as _mnemos

ONBOARDING_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Meet you</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
  font:16px/1.55 var(--font);color:var(--text);
  background:
    radial-gradient(800px 420px at 12% -8%, var(--acc-08), transparent 55%),
    radial-gradient(640px 360px at 95% 8%, rgba(30,91,79,.05), transparent 50%),
    linear-gradient(180deg, #FBF9F4 0%, var(--bg) 42%, #F2EFE8 100%);
  background-attachment:fixed;
}
a{color:var(--navy);text-decoration:none}
a:hover{opacity:.75}

.wrap{max-width:720px;margin:0 auto;padding:28px 20px 80px}

.hero{
  min-height:min(88vh,720px);display:flex;flex-direction:column;justify-content:center;
  padding:24px 0 48px;animation:rise .4s var(--ease) both;
}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes inkLine{
  from{transform:scaleX(0);opacity:0}
  to{transform:scaleX(1);opacity:1}
}
.brand-row{
  display:flex;align-items:center;gap:12px;margin:0 0 22px;
}
.brand-row .mark{color:var(--acc);width:36px;height:36px}
.brand-row .mark path{
  stroke-dasharray:48;animation:inkDraw .5s var(--ease) both;
}
@keyframes inkDraw{from{stroke-dashoffset:48}to{stroke-dashoffset:0}}
.brand{
  font-family:var(--display);font-weight:400;font-size:clamp(3.4rem,11vw,5.8rem);
  letter-spacing:-.03em;line-height:.92;margin:0;color:var(--navy);
}
.hero-rule{
  width:72px;height:1.5px;background:var(--acc);margin:0 0 22px;
  transform-origin:left center;animation:inkLine .4s var(--ease) .15s both;
}
.hero h1{
  font-family:var(--display);font-weight:400;font-size:clamp(1.5rem,3.6vw,2.1rem);
  letter-spacing:-.02em;margin:0 0 12px;max-width:18ch;color:var(--navy);
}
.hero p{color:var(--mut);font-size:1.05rem;max-width:36ch;margin:0 0 28px}
.cta-row{display:flex;flex-wrap:wrap;gap:14px;align-items:center}
.btn{
  appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;
  border-radius:12px;padding:12px 22px;
  transition:transform .22s var(--ease),background .28s var(--ease),opacity .28s var(--ease),
    border-color .28s var(--ease),box-shadow .28s var(--ease),filter .28s var(--ease);
}
.btn:hover:not(:disabled){transform:translateY(-2px)}
.btn:active:not(:disabled){transform:translateY(0) scale(.97)}
.btn-primary{
  background:var(--navy);color:#F8F6F1;
  box-shadow:0 2px 10px rgba(11,19,32,.16);
}
.btn-primary:hover:not(:disabled){
  background:#152033;box-shadow:0 8px 22px rgba(11,19,32,.22);filter:brightness(1.05);
}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.btn-ghost:hover:not(:disabled){
  border-color:var(--acc-45);color:var(--navy);
  background:var(--acc-05);box-shadow:0 4px 14px rgba(11,19,32,.06);
}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.skip{
  font-size:.9rem;color:var(--mut);
  transition:color .22s var(--ease),transform .22s var(--ease);
  display:inline-block;
}
.skip:hover{color:var(--navy);text-decoration:none;transform:translateY(-1px)}

.wizard{display:none;animation:rise .35s var(--ease) both}
.wizard.on{display:block}
.progress{display:flex;gap:6px;margin:8px 0 28px}
.progress i{
  flex:1;height:2px;border-radius:2px;background:rgba(11,19,32,.08);
  transition:background .3s var(--ease);
}
.progress i.on{background:var(--acc)}
.progress i.done{background:var(--acc-40)}

.panel{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:24px 22px 20px;box-shadow:var(--shadow);
  animation:slide .35s var(--ease) both;
}
@keyframes slide{from{opacity:0;transform:translateX(10px)}to{opacity:1;transform:none}}
.panel h2{
  font-family:var(--display);font-size:1.55rem;font-weight:400;margin:0 0 6px;
  letter-spacing:-.02em;color:var(--navy);
}
.panel .lead{color:var(--mut);font-size:.95rem;margin:0 0 20px}

label{display:block;font-size:.78rem;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;color:var(--mut);margin:14px 0 6px}
input,textarea,select{
  width:100%;font:inherit;color:var(--text);background:var(--bg-elev);
  border:1px solid var(--line);border-radius:12px;padding:11px 13px;outline:none;
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
input:focus,textarea:focus{border-color:var(--acc-45);
  box-shadow:0 0 0 3px var(--acc-dim)}
textarea{min-height:96px;resize:vertical}
.row{display:grid;gap:12px}
@media(min-width:640px){.row.two{grid-template-columns:1fr 1fr}}

.list{display:flex;flex-direction:column;gap:12px;margin-top:8px}
.item{
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;
  background:var(--bg);
}
.item-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:4px}
.item-head span{font-size:.8rem;color:var(--mut);font-weight:600}
.linkish{
  background:none;border:0;color:var(--mut);cursor:pointer;font:inherit;font-size:.85rem;
  padding:0;transition:color .22s var(--ease),transform .22s var(--ease);
}
.linkish:hover{color:var(--danger);transform:translateX(1px)}
.add{
  margin-top:12px;background:transparent;border:1px dashed rgba(11,19,32,.16);color:var(--mut);
  border-radius:12px;padding:10px;width:100%;cursor:pointer;font:inherit;font-weight:600;
  transition:border-color .28s var(--ease),color .28s var(--ease),
    background .28s var(--ease),transform .22s var(--ease),box-shadow .28s var(--ease);
}
.add:hover{
  border-color:var(--acc-45);color:var(--acc);
  background:var(--acc-05);transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(11,19,32,.05);
}
.add:active{transform:translateY(0) scale(.99)}

.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}
.chip-in{
  display:inline-flex;align-items:center;gap:6px;padding:6px 10px;border-radius:999px;
  background:var(--acc-dim);border:1px solid var(--acc-25);font-size:.9rem;color:var(--navy);
}
.chip-in button{background:none;border:0;color:var(--mut);cursor:pointer;font-size:1rem;line-height:1}
.tag-row{display:flex;gap:8px}
.tag-row input{flex:1}

.nav-btns{display:flex;justify-content:space-between;gap:12px;margin-top:22px;flex-wrap:wrap}
.err{color:var(--danger);font-size:.9rem;margin-top:10px;min-height:1.2em}
.ok-banner{
  border:1px solid rgba(46,111,87,.28);background:rgba(46,111,87,.06);
  border-radius:var(--radius);padding:18px;margin-top:12px;
}
.ok-banner h3{font-family:var(--display);font-weight:400;margin:0 0 8px;color:var(--navy);font-size:1.4rem}
.muted{color:var(--mut);font-size:.92rem}
.done-links{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px}
.top-mini{
  display:flex;justify-content:space-between;align-items:baseline;margin-bottom:8px;
  font-size:.85rem;color:var(--mut);
}
.top-mini .nm{
  font-family:var(--display);font-weight:400;color:var(--navy);font-size:1.15rem;
  display:inline-flex;align-items:center;gap:6px;
}
.top-mini .nm .mark{width:18px;height:18px;color:var(--acc)}
@media(max-width:640px){
  .wrap{padding:18px 14px 64px}
  .panel{padding:18px 16px}
  .nav-btns{flex-direction:column-reverse;align-items:stretch}
  .nav-btns button{width:100%}
  .tag-row{flex-direction:column}
  .progress{gap:4px}
}
</style>
</head>
<body>
<div class="wrap">

  <section class="hero" id="hero">
    <div class="brand-row">
      @@MARK@@
      <div class="brand">@@BRAND@@</div>
    </div>
    <div class="hero-rule" aria-hidden="true"></div>
    <h1>Your first meeting, remembered</h1>
    <p>Connect a calendar. Mnemos listens to the next meeting and writes a brief you can play back. Always-on capture stays off until you turn it on.</p>
    <div class="cta-row">
      <button class="btn btn-primary" id="startBtn" type="button">Get started</button>
      <a class="skip" href="/today">Skip to Today</a>
    </div>
    <p class="muted" id="statusLine" style="margin-top:20px"></p>
  </section>

  <section class="wizard" id="wizard">
    <div class="top-mini"><span class="nm">@@MARK@@ @@BRAND@@</span><span id="stepLabel">Step 1 of 4</span></div>
    <div class="progress" id="progress" aria-hidden="true">
      <i class="on"></i><i></i><i></i><i></i>
    </div>

    <!-- 0 You -->
    <div class="panel step" data-step="0">
      <h2>You</h2>
      <p class="lead">So @@BRAND@@ can recognize you in conversation and memory.</p>
      <div id="scanBox" hidden style="border:1px dashed var(--acc-28);border-radius:12px;padding:12px 14px;margin-bottom:16px;background:var(--acc-05)">
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <button type="button" class="btn btn-ghost" id="scanBtn">✨ Add context from my system</button>
          <span class="muted" id="scanStatus" style="flex:1;min-width:160px">Optional: let @@BRAND@@ learn what you're working on — the projects and tools you actually use — as background context in its memory. It doesn't fill in the answers below; those are yours to write. Nothing leaves your machine.</span>
        </div>
        <label id="bmConsent" hidden style="display:flex;gap:8px;align-items:flex-start;margin:12px 0 0;text-transform:none;letter-spacing:0;font-weight:400;font-size:.9rem;color:var(--text);cursor:pointer">
          <input type="checkbox" id="bmCheck" style="width:auto;margin-top:3px">
          <span>Also include my browser bookmarks. Only recognized apps (GitHub, Notion, Figma…) are learned — your personal bookmarks and their URLs are never read into @@BRAND@@.</span>
        </label>
      </div>
      <!-- Documents: content-level, so it's a SEPARATE explicit consent (unlike the
           metadata scan above, this reads file text and sends it to the model). -->
      <div id="docsBox" hidden style="border:1px dashed var(--acc-28);border-radius:12px;padding:12px 14px;margin-bottom:16px;background:var(--acc-05)">
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <button type="button" class="btn btn-ghost" id="docsBtn" disabled>📄 Read my documents</button>
          <span class="muted" id="docsStatus" style="flex:1;min-width:160px">Optional: let @@BRAND@@ read the text of your recent documents (PDF, Word, notes) so it can answer questions about them. Unlike the scan above, this sends document text to the model to pull out tasks and facts — everything it learns is <b>reviewable in Memory</b> and can be removed. <span id="docsRoots"></span></span>
        </div>
        <label id="docsConsent" style="display:flex;gap:8px;align-items:flex-start;margin:12px 0 0;text-transform:none;letter-spacing:0;font-weight:400;font-size:.9rem;color:var(--text);cursor:pointer">
          <input type="checkbox" id="docsCheck" style="width:auto;margin-top:3px">
          <span>I understand @@BRAND@@ will read the text of my recent documents and extract facts from them.</span>
        </label>
      </div>
      <label for="name">Your name</label>
      <input id="name" autocomplete="name" placeholder="Your name">
      <div class="row two">
        <div>
          <label for="role">Role</label>
          <input id="role" placeholder="e.g. Product engineer">
        </div>
        <div>
          <label for="description">How you describe your work</label>
          <input id="description" placeholder="Short line about your day-to-day">
        </div>
      </div>
      <label style="margin-top:18px">AI model account</label>
      <p class="lead" style="margin:4px 0 8px">@@BRAND@@ answers locally when it can and asks a bigger cloud model when it must. Connect whichever account you already have — the key is stored in <span style="font-family:var(--mono)">.credentials.env</span> on this machine and sent only to the provider you pick.</p>
      <!-- Invite path (WS-D Tier 1). Hidden unless this build has an invite
           service configured, so the BYO form is unchanged for everyone else.
           Shown first because it is the path that needs no account at all. -->
      <div id="inviteBox" hidden style="margin:0 0 14px;padding:12px 14px;border:1px solid var(--line);border-radius:10px">
        <label for="invitecode">Invite code</label>
        <p class="lead" style="margin:4px 0 8px">Were you given a code like
          <span style="font-family:var(--mono)">ABCD-EFGH-JKLM</span>? Use it here —
          nothing to sign up for. It is exchanged once for a key of your own,
          stored on this machine.</p>
        <div class="row two">
          <div>
            <input id="invitecode" autocomplete="off" placeholder="ABCD-EFGH-JKLM"
                   spellcheck="false" style="text-transform:uppercase">
          </div>
          <div>
            <button type="button" class="btn btn-ghost" id="inviteBtn">Use invite code</button>
          </div>
        </div>
        <div class="muted" id="inviteMsg"></div>
        <div class="muted" style="margin-top:8px">Or connect your own account below.</div>
      </div>
      <div class="row two">
        <div>
          <label for="provider">Provider</label>
          <select id="provider" style="width:100%">
            <option value="anthropic">Anthropic (Claude)</option>
            <option value="openai">OpenAI (ChatGPT)</option>
            <option value="google">Google (Gemini)</option>
            <option value="xai">xAI (Grok)</option>
          </select>
        </div>
        <div>
          <label for="apikey">API key</label>
          <input id="apikey" type="password" autocomplete="off" placeholder="sk-ant-…">
        </div>
      </div>
      <button type="button" class="btn btn-ghost" id="keyBtn" style="margin-top:8px">Save &amp; test key</button>
      <div class="muted" id="keyMsg"></div>
      <label for="primary_email" style="margin-top:18px">Primary email</label>
      <input id="primary_email" type="email" autocomplete="email" placeholder="you@example.com">
      <div class="row two">
        <div>
          <label for="secondary_email">Secondary email</label>
          <input id="secondary_email" type="email" autocomplete="email" placeholder="Optional">
        </div>
        <div>
          <label for="phone">Phone</label>
          <input id="phone" type="tel" autocomplete="tel" placeholder="+1 555 0100">
        </div>
      </div>
    </div>

    <!-- 1 Calendar -->
    <div class="panel step" data-step="1" hidden>
      <h2>Calendar</h2>
      <p class="lead">Mnemos reads event titles, times, and attendees — never email bodies. This seeds who you work with before any audio exists.</p>
      <div id="exStatus" class="muted">Checking Google connection…</div>
      <button type="button" class="btn btn-primary" id="exBtn" style="margin-top:10px">Connect Google (Gmail + Calendar metadata)</button>
      <div class="err" id="exMsg"></div>
      <div id="exProg" class="muted" style="margin-top:8px"></div>
      <label style="margin-top:26px">iCloud calendar (optional)</label>
      <p class="lead" style="margin:4px 0 10px">App-specific password at
      <a href="https://appleid.apple.com" target="_blank" rel="noopener">appleid.apple.com</a>
      — never your real Apple password.</p>
      <div id="icStatusOb" class="muted" style="margin:0 0 8px"></div>
      <div id="icFormOb" class="row two">
        <input id="icUserOb" placeholder="Apple ID email" autocomplete="username">
        <input id="icPassOb" placeholder="xxxx-xxxx-xxxx-xxxx" type="password" autocomplete="off">
      </div>
      <button type="button" class="btn btn-ghost" id="icBtnOb" style="margin-top:10px">Connect iCloud</button>
      <div class="err" id="icMsgOb"></div>
    </div>

    <!-- 2 Next meeting -->
    <div class="panel step" data-step="2" hidden>
      <h2>Your next meeting</h2>
      <p class="lead" id="nextMeetLead">Once a calendar is connected, Mnemos will listen inside that window and write a brief with playable clips.</p>
      <div id="nextMeetBox" class="ok-banner" hidden>
        <h3 id="nextMeetTitle">Meeting</h3>
        <p class="muted" id="nextMeetWhen"></p>
        <p>@@BRAND@@ will listen and produce a brief. Always-on mic stays off.</p>
      </div>
      <label style="display:flex;gap:8px;align-items:flex-start;margin-top:16px;text-transform:none;letter-spacing:0;font-weight:400;font-size:.95rem;color:var(--text);cursor:pointer">
        <input type="checkbox" id="meetListen" style="width:auto;margin-top:3px" checked>
        <span>Listen during calendar meetings (audio is stored under <span style="font-family:var(--mono)">data/audio</span> on this laptop).</span>
      </label>
    </div>

    <!-- 3 Capture opt-ins (default OFF) -->
    <div class="panel step" data-step="3" hidden>
      <h2>Always-on capture</h2>
      <p class="lead">Optional. Each source stays off unless you check it. You can change this later in Privacy.</p>
      <label style="display:flex;gap:8px;align-items:flex-start;text-transform:none;letter-spacing:0;font-weight:400;font-size:.95rem;color:var(--text);cursor:pointer">
        <input type="checkbox" id="ambMic" style="width:auto;margin-top:3px">
        <span><b>Microphone (always on)</b> — hears the room between meetings. WAV clips in <span style="font-family:var(--mono)">data/audio</span>.</span>
      </label>
      <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;text-transform:none;letter-spacing:0;font-weight:400;font-size:.95rem;color:var(--text);cursor:pointer">
        <input type="checkbox" id="ambCam" style="width:auto;margin-top:3px">
        <span><b>Webcam</b> — occasional frames in <span style="font-family:var(--mono)">data/frames</span>. Off unless you want visual memory.</span>
      </label>
      <label style="display:flex;gap:8px;align-items:flex-start;margin-top:12px;text-transform:none;letter-spacing:0;font-weight:400;font-size:.95rem;color:var(--text);cursor:pointer">
        <input type="checkbox" id="ambDesk" style="width:auto;margin-top:3px">
        <span><b>Screen</b> — window titles and frames in <span style="font-family:var(--mono)">data/</span>. No keystrokes. Off by default.</span>
      </label>
    </div>

    <!-- 10 People (optional, not in primary wizard) -->
    <div class="panel step" data-step="10" hidden>
      <h2>People</h2>
      <p class="lead">Names @@BRAND@@ should get right — teammates, family, partners.</p>
      <div class="list" id="peopleList"></div>
      <button type="button" class="add" id="addPerson">+ Add person</button>
    </div>

    <!-- 11 Work (optional) -->
    <div class="panel step" data-step="11" hidden>
      <h2>Work</h2>
      <p class="lead">Projects and tools you live in every day.</p>
      <div class="list" id="projectsList"></div>
      <button type="button" class="add" id="addProject">+ Add project</button>
      <label style="margin-top:22px">Tools you use</label>
      <div class="tag-row">
        <input id="toolInput" placeholder="Type a tool and press Enter">
        <button type="button" class="btn btn-ghost" id="addTool">Add</button>
      </div>
      <div class="chips" id="toolsChips"></div>
    </div>

    <!-- 12 Rhythm (optional) -->
    <div class="panel step" data-step="12" hidden>
      <h2>Rhythm</h2>
      <p class="lead">Routines and what matters right now — helps anticipation and chat.</p>
      <label>Schedule / routines</label>
      <div class="tag-row">
        <input id="schedInput" placeholder="e.g. Standup at 10am">
        <button type="button" class="btn btn-ghost" id="addSched">Add</button>
      </div>
      <div class="chips" id="schedChips"></div>
      <label style="margin-top:18px">Current priorities</label>
      <div class="tag-row">
        <input id="prioInput" placeholder="e.g. Ship onboarding this week">
        <button type="button" class="btn btn-ghost" id="addPrio">Add</button>
      </div>
      <div class="chips" id="prioChips"></div>
      <label for="notes">Anything else</label>
      <textarea id="notes" placeholder="Free-form notes @@BRAND@@ should remember…"></textarea>
    </div>

    <!-- 4 Phone -->
    <div class="panel step" data-step="13" hidden>
      <h2>Your phone</h2>
      <p class="lead">Optional — pair your iPhone or Android so it can send @@BRAND@@
      notes, dictations, and shares directly. Skippable; you can pair anytime at
      <a href="/phone" target="_blank" rel="noopener">the Phone page</a>.</p>
      <div id="phoneWarn"></div>
      <button type="button" class="btn btn-ghost" id="phonePairBtn">Connect a phone</button>
      <div id="phonePairBox" hidden style="margin-top:16px">
        <div style="display:flex;gap:20px;align-items:center;flex-wrap:wrap">
          <div id="phoneQr" style="background:#fff;border:1px solid var(--line);border-radius:12px;padding:10px;width:184px;height:184px"></div>
          <div>
            <label style="margin-top:0">Pairing code</label>
            <div style="font-family:var(--mono);font-size:1.9rem;letter-spacing:.3em;color:var(--navy)" id="phoneCode"></div>
            <p class="muted" style="margin:10px 0 0">Scan with the phone camera, or open<br>
            <span style="font-family:var(--mono);font-size:.8rem;word-break:break-all" id="phoneUrl"></span></p>
          </div>
        </div>
        <p class="muted" id="phonePairStatus" style="margin-top:12px">Waiting for the phone…</p>
      </div>

      <p class="muted">Pair anytime at <a href="/phone">the Phone page</a>. Calendar connect lives on the Calendar step.</p>
    </div>

    <div class="nav-btns">
      <button type="button" class="btn btn-ghost" id="backBtn">Back</button>
      <button type="button" class="btn btn-primary" id="nextBtn">Continue</button>
    </div>
    <div class="err" id="err"></div>
    <div id="doneBox" hidden></div>
  </section>
</div>

<script>
const state = {
  step: 0,
  identity: {name:"", role:"", description:"",
             primary_email:"", secondary_email:"", phone:""},
  people: [],
  projects: [],
  tools: [],
  schedule: [],
  priorities: [],
  notes: "",
};

const STEPS = ["You","Calendar","Next meeting","Capture"];

function el(tag, attrs={}, kids=[]){
  const n=document.createElement(tag);
  for(const [k,v] of Object.entries(attrs)){
    if(k==="className") n.className=v;
    else if(k.startsWith("on") && typeof v==="function") n.addEventListener(k.slice(2).toLowerCase(), v);
    else if(k==="text") n.textContent=v;
    else n.setAttribute(k,v);
  }
  for(const c of kids) n.append(c);
  return n;
}

function chipList(container, arr, onRemove){
  container.innerHTML="";
  arr.forEach((t,i)=>{
    const b=el("button",{type:"button", text:"×", onclick:()=>{arr.splice(i,1); chipList(container,arr,onRemove);}});
    container.append(el("span",{className:"chip-in"},[document.createTextNode(t), b]));
  });
}

function addTag(inputId, arr, chipsId){
  const inp=document.getElementById(inputId);
  const v=(inp.value||"").trim();
  if(!v) return;
  if(!arr.includes(v)) arr.push(v);
  inp.value="";
  chipList(document.getElementById(chipsId), arr);
}

function renderPeople(){
  const list=document.getElementById("peopleList");
  list.innerHTML="";
  if(!state.people.length) state.people.push({name:"",aliases:[],relationship:"",note:""});
  state.people.forEach((p,i)=>{
    const box=el("div",{className:"item"});
    const head=el("div",{className:"item-head"},[
      el("span",{text:"Person "+(i+1)}),
      el("button",{type:"button",className:"linkish",text:"Remove",onclick:()=>{
        state.people.splice(i,1); renderPeople();
      }})
    ]);
    box.append(head);
    const name=el("input",{placeholder:"Name", value:p.name||""});
    name.addEventListener("input",e=>p.name=e.target.value);
    const rel=el("input",{placeholder:"Relationship (e.g. teammate, manager)", value:p.relationship||""});
    rel.addEventListener("input",e=>p.relationship=e.target.value);
    const aliases=el("input",{placeholder:"Aliases, comma-separated", value:(p.aliases||[]).join(", ")});
    aliases.addEventListener("input",e=>p.aliases=e.target.value.split(",").map(s=>s.trim()).filter(Boolean));
    const note=el("input",{placeholder:"Note (optional)", value:p.note||""});
    note.addEventListener("input",e=>p.note=e.target.value);
    box.append(el("label",{text:"Name"}), name, el("label",{text:"Relationship"}), rel,
               el("label",{text:"Aliases"}), aliases, el("label",{text:"Note"}), note);
    list.append(box);
  });
}

function renderProjects(){
  const list=document.getElementById("projectsList");
  list.innerHTML="";
  if(!state.projects.length) state.projects.push({name:"",kind:"project",aliases:[],note:""});
  state.projects.forEach((p,i)=>{
    const box=el("div",{className:"item"});
    const head=el("div",{className:"item-head"},[
      el("span",{text:"Project "+(i+1)}),
      el("button",{type:"button",className:"linkish",text:"Remove",onclick:()=>{
        state.projects.splice(i,1); renderProjects();
      }})
    ]);
    box.append(head);
    const name=el("input",{placeholder:"Name", value:p.name||""});
    name.addEventListener("input",e=>p.name=e.target.value);
    const kind=el("input",{placeholder:"Kind (project / org / …)", value:p.kind||"project"});
    kind.addEventListener("input",e=>p.kind=e.target.value);
    const note=el("input",{placeholder:"Note (optional)", value:p.note||""});
    note.addEventListener("input",e=>p.note=e.target.value);
    box.append(el("label",{text:"Name"}), name, el("label",{text:"Kind"}), kind,
               el("label",{text:"Note"}), note);
    list.append(box);
  });
}

function showStep(n){
  state.step=n;
  document.getElementById("stepLabel").textContent="Step "+(n+1)+" of "+STEPS.length+" — "+STEPS[n];
  document.querySelectorAll(".step").forEach(s=>{
    s.hidden = +s.dataset.step !== n;
  });
  document.querySelectorAll("#progress i").forEach((dot,i)=>{
    dot.className = i<n ? "done" : (i===n ? "on" : "");
  });
  document.getElementById("backBtn").style.visibility = n===0 ? "hidden" : "visible";
  document.getElementById("nextBtn").textContent = n===STEPS.length-1 ? "Save to @@BRAND@@" : "Continue";
  document.getElementById("err").textContent="";
}

function collect(){
  state.identity={
    name: document.getElementById("name").value.trim(),
    role: document.getElementById("role").value.trim(),
    description: document.getElementById("description").value.trim(),
    primary_email: document.getElementById("primary_email").value.trim(),
    secondary_email: document.getElementById("secondary_email").value.trim(),
    phone: document.getElementById("phone").value.trim(),
  };
  state.notes = document.getElementById("notes").value.trim();
  return {
    identity: state.identity,
    people: state.people.filter(p=> (p.name||"").trim()),
    projects: state.projects.filter(p=> (p.name||"").trim()),
    tools: state.tools.slice(),
    schedule: state.schedule.slice(),
    priorities: state.priorities.slice(),
    notes: state.notes,
  };
}

function applyProfile(p){
  if(!p || typeof p!=="object") return;
  const id=p.identity||{};
  document.getElementById("name").value=id.name||"";
  document.getElementById("role").value=id.role||"";
  document.getElementById("description").value=id.description||"";
  document.getElementById("primary_email").value=id.primary_email||"";
  document.getElementById("secondary_email").value=id.secondary_email||"";
  document.getElementById("phone").value=id.phone||"";
  state.people=Array.isArray(p.people)&&p.people.length ? p.people.map(x=>({
    name:x.name||"", aliases:x.aliases||[], relationship:x.relationship||"", note:x.note||""
  })) : [];
  state.projects=Array.isArray(p.projects)&&p.projects.length ? p.projects.map(x=>({
    name:x.name||"", kind:x.kind||"project", aliases:x.aliases||[], note:x.note||""
  })) : [];
  state.tools=(p.tools||[]).map(t=> typeof t==="string" ? t : (t.name||"")).filter(Boolean);
  state.schedule=(p.schedule||[]).filter(Boolean);
  state.priorities=(p.priorities||[]).filter(Boolean);
  document.getElementById("notes").value=p.notes||"";
  renderPeople(); renderProjects();
  chipList(document.getElementById("toolsChips"), state.tools);
  chipList(document.getElementById("schedChips"), state.schedule);
  chipList(document.getElementById("prioChips"), state.priorities);
}

// --- system enrichment: add machine context to memory (does NOT touch this
// form — the answers below stay entirely user-authored). Observed/reversible.
async function runEnrich(){
  const s=document.getElementById("scanStatus"), b=document.getElementById("scanBtn");
  s.textContent="Learning about your work…"; b.disabled=true;
  const include=[];
  if(document.getElementById("bmCheck") && document.getElementById("bmCheck").checked) include.push("bookmarks");
  try{
    const j=await (await fetch("/onboarding/enrich",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({include})})).json();
    if(j.ok){
      const bits=[];
      if(j.projects) bits.push(j.projects+" project"+(j.projects>1?"s":""));
      if(j.tools) bits.push(j.tools+" tool"+(j.tools>1?"s":""));
      if(j.identity) bits.push("your git identity");
      if(bits.length) s.textContent="✓ Added "+bits.join(", ")+" to @@BRAND@@'s memory as background context. Now tell it about yourself below.";
      else if(j.skipped) s.textContent="Already up to date — nothing new to add. Fill in your details below.";
      else s.textContent="Didn't find much to add — just fill in your details below.";
    } else { s.textContent=j.error||"Enrichment unavailable — fill in your details below."; }
  }catch(e){ s.textContent="Couldn't reach @@BRAND@@ — fill in your details below."; }
  b.disabled=false;
}
document.getElementById("scanBtn").onclick=runEnrich;

// --- documents: content-level ingestion, gated behind an explicit checkbox.
async function runDocs(){
  const s=document.getElementById("docsStatus"), b=document.getElementById("docsBtn");
  s.textContent="Reading your documents… this can take a minute."; b.disabled=true;
  try{
    const j=await (await fetch("/onboarding/documents",{method:"POST"})).json();
    if(j.ok){
      if(j.documents){
        let msg="✓ Read "+j.documents+" document"+(j.documents>1?"s":"")+
                " and added "+j.facts+" fact"+(j.facts===1?"":"s")+
                " to @@BRAND@@'s memory. Review them anytime in Memory.";
        if(j.skipped) msg+=" ("+j.skipped+" unchanged, skipped.)";
        s.textContent=msg;
      } else if(j.skipped){ s.textContent="Already up to date — no changed documents to read."; }
      else { s.textContent="Didn't find readable documents to add."; }
    } else { s.textContent=j.error||"Document reading is unavailable."; }
  }catch(e){ s.textContent="Couldn't reach @@BRAND@@ to read documents."; }
}
document.getElementById("docsBtn").onclick=runDocs;
document.getElementById("docsCheck").onchange=function(){
  document.getElementById("docsBtn").disabled=!this.checked;
};

async function boot(){
  try{
    const st=await (await fetch("/onboarding/status")).json();
    const line=document.getElementById("statusLine");
    // Show the auto-fill affordance only when the server allows scanning.
    try{
      const sc=await (await fetch("/onboarding/scan-available")).json();
      if(sc && sc.available){
        document.getElementById("scanBox").hidden=false;
        if((sc.optional||[]).includes("bookmarks"))
          document.getElementById("bmConsent").hidden=false;
      }
    }catch(e){}
    // Documents box: only when the server allows content ingestion.
    try{
      const dc=await (await fetch("/onboarding/documents-available")).json();
      if(dc && dc.available){
        document.getElementById("docsBox").hidden=false;
        const roots=(dc.roots||[]).map(p=>p.split(/[\\/]/).pop()).filter(Boolean);
        if(roots.length) document.getElementById("docsRoots").textContent=
          "Folders it will read: "+roots.join(", ")+".";
      }
    }catch(e){}
    if(st.completed){
      line.innerHTML='You already finished setup. You can <a href="#" id="reopen">update answers</a> or go to <a href="/today">Today</a>.';
      document.getElementById("reopen")?.addEventListener("click",e=>{e.preventDefault(); openWizard();});
    }else{
      line.textContent="Takes a couple of minutes. Everything is optional.";
    }
    const pr=await (await fetch("/onboarding/profile")).json();
    if(pr.ok && pr.profile) applyProfile(pr.profile);
    else { renderPeople(); renderProjects(); }
  }catch(e){
    document.getElementById("statusLine").textContent="Could not load status — you can still fill the form.";
    renderPeople(); renderProjects();
  }
}

function openWizard(startStep){
  document.getElementById("hero").style.display="none";
  document.getElementById("wizard").classList.add("on");
  const n = (typeof startStep === "number") ? startStep : stepFromUrl();
  showStep(Math.max(0, Math.min(STEPS.length - 1, n)));
}
function stepFromUrl(){
  try{
    const qp = new URLSearchParams(location.search);
    const raw = (qp.get("step") || location.hash.replace(/^#/, "") || "").toLowerCase();
    if(!raw) return 0;
    if(/^\d+$/.test(raw)) return +raw;
    const ix = STEPS.findIndex(s => s.toLowerCase() === raw);
    return ix >= 0 ? ix : 0;
  }catch(e){ return 0; }
}

async function save(){
  const err=document.getElementById("err");
  const btn=document.getElementById("nextBtn");
  err.textContent="";
  btn.disabled=true;
  try{
    const profile=collect();
    const r=await fetch("/onboarding/ingest",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({profile})
    });
    const j=await r.json();
    if(!j.ok){ err.textContent=j.error||"Save failed"; btn.disabled=false; return; }
    const box=document.getElementById("doneBox");
    box.hidden=false;
    box.innerHTML=`<div class="ok-banner">
      <h3>You're in.</h3>
      <p class="muted">Saved ${j.claims||0} notes, ${j.people||0} people, ${j.entities||0} projects/tools
      ${j.skipped?` · ${j.skipped} already known`:""}.</p>
      <div class="done-links">
        <a class="btn btn-primary" href="/meetings">Open Meetings</a>
        <a class="btn btn-ghost" href="/today">Today</a>
        <a class="btn btn-ghost" href="/memory">Memory</a>
      </div>
      <p class="muted" style="margin-top:14px">Optional: point Claude Desktop at this machine as a read-only memory server.
      Copy the config from <a href="/help/mcp">docs/mcp.md</a> after you enable <span style="font-family:var(--mono)">QUILL_MCP=1</span>.</p>
    </div>`;
    document.getElementById("backBtn").hidden=true;
    btn.hidden=true;
  }catch(e){
    err.textContent="Network error — is @@BRAND@@ running?";
    btn.disabled=false;
  }
}

// --- optional phone pairing (step 5) — live, independent of the profile save.
let phoneBaseline=null, phonePoll=null;
async function phoneStatus(){
  try{ return await (await fetch("/phone/status")).json(); }catch(e){ return null; }
}
async function startPhonePair(){
  const st=await phoneStatus();
  if(st && st.localhost_only){
    document.getElementById("phoneWarn").innerHTML=
      '<p class="muted" style="color:var(--warn)">'+st.hint+'</p>';
  }
  let r;
  try{ r=await (await fetch("/phone/pair/start",{method:"POST"})).json(); }
  catch(e){ r=null; }
  if(!r || !r.ok){
    document.getElementById("phonePairStatus").textContent=(r&&r.error)||"Could not start pairing.";
    document.getElementById("phonePairBox").hidden=false; return;
  }
  phoneBaseline = st ? st.devices.length : 0;
  document.getElementById("phonePairBox").hidden=false;
  document.getElementById("phoneCode").textContent=r.code;
  document.getElementById("phoneUrl").textContent=r.setup_url;
  document.getElementById("phoneQr").innerHTML =
    r.qr_svg || '<span class="muted">QR unavailable — use the link.</span>';
  document.getElementById("phonePairBtn").textContent="New pairing code";
  document.getElementById("phonePairStatus").textContent="Waiting for the phone…";
  if(phonePoll) clearInterval(phonePoll);
  phonePoll=setInterval(async ()=>{
    if(document.hidden) return;
    const s=await phoneStatus();
    if(s && phoneBaseline!==null && s.devices.length>phoneBaseline){
      const d=s.devices[s.devices.length-1];
      document.getElementById("phonePairStatus").innerHTML=
        '<span style="color:var(--ok);font-weight:600">✓ '+(d?d.name:"Device")+" connected.</span> Finish the shortcut steps on the phone, then continue.";
      clearInterval(phonePoll); phonePoll=null;
    }
  },3000);
}
document.getElementById("phonePairBtn").onclick=startPhonePair;

async function icRefreshOb(){
  try{
    const s=await (await fetch("/icloud/status")).json();
    document.getElementById("icStatusOb").innerHTML = s.connected
      ? '<span style="color:var(--ok);font-weight:600">✓ iCloud connected as '+s.user+'</span>'
      : "";
    document.getElementById("icFormOb").hidden = s.connected;
    document.getElementById("icBtnOb").hidden = s.connected;
  }catch(e){}
}
document.getElementById("icBtnOb").onclick = async () => {
  const msg=document.getElementById("icMsgOb"), btn=document.getElementById("icBtnOb");
  msg.textContent="Checking with Apple…"; btn.disabled=true;
  try{
    const r=await fetch("/icloud/connect",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({user:document.getElementById("icUserOb").value,
                           app_password:document.getElementById("icPassOb").value})});
    const j=await r.json();
    if(r.ok && j.ok){ msg.textContent=""; document.getElementById("icPassOb").value=""; icRefreshOb(); }
    else msg.textContent=j.detail||"Could not connect.";
  }catch(e){ msg.textContent="Network error — is @@BRAND@@ running?"; }
  btn.disabled=false;
};
icRefreshOb();

document.getElementById("startBtn").onclick=()=>openWizard();
document.getElementById("addPerson").onclick=()=>{state.people.push({name:"",aliases:[],relationship:"",note:""}); renderPeople();};
document.getElementById("addProject").onclick=()=>{state.projects.push({name:"",kind:"project",aliases:[],note:""}); renderProjects();};
document.getElementById("addTool").onclick=()=>addTag("toolInput", state.tools, "toolsChips");
document.getElementById("addSched").onclick=()=>addTag("schedInput", state.schedule, "schedChips");
document.getElementById("addPrio").onclick=()=>addTag("prioInput", state.priorities, "prioChips");
document.getElementById("toolInput").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addTag("toolInput",state.tools,"toolsChips");}});
document.getElementById("schedInput").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addTag("schedInput",state.schedule,"schedChips");}});
document.getElementById("prioInput").addEventListener("keydown",e=>{if(e.key==="Enter"){e.preventDefault();addTag("prioInput",state.priorities,"prioChips");}});
document.getElementById("backBtn").onclick=()=>showStep(Math.max(0,state.step-1));
document.getElementById("nextBtn").onclick=async ()=>{
  if(state.step===2){
    try{
      await fetch("/first-run/meeting-listen",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({consent:!!document.getElementById("meetListen").checked})});
    }catch(e){}
  }
  if(state.step===3 || state.step===STEPS.length-1){
    try{
      await fetch("/first-run/ambient",{method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          mic:!!document.getElementById("ambMic").checked,
          webcam:!!document.getElementById("ambCam").checked,
          desktop:!!document.getElementById("ambDesk").checked
        })});
    }catch(e){}
  }
  if(state.step===STEPS.length-1) save();
  else showStep(state.step+1);
};

const PROVIDER_HINTS={anthropic:"sk-ant-…",openai:"sk-…",google:"AIza…",xai:"xai-…"};
document.getElementById("provider").onchange=()=>{
  const p=document.getElementById("provider").value;
  document.getElementById("apikey").placeholder=PROVIDER_HINTS[p]||"";
};
(async ()=>{ // reflect an already-connected provider when revisiting setup
  try{
    const s=await (await fetch("/onboarding/parent-model")).json();
    const sel=document.getElementById("provider");
    sel.value=s.active||"anthropic";
    sel.onchange();
    const on=(s.providers||[]).find(p=>p.id===s.active&&p.connected);
    if(on) document.getElementById("keyMsg").textContent=on.label+" is connected.";
  }catch(e){}
})();
(async ()=>{ // offer the invite path only when this build has a service
  try{
    const s=await (await fetch("/onboarding/invite")).json();
    if(s.configured) document.getElementById("inviteBox").hidden=false;
  }catch(e){}
})();
const inviteBtn=document.getElementById("inviteBtn");
if(inviteBtn) inviteBtn.onclick=async ()=>{
  const msg=document.getElementById("inviteMsg"), el=document.getElementById("invitecode");
  msg.textContent="Checking your code…"; inviteBtn.disabled=true;
  try{
    const r=await fetch("/onboarding/invite",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({code:el.value})});
    const j=await r.json();
    if(r.ok){
      // Same end state as pasting a key: the connected-provider line below is
      // what actually confirms it, so refresh that rather than claim success here.
      msg.textContent="Connected"+(j.label?(" for "+j.label):"")+".";
      el.value="";
      try{
        const s=await (await fetch("/onboarding/parent-model")).json();
        const on=(s.providers||[]).find(p=>p.id===s.active&&p.connected);
        if(on) document.getElementById("keyMsg").textContent=on.label+" is connected.";
      }catch(e){}
    } else {
      msg.textContent=j.detail||"That code was not accepted.";
    }
  }catch(e){ msg.textContent="Could not reach @@BRAND@@."; }
  inviteBtn.disabled=false;
};
document.getElementById("keyBtn").onclick=async ()=>{
  const msg=document.getElementById("keyMsg"), btn=document.getElementById("keyBtn");
  msg.textContent="Testing key…"; btn.disabled=true;
  try{
    const r=await fetch("/onboarding/api-key",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({key:document.getElementById("apikey").value,
        provider:document.getElementById("provider").value})});
    const j=await r.json();
    msg.textContent = r.ok ? "Key saved on this machine." : (j.detail||"Key rejected");
  }catch(e){ msg.textContent="Could not reach @@BRAND@@."; }
  btn.disabled=false;
};

async function refreshExhaust(){
  try{
    const s=await (await fetch("/exhaust/status")).json();
    const el=document.getElementById("exStatus");
    if(!s.oauth_configured){
      el.textContent="Google OAuth client is not configured on this install — use iCloud below, or skip. People can still be added later.";
      document.getElementById("exBtn").disabled=true;
      return;
    }
    el.textContent = s.connected ? "Google connected. Click to import the last 90 days of headers + calendar attendees." : "Not connected yet.";
    const p=s.progress||{};
    if(p.running) document.getElementById("exProg").textContent=
      "Scanning… "+(p.contacts||0)+" contacts, "+(p.events||0)+" events.";
  }catch(e){}
}
document.getElementById("exBtn").onclick=async ()=>{
  const msg=document.getElementById("exMsg"), btn=document.getElementById("exBtn");
  msg.textContent="A browser window will open for Google consent (metadata only).";
  btn.disabled=true;
  try{
    const st=await (await fetch("/exhaust/status")).json();
    if(!st.connected){
      const r=await (await fetch("/exhaust/connect",{method:"POST"})).json();
      if(!r.ok){ msg.textContent=r.error||"Could not connect"; btn.disabled=false; return; }
    }
    await fetch("/exhaust/refresh",{method:"POST"});
    const poll=setInterval(async ()=>{
      if(document.hidden) return;
      const s=await (await fetch("/exhaust/status")).json();
      const p=s.progress||{};
      document.getElementById("exProg").textContent=
        (p.running?"Scanning… ":"Done. ")+(p.contacts||0)+" contacts, "+(p.events||0)+" events.";
      if(!p.running){ clearInterval(poll); btn.disabled=false; loadNextMeeting(); }
    },1500);
  }catch(e){ msg.textContent="Network error"; btn.disabled=false; }
};

async function loadNextMeeting(){
  try{
    const s=await (await fetch("/first-run/status")).json();
    const n=s.next_meeting;
    if(!n){ document.getElementById("nextMeetLead").textContent="No upcoming meeting yet — connect a calendar, or skip and add one later."; return; }
    document.getElementById("nextMeetBox").hidden=false;
    document.getElementById("nextMeetTitle").textContent=n.title||"Meeting";
    const when=n.start ? new Date(n.start*1000).toLocaleString() : "";
    document.getElementById("nextMeetWhen").textContent=when;
  }catch(e){}
}
refreshExhaust();
loadNextMeeting();
boot().then(()=>{
  try{
    const qp=new URLSearchParams(location.search);
    const h=location.hash.replace(/^#/,'');
    if(qp.get('step') || (h && STEPS.some(s=>s.toLowerCase()===h.toLowerCase()))){
      openWizard();
    }
  }catch(e){}
});
</script>
</body>
</html>""")
