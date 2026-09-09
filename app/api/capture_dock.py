"""Capture dock — the compact companion window that owns the capture streams.

Why a separate window at all: a MediaStream dies with the document that
created it, and the main app does full page navigations (Today, Chat, Memory
are separate documents). Capture started "inline" on one of those pages would
be killed by the user's next click — the worst possible failure for ambient
memory. A popup is its own top-level browsing context, so the dock keeps mic,
meeting-tab audio and screen alive while the user browses the whole app, and
survives the opener navigating away or closing.

(Document Picture-in-Picture looks like the nicer window, but its window is
owned by the opener document and closes when that document is destroyed — it
cannot survive navigation, which is the entire point here. Popup it is.)

The dock runs the SAME engine as /capture (capture_page.CAPTURE_CORE_JS) and
paints into the same element ids, so there is one capture code path, not two.
Live state is published server-side (GET /capture/status → "web"), so every
page shows it without cross-window messaging; a BroadcastChannel carries only
*commands* (stop/pause from the recording bar) and a "dock is open" ping.
"""

from app.api.capture_page import CAPTURE_CORE_JS
from app.api.mnemos_theme import apply as _mnemos

# Query flags the opener sets from the Privacy sheet's ticked sources, e.g.
# /capture/dock?mic=1&tab=1 — the dock arms those rows and auto-starts what it
# can without a fresh user gesture (getUserMedia); getDisplayMedia always
# needs one click here, because Chrome requires transient activation in the
# document that will own the stream.
_DOCK_HEAD = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Capture · @@BRAND@@</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 var(--font);color:var(--text);
  background:var(--paper);padding:12px 14px 16px}
h1{font:600 13px var(--font);color:var(--navy);margin:0 0 2px;
  display:flex;align-items:center;gap:8px}
h1 .sub{font:11px var(--sans);color:var(--mut);font-weight:400;margin-left:auto}
.lead{font-size:12px;color:var(--mut);margin:0 0 12px;line-height:1.45}
.src{border:1px solid var(--line);border-radius:10px;background:var(--surface);
  padding:10px 11px;margin-bottom:8px}
.src.armed{border-color:var(--acc)}
.src h2{font:600 12.5px var(--font);color:var(--navy);margin:0;
  display:flex;align-items:center;gap:8px}
.status{font:11px var(--mono);color:var(--mut);margin-left:auto}
.dot{width:9px;height:9px;border-radius:50%;background:var(--line);flex:0 0 auto}
.dot.rec{background:#C0392B;box-shadow:0 0 0 3px rgba(192,57,43,.18);
  animation:pulse 1.4s infinite}
.dot.paused{background:#D8A200}
.dot.connecting{background:var(--acc)}
.dot.err{background:#C0392B}
@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(192,57,43,.06)}}
.row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}
button{border-radius:7px;padding:6px 11px;font:500 12px var(--font);
  cursor:pointer;border:1px solid var(--line);background:var(--panel);
  color:var(--navy)}
button.primary{background:var(--acc);color:var(--acc-fg);border:none}
button.danger{background:#C0392B;color:#FFF;border:none}
button:disabled{opacity:.45;cursor:default}
.meter{font:11px var(--mono);color:var(--mut);margin-left:auto}
.hint{font-size:11.5px;color:var(--mut);margin-top:6px;line-height:1.45}
.consent{margin-top:8px;padding:8px 9px;border:1px dashed var(--line);
  border-radius:8px;font-size:11.5px}
.consent b{color:var(--navy)}
.banner{display:none;margin:0 0 10px;padding:9px 10px;border-radius:9px;
  border:1px solid #C0392B;background:rgba(192,57,43,.10);font-size:12px}
.banner.show{display:block}
.notice{display:none;margin-top:6px;font-size:11.5px;color:var(--mut)}
.notice.show{display:block}
.priv{display:none;margin-top:6px;font:11px var(--mono);color:#2E7D32}
.priv.show{display:block}
.foot{margin-top:12px;padding-top:10px;border-top:1px solid var(--line);
  display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.foot a{color:var(--acc);font-size:11.5px;margin-left:auto}
#meet-title{font:inherit;font-size:12px;padding:6px 8px;flex:1 1 120px;
  border:1px solid var(--line);border-radius:7px;background:var(--panel);
  color:var(--text)}
</style></head><body>
<h1><span class="dot" id="dot-any"></span>@@BRAND@@ capture
  <span class="sub" id="keepopen">keep this window open</span></h1>
<p class="lead">This little window holds your microphone, meeting audio and
screen. Browse @@BRAND@@ freely in the main window — capture keeps running
here.</p>

<div id="offline" class="banner">Connection lost — audio is <b>not</b> being
captured.</div>

<div class="src" id="card-mic">
  <h2><span class="dot" id="dot-mic"></span>Microphone
      <span class="status" id="st-mic">off</span></h2>
  <div class="consent" id="consent-mic">
    <b>Not allowed yet.</b>
    <button id="optin-mic">Allow microphone</button>
  </div>
  <div class="row">
    <button id="start-mic" class="primary">Start mic</button>
    <button id="pause-mic" disabled>Pause</button>
    <button id="stop-mic" disabled>Stop</button>
    <span class="meter" id="meter-mic"></span>
  </div>
  <div class="priv" id="priv-mic">&#128274; Speech only — silence stays local.</div>
</div>

<div class="src" id="card-tab">
  <h2><span class="dot" id="dot-tab"></span>Meeting audio
      <span class="status" id="st-tab">off</span></h2>
  <div class="consent" id="consent-tab">
    <b>Not allowed yet.</b>
    <button id="optin-tab">Allow meeting audio</button>
  </div>
  <div class="row">
    <button id="start-tab" class="primary">Share meeting tab</button>
    <button id="pause-tab" disabled>Pause</button>
    <button id="stop-tab" disabled>Stop</button>
    <span class="meter" id="meter-tab"></span>
  </div>
  <div class="hint">Pick the meeting tab and tick &ldquo;Also share tab
  audio&rdquo;. Records other participants.</div>
  <div class="hint" id="share-what" hidden></div>
  <div class="priv" id="priv-tab">&#128274; Speech only — silence stays local.</div>
  <div class="notice" id="no-tab-audio">This browser can&rsquo;t share tab
  audio (Chromium only). Your mic still records the room.</div>
</div>

<div class="src" id="card-screen">
  <h2><span class="dot" id="dot-screen"></span>Screen
      <span class="status" id="st-screen">off</span></h2>
  <div class="consent" id="consent-screen">
    <b>Not allowed yet.</b>
    <button id="optin-screen">Allow screen</button>
  </div>
  <div class="row">
    <button id="start-screen" class="primary">Share screen</button>
    <button id="stop-screen" disabled>Stop</button>
    <span class="meter" id="meter-screen"></span>
  </div>
  <div class="priv" id="priv-screen">&#128274; One frame every few seconds,
  only on change.</div>
</div>

<div class="src" id="card-meeting">
  <h2><span class="dot" id="dot-meet"></span>Meeting
      <span class="status" id="st-meet">none</span></h2>
  <div class="row" id="meet-setup">
    <input id="meet-title" placeholder="Meeting title (optional)" maxlength="200">
    <button id="meeting" class="primary">Start</button>
  </div>
  <div class="row">
    <button id="end-meeting" class="danger" hidden>End meeting</button>
  </div>
</div>

<div class="foot">
  <button id="stop-all">Stop everything</button>
  <a href="/capture" target="_blank" rel="noopener">Full capture page</a>
</div>

<script>
"""

_DOCK_TAIL = r"""
/* --- dock shell: consent opt-ins, meeting, auto-arm, cross-window ---------
   The engine above is byte-identical to /capture; only the shell differs. */
const FLAGS = new URLSearchParams(location.search);
const WANT = {mic: FLAGS.get('mic') === '1', tab: FLAGS.get('tab') === '1',
              screen: FLAGS.get('screen') === '1'};

$('optin-mic').onclick = async () => {
  await post('/capture/consent', {mic: true}); await refreshConsent(); armWanted(); };
$('optin-tab').onclick = async () => {
  await post('/capture/consent', {system_audio: true}); await refreshConsent(); armWanted(); };
$('optin-screen').onclick = async () => {
  await post('/capture/consent', {screen: true}); await refreshConsent(); armWanted(); };
$('start-mic').onclick = () => startMic().catch(e => note('Mic: ' + e.message));
$('start-tab').onclick = () => startTab().catch(e => {
  if (e.message !== 'no tab audio') note('Meeting audio: ' + e.message); });
$('start-screen').onclick = () => scr.start().catch(e => {
  if (e.name !== 'NotAllowedError') note('Screen: ' + e.message); });
$('pause-mic').onclick = () => mic.togglePause();
$('pause-tab').onclick = () => tab.togglePause();
$('stop-mic').onclick = () => mic.stop();
$('stop-tab').onclick = () => { tab.stop(); $('share-what').hidden = true; };
$('stop-screen').onclick = () => scr.stop();

/* The engine calls this once a tab share succeeds: show WHAT is being
   captured (the tab's own title), so a mis-picked surface is obvious
   immediately rather than after the meeting. */
function onShareStarted(share) {
  const el = $('share-what');
  const what = (share.label || '').trim();
  if (!what) { el.hidden = true; return; }
  el.hidden = false;
  el.textContent = 'Capturing audio from: ' + what;
  // Offer it as the meeting title when one hasn't been typed.
  const title = $('meet-title');
  if (title && !title.value.trim() && !MEET.active) title.value = what;
}

function note(msg) {
  const b = $('offline');
  b.textContent = msg;
  b.classList.add('show');
  setTimeout(() => { b.classList.remove('show'); b.innerHTML =
    'Connection lost — audio is <b>not</b> being captured.'; }, 6000);
}

/* Highlight the rows the opener asked for, and start what we can without a
   fresh gesture. getDisplayMedia (tab audio, screen) needs a click IN THIS
   window — the browser requires transient activation in the document that
   will own the stream — so those rows are armed and wait for one click. */
function armWanted() {
  ['mic', 'tab', 'screen'].forEach(k => {
    $('card-' + k).classList.toggle('armed', !!WANT[k]);
  });
  if (WANT.mic && consentState.mic && mic.state === 'off') {
    WANT.mic = false;                       // one attempt; no prompt loops
    startMic().catch(e => note('Mic: ' + e.message));
  }
}

/* --- meeting session ---------------------------------------------------- */
let MEET = {active: false, title: ''};
function paintMeeting() {
  $('dot-meet').className = 'dot' + (MEET.active ? ' rec' : '');
  $('st-meet').textContent = MEET.active
    ? ('recording — ' + (MEET.title || 'Meeting')) : 'none';
  $('meeting').hidden = MEET.active;
  $('meet-setup').style.display = MEET.active ? 'none' : '';
  $('end-meeting').hidden = !MEET.active;
}
async function refreshMeeting() {
  try {
    const s = await (await fetch('/meeting/session/status')).json();
    MEET = {active: !!s.active, title: s.title || ''};
  } catch (e) {}
  paintMeeting();
}
$('meeting').onclick = async () => {
  try {
    const d = await post('/meeting/session/start',
      {title: $('meet-title').value.trim()});
    if (!d.ok) { note('Meeting: ' + (d.error || 'could not start')); return; }
    MEET = {active: true, title: (d.session && d.session.title) || ''};
    paintMeeting();
  } catch (e) { note('Meeting: ' + e.message); return; }
  try { if (mic.state === 'off') await startMic(); }
  catch (e) { note('Mic: ' + e.message); }
  try { if (tab.state === 'off') await startTab(); }
  catch (e) { if (e.message !== 'no tab audio') note('Meeting audio: ' + e.message); }
};
$('end-meeting').onclick = async () => {
  mic.stop(); tab.stop();
  try { await post('/meeting/session/end', {}); } catch (e) {}
  MEET = {active: false, title: ''};
  paintMeeting();
  $('st-meet').textContent = 'ended — the note appears in Meetings shortly';
};
function stopEverything() {
  mic.stop(); tab.stop(); scr.stop();
  if (MEET.active) $('end-meeting').click();
}
$('stop-all').onclick = stopEverything;

/* --- cross-window control ------------------------------------------------
   Live STATE is server-side (GET /capture/status → "web"), so pages don't
   need us to tell them what is running. This channel carries commands the
   recording bar issues from any page, plus a presence ping so it can tell
   "dock open" from "dock closed". */
let BUS = null;
try { BUS = new BroadcastChannel('mnemos-capture'); } catch (e) {}
function announce() {
  if (!BUS) return;
  try { BUS.postMessage({type: 'dock', alive: true}); } catch (e) {}
}
if (BUS) {
  BUS.onmessage = ev => {
    const m = ev.data || {};
    if (m.type === 'cmd') {
      if (m.cmd === 'stop-all') stopEverything();
      else if (m.cmd === 'pause') {
        if (mic.state === 'recording') mic.togglePause();
        if (tab.state === 'recording') tab.togglePause();
      } else if (m.cmd === 'resume') {
        if (mic.state === 'paused') mic.togglePause();
        if (tab.state === 'paused') tab.togglePause();
      } else if (m.cmd === 'focus') { try { window.focus(); } catch (e) {} }
    } else if (m.type === 'ping') {
      announce();
    }
  };
}
setInterval(announce, 4000);
announce();
window.addEventListener('pagehide', () => {
  if (BUS) { try { BUS.postMessage({type: 'dock', alive: false}); } catch (e) {} }
});

/* Closing the dock ends capture — say so while something is live. The engine
   calls this on every state change; the dock's version also paints the
   header dot and the "keep this window open" line. */
updateUnloadGuard = function () {
  const live = mic.wanted || tab.wanted || scr.on;
  window.onbeforeunload = live
    ? (e => { e.preventDefault(); e.returnValue = ''; }) : null;
  $('dot-any').className = 'dot' + (live ? ' rec' : '');
  $('keepopen').textContent = live
    ? 'capture runs while this window is open'
    : 'keep this window open';
};

refreshMeeting();
setInterval(refreshMeeting, 15000);

/* The opener saves consent and opens this window in the same click, so the
   POST can still be in flight when we first read /capture/status. Re-read a
   couple of times while a requested source is still un-consented, otherwise
   a freshly ticked box would look denied here. */
const CONSENT_KEY = {mic: 'mic', tab: 'system_audio', screen: 'screen'};
async function settleConsent(tries) {
  await refreshConsent();
  armWanted();
  const pending = Object.keys(WANT).some(
    k => WANT[k] && !consentState[CONSENT_KEY[k]]);
  if (pending && tries > 0) setTimeout(() => settleConsent(tries - 1), 800);
}
settleConsent(3);
</script>
</body></html>
"""

DOCK_PAGE = _mnemos(_DOCK_HEAD + CAPTURE_CORE_JS + _DOCK_TAIL)
