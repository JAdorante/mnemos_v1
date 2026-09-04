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
  margin:0;font:var(--fs-body)/var(--lh-body) var(--font);color:var(--text);
  height:100vh;height:100dvh;display:flex;flex-direction:column;
  background:var(--paper);
}
.chat-layout{
  flex:1;min-height:0;display:grid;
  grid-template-columns:minmax(0,min(240px,22vw)) minmax(0,1fr);
  transition:grid-template-columns var(--dur) var(--ease);
}
.chat-layout.stream-collapsed{
  grid-template-columns:48px minmax(0,1fr);
}
#ambientChat{
  padding:var(--sp-5) var(--sp-4);overflow:auto;min-width:0;
  border-right:1px solid var(--hairline);
  background:var(--surface);
}
.chat-layout.stream-collapsed #ambientChat{
  padding:var(--sp-3) var(--sp-2);overflow:hidden;
}
.chat-layout.stream-collapsed #ambientChat .cs-body{display:none}
#streamExpand{
  display:none;width:100%;height:auto;padding:var(--sp-2) 0;
  background:transparent;border:none;cursor:pointer;color:var(--mut);
  font:500 var(--fs-caption2)/1.2 var(--mono);letter-spacing:var(--track-caps);text-transform:uppercase;
  writing-mode:vertical-rl;transform:rotate(180deg);box-shadow:none;
}
.chat-layout.stream-collapsed #streamExpand{display:inline-flex;align-items:center;justify-content:center}
.chat-layout.stream-collapsed #streamExpand:hover{color:var(--navy);transform:rotate(180deg)}
@media(max-width:900px){
  .chat-layout,.chat-layout.stream-collapsed{grid-template-columns:1fr}
  #ambientChat{display:none}
}
.top{
  display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap;
  padding:var(--sp-3) var(--sp-5);
}
.chat-status{
  position:relative;flex:0 0 auto;
}
.chat-status > summary{
  list-style:none;cursor:pointer;user-select:none;
  font:500 var(--fs-caption)/1.2 var(--font);color:var(--mut);
  letter-spacing:var(--track-snug);
  padding:6px 10px;border-radius:var(--radius-sm);border:1px solid transparent;
  transition:color var(--dur-fast) var(--ease-io),background var(--dur-fast) var(--ease-io),
    border-color var(--dur-fast) var(--ease-io);
}
.chat-status > summary::-webkit-details-marker{display:none}
.chat-status > summary:hover,
.chat-status[open] > summary{
  color:var(--text);border-color:var(--line);background:rgba(11,19,32,.03);
}
.chat-status .status-panel{
  display:none;position:absolute;right:0;top:calc(100% + var(--sp-2));z-index:var(--z-popover);
  width:min(280px,calc(100vw - 24px));
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);padding:var(--sp-3) var(--sp-4);
  font:var(--fs-caption2)/1.45 var(--mono);color:var(--mut);
  flex-direction:column;gap:6px;
  animation:menuIn var(--dur-fast) var(--ease) both;transform-origin:top right;
}
.chat-status[open] .status-panel{display:flex}
.chat-status .status-panel span{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chat-tools{display:flex;gap:6px;align-items:center;position:relative}
.chat-tools button{
  background:transparent;border:1px solid transparent;border-radius:var(--radius-sm);
  padding:6px 10px;font:500 var(--fs-caption)/1.2 var(--font);color:var(--mut);cursor:pointer;
  letter-spacing:var(--track-snug);box-shadow:none;
}
.chat-tools button:hover{
  color:var(--text);border-color:var(--line);background:rgba(11,19,32,.03);
  transform:none;box-shadow:none;
}
.chat-tools button:active{transform:none}
#pastPanel{
  display:none;position:absolute;left:0;bottom:calc(100% + var(--sp-2));z-index:var(--z-popover);
  width:min(340px,calc(100vw - 24px));max-height:min(360px,calc(100dvh - var(--chrome-h) - 24px));overflow:auto;
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);padding:var(--sp-2);animation:fadeUp var(--dur-fast) var(--ease) both;
}
#pastPanel.open{display:block}
#pastPanel .past-head{
  display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2);
  padding:6px 8px 8px;font:var(--fs-caption2)/1.2 var(--mono);color:var(--mut);
  text-transform:uppercase;letter-spacing:var(--track-caps);
}
#pastPanel .past-empty{padding:var(--sp-3) var(--sp-2);color:var(--mut);font:var(--fs-footnote)/var(--lh-snug) var(--font)}
.past-item{
  display:block;width:100%;text-align:left;border:none;background:transparent;
  border-radius:var(--radius-sm);padding:var(--sp-2) var(--sp-3);cursor:pointer;color:var(--text);
  font:var(--fs-footnote)/var(--lh-snug) var(--font);box-shadow:none;
}
.past-item:hover{background:rgba(11,19,32,.04);transform:none;box-shadow:none}
.past-item .past-title{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.past-item .past-meta{display:block;margin-top:3px;font:var(--fs-caption2)/1.2 var(--mono);color:var(--mut)}
#archiveBanner{
  display:none;width:min(800px,94%);margin:var(--sp-2) auto 0;padding:var(--sp-2) var(--sp-4);
  background:rgba(30,91,79,.06);border:1px solid rgba(30,91,79,.18);
  border-radius:var(--radius-sm);font:var(--fs-footnote)/var(--lh-snug) var(--font);color:var(--navy);
  align-items:center;justify-content:space-between;gap:var(--sp-3);
}
#archiveBanner.show{display:flex}
#archiveBanner button{
  background:transparent;border:1px solid var(--line);border-radius:var(--radius-xs);
  padding:5px 10px;font:500 var(--fs-caption)/1.2 var(--font);color:var(--navy);cursor:pointer;
  letter-spacing:var(--track-snug);box-shadow:none;flex:0 0 auto;
}
#archiveBanner button:hover{background:rgba(11,19,32,.04);transform:none;box-shadow:none}
#log{
  flex:1;overflow:auto;padding:var(--sp-7) var(--sp-5) calc(var(--sp-12) + var(--dock-clear, 0px));
  display:flex;flex-direction:column;gap:4px;align-items:center;min-width:0;min-height:0;
}
.chat-main{display:flex;flex-direction:column;min-width:0;min-height:0}
.msg{
  max-width:min(800px,94%);width:100%;
  animation:fadeUp var(--dur) var(--ease) both;
  position:relative;
}
.msg-label{
  font:500 10px/1 var(--mono);letter-spacing:var(--track-caps);text-transform:uppercase;
  color:var(--mut);margin:0 0 var(--sp-2);padding:0 2px;
}
.msg-body{
  white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere;
  font:var(--fs-body)/var(--lh-loose) var(--font);color:var(--text);letter-spacing:-.01em;
}
/* User — warm ivory bubble, right */
.msg.user{
  align-self:stretch;max-width:min(800px,94%);
  margin:var(--sp-4) 0 var(--sp-2);display:flex;flex-direction:column;align-items:flex-end;
}
.msg.user .msg-label{display:none}
.msg.user .msg-body{
  max-width:min(420px,88%);
  background:var(--panel-2);color:var(--navy);font-weight:500;
  padding:var(--sp-3) var(--sp-4);
  border-radius:var(--radius) var(--radius) var(--radius-xs) var(--radius);
  text-align:left;box-shadow:var(--shadow-workspace);
  border:1px solid var(--hairline);
}
/* Sparrow — uncontained editorial prose */
.msg.result{
  margin:var(--sp-5) 0 var(--sp-2);padding:0;
}
.msg.result .msg-shell{
  background:transparent;border:none;border-radius:0;padding:0;
  box-shadow:none;position:relative;
}
.msg.result .msg-shell::before{display:none}
.msg.result .msg-label{color:var(--mut);opacity:.9}
.msg.result .msg-body{padding-left:0}
.msg.result .msg-body.rd-host{padding-left:0;white-space:normal}
.sources,.mnemos-prov.sources{
  margin:var(--sp-3) 0 0;
}
.sources:not(.mnemos-prov){
  padding:var(--sp-2) var(--sp-3);border:1px solid var(--hairline);border-radius:var(--radius-sm);
  background:var(--surface);box-shadow:var(--shadow-workspace);
  font-size:var(--fs-caption2);color:var(--mut);line-height:var(--lh-body);
}
.sources:not(.mnemos-prov) summary{
  cursor:pointer;user-select:none;list-style:none;
  font:500 var(--fs-caption2)/1.2 var(--mono);letter-spacing:.02em;color:var(--charcoal);
}
.sources summary::-webkit-details-marker{display:none}
.sources summary:hover{color:var(--navy)}
.sources div{margin:4px 0 0 2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.verdict{
  display:flex;gap:6px;margin:var(--sp-3) 0 0;padding-top:var(--sp-2);
  border-top:1px solid var(--hairline);
}
.verdict button{
  background:transparent;border:1px solid transparent;border-radius:var(--radius-xs);
  padding:4px 10px;font-size:var(--fs-footnote);line-height:1.2;cursor:pointer;color:var(--mut);
  font-family:var(--font);letter-spacing:var(--track-snug);
}
.verdict button:hover:not(:disabled){
  border-color:var(--line);color:var(--text);background:rgba(11,19,32,.03);
}
.verdict button.on{border-color:rgba(46,111,87,.35);color:var(--ok);background:rgba(46,111,87,.06)}
.verdict button.bad.on{border-color:rgba(166,71,71,.35);color:var(--danger);background:rgba(166,71,71,.06)}
.verdict button:disabled{opacity:.5;cursor:default;transform:none;box-shadow:none}
@media(hover:hover) and (pointer:fine){
  .msg.result .verdict{opacity:0;transition:opacity var(--dur-fast) var(--ease-io)}
  .msg.result:hover .verdict,
  .msg.result:focus-within .verdict,
  .msg.result .verdict:has(.on){opacity:1}
}
.msg.system{
  align-self:center;max-width:min(480px,90%);margin:var(--sp-2) 0;
  text-align:center;background:transparent;padding:0;box-shadow:none;
}
.msg.system .msg-body{
  font:italic var(--fs-footnote)/var(--lh-snug) var(--font);color:var(--mut);text-align:center;
}
.msg.ask{margin:var(--sp-3) 0 var(--sp-2)}
.msg.ask:not(.folio-wrap) .msg-shell{
  background:rgba(255,254,251,.95);border:1px solid rgba(199,138,44,.22);
  border-radius:var(--radius);padding:var(--sp-4);
  box-shadow:var(--shadow-surface);
}
.msg.ask .msg-label{color:var(--warn)}
.msg.ask.folio-wrap{background:transparent;border:none;box-shadow:none;padding:0;margin:var(--sp-5) 0}
.msg.error{margin:var(--sp-3) 0}
.msg.error .msg-shell{
  background:rgba(166,71,71,.05);border:1px solid rgba(166,71,71,.18);
  border-radius:var(--radius);padding:var(--sp-3) var(--sp-4);
}
.msg.error .msg-label{color:var(--danger)}
.msg.error .msg-body{color:var(--danger);font-size:var(--fs-sub)}
.msg.progress{
  margin:2px 0;max-width:min(800px,94%);
}
.msg.progress .msg-body{
  font:var(--fs-caption2)/1.5 var(--mono);color:var(--mut);
  padding:4px 0 4px var(--sp-4);border-left:1.5px solid var(--line);
  background:transparent;box-shadow:none;
}
/* Empty welcome */
#emptyState{
  width:min(800px,94%);margin:auto 0;padding:var(--sp-12) var(--sp-2) var(--sp-8);
  text-align:center;animation:fadeUp var(--dur-slow) var(--ease) both;
}
#emptyState.hidden{display:none}
#emptyState .empty-greeting{
  font-family:var(--display);font-size:clamp(26px,3.2vw,var(--fs-large));font-weight:400;
  line-height:var(--lh-tight);letter-spacing:var(--track-tight);color:var(--navy);margin:0 0 var(--sp-2);
}
#emptyState .empty-sub{
  font:var(--fs-body)/var(--lh-body) var(--font);color:var(--mut);margin:0 0 var(--sp-7);
}
#emptyState .empty-actions{
  display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:var(--sp-3);
  max-width:640px;margin:0 auto;text-align:left;
}
@media(max-width:720px){
  #emptyState .empty-actions{grid-template-columns:1fr}
}
.empty-card{
  background:var(--float);border:1px solid var(--hairline);
  border-radius:var(--radius);padding:var(--sp-4);cursor:pointer;box-shadow:var(--shadow-workspace);
  transition:border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease),
    background var(--dur) var(--ease);
  text-align:left;width:100%;font:inherit;color:inherit;
}
.empty-card:hover{
  border-color:var(--line-strong);background:var(--bg-elev);
  box-shadow:var(--shadow-surface);
}
.empty-card:active{transform:scale(.97)}
.empty-card .ec-title{
  display:block;font:600 var(--fs-footnote)/1.3 var(--font);color:var(--navy);
  letter-spacing:var(--track-snug);margin-bottom:var(--sp-1);
}
.empty-card .ec-desc{
  display:block;font:var(--fs-caption)/var(--lh-snug) var(--font);color:var(--mut);
}
/* Context stream */
.cs-head{
  display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2);margin-bottom:var(--sp-4);
}
.cs-kicker{
  font:500 10px/1 var(--mono);letter-spacing:var(--track-caps);text-transform:uppercase;color:var(--mut);
}
.cs-collapse{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:var(--fs-sub)/1 var(--font);padding:2px 6px;border-radius:var(--radius-xs);box-shadow:none;line-height:1;
}
.cs-collapse:hover{color:var(--navy);background:rgba(11,19,32,.04);transform:none;box-shadow:none}
.cs-section{margin-bottom:var(--sp-5)}
.cs-listen-row{display:flex;align-items:center;gap:var(--sp-2);margin-bottom:var(--sp-1)}
.cs-dot{
  width:7px;height:7px;border-radius:var(--radius-full);background:rgba(11,19,32,.2);flex:0 0 auto;
}
.cs-dot.on{
  background:var(--acc);
  box-shadow:0 0 0 0 var(--acc-35);
  animation:listenPulse 2.4s var(--ease) infinite;
}
@keyframes listenPulse{
  0%,100%{box-shadow:0 0 0 0 var(--acc-28)}
  50%{box-shadow:0 0 0 6px transparent}
}
@media(prefers-reduced-motion:reduce){
  .cs-dot.on{animation:none;box-shadow:0 0 0 3px var(--acc-12)}
}
.cs-listen-label{font:500 var(--fs-footnote)/1.2 var(--font);letter-spacing:var(--track-snug);color:var(--navy)}
.cs-status{margin:0;font:var(--fs-caption)/1.4 var(--font);color:var(--mut);padding-left:15px}
.cs-empty{margin:6px 0 0;font:var(--fs-caption)/var(--lh-snug) var(--font);color:var(--mut)}
.cs-recent{list-style:none;margin:var(--sp-2) 0 0;padding:0;display:flex;flex-direction:column;gap:2px}
.cs-recent-item{
  display:flex;flex-direction:column;gap:2px;width:100%;text-align:left;
  background:transparent;border:none;border-radius:var(--radius-sm);padding:var(--sp-2);
  cursor:pointer;box-shadow:none;color:var(--text);
}
.cs-recent-item:hover{background:rgba(11,19,32,.04);transform:none;box-shadow:none}
.cs-recent-time{font:10px/1.2 var(--mono);color:var(--mut)}
.cs-recent-title{
  font:var(--fs-caption)/var(--lh-snug) var(--font);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
}
.cs-mem{list-style:none;margin:var(--sp-2) 0 0;padding:0;display:flex;flex-direction:column;gap:6px}
.cs-mem li{font:var(--fs-caption)/var(--lh-snug) var(--font);color:var(--charcoal)}
.cs-mem-n{font:500 var(--fs-caption)/1.2 var(--mono);color:var(--acc);margin-right:4px}
/* Dock + floating composer */
.dock{
  border-top:none;background:transparent;
  padding:0 var(--sp-5) var(--sp-5);display:flex;flex-direction:column;align-items:center;
}
#bar{
  display:none;gap:var(--sp-3);align-items:center;justify-content:flex-start;flex-wrap:wrap;
  width:min(800px,100%);margin:var(--sp-2) 0 var(--sp-1);padding:var(--sp-3) var(--sp-4);
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);animation:fadeUp var(--dur) var(--ease) both;
}
#bar .action-detail{flex:1 1 100%;order:5;margin:var(--sp-1) 0 0}
#bar .approval-form{order:6}
#waiting{
  flex:1;min-width:0;font-size:var(--fs-footnote);color:var(--warn);line-height:var(--lh-snug);
  overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;
}
#bar button{
  flex:0 0 auto;border-radius:var(--radius-sm);padding:var(--sp-2) var(--sp-4);cursor:pointer;
  font:500 var(--fs-sub)/1.2 var(--font);letter-spacing:var(--track-snug);
  border:1px solid var(--line);background:var(--bg-elev);color:var(--text);
}
#bar .yes{border-color:rgba(46,111,87,.4);color:var(--ok)}
#bar .yes:hover{
  background:rgba(46,111,87,.1);border-color:rgba(46,111,87,.55);
}
#bar .no{border-color:rgba(166,71,71,.4);color:var(--danger)}
#bar .no:hover{
  background:rgba(166,71,71,.1);border-color:rgba(166,71,71,.55);
}
.composer-wrap{
  display:flex;flex-direction:column;gap:var(--sp-2);width:min(820px,100%);padding-top:var(--sp-2);
}
.composer-toolbar{
  display:flex;align-items:center;justify-content:flex-end;gap:var(--sp-1);
  width:100%;position:relative;
}
.composer{
  display:flex;flex-direction:column;gap:var(--sp-2);width:100%;
  background:var(--float);border:1px solid var(--line);
  border-radius:var(--radius-lg);padding:var(--sp-4) var(--sp-4) var(--sp-3);
  box-shadow:var(--shadow-float);
  transition:border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease);
}
.composer-footer{
  display:flex;align-items:center;gap:6px;flex-wrap:wrap;
  min-width:0;
}
.composer-footer select{min-width:0;max-width:100%}
#box{
  width:100%;background:transparent;color:var(--text);border:none;
  border-radius:0;padding:2px 4px;resize:none;min-height:56px;max-height:40vh;
  height:56px;font:inherit;line-height:var(--lh-body);box-shadow:none;overflow-y:auto;
}
#box::placeholder{color:var(--mut);opacity:.75}
#box:focus{outline:none;border:none;box-shadow:none}
.composer:focus-within{
  border-color:var(--acc-45);
  box-shadow:var(--shadow-float),0 0 0 4px var(--acc-12);
}
#dry,#studyMode{
  background:transparent;color:var(--mut);border:1px solid transparent;
  border-radius:var(--radius-sm);padding:6px 8px;min-width:0;max-width:140px;
  font:500 var(--fs-caption)/1.2 var(--font);letter-spacing:var(--track-snug);
  cursor:pointer;box-shadow:none;
  transition:border-color var(--dur-fast) var(--ease-io),color var(--dur-fast) var(--ease-io),
    background var(--dur-fast) var(--ease-io);
}
#studyMode{max-width:150px}
#dry:hover,#studyMode:hover,
#dry:focus,#studyMode:focus{
  color:var(--navy);border-color:var(--line);background:rgba(11,19,32,.04);
  outline:none;transform:none;box-shadow:none;
}
#dry:focus,#studyMode:focus{border-color:var(--line-strong)}
#ctxBtn{
  background:transparent;color:var(--mut);border:1px solid transparent;
  border-radius:var(--radius-sm);padding:6px 10px;min-width:auto;cursor:pointer;
  font:500 var(--fs-caption)/1.2 var(--font);letter-spacing:var(--track-snug);
  white-space:nowrap;box-shadow:none;
}
#ctxBtn:hover{
  color:var(--navy);border-color:var(--line);background:rgba(11,19,32,.04);
  transform:none;box-shadow:none;
}
#ctxBtn.on{color:var(--navy);border-color:var(--acc-45);background:var(--acc-08)}
#ctxBtn.has{color:var(--ok);border-color:rgba(46,111,87,.35)}
.composer-footer .grow{flex:1;min-width:4px}
#ctxPanel{
  display:none;width:100%;background:var(--float);border:1px solid var(--line);
  border-radius:var(--radius-lg);padding:var(--sp-3) var(--sp-4);box-shadow:var(--shadow-surface);
  animation:fadeUp var(--dur) var(--ease) both;
}
#ctxPanel.open{display:block}
#ctxPanel .ctx-label{
  display:flex;align-items:center;justify-content:space-between;gap:var(--sp-2);
  font:var(--fs-caption2)/var(--lh-snug) var(--mono);color:var(--mut);margin-bottom:6px;
}
#ctxPanel .ctx-label button{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:var(--fs-caption)/1.2 var(--font);padding:0 4px;box-shadow:none;
}
#ctxPanel .ctx-label button:hover{
  color:var(--danger);transform:none;box-shadow:none;background:transparent;
}
#ctxBox{
  width:100%;box-sizing:border-box;background:var(--bg-elev);color:var(--text);
  border:1px solid var(--line);border-radius:var(--radius-sm);padding:var(--sp-2) var(--sp-3);resize:vertical;
  min-height:72px;max-height:200px;font:inherit;line-height:1.45;
  transition:border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease);
}
#ctxBox::placeholder{color:var(--mut);opacity:.75}
#ctxBox:focus{outline:none;border-color:var(--acc-45);box-shadow:0 0 0 4px var(--acc-12)}
#ctxFiles{
  display:flex;flex-wrap:wrap;gap:6px;margin-top:var(--sp-2);min-height:0;
}
#ctxFiles:empty{display:none}
.ctx-file{
  display:inline-flex;align-items:center;gap:6px;max-width:100%;
  background:var(--bg-elev);border:1px solid var(--line);border-radius:var(--radius-xs);
  padding:5px 8px 5px 10px;font:var(--fs-caption)/1.2 var(--font);color:var(--text);
}
.ctx-file .ctx-file-name{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px;
}
.ctx-file .ctx-file-meta{color:var(--mut);font:var(--fs-caption2)/1.2 var(--mono);white-space:nowrap}
.ctx-file.pending{opacity:.7}
.ctx-file.err{border-color:rgba(160,50,50,.45);color:var(--danger)}
.ctx-file.ok{border-color:rgba(46,111,87,.35)}
.ctx-file button{
  background:transparent;border:none;color:var(--mut);cursor:pointer;
  font:var(--fs-caption)/1 var(--font);padding:0 2px;box-shadow:none;line-height:1;
}
.ctx-file button:hover{
  color:var(--danger);transform:none;box-shadow:none;background:transparent;
}
#ctxAttach{
  background:transparent;border:1px dashed var(--line);border-radius:var(--radius-sm);
  color:var(--mut);cursor:pointer;font:var(--fs-caption)/1.2 var(--font);padding:6px 10px;
  margin-top:var(--sp-2);width:100%;text-align:left;
  transition:border-color var(--dur-fast) var(--ease-io),color var(--dur-fast) var(--ease-io),
    background var(--dur-fast) var(--ease-io);
}
#ctxAttach:hover{
  color:var(--text);border-color:var(--line-strong);background:var(--bg-elev);
  transform:none;box-shadow:none;
}
#ctxAttach:disabled{opacity:.55;cursor:wait}
#ctxLearn{
  margin-top:6px;font:var(--fs-caption2)/var(--lh-snug) var(--mono);color:var(--mut);
}
#send{
  width:36px;height:36px;flex:0 0 auto;padding:0;
  background:var(--navy);color:var(--paper);border:none;border-radius:var(--radius-full);
  cursor:pointer;font:600 16px/1 var(--font);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.08),0 1px 2px rgba(11,19,32,.16);
  display:inline-flex;align-items:center;justify-content:center;
}
#send:hover:not(:disabled){
  background:color-mix(in srgb,var(--navy) 86%,#41527A);
  transform:none;filter:none;
}
#send:active:not(:disabled){transform:scale(.97);box-shadow:var(--shadow-press)}
#send:disabled{opacity:.45;cursor:default;transform:none}
#ghost{
  position:relative;width:min(380px,calc(100vw - 48px));display:none;
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);overflow:hidden;animation:fadeUp var(--dur) var(--ease) both;
}
#ghost.ink-border{box-shadow:var(--shadow-float),inset 0 0 0 1px var(--acc-20)}
#ghost .head{
  display:flex;align-items:center;gap:var(--sp-2);padding:7px 10px;
  border-bottom:1px solid var(--hairline);font:var(--fs-caption)/1.2 var(--mono);color:var(--mut);
}
#ghost .head .ttl{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#ghost .head button{
  background:transparent;border:1px solid var(--line);border-radius:var(--radius-xs);
  padding:2px 8px;font-size:var(--fs-caption2);cursor:pointer;color:var(--mut);font-family:var(--font);
}
#ghost .head button:hover{
  color:var(--text);border-color:var(--line-strong);background:var(--bg-elev);
}
#ghost img{display:block;width:100%;background:var(--panel)}
#ghost.min img{display:none}
@media(max-width:900px){#ghost{width:min(280px,calc(100vw - 48px))}}
@media(max-width:640px){
  .top{padding:var(--sp-2) var(--sp-4);gap:var(--sp-2)}
  #log{padding:var(--sp-5) var(--sp-3) calc(var(--sp-8) + var(--dock-clear, 0px))}
  .composer{padding:var(--sp-3) var(--sp-3) calc(var(--sp-3) + env(safe-area-inset-bottom,0px))}
  .msg{max-width:100%}
  .msg.user .msg-body{max-width:92%}
  #ghost{display:none !important}
  #archiveBanner{width:calc(100% - 24px);flex-wrap:wrap}
}
@media(prefers-reduced-motion:reduce){
  .chat-layout{transition:none}
  .msg,#emptyState,#pastPanel,#ctxPanel,#bar,#ghost,.chat-status .status-panel{animation:none}
}
</style></head><body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  @@NAV@@
  <details class="chat-status" id="chatStatus">
    <summary title="Model &amp; session status">Status</summary>
    <div class="status-panel">
      <span id="url"></span>
      <span id="policy"></span>
      <span id="cost"></span>
    </div>
  </details>
</header>
<div class="chat-layout" id="chatLayout">
<aside id="ambientChat" aria-label="Context stream">
  <button type="button" id="streamExpand" title="Expand context stream">Context</button>
  <div class="cs-body" id="csBody"></div>
</aside>
<div class="chat-main">
<div id="archiveBanner">
  <span id="archiveBannerText">Viewing a saved conversation (read-only).</span>
  <button type="button" id="backLiveBtn">Back to live</button>
</div>
<div id="log">
  <div id="emptyState">
    <h1 class="empty-greeting" id="emptyGreeting">Good day.</h1>
    <p class="empty-sub">What are we working through?</p>
    <div class="empty-actions">
      <button type="button" class="empty-card" data-prompt="What do you remember about ">
        <span class="ec-title">Recall something</span>
        <span class="ec-desc">Search what Sparrow remembers</span>
      </button>
      <button type="button" class="empty-card" data-prompt="Help me think through ">
        <span class="ec-title">Think through something</span>
        <span class="ec-desc">Work through an idea with your context</span>
      </button>
      <button type="button" class="empty-card" data-prompt="Help me draft or prepare ">
        <span class="ec-title">Take an action</span>
        <span class="ec-desc">Draft, research, or prepare something</span>
      </button>
    </div>
  </div>
</div>
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
    <div class="composer-toolbar">
      <div class="chat-tools">
        <button type="button" id="pastBtn" title="Browse saved conversations">Past</button>
        <button type="button" id="newChatBtn" title="Save this chat and start fresh">New</button>
        <div id="pastPanel" role="dialog" aria-modal="true" aria-label="Past conversations" aria-hidden="true">
          <div class="past-head"><span>Saved chats</span><span id="pastCount"></span></div>
          <div id="pastList"><div class="past-empty">No saved chats yet.</div></div>
        </div>
      </div>
    </div>
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
      <textarea id="box" placeholder="Ask @@BRAND@@ anything…" rows="2"></textarea>
      <div class="composer-footer">
        <button type="button" id="ctxBtn" title="Add notes, documents, or photos for the next message">+ Context</button>
        <select id="studyMode" title="Study mode — how the assistant coaches this session">
          <option value="general">General</option>
          <option value="lecture_notes">Lecture notes</option>
          <option value="homework">Homework help</option>
          <option value="study_quiz">Study / quiz</option>
          <option value="syllabus">Syllabus &amp; deadlines</option>
          <option value="essay_rubric">Essay / rubric</option>
          <option value="reading">Reading / textbook</option>
        </select>
        <select id="dry" title="How far the agent may go this turn">
          <option value="">Default</option>
          <option value="plan">Plan only</option>
          <option value="navigate">Navigate only</option>
          <option value="draft">Draft only</option>
          <option value="approval">Approval</option>
          <option value="full">Full (autonomous)</option>
          <option value="autonomous">Autonomous</option>
        </select>
        <span class="grow"></span>
        <button id="send" onclick="send()" title="Send" aria-label="Send">↑</button>
      </div>
    </div>
  </div>
</div>
<script>
let since=0, awaiting=false, todo=false, polling=false, approvalMode=false;
let lastErrShown=null;
let liveMode=true;
let userName='there';
let streamRecent=[];
let streamMemory={people:null,commitments:null,related:null};
let lastStreamStatus='Quiet for now';
const log=document.getElementById('log'), box=document.getElementById('box');
const emptyState=document.getElementById('emptyState');
const chatLayout=document.getElementById('chatLayout');
const ambientEl=document.getElementById('ambientChat');
const csBody=document.getElementById('csBody');
function fillDockDetail(s){
  const det=document.getElementById('dockDetail');
  if(!det) return;
  const pkt=s&&s.packet;
  const fields=(pkt&&pkt.fields)||{};
  const intent=(fields.action||(pkt&&pkt.summary)||s.waiting_on||s.question||'').trim();
  document.getElementById('dockIntent').textContent=intent||'Sparrow is waiting for your decision.';
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
  if(MnemosMemory.get('chat.streamCollapsed', false)){
    chatLayout.classList.add('stream-collapsed');
  }
  resizeBox();
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
function fmtTime(iso){
  if(!iso) return '';
  try{
    const d=new Date(iso); if(isNaN(d)) return '';
    return d.toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
  }catch(e){ return ''; }
}
function greetingForNow(){
  const h=new Date().getHours();
  if(h<12) return 'Good morning';
  if(h<17) return 'Good afternoon';
  return 'Good evening';
}
function firstName(name){
  const n=(name||'').trim();
  if(!n) return 'there';
  return n.split(/\s+/)[0];
}
function syncEmptyGreeting(){
  const el=document.getElementById('emptyGreeting');
  if(el) el.textContent=greetingForNow()+', '+firstName(userName)+'.';
}
function hasConversation(){
  return !!(log.querySelector('.msg.user, .msg.result, .msg.ask, .msg.error'));
}
function syncEmptyState(){
  if(!emptyState) return;
  const show=liveMode && !hasConversation();
  emptyState.classList.toggle('hidden', !show);
  if(show){
    if(!emptyState.parentElement || emptyState.parentElement!==log){
      log.prepend(emptyState);
    }
  }
}
function resizeBox(){
  box.style.height='auto';
  const next=Math.min(Math.max(box.scrollHeight, 56), Math.floor(window.innerHeight*0.4));
  box.style.height=next+'px';
}
function setLiveMode(on){
  liveMode=!!on;
  archiveBanner.classList.toggle('show', !liveMode);
  box.disabled=!liveMode;
  document.getElementById('send').disabled=!liveMode;
  if(liveMode) archiveBannerText.textContent='Viewing a saved conversation (read-only).';
  syncEmptyState();
}
function setStreamCollapsed(on){
  chatLayout.classList.toggle('stream-collapsed', !!on);
  MnemosMemory.set('chat.streamCollapsed', !!on);
}
function renderContextStream(){
  if(!window.MnemosContextStream || !csBody) return;
  const host=csBody;
  MnemosContextStream.render(host, {
    listening: liveMode,
    status: lastStreamStatus,
    recent: streamRecent,
    memory: streamMemory,
  }, {
    onCollapse:()=>setStreamCollapsed(true),
    onOpenSession:(id, title)=>{ if(id) openPast(id, title); },
  });
}
async function refreshStreamData(){
  try{
    const r=await fetch('/chat/sessions?limit=5'); const j=await r.json();
    streamRecent=((j&&j.sessions)||[]).map(s=>({
      id:s.id, title:s.title||'Untitled chat',
      saved_at:s.saved_at, when:fmtTime(s.saved_at)
    }));
  }catch(e){}
  try{
    const [pd, pl]=await Promise.all([
      fetch('/profile/data').then(x=>x.json()).catch(()=>({})),
      fetch('/people/list?include_candidates=0').then(x=>x.json()).catch(()=>({})),
    ]);
    if(pd&&pd.identity&&pd.identity.name){
      userName=pd.identity.name; syncEmptyGreeting();
    }
    const work=(pd&&pd.work)||[];
    const commitments=work.filter(w=>w.kind==='commitment').length;
    const people=((pl&&pl.people)||[]).length;
    streamMemory={
      related: ((pd&&pd.about)||[]).length + work.length,
      people: people,
      commitments: commitments,
    };
  }catch(e){}
  renderContextStream();
}
document.getElementById('streamExpand').onclick=()=>setStreamCollapsed(false);
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
    [...log.querySelectorAll('.msg')].forEach(n=>n.remove());
    for(const e of (j.session.events||[])){
      add(e.kind, e.text, e.distill_id, e.sources, e.packet, null);
    }
    if(!(j.session.events||[]).length){
      add('system','(empty saved chat)');
    }
    syncEmptyState();
  }catch(e){ alert('Could not open saved chat'); }
}
async function backToLive(){
  setLiveMode(true);
  [...log.querySelectorAll('.msg')].forEach(n=>n.remove());
  since=0;
  syncEmptyState();
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
    [...log.querySelectorAll('.msg')].forEach(n=>n.remove());
    since=0;
    syncEmptyState();
    await poll();
    refreshPast();
    refreshStreamData();
  }catch(e){ alert('Could not start a new chat'); }
}
function closePastPanel(){
  MnemosDialog.close(pastPanel);
}
pastBtn.onclick=(ev)=>{
  ev.stopPropagation();
  if(MnemosDialog.isOpen(pastPanel)){
    closePastPanel();
  }else{
    pastPanel.style.left='auto';
    pastPanel.style.right='0';
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
box.addEventListener('input', ()=>{ persistChat(); resizeBox(); });
document.getElementById('dry').addEventListener('change', persistChat);
document.querySelectorAll('.empty-card').forEach(card=>{
  card.addEventListener('click',()=>{
    const p=card.getAttribute('data-prompt')||'';
    box.value=p; persistChat(); resizeBox(); box.focus();
    try{ box.setSelectionRange(p.length, p.length); }catch(e){}
  });
});
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
  // Orchestrator logs and startup heartbeats stay in the event log; they
  // are not conversation. Status already shows Working… while a turn runs.
  if(kind==='progress') return;
  if(kind==='system' && /^(Agent ready|Fast lane ready|Offer expired)\b/.test(String(text||''))) return;
  const d=document.createElement('div');d.className='msg '+kind;
  const pkt=packet||(kind==='ask'?MnemosParsePacket(text):null);
  if(kind==='ask' && pkt && pkt.kind==='approval'){
    d.className='msg ask folio-wrap';
    d.innerHTML=MnemosRenderFolio(pkt,{editable:true,meta:'Hold to seal · release early to abort'});
    bindFolioSeal(d);
    log.appendChild(d);log.scrollTop=log.scrollHeight;
    syncEmptyState();
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
    MnemosResponse.mount(body, doc, {
      includeGrounding:true,
      onAction:(prompt)=>{
        if(!prompt) return;
        box.value=prompt; persistChat(); resizeBox(); send();
      }
    });
    body.dataset.rawText=text||'';
  }else{
    body.textContent=text;
  }
  host.appendChild(body);
  if(kind==='result' && sources && sources.length && !useDoc){
    const det=document.createElement('details');det.className='sources mnemos-prov';
    const sum=document.createElement('summary');
    sum.innerHTML='<span class="prov-mark" aria-hidden="true">◇</span>'
      +'<span class="prov-label">Sources</span>'
      +'<a class="prov-go" href="/memory" title="Open Memory" onclick="event.stopPropagation()">↗</a>';
    det.appendChild(sum);
    const bodyWrap=document.createElement('div');bodyWrap.className='prov-body';
    for(const s of sources){
      for(const it of (s.items||[])){
        const li=document.createElement('div');li.className='rd-g-item';li.textContent='— '+it;
        bodyWrap.appendChild(li);
      }
    }
    det.appendChild(bodyWrap);
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
  syncEmptyState();
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
 if(document.hidden) return;
 if(polling) return; polling=true;
 try{
  const r=await fetch('/chat/poll?since='+since); const j=await r.json();
  for(const e of (j.events||[])){
    since=e.id+1;
    if(e.kind==='error') lastErrShown=e.text;
    if(liveMode) add(e.kind, e.text, e.distill_id, e.sources, e.packet, e.compiled);
  }
  const s=j.state||{};
  awaiting=!!s.awaiting; todo=!!s.todo_pending;
  approvalMode=!!(s.packet && s.packet.kind==='approval')
    || !!(s.question && /APPROVAL NEEDED/.test(s.question));
  document.getElementById('url').textContent=s.url?('URL · '+s.url):'URL · —';
  const pol=[]; if(s.study_mode)pol.push(s.study_mode); if(s.mode)pol.push(s.mode); if(s.dry_run&&s.dry_run!=='approval')pol.push(s.dry_run==='full'||s.dry_run==='autonomous'?'autonomous':s.dry_run);
  document.getElementById('policy').textContent=pol.length?('Mode · '+pol.join(' · ')):'Mode · —';
  document.getElementById('cost').textContent=(s.cost!=null)?('Cost · $'+Number(s.cost).toFixed(4)):'Cost · —';
  const waitEl=document.getElementById('waiting');
  if(waitEl) waitEl.textContent=s.waiting_on||(awaiting?(approvalMode?'Seal the approval folio…':'Waiting on your reply…'):(todo?'Waiting on yes/no…':''));
  document.getElementById('bar').style.display=(liveMode&&((awaiting&&!approvalMode)||todo))?'flex':'none';
  fillDockDetail(s);
  box.placeholder=!liveMode?'Viewing a saved chat — Back to live to continue…'
    :(awaiting||todo)?(approvalMode?'Edit the folio, or type a revision…':'Yes/no above, or type a new request…')
    :'Ask @@BRAND@@ anything…';
  if(liveMode){
    if(approvalMode || todo) lastStreamStatus=s.waiting_on||'Waiting on you';
    else if(s.busy) lastStreamStatus='Working…';
    else if(s.waiting_on) lastStreamStatus=s.waiting_on;
    else lastStreamStatus='Quiet for now';
  } else {
    lastStreamStatus='Reading a saved conversation.';
  }
  renderContextStream();
  if(liveMode && s.error && s.error!==lastErrShown){
    lastErrShown=s.error; add('error', s.error);
  }
  syncEmptyState();
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
 box.value=''; resizeBox(); persistChat();
 const dry=document.getElementById('dry').value||null;
 const mode=document.getElementById('studyMode').value||'general';
 const note=(ctxBox.value||'').trim();
 const attachCtx=pendingAttach.filter(a=>a.status==='ok'&&a.context)
   .map(a=>a.context).join('\n\n');
 const ctxParts=[note,attachCtx].filter(Boolean);
 const ctx=ctxParts.length?ctxParts.join('\n\n'):null;
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
syncEmptyGreeting();
syncEmptyState();
refreshStreamData();
startChatPoll();
poll();

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
  try{
    const s=await (await fetch('/agent/ghost/status')).json();
    if(s.fresh){
      ghostHideAt=Date.now()+30000;
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
