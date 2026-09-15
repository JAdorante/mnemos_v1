"""Front-door pages: sign in (returning) and sign up (new), plus the short
"building your workspace" wait a new seat needs while its container boots.

Self-contained by design — the gateway is a separate image from the app, so it
does not import app.api.mnemos_theme; the few tokens below mirror it.
"""

_SHELL = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sparrow — @@TITLE@@</title>
<link rel="icon" href="/gw-static/ravenry-mark.png" type="image/png">
<style>
:root{--paper:#fbfaf7;--panel:#fff;--text:#1b1c1e;--mut:#6b6f76;
--navy:#1f3a5f;--line:#e3e1db;--bad:#9b2c2c}
@media (prefers-color-scheme:dark){:root{--paper:#16171a;--panel:#1e2024;
--text:#e9e8e4;--mut:#9aa0a8;--navy:#8fb0d9;--line:#2e3238}}
*{box-sizing:border-box}
body{font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif;
max-width:26rem;margin:3.5rem auto;padding:0 1.1rem;
color:var(--text);background:var(--paper)}
h1{font-size:1.35rem;font-weight:650;margin:0 0 .4rem;color:var(--navy)}
p.sub{color:var(--mut);margin:0 0 1.3rem}
label{display:block;font-size:13px;color:var(--mut);margin-top:.85rem}
input{font:inherit;padding:.55rem .7rem;border-radius:6px;
border:1px solid var(--line);width:100%;background:var(--panel);
color:var(--text);margin-top:.3rem}
button{font:inherit;padding:.6rem .7rem;border-radius:6px;width:100%;
margin-top:1.1rem;background:var(--navy);color:var(--paper);
border:1px solid var(--navy);cursor:pointer}
button[disabled]{opacity:.55;cursor:progress}
label.chk{display:flex;gap:.5rem;align-items:center;margin-top:.8rem}
label.chk input{width:auto;margin:0}
#msg{margin-top:.9rem;min-height:1.2em;font-size:13.5px;color:var(--bad)}
.alt{margin-top:1.4rem;font-size:13px;color:var(--mut)}
.alt a{color:var(--navy)}
.hint{font-size:12.5px;color:var(--mut);margin-top:.3rem}
</style></head><body>
@@BODY@@
<div id="msg" role="status" aria-live="polite"></div>
<script>
/* Same-origin relative redirect only — "//" or a scheme is an open redirect. */
function nextUrl(){
  try{
    const raw=new URLSearchParams(location.search).get('next')||'';
    if(raw.startsWith('/')&&!raw.startsWith('//')&&!raw.includes('\\\\'))return raw;
  }catch(e){}
  return '/';
}
const msg=document.getElementById('msg');
async function post(url,body){
  const r=await fetch(url,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.detail||('Failed ('+r.status+')'));
  return j;
}
@@SCRIPT@@
</script></body></html>"""


def _page(title: str, body: str, script: str) -> str:
    return (_SHELL.replace("@@TITLE@@", title)
            .replace("@@BODY@@", body).replace("@@SCRIPT@@", script))


SIGNIN_PAGE = _page("sign in", """
<h1>Sign in</h1>
<p class="sub">Welcome back. Your Sparrow is waiting where you left it.</p>
<label for="email">Email</label>
<input id="email" type="email" autocomplete="email" autofocus>
<label for="pw">Password</label>
<input id="pw" type="password" autocomplete="current-password">
<label class="chk"><input id="remember" type="checkbox" checked>
Keep me signed in on this browser</label>
<button id="go" type="button">Sign in</button>
<div class="alt">New here? <a href="/signup">Create an account</a></div>
""", """
const go=document.getElementById('go');
go.onclick=async()=>{
  msg.textContent='';go.disabled=true;
  try{
    await post('/api/signin',{email:document.getElementById('email').value,
      password:document.getElementById('pw').value,
      remember:document.getElementById('remember').checked});
    location.href=nextUrl();
  }catch(e){msg.textContent=e.message;go.disabled=false;}
};
for(const id of ['email','pw'])
  document.getElementById(id).addEventListener('keydown',
    e=>{if(e.key==='Enter')go.click();});
""")


SIGNUP_PAGE = _page("create your account", """
<h1>Create your account</h1>
<p class="sub">Signing up builds you a private Sparrow — your own instance,
your own storage. Nothing you capture is shared with anyone else here.</p>
<label for="email">Email</label>
<input id="email" type="email" autocomplete="email" autofocus>
<label for="pw">Password</label>
<input id="pw" type="password" autocomplete="new-password">
<div class="hint">At least 10 characters.</div>
<label for="pw2">Repeat password</label>
<input id="pw2" type="password" autocomplete="new-password">
<div id="invite-wrap" hidden>
  <label for="invite">Invite code</label>
  <input id="invite" type="text" autocomplete="off">
</div>
<button id="go" type="button">Create my Sparrow</button>
<div class="alt">Already have an account? <a href="/signin">Sign in</a></div>
""", """
fetch('/api/config').then(r=>r.json()).then(c=>{
  document.getElementById('invite-wrap').hidden=!c.invite_required;
}).catch(()=>{});

const go=document.getElementById('go');
go.onclick=async()=>{
  const pw=document.getElementById('pw').value;
  if(pw!==document.getElementById('pw2').value){
    msg.textContent='passwords do not match';return;}
  msg.textContent='';go.disabled=true;
  try{
    await post('/api/signup',{email:document.getElementById('email').value,
      password:pw,invite:document.getElementById('invite').value});
    location.href='/provisioning';
  }catch(e){msg.textContent=e.message;go.disabled=false;}
};
""")


# Container boot is not instant (ASR warm-up under QUILL_ASR_WARMUP=1 loads the
# model before the first utterance), so a new account waits here rather than
# landing on a connection error.
PROVISIONING_PAGE = _page("building your workspace", """
<h1>Building your Sparrow</h1>
<p class="sub">Starting your private instance and warming up transcription.
This takes a minute on the first run. You can leave this tab open.</p>
""", """
msg.style.color='var(--mut)';
msg.textContent='Starting…';
let tries=0;
async function poll(){
  tries++;
  try{
    const r=await fetch('/api/seat/status');
    const j=await r.json();
    if(j.ready){msg.textContent='Ready — taking you in.';
      location.href='/';return;}
    if(!r.ok&&r.status===401){location.href='/signin';return;}
    msg.textContent=j.detail||'Warming up…';
  }catch(e){msg.textContent='Warming up…';}
  if(tries>150){
    msg.style.color='var(--bad)';
    msg.textContent='Still starting — this is slower than expected. '+
      'Reload to keep waiting.';return;}
  setTimeout(poll,2000);
}
poll();
""")
