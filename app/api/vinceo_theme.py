"""vinceo.ai brand tokens — shared across Home, Chat, Console, Desktop, Onboarding.

Palette: warm ivory paper, midnight navy, charcoal, burnished copper.
Motion: ink-like, 200–400ms. Not AI-startup neon.
Copper means human attention — brand mark, Seal, decisions — not navigation.
"""

BRAND = "vinceo.ai"

FONT_LINKS = """\
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.css" crossorigin>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/katex.min.js" crossorigin></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.11/dist/contrib/auto-render.min.js" crossorigin></script>"""

# Continuous single-stroke mark (pen never lifts).
BRAND_MARK = """\
<svg class="mark" width="26" height="26" viewBox="0 0 28 28" fill="none" aria-hidden="true">
  <path d="M5 21C7.5 9 11 7 14 14c2.5 6 6.5 6 9-7" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
</svg>"""

ROOT_TOKENS = """\
:root{
  color-scheme:light;
  --paper:#F8F6F1;--bg:#F8F6F1;--bg-elev:#FFFEFB;
  --workspace:#F3F0E9;--surface:#FFFEFB;--panel:#FFFFFF;--panel-2:#F1EDE6;
  --folio:#FFFEFB;--overlay:rgba(11,19,32,.28);--float:#FFFEFB;
  --text:#23262B;--mut:#555960;--line:rgba(11,19,32,.09);
  --navy:#0B1320;--charcoal:#3A3F47;
  --acc:#B87333;--acc-dim:rgba(184,115,51,.12);--acc-ink:rgba(184,115,51,.22);
  --emerald:#1E5B4F;
  --ok:#2E6F57;--warn:#A66F14;--danger:#9A3F3F;
  --audio:#1E5B4F;--vision:#A86A32;--desktop:#6E2433;
  --radius:14px;
  --shadow:0 1px 2px rgba(11,19,32,.04),0 10px 28px rgba(11,19,32,.05);
  --shadow-workspace:0 1px 2px rgba(11,19,32,.03);
  --shadow-surface:0 1px 2px rgba(11,19,32,.04),0 8px 24px rgba(11,19,32,.05);
  --shadow-folio:0 2px 4px rgba(11,19,32,.05),0 16px 40px rgba(11,19,32,.08);
  --shadow-float:0 4px 8px rgba(11,19,32,.06),0 24px 56px rgba(11,19,32,.12);
  --ease:cubic-bezier(.22,1,.36,1);
  --font:"Inter","Inter Fallback",system-ui,sans-serif;
  --display:"Instrument Serif","Instrument Serif Fallback",Georgia,"Times New Roman",serif;
  --mono:"IBM Plex Mono","IBM Plex Mono Fallback",ui-monospace,Consolas,monospace;
  /* Static CSS grain — no SVG feTurbulence at paint time. */
  --grain: repeating-linear-gradient(0deg, transparent, transparent 2px, rgba(11,19,32,.015) 2px, rgba(11,19,32,.015) 3px),
           repeating-linear-gradient(90deg, transparent, transparent 2px, rgba(11,19,32,.012) 2px, rgba(11,19,32,.012) 3px);
}
@supports not (backdrop-filter: blur(1px)) {
  :root{ --chrome-bg: #F8F6F1; }
}
@font-face{
  font-family:"Inter Fallback";src:local("Arial");
  size-adjust:107%;ascent-override:90%;descent-override:22%;line-gap-override:0%;
}
@font-face{
  font-family:"Instrument Serif Fallback";src:local("Georgia");
  size-adjust:98%;ascent-override:92%;descent-override:22%;line-gap-override:0%;
}
@font-face{
  font-family:"IBM Plex Mono Fallback";src:local("Consolas"),local("Courier New");
  size-adjust:100%;ascent-override:90%;descent-override:22%;line-gap-override:0%;
}
"""

INK_CSS = """\
/* Ink language — written, not rendered */
@keyframes fadeUp{
  from{opacity:0;transform:translateY(8px)}
  to{opacity:1;transform:none}
}
@keyframes morningPaper{
  from{opacity:0;transform:translateY(10px)}
  to{opacity:1;transform:none}
}
@keyframes inkDraw{
  from{stroke-dashoffset:48}
  to{stroke-dashoffset:0}
}
@keyframes inkLine{
  from{transform:scaleX(0);opacity:0}
  to{transform:scaleX(1);opacity:1}
}
@keyframes inkBleed{
  from{opacity:0;transform:scale(.92);filter:blur(2px)}
  to{opacity:1;transform:scale(1);filter:none}
}
@keyframes sealDraw{
  from{stroke-dashoffset:120}
  to{stroke-dashoffset:0}
}
@keyframes sealPress{
  0%{transform:scale(1.12);opacity:.35}
  55%{transform:scale(.96);opacity:1}
  100%{transform:scale(1);opacity:.92}
}
@keyframes nodeBreathe{
  0%,100%{transform:scale(1);opacity:.88}
  50%{transform:scale(1.015);opacity:1}
}
@keyframes inkProgress{
  from{stroke-dashoffset:100}
  to{stroke-dashoffset:0}
}
.ink-rule{
  height:1.5px;width:64px;background:var(--acc);transform-origin:left center;
  animation:inkLine .4s var(--ease) both;border:0;margin:0;
}
.ink-divider{
  height:1px;background:var(--line);transform-origin:left center;
  animation:inkLine .35s var(--ease) both;border:0;margin:12px 0;
}
.ink-underline{
  background-image:linear-gradient(var(--acc-ink),var(--acc-ink));
  background-position:0 100%;background-repeat:no-repeat;
  background-size:0% 1.5px;transition:background-size .35s var(--ease);
}
.ink-underline.on,.ink-underline:focus{background-size:100% 1.5px}
.pen-mark{
  background:linear-gradient(105deg,transparent 0%,rgba(184,115,51,.14) 40%,
    rgba(184,115,51,.1) 60%,transparent 100%);
  box-decoration-break:clone;
}
.surface{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);position:relative;
}
.surface::after{
  content:"";pointer-events:none;position:absolute;inset:0;border-radius:inherit;
  opacity:.55;background-image:var(--grain);mix-blend-mode:multiply;
}
.folio{
  background:var(--folio);border:1px solid rgba(11,19,32,.1);border-radius:var(--radius);
  box-shadow:var(--shadow-folio);position:relative;
}
.folio::before{
  content:"";position:absolute;left:0;top:14px;bottom:14px;width:3px;
  background:linear-gradient(180deg,var(--acc),rgba(184,115,51,.15));
  border-radius:2px;
}
.float-surface{
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);
}
.serif-title{
  font-family:var(--display);font-weight:400;letter-spacing:-.02em;color:var(--navy);
}
.ambient-note{
  font:italic 13px/1.45 var(--font);color:var(--mut);padding:6px 0 6px 12px;
  border-left:1.5px solid rgba(11,19,32,.1);margin:0 0 10px;
  animation:fadeUp .35s var(--ease) both;
}
.ambient-note.attention{
  border-left-color:var(--acc);color:var(--charcoal);
}
.provenance-stack{display:flex;flex-direction:column;gap:0;margin-top:10px}
.provenance-stack .pv-step{
  display:grid;grid-template-columns:16px 1fr;gap:10px;padding:8px 0;
  border-left:1px solid rgba(184,115,51,.25);margin-left:7px;padding-left:14px;
  animation:inkBleed .35s var(--ease) both;font-size:13px;
}
.provenance-stack .pv-step .pv-dot{
  width:9px;height:9px;border-radius:50%;background:var(--acc);
  margin-left:-19px;margin-top:4px;box-shadow:0 0 0 3px var(--paper);
}
.provenance-stack .pv-label{
  font:11px/1.2 var(--mono);text-transform:uppercase;letter-spacing:.05em;color:var(--mut);
}
.provenance-stack .pv-body{color:var(--text);margin-top:2px;white-space:pre-wrap}
/* Response document — semantic layout engine */
:root{
  --r-title:24px;--r-section:18px;--r-body:16px;--r-support:14px;--r-meta:12px;
}
.rd{
  display:flex;flex-direction:column;gap:14px;padding-left:8px;
  animation:fadeUp .28s var(--ease) both;
}
.rd-title{
  font-family:var(--display);font-size:var(--r-title);font-weight:400;
  line-height:1.25;letter-spacing:-.02em;color:var(--navy);margin:0;
  animation:fadeUp .32s var(--ease) both;
}
.rd-heading{
  font-size:var(--r-section);font-weight:600;line-height:1.35;
  color:var(--navy);margin:6px 0 0;letter-spacing:-.015em;
}
.rd-takeaway{
  font-size:var(--r-body);font-weight:500;line-height:1.5;color:var(--navy);
  padding:10px 0 12px;border-bottom:1px solid rgba(11,19,32,.07);margin:0;
}
.rd-takeaway .rd-kicker{
  display:block;font:500 10px/1 var(--mono);letter-spacing:.08em;
  text-transform:uppercase;color:var(--mut);margin-bottom:6px;
}
.rd-p{
  font-size:var(--r-body);font-weight:400;line-height:1.65;color:var(--text);
  margin:0;max-width:38em;
}
.rd-p + .rd-p{margin-top:2px}
.rd-em{
  font-weight:600;color:var(--navy);
  background-image:linear-gradient(var(--acc-ink),var(--acc-ink));
  background-position:0 100%;background-repeat:no-repeat;background-size:100% 1.5px;
}
.rd-card{
  border-radius:12px;padding:12px 14px;margin:2px 0;
  border:1px solid var(--line);animation:fadeUp .35s var(--ease) both;
  transform:translateY(0);transition:transform .2s var(--ease),box-shadow .2s var(--ease);
}
.rd-card:hover{transform:translateY(-1px);box-shadow:var(--shadow-workspace)}
.rd-card .rd-card-head{
  display:flex;align-items:center;gap:8px;margin-bottom:6px;
  font:600 11px/1.2 var(--mono);letter-spacing:.06em;text-transform:uppercase;
}
.rd-card .rd-card-icon{
  width:18px;height:18px;border-radius:5px;display:inline-flex;
  align-items:center;justify-content:center;font-size:11px;flex:0 0 auto;
}
.rd-card .rd-card-body{
  font-size:var(--r-support);line-height:1.55;color:var(--text);margin:0;
  white-space:pre-wrap;
}
.rd-card.key_idea,.rd-card.concept{
  background:rgba(184,115,51,.07);border-color:rgba(184,115,51,.2);
}
.rd-card.key_idea .rd-card-head,.rd-card.concept .rd-card-head{color:var(--acc)}
.rd-card.key_idea .rd-card-icon,.rd-card.concept .rd-card-icon{
  background:rgba(184,115,51,.15);color:var(--acc);
}
.rd-card.definition{
  background:rgba(30,91,79,.06);border-color:rgba(30,91,79,.18);
}
.rd-card.definition .rd-card-head{color:var(--emerald)}
.rd-card.definition .rd-card-icon{background:rgba(30,91,79,.12);color:var(--emerald)}
.rd-card.example{
  background:rgba(11,19,32,.03);border-color:rgba(11,19,32,.08);
}
.rd-card.example .rd-card-head{color:var(--charcoal)}
.rd-card.example .rd-card-icon{background:rgba(11,19,32,.06);color:var(--navy)}
.rd-card.warning,.rd-card.mistake{
  background:rgba(199,138,44,.08);border-color:rgba(199,138,44,.28);
}
.rd-card.warning .rd-card-head,.rd-card.mistake .rd-card-head{color:var(--warn)}
.rd-card.warning .rd-card-icon,.rd-card.mistake .rd-card-icon{
  background:rgba(199,138,44,.16);color:var(--warn);
}
.rd-card.note,.rd-card.summary{
  background:rgba(11,19,32,.025);border-color:rgba(11,19,32,.07);
}
.rd-card.note .rd-card-head,.rd-card.summary .rd-card-head{color:var(--mut)}
.rd-formula{
  margin:8px 0;padding:16px 12px;text-align:center;
  background:rgba(11,19,32,.025);border:1px solid rgba(11,19,32,.06);
  border-radius:12px;overflow-x:auto;
  animation:inkBleed .4s var(--ease) both;
  font-size:1.15em;line-height:1.6;
}
.rd-formula .katex-display{margin:0}
.rd-list{margin:0;padding:0 0 0 1.15em;font-size:var(--r-body);line-height:1.6}
.rd-list li{margin:4px 0;padding-left:2px}
.rd-list li::marker{color:var(--acc)}
.rd-code{
  margin:4px 0;padding:12px 14px;border-radius:12px;
  background:var(--navy);color:#F8F6F1;font:13px/1.5 var(--mono);
  overflow-x:auto;white-space:pre;
}
.rd-actions{
  display:flex;flex-wrap:wrap;gap:8px;margin-top:4px;padding-top:12px;
  border-top:1px solid rgba(11,19,32,.06);
}
.rd-actions button{
  background:var(--bg-elev);border:1px solid var(--line);border-radius:10px;
  padding:8px 12px;font:500 13px/1.2 var(--font);color:var(--charcoal);
  cursor:pointer;
}
.rd-actions button:hover{
  border-color:rgba(184,115,51,.4);color:var(--navy);
  background:rgba(184,115,51,.06);transform:translateY(-1px) scale(1.01);
}
.rd-grounding{
  margin-top:4px;font-size:var(--r-meta);color:var(--mut);line-height:1.5;
}
.rd-grounding summary{
  cursor:pointer;user-select:none;list-style:none;
  font:500 11px/1.3 var(--mono);letter-spacing:.04em;
}
.rd-grounding summary::-webkit-details-marker{display:none}
.rd-grounding summary::after{content:" ▾";opacity:.55}
.rd-grounding[open] summary::after{content:" ▴"}
.rd-grounding summary:hover{color:var(--navy)}
.rd-grounding .rd-g-group{margin:8px 0 0}
.rd-grounding .rd-g-label{
  font:500 10px/1.2 var(--mono);text-transform:uppercase;letter-spacing:.05em;
  color:var(--mut);margin-bottom:2px;
}
.rd-grounding .rd-g-item{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:2px;
}
.msg.result .msg-body.rd-host{white-space:normal;font-size:inherit;line-height:inherit}
@media (prefers-reduced-motion:reduce){
  .rd,.rd-title,.rd-card,.rd-formula{animation:none}
  .rd-card:hover,.rd-actions button:hover{transform:none}
}
"""

CHROME_CSS = """\
a{color:inherit;text-decoration:none}
.top,.chrome{
  background:var(--chrome-bg, rgba(248,246,241,.94));
  border-bottom:1px solid var(--line);
}
@supports (backdrop-filter: blur(12px)) {
  .top,.chrome{
    background:rgba(248,246,241,.94);backdrop-filter:blur(12px);
  }
}
.brand{
  display:inline-flex;align-items:center;gap:8px;
  font-family:var(--display);font-size:1.55rem;font-weight:400;
  letter-spacing:-.02em;color:var(--navy);line-height:1;
  transition:color .28s var(--ease),opacity .28s var(--ease);
}
.brand:hover{color:var(--navy);opacity:.85}
.brand .mark{color:var(--acc);flex:0 0 auto;transition:transform .32s var(--ease)}
.brand:hover .mark{transform:rotate(-8deg) scale(1.06)}
.page-sub{color:var(--mut);font-size:13px;font-weight:500}
.nav{display:flex;gap:2px;align-items:center}
.nav a{
  color:var(--mut);font-size:13px;font-weight:500;padding:7px 12px;
  border-radius:10px;position:relative;
  transition:color .28s var(--ease),background .28s var(--ease),
    transform .22s var(--ease),box-shadow .28s var(--ease);
}
.nav a:hover{
  color:var(--navy);background:var(--panel-2);
  transform:translateY(-1px);box-shadow:0 4px 12px rgba(11,19,32,.06);
}
.nav a:active{transform:translateY(0) scale(.98)}
.nav a.on{
  color:var(--navy);background:rgba(11,19,32,.06);
  box-shadow:inset 0 0 0 1px rgba(11,19,32,.08);
}
.nav a.on:hover{color:var(--navy);background:rgba(11,19,32,.08);transform:translateY(-1px)}
.nav a.attn{color:var(--acc);background:var(--acc-dim);
  box-shadow:inset 0 0 0 1px rgba(184,115,51,.18)}
.spacer{flex:1}
button,.btn,.mini,.ctl{
  transition:transform .22s var(--ease),box-shadow .28s var(--ease),
    background .28s var(--ease),border-color .28s var(--ease),color .28s var(--ease),
    opacity .28s var(--ease),filter .28s var(--ease);
  will-change:transform;
}
button:hover:not(:disabled),.btn:hover:not(:disabled),
.mini:hover:not(:disabled),.ctl:hover:not(:disabled){
  transform:translateY(-1px);
  box-shadow:0 4px 14px rgba(11,19,32,.08);
}
button:active:not(:disabled),.btn:active:not(:disabled),
.mini:active:not(:disabled),.ctl:active:not(:disabled){
  transform:translateY(0) scale(.97);
  box-shadow:0 1px 3px rgba(11,19,32,.06);
}
button:disabled,.btn:disabled{cursor:default}
.brand .mark path{
  stroke-dasharray:48;animation:inkDraw .45s var(--ease) both;
}
/* The Seal + shared hold primitive */
.seal-btn{
  position:relative;isolation:isolate;overflow:hidden;
  border:1px solid rgba(184,115,51,.45)!important;
  background:rgba(184,115,51,.08)!important;color:var(--navy)!important;
  min-width:132px;font-weight:600;
}
.seal-btn .seal-ring,.holdable .hold-ring{
  position:absolute;right:10px;top:50%;width:22px;height:22px;
  transform:translateY(-50%);pointer-events:none;opacity:0;
}
.seal-btn.holding .seal-ring,.holdable.holding .hold-ring{opacity:1}
.seal-btn .seal-ring circle,.holdable .hold-ring circle{
  fill:none;stroke:var(--acc);stroke-width:2;stroke-linecap:round;
  stroke-dasharray:100;stroke-dashoffset:100;
}
.holdable.holding .hold-ring circle{/* progress driven by --hold-p via JS */}
.seal-btn.sealed .seal-ring,.holdable.sealed .hold-ring{opacity:.92}
.seal-btn.sealed .seal-ring circle,.holdable.sealed .hold-ring circle{
  stroke-dashoffset:0;fill:rgba(184,115,51,.12);
}
.holdable.hold-spine{position:relative}
.holdable.hold-spine::after{
  content:"";position:absolute;left:0;top:14px;bottom:14px;width:2px;
  background:var(--acc);opacity:.55;border-radius:1px;
  transform-origin:top center;transform:scaleY(var(--hold-p,0));
  transition:none;pointer-events:none;
}
.holdable.hold-spine.holding::after{opacity:1}
.holdable.hold-flash{
  box-shadow:inset 0 0 0 1px rgba(184,115,51,.55)!important;
}
.hold-more{
  position:absolute;right:8px;top:8px;border:0;background:transparent;
  color:var(--mut);font:14px var(--mono);cursor:pointer;padding:2px 6px;
  border-radius:6px;opacity:.55;
}
.hold-more:hover,.hold-more:focus{opacity:1;background:var(--panel-2);color:var(--navy)}
/* Recording indicator — persistent, one-click pause per source */
#vinceoRecBar{
  position:fixed;z-index:70;right:16px;bottom:16px;
  display:flex;flex-direction:column;gap:8px;align-items:flex-end;
  pointer-events:none;font-family:var(--font);
}
#vinceoRecBar .rec-chip,#vinceoRecBar .rec-consent-btn,#vinceoPrivacy .pv-btn{
  pointer-events:auto;
}
#vinceoRecBar .rec-row{
  display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;
  max-width:min(420px,92vw);
}
#vinceoRecBar .rec-chip{
  display:inline-flex;align-items:center;gap:7px;
  background:rgba(248,246,241,.96);border:1px solid var(--line);
  border-radius:999px;padding:7px 12px 7px 10px;cursor:pointer;
  box-shadow:0 6px 18px rgba(11,19,32,.1);font-size:12px;font-weight:600;
  color:var(--navy);backdrop-filter:blur(10px);
}
#vinceoRecBar .rec-chip .dot{
  width:8px;height:8px;border-radius:50%;background:var(--danger);
  box-shadow:0 0 0 0 rgba(166,71,71,.45);
  animation:recPulse 1.6s var(--ease) infinite;
}
#vinceoRecBar .rec-chip.paused .dot{
  background:var(--mut);animation:none;box-shadow:none;
}
#vinceoRecBar .rec-chip.paused{color:var(--mut);font-weight:500}
#vinceoRecBar .rec-chip .act{font:10px/1 var(--mono);color:var(--mut);letter-spacing:.04em;text-transform:uppercase}
#vinceoRecBar .rec-consent-btn{
  border:1px solid rgba(184,115,51,.4);background:rgba(184,115,51,.1);
  color:var(--navy);border-radius:12px;padding:9px 14px;font:600 12px var(--font);
  cursor:pointer;box-shadow:0 6px 18px rgba(11,19,32,.08);
}
#vinceoPrivacy{
  display:none;position:fixed;inset:0;z-index:80;background:var(--overlay);
  align-items:center;justify-content:center;padding:24px 16px;
}
#vinceoPrivacy.open{display:flex}
#vinceoPrivacy .pv-sheet{
  width:min(480px,100%);background:var(--folio);border:1px solid var(--line);
  border-radius:var(--radius);box-shadow:var(--shadow-float);padding:22px 22px 18px;
  animation:fadeUp .28s var(--ease) both;
}
#vinceoPrivacy h2{
  font-family:var(--display);font-weight:400;font-size:1.55rem;margin:0 0 8px;
  color:var(--navy);letter-spacing:-.02em;
}
#vinceoPrivacy .pv-lead{color:var(--mut);font-size:14px;margin:0 0 16px;max-width:40ch}
#vinceoPrivacy label.pv-src{
  display:flex;gap:10px;align-items:flex-start;padding:10px 0;
  border-top:1px solid var(--line);cursor:pointer;font-size:14px;
}
#vinceoPrivacy label.pv-src:first-of-type{border-top:0}
#vinceoPrivacy label.pv-src input{margin-top:3px;flex:0 0 auto}
#vinceoPrivacy .pv-src b{display:block;color:var(--navy);font-weight:600}
#vinceoPrivacy .pv-src span{display:block;color:var(--mut);font-size:12px;margin-top:2px}
#vinceoPrivacy .pv-warn{
  margin:12px 0 0;padding:10px 12px;border-radius:10px;font-size:12px;
  background:rgba(166,71,71,.06);border:1px solid rgba(166,71,71,.22);color:var(--danger);
}
#vinceoPrivacy .pv-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;justify-content:flex-end}
#vinceoPrivacy .pv-btn{
  border-radius:10px;padding:9px 16px;font:500 13px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
}
#vinceoPrivacy .pv-btn.go{background:var(--navy);color:#F8F6F1;border:none}
#vinceoPrivacy .pv-btn.quiet{background:transparent;color:var(--mut)}
@keyframes recPulse{
  0%{box-shadow:0 0 0 0 rgba(166,71,71,.4)}
  70%{box-shadow:0 0 0 8px rgba(166,71,71,0)}
  100%{box-shadow:0 0 0 0 rgba(166,71,71,0)}
}
@media (prefers-reduced-motion:reduce){
  .brand:hover .mark,.nav a:hover,.nav a:active,
  button:hover:not(:disabled),.btn:hover:not(:disabled),
  .mini:hover:not(:disabled),.ctl:hover:not(:disabled),
  button:active:not(:disabled),.btn:active:not(:disabled),
  .mini:active:not(:disabled),.ctl:active:not(:disabled){transform:none;box-shadow:none}
  .brand .mark path,.ink-rule,.ink-divider,.seal-btn.holding .seal-ring circle,
  .holdable.holding .hold-ring circle,
  #vinceoRecBar .rec-chip .dot,#vinceoPrivacy .pv-sheet{animation:none}
  .seal-btn.holding .seal-ring circle,.holdable.holding .hold-ring circle{stroke-dashoffset:0}
  .holdable.hold-spine::after{transition:none}
}
@media (forced-colors: active){
  :root{--acc:CanvasText;--acc-dim:Highlight;--navy:CanvasText;--mut:GrayText;--line:CanvasText}
  #vinceoApproval,.band.proposal,.holdable.hold-spine::after,.row::before{
    border:2px solid Highlight !important;
  }
  .nav a.on,.seal-btn,.holdable.holding{
    outline:2px solid Highlight;outline-offset:2px;
  }
}
"""


def apply(page: str) -> str:
    """Inject shared fonts/tokens/ink/chrome/UI into a page with @@placeholders@@.

    Leaves @@APPROVAL@@ for per-request SSR via approval_partial.inject_page.
    """
    from app.api.approval_partial import APPROVAL_CSS, APPROVAL_JS
    from app.api.vinceo_ui import UI_JS

    return (
        page.replace("@@FONTS@@", FONT_LINKS)
        .replace("@@ROOT@@", ROOT_TOKENS)
        .replace("@@INK@@", INK_CSS)
        .replace("@@CHROME@@", CHROME_CSS + "\n" + APPROVAL_CSS)
        .replace("@@UI_JS@@", UI_JS + "\n" + APPROVAL_JS)
        .replace("@@MARK@@", BRAND_MARK)
        .replace("@@BRAND@@", BRAND)
    )
