"""Sign-in page — password login for returning users, token unlock as
bootstrap, and one-time password creation. Sets an HttpOnly session cookie."""

from app.api.mnemos_theme import apply_plain as _plain

AUTH_PAGE = _plain("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@BRAND@@ — sign in</title>
@@FONTS@@
<style>
@@ROOT@@
body{font:15px/1.45 var(--font);max-width:28rem;margin:3rem auto;padding:0 1rem;
color:var(--text);background:var(--paper)}
h1{font-size:1.35rem;font-weight:650;margin:0 0 .5rem;color:var(--navy)}
p{color:var(--mut);margin:0 0 1rem}
input,button{font:inherit;padding:.55rem .7rem;border-radius:6px;border:1px solid var(--line);
width:100%;box-sizing:border-box;background:var(--panel);color:var(--text);margin-top:.5rem}
button{margin-top:.6rem;background:var(--navy);color:var(--paper);border-color:var(--navy);cursor:pointer}
label.chk{display:flex;gap:.5rem;align-items:center;margin-top:.6rem;color:var(--mut);font-size:13px}
label.chk input{width:auto;margin:0}
#msg{margin-top:.8rem;min-height:1.2em}
.alt{margin-top:1.2rem;font-size:13px}
.alt a{color:var(--navy);cursor:pointer;text-decoration:underline}
section{display:none}
section.show{display:block}
</style></head><body>

<section id="login">
  <h1>Sign in</h1>
  <p>Welcome back. Enter your password to unlock this @@BRAND@@.</p>
  <input id="pw" type="password" autocomplete="current-password" placeholder="Password">
  <label class="chk"><input id="remember" type="checkbox" checked>Keep me signed in on this browser</label>
  <button id="login-go" type="button">Sign in</button>
  <div class="alt"><a id="show-token">Use the API token instead</a></div>
</section>

<section id="token">
  <h1>Unlock with API token</h1>
  <p>Paste the API token from <code>QUILL_API_TOKEN</code> or
  <code>data/.api_token</code>.</p>
  <input id="tok" type="password" autocomplete="off" placeholder="API token">
  <button id="tok-go" type="button">Unlock</button>
  <div class="alt" id="back-wrap" hidden><a id="show-login">Back to password sign-in</a></div>
</section>

<section id="create">
  <h1>Create your password</h1>
  <p>You're unlocked. Set a password so next time you can sign in without the
  token.</p>
  <input id="new-email" type="email" autocomplete="email" placeholder="Email (optional)">
  <input id="new-pw" type="password" autocomplete="new-password" placeholder="Password (8+ characters)">
  <input id="new-pw2" type="password" autocomplete="new-password" placeholder="Repeat password">
  <button id="create-go" type="button">Create password</button>
  <div class="alt"><a id="skip-create">Skip for now</a></div>
</section>

<div id="msg" role="status" aria-live="polite"></div>

<script>
const msg=document.getElementById('msg');
const S={login:document.getElementById('login'),
  token:document.getElementById('token'),
  create:document.getElementById('create')};
function show(name){for(const k in S)S[k].classList.toggle('show',k===name);msg.textContent='';}

/* Same-origin relative redirect only — "//" or a scheme = open redirect. */
function nextUrl(){
  try{
    const raw=new URLSearchParams(location.search).get('next')||'';
    if(raw.startsWith('/')&&!raw.startsWith('//')&&!raw.includes('\\\\'))return raw;
  }catch(e){}
  return '/';
}
async function post(url,body){
  const r=await fetch(url,{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body||{})});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)throw new Error(j.detail||('Failed ('+r.status+')'));
  return j;
}

let HAS_ACCOUNT=false;
fetch('/auth/account').then(r=>r.json()).then(a=>{
  HAS_ACCOUNT=!!a.configured;
  document.getElementById('back-wrap').hidden=!HAS_ACCOUNT;
  show(HAS_ACCOUNT?'login':'token');
}).catch(()=>show('token'));

document.getElementById('show-token').onclick=()=>show('token');
document.getElementById('show-login').onclick=()=>show('login');

document.getElementById('login-go').onclick=async()=>{
  msg.textContent='…';
  try{
    await post('/auth/login',{password:document.getElementById('pw').value,
      remember:document.getElementById('remember').checked});
    location.href=nextUrl();
  }catch(e){msg.textContent=e.message;}
};
document.getElementById('pw').addEventListener('keydown',e=>{
  if(e.key==='Enter')document.getElementById('login-go').click();});

document.getElementById('tok-go').onclick=async()=>{
  msg.textContent='…';
  try{
    await post('/auth/unlock',{token:document.getElementById('tok').value});
    if(HAS_ACCOUNT)location.href=nextUrl();
    else show('create');
  }catch(e){msg.textContent=e.message;}
};

document.getElementById('create-go').onclick=async()=>{
  const p=document.getElementById('new-pw').value;
  if(p!==document.getElementById('new-pw2').value){
    msg.textContent='passwords do not match';return;}
  msg.textContent='…';
  try{
    await post('/auth/register',{password:p,
      email:document.getElementById('new-email').value});
    location.href=nextUrl();
  }catch(e){msg.textContent=e.message;}
};
document.getElementById('skip-create').onclick=()=>{location.href=nextUrl();};
</script></body></html>""")
