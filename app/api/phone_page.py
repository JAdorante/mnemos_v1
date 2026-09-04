"""Phone-channel pages: desktop pairing ("Connect a phone") + the mobile setup
page the QR code opens. Device-specific behavior stays on the phone (Shortcuts
recipes are instructions, not code); these pages only pair and explain."""

from app.api.mnemos_theme import apply as _mnemos

# --- desktop: /phone --------------------------------------------------------
PHONE_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Connect a phone</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 var(--font);color:var(--text);background:var(--paper)}
.top{position:sticky;top:0;display:flex;gap:14px;align-items:center;padding:10px 20px;z-index:var(--z-raised);background:var(--chrome-bg);backdrop-filter:blur(10px)}
.wrap{max-width:760px;margin:0 auto;padding:26px 20px 80px}
h1{font-family:var(--display);font-weight:400;font-size:2rem;letter-spacing:-.02em;color:var(--navy);margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 22px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px;box-shadow:var(--shadow);margin-bottom:18px;animation:fadeUp .35s var(--ease) both}
.panel h2{font-family:var(--display);font-weight:400;font-size:1.35rem;margin:0 0 10px;color:var(--navy)}
.btn{appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;border-radius:12px;
  padding:11px 20px;background:var(--navy);color:#F8F6F1}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.muted{color:var(--mut);font-size:.92rem}
.warn{border:1px solid rgba(199,138,44,.35);background:rgba(199,138,44,.08);
  border-radius:12px;padding:12px 14px;font-size:.92rem;margin:12px 0}
.code{font-family:var(--mono);font-size:2.2rem;letter-spacing:.35em;color:var(--navy);
  padding:8px 0 2px}
.qr{display:flex;gap:22px;align-items:center;flex-wrap:wrap;margin-top:10px}
.qr svg{width:200px;height:200px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:10px}
.url{font-family:var(--mono);font-size:.85rem;word-break:break-all;color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:.92rem}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);text-align:left;padding:6px 8px}
td{border-top:1px solid var(--line);padding:9px 8px}
.linkish{background:none;border:0;color:var(--danger);cursor:pointer;font:inherit;font-size:.85rem;padding:0}
.ok{color:var(--ok);font-weight:600}
@media(max-width:640px){
  .top{padding:8px 14px;gap:10px;flex-wrap:wrap}
  .wrap{padding:18px 14px 64px}
  h1{font-size:clamp(1.5rem,6vw,2rem)}
  .panel{padding:16px}
  .code{font-size:1.6rem;letter-spacing:.2em}
  .qr{flex-direction:column;align-items:flex-start}
  .qr svg{width:min(200px,100%);height:auto;aspect-ratio:1}
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .panel div[style*="display:flex"]{flex-direction:column;align-items:stretch}
  .panel div[style*="display:flex"] input{min-width:0!important;width:100%}
  .panel div[style*="display:flex"] select{width:100%}
}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Connect a phone</span>
  @@NAV@@
  <span class="spacer"></span></div>
<div class="wrap">
  <div id="phoneErr" class="fetch-err" hidden role="alert" style="margin-bottom:18px;padding:10px 14px;border-radius:10px;background:rgba(154,63,63,.08);border:1px solid rgba(154,63,63,.25);color:var(--danger);font-size:13px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <span>Couldn't reach Sparrow — retrying…</span>
    <button type="button" id="phoneRetry">Retry now</button>
  </div>
  <h1>Connect a phone</h1>
  <p class="lead">Pair your iPhone or Android so it can send @@BRAND@@ notes, dictations,
  shares, and locations directly — no Phone Link needed. Everything a phone sends becomes
  memory it can ground on; nothing a phone sends can act on its own.</p>

  <div class="panel" id="pairPanel">
    <h2>Pair a new device</h2>
    <p class="muted">A pairing code stays valid for 10 minutes and works once.
    Scan the QR with the phone's camera, or open the setup link on the phone and
    type the code.</p>
    <div id="reachWarn"></div>
    <button class="btn" id="startBtn" type="button">Show pairing code</button>
    <div id="pairBox" hidden>
      <div class="qr"><div id="qrHolder"></div>
        <div>
          <div class="muted">Pairing code</div>
          <div class="code" id="codeText"></div>
          <div class="muted" style="margin-top:8px">Setup link (same Wi-Fi):</div>
          <div class="url" id="setupUrl"></div>
          <div class="muted" id="expiry" style="margin-top:8px"></div>
        </div>
      </div>
      <p class="ok" id="pairedMsg" hidden>✓ Device connected.</p>
    </div>
  </div>

  <div class="panel">
    <h2>Paired devices</h2>
    <div id="devBox" class="muted">Loading…</div>
  </div>

  <div class="panel">
    <h2>Send to your phone</h2>
    <p class="muted">Queue something for the phone to pick up. Nothing is pushed —
    the phone pulls when you run the <b>Check Sparrow</b> shortcut (by Siri, a tap,
    or an iOS automation like "when I arrive home").</p>
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0">
      <select id="outKind" style="font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev)">
        <option value="notify">Notification</option>
        <option value="reminder">Reminder</option>
        <option value="url">Link to open</option>
        <option value="query">Question (phone answers back)</option>
      </select>
      <input id="outText" placeholder="What should the phone show / remind / open?"
        style="flex:1;min-width:220px;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev)">
      <button class="btn" id="outBtn" type="button">Queue</button>
    </div>
    <div id="outMsg" class="muted"></div>
    <div id="outPending" class="muted" style="margin-top:8px"></div>
    <details id="recipeDetails" style="margin-top:14px">
      <summary style="cursor:pointer;font-weight:600">The one "@@BRAND@@" shortcut (does send + receive)</summary>
      <p class="muted" style="margin-top:8px">One shortcut covers both directions
      via a single call to <span class="url" id="syncUrlD" style="display:inline"></span>.
      The exact steps — with your device key already filled in — are shown on the
      <b>phone's</b> setup page when you pair (scan the QR above). In short:</p>
      <ol style="padding-left:20px">
        <li><b>Get Contents of URL</b> → <b>POST</b> to <code>/phone/sync</code>,
          Authorization header = your device key, JSON body
          <code>{"kind":"note","text":…}</code> (text from <i>Ask Each Time</i> /
          <i>Shortcut Input</i>, or empty to only receive).</li>
        <li><b>Get Dictionary Value</b> key <b>items</b> → <b>Repeat with Each</b>
          → <b>Show Notification</b> of each item's <b>text</b>.</li>
        <li>Optional branches on each item's <b>kind</b>: <code>reminder</code> →
          Add Reminder, <code>url</code> → Open URLs, <code>query</code> → read
          the asked-for value (battery, location…) and POST it back.</li>
        <li>Name it <b>mnemos</b>; enable Share Sheet + Siri, and add Automations
          (arrive home / a time / on charge) that run it to receive hands-free.</li>
      </ol>
    </details>
  </div>

  <div class="panel">
    <h2>Connect iCloud <span class="muted" style="font-size:.85rem">(calendar)</span></h2>
    <p class="muted">Lets @@BRAND@@ read your iCloud calendar so it knows your real
    schedule. Uses an <b>app-specific password</b> — a limited key you create at
    Apple, <b>not</b> your Apple password — and you can revoke it at Apple anytime.</p>
    <div id="icStatus" class="muted" style="margin:8px 0"></div>
    <div id="icForm">
      <ol style="padding-left:20px" class="muted">
        <li>Open <a href="https://appleid.apple.com" target="_blank" rel="noopener">appleid.apple.com</a>
          → Sign-In and Security → <b>App-Specific Passwords</b> → <b>+</b>, name it
          <code>mnemos</code>, and copy the password Apple shows you.</li>
        <li>Enter both below — the pair is tested against Apple before anything is saved.</li>
      </ol>
      <div style="display:flex;gap:10px;flex-wrap:wrap;margin:10px 0">
        <input id="icUser" placeholder="Apple ID email" autocomplete="username"
          style="flex:1;min-width:200px;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev)">
        <input id="icPass" placeholder="xxxx-xxxx-xxxx-xxxx" type="password" autocomplete="off"
          style="flex:1;min-width:200px;font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev)">
        <button class="btn" id="icBtn" type="button">Connect</button>
      </div>
      <div id="icMsg" class="muted"></div>
    </div>
    <div id="icConnected" hidden>
      <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:6px 0">
        <button class="btn" id="icSyncBtn" type="button">Sync now</button>
        <button class="linkish" id="icDisc" type="button">Disconnect iCloud</button>
      </div>
      <div id="icSyncMsg" class="muted" style="margin-top:6px"></div>
    </div>
  </div>
</div>
<script>
let baseline = null;
let _phoneSig = null;
async function refresh(){
  if(document.hidden) return;
  const errEl=document.getElementById('phoneErr');
  try{
    const r=await fetch("/phone/status");
    if(!r.ok) throw new Error('HTTP '+r.status);
    const st=await r.json();
    const sig=JSON.stringify(st);
    if(sig===_phoneSig){ if(errEl) errEl.hidden=true; return st; }
    _phoneSig=sig;
    if(errEl) errEl.hidden=true;
    if(st.localhost_only){
      document.getElementById("reachWarn").innerHTML =
        '<div class="warn">'+st.hint+'</div>';
    }
    const box=document.getElementById("devBox");
    if(!st.devices.length){ box.textContent="No phones paired yet."; }
    else{
      const rows = st.devices.map(d=>{
        const seen = d.last_seen ? new Date(d.last_seen*1000).toLocaleString() : "never";
        return `<tr><td>${d.name}</td><td>${d.platform||"?"}</td><td>${d.events}</td>
        <td>${seen}${d.last_kind?" ("+d.last_kind+")":""}</td>
        <td><button class="linkish" onclick="revoke('${d.device_id}')">Revoke</button></td></tr>`;
      }).join("");
      box.innerHTML=`<table><tr><th>Name</th><th>Platform</th><th>Events</th>
        <th>Last seen</th><th></th></tr>${rows}</table>`;
    }
    if(baseline!==null && st.devices.length>baseline){
      document.getElementById("pairedMsg").hidden=false;
      baseline = st.devices.length;
    }
    const pend = st.outbox_pending || [];
    document.getElementById("outPending").textContent = pend.length
      ? ("Waiting for the phone: " + pend.map(i=>i.kind+" — "+i.text.slice(0,60)).join(" · "))
      : "Outbox empty — everything delivered.";
    return st;
  }catch(e){
    if(errEl) errEl.hidden=false;
    return null;
  }
}
document.getElementById('phoneRetry')?.addEventListener('click',()=>refresh());
async function startPair(){
  const r = await (await fetch("/phone/pair/start",{method:"POST"})).json();
  if(!r.ok){ alert(r.error||"Could not start pairing"); return; }
  const st = await refresh();
  baseline = st ? st.devices.length : 0;
  document.getElementById("pairBox").hidden=false;
  document.getElementById("pairedMsg").hidden=true;
  document.getElementById("codeText").textContent=r.code;
  document.getElementById("setupUrl").textContent=r.setup_url;
  document.getElementById("expiry").textContent=
    "Expires "+new Date(r.expires_at*1000).toLocaleTimeString();
  document.getElementById("qrHolder").innerHTML =
    r.qr_svg || '<div class="muted">QR unavailable — open the setup link on the phone.</div>';
  document.getElementById("startBtn").textContent="New pairing code";
}
async function revoke(id){
  if(!confirm("Revoke this device? Its token stops working immediately.")) return;
  await fetch("/phone/revoke",{method:"POST",headers:{"Content-Type":"application/json"},
    body:JSON.stringify({device_id:id})});
  refresh();
}
async function queueOut(){
  const text=document.getElementById("outText").value.trim();
  const kind=document.getElementById("outKind").value;
  const msg=document.getElementById("outMsg");
  if(!text){ msg.textContent="Type something to send first."; return; }
  try{
    const r=await (await fetch("/phone/outbox/queue",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({kind, text})})).json();
    msg.textContent = r.ok ? "Queued — run \"Check Sparrow\" on the phone to receive it."
                           : (r.detail||"Could not queue");
    if(r.ok) document.getElementById("outText").value="";
  }catch(e){ msg.textContent="Network error."; }
  refresh();
}
document.getElementById("outBtn").onclick=queueOut;
document.getElementById("outText").addEventListener("keydown",e=>{
  if(e.key==="Enter") queueOut();
});
document.getElementById("syncUrlD").textContent = location.origin + "/phone/sync";

// --- iCloud connect (guided; validated server-side before saving) ----------
function icAgo(ts){
  if(!ts) return "never synced";
  const secs=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(secs<60) return "synced just now";
  if(secs<3600) return "synced "+Math.floor(secs/60)+" min ago";
  return "synced "+Math.floor(secs/3600)+" h ago";
}
async function icRefresh(){
  try{
    const s=await (await fetch("/icloud/status")).json();
    const on=s.connected;
    document.getElementById("icStatus").innerHTML = on
      ? '<span style="color:var(--ok);font-weight:600">✓ Connected as '+s.user+'</span>'
      : "Not connected.";
    document.getElementById("icForm").hidden = on;
    document.getElementById("icConnected").hidden = !on;
    if(on){
      try{
        const ss=await (await fetch("/icloud/sync/status")).json();
        const lr=ss.last_result||{};
        let note = icAgo(ss.last_sync);
        if(lr.ok && (lr.new!==undefined)) note += ` · ${lr.new} new of ${lr.events_seen} events (${lr.calendars} calendars)`;
        else if(lr.error) note += " · last run: "+lr.error;
        document.getElementById("icSyncMsg").textContent = note;
      }catch(e){}
    }
  }catch(e){}
}
document.getElementById("icBtn").onclick = async () => {
  const msg=document.getElementById("icMsg"), btn=document.getElementById("icBtn");
  msg.textContent="Checking with Apple…"; btn.disabled=true;
  try{
    const r=await fetch("/icloud/connect",{method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({user:document.getElementById("icUser").value,
                           app_password:document.getElementById("icPass").value})});
    const j=await r.json();
    if(r.ok && j.ok){ msg.textContent=""; document.getElementById("icPass").value=""; icRefresh(); }
    else msg.textContent=j.detail||"Could not connect.";
  }catch(e){ msg.textContent="Network error."; }
  btn.disabled=false;
};
document.getElementById("icSyncBtn").onclick = async () => {
  const btn=document.getElementById("icSyncBtn"), msg=document.getElementById("icSyncMsg");
  btn.disabled=true; msg.textContent="Syncing your calendar…";
  try{
    const j=await (await fetch("/icloud/sync",{method:"POST"})).json();
    msg.textContent = j.ok
      ? `Synced — ${j.new} new of ${j.events_seen} events across ${j.calendars} calendars.`
      : ("Sync failed: "+(j.error||j.detail||"unknown"));
  }catch(e){ msg.textContent="Network error."; }
  btn.disabled=false;
  icRefresh();
};
document.getElementById("icDisc").onclick = async () => {
  if(!confirm("Disconnect iCloud? (You can also revoke the password at appleid.apple.com.)")) return;
  await fetch("/icloud/disconnect",{method:"POST"});
  icRefresh();
};
icRefresh();

document.getElementById("startBtn").onclick=startPair;
refresh(); setInterval(() => { if (!document.hidden) refresh(); }, 3000);
</script>
</body>
</html>""")

# --- mobile: /phone/setup?code= ---------------------------------------------
PHONE_SETUP_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Phone setup</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
*{box-sizing:border-box}
body{margin:0;font:16px/1.6 var(--font);color:var(--text);background:var(--bg)}
.wrap{max-width:560px;margin:0 auto;padding:26px 18px 80px}
h1{font-family:var(--display);font-weight:400;font-size:1.7rem;color:var(--navy);margin:0 0 4px}
.lead{color:var(--mut);margin:0 0 18px;font-size:.95rem}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:18px;box-shadow:var(--shadow);margin-bottom:16px}
label{display:block;font-size:.78rem;font-weight:600;letter-spacing:.04em;
  text-transform:uppercase;color:var(--mut);margin:12px 0 6px}
input,select{width:100%;font:inherit;color:var(--text);background:var(--bg-elev);
  border:1px solid var(--line);border-radius:12px;padding:12px 13px;outline:none}
.btn{appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;border-radius:12px;
  padding:13px 20px;background:var(--navy);color:#F8F6F1;width:100%;margin-top:16px}
.err{color:var(--danger);font-size:.9rem;min-height:1.2em;margin-top:8px}
.tok{font-family:var(--mono);font-size:.82rem;word-break:break-all;background:var(--panel-2);
  border:1px solid var(--line);border-radius:10px;padding:10px;margin:8px 0}
.copy{font-size:.82rem;padding:8px 14px;border-radius:10px;border:1px solid var(--line);
  background:transparent;cursor:pointer;font-weight:600;color:var(--text)}
ol{padding-left:20px;margin:8px 0}
li{margin:8px 0}
h2{font-family:var(--display);font-weight:400;font-size:1.25rem;color:var(--navy);margin:0 0 8px}
.warn{border:1px solid rgba(199,138,44,.35);background:rgba(199,138,44,.08);
  border-radius:12px;padding:10px 12px;font-size:.88rem;margin-top:10px}
.muted{color:var(--mut);font-size:.88rem}
@media(max-width:640px){
  .wrap{padding:18px 14px 64px}
  h1{font-size:clamp(1.35rem,5.5vw,1.7rem)}
  .panel{padding:14px}
}
</style>
</head>
<body>
<div class="wrap">
  <h1>Connect this phone to @@BRAND@@</h1>
  <p class="lead">Pairs this device so its shortcuts can send notes, dictations,
  shares, and locations to @@BRAND@@ on your computer.</p>

  <div class="panel" id="claimPanel">
    <h2>1 · Pair</h2>
    <label for="code">Pairing code (on your computer screen)</label>
    <input id="code" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code">
    <label for="name">Name this device</label>
    <input id="name" placeholder="e.g. My iPhone">
    <label for="platform">Device type</label>
    <select id="platform">
      <option value="ios">iPhone / iPad</option>
      <option value="android">Android</option>
      <option value="other">Other</option>
    </select>
    <button class="btn" id="claimBtn" type="button">Connect</button>
    <div class="err" id="err"></div>
  </div>

  <div class="panel" id="doneP" hidden>
    <h2>2 · Your device key</h2>
    <p class="muted">Shown once — it goes in the shortcut you'll create next.
    Anyone with this key can add notes to @@BRAND@@'s memory, so keep it on this
    phone only. You can revoke it anytime from the computer.</p>
    <div class="tok" id="token"></div>
    <button class="copy" onclick="copyText('token')">Copy key</button>
    <div class="warn">Do this step now — the key is not shown again.</div>
  </div>

  <div class="panel" id="howP" hidden>
    <h2>3 · Make ONE shortcut — "@@BRAND@@"</h2>
    <p class="muted">This single shortcut does both directions: run it with text
    (Siri or the share sheet) and it <b>sends</b>; run it empty (a tap or an
    automation) and it <b>receives</b> whatever @@BRAND@@ queued for you. You only
    build this once. The two values you'll paste are ready below.</p>
    <label>Endpoint URL</label>
    <span class="tok" id="syncUrl" style="display:block"></span>
    <button class="copy" onclick="copyText('syncUrl')">Copy URL</button>
    <label style="margin-top:12px">Authorization header value</label>
    <span class="tok" id="authHdr" style="display:block"></span>
    <button class="copy" onclick="copyText('authHdr')">Copy header value</button>

    <div id="iosHow" hidden style="margin-top:14px">
      <ol>
        <li>Open <b>Shortcuts</b> → <b>+</b>. (Optional, for sending) add a
          <b>Text</b> action set to <i>Ask Each Time</i> — or leave it out to
          make a receive-only shortcut.</li>
        <li>Add <b>Get Contents of URL</b>. Paste the <b>URL</b> above.</li>
        <li>Expand it (▸): <b>Method POST</b> → <b>Headers</b>: add
          <b>Authorization</b> = the header value above.</li>
        <li><b>Request Body: JSON</b> → add <b>kind</b> = <code>note</code> and
          <b>text</b> = your <i>Ask Each Time</i> / <i>Shortcut Input</i> (or leave
          text empty for receive-only).</li>
        <li>Add <b>Get Dictionary Value</b> → key <b>items</b>. Then
          <b>Repeat with Each</b> → inside it <b>Get Dictionary Value</b> key
          <b>text</b> → <b>Show Notification</b>.</li>
        <li>Name it <b>@@BRAND@@</b>. In settings turn on <b>Show in Share Sheet</b>,
          try "Hey Siri, @@BRAND@@", and add Automations (arrive home / a set time /
          on charge) that run it to receive hands-free.</li>
      </ol>
      <p class="muted">Want it to answer questions or file reminders? Inside the
      Repeat, branch on each item's <b>kind</b> (<code>reminder</code> →
      Add Reminder, <code>url</code> → Open URLs, <code>query</code> → read the
      thing it asks for and POST it back the same way). Each branch you add is one
      more thing it can do — all in this one shortcut.</p>
      <p class="muted" style="margin-top:10px"><b>Send photos too (optional):</b>
      make a separate shortcut named <b>Photo to @@BRAND@@</b>. In its settings turn on
      <b>Show in Share Sheet</b> and accept <b>Images</b> — then you can share
      straight from the Photos app. Steps:</p>
      <ol style="margin-top:6px">
        <li><b>Receive Images from Share Sheet</b> (or add <b>Select Photos</b>,
          allow multiple, to pick from inside the shortcut).</li>
        <li><b>Repeat with Each</b> image. Inside the repeat:</li>
        <li><b>Convert Image</b> → <b>JPEG</b> (iPhone shoots HEIC, which can't be
          read — this is required).</li>
        <li>(Optional) <b>Get Details of Images</b> → <b>Date Taken</b>, then
          <b>Format Date</b> as a Unix timestamp — pass it as
          <code>?taken_at=</code> so old shared photos land at their real date.</li>
        <li><b>Get Contents of URL</b> → <b>POST</b> to
          <span class="tok" id="photoUrl" style="display:block"></span>
          (append <code>?taken_at=[formatted date]</code> if you did step 4),
          <b>Request Body: File</b> = the JPEG, and the same <b>Authorization</b>
          header as above.</li>
      </ol>
      <p class="muted">@@BRAND@@ reads/OCRs each photo into memory — great for
      whiteboards, notes, receipts, signs. iOS asks for Photos permission the
      first time; you stay in control of what you share.</p>
    </div>
    <div id="androidHow" hidden style="margin-top:14px">
      <ol>
        <li>Install <b>HTTP Shortcuts</b> (free, open source) or use Tasker.</li>
        <li>New shortcut → Method <b>POST</b> → the URL above; add the
          <b>Authorization</b> header above.</li>
        <li>Body (JSON): <code>{"kind":"note","text":"..."}</code> (empty text =
          receive-only). Parse <b>items[]</b> from the response and show each.</li>
      </ol>
    </div>
    <p class="muted" style="margin-top:10px">Test it: run it with a bit of text —
    it should appear on the computer's Phone page within seconds, and anything
    queued there comes back as a notification.</p>
  </div>
</div>
<script>
function copyText(id){
  navigator.clipboard.writeText(document.getElementById(id).textContent)
    .then(()=>{},()=>{});
}
const qs = new URLSearchParams(location.search);
if(qs.get("code")) document.getElementById("code").value = qs.get("code");
document.getElementById("claimBtn").onclick = async () => {
  const err=document.getElementById("err"); err.textContent="";
  const body={code:document.getElementById("code").value.trim(),
              name:document.getElementById("name").value.trim(),
              platform:document.getElementById("platform").value};
  let j;
  try{
    j = await (await fetch("/phone/pair/claim",{method:"POST",
      headers:{"Content-Type":"application/json"},body:JSON.stringify(body)})).json();
  }catch(e){ err.textContent="Could not reach the server — same Wi-Fi?"; return; }
  if(!j.ok){ err.textContent=j.error||"Pairing failed"; return; }
  document.getElementById("claimPanel").style.opacity=.5;
  document.getElementById("claimBtn").disabled=true;
  document.getElementById("doneP").hidden=false;
  document.getElementById("howP").hidden=false;
  document.getElementById("token").textContent=j.token;
  const base=location.origin;
  for(const [id,val] of [["syncUrl",base+"/phone/sync"],
      ["authHdr","Bearer "+j.token],["photoUrl",base+"/phone/photo"]]){
    document.getElementById(id).textContent=val;
  }
  const plat=body.platform;
  document.getElementById("iosHow").hidden = plat!=="ios" && plat!=="other";
  document.getElementById("androidHow").hidden = plat!=="android";
};
</script>
</body>
</html>""")
