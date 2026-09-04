"""Web capture page — browser mic + meeting-tab audio into /ingest/audio.

The trust surface for hosted (headless) instances: unmissable per-source
recording indicators, the same consent language and gates as the desktop
Privacy sheet, a live "last heard" ticker as proof capture works, and a
visible warning the moment audio stops flowing. Capture itself is an
AudioWorklet that resamples to 16 kHz mono s16le and streams over the
WebSocket; all intelligence stays server-side.
"""

from app.api.mnemos_theme import apply as _mnemos

CAPTURE_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Capture · @@BRAND@@</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;min-height:100vh;font:15px/1.55 var(--font);color:var(--text);
  background:
    radial-gradient(900px 480px at 8% -8%, var(--acc-06), transparent 55%),
    linear-gradient(180deg,#FBF9F4 0%,var(--paper) 40%,var(--workspace) 100%);
}
.wrap{max-width:720px;margin:0 auto;padding:8px 22px 64px}
.mast{padding:18px 0 8px}
.mast .kicker{font:12px var(--mono);color:var(--mut);letter-spacing:.04em;text-transform:uppercase}
.mast h1{font-family:var(--display);font-weight:400;font-size:clamp(1.6rem,3.2vw,2.1rem);
  color:var(--navy);margin:6px 0 0;letter-spacing:-.02em}
.mast .summary{color:var(--text);font-size:14px;margin:10px 0 0;max-width:46em}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--radius);
  box-shadow:var(--shadow-surface);padding:16px 18px;margin-top:14px}
.card h2{font:600 14px var(--font);color:var(--navy);margin:0;display:flex;align-items:center;gap:10px}
.card .hint{font-size:13px;color:var(--mut);margin:6px 0 0}
.dot{width:11px;height:11px;border-radius:50%;background:var(--line);flex:0 0 auto}
.dot.rec{background:#C0392B;box-shadow:0 0 0 4px rgba(192,57,43,.18);animation:pulse 1.4s infinite}
.dot.paused{background:#D8A200}
.dot.connecting{background:var(--acc)}
.dot.err{background:#C0392B}
@keyframes pulse{50%{box-shadow:0 0 0 7px rgba(192,57,43,.06)}}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:12px}
button{border-radius:8px;padding:8px 14px;font:500 13px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy)}
button.primary{background:var(--navy);color:#F8F6F1;border:none}
button.danger{background:#C0392B;color:#F8F6F1;border:none}
button:disabled{opacity:.45;cursor:default}
.status{font:12px var(--mono);color:var(--mut);margin-left:auto}
.consent{padding:10px 12px;border:1px dashed var(--line);border-radius:8px;
  background:rgba(248,246,241,.7);font-size:13px;margin-top:10px}
.consent b{color:var(--navy)}
.banner{display:none;margin-top:14px;padding:12px 14px;border-radius:var(--radius);
  border:1px solid #C0392B;background:rgba(192,57,43,.08);color:#8f2b20;font-size:13px}
.banner.show{display:block}
.notice{display:none;margin-top:10px;font-size:13px;color:var(--mut)}
.notice.show{display:block}
.ticker{margin-top:14px}
.ticker .line{padding:8px 0;border-top:1px solid var(--line);font-size:14px;color:var(--navy)}
.ticker .line .src{font:11px var(--mono);color:var(--mut);margin-right:8px;text-transform:uppercase}
.ticker .empty{color:var(--mut);font-size:13px;padding:8px 0}
.small{font-size:12px;color:var(--mut)}
.priv{display:none;margin-top:8px;font:12px var(--mono);color:#2E7D32}
.priv.show{display:block}
.wrap a{color:var(--acc)}
</style></head><body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Capture</span>
  @@NAV@@
</header>
<div class="wrap">
<div class="mast">
  <div class="kicker">Perceive · Web</div>
  <h1>Capture from this browser</h1>
  <p class="summary">Your microphone (and, in a meeting, the meeting tab's audio)
  streams to <b>your own @@BRAND@@ instance</b>, where it becomes transcripts and
  memory. Audio is processed on @@BRAND@@ servers. Nothing records until you opt
  in below, and every capture can be paused, stopped, or erased with a receipt.</p>
</div>

<div id="offline" class="banner">Connection lost — audio is <b>not</b> being captured.</div>

<div class="card" id="card-mic">
  <h2><span class="dot" id="dot-mic"></span>Microphone
      <span class="status" id="st-mic">off</span></h2>
  <div class="hint">What you say near this computer, transcribed and attributed to you.</div>
  <div class="consent" id="consent-mic">
    <b>Consent required.</b> Capture source “mic” is off until Privacy consent.
    <button id="optin-mic">Allow microphone capture</button>
  </div>
  <div class="row">
    <button id="start-mic" class="primary">Start mic</button>
    <button id="pause-mic" disabled>Pause</button>
    <button id="stop-mic" disabled>Stop</button>
    <span class="small" id="meter-mic"></span>
  </div>
  <div class="priv" id="priv-mic">&#128274; Private capture: audio leaves this
  device only during detected speech — silence stays local.</div>
</div>

<div class="card" id="card-tab">
  <h2><span class="dot" id="dot-tab"></span>Meeting tab audio
      <span class="status" id="st-tab">off</span></h2>
  <div class="hint">The other side of a call: share the meeting tab and tick
  “Also share tab audio”. This records other participants — it uses the same
  consent as system audio.</div>
  <div class="consent" id="consent-tab">
    <b>Consent required.</b> Capture source “system_audio” is off until Privacy consent.
    <button id="optin-tab">Allow meeting/tab audio capture</button>
  </div>
  <div class="row">
    <button id="start-tab" class="primary">Share meeting tab</button>
    <button id="pause-tab" disabled>Pause</button>
    <button id="stop-tab" disabled>Stop</button>
    <span class="small" id="meter-tab"></span>
  </div>
  <div class="priv" id="priv-tab">&#128274; Private capture: audio leaves this
  device only during detected speech — silence stays local.</div>
  <div class="notice" id="no-tab-audio">This browser can’t share tab audio
  (Chromium only). Mic-only mode still works — use headphones so the mic hears
  the far side, or keep speakers on and expect lower far-side quality.</div>
</div>

<div class="card" id="card-meeting">
  <h2><span class="dot" id="dot-meet"></span>Meeting
      <span class="status" id="st-meet">none</span></h2>
  <div class="hint">Start a meeting to get a titled session with its own
  consent, receipts and an enhanced note when it ends. Starts your mic and
  prompts for the meeting tab in one flow. Keep this tab open — closing it
  ends capture.</div>
  <div class="row" id="meet-setup">
    <input id="meet-title" placeholder="Meeting title (optional)" maxlength="200"
      style="font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px;flex:1 1 180px">
    <select id="meet-ret" style="font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px">
      <option value="transcript_only">Transcript only</option>
      <option value="keep_receipts">Keep audio receipts</option>
    </select>
  </div>
  <div class="row">
    <button id="meeting" class="primary">Start meeting</button>
    <button id="end-meeting" class="danger" hidden>End meeting</button>
    <button id="stop-all">Stop everything</button>
  </div>
</div>

<div class="card" id="card-enroll">
  <h2><span class="dot" id="dot-enroll"></span>Voice enrollment
      <span class="status" id="st-enroll"></span></h2>
  <div class="hint">Ten seconds of your voice gives transcripts your name
  instead of “Speaker 1”. Speak naturally — read anything nearby.</div>
  <div class="row">
    <input id="enroll-name" placeholder="Your name" maxlength="80"
      style="font:inherit;padding:8px 10px;border:1px solid var(--line);border-radius:8px">
    <button id="enroll-go" class="primary">Record 10s</button>
    <span class="small" id="enroll-msg"></span>
  </div>
</div>

<div class="card ticker">
  <h2 style="margin-bottom:4px">Last heard</h2>
  <div id="lines"><div class="empty">Nothing yet — start a source and say something.</div></div>
  <div class="small" style="margin-top:8px">Full timeline, playback and erase:
  <a href="/memory">Memory Console</a></div>
</div>
</div>

<script>
"use strict";
const $ = id => document.getElementById(id);
const FRAME = 512;                       // 32 ms @ 16 kHz — Silero's window
const BATCH_DEFAULT = 4;                 // frames per WS message (~128 ms)
const BATCH_THROTTLED = 16;
const RING_MAX = 320;                    // ~10 s of buffered frames on a drop

const WORKLET_SRC = `
class PcmFeeder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;     // 1 when the context honors 16 kHz
    this.carry = 0; this.prev = 0;
    this.frame = new Int16Array(${FRAME}); this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch || !ch.length) return true;
    const len = ch.length;
    const v = i => i <= 0 ? this.prev : ch[Math.min(i, len) - 1];
    let x = this.carry;
    while (x < len) {
      const i = Math.floor(x), f = x - i;
      let s = v(i) * (1 - f) + v(i + 1) * f;
      s = Math.max(-1, Math.min(1, s));
      this.frame[this.n++] = s < 0 ? s * 32768 : s * 32767;
      if (this.n === ${FRAME}) {
        const out = this.frame;
        this.port.postMessage(out.buffer, [out.buffer]);
        this.frame = new Int16Array(${FRAME}); this.n = 0;
      }
      x += this.ratio;
    }
    this.carry = x - len;
    this.prev = ch[len - 1];
    return true;
  }
}
registerProcessor('pcm-feeder', PcmFeeder);`;
const workletURL = URL.createObjectURL(
  new Blob([WORKLET_SRC], {type: 'application/javascript'}));

function csrf() {
  const m = document.cookie.match(/(?:^|;\s*)quill_csrf=([^;]+)/);
  return m ? decodeURIComponent(m[1]) : '';
}
async function post(url, body) {
  const r = await fetch(url, {method: 'POST',
    headers: {'Content-Type': 'application/json', 'x-csrf-token': csrf()},
    body: JSON.stringify(body || {})});
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}

// --- client-side VAD (Phase 4): Silero in a worker; silence stays local ---
// onnxruntime-web from /static/ort/ when the deployment bakes it in, else the
// pinned CDN. If neither loads (offline LAN, blocked CDN), createVadWorker
// resolves null and the channel streams to server-side VAD instead — capture
// never breaks, only the privacy rung steps down.
let VAD_CFG = null;
fetch('/capture/config').then(r => r.json())
  .then(c => { VAD_CFG = c; }).catch(() => {});
const ORT_BASES = ['/static/ort/',
  'https://cdn.jsdelivr.net/npm/onnxruntime-web@1.19.2/dist/'];

function createVadWorker(onUtterance, onDown) {
  return new Promise(resolve => {
    if (!VAD_CFG) { resolve(null); return; }
    let w;
    try { w = new Worker('/static/vad_worker.js'); }
    catch (e) { resolve(null); return; }
    let settled = false;
    const fail = () => {
      if (!settled) { settled = true; resolve(null); }
      else onDown();
      try { w.terminate(); } catch (e) {}
    };
    const timer = setTimeout(fail, 12000);
    w.onerror = fail;
    w.onmessage = e => {
      const m = e.data;
      if (m.type === 'ready' && !settled) {
        settled = true; clearTimeout(timer); resolve(w);
      } else if (m.type === 'unavailable') {
        clearTimeout(timer); fail();
      } else if (m.type === 'utterance') {
        onUtterance(m);
      }
    };
    w.postMessage({type: 'init', cfg: VAD_CFG, ortBases: ORT_BASES,
                   modelUrl: '/capture/vad-model'});
  });
}

class SourceChannel {
  constructor(kind) {
    this.kind = kind;                    // 'mic' | 'tab'
    this.ws = null; this.ready = false;
    this.ctx = null; this.node = null; this.tracks = [];
    this.state = 'off';                  // off|connecting|recording|paused|error
    this.ring = []; this.ringBytes = 0;  // {h?, buf} entries, byte-capped
    this.batch = []; this.batchN = BATCH_DEFAULT;
    this.bytes = 0; this.retry = 0; this.wanted = false;
    this.throttleUntil = 0;
    this.vadWorker = null;               // client-side Silero when available
    this.vadMode = 'server';
    this.framesIn = 0;                   // every captured frame, sent or not
  }
  setState(s, note) {
    this.state = s;
    const dot = $('dot-' + this.kind), st = $('st-' + this.kind);
    dot.className = 'dot ' + ({recording: 'rec', paused: 'paused',
      connecting: 'connecting', error: 'err'}[s] || '');
    st.textContent = note || s;
    $('pause-' + this.kind).disabled = (s !== 'recording' && s !== 'paused');
    $('pause-' + this.kind).textContent = (s === 'paused') ? 'Resume' : 'Pause';
    $('stop-' + this.kind).disabled = (s === 'off');
    $('start-' + this.kind).disabled = (s !== 'off');
    $('priv-' + this.kind).classList.toggle('show',
      this.vadMode === 'client' && s !== 'off' && s !== 'error');
    updateUnloadGuard();
  }
  async start(stream) {
    this.wanted = true;
    this.tracks = stream.getAudioTracks();
    if (!this.tracks.length) throw new Error('no audio track');
    // Some browsers end display capture silently; reflect it in the UI.
    this.tracks.forEach(t => t.onended = () => { if (this.wanted) this.stop(); });
    let ctx;
    try { ctx = new AudioContext({sampleRate: 16000}); }
    catch (e) { ctx = new AudioContext(); }       // Safari: resample in worklet
    this.ctx = ctx;
    await ctx.audioWorklet.addModule(workletURL);
    const src = ctx.createMediaStreamSource(new MediaStream(this.tracks));
    this.node = new AudioWorkletNode(ctx, 'pcm-feeder',
      {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
    this.node.port.onmessage = e => this.onFrame(e.data);
    src.connect(this.node);
    // Keep the graph pulled without audible output.
    const mute = ctx.createGain(); mute.gain.value = 0;
    this.node.connect(mute); mute.connect(ctx.destination);
    // Client-side VAD when the runtime loads; frames stream to server VAD
    // meanwhile and the mode flips per-frame once the worker is ready.
    this.setState('connecting');
    this.connect();
    createVadWorker(m => this.sendUtterance(m), () => this.vadDown())
      .then(w => {
        if (!w) return;
        if (!this.wanted) { try { w.terminate(); } catch (e) {} return; }
        this.vadWorker = w;
        this.vadMode = 'client';
        this.setState(this.state);       // repaint the privacy badge
      });
  }
  onFrame(buf) {
    this.framesIn++;
    if (this.state === 'paused') return;
    if (this.vadMode === 'client' && this.vadWorker) {
      this.vadWorker.postMessage({type: 'frame', pcm: buf}, [buf]);
      if (this.framesIn % 32 === 0) this.updateMeter();
    } else {
      this.enqueue(buf);
    }
  }
  sendUtterance(m) {
    const h = JSON.stringify({type: 'utterance',
      start_ts: m.start_ts, end_ts: m.end_ts});
    if (this.ready && this.ws && this.ws.readyState === 1) {
      this.ws.send(h);
      this.ws.send(m.pcm);
      this.bytes += m.pcm.byteLength;
      this.updateMeter();
    } else {
      this.pushRing({h, buf: m.pcm});
    }
  }
  vadDown() {
    // Worker died mid-run: step down to server VAD, capture uninterrupted.
    this.vadWorker = null;
    this.vadMode = 'server';
    this.setState(this.state);
  }
  updateMeter() {
    let t = (this.bytes / 1024).toFixed(0) + ' KB sent';
    if (this.vadMode === 'client') {
      const kept = Math.max(0, this.framesIn * 1024 - this.bytes);
      t += ' (speech only) · ' + (kept / 1024).toFixed(0) + ' KB kept local';
    }
    $('meter-' + this.kind).textContent = t;
  }
  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ingest/audio`);
    ws.binaryType = 'arraybuffer';
    this.ws = ws; this.ready = false;
    ws.onopen = () => {
      this.retry = 0;
      ws.send(JSON.stringify({type: 'hello', source: this.kind,
        sample_rate: 16000, format: 's16le', session_id: SESSION_ID,
        vad: this.vadMode}));
    };
    ws.onmessage = ev => {
      let m; try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === 'ready') {
        this.ready = true;
        offline(false);
        this.setState(this.state === 'paused' ? 'paused' : 'recording');
        this.drainRing();
      } else if (m.type === 'throttle') {
        this.batchN = BATCH_THROTTLED;
        this.throttleUntil = Date.now() + 10000;
      } else if (m.type === 'error') {
        this.wanted = false;
        this.teardown();
        this.setState('error', m.error);
        alert('@@BRAND@@ refused capture: ' + (m.detail || m.error));
        refreshConsent();
      } else if (m.type === 'bye') {
        console.log('bye', m);
      }
    };
    ws.onclose = () => {
      this.ready = false;
      if (!this.wanted) return;
      // Reconnect with backoff; ring-buffer audio meanwhile (bounded).
      this.setState('connecting', 'reconnecting…');
      const delay = Math.min(15000, 500 * Math.pow(2, this.retry++));
      if (this.retry > 3) offline(true);
      setTimeout(() => { if (this.wanted) this.connect(); }, delay);
    };
    ws.onerror = () => {};
  }
  enqueue(buf) {
    if (this.throttleUntil && Date.now() > this.throttleUntil) {
      this.batchN = BATCH_DEFAULT; this.throttleUntil = 0;
    }
    this.batch.push(buf);
    if (this.batch.length < this.batchN) return;
    const msg = concat(this.batch); this.batch = [];
    if (this.ready && this.ws && this.ws.readyState === 1) {
      this.ws.send(msg);
      this.bytes += msg.byteLength;
      this.updateMeter();
    } else {
      this.pushRing({buf: msg});
    }
  }
  pushRing(entry) {
    // ~10 s of raw frames, or ~60 s of speech-only utterances.
    this.ring.push(entry);
    this.ringBytes += entry.buf.byteLength;
    const cap = this.vadMode === 'client' ? 1920000 : RING_MAX * 1024;
    while (this.ringBytes > cap && this.ring.length) {
      this.ringBytes -= this.ring.shift().buf.byteLength;
      offline(true);
    }
  }
  drainRing() {
    while (this.ring.length && this.ws && this.ws.readyState === 1) {
      const e = this.ring.shift();
      this.ringBytes -= e.buf.byteLength;
      if (e.h) this.ws.send(e.h);
      this.ws.send(e.buf);
      this.bytes += e.buf.byteLength;
    }
    this.updateMeter();
  }
  ctl(type) {
    if (this.ws && this.ws.readyState === 1) {
      this.ws.send(JSON.stringify({type}));
    }
  }
  togglePause() {
    if (this.state === 'paused') { this.ctl('resume'); this.setState('recording'); }
    else if (this.state === 'recording') {
      // Ship any in-progress speech before going quiet (server flush twin).
      if (this.vadWorker) this.vadWorker.postMessage({type: 'flush'});
      this.ctl('pause'); this.setState('paused');
    }
  }
  stop() {
    this.wanted = false;
    // Release the mic/tab NOW — the browser recording indicator must drop the
    // instant the user clicks; only the WS goodbye waits for the VAD flush.
    this.tracks.forEach(t => { t.onended = null; t.stop(); });
    this.tracks = [];
    if (this.vadWorker) {
      const w = this.vadWorker;
      this.vadWorker = null;
      w.postMessage({type: 'flush'});   // ship in-progress speech first
      setTimeout(() => {
        this.ctl('stop'); this.teardown();
        try { w.terminate(); } catch (e) {}
      }, 300);
    } else {
      this.ctl('stop');
      this.teardown();
    }
    this.vadMode = 'server';
    this.setState('off');
    offline(false);
    refreshConsent();          // start buttons re-respect the consent gate
  }
  teardown() {
    this.tracks.forEach(t => { t.onended = null; t.stop(); });
    this.tracks = [];
    if (this.ctx) { this.ctx.close().catch(() => {}); this.ctx = null; }
    if (this.ws && this.ws.readyState === 1 && !this.wanted) {
      setTimeout(() => { try { this.ws.close(); } catch (e) {} }, 500);
    }
    this.ring = []; this.ringBytes = 0; this.batch = [];
  }
}

function concat(bufs) {
  if (bufs.length === 1) return bufs[0];
  let n = 0; bufs.forEach(b => n += b.byteLength);
  const out = new Uint8Array(n); let o = 0;
  bufs.forEach(b => { out.set(new Uint8Array(b), o); o += b.byteLength; });
  return out.buffer;
}

const SESSION_ID = (crypto.randomUUID ? crypto.randomUUID()
  : String(Date.now()) + Math.random().toString(16).slice(2));
const mic = new SourceChannel('mic');
const tab = new SourceChannel('tab');
let consentState = {mic: false, system_audio: false};

function offline(on) {
  $('offline').classList.toggle('show',
    !!on && (mic.wanted || tab.wanted));
}
function updateUnloadGuard() {
  window.onbeforeunload = (mic.wanted || tab.wanted)
    ? (e => { e.preventDefault(); e.returnValue = ''; }) : null;
}

async function refreshConsent() {
  try {
    const r = await fetch('/capture/status');
    const s = await r.json();
    const src = (s.consent && s.consent.sources) || {};
    consentState = {mic: !!src.mic, system_audio: !!src.system_audio};
  } catch (e) {}
  $('consent-mic').style.display = consentState.mic ? 'none' : '';
  $('consent-tab').style.display = consentState.system_audio ? 'none' : '';
  $('start-mic').disabled = !consentState.mic || mic.state !== 'off';
  $('start-tab').disabled = !consentState.system_audio || tab.state !== 'off';
  // Mic consent is enough to run a meeting; tab audio joins when consented.
  $('meeting').disabled = !consentState.mic;
}

async function startMic() {
  const stream = await navigator.mediaDevices.getUserMedia({audio: {
    channelCount: 1,
    echoCancellation: true,     // stop the mic re-capturing tab audio
    noiseSuppression: false,    // the server pipeline denoises
    autoGainControl: false,
  }});
  await mic.start(stream);
}
async function startTab() {
  // Video must be requested for the picker; we only keep the audio track.
  const stream = await navigator.mediaDevices.getDisplayMedia(
    {video: true, audio: true});
  stream.getVideoTracks().forEach(t => t.stop());
  if (!stream.getAudioTracks().length) {
    $('no-tab-audio').classList.add('show');
    throw new Error('no tab audio');
  }
  $('no-tab-audio').classList.remove('show');
  await tab.start(stream);
}

$('optin-mic').onclick = async () => {
  await post('/capture/consent', {mic: true}); refreshConsent(); };
$('optin-tab').onclick = async () => {
  await post('/capture/consent', {system_audio: true}); refreshConsent(); };
$('start-mic').onclick = () => startMic().catch(e => alert('Mic: ' + e.message));
$('start-tab').onclick = () => startTab().catch(e => {
  if (e.message !== 'no tab audio') alert('Tab audio: ' + e.message); });
$('pause-mic').onclick = () => mic.togglePause();
$('pause-tab').onclick = () => tab.togglePause();
$('stop-mic').onclick = () => mic.stop();
$('stop-tab').onclick = () => tab.stop();
// --- meeting session: titled start/end with its own consent ---------------
let MEET = {active: false, title: ''};
function paintMeeting() {
  $('dot-meet').className = 'dot' + (MEET.active ? ' rec' : '');
  $('st-meet').textContent = MEET.active
    ? ('recording — ' + (MEET.title || 'Meeting')) : 'none';
  $('meeting').hidden = MEET.active;
  $('end-meeting').hidden = !MEET.active;
  $('meet-setup').style.display = MEET.active ? 'none' : '';
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
    const d = await post('/meeting/session/start', {
      title: $('meet-title').value.trim(),
      consent: $('meet-ret').value});
    if (!d.ok) { alert('Meeting: ' + (d.error || 'could not start')); return; }
    MEET = {active: true, title: (d.session && d.session.title) || ''};
    paintMeeting();
  } catch (e) { alert('Meeting: ' + e.message); return; }
  try { if (mic.state === 'off') await startMic(); }
  catch (e) { alert('Mic: ' + e.message); }
  try { if (tab.state === 'off') await startTab(); }
  catch (e) { if (e.message !== 'no tab audio') alert('Tab audio: ' + e.message); }
};
$('end-meeting').onclick = async () => {
  mic.stop(); tab.stop();
  try { await post('/meeting/session/end', {}); } catch (e) {}
  MEET = {active: false, title: ''};
  paintMeeting();
  $('st-meet').textContent =
    'ended — the note appears in Meetings shortly';
};
$('stop-all').onclick = () => {
  mic.stop(); tab.stop();
  if (MEET.active) $('end-meeting').click();
};
setInterval(refreshMeeting, 15000);
refreshMeeting();

// --- voice enrollment: 10 s of raw PCM -> POST /speakers/enroll/web -------
const ENROLL_S = 10;
async function recordEnrollment() {
  const name = $('enroll-name').value.trim();
  if (!name) { $('enroll-msg').textContent = 'enter your name first'; return; }
  if (!consentState.mic) { $('enroll-msg').textContent =
    'allow microphone capture first (above)'; return; }
  const btn = $('enroll-go'); btn.disabled = true;
  $('dot-enroll').className = 'dot rec';
  const chunks = [];
  let stream, ctx;
  try {
    stream = await navigator.mediaDevices.getUserMedia({audio: {
      channelCount: 1, echoCancellation: true,
      noiseSuppression: false, autoGainControl: false}});
    try { ctx = new AudioContext({sampleRate: 16000}); }
    catch (e) { ctx = new AudioContext(); }
    await ctx.audioWorklet.addModule(workletURL);
    const src = ctx.createMediaStreamSource(stream);
    const node = new AudioWorkletNode(ctx, 'pcm-feeder',
      {numberOfInputs: 1, numberOfOutputs: 1, outputChannelCount: [1]});
    node.port.onmessage = e => chunks.push(e.data);
    src.connect(node);
    const mute = ctx.createGain(); mute.gain.value = 0;
    node.connect(mute); mute.connect(ctx.destination);
    for (let s = ENROLL_S; s > 0; s--) {
      $('st-enroll').textContent = 'recording… ' + s + 's';
      await new Promise(r => setTimeout(r, 1000));
    }
    $('st-enroll').textContent = 'saving…';
    const pcm = concat(chunks);
    const r = await fetch('/speakers/enroll/web?name=' +
        encodeURIComponent(name),
      {method: 'POST', headers: {'Content-Type': 'application/octet-stream',
        'x-csrf-token': csrf()}, body: pcm});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'enrollment failed');
    $('enroll-msg').textContent =
      'enrolled: ' + (d.enrolled || []).join(', ');
    $('st-enroll').textContent = 'done';
  } catch (e) {
    $('enroll-msg').textContent = e.message;
    $('st-enroll').textContent = 'failed';
  } finally {
    if (stream) stream.getTracks().forEach(t => t.stop());
    if (ctx) ctx.close().catch(() => {});
    $('dot-enroll').className = 'dot';
    btn.disabled = false;
  }
}
$('enroll-go').onclick = recordEnrollment;

// Live proof capture works: the newest web-audio transcripts.
async function tick() {
  if (mic.state === 'off' && tab.state === 'off') return;
  try {
    const r = await fetch('/console/events?source=audio.web&limit=6');
    const d = await r.json();
    const rows = (d.events || []).filter(e => (e.text || '').trim());
    const el = $('lines');
    if (!rows.length) return;
    el.innerHTML = rows.map(e =>
      `<div class="line"><span class="src">${
        e.source === 'audio.web_tab' ? 'tab' : 'mic'}${
        e.speaker ? ' · ' + esc(e.speaker) : ''}</span>${esc(e.text)}</div>`
    ).join('');
  } catch (e) {}
}
function esc(s) { return String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }
setInterval(tick, 4000);
refreshConsent();
</script>
</body></html>
""")
