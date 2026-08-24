"""Chat UI — live agent conversation surface."""

from app.api.mnemos_theme import apply as _mnemos

CHAT_PAGE = _mnemos(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — Chat</title><meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
@@KATEX@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;font:15px/1.55 var(--font);color:var(--text);
  height:100vh;display:flex;flex-direction:column;
  background:
    radial-gradient(900px 480px at 8% -5%, var(--acc-05), transparent 55%),
    radial-gradient(700px 400px at 96% 0%, rgba(30,91,79,.04), transparent 50%),
    linear-gradient(180deg, #FBF9F4 0%, var(--paper) 40%, var(--workspace) 100%);
}
.chat-layout{
  flex:1;min-height:0;display:grid;
  grid-template-columns:minmax(0,min(200px,18vw)) minmax(0,1fr);
}
#ambientChat{
  padding:14px 12px;overflow:auto;min-width:0;border-right:1px solid var(--line);
}
@media(max-width:900px){
  .chat-layout{grid-template-columns:1fr}
  #ambientChat{display:none}
}
.top{
  display:flex;align-items:center;gap:18px;flex-wrap:wrap;
  padding:14px 22px;
}
.page-sub{margin-left:-4px}
.meta{display:flex;gap:14px;align-items:center;font-family:var(--mono);font-size:12px;color:var(--mut);min-width:0}
.meta #url{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-tools{display:flex;gap:8px;align-items:center;position:relative}
.chat-tools button{
  background:transparent;border:1px solid transparent;border-radius:10px;
  padding:6px 10px;font:500 12px var(--font);color:var(--mut);cursor:pointer;
  box-shadow:none;
}
.chat-tools button:hover{
  color:var(--text);border-color:rgba(11,19,32,.1);background:rgba(11,19,32,.03);
  transform:none;box-shadow:none;
}
.chat-tools button:active{transform:none}
#pastPanel{
  display:none;position:absolute;right:0;top:calc(100% + 8px);z-index:var(--z-popover);
  width:min(340px,calc(100vw - 24px));max-height:min(360px,calc(100dvh - var(--chrome-h) - 24px));overflow:auto;
  background:var(--surface);border:1px solid rgba(11,19,32,.1);border-radius:14px;
  box-shadow:var(--shadow-float);padding:8px;animation:fadeUp .22s var(--ease) both;
}
#pastPanel.open{display:block}
#pastPanel .past-head{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  padding:6px 8px 8px;font:11px/1.2 var(--mono);color:var(--mut);
  text-transform:uppercase;letter-spacing:.06em;
}
#pastPanel .past-empty{padding:14px 10px;color:var(--mut);font:13px var(--font)}
.past-item{
  display:block;width:100%;text-align:left;border:none;background:transparent;
  border-radius:10px;padding:10px 10px;cursor:pointer;color:var(--text);
  font:13px/1.35 var(--font);box-shadow:none;
}
.past-item:hover{background:var(--acc-08);transform:none;box-shadow:none}
.past-item .past-title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.past-item .past-meta{display:block;margin-top:3px;font:11px var(--mono);color:var(--mut)}
#archiveBanner{
  display:none;width:min(640px,94%);margin:8px auto 0;padding:10px 14px;
  background:rgba(30,91,79,.06);border:1px solid rgba(30,91,79,.18);
  border-radius:12px;font:13px var(--font);color:var(--navy);
  align-items:center;justify-content:space-between;gap:12px;
}
#archiveBanner.show{display:flex}
#archiveBanner button{
  background:transparent;border:1px solid rgba(11,19,32,.12);border-radius:8px;
  padding:5px 10px;font:500 12px var(--font);color:var(--navy);cursor:pointer;
  box-shadow:none;flex:0 0 auto;
}
#archiveBanner button:hover{background:rgba(11,19,32,.04);transform:none;box-shadow:none}
#log{
  flex:1;overflow:auto;padding:32px 20px 40px;
  display:flex;flex-direction:column;gap:4px;align-items:center;min-width:0;min-height:0;
}
.chat-main{display:flex;flex-direction:column;min-width:0;min-height:0}
.msg{
  max-width:min(640px,94%);width:100%;
  animation:fadeUp .32s var(--ease) both;
  position:relative;
}
.msg-label{
  font:500 10px/1 var(--mono);letter-spacing:.08em;text-transform:uppercase;
  color:var(--mut);margin:0 0 6px;padding:0 2px;
}
.msg-body{
  white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;
  font:15px/1.65 var(--font);color:var(--text);letter-spacing:-.01em;
}
/* You — quiet ink note, not a navy brick */
.msg.user{
  align-self:stretch;max-width:min(640px,94%);
  margin:14px 0 6px;display:flex;flex-direction:column;align-items:flex-end;
}
.msg.user .msg-label{align-self:flex-end;color:rgba(11,19,32,.45)}
.msg.user .msg-body{
  max-width:min(420px,88%);
  background:transparent;color:var(--navy);font-weight:500;
  padding:0 0 10px;border-bottom:1.5px solid var(--acc-35);
  text-align:right;box-shadow:none;border-radius:0;
}
/* Mnemos — paper page of a reply */
.msg.result{
  margin:18px 0 8px;padding:0;
}
.msg.result .msg-shell{
  background:var(--surface);border:1px solid rgba(11,19,32,.07);
  border-radius:16px;padding:16px 18px 14px;
  box-shadow:var(--shadow-surface);
  position:relative;
}
.msg.result .msg-shell::before{
  content:"";position:absolute;left:0;top:14px;bottom:14px;width:2px;
  background:linear-gradient(180deg,var(--acc-55),var(--acc-08));
  border-radius:2px;
}
.msg.result .msg-label{color:var(--navy);opacity:.55}
.msg.result .msg-body{padding-left:8px}
.msg.result .msg-body.rd-host{padding-left:0;white-space:normal}
.msg.result .msg-shell{padding:18px 20px 16px}
.sources{
  margin:10px 0 0 8px;padding-top:8px;border-top:1px solid rgba(11,19,32,.06);
  font-size:12px;color:var(--mut);line-height:1.55;
}
.sources summary{
  cursor:pointer;user-select:none;list-style:none;
  font:500 11px/1.2 var(--mono);letter-spacing:.04em;text-transform:uppercase;
}
.sources summary::-webkit-details-marker{display:none}
.sources summary:hover{color:var(--navy)}
.sources div{margin:4px 0 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.verdict{
  display:flex;gap:6px;margin:12px 0 0 8px;padding-top:10px;
  border-top:1px solid rgba(11,19,32,.06);
}
.verdict button{
  background:transparent;border:1px solid transparent;border-radius:8px;
  padding:4px 10px;font-size:13px;line-height:1.2;cursor:pointer;color:var(--mut);
  font-family:var(--font);
}
.verdict button:hover:not(:disabled){
  border-color:rgba(11,19,32,.1);color:var(--text);background:rgba(11,19,32,.03);
}
.verdict button.on{border-color:rgba(46,111,87,.35);color:var(--ok);background:rgba(46,111,87,.06)}
.verdict button.bad.on{border-color:rgba(166,71,71,.35);color:var(--danger);background:rgba(166,71,71,.06)}
.verdict button:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
.msg.system{
  align-self:center;max-width:min(480px,90%);margin:10px 0;
  text-align:center;background:transparent;padding:0;box-shadow:none;
}
.msg.system .msg-body{
  font:italic 13px/1.5 var(--font);color:var(--mut);text-align:center;
}
.msg.ask{
  margin:14px 0 8px;
}
.msg.ask:not(.folio-wrap) .msg-shell{
  background:rgba(255,254,251,.9);border:1px solid rgba(199,138,44,.22);
  border-radius:16px;padding:14px 16px;
  box-shadow:var(--shadow-workspace);
}
.msg.ask .msg-label{color:var(--warn)}
.msg.ask.folio-wrap{background:transparent;border:none;box-shadow:none;padding:0;margin:18px 0}
.msg.error{margin:12px 0}
.msg.error .msg-shell{
  background:rgba(166,71,71,.05);border:1px solid rgba(166,71,71,.18);
  border-radius:14px;padding:12px 14px;
}
.msg.error .msg-label{color:var(--danger)}
.msg.error .msg-body{color:#6b3030;font-size:14px}
.msg.progress{
  margin:2px 0;max-width:min(640px,94%);
}
.msg.progress .msg-body{
  font:12px/1.5 var(--mono);color:rgba(107,111,118,.85);
  padding:3px 0 3px 14px;border-left:1.5px solid rgba(11,19,32,.1);
  background:transparent;box-shadow:none;
}
.dock{
  border-top:1px solid rgba(11,19,32,.06);background:rgba(248,246,241,.96);backdrop-filter:blur(14px);
  padding:0 18px 18px;display:flex;flex-direction:column;align-items:center;
}
#bar{
  display:none;gap:10px;align-items:center;justify-content:flex-start;flex-wrap:wrap;
  width:min(640px,100%);margin:12px 0 4px;padding:10px 14px;
  background:var(--surface);border:1px solid rgba(11,19,32,.08);border-radius:14px;
  box-shadow:var(--shadow-workspace);animation:fadeUp .3s var(--ease) both;
}
#bar .action-detail{flex:1 1 100%;order:5;margin:4px 0 0}
#bar .approval-form{order:6}
#waiting{
  flex:1;min-width:0;font-size:12.5px;color:var(--warn);line-height:1.35;
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
}
#bar button{
  flex:0 0 auto;border-radius:10px;padding:8px 18px;cursor:pointer;font:500 14px var(--font);
  border:1px solid var(--line);background:var(--bg-elev);color:var(--text);
}
#bar .yes{border-color:rgba(46,111,87,.4);color:var(--ok)}
#bar .yes:hover{
  background:rgba(46,111,87,.1);border-color:rgba(46,111,87,.55);
  box-shadow:0 4px 14px rgba(46,111,87,.12);
}
#bar .no{border-color:rgba(166,71,71,.4);color:var(--danger)}
#bar .no:hover{
  background:rgba(166,71,71,.1);border-color:rgba(166,71,71,.55);
  box-shadow:0 4px 14px rgba(166,71,71,.12);
}
.composer-wrap{
  display:flex;flex-direction:column;gap:8px;width:min(640px,100%);padding-top:12px;
}
.composer{
  display:flex;gap:10px;align-items:stretch;width:100%;
}
#box{
  flex:1;background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:var(--radius);padding:12px 14px;resize:none;min-height:52px;height:52px;
  font:inherit;line-height:1.45;box-shadow:var(--shadow);
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
#box:focus{outline:none;border-color:var(--acc-45);box-shadow:0 0 0 3px var(--acc-dim)}
#dry,#studyMode{
  background:var(--panel);color:var(--mut);border:1px solid var(--line);
  border-radius:var(--radius);padding:0 10px;min-width:132px;font:13px var(--font);
  cursor:pointer;
  transition:border-color .28s var(--ease),color .28s var(--ease),
    background .28s var(--ease),box-shadow .28s var(--ease),transform .22s var(--ease);
}
#studyMode{min-width:148px}
#dry:hover,#studyMode:hover{
  color:var(--text);border-color:var(--acc-40);
  background:var(--bg-elev);transform:translateY(-1px);
  box-shadow:0 4px 12px rgba(11,19,32,.06);
}
#dry:focus,#studyMode:focus{color:var(--text);outline:none;border-color:var(--acc-40)}
#ctxBtn{
  background:var(--panel);color:var(--mut);border:1px solid var(--line);
  border-radius:var(--radius);padding:0 12px;min-width:auto;cursor:pointer;
  font:500 13px var(--font);white-space:nowrap;
}
#ctxBtn:hover{
  color:var(--text);border-color:var(--acc-40);
  background:var(--bg-elev);
}
#ctxBtn.on{color:var(--navy);border-color:var(--acc-45);background:var(--acc-08)}
#ctxBtn.has{color:var(--ok);border-color:rgba(46,111,87,.4)}
#ctxPanel{
  display:none;width:100%;background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius);padding:10px 12px;box-shadow:var(--shadow);
  animation:fadeUp .25s var(--ease) both;
}
#ctxPanel.open{display:block}
#ctxPanel .ctx-label{
  display:flex;align-items:center;justify-content:space-between;gap:8px;
  font:12px var(--mono);color:var(--mut);margin-bottom:6px;
}
#ctxPanel .ctx-label button{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:12px var(--font);padding:0 4px;box-shadow:none;
}
#ctxPanel .ctx-label button:hover{
  color:var(--danger);transform:none;box-shadow:none;background:transparent;
}
#ctxBox{
  width:100%;box-sizing:border-box;background:var(--bg-elev);color:var(--text);
  border:1px solid var(--line);border-radius:10px;padding:10px 12px;resize:vertical;
  min-height:72px;max-height:200px;font:inherit;line-height:1.45;
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
#ctxBox:focus{outline:none;border-color:var(--acc-45);box-shadow:0 0 0 3px var(--acc-dim)}
#ctxFiles{
  display:flex;flex-wrap:wrap;gap:6px;margin-top:8px;min-height:0;
}
#ctxFiles:empty{display:none}
.ctx-file{
  display:inline-flex;align-items:center;gap:6px;max-width:100%;
  background:var(--bg-elev);border:1px solid var(--line);border-radius:10px;
  padding:5px 8px 5px 10px;font:12px var(--font);color:var(--text);
}
.ctx-file .ctx-file-name{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px;
}
.ctx-file .ctx-file-meta{color:var(--mut);font:11px var(--mono);white-space:nowrap}
.ctx-file.pending{opacity:.7}
.ctx-file.err{border-color:rgba(160,50,50,.45);color:var(--danger)}
.ctx-file.ok{border-color:rgba(46,111,87,.35)}
.ctx-file button{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:12px var(--font);padding:0 2px;box-shadow:none;line-height:1;
}
.ctx-file button:hover{
  color:var(--danger);transform:none;box-shadow:none;background:transparent;
}
#ctxAttach{
  background:transparent;border:1px dashed var(--line);border-radius:10px;
  color:var(--mut);cursor:pointer;font:12px var(--font);padding:6px 10px;
  margin-top:8px;width:100%;text-align:left;
  transition:border-color .22s var(--ease),color .22s var(--ease),background .22s var(--ease);
}
#ctxAttach:hover{
  color:var(--text);border-color:var(--acc-45);background:var(--acc-05);
  transform:none;box-shadow:none;
}
#ctxAttach:disabled{opacity:.55;cursor:wait}
#ctxLearn{
  margin-top:6px;font:11px var(--mono);color:var(--mut);line-height:1.35;
}
#send{
  background:var(--navy);color:#F8F6F1;border:none;border-radius:var(--radius);
  padding:0 22px;cursor:pointer;font-weight:600;font-size:14px;
  box-shadow:0 2px 8px rgba(11,19,32,.16);
}
#send:hover{
  background:#152033;transform:translateY(-2px);
  box-shadow:0 8px 20px rgba(11,19,32,.22);
  filter:brightness(1.06);
}
#send:active{transform:translateY(0) scale(.97);box-shadow:0 2px 6px rgba(11,19,32,.14)}
#ghost{
  position:relative;width:min(380px,calc(100vw - 48px));display:none;
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);overflow:hidden;animation:fadeUp .3s var(--ease) both;
  transition:box-shadow .32s var(--ease);
}
#ghost:hover{box-shadow:0 8px 32px rgba(11,19,32,.14)}
#ghost.ink-border{box-shadow:var(--shadow-float),inset 0 0 0 1px var(--acc-20)}
#ghost .head{
  display:flex;align-items:center;gap:8px;padding:7px 10px;
  border-bottom:1px solid var(--line);font:12px var(--mono);color:var(--mut);
}
#ghost .head .ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ghost .head button{
  background:transparent;border:1px solid var(--line);border-radius:8px;
  padding:2px 8px;font-size:11px;cursor:pointer;color:var(--mut);font-family:var(--font);
}
#ghost .head button:hover{
  color:var(--text);border-color:var(--acc-45);background:var(--acc-06);
}
#ghost img{display:block;width:100%;background:#fff}
#ghost.min img{display:none}
@media(max-width:900px){#ghost{width:min(280px,calc(100vw - 48px))}}
@media(max-width:640px){
  .top{padding:10px 14px;gap:10px}
  .meta{width:100%;flex-wrap:wrap;gap:8px;font-size:11px}
  .chat-tools{margin-left:auto}
  #pastPanel{right:-8px;width:min(340px,calc(100vw - 28px))}
  #log{padding:20px 12px 32px}
  .composer{padding:10px 12px calc(10px + env(safe-area-inset-bottom,0px))}
  .composer{flex-wrap:wrap}
  #dry,#studyMode,#send,#ctxBtn{height:44px}
  #ctxBtn{flex:0 0 auto}
  #dry,#studyMode{flex:1} #send{flex:0 0 auto}
  .msg{max-width:100%}
  .msg.user .msg-body{max-width:92%;text-align:left}
  .msg.user,.msg.user .msg-label{align-items:flex-start;align-self:flex-start}
  #ghost{display:none !important}
  #archiveBanner{width:calc(100% - 24px);flex-wrap:wrap}
}
</style></head><body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Chat</span>
  @@NAV@@
  <span class="spacer"></span>
  <div class="chat-tools">
    <button type="button" id="pastBtn" title="Browse saved conversations">Past</button>
    <button type="button" id="newChatBtn" title="Save this chat and start fresh">New</button>
    <div id="pastPanel" role="dialog" aria-modal="true" aria-label="Past conversations" aria-hidden="true">
      <div class="past-head"><span>Saved chats</span><span id="pastCount"></span></div>
      <div id="pastList"><div class="past-empty">No saved chats yet.</div></div>
    </div>
  </div>
  <div class="meta">
    <span id="url"></span>
    <span id="policy"></span>
    <span id="cost"></span>
  </div>
</header>
<div class="chat-layout">
<aside id="ambientChat" aria-hidden="true"></aside>
<div class="chat-main">
<div id="archiveBanner">
  <span id="archiveBannerText">Viewing a saved conversation (read-only).</span>
  <button type="button" id="backLiveBtn">Back to live</button>
</div>
<div id="log"></div>
</div>
</div>
@@UI_JS@@
<div id="ghost">
  <div class="head">
    <span class="ttl" id="ghostttl">Agent browser</span>
    <button id="ghostreveal" title="Bring the agent's browser window on-screen (e.g. to sign in), or park it again">reveal</button>
    <button id="ghostmin" title="Collapse">–</button>
  </div>
  <img id="ghostimg" alt="agent browser view">
</div>
<div class="dock">
  <div id="bar">
    <span id="waiting"></span>
    <details class="action-detail" id="dockDetail">
      <summary>What will happen</summary>
      <div class="detail-card">
        <p class="intent" id="dockIntent"></p>
        <ol class="steps" id="dockSteps"></ol>
        <div class="payload" id="dockPayload" hidden></div>
      </div>
    </details>
    <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
      <input type="hidden" name="accept" value="1">
      <input type="hidden" name="next" value="/chat">
      <button type="submit" class="yes">✓ Yes</button>
    </form>
    <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
      <input type="hidden" name="accept" value="0">
      <input type="hidden" name="next" value="/chat">
      <button type="submit" class="no">✕ No</button>
    </form>
  </div>
  <div class="composer-wrap">
    <div id="ctxPanel">
      <div class="ctx-label">
        <span>Extra context for the next message (notes, files, photos)</span>
        <button type="button" id="ctxClear" title="Clear context">Clear</button>
      </div>
      <textarea id="ctxBox" placeholder="Paste facts, constraints, or background the model should treat as authoritative for this turn…"></textarea>
      <div id="ctxFiles" aria-live="polite"></div>
      <button type="button" id="ctxAttach" title="Attach a document or photo — saved to memory to learn about you">+ Attach document or photo</button>
      <input type="file" id="ctxFileInput" multiple accept=".txt,.md,.markdown,.pdf,.docx,.rst,.text,.log,.jpg,.jpeg,.png,.webp,.gif,.bmp,image/*,text/plain,application/pdf" hidden>
      <div id="ctxLearn">Attachments are kept in memory (reviewable in Memory) so @@BRAND@@ can learn about you.</div>
    </div>
    <div class="composer">
      <textarea id="box" placeholder="Ask @@BRAND@@, or give the agent a task… (show a to-do list to the camera and it will offer to run it)"></textarea>
      <button type="button" id="ctxBtn" title="Add notes, documents, or photos for the next message">+ Context</button>
      <select id="studyMode" title="Study mode — how the assistant coaches this session">
        <option value="general">Mode: General</option>
        <option value="lecture_notes">Lecture notes</option>
        <option value="homework">Homework help</option>
        <option value="study_quiz">Study / quiz</option>
        <option value="syllabus">Syllabus &amp; deadlines</option>
        <option value="essay_rubric">Essay / rubric</option>
        <option value="reading">Reading / textbook</option>
      </select>
      <select id="dry" title="How far the agent may go this turn">
        <option value="">Posture: default</option>
        <option value="plan">Plan only</option>
        <option value="navigate">Navigate only</option>
        <option value="draft">Draft only</option>
        <option value="approval">Approval</option>
        <option value="full">Full (autonomous)</option>
        <option value="autonomous">Autonomous</option>
      </select>
      <button id="send" onclick="send()">Send</button>
    </div>
  </div>
</div>
<script>
let since=0, awaiting=false, todo=false, polling=false, approvalMode=false;
let lastErrShown=null; // dedup: state.error persists across polls — show once
let liveMode=true;
const log=document.getElementById('log'), box=document.getElementById('box');
function fillDockDetail(s){
  const det=document.getElementById('dockDetail');
  if(!det) return;
  const pkt=s&&s.packet;
  const fields=(pkt&&pkt.fields)||{};
  const intent=(fields.action||(pkt&&pkt.summary)||s.waiting_on||s.question||'').trim();
  document.getElementById('dockIntent').textContent=intent||'Mnemos is waiting for your decision.';
  const steps=[];
  if(fields.to) steps.push('Compose to '+fields.to);
  if(fields.subject) steps.push('Subject: '+fields.subject);
  if(fields.action&&!steps.length) steps.push(fields.action);
  if(!steps.length&&intent) steps.push(intent);
  document.getElementById('dockSteps').innerHTML=steps.map(x=>'<li>'+MnemosEsc(String(x))+'</li>').join('');
  const body=(fields.body||fields.details||'').trim();
  const payload=document.getElementById('dockPayload');
  const outbound=!!(body||fields.to||/email|message|send|post|sms|text/i.test(intent));
  if(body){ payload.hidden=false; payload.textContent=body; }
  else { payload.hidden=true; payload.textContent=''; }
  det.open=outbound;
  // Show detail whenever the bar is visible (mobile + minimized ghost).
  det.style.display=(s&&(s.awaiting||s.todo_pending))?'block':'none';
}
window.addEventListener('mnemos:approval-resolved',()=>{ try{ poll(); }catch(e){} });
window.addEventListener('mnemos:approval',()=>{ try{ poll(); }catch(e){} });
const ctxBtn=document.getElementById('ctxBtn'), ctxPanel=document.getElementById('ctxPanel'),
      ctxBox=document.getElementById('ctxBox'), ctxClear=document.getElementById('ctxClear'),
      ctxAttach=document.getElementById('ctxAttach'), ctxFileInput=document.getElementById('ctxFileInput'),
      ctxFiles=document.getElementById('ctxFiles');
const pastBtn=document.getElementById('pastBtn'), pastPanel=document.getElementById('pastPanel'),
      pastList=document.getElementById('pastList'), pastCount=document.getElementById('pastCount'),
      newChatBtn=document.getElementById('newChatBtn'),
      archiveBanner=document.getElementById('archiveBanner'),
      archiveBannerText=document.getElementById('archiveBannerText'),
      backLiveBtn=document.getElementById('backLiveBtn');
// Pending attachments for the next send: {id,name,kind,context,summary,status,error}
let pendingAttach=[];
let attachSeq=0;
MnemosMemory.set('lastRoute','/chat');
(function restoreChat(){
  const st=MnemosMemory.get('chat',{});
  if(st.dry) document.getElementById('dry').value=st.dry;
  if(st.mode) document.getElementById('studyMode').value=st.mode;
  if(st.draft) box.value=st.draft;
  if(st.ctx){ ctxBox.value=st.ctx; }
  if(st.ctxOpen){ ctxPanel.classList.add('open'); ctxBtn.classList.add('on'); }
})();
function persistChat(){
  MnemosMemory.set('chat',{
    dry:document.getElementById('dry').value||'',
    mode:document.getElementById('studyMode').value||'general',
    draft:box.value||'',
    ctx:ctxBox.value||'',
    ctxOpen:ctxPanel.classList.contains('open')
  });
}
function fmtWhen(iso){
  if(!iso) return '';
  try{
    const d=new Date(iso); if(isNaN(d)) return iso;
    return d.toLocaleString([], {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  }catch(e){ return iso; }
}
function setLiveMode(on){
  liveMode=!!on;
  archiveBanner.classList.toggle('show', !liveMode);
  box.disabled=!liveMode;
  document.getElementById('send').disabled=!liveMode;
  if(liveMode) archiveBannerText.textContent='Viewing a saved conversation (read-only).';
}
async function refreshPast(){
  try{
    const r=await fetch('/chat/sessions?limit=40'); const j=await r.json();
    const sessions=(j&&j.sessions)||[];
    pastCount.textContent=sessions.length?String(sessions.length):'';
    if(!sessions.length){
      pastList.innerHTML='<div class="past-empty">No saved chats yet. Hit New after a conversation to archive it.</div>';
      return;
    }
    pastList.innerHTML='';
    for(const s of sessions){
      const b=document.createElement('button');
      b.type='button'; b.className='past-item';
      b.innerHTML='<span class="past-title"></span><span class="past-meta"></span>';
      b.querySelector('.past-title').textContent=s.title||'Untitled chat';
      b.querySelector('.past-meta').textContent=
        fmtWhen(s.saved_at)+(s.n_turns!=null?(' · '+s.n_turns+' turn'+(s.n_turns===1?'':'s')):'');
      b.onclick=()=>openPast(s.id, s.title||'Untitled chat');
      pastList.appendChild(b);
    }
  }catch(e){
    pastList.innerHTML='<div class="past-empty">Could not load saved chats.</div>';
  }
}
async function openPast(id, title){
  closePastPanel();
  try{
    const r=await fetch('/chat/sessions/'+encodeURIComponent(id));
    const j=await r.json();
    if(!r.ok||!j.session){ alert((j&&j.detail)||'Could not open saved chat'); return; }
    setLiveMode(false);
    archiveBannerText.textContent='Viewing “'+(title||j.session.title||'saved chat')+'” (read-only).';
    log.innerHTML='';
    for(const e of (j.session.events||[])){
      add(e.kind, e.text, e.distill_id, e.sources, e.packet, null);
    }
    if(!(j.session.events||[]).length){
      add('system','(empty saved chat)');
    }
  }catch(e){ alert('Could not open saved chat'); }
}
async function backToLive(){
  setLiveMode(true);
  log.innerHTML='';
  since=0;
  await poll();
}
async function newChat(){
  if(!liveMode){
    await backToLive();
  }
  if(!confirm('Start a new conversation? The current chat will be saved if it has messages.')) return;
  closePastPanel();
  try{
    const r=await fetch('/chat/new',{method:'POST'});
    const j=await r.json().catch(()=>({}));
    if(!r.ok||j.ok===false){ alert(j.error||j.detail||'Could not start a new chat'); return; }
    setLiveMode(true);
    log.innerHTML='';
    since=0;
    await poll();
    refreshPast();
  }catch(e){ alert('Could not start a new chat'); }
}
let _pastReturn=null;
function closePastPanel(){
  MnemosDialog.close(pastPanel);
}
pastBtn.onclick=(ev)=>{
  ev.stopPropagation();
  if(MnemosDialog.isOpen(pastPanel)){
    closePastPanel();
  }else{
    const rect=pastBtn.getBoundingClientRect();
    const panelW=Math.min(340, window.innerWidth-24);
    if(rect.right+panelW>window.innerWidth-12){
      pastPanel.style.right='0';
      pastPanel.style.left='auto';
    }else{
      pastPanel.style.left='0';
      pastPanel.style.right='auto';
    }
    MnemosDialog.open(pastPanel,{
      onEscape:closePastPanel,
      focus:'button',
    });
    refreshPast();
  }
};
newChatBtn.onclick=()=>newChat();
backLiveBtn.onclick=()=>backToLive();
document.addEventListener('click',(ev)=>{
  if(!MnemosDialog.isOpen(pastPanel)) return;
  if(pastPanel.contains(ev.target)||pastBtn.contains(ev.target)) return;
  closePastPanel();
});
box.addEventListener('input', persistChat);
document.getElementById('dry').addEventListener('change', persistChat);
async function setStudyMode(id){
  const mode=id||document.getElementById('studyMode').value||'general';
  document.getElementById('studyMode').value=mode;
  persistChat();
  try{
    await fetch('/chat/mode',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode})});
  }catch(e){}
}
document.getElementById('studyMode').addEventListener('change',()=>setStudyMode());
(async function syncStudyMode(){
  try{
    const r=await fetch('/chat/mode'); const j=await r.json();
    if(j&&j.id){ document.getElementById('studyMode').value=j.id; persistChat(); }
  }catch(e){}
})();
function renderAttach(){
  ctxFiles.innerHTML='';
  for(const a of pendingAttach){
    const el=document.createElement('div');
    el.className='ctx-file '+(a.status||'');
    const kind=a.kind==='photo'?'photo':(a.kind==='document'?'doc':'file');
    const meta=a.status==='pending'?'uploading…'
      :(a.status==='err'?(a.error||'failed')
      :(a.facts_pending?'saved · mining facts…'
      :(a.facts!=null?('saved · '+(a.facts||0)+' facts'):'saved')));
    el.innerHTML='<span class="ctx-file-name" title="'+(a.name||'')+'">'
      +kind+' · '+(a.name||'file')+'</span>'
      +'<span class="ctx-file-meta">'+meta+'</span>';
    if(a.status!=='pending'){
      const rm=document.createElement('button');rm.type='button';rm.title='Remove from this message';
      rm.textContent='×';rm.onclick=()=>{pendingAttach=pendingAttach.filter(x=>x.id!==a.id);renderAttach();syncCtxBtn();};
      el.appendChild(rm);
    }
    ctxFiles.appendChild(el);
  }
}
function syncCtxBtn(){
  const nOk=pendingAttach.filter(a=>a.status==='ok').length;
  const nPend=pendingAttach.filter(a=>a.status==='pending').length;
  const has=!!(ctxBox.value||'').trim() || nOk>0 || nPend>0;
  ctxBtn.classList.toggle('has', has && !ctxPanel.classList.contains('open'));
  let label='+ Context';
  if(has){
    const bits=[];
    if((ctxBox.value||'').trim()) bits.push('notes');
    if(nOk||nPend) bits.push((nOk+nPend)+' file'+(nOk+nPend===1?'':'s'));
    label='Context ✓'+(bits.length?' · '+bits.join(' + '):'');
  }
  ctxBtn.textContent=label;
  persistChat();
}
ctxBtn.onclick=()=>{
  ctxPanel.classList.toggle('open');
  ctxBtn.classList.toggle('on', ctxPanel.classList.contains('open'));
  if(ctxPanel.classList.contains('open')) ctxBox.focus();
  syncCtxBtn();
};
ctxClear.onclick=()=>{
  ctxBox.value=''; pendingAttach=[]; renderAttach(); syncCtxBtn();
};
ctxBox.addEventListener('input', syncCtxBtn);
ctxAttach.onclick=()=>ctxFileInput.click();
ctxFileInput.addEventListener('change', async()=>{
  const files=[...ctxFileInput.files||[]];
  ctxFileInput.value='';
  if(!files.length) return;
  ctxPanel.classList.add('open'); ctxBtn.classList.add('on');
  for(const f of files){
    const id=++attachSeq;
    const row={id,name:f.name,kind:'',context:'',summary:'',status:'pending',facts:null,facts_pending:false,error:''};
    pendingAttach.push(row); renderAttach(); syncCtxBtn();
    ctxAttach.disabled=true;
    try{
      const fd=new FormData(); fd.append('file', f, f.name);
      const r=await fetch('/chat/attach',{method:'POST',body:fd});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){
        row.status='err'; row.error=j.detail||('upload failed ('+r.status+')');
      }else{
        row.status='ok'; row.kind=j.kind||''; row.context=j.context||'';
        row.summary=j.summary||''; row.facts=j.facts||0;
        row.facts_pending=!!j.facts_pending; row.path=j.path||'';
      }
    }catch(e){
      row.status='err'; row.error=String(e.message||e);
    }
    renderAttach(); syncCtxBtn();
  }
  ctxAttach.disabled=false;
});
function bindFolioSeal(root){
  const approve=root.querySelector('.seal-approve');
  const cancel=root.querySelector('.seal-cancel');
  if(!approve) return;
  const row=root.querySelector('.seal-row')||root;
  const packetId=row.getAttribute('data-packet-id')||'';
  const payloadHash=row.getAttribute('data-payload-hash')||'';
  async function decide(decision, extra){
    if(!packetId||!payloadHash){
      // Legacy folio without bind metadata — fall back to typed reply.
      reply(decision==='cancel'?'cancel':(extra&&extra.user_edit)||'approve');
      return;
    }
    const body=Object.assign({
      payload_hash:payloadHash,
      decision:decision,
      approved_via:'button',
    }, extra||{});
    try{
      const r=await fetch('/approval/'+encodeURIComponent(packetId)+'/decide',{
        method:'POST',
        headers:{'Content-Type':'application/json','Accept':'application/json'},
        body:JSON.stringify(body),
      });
      const j=await r.json().catch(()=>({}));
      if(!r.ok||j.ok===false){
        add('system', (j&&j.error)||('Approval refused ('+r.status+')'));
        return;
      }
      try{ window.dispatchEvent(new CustomEvent('mnemos:approval-resolved',{detail:j})); }catch(e){}
    }catch(e){
      add('system','Approval request failed: '+String(e.message||e));
    }
  }
  MnemosSeal.bind(approve,{
    onApprove:()=>{
      const promo=root.querySelector('[data-app-promotion]');
      if(promo){
        const fields={
          remember_app:!!(root.querySelector('#rememberApp')&&root.querySelector('#rememberApp').checked),
          app_template:(root.querySelector('#appTemplate')||{}).value||'text_notes',
        };
        decide('approve',{fields:fields});
        return;
      }
      const subjEl=root.querySelector('[data-field=subject]');
      const bodyEl=root.querySelector('[data-field=body]');
      const fields={};
      let changed=false;
      if(subjEl&&subjEl.defaultValue!==subjEl.value){
        fields.subject=subjEl.value; changed=true;
      }
      if(bodyEl&&bodyEl.defaultValue!==bodyEl.value){
        fields.body=bodyEl.value; changed=true;
      }
      if(changed){
        let msg='Please revise: ';
        if(fields.subject!=null) msg+='subject → '+fields.subject+'. ';
        if(fields.body!=null) msg+='body → '+fields.body;
        decide('edit',{user_edit:msg.trim(), fields:fields});
      } else {
        decide('approve');
      }
    }
  });
  if(cancel) cancel.onclick=()=>decide('cancel');
}
function add(kind,text,distillId,sources,packet,compiled){
  const d=document.createElement('div');d.className='msg '+kind;
  const pkt=packet||(kind==='ask'?MnemosParsePacket(text):null);
  if(kind==='ask' && pkt && pkt.kind==='approval'){
    d.className='msg ask folio-wrap';
    d.innerHTML=MnemosRenderFolio(pkt,{editable:true,meta:'Hold to seal · release early to abort'});
    bindFolioSeal(d);
    log.appendChild(d);log.scrollTop=log.scrollHeight;
    return;
  }
  const labels={user:'You',result:'@@BRAND@@',ask:'Needs you',error:'Issue',
    system:'',progress:''};
  const label=labels[kind];
  if(label){
    const lab=document.createElement('div');lab.className='msg-label';
    lab.textContent=label;d.appendChild(lab);
  }
  const shellNeeded=kind==='result'||kind==='ask'||kind==='error';
  const host=shellNeeded?document.createElement('div'):d;
  if(shellNeeded){host.className='msg-shell';d.appendChild(host);}
  const body=document.createElement('div');body.className='msg-body';
  const doc=compiled||null;
  const useDoc=kind==='result' && doc && doc.sections && doc.sections.length
    && window.MnemosResponse;
  if(useDoc){
    // Grounding lives inside the compiled document (collapsed).
    MnemosResponse.mount(body, doc, {
      includeGrounding:true,
      onAction:(prompt)=>{
        if(!prompt) return;
        box.value=prompt; persistChat(); send();
      }
    });
    // Keep raw text for verdict edit fallback
    body.dataset.rawText=text||'';
  }else{
    body.textContent=text;
  }
  host.appendChild(body);
  if(kind==='result' && sources && sources.length && !useDoc){
    const det=document.createElement('details');det.className='sources';
    const total=sources.reduce((n,s)=>n+(s.n||(s.items||[]).length||0),0);
    const sum=document.createElement('summary');
    sum.textContent='Grounded in '+total+' memory source'+(total===1?'':'s');
    det.appendChild(sum);
    for(const s of sources){
      for(const it of (s.items||[])){
        const li=document.createElement('div');li.textContent='— '+it;det.appendChild(li);
      }
    }
    host.appendChild(det);
  }
  if(kind==='result' && distillId){
    const acts=document.createElement('div');acts.className='verdict';
    const mk=(labelTxt,outcome,cls)=>{
      const b=document.createElement('button');b.type='button';b.textContent=labelTxt;
      if(cls) b.className=cls;
      b.title=outcome;b.onclick=()=>verdict(acts,distillId,outcome,b);
      return b;
    };
    acts.appendChild(mk('Helpful','accepted'));
    acts.appendChild(mk('Off','rejected','bad'));
    acts.appendChild(mk('Edit','edited'));
    host.appendChild(acts);
  }
  log.appendChild(d);log.scrollTop=log.scrollHeight;
}
async function verdict(acts,distillId,outcome,btn){
  let edited=null;
  if(outcome==='edited'){
    const bodyEl=acts.parentElement&&acts.parentElement.querySelector('.msg-body');
    const cur=(bodyEl&&(bodyEl.dataset.rawText||bodyEl.innerText))||'';
    edited=prompt('Corrected answer (saved as the training target):',cur);
    if(edited==null) return;
    edited=edited.trim(); if(!edited){alert('Edit needs corrected text.'); return;}
  }
  try{
    const r=await fetch('/chat/outcome',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({distill_id:distillId,outcome:outcome,edited_text:edited})});
    if(!r.ok){const j=await r.json().catch(()=>({})); alert(j.detail||('label failed ('+r.status+')')); return;}
    [...acts.querySelectorAll('button')].forEach(b=>{b.disabled=true;b.classList.remove('on');});
    btn.classList.add('on');
    if(outcome==='edited' && edited){
      const body=acts.parentElement&&acts.parentElement.querySelector('.msg-body');
      if(body){ body.classList.remove('rd-host'); body.textContent=edited; delete body.dataset.rawText; }
    }
  }catch(e){alert('label failed: '+e);}
}
async function poll(){
 // Guard against overlap: `since` only advances after the await, so a second
 // poll firing mid-flight (send()+setInterval, or a burst after the tab
 // regains focus) would re-fetch and re-render the same events (the "exit 0
 // x8" duplication). Skip if one is already running; the cursor persists.
 if(document.hidden) return;
 if(polling) return; polling=true;
 try{
  const r=await fetch('/chat/poll?since='+since); const j=await r.json();
  for(const e of (j.events||[])){
    since=e.id+1;
    if(e.kind==='error') lastErrShown=e.text; // event already renders it
    if(liveMode) add(e.kind, e.text, e.distill_id, e.sources, e.packet, e.compiled);
  }
  const s=j.state||{};
  awaiting=!!s.awaiting; todo=!!s.todo_pending;
  approvalMode=!!(s.packet && s.packet.kind==='approval')
    || !!(s.question && /APPROVAL NEEDED/.test(s.question));
  document.getElementById('url').textContent=s.url||'';
  const pol=[]; if(s.study_mode)pol.push(s.study_mode); if(s.mode)pol.push(s.mode); if(s.dry_run&&s.dry_run!=='approval')pol.push(s.dry_run==='full'||s.dry_run==='autonomous'?'autonomous':s.dry_run);
  document.getElementById('policy').textContent=pol.join(' · ');
  document.getElementById('cost').textContent=(s.cost!=null)?('$'+Number(s.cost).toFixed(4)):'';
  const waitEl=document.getElementById('waiting');
  if(waitEl) waitEl.textContent=s.waiting_on||(awaiting?(approvalMode?'Seal the approval folio…':'Waiting on your reply…'):(todo?'Waiting on yes/no…':''));
  // Offers keep Yes/No; approvals live in the folio Seal (hide generic bar).
  document.getElementById('bar').style.display=(liveMode&&((awaiting&&!approvalMode)||todo))?'flex':'none';
  fillDockDetail(s);
  box.placeholder=!liveMode?'Viewing a saved chat — Back to live to continue…'
    :(awaiting||todo)?(approvalMode?'Edit the folio, or type a revision…':'Yes/no above, or type a new request…')
    :'Ask @@BRAND@@, or give the agent a task…';
  // Banner + NEEDS YOU card + dock Yes/No already show pending offers —
  // never mirror waiting_on into the ambient column.
  const notes=[];
  if(liveMode){
    if(!(approvalMode || todo)){
      if(s.waiting_on) notes.push({text:s.waiting_on,attention:false});
    }
  } else {
    notes.push({text:'Reading a saved conversation.',attention:false});
  }
  MnemosAmbient.render(document.getElementById('ambientChat'), notes);
  if(liveMode && s.error && s.error!==lastErrShown){
    lastErrShown=s.error; add('error', s.error);
  }
 }catch(e){}
 finally{ polling=false; }
}
async function send(){
 if(!liveMode){ alert('You are viewing a saved chat. Click Back to live first.'); return; }
 const t=box.value.trim(); if(!t) return;
 if(pendingAttach.some(a=>a.status==='pending')){
   alert('Still uploading attachments — wait a moment, then send.');
   return;
 }
 box.value='';
 const dry=document.getElementById('dry').value||null;
 const mode=document.getElementById('studyMode').value||'general';
 const note=(ctxBox.value||'').trim();
 const attachCtx=pendingAttach.filter(a=>a.status==='ok'&&a.context)
   .map(a=>a.context).join('\n\n');
 const ctxParts=[note,attachCtx].filter(Boolean);
 const ctx=ctxParts.length?ctxParts.join('\n\n'):null;
 // Sticky context + attachment snippets are one-shot with the message.
 // File contents stay in memory (source=chat.attach) for learning.
 if(note||pendingAttach.length){
   ctxBox.value=''; pendingAttach=[]; renderAttach();
   ctxPanel.classList.remove('open'); ctxBtn.classList.remove('on'); syncCtxBtn();
 }
 const payload={message:t,dry_run:dry,mode}; if(ctx) payload.context=ctx;
 await fetch('/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 poll();
}
function reply(t){ box.value=t; send(); }
box.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();send();}});
let chatStreamOn=false;
let chatPollTimer=null;
function startChatPoll(){
  if(chatPollTimer) clearInterval(chatPollTimer);
  chatPollTimer=setInterval(poll, chatStreamOn?5000:1500);
}
if(window.MnemosChatStream){
  chatStreamOn=!!MnemosChatStream.connect(()=>poll());
}
startChatPoll();
poll();

// --- ghost browser pane: the agent's live view, no window on your screen ---
const ghostEl=document.getElementById('ghost'), ghostImg=document.getElementById('ghostimg'),
      ghostTtl=document.getElementById('ghostttl');
if(window.MnemosDock&&ghostEl) MnemosDock.add(ghostEl, MnemosDock.PRIORITY.ghost);
let ghostRevealed=false, ghostHideAt=0;
document.getElementById('ghostmin').onclick=()=>{
  ghostEl.classList.toggle('min');
  document.getElementById('ghostmin').textContent=ghostEl.classList.contains('min')?'+':'–';
};
document.getElementById('ghostreveal').onclick=async()=>{
  const ep=ghostRevealed?'/agent/ghost/park':'/agent/ghost/reveal';
  try{
    const j=await (await fetch(ep,{method:'POST'})).json();
    if(j.ok){ghostRevealed=!ghostRevealed;
      document.getElementById('ghostreveal').textContent=ghostRevealed?'park':'reveal';}
    else if(j.reason) ghostTtl.textContent=j.reason;
  }catch(e){}
};
async function ghostPoll(){
 if(document.hidden) return;
  if(document.hidden) return;
  try{
    const s=await (await fetch('/agent/ghost/status')).json();
    if(s.fresh){
      ghostHideAt=Date.now()+30000;   // linger a moment after the run ends
      ghostEl.style.display='block';
      ghostEl.classList.add('ink-border');
      ghostTtl.textContent=s.title||s.url||'Agent browser';
      ghostTtl.title=s.url||'';
      if(!ghostEl.classList.contains('min'))
        ghostImg.src='/agent/ghost/frame?t='+Date.now();
    }else if(Date.now()>ghostHideAt){
      ghostEl.style.display='none';
      ghostEl.classList.remove('ink-border');
    }
  }catch(e){}
}
setInterval(ghostPoll, chatStreamOn?2500:1200); ghostPoll();
</script></body></html>""")
