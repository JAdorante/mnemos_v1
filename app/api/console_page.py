"""Memory Console — timeline, search, provenance, confidence."""

from app.api.mnemos_theme import apply as _mnemos

CONSOLE_PAGE = _mnemos(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — Memory Console</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
@@KATEX@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;font:var(--fs-body)/var(--lh-body) var(--font);color:var(--text);
  height:100vh;display:flex;flex-direction:column;
  background:
    radial-gradient(900px 480px at 8% -10%, var(--acc-05), transparent 55%),
    radial-gradient(700px 400px at 94% 0%, rgba(30,91,79,.04), transparent 50%),
    var(--paper);
}
.layout{flex:1;display:grid;grid-template-columns:1fr minmax(180px,220px);min-height:0}
@media(max-width:860px){.layout{grid-template-columns:1fr}#consoleAmbient{display:none}}
#consoleAmbient{padding:var(--sp-4);padding-bottom:calc(var(--sp-4) + var(--dock-clear,72px));border-left:1px solid var(--hairline);overflow:auto}
#consoleAmbient h2{font:500 var(--fs-caption2)/1 var(--mono);letter-spacing:var(--track-caps);text-transform:uppercase;color:var(--mut);margin:0 0 var(--sp-3)}
/* First-brief / unlock toast docks here so it never covers the feed or RecBar. */
#mnemosToastSlot:empty{display:none}
#mnemosToastSlot .mnemos-toast--docked{
  position:relative!important;right:auto!important;bottom:auto!important;
  left:auto!important;max-width:100%!important;width:100%!important;
  margin:0 0 12px!important;z-index:var(--z-base)!important;box-sizing:border-box;
}
#constPane{display:none;flex:1;min-height:0;padding:12px 20px calc(20px + var(--dock-clear, 72px));align-items:center}
#constPane.on{display:flex;flex-direction:column}
#constPane .const-frame{
  position:relative;width:min(560px,100%);height:min(420px,62vh);margin:0 auto;
  border:1px solid var(--line);border-radius:var(--radius);background:var(--bg-elev);
  box-shadow:var(--shadow-surface);overflow:hidden;
}
#constPane canvas{width:100%;height:100%;display:block;touch-action:none}
.const-tools{
  position:absolute;right:10px;bottom:10px;z-index:var(--z-base);display:flex;gap:var(--sp-1);
  background:rgba(255,254,251,.92);border:1px solid var(--line);border-radius:var(--radius-sm);
  padding:var(--sp-1);box-shadow:var(--shadow-workspace);
}
.const-tools button{
  width:32px;height:28px;border:0;background:transparent;border-radius:var(--radius-xs);
  font:600 var(--fs-footnote) var(--font);color:var(--navy);cursor:pointer;padding:0;
  box-shadow:none;transform:none;
}
.const-tools button:hover{background:rgba(11,19,32,.045);transform:none;box-shadow:none}
.const-tools button[data-act="fit"],
.const-tools button[data-act="focus"],
.const-tools button[data-act="filaments"],
.const-tools button[data-act="correct"],
.const-tools button[data-act="diff"]{width:auto;padding:0 10px;font-size:var(--fs-caption);font-weight:500;color:var(--mut)}
.const-tools button.on{color:var(--navy);background:rgba(11,19,32,.06)}
.const-tip{
  position:absolute;z-index:var(--z-raised);max-width:200px;padding:var(--sp-2) var(--sp-3);
  border-radius:var(--radius-sm);
  background:rgba(255,254,251,.96);border:1px solid var(--line);box-shadow:var(--shadow-float);
  font-size:var(--fs-caption);pointer-events:none;line-height:var(--lh-snug);
}
.const-tip strong{display:block;font-family:var(--display);font-weight:400;font-size:1rem;color:var(--navy)}
.const-tip-kind{font:var(--fs-caption2) var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:var(--track-caps)}
.const-tip-why{margin-top:4px;color:var(--mut);font-style:italic}
.const-insight{
  /* bottom:52px, not 10px — the const-tools row owns the frame's bottom strip;
     insight cards stack in the clear band above it so neither covers the other
     however narrow the frame gets. */
  position:absolute;left:10px;bottom:52px;z-index:var(--z-base);max-width:min(320px,80%);
  display:flex;flex-direction:column;gap:6px;
}
.const-insight-btn{
  text-align:left;border:1px solid var(--line);background:rgba(255,254,251,.94);
  border-radius:var(--radius-sm);padding:var(--sp-2) var(--sp-3);
  font:var(--fs-caption)/var(--lh-snug) var(--font);color:var(--navy);cursor:pointer;
  box-shadow:var(--shadow-workspace);
}
.const-insight-btn:hover{border-color:var(--acc-40)}
.const-why{margin:6px 0;color:var(--mut);font-style:italic;line-height:1.4}
.const-rank{margin:8px 0 10px;padding-top:6px;border-top:1px solid var(--line)}
.const-rank-title{font:var(--fs-caption2) var(--mono);letter-spacing:var(--track-caps);text-transform:uppercase;
  color:var(--mut);margin-bottom:var(--sp-1)}
.const-rank-admit{color:var(--navy);font-style:italic;margin:0 0 6px;line-height:1.35}
.const-rank-bar{display:flex;height:8px;border-radius:4px;overflow:hidden;
  background:rgba(11,19,32,.06);gap:1px;margin:4px 0 2px}
.const-rank-seg{display:block;min-width:2px;height:100%}
.const-rank-total{font:var(--fs-caption2) var(--mono);color:var(--mut);margin-bottom:6px}
.const-rank-list{display:flex;flex-direction:column;gap:2px}
.const-rank-row{display:flex;justify-content:space-between;gap:8px;align-items:baseline;
  width:100%;text-align:left;border:0;background:transparent;padding:4px 0;
  cursor:pointer;font:var(--fs-caption) var(--font);color:var(--navy);border-radius:0}
.const-rank-row:hover{color:var(--acc)}
.const-rank-label{flex:1;line-height:1.35}
.const-rank-val{font:var(--fs-caption2) var(--mono);color:var(--mut);flex-shrink:0}
.const-rank-ev{padding:0 0 4px 2px;margin:0 0 4px}
.const-edit-actions{display:flex;gap:6px;margin:8px 0}
.const-ev-row{padding:6px 0;border-top:1px solid var(--line);line-height:1.35}
.const-ev-ch{font:var(--fs-caption2) var(--mono);color:var(--mut);text-transform:uppercase;letter-spacing:var(--track-caps)}
.const-ev-body{margin-top:4px}
.const-ev-transcript{line-height:1.45;color:var(--text)}
.const-ev-quote{margin-top:4px;font-style:italic;color:var(--mut)}
.const-ev-audio{display:block;width:100%;height:28px;margin-top:6px}
.const-play-moment{margin-top:6px}
mark.span-hl{
  background:var(--acc-22);color:inherit;padding:0 .12em;border-radius:2px;
}
.const-kind-select{
  width:100%;margin-top:var(--sp-1);padding:7px 10px;border-radius:var(--radius-xs);border:1px solid var(--line);
  background:var(--panel);color:var(--navy);font:var(--fs-footnote) var(--font);
}
.const-edit{
  position:absolute;left:10px;top:10px;z-index:var(--z-raised);width:min(260px,72%);
  background:rgba(255,254,251,.97);border:1px solid var(--line);border-radius:var(--radius-sm);
  box-shadow:var(--shadow-folio);padding:var(--sp-3);font-size:var(--fs-caption);max-height:78%;overflow:auto;
}
.const-edit-head{display:flex;justify-content:space-between;gap:8px;align-items:baseline;
  font-family:var(--display);font-size:1.05rem;color:var(--navy);margin-bottom:6px}
.const-edit-hint{color:var(--mut);font-style:italic;margin:4px 0 8px;line-height:1.4}
.const-edit-list{display:flex;flex-direction:column;gap:6px;margin-top:8px}
.const-edit-row{display:flex;justify-content:space-between;gap:8px;align-items:center;
  padding:6px 0;border-top:1px solid var(--line)}
.const-edit-row button,.const-link-btn{
  border:1px solid var(--line);background:var(--panel);border-radius:var(--radius-xs);padding:var(--sp-1) var(--sp-2);
  font:var(--fs-caption) var(--font);cursor:pointer;color:var(--navy);box-shadow:none;transform:none;
}
.const-edit-row button:hover{border-color:rgba(166,71,71,.45);color:var(--danger)}
.const-link-btn{width:100%;margin-top:4px}
.const-link-btn:hover{border-color:var(--acc-45);color:var(--acc)}
.const-edit .linkish{background:none;border:0;color:var(--mut);cursor:pointer;font:var(--fs-caption) var(--font);padding:0}
.const-frame.editing{box-shadow:var(--shadow-folio),inset 0 0 0 1px var(--acc-18)}
.mode-toggle{display:flex;gap:var(--sp-2);padding:0;align-items:center;flex-wrap:wrap}
.mode-toggle .chip.on{color:var(--navy);background:rgba(11,19,32,.06);border-color:rgba(11,19,32,.12)}
.mode-seg{display:inline-flex;gap:0;border:1px solid var(--line);border-radius:var(--radius-sm);overflow:hidden;flex:0 0 auto}
.mode-seg .chip{border:0;border-radius:0;margin:0}
.mode-seg .chip + .chip{border-left:1px solid var(--line)}
.chrome{border-bottom:1px solid var(--hairline);position:relative;z-index:var(--z-raised);background:var(--chrome-bg)}
.chrome-tools{
  display:flex;gap:var(--sp-2);align-items:center;flex-wrap:wrap;
  padding:var(--sp-2) var(--sp-5) var(--sp-3);background:var(--chrome-bg);
  border-bottom:1px solid var(--hairline);
  position:sticky;top:0;z-index:var(--z-raised);
  transition:transform var(--dur) var(--ease),opacity var(--dur) var(--ease);
}
.chrome-tools.tucked{transform:translateY(-110%);opacity:0;pointer-events:none}
.chrome-tools .tabs{display:flex;gap:var(--sp-2);flex:1;flex-wrap:wrap;padding:0;min-width:0}
.row{position:relative}
.row.holdable::before{
  content:"";position:absolute;left:0;top:18px;bottom:18px;width:2px;
  background:var(--acc);border-radius:2px;opacity:.85;
}
.top{
  display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap;
  padding:var(--sp-3) var(--sp-5) var(--sp-2);
}
.page-sub{margin-left:-4px}
.mut{color:var(--mut);font-size:var(--fs-footnote)}
.meta-bar{display:flex;gap:var(--sp-3);align-items:center;font-family:var(--mono);font-size:var(--fs-caption);color:var(--mut);letter-spacing:0}
input,button,select{font:inherit}
#q{
  background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:var(--radius-sm);padding:var(--sp-2) var(--sp-3);width:min(280px,100%);
  box-shadow:var(--shadow-workspace);
  transition:border-color var(--dur) var(--ease),box-shadow var(--dur) var(--ease);
}
#q:focus{outline:none;border-color:var(--acc-45);box-shadow:0 0 0 3px var(--acc-dim)}
.btn{
  background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:var(--radius-sm);padding:7px 12px;cursor:pointer;
  font-size:var(--fs-footnote);font-weight:500;letter-spacing:var(--track-snug);
}
.btn:hover{background:var(--bg-elev);border-color:var(--line-strong);color:var(--navy)}
.tabs{
  display:flex;gap:var(--sp-2);flex-wrap:wrap;align-items:center;
  padding:0;
}
.chip{
  display:inline-flex;align-items:center;min-height:30px;
  background:var(--panel);color:var(--mut);border:1px solid var(--line);
  border-radius:var(--radius-full);padding:5px 13px;cursor:pointer;
  font-size:var(--fs-footnote);font-weight:500;letter-spacing:var(--track-snug);
  transition:background var(--dur-fast) var(--ease-io),border-color var(--dur-fast) var(--ease-io),
    color var(--dur-fast) var(--ease-io),transform var(--dur-fast) var(--ease-io);
}
.chip:hover{
  color:var(--navy);border-color:var(--line-strong);background:rgba(11,19,32,.03);
}
.chip:active{transform:scale(.97)}
.chip.on{background:rgba(11,19,32,.06);color:var(--navy);border-color:rgba(11,19,32,.12)}
.chip.on:hover{color:var(--navy);background:rgba(11,19,32,.08)}
.mode-caption{font:var(--fs-caption) var(--font);color:var(--mut);padding:2px 12px 6px;font-style:italic}
.ambient-note.actionable{cursor:pointer;background:transparent;border:0;text-align:left;width:100%;
  font:inherit;color:inherit;padding:0;display:block}
.ambient-note.actionable:hover{color:var(--acc)}
.ambient-act{display:block;margin-top:3px;font:var(--fs-caption2) var(--mono);color:var(--mut)}
#list{
  /* Extra bottom pad clears the fixed #mnemosRecBar (privacy + voice chips). */
  flex:1;overflow:auto;padding:var(--sp-4) var(--sp-5) calc(28px + var(--dock-clear, 72px));
  display:flex;flex-direction:column;gap:var(--sp-3);max-width:960px;width:100%;
  margin:0 auto;align-self:center;
}
.row{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:var(--sp-3) var(--sp-4);display:flex;gap:var(--sp-3);align-items:flex-start;
  box-shadow:var(--shadow-workspace);animation:fadeUp var(--dur) var(--ease) both;
  position:relative;
  transition:border-color var(--dur) var(--ease),background var(--dur) var(--ease);
}
.row:hover{
  border-color:var(--line-strong);
  background:color-mix(in srgb,var(--navy) 2%,var(--panel));
}
.row::before{
  content:"";position:absolute;left:0;top:18px;bottom:18px;width:2px;
  background:var(--acc);border-radius:2px;
  opacity:.85;
}
.row.low{border-color:rgba(199,138,44,.35);background:rgba(199,138,44,.05)}
.badge{
  font:600 var(--fs-caption2)/1.4 var(--mono);padding:3px 8px;border-radius:var(--radius-xs);
  white-space:nowrap;margin-top:2px;letter-spacing:var(--track-caps);text-transform:uppercase;
}
.b-audio{background:rgba(30,91,79,.1);color:var(--audio)}
.b-vision{background:var(--acc-12);color:var(--vision)}
.b-desktop{background:rgba(110,36,51,.1);color:var(--desktop)}
.b-other{background:var(--panel-2);color:var(--mut)}
.actev{margin-top:var(--sp-2);display:flex;flex-direction:column;gap:var(--sp-2)}
.actev .row{background:var(--bg-elev)}
.actev .row::before{opacity:.25}
.body{flex:1;min-width:0}.text{white-space:pre-wrap;word-wrap:break-word;overflow-wrap:anywhere}
.meta{
  margin-top:var(--sp-2);font-size:var(--fs-caption);color:var(--mut);font-family:var(--mono);
  display:flex;gap:var(--sp-2);flex-wrap:wrap;align-items:center;
}
.spk{color:var(--emerald);font-family:var(--font)}.lowtag{color:var(--warn)}
audio{height:30px;margin-top:var(--sp-2);max-width:320px;display:block}
img.thumb{margin-top:var(--sp-2);max-height:120px;border-radius:var(--radius-sm);border:1px solid var(--line);cursor:zoom-in}
img.thumb.big{max-height:none;max-width:100%}
.empty{color:var(--mut);text-align:center;margin:var(--sp-14) var(--sp-4);line-height:var(--lh-loose)}
.prov{
  margin-top:var(--sp-2);padding:var(--sp-2) var(--sp-3);border-left:2px solid var(--acc);
  color:var(--mut);font-size:var(--fs-footnote);font-style:italic;background:var(--acc-05);
  border-radius:0 var(--radius-sm) var(--radius-sm) 0;
}
.prov audio{height:28px;margin-top:6px;font-style:normal;display:block;width:100%}
.prov .span-transcript{font-style:normal;color:var(--text);line-height:1.45}
.prov mark.span-hl{
  background:var(--acc-22);color:inherit;padding:0 .12em;border-radius:2px;font-style:normal;
}
.prov .play-moment{
  display:inline-block;margin-top:6px;border:1px solid var(--line);background:var(--panel);
  border-radius:var(--radius-xs);padding:var(--sp-1) var(--sp-3);
  font:500 var(--fs-caption) var(--font);cursor:pointer;color:var(--navy);
  font-style:normal;
}
.prov .play-moment:hover{border-color:var(--acc-45);color:var(--acc)}
.acts{margin-top:var(--sp-3);display:flex;gap:var(--sp-2);flex-wrap:wrap}
.mini{
  border:1px solid var(--line);background:var(--bg-elev);color:var(--text);
  border-radius:var(--radius-sm);padding:5px 12px;cursor:pointer;
  font-size:var(--fs-footnote);font-weight:500;letter-spacing:var(--track-snug);
}
.mini:hover{border-color:var(--line-strong);background:rgba(11,19,32,.03);color:var(--navy)}
.mini.done:hover{
  border-color:rgba(46,111,87,.5);color:var(--ok);background:rgba(46,111,87,.08);
}
.mini.drop:hover{
  border-color:rgba(166,71,71,.5);color:var(--danger);background:rgba(166,71,71,.08);
}
.sechead{
  color:var(--mut);font:500 var(--fs-caption2)/1.4 var(--mono);text-transform:uppercase;
  letter-spacing:var(--track-caps);margin:var(--sp-3) 2px var(--sp-1);
}
.rev{color:var(--ok);text-transform:uppercase;font-size:var(--fs-caption2);letter-spacing:var(--track-caps)}
.refl{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:var(--sp-4);margin-bottom:var(--sp-2);box-shadow:var(--shadow-workspace);
  animation:fadeUp var(--dur) var(--ease) both;
}
.refl .sum{font-size:var(--fs-body);margin-top:var(--sp-2)}
.kind{
  font:600 var(--fs-caption2)/1.4 var(--mono);padding:3px 8px;border-radius:var(--radius-xs);
  background:var(--panel-2);color:var(--text);text-transform:uppercase;
  letter-spacing:var(--track-caps);white-space:nowrap;margin-top:2px;
}
.k-recommendation,.k-open_loop{background:rgba(199,138,44,.12);color:var(--warn)}
.k-risk{background:rgba(166,71,71,.1);color:var(--danger)}
.k-pattern,.k-change{background:rgba(30,91,79,.1);color:var(--audio)}
.k-policy,.k-project_update,.k-relationship_update{background:var(--acc-10);color:var(--vision)}
.detail{color:var(--mut);font-size:var(--fs-footnote);margin-top:3px}
.ev{margin-top:7px;font-size:var(--fs-caption);color:var(--mut)}
.ev .evrow{padding:2px 0 2px 10px;border-left:2px solid var(--acc-35);margin-top:2px}
.hgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:var(--sp-3)}
.hcard{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:var(--sp-4);box-shadow:var(--shadow-workspace);animation:fadeUp var(--dur) var(--ease) both;
}
.hlabel{color:var(--mut);font:500 var(--fs-caption2)/1.4 var(--mono);text-transform:uppercase;letter-spacing:var(--track-caps)}
.hval{font-family:var(--display);font-size:var(--fs-title1);font-weight:400;margin:var(--sp-2) 0 var(--sp-1);letter-spacing:var(--track-tight);color:var(--navy)}
.hsub{color:var(--mut);font-size:var(--fs-caption);display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.pill{background:var(--panel-2);border:1px solid var(--line);border-radius:var(--radius-full);padding:2px 9px;font-size:var(--fs-caption)}
.dead-jobs{font-size:var(--fs-caption);color:var(--warn);max-width:280px}
.dead-jobs summary{cursor:pointer;list-style:none;white-space:nowrap}
.dead-jobs-list{margin-top:6px;padding:var(--sp-2) var(--sp-3);background:var(--panel);border:1px solid var(--line);
  border-radius:var(--radius-sm);max-height:180px;overflow:auto;font-size:var(--fs-caption2);color:var(--text);
  box-shadow:var(--shadow-float);position:absolute;z-index:var(--z-popover);min-width:260px}
.dead-jobs-list .dj{padding:4px 0;border-bottom:1px solid var(--line)}
.dead-jobs-list .dj:last-child{border-bottom:0}
.dead-jobs-list .dj .err{color:var(--mut);display:block;word-break:break-word}
@media(max-width:720px){
  .top{padding:var(--sp-2) var(--sp-4);gap:var(--sp-2)}
  #q{width:100%;order:6;flex:1 1 100%}
  .meta-bar{width:100%;flex-wrap:wrap;gap:var(--sp-2)}
  .chrome-tools{padding:var(--sp-2) var(--sp-4) var(--sp-3)}
  #list{padding:var(--sp-4) var(--sp-4) calc(24px + var(--dock-clear, 72px))}
  .row{flex-wrap:wrap;gap:var(--sp-2);padding:var(--sp-3) var(--sp-4)}
  .badge{margin-top:0}
  audio{max-width:100%}
  .dead-jobs-list{position:static;min-width:0;width:100%}
  #constPane{padding:10px 12px 16px}
  #constPane .const-frame{height:min(360px,52vh)}
}
</style></head><body>
<div class="chrome">
  <div class="top">
    <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
    <span class="page-sub">Memory</span>
    @@NAV@@
    <input id="q" placeholder="search memories by meaning…">
    <span class="spacer"></span>
    <div class="meta-bar">
      <span id="jobs" title="background worker"></span>
      <details id="deadJobsBox" class="dead-jobs" style="display:none">
        <summary id="deadJobsSummary">Dead-letter</summary>
        <div id="deadJobsList" class="dead-jobs-list"></div>
      </details>
      <span id="stat"></span>
    </div>
    <button class="btn" id="rebuild" onclick="rebuild()" style="display:none">Rebuild turns</button>
    <button class="btn" id="reflectrun" onclick="runReflect()" style="display:none">Run reflection</button>
    <button class="btn" onclick="load()">Refresh</button>
  </div>
  @@APPROVAL@@
  <div class="chrome-tools" id="chromeTools">
    <div class="mode-seg mode-toggle">
      <span class="chip on" id="modeArchive" onclick="setLayer('archive')">Archive</span>
      <span class="chip" id="modeConst" onclick="setLayer('constellation')">Constellation</span>
    </div>
    <div class="tabs" id="archiveTabs">
      <span class="chip on" data-mod="" onclick="pickMod(this)">All</span>
      <span class="chip" data-mod="audio" onclick="pickMod(this)">Audio</span>
      <span class="chip" data-mod="vision" onclick="pickMod(this)">Vision</span>
      <span class="chip" data-mod="" data-source="desktop." onclick="pickMod(this)">Desktop</span>
      <span class="chip" id="actchip" onclick="pickActivity()">Activity</span>
      <span class="chip" id="turnchip" onclick="pickTurns()">Turns</span>
      <span class="chip" id="sesschip" onclick="pickSessions()">Sessions</span>
      <span class="chip" id="factchip" onclick="pickFacts()">Tasks</span>
      <span class="chip" id="reflectchip" onclick="pickReflect()">Reflection</span>
      <span class="chip" id="attnchip" onclick="pickAttention()">Attention</span>
      <span class="chip" id="egresschip" onclick="pickEgress()">Egress</span>
      <span class="chip" id="healthchip" onclick="pickHealth()">Audio Health</span>
      <span class="chip" id="learnchip" onclick="pickLearning()">Learning</span>
      <span class="chip" id="lowchip" onclick="toggleLow()">Low-confidence</span>
    </div>
  </div>
</div>
<div class="layout">
<div style="display:flex;flex-direction:column;min-height:0;min-width:0;flex:1">
<div id="list"><div class="empty">loading…</div></div>
<div id="constPane"><div id="horizonStrip" class="hsub" style="padding:8px 12px 0;gap:8px;flex-wrap:wrap"></div><div id="modeChips" class="hsub" style="padding:6px 12px 0;gap:6px"></div><div class="const-frame"><canvas id="memConst"></canvas></div></div>
</div>
<aside id="consoleAmbient"><h2>In the margin</h2><div id="mnemosToastSlot"></div><div id="ambientBox"></div></aside>
</div>
@@UI_JS@@
<script>
 let mod="", src="", low=false, view="raw", timer=null, layer="archive", constCtl=null;
 let _archiveSig=null, _jobsSig=null;
MnemosMemory.set('lastRoute','/memory');
(function restoreConsole(){
  const st=MnemosMemory.get('console',{});
  if(st.q) document.getElementById('q').value=st.q;
  if(st.layer==='constellation') layer='constellation';
  if(st.view) view=st.view;
  if(st.mod!=null) mod=st.mod;
  if(st.src!=null) src=st.src;
  if(st.low) low=!!st.low;
  try{
    const qp=new URLSearchParams(location.search);
    const m=qp.get('mode')||qp.get('layer');
    if(m==='constellation') layer='constellation';
    else if(m==='archive') layer='archive';
  }catch(e){}
})();
function persistConsole(){
  MnemosMemory.set('console',{q:document.getElementById('q').value,layer,view,mod,src,low,
    expanded:MnemosMemory.get('console.expanded',[])});
}
function setLayer(name){
  layer=name; persistConsole();
  document.getElementById('modeArchive').classList.toggle('on', layer==='archive');
  document.getElementById('modeConst').classList.toggle('on', layer==='constellation');
  document.getElementById('archiveTabs').style.display=layer==='archive'?'flex':'none';
  document.getElementById('list').style.display=layer==='archive'?'flex':'none';
  document.getElementById('constPane').classList.toggle('on', layer==='constellation');
  try{
    const u=new URL(location.href);
    if(layer==='constellation') u.searchParams.set('mode','constellation');
    else u.searchParams.delete('mode');
    history.replaceState(null,'',u.pathname+(u.search||'')+(u.hash||''));
  }catch(e){}
  if(layer==='constellation') loadConstellation();
}
let constVersion=null, constPoll=null, constStreamOn=false, constLoading=false;
async function constCheck(){
  if(document.hidden) return;
  if(layer!=='constellation') return;
  try{
    const v=(await (await fetch('/graph/version')).json()).version;
    if(constVersion!==null && v!==constVersion) await loadConstellation();
    constVersion=v;
  }catch(e){}
}
function renderHorizonStrip(data){
  const host=document.getElementById('horizonStrip');
  if(!host) return;
  const hz=(data&&data.horizon)||{};
  const items=hz.items||[];
  if(!items.length){
    host.innerHTML='<span class="mut" style="font-size:12px">Horizon quiet</span>';
    return;
  }
  host.innerHTML=items.map(it=>{
    const why=(it.reason&&it.reason[0])?it.reason[0]:'';
    return '<span class="pill" title="'+MnemosEsc(why)+'" style="cursor:pointer" data-hid="'
      +MnemosEsc(it.id||'')+'">'
      +'<b>in '+(it.when_label||'?')+'</b> · '+MnemosEsc(it.label||it.id||'')
      +' <span class="mut" style="margin-left:4px">×</span></span>';
  }).join('');
  host.querySelectorAll('[data-hid]').forEach(el=>{
    el.onclick=async()=>{
      const id=el.getAttribute('data-hid');
      if(!id) return;
      try{
        await fetch('/field/feedback',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id, outcome:'dismiss'})});
      }catch(e){}
      loadConstellation();
    };
  });
}
function renderModeChips(data){
  const host=document.getElementById('modeChips');
  if(!host) return;
  const cur=(data&&data.mode)||{};
  const modes=(data&&data.modes)||[];
  if(!modes.length){ host.innerHTML=''; return; }
  let cap=document.getElementById('modeCaption');
  if(!cap){
    cap=document.createElement('div');
    cap.id='modeCaption';
    cap.className='mode-caption';
    host.parentElement.insertBefore(cap, host.nextSibling);
  }
  const label=cur.label||(cur.id?String(cur.id):'Auto');
  const src=cur.source==='manual'?'':(cur.source?(' · '+cur.source):'');
  cap.textContent='Ranking for: '+label+src;
  host.innerHTML=modes.map(m=>{
    const on=m.id===cur.id;
    return '<button type="button" class="chip'+(on?' on':'')+'" data-mode="'+m.id+'" title="Reweights gravity for this context — does not filter">'
      +MnemosEsc(m.label||m.id)+'</button>';
  }).join('')
    +'<button type="button" class="chip'+(cur.source!=='manual'?' on':'')+'" data-mode="auto" title="Infer context from recent events">Auto</button>';
  host.querySelectorAll('[data-mode]').forEach(btn=>{
    btn.onclick=async()=>{
      try{
        await fetch('/field/mode',{method:'POST',
          headers:{'Content-Type':'application/json'},
          body:JSON.stringify({mode:btn.getAttribute('data-mode')})});
      }catch(e){}
      loadConstellation();
    };
  });
}
async function loadConstellation(){
  if(constLoading) return;
  constLoading=true;
  try{
  const data=await (await fetch('/field/state?limit=28')).json();
  try{
    const v=(await (await fetch('/graph/version')).json()).version;
    constVersion=v;
  }catch(e){}
  renderHorizonStrip(data);
  renderModeChips(data);
  if(!constStreamOn && window.MnemosFieldStream){
    constStreamOn=!!MnemosFieldStream.connect((d)=>{
      if(layer!=='constellation') return;
      if(d.version!=null) constVersion=d.version;
      loadConstellation();
    });
    if(typeof startJobsPoll==='function') startJobsPoll();
  }
  // Poll is fallback; slow down when SSE is live.
  if(!constPoll) constPoll=setInterval(constCheck, constStreamOn?20000:4000);
  if(constCtl){ constCtl.update(data); return; }
  constCtl=MnemosConstellation.mount(document.getElementById('memConst'), data, {
    persistKey:'console.constellation.cam',
    onSelect(node){
      if(!node) return;
      if(node.kind==='person'){
        const fav=new Set(MnemosMemory.get('favoritePeople',[])||[]);
        if(fav.has(node.id)) fav.delete(node.id); else fav.add(node.id);
        MnemosMemory.set('favoritePeople',[...fav]);
      }
    }
  });
  }finally{ constLoading=false; }
}
async function loadAmbient(){
  try{
    const intel=await (await fetch('/home/intelligence')).json();
    MnemosAmbient.render(document.getElementById('ambientBox'), intel.ambient||[], {
      constellation: constCtl,
    });
  }catch(e){
    MnemosAmbient.render(document.getElementById('ambientBox'), [{text:'Listening…'}]);
  }
}
async function revealProvenance(rowEl){
  const eid=rowEl && rowEl.dataset && rowEl.dataset.eventId;
  if(!eid) return;
  let host=rowEl.querySelector('.prov-host');
  if(!host){ host=document.createElement('div'); host.className='prov-host'; rowEl.appendChild(host); }
  if(host.dataset.open==='1'){ host.innerHTML=''; host.dataset.open='0'; return; }
  host.dataset.open='1';
  host.innerHTML='<div class="meta">Revealing…</div>';
  try{
    const j=await (await fetch('/console/provenance/'+eid)).json();
    const chain=j.chain||{};
    const audio=chain.enhanced_audio||chain.raw_audio||'';
    const corr=(chain.corrections||[]).map(c=>
      (c.stage||'')+((c.before||c.after)?(' “'+(c.before||'')+'” → “'+(c.after||'')+'”'):'')
      +(c.note?(' — '+c.note):'')).join('\n')||'none (verbatim as captured)';
    const steps=[
      {label:'Conversation', body:j.summary?JSON.stringify(j.summary):'utterance '+eid},
      {label:'Audio clip', html: audio ? ('<audio controls src="/artifact?path='+encodeURIComponent(audio)+'"></audio>') : '—'},
      {label:'Transcript', body:chain.transcript||'—'},
      {label:'Visual frame', body:'—'},
      {label:'Reasoning', body:corr},
      {label:'Confidence', body: (chain.capture_quality||'—')+(chain.snr_est!=null?(' · SNR '+chain.snr_est+'dB'):'')},
      {label:'Model used', body:chain.asr_prompt?('ASR bias applied'):'capture pipeline'},
      {label:'Timestamp', body:chain.captured_at?new Date(chain.captured_at*1000).toLocaleString():'—'},
      {label:'Source', body:j.rendered||chain.raw_audio||'—'},
    ];
    MnemosBleed.renderStack(host, steps);
    const exp=new Set(MnemosMemory.get('console.expanded',[])||[]);
    exp.add(String(eid)); MnemosMemory.set('console.expanded',[...exp]);
  }catch(e){
    host.innerHTML='<div class="meta">No provenance chain for this row.</div>';
  }
}
const q=document.getElementById('q'), list=document.getElementById('list');
function setViewUI(){
 document.getElementById('actchip').classList.toggle('on',view==="activity");
 document.getElementById('turnchip').classList.toggle('on',view==="turns");
 document.getElementById('sesschip').classList.toggle('on',view==="sessions");
 document.getElementById('factchip').classList.toggle('on',view==="facts");
 document.getElementById('reflectchip').classList.toggle('on',view==="reflect");
 document.getElementById('attnchip').classList.toggle('on',view==="attention");
 document.getElementById('egresschip').classList.toggle('on',view==="egress");
 document.getElementById('healthchip').classList.toggle('on',view==="health");
 document.getElementById('learnchip').classList.toggle('on',view==="learning");
 const rb=document.getElementById('rebuild');
 rb.style.display=(view==="turns"||view==="activity"||view==="sessions")?'inline-block':'none';
 rb.textContent=view==="activity"?'Rebuild activity':view==="sessions"?'Rebuild sessions':'Rebuild turns';
 document.getElementById('reflectrun').style.display=view==="reflect"?'inline-block':'none';
 q.style.display=(view==="raw")?'inline-block':'none';
}
function pickMod(el){view="raw";mod=el.dataset.mod;src=el.dataset.source||"";setViewUI();
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.toggle('on',c===el));load();}
function pickTurns(){view="turns";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickActivity(){view="activity";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickSessions(){view="sessions";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickFacts(){view="facts";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickReflect(){view="reflect";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));setViewUI();load();}
function pickAttention(){view="attention";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('attnchip').classList.add('on');setViewUI();load();}
function pickEgress(){view="egress";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('egresschip').classList.add('on');setViewUI();load();}
function pickHealth(){view="health";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('healthchip').classList.add('on');setViewUI();load();}
function pickLearning(){view="learning";
 document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
 document.getElementById('learnchip').classList.add('on');setViewUI();load();}
function toggleLow(){low=!low;document.getElementById('lowchip').classList.toggle('on',low);
 if(view!=="raw"){view="raw";setViewUI();}load();}
async function rebuild(){document.getElementById('stat').textContent='rebuilding…';
 const ep=view==="activity"?'/console/activity/rebuild'
   :view==="sessions"?'/console/sessions/rebuild':'/console/consolidate';
 await fetch(ep,{method:'POST'});load();}
async function factAction(fact_id,verb){
 await fetch('/facts/'+fact_id+'/'+verb,{method:'POST'});
 loadFacts();
}
async function factEdit(fact_id,current){
 const t=prompt('Edit this fact:',current); if(t==null) return;
 await fetch('/facts/'+fact_id+'/edit',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({text:t})});
 loadFacts();
}
function fmtTime(t){if(!t)return'';const d=new Date(t*1000);
 return d.toLocaleDateString([], {month:'short',day:'numeric'})+' '+d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function conf(c){return (c==null)?'':('conf '+Number(c).toFixed(2));}
function art(p){return '/artifact?path='+encodeURIComponent(p);}
function row(e){
 // Desktop-capture rows span two modalities (screen=vision, click=input) —
 // badge them by source so the Desktop tab (and the All view) reads at a glance.
 const desk=(e.source||'').startsWith('desktop.');
 const cls=desk?'b-desktop':(e.modality==='audio')?'b-audio':(e.modality==='vision')?'b-vision':'b-other';
 const label=desk?(e.modality==='input'?'click':'screen'):(e.modality||'?');
 let media='';
 if(e.audio_path) media='<audio controls preload="none" src="'+art(e.audio_path)+'"></audio>';
 if(e.enhanced_audio) media+='<audio controls preload="none" title="enhanced (what Whisper heard)" src="'+art(e.enhanced_audio)+'"></audio>';
 if(e.frame_path) media+='<img class="thumb" alt="" src="'+art(e.frame_path)+'" onclick="this.classList.toggle(\'big\')">';
 const bits=[];
 if(e.speaker) bits.push('<span class="spk">'+MnemosEsc(e.speaker)+(e.speaker_profile?(' · '+MnemosEsc(e.speaker_profile)):'')+'</span>');
 if(desk&&e.window) bits.push('<span class="spk" title="'+MnemosEsc(e.window)+'">'
   +MnemosEsc(e.window.length>48?e.window.slice(0,45)+'…':e.window)+'</span>');
 bits.push(fmtTime(e.time));
 if(e.confidence!=null) bits.push(conf(e.confidence));
 if(e.score!=null) bits.push('match '+Number(e.score).toFixed(2));
 if(e.utterance_type) bits.push('<span class="spk">'+(e.utterance_type==='command'?'⌘ command':'✎ dictation')+'</span>');
 if(e.vision_provider) bits.push('<span class="pill" title="'+MnemosEsc(e.vision_route||'')+'">'+MnemosEsc(e.vision_provider)+'</span>');
 if(e.provenance&&e.provenance.n_corrections) bits.push('<span class="spk" title="'+MnemosEsc(e.provenance_detail||'')+'">🔗 '+e.provenance.n_corrections+' fix'+(e.provenance.n_corrections!=1?'es':'')+'</span>');
 if(e.needs_review) bits.push('<span class="lowtag">⚠ needs review</span>');
 if(e.skipped) bits.push('<span class="lowtag">audio-only ('+MnemosEsc(e.skipped)+')</span>');
 if(e.low_confidence&&!e.needs_review) bits.push('<span class="lowtag">⚠ low ('+MnemosEsc(e.quality_reason||'')+')</span>');
 return '<div class="row'+(e.low_confidence||e.skipped?' low':'')+'" data-event-id="'+(e.id||'')+'">'
  +'<span class="badge '+cls+'">'+MnemosEsc(label)+'</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(e.text||'(empty)')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+media
  +'<div class="prov-host"></div></div></div>';
}
function bindBleedRows(){
  list.querySelectorAll('.row[data-event-id]').forEach(el=>{
    MnemosBleed.bind(el, revealProvenance);
    const exp=MnemosMemory.get('console.expanded',[])||[];
    if(exp.includes(String(el.dataset.eventId))) revealProvenance(el);
  });
}
function fmtDur(s){if(s==null)return'';s=Math.round(s);
 return s>=3600?(Math.floor(s/3600)+'h '+Math.round((s%3600)/60)+'m')
   :s>=60?(Math.floor(s/60)+'m '+(s%60)+'s'):(s+'s');}
function endTime(t){return t?new Date(t*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'}):'';}
// One "what was I doing?" block: app + focus windows + fold counts + summary,
// expandable to the underlying screen/click events (thumbs included).
function actRow(a){
 const bits=[fmtTime(a.start)+(a.end?(' → '+endTime(a.end)):'')];
 if(a.duration_s!=null) bits.push(fmtDur(a.duration_s));
 const counts=[];
 if(a.n_screens!=null) counts.push(a.n_screens+' screen'+(a.n_screens===1?'':'s'));
 if(a.n_clicks!=null) counts.push(a.n_clicks+' click'+(a.n_clicks===1?'':'s'));
 // Optional multimodal counts (Part 2 contract) — render only if present.
 if(a.n_audio!=null) counts.push(a.n_audio+' audio');
 if(a.n_webcam!=null) counts.push(a.n_webcam+' webcam');
 if(counts.length) bits.push(counts.join(' · '));
 if(a.modalities&&a.modalities.length) bits.push('<span class="spk">'+a.modalities.map(esc).join(' + ')+'</span>');
 const wins=(a.windows||[]).map(w=>'<span class="pill" title="'+MnemosEsc(w)+'">'
   +MnemosEsc(w.length>64?w.slice(0,61)+'…':w)+'</span>').join('');
 const ids=(a.event_ids||[]);
 const expand=ids.length?'<div class="acts"><button class="mini" onclick="actExpand(this,\''+ids.join(',')+'\')">▸ '
   +ids.length+' linked event'+(ids.length===1?'':'s')+'</button></div><div class="actev"></div>':'';
 return '<div class="row"><span class="badge b-desktop">'+MnemosEsc(a.app||'desktop')+'</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(a.summary||'(no summary)')+'</div>'
  +(wins?'<div class="meta">'+wins+'</div>':'')
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+expand+'</div></div>';
}
async function actExpand(btn,ids){
 const box=btn.parentElement.nextElementSibling;
 if(box.dataset.open==='1'){box.dataset.open='0';box.innerHTML='';
   btn.innerHTML=btn.innerHTML.replace('▾','▸');return;}
 box.dataset.open='1';btn.innerHTML=btn.innerHTML.replace('▸','▾');
 box.innerHTML='<div class="empty" style="margin:12px">loading…</div>';
 try{
  const j=await (await fetch('/console/activity/events?ids='+ids)).json();
  box.innerHTML=j.events.length?j.events.map(row).join('')
    :'<div class="empty" style="margin:12px">no linked events found.</div>';
 }catch(e){ box.innerHTML='<div class="empty" style="margin:12px">error: '+e+'</div>'; }
}
async function loadActivity(){
 try{
  const j=await (await fetch('/console/activity?limit=200')).json();
  const acts=j.activities||[];
  document.getElementById('stat').textContent=j.count+' activit'+(j.count===1?'y':'ies');
  list.innerHTML=acts.length?acts.map(actRow).join('')
    :'<div class="empty">no activity blocks yet — desktop capture folds them as you work.<br>Click “Rebuild activity” to fold existing desktop events.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function sessRow(s){
 const bits=[];
 if(s.speakers&&s.speakers.length) bits.push('<span class="spk">'+s.speakers.map(esc).join(', ')+'</span>');
 bits.push(fmtTime(s.start)+(s.end?(' → '+endTime(s.end)):''));
 if(s.duration_s!=null) bits.push(fmtDur(s.duration_s));
 if(s.n_turns!=null) bits.push(s.n_turns+' turn'+(s.n_turns===1?'':'s')
   +(s.n_utterances!=null?(' · '+s.n_utterances+' utterance'+(s.n_utterances===1?'':'s')):''));
 return '<div class="row"><span class="badge b-audio">session</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(s.text||'(empty)')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div></div></div>';
}
async function loadSessions(){
 try{
  const j=await (await fetch('/console/sessions?limit=200')).json();
  const rows=j.sessions||[];
  document.getElementById('stat').textContent=j.count+' session'+(j.count===1?'':'s');
  list.innerHTML=rows.length?rows.map(sessRow).join('')
    :'<div class="empty">no sessions yet — click “Rebuild sessions”.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function turnRow(t){
 const clips=(t.audio_paths||[]).map(p=>'<audio controls preload="none" src="'+art(p)+'"></audio>').join('');
 const range=fmtTime(t.start)+(t.n_utterances>1?(' → '+new Date(t.end*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit',second:'2-digit'})):'');
 const bits=[];
 if(t.speaker) bits.push('<span class="spk">'+MnemosEsc(t.speaker)+'</span>');
 bits.push(range);
 bits.push(t.n_utterances+' utterance'+(t.n_utterances===1?'':'s'));
 if(t.duration_s) bits.push(t.duration_s+'s');
 return '<div class="row"><span class="badge b-audio">turn</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(t.text||'(empty)')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+clips+'</div></div>';
}
async function loadTurns(){
 try{
  const r=await fetch('/console/turns?limit=300'); const j=await r.json();
  document.getElementById('stat').textContent=j.count+' turns';
  list.innerHTML = j.turns.length ? j.turns.map(turnRow).join('')
    : '<div class="empty">no turns yet — click “Rebuild turns”.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function factRow(f){
 const kind=f.kind;
 const parties = kind==='commitment'
   ? [f.from_person, f.to_person&&('→ '+f.to_person)].filter(Boolean).join(' ')
   : (f.owner||'');
 const bits=[];
 if(parties) bits.push('<span class="spk">'+MnemosEsc(parties)+'</span>');
 if(f.due) bits.push('due '+MnemosEsc(f.due));
 if(f.confidence!=null) bits.push('conf '+Number(f.confidence).toFixed(2));
 if(f.source) bits.push(MnemosEsc(f.source));
 if(f.review) bits.push('<span class="rev">'+MnemosEsc(f.review)+'</span>');
 const play = f.play_path || f.enhanced_audio || f.source_audio;
 const aid = 'fact-audio-'+f.fact_id;
 let transcriptHtml = '';
 if(f.span_highlight && f.span_highlight.match!=null){
  const hl=f.span_highlight;
  transcriptHtml = '<div class="span-transcript">'
   +MnemosEsc(hl.before||'')+'<mark class="span-hl">'+MnemosEsc(hl.match||'')+'</mark>'
   +MnemosEsc(hl.after||'')+'</div>';
 } else if(f.source_span){
  transcriptHtml = '“'+MnemosEsc(f.source_span)+'”';
 }
 const clip = play
  ? ('<button type="button" class="play-moment" onclick="playFactMoment(\''+aid+'\')">Play the moment</button>'
     +'<audio id="'+aid+'" controls preload="none" src="'+art(play)+'"></audio>')
  : '';
 const prov = (transcriptHtml||clip) ? '<div class="prov">'+transcriptHtml+clip+'</div>' : '';
 const badge = kind==='commitment' ? 'b-vision' : 'b-audio';
 const t=(f.text||'').replace(/'/g,"\\'");
 return '<div class="row"><span class="badge '+badge+'">'+MnemosEsc(kind)+'</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(f.text||'')+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+prov
  +'<div class="acts">'
  +'<button class="mini done" onclick="factAction('+f.fact_id+',\'approve\')">✓ Approve</button>'
  +'<button class="mini done" onclick="factAction('+f.fact_id+',\'done\')">● Done</button>'
  +'<button class="mini" onclick="factEdit('+f.fact_id+',\''+t+'\')">✎ Edit</button>'
  +'<button class="mini drop" onclick="factAction('+f.fact_id+',\'dismiss\')">✕ Dismiss</button>'
  +'</div></div></div>';
}
function playFactMoment(aid){
 const audio=document.getElementById(aid);
 if(!audio) return;
 try{ audio.play(); }catch(e){}
 audio.scrollIntoView({block:'nearest'});
}
async function loadFacts(){
 try{
  // The review queue: open facts not yet dismissed. Newest first.
  const j=await (await fetch('/facts?status=open&limit=300')).json();
  const facts=j.facts||[];
  const tasks=facts.filter(f=>f.kind==='task');
  const comms=facts.filter(f=>f.kind==='commitment');
  document.getElementById('stat').textContent=
    tasks.length+' tasks · '+comms.length+' commitments';
  const sec=(title,arr)=> arr.length
    ? '<div class="sechead">'+title+'</div>'+arr.map(factRow).join('') : '';
  const html=sec('Tasks',tasks)+sec('Commitments',comms);
  list.innerHTML = html || '<div class="empty">no open tasks or commitments yet — '
    +'they appear here as @@BRAND@@ extracts them from conversation.</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
function reflItem(it){
 const bits=[];
 if(it.subject) bits.push('<span class="spk">'+MnemosEsc(it.subject)+'</span>');
 if(it.confidence!=null) bits.push('conf '+Number(it.confidence).toFixed(2));
 if(it.review) bits.push('<span class="rev">'+MnemosEsc(it.review)+'</span>');
 if(it.converted_fact_id) bits.push('→ task #'+it.converted_fact_id);
 const detail = it.detail ? '<div class="detail">'+MnemosEsc(it.detail)+'</div>' : '';
 const ev = (it.evidence&&it.evidence.length)
   ? '<div class="ev">evidence:'+it.evidence.map(e=>'<div class="evrow">['+e.fact_id+'] '
       +MnemosEsc(e.text)+(e.source?(' · '+MnemosEsc(e.source)):'')+'</div>').join('')+'</div>' : '';
 const t=(it.text||'').replace(/'/g,"\\'");
 const conv = it.converted_fact_id ? ''
   : '<button class="mini done" onclick="itemConvert('+it.id+')">→ Task</button>';
 return '<div class="row"><span class="kind k-'+MnemosEsc(it.kind)+'">'+MnemosEsc(it.kind)+'</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(it.text||'')+'</div>'+detail
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'+ev
  +'<div class="acts">'
  +'<button class="mini done" onclick="itemAction('+it.id+',\'approve\')">✓ Approve</button>'
  +'<button class="mini" onclick="itemEdit('+it.id+',\''+t+'\')">✎ Edit</button>'+conv
  +'<button class="mini drop" onclick="itemAction('+it.id+',\'dismiss\')">✕ Dismiss</button>'
  +'</div></div></div>';
}
async function loadReflect(){
 try{
  const j=await (await fetch('/reflections?scope=daily')).json();
  const r=j.reflection;
  if(!r){ document.getElementById('stat').textContent='no reflections';
    list.innerHTML='<div class="empty">no reflection yet — click “Run reflection”.</div>'; return; }
  const when=fmtTime(r.created_at);
  const head='<div class="refl"><div class="sechead">Daily reflection · '+MnemosEsc(when)
    +(r.confidence!=null?(' · conf '+Number(r.confidence).toFixed(2)):'')+'</div>'
    +'<div class="sum">'+MnemosEsc(r.summary||'(no summary)')+'</div></div>';
  const items=r.items||[];
  document.getElementById('stat').textContent=items.length+' insight'+(items.length===1?'':'s');
  list.innerHTML=head+(items.length?items.map(reflItem).join('')
    :'<div class="empty">no insights in this reflection.</div>');
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function itemAction(id,verb){
 await fetch('/reflection_items/'+id+'/'+verb,{method:'POST'}); loadReflect();
}
// --- Learning tab: what Sparrow harvested from your verdicts (Workstream A) --
function learnRow(p){
 const bits=[fmtTime(p.created_at)];
 bits.push('<span class="spk">'+MnemosEsc(p.verdict)+'</span>');
 bits.push(MnemosEsc(p.verdict_source||''));
 if(p.model_tag) bits.push(MnemosEsc(p.model_tag));
 if(!p.human_confirmed) bits.push('<span class="lowtag">unconfirmed (shadow)</span>');
 if(!p.shadow_eligible) bits.push('<span class="pill" title="personal-classed — never sent to cloud shadow eval">local-only</span>');
 const target=p.final_target?'<div class="detail">→ '+MnemosEsc(p.final_target)+'</div>':'';
 const confirm=(!p.human_confirmed)
   ?'<button class="mini done" onclick="learnConfirm(\''+p.id+'\')">✓ Confirm</button>':'';
 return '<div class="row"><span class="kind">'+MnemosEsc(p.task_type)+'</span>'
  +'<div class="body"><div class="text">'+MnemosEsc(p.input_text||'(empty)')+'</div>'+target
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'
  +'<div class="acts">'+confirm
  +'<button class="mini drop" onclick="learnDelete(\''+p.id+'\')">✕ Delete</button>'
  +'</div></div></div>';
}
function exemplarRow(x){
 const bits=[fmtTime(x.created_at),'<span class="spk">'+MnemosEsc(x.quality_tier||'')+'</span>',
   'used '+(x.use_count||0)+'×'];
 return '<div class="row"><span class="kind">'+MnemosEsc(x.task_type)+'</span>'
  +'<div class="body"><div class="text">'+MnemosEsc((x.input_text||'').slice(0,240))+'</div>'
  +'<div class="detail">→ '+MnemosEsc((x.target_text||'').slice(0,240))+'</div>'
  +'<div class="meta">'+bits.join('<span>·</span>')+'</div>'
  +'<div class="acts"><button class="mini drop" onclick="exemplarDelete(\''+x.exemplar_id+'\')">✕ Delete</button></div>'
  +'</div></div>';
}
async function loadLearning(){
 try{
  const [sj,pj,ej,shj]=await Promise.all([
    (await fetch('/learning/stats')).json(),
    (await fetch('/learning/pairs?limit=200')).json(),
    (await fetch('/learning/exemplars?limit=100')).json(),
    (await fetch('/learning/shadow')).json()]);
  const wk=Object.values(sj.week||{}).reduce((a,t)=>a+(t.total||0),0);
  const tot=Object.values(sj.total||{}).reduce((a,t)=>a+(t.total||0),0);
  const es=ej.stats||{};
  document.getElementById('stat').textContent=wk+' this week · '+tot+' total · '
    +(es.count||0)+' exemplar'+((es.count||0)===1?'':'s');
  const types={};
  (pj.pairs||[]).forEach(p=>{(types[p.task_type]=types[p.task_type]||[]).push(p);});
  const cards='<div class="hgrid">'+Object.entries(sj.total||{}).map(([k,v])=>
    '<div class="hcard"><div class="hlabel">'+MnemosEsc(k)+'</div><div class="hval">'+(v.total||0)
    +'</div><div class="hsub">'+((sj.week||{})[k]?((sj.week[k].total||0)+' this week'):'quiet this week')
    +'</div></div>').join('')+'</div>';
  const allOff=!!((es.gates||{})._all||{}).off;
  const killBtn='<div class="acts" style="margin:4px 0 8px">'
    +'<button class="mini'+(allOff?' done':' drop')+'" onclick="exemplarKill('+(!allOff)+')">'
    +(allOff?'▶ Re-enable exemplar injection':'⏸ Pause exemplar injection')+'</button>'
    +(es.enabled?'':'<span class="lowtag" style="margin-left:8px">QUILL_EXEMPLARS=0 (store off)</span>')
    +'</div>';
  const exSec=(ej.rows&&ej.rows.length)
    ?'<div class="sechead">What @@BRAND@@ has learned (exemplars)</div>'+killBtn
      +ej.rows.map(exemplarRow).join('')
    :(es.enabled?'<div class="sechead">Exemplars</div>'+killBtn
      +'<div class="empty">No exemplars yet — 👍 or ✏️ verdicts mint them.</div>':'');
  let shSec='';
  if(shj&&shj.enabled){
    const at=shj.agreement_by_task||{};
    const cards2=Object.entries(at).map(([k,v])=>
      '<div class="hcard"><div class="hlabel">shadow · '+MnemosEsc(k)+'</div>'
      +'<div class="hval">'+(v.agree_rate==null?'—':Math.round(v.agree_rate*100)+'%')+'</div>'
      +'<div class="hsub">agree rate · '+v.graded+' graded</div></div>').join('');
    const reasons=(shj.top_reason_codes||[]).map(r=>'<span class="pill">'+MnemosEsc(r[0])+' ×'+r[1]+'</span>').join(' ');
    shSec='<div class="sechead">Shadow evaluation (last '+shj.window_days+' day'+(shj.window_days===1?'':'s')+')</div>'
      +(cards2?'<div class="hgrid">'+cards2+'</div>':'<div class="empty">No shadow grades yet — runs while the machine is idle.</div>')
      +(reasons?'<div class="hsub" style="margin:8px 2px">'+reasons+' · '+(shj.tokens_spent||0)+' tokens spent</div>':'');
  }
  const rows=Object.entries(types).map(([k,arr])=>
    '<div class="sechead">'+MnemosEsc(k)+'</div>'+arr.map(learnRow).join('')).join('');
  list.innerHTML=(tot?cards:'')+shSec+exSec+(rows||'<div class="empty">Nothing harvested yet — '
    +'approve, edit, or dismiss anything (tasks, chat answers, insights) and it lands here. '
    +'Every row is yours to delete.</div>');
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function exemplarDelete(id){
 await fetch('/learning/exemplars/'+id,{method:'DELETE'}); loadLearning();
}
async function exemplarKill(off){
 await fetch('/learning/exemplars/gate',{method:'POST',headers:{'Content-Type':'application/json'},
   body:JSON.stringify({task_type:'_all',off:off})}); loadLearning();
}
async function learnDelete(id){
 await fetch('/learning/pairs/'+id,{method:'DELETE'}); loadLearning();
}
async function learnConfirm(id){
 await fetch('/learning/pairs/'+id+'/confirm',{method:'POST'}); loadLearning();
}
async function itemEdit(id,current){
 const t=prompt('Edit this insight:',current); if(t==null) return;
 await fetch('/reflection_items/'+id+'/edit',{method:'POST',
   headers:{'Content-Type':'application/json'},body:JSON.stringify({text:t})}); loadReflect();
}
async function itemConvert(id){
 await fetch('/reflection_items/'+id+'/convert',{method:'POST'}); loadReflect();
}
async function runReflect(){
 document.getElementById('stat').textContent='reflecting…';
 try{ await fetch('/reflect/run?scope=daily',{method:'POST'}); }catch(e){}
 loadReflect();
}
function hcard(label,val,sub){
 return '<div class="hcard"><div class="hlabel">'+label+'</div>'
  +'<div class="hval">'+val+'</div>'+(sub?'<div class="hsub">'+sub+'</div>':'')+'</div>';
}
// --- engine provenance ------------------------------------------------------
// Which engine produced these transcripts, and what it cost. With the engine
// behind a flag, "transcripts got worse" is unanswerable without this line.
function engineSub(h){
  const by=h.by_engine||{};
  const names=Object.keys(by);
  const rtf=(h.rtf!=null)?(' · RTF '+h.rtf):'';
  if(!names.length) return 'transcribe wall-time'+rtf;
  if(names.length===1) return MnemosEsc(names[0])+rtf;
  // Two engines in one window means a flag was flipped mid-window; show the
  // split rather than an average across engines, which is meaningless.
  return names.map(n=>MnemosEsc(n)+' '+by[n].utterances
    +(by[n].rtf!=null?(' (RTF '+by[n].rtf+')'):'')).join(' · ');
}
// Where the utterance budget went. VAD is shown apart from the arrow chain: it
// runs during speech, before the speech-end the other three are measured from.
function stageSub(h){
  const s=h.stage_ms||{};
  const ms=(o)=> (o&&o.p50!=null)?(o.p50+'ms'):'—';
  const chain='queue '+ms(s.queue_wait)+' → asr '+ms(s.asr)+' → post '+ms(s.post);
  return (s.vad&&s.vad.p50!=null)?(chain+' · vad '+ms(s.vad)+' during speech')
                                 :chain;
}
// Is each engine's output being judged on thresholds fitted for ITS confidence
// scale? Whisper never needs them — the shipped defaults were written for it.
// Any other engine running uncalibrated is the silent-drift condition.
function calibCard(h){
  const c=h.calibration||{};
  const names=Object.keys(c);
  if(!names.length) return '';
  const bad=names.filter(n=>!c[n].calibrated && n.split(':')[0]!=='whisper');
  if(!bad.length){
    const cal=names.filter(n=>c[n].calibrated);
    if(!cal.length) return '';
    return hcard('Ingest thresholds','calibrated',
      cal.map(n=>MnemosEsc(n)+' · '+(c[n].n_utterances||'?')+' utterances').join(' · '));
  }
  return hcard('Ingest thresholds','⚠ uncalibrated',
    bad.map(n=>MnemosEsc(n)).join(' · ')
    +' judged on Whisper\'s confidence scale — run '
    +'scripts/calibrate_asr_confidence.py');
}
function channelCard(h){
  const by=h.by_channel||{};
  const names=Object.keys(by);
  if(!names.length) return '';
  const val=names.map(n=>MnemosEsc(n)+' '+by[n].utterances).join(' · ');
  const sub=names.map(function(n){
    const t=by[n].total_latency_ms||{};
    return MnemosEsc(n)+' '+(t.p50!=null?(t.p50+'ms p50'):'—');
  }).join(' · ');
  return hcard('By channel', val, sub);
}
// The offline half of the picture: how the last eval_asr.py run scored the
// engine. Stale by nature, so it always says when it ran.
function probeCard(h,pct){
  const e=h.last_eval;
  if(!e||!e.ran_at) return '';
  const age=Math.max(0,Math.round((Date.now()/1000-e.ran_at)/86400));
  const when=age<1?'today':(age+'d ago');
  const raw=e.raw_hallucination_rate, post=e.post_filter_hallucination_rate;
  const val=(post!=null)?pct(post):'—';
  const gap=(raw!=null&&post!=null&&raw>post)
    ? (' · '+pct(raw)+' before the ingest filter') : '';
  const drift=(e.engine_id&&h.configured_engine
               &&e.engine_id.split(':')[0]!==h.configured_engine)
    ? ' · ⚠ scored '+MnemosEsc(e.engine_id)+', running '
      +MnemosEsc(h.configured_engine) : '';
  return hcard('Hallucination probe', val,
    'no-speech fixtures · '+when+gap+drift
    +(e.wer!=null?(' · WER '+e.wer):''));
}
function killSwitchPanel(rows){
  if(!rows||!rows.length) return '';
  const items=rows.map(s=>{
    const on=!!s.on;
    const nd=s.non_default?' · non-default':'';
    return '<label style="display:flex;align-items:center;justify-content:space-between;'
      +'gap:12px;padding:8px 0;border-top:1px solid var(--line);font-size:13px">'
      +'<span><b style="color:var(--navy)">'+MnemosEsc(s.label||s.env)+'</b>'
      +'<span class="mut" style="display:block;font:11px var(--mono)">'
      +MnemosEsc(s.env)+(on?' · ON':' · off')+nd+'</span></span>'
      +'<input type="checkbox" '+(on?'checked':'')
      +' onchange="toggleKillSwitch(\''+MnemosEsc(s.env)+'\',this.checked)"></label>';
  }).join('');
  return '<div class="refl" style="margin-top:14px"><div class="sechead">Kill switches</div>'
    +'<div class="sum" style="margin-bottom:6px">Behavior gates — flip without restart. '
    +'Persisted to data/kill_switches.json.</div>'+items+'</div>';
}
async function toggleKillSwitch(env,on){
  document.getElementById('stat').textContent='updating '+env+'…';
  try{
    await fetch('/console/hardening/kill-switch',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({env:env,on:!!on})});
  }catch(e){}
  loadAttention();
}
async function loadEgress(){
  try{
  const eg=await (await fetch('/console/egress?recent=40')).json();
  let models={};
  try{ models=await (await fetch('/console/models')).json(); }catch(e){}
  const by=eg.by_class||{};
  const sess=(eg.session||{});
  const sessBy=sess.by_class||{};
  const order=['public','internal','personal','sensitive','never-send'];
  const pill=(cls,n)=>'<span class="pill">'+MnemosEsc(cls)+' '+n+'</span>';
  const classPills=order.filter(c=>by[c]).map(c=>pill(c,by[c])).join('')
    || '<span class="mut">no cloud calls with privacy_max yet</span>';
  const sessPills=order.filter(c=>sessBy[c]).map(c=>pill(c,sessBy[c])).join('')
    || '<span class="mut">—</span>';
  const rows=(eg.recent||[]).map(r=>{
    const when=r.time?new Date(r.time*1000).toLocaleString():'—';
    const act=r.privacy_action||(r.ok===false&&r.privacy_max==='never-send'?'refuse':'');
    const badge=act==='refuse'
      ? '<span class="pill" style="color:var(--danger,#a33)">refused</span>'
      : (act==='redact'?'<span class="pill">redacted</span>':'');
    return '<div class="row bleed" style="padding:10px 0;border-top:1px solid var(--line)">'
      +'<div class="t"><b style="color:var(--navy)">'+MnemosEsc(r.privacy_max||'—')+'</b>'
      +' · '+MnemosEsc(r.task||'')+' · '+MnemosEsc(r.provider||'')+'/'+MnemosEsc(r.model||'')
      +'<div class="meta">'+MnemosEsc(when)
      +(r.input_tokens!=null?(' · '+r.input_tokens+' in'):'')
      +(r.cost_usd!=null?(' · $'+Number(r.cost_usd).toFixed(4)):'')
      +' '+badge+'</div></div></div>';
  }).join('') || '<div class="empty">No external calls recorded yet.</div>';
  document.getElementById('stat').textContent=
    (eg.max_seen?('max '+eg.max_seen+' · '):'')
    +(eg.refused||0)+' refused · '+(Object.values(by).reduce((a,b)=>a+b,0))+' cloud w/ class';
  const priv=(models.privacy||{});
  list.innerHTML='<div class="refl" style="margin-bottom:12px">'
    +'<div class="sechead">What left the machine</div>'
    +'<div class="sum">Highest privacy_class on each external model call '
    +'(plan 6.2). never-send is refused before bytes leave; sensitive/personal '
    +'are redacted.</div></div>'
    +'<div class="hgrid">'
    +hcard('Trail max', eg.max_seen||'—', classPills)
    +hcard('Refused', String(eg.refused||0), 'never-send blocked at gate')
    +hcard('Session max', sess.max_seen||priv.max_seen||'—', sessPills)
    +hcard('Session cloud', String(sess.cloud_calls||priv.cloud_calls||0),
           (sess.refused||priv.refused||0)+' refused this process')
    +'</div>'
    +'<div class="sechead" style="margin-top:18px">Recent egress</div>'
    +rows;
  }catch(e){ list.innerHTML='<div class="empty">error loading egress: '+e+'</div>'; }
}
async function loadHealth(){
  try{
  const h=await (await fetch('/console/audio-health')).json();
  let cog={metrics:{}};
  try{ cog=await (await fetch('/console/cognition')).json(); }catch(e){}
  const mins=Math.round((h.window_s||3600)/60);
  document.getElementById('stat').textContent=h.utterances+' utterances · last '+mins+'m';
  const pct=(x)=> x==null?'—':((Math.round(x*1000)/10)+'%');
  const lat=(o)=> (o&&o.avg!=null)
    ? (o.avg+'ms avg'+(o.p95!=null?(' · '+o.p95+'ms p95'):'')+(o.max!=null?(' · '+o.max+'ms max'):''))
    : '—';
  const ph=h.per_hour||{};
  const drops=h.drops_by_reason||{};
  const dropList=Object.keys(drops).length
    ? Object.keys(drops).map(k=>'<span class="pill">'+MnemosEsc(k)+' '+drops[k]+'</span>').join('')
    : '<span class="mut">none</span>';
  const q=h.quality_dist||{};
  list.innerHTML='<div class="hgrid">'
   +hcard('Utterances / hr', (ph.utterances!=null?ph.utterances:'—'),
          h.kept+' kept · '+h.dropped+' dropped')
   +hcard('Dropped / hr', (ph.dropped!=null?ph.dropped:'—'), dropList)
   +hcard('ASR latency', lat(h.asr_latency_ms), engineSub(h))
   +hcard('End-to-end', lat(h.total_latency_ms), stageSub(h))
   +hcard('Quality mix',
          'good '+(q.good||0)+' · noisy '+(q.noisy||0)+' · bad '+(q.bad||0),
          'avg SNR '+(h.avg_snr!=null?h.avg_snr+'dB':'—')
            +' · clip '+(h.avg_clipping!=null?h.avg_clipping+'%':'—'))
   +hcard('Low-confidence', pct(h.low_confidence_rate), 'of kept transcripts')
   +hcard('Speaker unknown', pct(h.speaker_unknown_rate), 'of attributed utterances')
   +channelCard(h)
   +calibCard(h)
   +probeCard(h,pct)
   +offerCards(cog.metrics||{})
   +'</div>';
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function loadAttention(){
  try{
  const a=await (await fetch('/console/attention?days=7')).json();
  const pct=(x)=> x==null?'—':((Math.round(x*1000)/10)+'%');
  const f=a.fulfillment||{};
  const c=a.corpus||{};
  const surf=a.by_surface||{};
  const a1=a.a1||{};
  const a2=a.a2||{};
  const a3=a.a3||{};
  const a4=a.a4||{};
  const tr=a1.traces||{};
  const rp=a1.replay||{};
  const feeder=a2.feeder||{};
  const learn=a4.learn||{};
  const hz=a4.horizon||{};
  const meta=a4.meta||{};
  const promo=a4.promote||{};
  const lastPromo=promo.last||{};
  const rzn=a4.reasoners||{};
  const lastRzn=(rzn.last||{});
  const eco=a.c||{};
  const ecoLc=(eco.lifecycle&&eco.lifecycle.counts)||{};
  const ecoLance=eco.lance||{};
  const ecoForgot=(eco.forgotten_this_month||[]);
  const fTrk=a.f||{};
  const fPred=fTrk.predictors||{};
  const fHard=fTrk.hardening||{};
  const fTasks=fPred.tasks||{};
  const fApp=(fTasks.next_app&&fTasks.next_app.active)||{};
  const fBat=fHard.battery||null;
  const fNonDef=(fHard.non_default||[]);
  document.getElementById('stat').textContent=
    (a.field_impressions||0)+' field impressions · '+(a.misses||0)+' misses · last '+(a.days||7)+'d';
  const kindPills=Object.keys(c.by_kind||{}).map(k=>
    '<span class="pill">'+MnemosEsc(k)+' '+(c.by_kind[k])+'</span>').join('') || '<span class="mut">—</span>';
  const nudge=a.self_report_due
    ? '<div class="refl" style="margin-bottom:12px"><div class="sechead">Weekly check-in due</div>'
      +'<div class="sum">Cognitive load + trust — <a href="/selfreport">open self-report</a>'
      +(a.self_report_last_ts?(' · last '
        +new Date(a.self_report_last_ts*1000).toLocaleDateString()):' · never filed')
      +'</div></div>'
    : '';
  const corpusOk=c.ok?'frozen ✓':'needs attention';
  const gateLabel=rp.status==null?'not run yet'
    :(rp.status==='pass'?'PASS τ='+(rp.mean_tau!=null?rp.mean_tau:'—')
      :(rp.status==='fail'?'FAIL τ='+(rp.mean_tau!=null?rp.mean_tau:'—')
        :'insufficient data'));
  const seedPills=(feeder.top_seeds||[]).map(s=>
    '<span class="pill">'+MnemosEsc(s.id)+' '+(s.weight)+'</span>').join('') || '<span class="mut">none</span>';
  list.innerHTML=nudge+'<div class="hgrid">'
   +hcard('Field engagement', pct(a.field_engagement_rate),
          (a.field_engaged||0)+' of '+(a.field_impressions||0)+' closed')
   +hcard('Misses', String(a.misses||0),
          'chat asked about a node absent from field')
   +hcard('Offers', String(a.offers||surf.offer||0),
          'accept-rate '+pct(a.offer_accept_rate)
          +' · accepted '+(a.offer_accepted||0)
          +' · dismissed '+(a.offer_dismissed||0))
   +hcard('Surfaces',
          'field '+(surf.field||0)+' · ground '+(surf.grounding||0)
          +' · offer '+(surf.offer||0),
          'reaction '+(surf.reaction||0))
   +hcard('Fulfillment', pct(f.fulfillment_rate),
          (f.counts&&f.counts.done||0)+' done · '
          +(f.counts&&f.counts.cancelled||0)+' dropped · '
          +(f.overdue_open||0)+' overdue open'
          +(f.fulfillment_delta!=null
            ?(' · Δ '+(f.fulfillment_delta>=0?'+':'')+f.fulfillment_delta+' vs baseline')
            :(f.baseline?' · baseline set':' · no baseline')))
   +hcard('On-time', pct(f.on_time_rate),
          'median open age '+(f.median_open_age_days!=null?f.median_open_age_days+'d':'—'))
   +hcard('Golden corpus', String(c.n||0)+' cases',
          corpusOk+' · '+kindPills)
   +hcard('Traces (A1)', String(tr.total||0),
          'person '+(tr.person||0)+' · entity '+(tr.entity||0)
          +' · fact '+(tr.fact||0))
   +hcard('Replay gate', gateLabel,
          'threshold '+(a1.gate!=null?a1.gate:0.6)
          +' · renders '+(rp.renders!=null?rp.renders:'—')
          +(a1.due?' · due':''))
   +hcard('Field v2 (A2)', a2.field_v2?'ON':'off',
          (a2.field_v2?'ranking by traces+activation':'gravity ranks; shadow logged')
          +' · edges '+(a2.conductive_edges!=null?a2.conductive_edges:0))
   +hcard('Now-Context', String(feeder.seed_count||0)+' seeds',
          (feeder.attached?'feeder live':'feeder idle')
          +' · gen '+(feeder.generation!=null?feeder.generation:0)
          +' · '+seedPills)
   +hcard('Working Memory (A3)', a3.enabled===false?'off':(String(a3.n_slots||0)+' slots'),
          (a3.enabled===false?'QUILL_WM=0 — quota path'
            :(a3.selection&&a3.selection.fallback
              ?('FALLBACK · '+(a3.selection.reason||'quota'))
              :'MMR + hysteresis'))
          +' · γ '+(a3.gamma!=null?a3.gamma:0.35)
          +((a3.mode&&a3.mode.label)?(' · mode '+a3.mode.label):''))
   +hcard('Horizon (A4)', String((hz.items||[]).length)+' items',
          (hz.enabled===false?'off':('min_p '+(hz.min_p!=null?hz.min_p:0.5)))
          +((hz.items&&hz.items[0])
            ?(' · '+MnemosEsc((hz.items[0].when_label||'')+' '+(hz.items[0].label||'')))
            :' · none yet'))
   +hcard('Learning β', learn.learn_enabled?'ON':'off (kill switch)',
          'updates '+(learn.n_updates!=null?learn.n_updates:0)
          +' · drift '+(learn.drift!=null?learn.drift:0)
          +' · day '+(learn.day_drift!=null?Number(learn.day_drift).toFixed(4):'0'))
   +hcard('β promote', lastPromo.status||(promo.due?'due':'—'),
          (lastPromo.reason||'')
          +(lastPromo.cand_acc!=null?(' · cand '+lastPromo.cand_acc):'')
          +(lastPromo.prior_acc!=null?(' vs prior '+lastPromo.prior_acc):''))
   +hcard('Meta-memory', String(meta.at_risk||0)+' at-risk',
          'stale '+(meta.stale||0)+' · forget '+(meta.forget||0)
          +' · dropped '+(meta.dropped||0)+' · Q '+(meta.questions||0)
          +' · fade '+(meta.fading||0)+' · weak '+(meta.weakening||0))
   +hcard('Reasoners (D)', rzn.enabled===false?'off':('budget '+(rzn.daily_remaining!=null?rzn.daily_remaining:'—')),
          (lastRzn.reason||'idle')
          +((lastRzn.proposal&&lastRzn.proposal.reasoner)
            ?(' · '+lastRzn.proposal.reasoner):'')
          +(rzn.fulfillment_delta!=null
            ?(' · fulfill Δ '+rzn.fulfillment_delta):''))
   +hcard('Economy (C)', eco.enabled===false?'off':(eco.compaction?'compact ON':'observe'),
          'fresh '+(ecoLc.fresh||0)+' · absorbed '+(ecoLc.absorbed||0)
          +' · compacted '+(ecoLc.compacted||0)
          +(eco.due?' · due':''))
   +hcard('Lance index', ecoLance.exists===false?'empty'
          :(String(ecoLance.versions!=null?ecoLance.versions:'—')+' vers'),
          'rows '+(ecoLance.rows!=null?ecoLance.rows:'—')
          +' · every '+(ecoLance.optimize_every!=null?ecoLance.optimize_every:'—'))
   +hcard('Forgotten (30d)', String(ecoForgot.length),
          ecoForgot.length
            ?('latest event '+(ecoForgot[0].id||ecoForgot[0].event_id||'—'))
            :'none compacted this month')
   +hcard('Predictors (F)', fPred.enabled===false?'off'
          :(fApp.version||'heuristic-v1'),
          'next_app · next_contact · next_document'
          +((fTasks.next_app&&fTasks.next_app.preview&&fTasks.next_app.preview[0])
            ?(' · top '+MnemosEsc(String(fTasks.next_app.preview[0].label
              ||fTasks.next_app.preview[0].key||'')))
            :' · console-only'))
   +hcard('Restore drill', fHard.drill_due?'due'
          :((fHard.last_drill&&fHard.last_drill.ok)?'ok':'—'),
          (fBat&&fBat.percent!=null
            ?('battery '+fBat.percent+'%'+(fBat.plugged?' plugged':'')+' · ')
            :'')
          +(fNonDef.length?('non-default '+fNonDef.length):'defaults match'))
   +hcard('Corpus path', MnemosEsc((c.path||'data/bench/attention/golden.jsonl')),
          c.frozen?'MANIFEST stamped':'run freeze_attention_corpus.py --freeze')
   +'</div>'
   +killSwitchPanel(fHard.kill_switches||[])
   +'<div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">'
   +'<button class="btn" onclick="runAttnBackfill()">Backfill traces</button>'
   +'<button class="btn" onclick="runAttnReplay()">Run replay gate</button>'
   +'<button class="btn" onclick="runCtxFeed()">Refresh context</button>'
   +'<button class="btn" onclick="runMetaMemory()">Run meta-memory</button>'
   +'<button class="btn" onclick="runPromote()">Run β promote</button>'
   +'<button class="btn" onclick="runReasoners()">Run reasoners (dry)</button>'
   +'<button class="btn" onclick="runEconomySweep()">Economy sweep</button>'
   +'<button class="btn" onclick="runLanceOptimize()">Lance optimize</button>'
   +'<button class="btn" onclick="runPredictorBench()">Predictor bench</button>'
   +'<button class="btn" onclick="runRestoreDrill()">Restore drill</button>'
   +'<button class="btn" onclick="stampFulfillment()">Stamp fulfillment baseline</button>'
   +'<button class="btn" onclick="revertLearn()">Revert β to prior</button>'
   +'</div>'
   +(ecoForgot.length
     ? ('<div class="refl" style="margin-top:14px"><div class="sechead">Forgotten this month</div>'
        +ecoForgot.slice(0,12).map(f=>{
          const eid=f.event_id||f.id;
          const sum=MnemosEsc((f.summary||f.stub||('event '+eid)||'').toString().slice(0,140));
          return '<div class="sum" style="display:flex;gap:10px;align-items:center;justify-content:space-between;margin:6px 0">'
            +'<span>'+sum+' <span class="mut">#'+MnemosEsc(String(eid||''))+'</span></span>'
            +'<button class="btn" onclick="restoreForgotten('+Number(eid)+')">Restore</button></div>';
        }).join('')
        +'</div>')
     : '')
   +'<p class="mut" style="margin-top:14px;font-size:12px">P0–A4 harness. '
   +(a2.field_v2
     ? 'Field v2 is ON — context lights the neighborhood. '
     : 'Field v2 is off — set QUILL_FIELD_V2=1 to rank by activation. ')
   +(a3.enabled===false
     ? 'WM is off — set QUILL_WM=1 for one attention. '
     : 'WM is ON — field, chat WORKING SET, and planner share slots. ')
   +(learn.learn_enabled
     ? 'Learning is ON — β updates from closed impressions. '
     : 'Learning is off — set QUILL_ATTENTION_LEARN=1 to train β. ')
   +'<a href="/field/state">/field/state</a> · <a href="/field/predictions">/field/predictions</a> · '
   +'<a href="/memory/changes">Memory changes</a> · '
   +'<a href="/selfreport">Self-report</a></p>';
  document.querySelectorAll('#archiveTabs .chip').forEach(ch=>ch.classList.remove('on'));
  document.getElementById('attnchip').classList.add('on');
 }catch(e){ list.innerHTML='<div class="empty">error loading: '+e+'</div>'; }
}
async function runAttnBackfill(){
 document.getElementById('stat').textContent='backfilling traces…';
 try{ await fetch('/console/attention/backfill',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runAttnReplay(){
 document.getElementById('stat').textContent='running replay gate…';
 try{ await fetch('/console/attention/replay',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runCtxFeed(){
 document.getElementById('stat').textContent='refreshing Now-Context…';
 try{ await fetch('/console/attention/feed',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runMetaMemory(){
 document.getElementById('stat').textContent='running meta-memory…';
 try{ await fetch('/console/attention/meta',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runPromote(){
 document.getElementById('stat').textContent='running β promote gate…';
 try{ await fetch('/console/attention/promote',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runReasoners(){
 document.getElementById('stat').textContent='running reasoners (dry)…';
 try{ await fetch('/console/reasoners/run',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function stampFulfillment(){
 document.getElementById('stat').textContent='stamping fulfillment baseline…';
 try{ await fetch('/console/fulfillment/baseline',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runEconomySweep(){
 document.getElementById('stat').textContent='running economy sweep…';
 try{ await fetch('/console/economy/sweep',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runLanceOptimize(){
 document.getElementById('stat').textContent='optimizing Lance index…';
 try{ await fetch('/console/economy/lance/optimize',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runPredictorBench(){
 document.getElementById('stat').textContent='running predictor bench…';
 try{ await fetch('/console/predictors/bench',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function runRestoreDrill(){
 document.getElementById('stat').textContent='running restore drill…';
 try{ await fetch('/console/hardening/drill',{method:'POST'}); }catch(e){}
 loadAttention();
}
async function restoreForgotten(eventId){
 if(!eventId) return;
 document.getElementById('stat').textContent='restoring event '+eventId+'…';
 try{ await fetch('/console/economy/restore?event_id='+eventId,{method:'POST'}); }catch(e){}
 loadAttention();
}
async function revertLearn(){
 document.getElementById('stat').textContent='reverting β to prior…';
 try{ await fetch('/console/attention/learn/revert',{method:'POST'}); }catch(e){}
 loadAttention();
}
// #10: task-offer surfaced-rate ('getting chatty') + accept-rate (offers landing).
function offerCards(m){
 const pct=(x)=> x==null?'—':((Math.round(x*1000)/10)+'%');
 const off=m['proactive_offer'], out=m['offer_outcome'];
 let cards='';
 if(off && off.total){
   cards+=hcard('Offers surfaced', pct(off.rate),
                off.hits+' of '+off.total+' heard tasks (rest held)');
 }
 if(out && out.total){
   cards+=hcard('Offer accept-rate', pct(out.rate),
                out.hits+' of '+out.total+' surfaced offers accepted');
 } else if(off && off.total){
   cards+=hcard('Offer accept-rate', '—', 'no offers answered yet');
 }
 return cards;
}
async function load(){
 persistConsole();
 if(layer==='constellation'){ return loadConstellation(); }
 if(view==="facts"){ return loadFacts(); }
 if(view==="reflect"){ return loadReflect(); }
 if(view==="attention"){ return loadAttention(); }
 if(view==="egress"){ return loadEgress(); }
 if(view==="health"){ return loadHealth(); }
 if(view==="learning"){ return loadLearning(); }
 if(view==="turns"){ return loadTurns(); }
 if(view==="activity"){ return loadActivity(); }
 if(view==="sessions"){ return loadSessions(); }
 const u='/console/events?limit=300&low_only='+low+'&modality='+encodeURIComponent(mod)
   +'&source='+encodeURIComponent(src)+'&q='+encodeURIComponent(q.value.trim());
 try{
  const r=await fetch(u); const j=await r.json();
  const sig=JSON.stringify({count:j.count,total:j.total,events:j.events});
  if(sig===_archiveSig) return;
  _archiveSig=sig;
  document.getElementById('stat').textContent=j.count+' shown · '+j.total+' total';
  list.innerHTML = j.events.length ? j.events.map(row).join('')
    : emptyArchiveHtml();
  bindBleedRows();
 }catch(e){ list.innerHTML='<div class="empty">Could not load archive: '+e+'</div>'; }
}
function emptyArchiveHtml(){
  const parts=[];
  if(q.value.trim()) parts.push('search “'+MnemosEsc(q.value.trim())+'”');
  if(mod) parts.push(mod+' only');
  if(src) parts.push('source '+src.replace(/\.$/,''));
  if(low) parts.push('low-confidence');
  const why=parts.length?('Filters: '+parts.join(' · ')+'.'):'No memories in this view yet.';
  return '<div class="empty">'+why
    +'<br><button type="button" class="btn" style="margin-top:12px" onclick="clearArchiveFilters()">Clear filters</button></div>';
}
function clearArchiveFilters(){
  mod=''; src=''; low=false; view='raw';
  q.value='';
  document.querySelectorAll('#archiveTabs .chip').forEach(c=>c.classList.remove('on'));
  const all=document.querySelector('#archiveTabs .chip[data-mod=""]:not([data-source])');
  if(all) all.classList.add('on');
  document.getElementById('lowchip')&&document.getElementById('lowchip').classList.remove('on');
  persistConsole(); load();
}
async function jobs(){
 if(document.hidden) return;
 try{
  const j=await (await fetch('/console/jobs')).json();
  const sig=JSON.stringify(j);
  if(sig===_jobsSig) return;
  _jobsSig=sig;
  const s=j.stats||{};
  const parts=[]; if(s.pending)parts.push(s.pending+' pending'); if(s.running)parts.push('running');
  if(s.dead)parts.push(s.dead+' dead');
  else if(s.error)parts.push(s.error+' err');
  document.getElementById('jobs').textContent=parts.length?('worker: '+parts.join(', ')):'worker idle';
  const box=document.getElementById('deadJobsBox');
  const list=document.getElementById('deadJobsList');
  const sum=document.getElementById('deadJobsSummary');
  const dead=j.dead||[];
  if(box && list && sum){
    if(dead.length){
      box.style.display='';
      sum.textContent='Dead-letter ('+dead.length+')';
      list.innerHTML=dead.map(d=>{
        const err=MnemosEsc(d.error||'');
        const when=d.updated_at?new Date(d.updated_at*1000).toLocaleString():'';
        return '<div class="dj"><b>#'+d.id+'</b> '+MnemosEsc(d.kind||'')
          +' · '+d.attempts+'/'+(j.max_attempts||5)+' · '+MnemosEsc(when)
          +'<span class="err">'+err+'</span></div>';
      }).join('');
    } else {
      box.style.display='none';
      list.innerHTML='';
    }
  }
 }catch(e){}
}
q.addEventListener('input',()=>{clearTimeout(timer);timer=setTimeout(()=>{persistConsole();load();},250);});
let jobsPollTimer=null;
function startJobsPoll(){
  if(jobsPollTimer) clearInterval(jobsPollTimer);
  const slow=window.MnemosFieldStream&&MnemosFieldStream.connected();
  jobsPollTimer=setInterval(jobs, slow?5000:2000);
}
startJobsPoll(); jobs();
loadAmbient();
setLayer(layer);
load();
(function stickyChromeTools(){
  const tools=document.getElementById('chromeTools');
  const list=document.getElementById('list');
  if(!tools||!list) return;
  let last=list.scrollTop, tucked=false;
  list.addEventListener('scroll',()=>{
    const y=list.scrollTop;
    const down=y>last+4;
    const up=y<last-4;
    if(down && y>48 && !tucked){ tools.classList.add('tucked'); tucked=true; }
    else if(up && tucked){ tools.classList.remove('tucked'); tucked=false; }
    last=y;
  },{passive:true});
})();
</script></body></html>""")
