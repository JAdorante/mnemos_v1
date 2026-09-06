"""Sparrow brand tokens — shared across Home, Chat, Console, Desktop, Onboarding.

Design system (UI refactor spec, sparrow-today-redesign.html):
  - Surfaces: soft-ink page (--ink), cards (--surface), nested/hover (--raised),
    one opaque hairline (--line). No shadows anywhere — elevation on a dark
    theme is border + fill delta.
  - Text: warm off-white (--text), --muted for secondary, --faint for meta
    only (never load-bearing).
  - Semantic: violet = primary action + person entities; amber = topics +
    attention; green = healthy/idle.
  - Type: two voices. Fraunces (serif, display-only: page h1, card h2,
    margin-note prose) and Instrument Sans (everything functional). Mono is
    not a UI face — it survives only inside <pre> raw-payload disclosures.
    No ALL-CAPS eyebrows, no letterspaced labels.
  - Radii: three tiers mapped to hierarchy — 14 section cards, 10 nested
    panels/inputs, 7 buttons/small controls.

Legacy aliases (--paper, --navy, --acc, --panel, ...) keep older page CSS on
the same palette; new code should use the spec names. --navy = strongest ink,
--paper = the ground, so background:var(--navy);color:var(--paper) still
reads as a high-contrast solid on every page.
"""

BRAND = "Sparrow"
COMPANY = "Ravenry"
COPYRIGHT = "Copyright 2026 Ravenry LLC"

# Fingerprinted on each apply() so deploys bust browser caches without
# asking testers to hard-refresh. Paths are relative to app/static/.
_ASSET_FINGERPRINT_FILES = (
    "css/mnemos-ink.css",
    "css/mnemos-chrome.css",
    "css/mnemos-approval.css",
    "js/mnemos-ui.js",
    "js/mnemos-approval.js",
    "fonts/mnemos-fonts.css",
)


def asset_version() -> str:
    """Short content hash of shared UI assets (stable across workers)."""
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "static"
    h = hashlib.sha1()
    for rel in _ASSET_FINGERPRINT_FILES:
        path = root / rel
        if path.is_file():
            h.update(path.read_bytes())
        else:
            h.update(rel.encode())
    return h.hexdigest()[:10]


def _q(v: str) -> str:
    return f"?v={v}"


def font_links(v: str | None = None) -> str:
    q = _q(v or asset_version())
    return (
        '<link rel="icon" href="/static/ravenry-mark.png" type="image/png">\n'
        f'<link rel="stylesheet" href="/static/fonts/mnemos-fonts.css{q}">'
    )


def theme_style_links(v: str | None = None) -> str:
    q = _q(v or asset_version())
    return (
        f'<link rel="stylesheet" href="/static/css/mnemos-ink.css{q}">\n'
        f'<link rel="stylesheet" href="/static/css/mnemos-chrome.css{q}">\n'
        f'<link rel="stylesheet" href="/static/css/mnemos-approval.css{q}">'
    )


def theme_script_links(v: str | None = None) -> str:
    q = _q(v or asset_version())
    return (
        f'<script src="/static/js/mnemos-ui.js{q}"></script>\n'
        f'<script src="/static/js/mnemos-approval.js{q}"></script>'
    )


# Legacy aliases — tests and callers that only need the path substring.
FONT_LINKS = font_links("dev")
KATEX_LINKS = """\
<link rel="stylesheet" href="/static/katex/katex.min.css">
<script defer src="/static/katex/katex.min.js"></script>
<script defer src="/static/katex/auto-render.min.js"></script>"""
THEME_STYLE_LINKS = theme_style_links("dev")
THEME_SCRIPT_LINKS = theme_script_links("dev")

# Sparrow bird — inline SVG so it takes the accent token, no filter hacks.
BRAND_MARK = """\
<svg class="mark" width="20" height="20" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M3 13c5-1 9-5 11-10 1 4 0 8-2 11l9-3c-4 5-10 8-16 8l-2-6z" fill="var(--violet)"/></svg>"""

ROOT_TOKENS = """\
:root{
  color-scheme:dark;
  /* Spec surfaces — soft ink, not #000. */
  --ink:#101216;--surface-2:#161920;--raised:#1c2029;
  --paper:var(--ink);--bg:var(--ink);--bg-elev:var(--raised);
  --workspace:var(--ink);--surface:var(--surface-2);--panel:var(--surface-2);--panel-2:var(--raised);
  --folio:var(--surface-2);--overlay:rgba(0,0,0,.6);--float:var(--raised);
  /* Spec text. --faint is for meta lines only — never load-bearing. */
  --text:#e9e7e2;--mut:#9298a3;--muted:var(--mut);--faint:#5d626d;
  /* One opaque hairline everywhere. */
  --line:#262b35;--hairline:#1f232b;--line-strong:#333947;
  --navy:#e9e7e2;--charcoal:#9298a3;
  /* Semantic: violet = primary action + people; amber = topics + attention;
     green = healthy/idle. */
  --violet:#8d85f2;--amber:#dfb35e;--green:#74c294;
  --violet-dim:rgba(141,133,242,.14);
  --acc:var(--violet);--acc-fg:#15131f;
  --acc-05:color-mix(in srgb,var(--acc) 5%,transparent);
  --acc-06:color-mix(in srgb,var(--acc) 6%,transparent);
  --acc-07:color-mix(in srgb,var(--acc) 7%,transparent);
  --acc-08:color-mix(in srgb,var(--acc) 8%,transparent);
  --acc-10:color-mix(in srgb,var(--acc) 10%,transparent);
  --acc-12:color-mix(in srgb,var(--acc) 12%,transparent);
  --acc-14:color-mix(in srgb,var(--acc) 14%,transparent);
  --acc-15:color-mix(in srgb,var(--acc) 15%,transparent);
  --acc-18:color-mix(in srgb,var(--acc) 18%,transparent);
  --acc-20:color-mix(in srgb,var(--acc) 20%,transparent);
  --acc-22:color-mix(in srgb,var(--acc) 22%,transparent);
  --acc-25:color-mix(in srgb,var(--acc) 25%,transparent);
  --acc-28:color-mix(in srgb,var(--acc) 28%,transparent);
  --acc-35:color-mix(in srgb,var(--acc) 35%,transparent);
  --acc-40:color-mix(in srgb,var(--acc) 40%,transparent);
  --acc-45:color-mix(in srgb,var(--acc) 45%,transparent);
  --acc-55:color-mix(in srgb,var(--acc) 55%,transparent);
  --acc-dim:var(--acc-12);--acc-ink:var(--acc-22);--acc-warm:#14131D;
  /* Ink tints — light-ink washes for hovers, hairlines, quiet fills.
     Use these instead of raw rgba(...) so the ground can re-theme. */
  --ink-03:color-mix(in srgb,var(--navy) 3%,transparent);
  --ink-04:color-mix(in srgb,var(--navy) 4%,transparent);
  --ink-06:color-mix(in srgb,var(--navy) 6%,transparent);
  --ink-08:color-mix(in srgb,var(--navy) 8%,transparent);
  --ink-10:color-mix(in srgb,var(--navy) 10%,transparent);
  --ink-12:color-mix(in srgb,var(--navy) 12%,transparent);
  --ink-16:color-mix(in srgb,var(--navy) 16%,transparent);
  --ink-20:color-mix(in srgb,var(--navy) 20%,transparent);
  --ink-28:color-mix(in srgb,var(--navy) 28%,transparent);
  --emerald:var(--green);
  --ok:var(--green);--warn:var(--amber);--danger:#e0716a;
  --audio:var(--green);--vision:var(--amber);--desktop:#d4788c;
  /* Geometry — 4pt grid; never invent an off-grid gap. */
  --sp-1:4px;--sp-2:8px;--sp-3:12px;--sp-4:16px;--sp-5:20px;
  --sp-6:24px;--sp-7:28px;--sp-8:32px;--sp-10:40px;--sp-12:48px;--sp-14:56px;
  /* Corner scale — three tiers mapped to hierarchy: lg section cards,
     sm nested panels/inputs, xs buttons and small controls. */
  --radius-xs:7px;--radius-sm:10px;--radius:14px;
  --radius-lg:14px;--radius-xl:18px;--radius-full:999px;
  --r-lg:var(--radius);--r-md:var(--radius-sm);--r-sm:var(--radius-xs);
  /* Optical type scale + matched tracking; caps labels track wide. */
  --fs-caption2:11px;--fs-caption:12px;--fs-footnote:13px;--fs-sub:14px;
  --fs-body:15px;--fs-title3:17px;--fs-title2:21px;--fs-title1:26px;--fs-large:32px;
  --lh-tight:1.2;--lh-snug:1.35;--lh-body:1.55;--lh-loose:1.65;
  --track-tight:-.022em;--track-snug:-.012em;--track-caps:.06em;
  /* No shadows anywhere — elevation is border + fill delta on a dark theme.
     Tokens stay defined so page CSS keeps parsing; they all resolve to none. */
  --shadow:none;
  --shadow-workspace:none;
  --shadow-surface:none;
  --shadow-folio:none;
  --shadow-float:none;
  --shadow-press:none;
  /* Motion — fast to start, long to settle; springs only for the mark. */
  --ease:cubic-bezier(.22,1,.36,1);
  --ease-io:cubic-bezier(.4,0,.2,1);
  --ease-spring:cubic-bezier(.34,1.45,.5,1);
  --dur-fast:.15s;--dur:.26s;--dur-slow:.45s;
  /* Materials — chrome and floats share one glass recipe. */
  --glass:saturate(1.8) blur(20px);
  /* Two voices: serif is display-only (page h1, card h2, margin-note prose);
     everything functional is sans. Mono is not a UI face — <pre> payloads only. */
  --sans:"Instrument Sans","Instrument Sans Fallback",system-ui,sans-serif;
  --serif:"Fraunces","Fraunces Fallback",Georgia,serif;
  --font:var(--sans);
  --display:var(--serif);
  --mono:"IBM Plex Mono",ui-monospace,Consolas,monospace;
  --grain:none;
  /* Stacking bands — never invent raw z-index integers outside this file. */
  --z-base: 1;    /* in-flow decorations: ::before spines, grain overlays */
  --z-raised: 5;  /* sticky page chrome: .top bars, .work-bar, table heads */
  --z-rail: 15;   /* ambient side rails (legacy float; prefer layout) */
  --z-banner: 25; /* approval / status banners */
  --z-float: 40;  /* toasts, ghost panels, nudges — dock owns these */
  --z-popover: 50;/* dropdowns, past-chats panel */
  --z-system: 70; /* recording chips — must beat conversational float */
  --z-modal: 80;  /* modal sheets, privacy dialog, hold tips */
  --chrome-h: 56px; /* measured by MnemosChrome; fallback for first paint */
  --composer-h: 0px; /* in-flow chat composer; lifts the corner dock */
  --dock-clear: 72px; /* measured rec/toast dock height + gap */
  --chrome-bg: rgba(16,18,22,.88);
}
/* `hidden` must beat any class that sets display (.fetch-err{display:flex}),
   or the element stays painted forever. Author rules outrank the UA sheet. */
[hidden]{display:none!important}
@supports not (backdrop-filter: blur(1px)) {
  :root{ --chrome-bg: #101216; }
}
@font-face{
  font-family:"Instrument Sans Fallback";src:local("Arial");
  size-adjust:102%;ascent-override:94%;descent-override:24%;line-gap-override:0%;
}
@font-face{
  font-family:"Fraunces Fallback";src:local("Georgia");
  size-adjust:104%;ascent-override:96%;descent-override:24%;line-gap-override:0%;
}
"""

INK_CSS = """\
/* Ink language — written, not rendered */
@keyframes fadeUp{
  from{opacity:0;transform:translateY(6px)}
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
  from{opacity:0;transform:scale(.96);filter:blur(1.5px)}
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
  background:linear-gradient(105deg,transparent 0%,var(--acc-14) 40%,
    var(--acc-10) 60%,transparent 100%);
  box-decoration-break:clone;
}
.surface{
  background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);position:relative;
}
.surface::after{
  content:"";pointer-events:none;position:absolute;inset:0;border-radius:inherit;
  opacity:.5;background-image:var(--grain);mix-blend-mode:screen;
}
.folio{
  background:var(--folio);border:1px solid var(--ink-10);border-radius:var(--radius);
  box-shadow:var(--shadow-folio);position:relative;
}
.folio::before{
  content:"";position:absolute;left:0;top:14px;bottom:14px;width:3px;
  background:linear-gradient(180deg,var(--acc),var(--acc-15));
  border-radius:2px;z-index:var(--z-base);pointer-events:none;
}
.float-surface{
  background:var(--float);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-float);
}
.serif-title{
  font-family:var(--display);font-weight:400;letter-spacing:var(--track-tight);color:var(--navy);
}
.ambient-note{
  font:italic 13px/1.45 var(--font);color:var(--mut);padding:6px 0 6px 12px;
  border-left:1.5px solid var(--ink-10);margin:0 0 10px;
  animation:fadeUp .35s var(--ease) both;
}
.ambient-note.attention{
  border-left-color:var(--acc);color:var(--charcoal);
}
.provenance-stack{display:flex;flex-direction:column;gap:0;margin-top:10px}
.provenance-stack .pv-step{
  display:grid;grid-template-columns:16px 1fr;gap:10px;padding:8px 0;
  border-left:1px solid var(--acc-25);margin-left:7px;padding-left:14px;
  animation:inkBleed .35s var(--ease) both;font-size:13px;
}
.provenance-stack .pv-step .pv-dot{
  width:9px;height:9px;border-radius:50%;background:var(--acc);
  margin-left:-19px;margin-top:4px;box-shadow:0 0 0 3px var(--paper);
}
.provenance-stack .pv-label{
  font:11px/1.2 var(--sans);color:var(--mut);
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
  line-height:1.25;letter-spacing:var(--track-tight);color:var(--navy);margin:0;
  animation:fadeUp .32s var(--ease) both;
}
.rd-heading{
  font-size:var(--r-section);font-weight:600;line-height:1.35;
  color:var(--navy);margin:6px 0 0;letter-spacing:-.015em;
}
.rd-takeaway{
  font-size:var(--r-body);font-weight:500;line-height:1.5;color:var(--navy);
  padding:10px 0 12px;border-bottom:1px solid var(--ink-08);margin:0;
}
.rd-takeaway .rd-kicker{
  display:block;font:500 10px/1 var(--mono);letter-spacing:.08em;
  color:var(--mut);margin-bottom:6px;
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
  transition:box-shadow var(--dur) var(--ease),border-color var(--dur) var(--ease);
}
.rd-card:hover{box-shadow:var(--shadow-workspace);border-color:var(--line-strong)}
.rd-card .rd-card-head{
  display:flex;align-items:center;gap:8px;margin-bottom:6px;
  font:600 11px/1.2 var(--sans);
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
  background:var(--acc-07);border-color:var(--acc-20);
}
.rd-card.key_idea .rd-card-head,.rd-card.concept .rd-card-head{color:var(--acc)}
.rd-card.key_idea .rd-card-icon,.rd-card.concept .rd-card-icon{
  background:var(--acc-15);color:var(--acc);
}
.rd-card.definition{
  background:rgba(95,179,158,.09);border-color:rgba(95,179,158,.28);
}
.rd-card.definition .rd-card-head{color:var(--emerald)}
.rd-card.definition .rd-card-icon{background:rgba(95,179,158,.16);color:var(--emerald)}
.rd-card.example{
  background:var(--ink-03);border-color:var(--ink-08);
}
.rd-card.example .rd-card-head{color:var(--charcoal)}
.rd-card.example .rd-card-icon{background:var(--ink-06);color:var(--navy)}
.rd-card.warning,.rd-card.mistake{
  background:rgba(217,160,63,.1);border-color:rgba(217,160,63,.32);
}
.rd-card.warning .rd-card-head,.rd-card.mistake .rd-card-head{color:var(--warn)}
.rd-card.warning .rd-card-icon,.rd-card.mistake .rd-card-icon{
  background:rgba(217,160,63,.18);color:var(--warn);
}
.rd-card.note,.rd-card.summary{
  background:var(--ink-03);border-color:var(--ink-08);
}
.rd-card.note .rd-card-head,.rd-card.summary .rd-card-head{color:var(--mut)}
.rd-formula{
  margin:8px 0;padding:16px 12px;text-align:center;
  background:var(--ink-03);border:1px solid var(--ink-06);
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
  background:var(--panel-2);color:var(--navy);font:13px/1.5 var(--mono);
  overflow-x:auto;white-space:pre;
}
.rd-actions{
  display:flex;flex-wrap:wrap;gap:8px;margin-top:4px;padding-top:12px;
  border-top:1px solid var(--ink-06);
}
.rd-actions button{
  background:var(--bg-elev);border:1px solid var(--line);border-radius:var(--radius-sm);
  padding:8px 12px;font:500 13px/1.2 var(--font);color:var(--charcoal);
  cursor:pointer;
}
.rd-actions button:hover{
  border-color:var(--acc-40);color:var(--navy);
  background:var(--acc-06);
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
  font:500 10px/1.2 var(--sans);
  color:var(--mut);margin-bottom:2px;
}
.rd-grounding .rd-g-item{
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:2px;
}
.msg.result .msg-body.rd-host{white-space:normal;font-size:inherit;line-height:inherit}
@media (prefers-reduced-motion:reduce){
  .rd,.rd-title,.rd-card,.rd-formula{animation:none}
}
"""

CHROME_CSS = """\
a{color:inherit;text-decoration:none}
.top,.chrome{
  background:var(--chrome-bg, rgba(16,18,22,.88));
  border-bottom:1px solid var(--line);
}
.top{position:sticky;top:0;z-index:var(--z-raised)}
.chrome .top{position:static}
@supports (backdrop-filter: blur(10px)) {
  .top,.chrome{
    background:rgba(16,18,22,.88);
    backdrop-filter:blur(10px);-webkit-backdrop-filter:blur(10px);
  }
}
.brand{
  display:inline-flex;align-items:center;gap:10px;
  font-family:var(--serif);font-size:19px;font-weight:600;
  letter-spacing:.01em;color:var(--text);line-height:1;margin-right:16px;
  transition:opacity var(--dur) var(--ease);
}
.brand:hover{opacity:.85}
.brand .mark{flex:0 0 auto;display:block}
/* The shell never announces the page — current route lives on the nav item. */
.page-sub{display:none}
.top{
  display:flex;align-items:center;gap:4px;flex-wrap:wrap;
  min-height:56px;
  padding:6px max(24px,calc((100% - 1120px)/2));
}
.brand,.page-sub,.nav-profile{flex:0 0 auto}
.nav{display:flex;gap:2px;align-items:center;flex-wrap:wrap;min-width:0}
.nav-profile{
  order:20;width:32px;height:32px;padding:0;margin:0;
  display:inline-flex;align-items:center;justify-content:center;
  border:1px solid var(--line);border-radius:var(--radius-full);
  background:var(--panel);box-shadow:0 1px 2px rgba(0,0,0,.25);
  transition:border-color var(--dur-fast) var(--ease-io),background var(--dur-fast) var(--ease-io);
}
.nav-profile:hover{border-color:var(--line-strong);background:var(--bg-elev)}
.nav-profile.on{border-color:var(--line-strong);background:var(--ink-06)}
.nav-profile-dot{
  width:10px;height:10px;border-radius:50%;background:var(--navy);display:block;
}
.nav a, .nav-more > summary{
  color:var(--mut);font-size:14.5px;font-weight:500;
  padding:7px 13px;
  display:inline-flex;align-items:center;
  border-radius:var(--r-sm);position:relative;
  transition:color var(--dur-fast) var(--ease-io),background var(--dur-fast) var(--ease-io);
}
.nav-more{position:relative}
.nav-more > summary{
  list-style:none;cursor:pointer;user-select:none;
}
.nav-more > summary::-webkit-details-marker{display:none}
.nav-more[open] > summary, .nav-more.on > summary{
  color:var(--text);background:var(--raised);
}
.nav-more-menu{
  display:none;position:absolute;left:0;top:calc(100% + 6px);z-index:var(--z-popover);
  min-width:176px;max-width:min(220px,calc(100vw - 24px));padding:5px;border-radius:var(--radius);
  background:var(--folio);border:1px solid var(--line);
  box-shadow:var(--shadow-float);
  flex-direction:column;gap:1px;
  animation:menuIn var(--dur-fast) var(--ease) both;
  transform-origin:top left;
}
.nav-more[open] > .nav-more-menu{display:flex}
@keyframes menuIn{
  from{opacity:0;transform:scale(.97) translateY(-2px)}
  to{opacity:1;transform:none}
}
.nav-more-menu a{display:block;border-radius:var(--radius-xs);padding:7px 10px}
.nav-more-copy{
  margin:6px 6px 4px;padding:8px 10px 4px;
  border-top:1px solid var(--hairline);
  font:12px/1.35 var(--sans);color:var(--faint);user-select:text;
}
.nav a:hover, .nav-more > summary:hover{
  color:var(--text);background:var(--raised);
}
.nav a:active{background:var(--raised)}
.nav a.on, .nav a[aria-current="page"]{
  color:var(--text);background:var(--raised);
}
.nav a.on:hover{color:var(--text);background:var(--raised)}
.nav a.attn{color:var(--acc);background:var(--acc-dim);
  box-shadow:inset 0 0 0 1px var(--acc-18)}
.spacer{flex:1}
/* Button primitives — three variants, no ad-hoc styling.
   primary: violet fill, dark text, MAX ONE PER VIEW.
   secondary: transparent, --line border, muted -> text on hover.
   tertiary: text link. */
.btn-primary,.actions .go,#mnemosPrivacy .pv-btn.go{
  background:var(--violet);color:var(--acc-fg);border:1px solid transparent;
  border-radius:var(--r-sm);padding:9px 16px;min-height:36px;
  font:600 14px/1.2 var(--sans);cursor:pointer;
  text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;
}
.btn-primary:hover:not(:disabled),.actions .go:hover:not(:disabled),
#mnemosPrivacy .pv-btn.go:hover:not(:disabled){
  filter:brightness(1.08);
}
.btn-ghost,.actions a.btnish,.btnish,
.btn-quiet,.actions .quiet,#mnemosPrivacy .pv-btn.quiet{
  background:transparent;color:var(--mut);border:1px solid var(--line);
  border-radius:var(--r-sm);padding:9px 16px;min-height:36px;
  font:500 14px/1.2 var(--sans);cursor:pointer;
  text-decoration:none;display:inline-flex;align-items:center;justify-content:center;gap:6px;
}
.btn-ghost:hover:not(:disabled),.actions a.btnish:hover,.btnish:hover,
.btn-quiet:hover:not(:disabled),.actions .quiet:hover:not(:disabled),
#mnemosPrivacy .pv-btn.quiet:hover:not(:disabled){
  color:var(--text);border-color:var(--faint);
}
.btn-link{
  background:none;border:0;padding:0;min-height:0;cursor:pointer;
  color:var(--mut);font:500 13.5px/1.4 var(--sans);text-decoration:none;
}
.btn-link:hover{color:var(--text)}
/* Entity chips — pill, leading type indicator (circle violet = person,
   2px-radius square amber = entity/topic), trailing weight as faint number. */
.echip{
  display:inline-flex;align-items:center;gap:8px;max-width:100%;
  background:var(--raised);border:1px solid var(--line);border-radius:var(--radius-full);
  padding:7px 14px 7px 10px;font:500 14px/1.2 var(--sans);color:var(--text);
  cursor:pointer;transition:border-color var(--dur-fast) var(--ease-io);
}
.echip:hover{border-color:var(--faint)}
.echip .d{width:8px;height:8px;border-radius:50%;flex:0 0 auto}
.d.person{background:var(--violet);border-radius:50%}
.d.entity{background:var(--amber);border-radius:2px}
.d.screen{background:var(--green);border-radius:50%}
.d.chat{background:var(--violet);border-radius:50%}
.echip .t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:22ch}
.echip .w{color:var(--faint);font-size:12.5px;font-weight:400}
.chip-legend{display:flex;gap:18px;margin-top:14px;color:var(--faint);font-size:12.5px;flex-wrap:wrap}
.chip-legend span{display:inline-flex;align-items:center;gap:6px}
.chip-legend .d{width:7px;height:7px}
/* Empty state — says what will appear here and how to cause it. */
.empty,.empty-state{
  border:1px dashed var(--line);border-radius:var(--r-md);
  padding:16px;color:var(--mut);font-size:13.5px;line-height:1.5;
}
.empty-state a,.empty a{color:var(--violet);font-weight:600;text-decoration:none}
.empty-state a:hover,.empty a:hover{text-decoration:underline}
/* Disclosure — the only home for raw JSON anywhere in the product. */
details.disclosure > summary{
  color:var(--faint);font-size:12.5px;cursor:pointer;list-style:none;width:fit-content;
}
details.disclosure > summary::-webkit-details-marker{display:none}
details.disclosure > summary:hover{color:var(--mut)}
details.disclosure > pre{
  margin-top:6px;background:var(--ink);border:1px solid var(--line);border-radius:var(--r-sm);
  padding:10px 12px;font:12px/1.5 var(--mono);color:var(--mut);overflow-x:auto;
}
.skel{display:flex;flex-wrap:wrap;gap:8px}
.skel .bone{
  height:36px;min-width:88px;flex:1 1 120px;max-width:180px;border-radius:12px;
  background:linear-gradient(90deg,var(--panel-2) 0%,var(--bg-elev) 50%,var(--panel-2) 100%);
  background-size:200% 100%;animation:skelShine 1.6s var(--ease-io) infinite;
  border:1px solid var(--hairline);
}
.skel.rows .bone{flex:1 1 100%;max-width:none;height:44px;border-radius:10px}
@keyframes skelShine{
  from{background-position:100% 0} to{background-position:-100% 0}
}
@media(prefers-reduced-motion:reduce){.skel .bone{animation:none}}
/* Hover is a tint, press is a compression — movement stays with content. */
button,.btn,.mini,.ctl{
  transition:transform var(--dur-fast) var(--ease-io),box-shadow var(--dur-fast) var(--ease-io),
    background var(--dur-fast) var(--ease-io),border-color var(--dur-fast) var(--ease-io),
    color var(--dur-fast) var(--ease-io),
    opacity var(--dur-fast) var(--ease-io),filter var(--dur-fast) var(--ease-io);
}
button:hover:not(:disabled),.btn:hover:not(:disabled),
.mini:hover:not(:disabled),.ctl:hover:not(:disabled){
  filter:brightness(.985) saturate(1.02);
}
button:active:not(:disabled),.btn:active:not(:disabled),
.mini:active:not(:disabled),.ctl:active:not(:disabled){
  transform:scale(.97);
  box-shadow:var(--shadow-press);
}
button:disabled,.btn:disabled{cursor:default;opacity:.55}
:focus-visible{
  outline:2px solid var(--acc);outline-offset:2px;
}
button:focus:not(:focus-visible),.btn:focus:not(:focus-visible),
.mini:focus:not(:focus-visible),.ctl:focus:not(:focus-visible){
  outline:none;
}
/* The Seal + shared hold primitive */
.seal-btn{
  position:relative;isolation:isolate;overflow:hidden;
  border:1px solid var(--acc-45)!important;
  background:var(--acc-08)!important;color:var(--navy)!important;
  min-width:132px;font-weight:600;
  touch-action:none;user-select:none;-webkit-user-select:none;
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
  stroke-dashoffset:0;fill:var(--acc-12);
}
.holdable.hold-spine{position:relative}
.holdable.hold-spine::after{
  content:"";position:absolute;left:0;top:14px;bottom:14px;width:2px;
  background:var(--acc);opacity:.55;border-radius:1px;z-index:var(--z-base);
  transform-origin:top center;transform:scaleY(var(--hold-p,0));
  transition:none;pointer-events:none;
}
.holdable.hold-spine.holding::after{opacity:1}
.holdable.hold-flash{
  box-shadow:inset 0 0 0 1px var(--acc-55)!important;
}
.hold-more{
  position:absolute;right:8px;top:8px;border:0;background:transparent;
  color:var(--mut);font:14px var(--mono);cursor:pointer;padding:2px 6px;
  border-radius:var(--radius-xs);opacity:.55;
}
.hold-more:hover,.hold-more:focus{opacity:1;background:var(--panel-2);color:var(--navy)}
/* Bottom-right corner dock — one owner for rec bar / toast / ghost.
   --composer-h lifts it above the in-flow chat composer so Send is never covered.
   --composer-min is a first-paint floor on chat (JS then measures the real height). */
body:has(> .dock){--composer-min:152px}
#mnemosDockBR{
  position:fixed;right:16px;bottom:calc(16px + max(var(--composer-h, 0px), var(--composer-min, 0px)));z-index:var(--z-float);
  display:flex;flex-direction:column;gap:10px;align-items:flex-end;
  /* Wide enough for Mic+Screen+System audio on one wrap line; RecBar must
     never outgrow this or flex-end clips the left edge (overflow:auto). */
  max-width:min(320px,calc(100vw - 24px));
  max-height:calc(100vh - var(--chrome-h) - max(var(--composer-h, 0px), var(--composer-min, 0px)) - 32px);
  max-height:calc(100dvh - var(--chrome-h) - max(var(--composer-h, 0px), var(--composer-min, 0px)) - 32px);
  overflow-x:visible;overflow-y:auto;pointer-events:none;
}
#mnemosDockBR > *{pointer-events:auto}
/* Recording indicator — docks into #mnemosDockBR; never self-positions.
   One surface panel (same recipe as .float-surface), quiet ghost chips inside —
   nested pill-cards on a glass slab read as a bolted-on widget. */
#mnemosRecBar{
  position:relative;z-index:auto;right:auto;bottom:auto;
  display:flex;flex-direction:column;gap:4px;align-items:stretch;
  box-sizing:border-box;width:max-content;max-width:100%;
  pointer-events:auto;font-family:var(--font);
  background:var(--surface);border:1px solid var(--line);
  border-radius:var(--radius-sm);padding:5px;
}
#mnemosRecBar:empty{display:none}
#mnemosRecBar .rec-chip,#mnemosRecBar .rec-consent-btn,#mnemosPrivacy .pv-btn{
  pointer-events:auto;
}
#mnemosRecBar .rec-row{
  display:flex;gap:2px;flex-wrap:wrap;justify-content:flex-start;
  max-width:100%;
}
#mnemosRecBar .rec-chip{
  display:inline-flex;align-items:center;gap:7px;
  background:transparent;border:1px solid transparent;
  border-radius:var(--radius-xs);padding:5px 10px 5px 8px;cursor:pointer;
  box-shadow:none;font-size:12px;font-weight:500;
  letter-spacing:var(--track-snug);color:var(--text);
}
#mnemosRecBar .rec-chip:hover,
#mnemosRecBar .rec-chip:focus-visible{
  background:var(--raised);border-color:var(--line);color:var(--text);
}
#mnemosRecBar .rec-chip .dot,
#mnemosRecBar .rec-status .dot{
  width:7px;height:7px;border-radius:50%;background:var(--danger);flex:0 0 auto;
  box-shadow:0 0 0 0 rgba(224,113,106,.45);
  animation:recPulse 1.6s var(--ease) infinite;
}
#mnemosRecBar .rec-chip.paused .dot{
  background:var(--faint);animation:none;box-shadow:none;
}
#mnemosRecBar .rec-chip.paused{color:var(--mut);font-weight:500}
#mnemosRecBar .rec-chip.voice-on .dot,
#mnemosRecBar .rec-meta.voice-on .dot{
  background:var(--acc);animation:none;box-shadow:none;
}
#mnemosRecBar .rec-chip.stop-all{color:var(--danger)}
#mnemosRecBar .rec-chip.stop-all:hover,
#mnemosRecBar .rec-chip.stop-all:focus-visible{color:var(--danger)}
/* Glanceable capture state — Listening / Idle / Capture (hosted). */
#mnemosRecBar .rec-status{
  display:inline-flex;align-items:center;gap:7px;
  padding:5px 10px 5px 8px;border-radius:var(--radius-xs);
  font-size:12px;font-weight:600;letter-spacing:var(--track-snug);
  color:var(--text);text-decoration:none;border:1px solid transparent;
}
#mnemosRecBar .rec-status.idle{color:var(--mut);font-weight:500}
#mnemosRecBar .rec-status.idle .dot{
  background:var(--green);animation:none;box-shadow:none;
}
#mnemosRecBar .rec-status.hosted{
  color:var(--acc);cursor:pointer;font-weight:500;
}
#mnemosRecBar .rec-status.hosted .dot{
  background:var(--acc);animation:none;box-shadow:none;
}
#mnemosRecBar .rec-status.hosted:hover,
#mnemosRecBar .rec-status.hosted:focus-visible{
  background:var(--acc-10);border-color:var(--acc-35);
}
/* Trailing settings — Privacy / Voice are not capture sources. */
#mnemosRecBar .rec-sep{
  width:1px;align-self:stretch;min-height:18px;margin:2px 4px;
  background:var(--line);flex:0 0 auto;
}
#mnemosRecBar .rec-meta{
  display:inline-flex;align-items:center;gap:6px;
  background:transparent;border:0;border-radius:var(--radius-xs);
  padding:5px 8px;cursor:pointer;
  font:500 12px/1.2 var(--font);letter-spacing:var(--track-snug);
  color:var(--mut);
}
#mnemosRecBar .rec-meta:hover,
#mnemosRecBar .rec-meta:focus-visible{color:var(--text);background:var(--raised)}
#mnemosRecBar .rec-meta .dot{
  width:6px;height:6px;border-radius:50%;background:var(--faint);flex:0 0 auto;
}
#mnemosRecBar .rec-meta.voice-on{color:var(--text)}
/* Pages without an in-flow composer can scroll content out from under the dock. */
body:not(:has(> .dock)){padding-bottom:var(--dock-clear,72px)}
#mnemosRecBar .rec-chip .act{
  display:none;font:10px/1 var(--font);color:var(--faint);
}
@media(hover:hover){
  #mnemosRecBar .rec-chip:hover .act,
  #mnemosRecBar .rec-chip:focus-visible .act{display:inline}
}
#mnemosRecBar .rec-chip.meeting-on{
  background:var(--violet-dim);color:var(--text);border-color:var(--acc-35);
  cursor:default;
}
#mnemosRecBar .rec-chip.meeting-on .dot{background:var(--violet)}
#mnemosRecBar .rec-consent-btn{
  border:1px solid var(--acc-40);background:var(--acc-10);
  color:var(--text);border-radius:var(--radius-xs);padding:8px 12px;font:500 12px var(--font);
  cursor:pointer;box-shadow:none;
}
#mnemosPrivacy{
  display:none;position:fixed;inset:0;z-index:var(--z-modal);background:var(--overlay);
  align-items:center;justify-content:center;padding:24px 16px;
}
#mnemosPrivacy.open{display:flex}
#mnemosPrivacy .pv-sheet{
  width:min(480px,100%);max-height:calc(100dvh - 48px);overflow:auto;
  background:var(--folio);border:1px solid var(--line);
  border-radius:var(--radius-lg);box-shadow:var(--shadow-float);padding:24px 24px 20px;
  animation:sheetIn var(--dur) var(--ease) both;
}
@keyframes sheetIn{
  from{opacity:0;transform:translateY(10px) scale(.98)}
  to{opacity:1;transform:none}
}
.mnemos-hold-tip{
  position:fixed;z-index:var(--z-modal);max-width:240px;padding:8px 10px;
  font:11px/1.35 var(--mono);color:var(--navy);
  background:rgba(22,22,27,.97);border:1px solid var(--acc-35);
  border-radius:var(--radius-xs);box-shadow:var(--shadow-float);pointer-events:none;
}
#mnemosPrivacy h2{
  font-family:var(--display);font-weight:400;font-size:1.55rem;margin:0 0 8px;
  color:var(--navy);letter-spacing:var(--track-tight);
}
#mnemosPrivacy .pv-lead{color:var(--mut);font-size:14px;margin:0 0 16px;max-width:40ch}
#mnemosPrivacy label.pv-src{
  display:flex;gap:10px;align-items:flex-start;padding:10px 0;
  border-top:1px solid var(--line);cursor:pointer;font-size:14px;
}
#mnemosPrivacy label.pv-src:first-of-type{border-top:0}
#mnemosPrivacy label.pv-src input{margin-top:3px;flex:0 0 auto;accent-color:var(--acc)}
#mnemosPrivacy .pv-src b{display:block;color:var(--navy);font-weight:600}
#mnemosPrivacy .pv-src span{display:block;color:var(--mut);font-size:12px;margin-top:2px}
#mnemosPrivacy .pv-warn{
  margin:12px 0 0;padding:10px 12px;border-radius:var(--radius-sm);font-size:12px;
  background:rgba(224,113,106,.08);border:1px solid rgba(224,113,106,.28);color:var(--danger);
}
#mnemosPrivacy .pv-actions{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;justify-content:flex-end}
#mnemosPrivacy .pv-btn{
  border-radius:var(--radius-sm);padding:9px 16px;min-height:36px;
  font:500 13px/1.2 var(--font);letter-spacing:var(--track-snug);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
  display:inline-flex;align-items:center;justify-content:center;
}
#mnemosPrivacy .pv-btn.go{background:var(--acc);color:var(--acc-fg);border:none}
#mnemosPrivacy .pv-btn.quiet{background:transparent;color:var(--mut)}
@keyframes recPulse{
  0%{box-shadow:0 0 0 0 rgba(224,113,106,.4)}
  70%{box-shadow:0 0 0 8px rgba(224,113,106,0)}
  100%{box-shadow:0 0 0 0 rgba(224,113,106,0)}
}
@media (max-width:720px){
  .top{gap:8px;row-gap:6px;padding:8px 12px}
  .page-sub{display:none}
  .nav{flex:1 0 100%;order:5}
  .nav a, .nav-more > summary{padding:7px 10px}
}
@media (prefers-reduced-motion:reduce){
  .brand:hover .mark,
  button:active:not(:disabled),.btn:active:not(:disabled),
  .mini:active:not(:disabled),.ctl:active:not(:disabled){transform:none;box-shadow:none}
  .brand .mark path,.brand .mark .mark-dot,.ink-rule,.ink-divider,.seal-btn.holding .seal-ring circle,
  .holdable.holding .hold-ring circle,.nav-more-menu,
  #mnemosRecBar .rec-chip .dot,#mnemosRecBar .rec-status .dot,#mnemosPrivacy .pv-sheet{animation:none}
  .seal-btn.holding .seal-ring circle,.holdable.holding .hold-ring circle{stroke-dashoffset:0}
  .holdable.hold-spine::after{transition:none}
}
@media (forced-colors: active){
  :root{--acc:CanvasText;--acc-dim:Highlight;--navy:CanvasText;--mut:GrayText;--line:CanvasText}
  #mnemosApproval,.band.proposal,.holdable.hold-spine::after,.row::before{
    border:2px solid Highlight !important;
  }
  .nav a.on,.seal-btn,.holdable.holding{
    outline:2px solid Highlight;outline-offset:2px;
  }
}
/* Global Ask trigger + worker status dot — live in the shell, every route. */
.nav-right{margin-left:auto;display:flex;align-items:center;gap:12px}
.ask-trigger{
  display:inline-flex;align-items:center;gap:8px;
  background:var(--raised);border:1px solid var(--line);border-radius:var(--r-sm);
  color:var(--mut);padding:7px 12px;font:400 14px/1.2 var(--sans);cursor:text;
  min-width:200px;text-align:left;
}
.ask-trigger:hover{color:var(--text);border-color:var(--faint)}
.ask-trigger kbd{
  margin-left:auto;font:400 11.5px/1 var(--sans);color:var(--faint);
  border:1px solid var(--line);border-radius:4px;padding:2px 5px;
}
.status-dot{
  width:9px;height:9px;border-radius:50%;background:var(--green);
  border:0;padding:0;flex:0 0 auto;cursor:default;display:inline-block;
  box-shadow:0 0 0 3px rgba(116,194,148,.15);
}
.status-dot.warn{background:var(--amber);box-shadow:0 0 0 3px rgba(223,179,94,.15)}
.status-dot.changed{animation:dotPulse .9s var(--ease) 1}
@keyframes dotPulse{
  0%{box-shadow:0 0 0 3px rgba(116,194,148,.3)}
  60%{box-shadow:0 0 0 8px rgba(116,194,148,0)}
  100%{box-shadow:0 0 0 3px rgba(116,194,148,.15)}
}
/* Global Ask dialog (injected by MnemosAsk). */
#mnemosAsk{
  display:none;position:fixed;inset:0;z-index:var(--z-modal);background:var(--overlay);
  align-items:flex-start;justify-content:center;padding:12vh 16px 16px;
}
#mnemosAsk.open{display:flex}
#mnemosAsk .ask-sheet{
  width:min(560px,100%);max-height:calc(100dvh - 48px);overflow:auto;
  background:var(--surface);border:1px solid var(--line);
  border-radius:var(--r-lg);padding:22px 24px 20px;
  animation:sheetIn var(--dur) var(--ease) both;
}
#mnemosAsk h3{
  font-family:var(--serif);font-weight:500;font-size:19px;
  margin:0 0 4px;color:var(--text);
}
#mnemosAsk .ask-hint{color:var(--mut);font-size:13.5px;margin:0 0 12px}
#mnemosAsk textarea{
  width:100%;min-height:72px;resize:vertical;font:inherit;
  padding:13px 14px;border:1px solid var(--line);
  border-radius:var(--r-md);background:var(--ink);color:var(--text);
}
#mnemosAsk textarea:focus{outline:2px solid var(--violet);outline-offset:-1px;border-color:transparent}
#mnemosAsk .ask-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
@media(prefers-reduced-motion:reduce){
  .status-dot.changed{animation:none}
  #mnemosAsk .ask-sheet{animation:none}
}
"""


def nav_markup() -> str:
    """Shared chrome: daily work in the bar, connections/settings under More."""
    import os
    tester = os.environ.get("QUILL_PROFILE", "").strip().lower() == "tester"
    hide = {"phone", "desktop", "org"} if tester else set()
    primary = (
        ('/today', 'Today', ''),
        ('/chat', 'Chat', ' id="navChat"'),
        ('/memory', 'Memory', ''),
        ('/profile?tab=people', 'People', ''),
    )
    more = (
        ('/meetings', 'Meetings', 'meetings'),
        ('/peer', 'Team', 'team'),
        ('/phone', 'Phone', 'phone'),
        ('/desktop-access', 'Desktop', 'desktop'),
        ('/org-network', 'Org', 'org'),
        ('/onboarding', 'Setup', 'setup'),
    )
    links = "".join(
        f'<a href="{href}"{extra}>{label}</a>' for href, label, extra in primary)
    extra_links = "".join(
        f'<a href="{href}">{label}</a>'
        for href, label, key in more if key not in hide)
    right = (
        '<div class="nav-right">'
        f'<button type="button" class="ask-trigger" id="mnemosAskOpen">'
        f'Ask {BRAND} anything… <kbd>⌘K</kbd></button>'
        '<button type="button" class="status-dot" id="mnemosStatusDot"'
        ' title="worker" aria-label="worker status"></button>'
        '<a class="nav-profile" href="/profile" title="You" aria-label="Profile">'
        '<span class="nav-profile-dot" aria-hidden="true"></span>'
        '</a></div>'
    )
    return (
        f'<nav class="nav" id="mnemosNav" aria-label="Primary">'
        f'{links}'
        f'<details class="nav-more">'
        f'<summary>More</summary>'
        f'<div class="nav-more-menu">{extra_links}'
        f'<div class="nav-more-copy">{COPYRIGHT}</div></div>'
        f'</details></nav>'
        f'<span class="spacer"></span>'
        f'{right}'
        f'<script>(function(){{'
        f'var n=document.getElementById("mnemosNav");if(!n)return;'
        f'var p=(location.pathname||"/").replace(/\\/$/,"")||"/";'
        f'var q=(location.search||"");'
        f'function hit(h){{'
        f'if(h==="/today")return p==="/today"||p==="/"||p==="/shell";'
        f'if(h==="/meetings")return p==="/meetings"||p.indexOf("/meetings/")===0||p.indexOf("/meeting/")===0;'
        f'if(h==="/chat")return p==="/chat"||p.indexOf("/chat/")===0;'
        f'if(h==="/memory")return p==="/memory"||p==="/console";'
        f'if(h.indexOf("/profile?tab=people")===0)'
        f'return p==="/profile"&&q.indexOf("tab=people")>=0;'
        f'if(h==="/profile")return p==="/profile";'
        f'if(h==="/peer")return p==="/peer"||p.indexOf("/peer/")===0;'
        f'if(h==="/phone")return p.indexOf("/phone")===0;'
        f'if(h==="/desktop-access")return p==="/desktop-access";'
        f'if(h==="/org-network")return p==="/org-network"||p.indexOf("/org/")===0;'
        f'if(h==="/onboarding")return p==="/onboarding";'
        f'return p===h;}}'
        f'var moreOn=false;'
        f'n.querySelectorAll("a[href]").forEach(function(a){{'
        f'if(hit(a.getAttribute("href")||"")){{a.classList.add("on");'
        f'a.setAttribute("aria-current","page");'
        f'if(a.closest(".nav-more"))moreOn=true;}}'
        f'}});'
        f'var m=n.querySelector(".nav-more");'
        f'if(moreOn&&m)m.classList.add("on");'
        f'var pf=document.querySelector(".nav-profile");'
        f'if(pf&&p==="/profile"&&q.indexOf("tab=people")<0)pf.classList.add("on");'
        f'document.addEventListener("click",function(e){{'
        f'if(m&&m.open&&!m.contains(e.target))m.removeAttribute("open");}});'
        f'}})();</script>'
    )


def apply_plain(page: str) -> str:
    """Utility pages: fonts + tokens only (no nav, chrome, or UI bundle)."""
    v = asset_version()
    return (
        page.replace("@@FONTS@@", font_links(v))
        .replace("@@ROOT@@", ROOT_TOKENS)
        .replace("@@BRAND@@", BRAND)
        .replace("@@COMPANY@@", COMPANY)
        .replace("@@COPYRIGHT@@", COPYRIGHT)
    )


def apply(page: str) -> str:
    """Inject shared fonts/tokens/ink/chrome/UI into a page with @@placeholders@@.

    Leaves @@APPROVAL@@ for per-request SSR via approval_partial.inject_page.
    Pages that render math include @@KATEX@@ after @@FONTS@@ (Chat, Console).
    """
    v = asset_version()
    return (
        page.replace("@@FONTS@@", font_links(v) + "\n" + theme_style_links(v))
        .replace("@@KATEX@@", KATEX_LINKS)
        .replace("@@ROOT@@", ROOT_TOKENS)
        .replace("@@INK@@", "")
        .replace("@@CHROME@@", "")
        .replace("@@UI_JS@@", theme_script_links(v))
        .replace("@@MARK@@", BRAND_MARK)
        .replace("@@BRAND@@", BRAND)
        .replace("@@COMPANY@@", COMPANY)
        .replace("@@COPYRIGHT@@", COPYRIGHT)
        .replace("@@NAV@@", nav_markup())
    )
