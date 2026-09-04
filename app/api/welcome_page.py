"""Launch / welcome page — first screen at `/`.

Local-first: there are no cloud accounts. "New" runs onboarding on this machine;
"Sign in" means continue as the profile already on this install (and unlock the
LAN API token when the browser is not on loopback).
"""

from app.api.mnemos_theme import apply as _mnemos

WELCOME_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
html,body{margin:0;min-height:100%}
body{
  font:16px/1.55 var(--font);color:var(--text);
  min-height:100vh;display:flex;flex-direction:column;
  background:
    radial-gradient(920px 520px at 10% -10%, var(--acc-08), transparent 55%),
    radial-gradient(700px 420px at 100% 0%, rgba(30,91,79,.06), transparent 52%),
    linear-gradient(180deg, #FBF9F4 0%, var(--paper) 38%, var(--workspace) 100%);
  background-attachment:fixed;
}
a{color:var(--navy);text-decoration:none}
a:hover{opacity:.8}

.stage{
  flex:1;display:flex;flex-direction:column;justify-content:center;
  max-width:640px;width:100%;margin:0 auto;padding:48px 24px 64px;
  animation:morningPaper .45s var(--ease) both;
}
.brand-row{
  display:flex;align-items:center;gap:14px;margin:0 0 20px;
}
.brand-logo{
  display:block;width:min(340px,86vw);height:auto;
  animation:fadeUp .5s var(--ease) both;
}
.brand-row .mark{color:var(--navy);width:40px;height:40px}
.brand-row .mark path{
  stroke-dasharray:64;animation:inkDraw .55s var(--ease) both;
}
.brand{
  font-family:var(--display);font-weight:400;
  font-size:clamp(3.6rem,12vw,6rem);letter-spacing:-.035em;line-height:.9;
  margin:0;color:var(--navy);
}
.rule{
  width:76px;height:1.5px;background:var(--acc);margin:0 0 26px;
  transform-origin:left center;animation:inkLine .4s var(--ease) .12s both;
}
h1{
  font-family:var(--display);font-weight:400;
  font-size:clamp(1.55rem,3.8vw,2.15rem);letter-spacing:-.02em;
  margin:0 0 12px;max-width:18ch;color:var(--navy);line-height:1.15;
}
.lead{color:var(--mut);font-size:1.05rem;max-width:34ch;margin:0 0 30px}
.cta{display:flex;flex-wrap:wrap;gap:12px;align-items:center}
.btn{
  appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;
  border-radius:12px;padding:13px 22px;
  transition:transform .22s var(--ease),background .28s var(--ease),
    box-shadow .28s var(--ease),border-color .28s var(--ease),filter .28s var(--ease);
}
.btn:hover:not(:disabled){transform:translateY(-2px)}
.btn:active:not(:disabled){transform:translateY(0) scale(.97)}
.btn-primary{
  background:var(--navy);color:#F8F6F1;
  box-shadow:0 2px 10px rgba(11,19,32,.16);
}
.btn-primary:hover:not(:disabled){
  background:#152033;box-shadow:0 8px 22px rgba(11,19,32,.22);filter:brightness(1.05);
}
.btn-ghost{
  background:transparent;color:var(--text);border:1px solid var(--line);
}
.btn-ghost:hover:not(:disabled){
  border-color:var(--acc-45);color:var(--navy);
  background:var(--acc-05);box-shadow:0 4px 14px rgba(11,19,32,.06);
}
.btn:disabled{opacity:.45;cursor:not-allowed;transform:none;box-shadow:none}
.skip{
  font-size:.92rem;color:var(--mut);padding:8px 4px;
  transition:color .22s var(--ease),transform .22s var(--ease);
}
.skip:hover{color:var(--navy);transform:translateY(-1px)}

.unlock{
  margin-top:36px;padding-top:28px;border-top:1px solid rgba(11,19,32,.08);
  max-width:28rem;animation:fadeUp .35s var(--ease) both;
}
.unlock h2{
  font-family:var(--display);font-weight:400;font-size:1.35rem;
  margin:0 0 8px;color:var(--navy);letter-spacing:-.02em;
}
.unlock p{color:var(--mut);font-size:.95rem;margin:0 0 14px;max-width:36ch}
.unlock label{
  display:block;font:600 11px/1.2 var(--mono);letter-spacing:.06em;
  text-transform:uppercase;color:var(--mut);margin:0 0 6px;
}
.unlock input{
  width:100%;font:inherit;color:var(--text);background:var(--bg-elev);
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;outline:none;
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
.unlock input:focus{
  border-color:var(--acc-45);box-shadow:0 0 0 3px var(--acc-dim);
}
.unlock .row{display:flex;gap:10px;align-items:stretch;margin-top:10px}
.unlock .row .btn{flex:0 0 auto}
.unlock .msg{min-height:1.3em;margin:10px 0 0;font-size:.9rem;color:var(--mut)}
.unlock .msg.err{color:var(--danger)}
.unlock .msg.ok{color:var(--ok)}

.foot{
  padding:16px 24px 28px;text-align:center;
  font:12px/1.4 var(--mono);color:var(--mut);
}
.foot a{color:var(--mut)}
.foot a:hover{color:var(--navy)}

@media(max-width:520px){
  .stage{padding:36px 20px 48px}
  .cta .btn{width:100%;text-align:center}
  .unlock .row{flex-direction:column}
}
@media(prefers-reduced-motion:reduce){
  .stage,.brand-logo,.brand-row .mark path,.rule,.unlock{animation:none}
}
</style>
</head>
<body>
  <main class="stage" id="stage">
    <div class="brand-row">
      <img class="brand-logo" src="/static/ravenry-logo.png" width="340" height="88"
           alt="@@COMPANY@@" decoding="async">
    </div>
    <div class="rule" aria-hidden="true"></div>
    <h1 id="headline">Your memory starts here</h1>
    <p class="lead" id="lead">A personal memory that hears, remembers, and — with your approval — acts. This install lives on your machine.</p>
    <div class="cta" id="cta">
      <button type="button" class="btn btn-primary" id="primaryBtn">Get started</button>
      <a class="skip" id="secondaryLink" href="/today">Explore without setup</a>
    </div>

    <section class="unlock" id="unlock" hidden>
      <h2>Unlock this browser</h2>
      <p>@@BRAND@@ is reachable on your network. Paste the API token from
      <code>QUILL_API_TOKEN</code> or <code>data/.api_token</code> to continue.</p>
      <label for="tok">API token</label>
      <div class="row">
        <input id="tok" type="password" autocomplete="current-password" placeholder="Paste token">
        <button type="button" class="btn btn-primary" id="unlockBtn">Sign in</button>
      </div>
      <div class="msg" id="unlockMsg" role="status"></div>
    </section>
  </main>
  <footer class="foot">
    @@COPYRIGHT@@ · Local-first · <a href="/onboarding">Setup</a> · <a href="/today">Today</a> · <a href="/chat">Chat</a>
  </footer>
@@UI_JS@@
<script>
(function(){
  const headline=document.getElementById('headline');
  const lead=document.getElementById('lead');
  const primaryBtn=document.getElementById('primaryBtn');
  const secondaryLink=document.getElementById('secondaryLink');
  const unlock=document.getElementById('unlock');
  const unlockMsg=document.getElementById('unlockMsg');
  const tok=document.getElementById('tok');

  let state={mode:'new', home_url:'/today', onboarding_url:'/onboarding',
             user_name:'', needs_unlock:false};

  function goHome(){
    try{ MnemosMemory.set('lastRoute', state.home_url||'/today'); }catch(e){}
    location.href=state.home_url||'/today';
  }
  function goOnboarding(){
    location.href=state.onboarding_url||'/onboarding';
  }

  function render(){
    const name=(state.user_name||'').trim();
    const returning=state.mode==='returning';
    if(returning){
      headline.textContent=name?('Welcome back, '+name):'Welcome back';
      lead.textContent='Continue on this machine with your memory, or update setup anytime.';
      primaryBtn.textContent='Continue';
      primaryBtn.onclick=()=>{
        if(state.needs_unlock){
          unlock.hidden=false; tok.focus();
          unlockMsg.textContent='Unlock below to continue.';
          unlockMsg.className='msg';
          return;
        }
        goHome();
      };
      secondaryLink.textContent='Update setup';
      secondaryLink.href=state.onboarding_url||'/onboarding';
      secondaryLink.onclick=null;
    }else{
      headline.textContent='Your memory starts here';
      lead.textContent='A short setup so @@BRAND@@ knows your name, people, and work — then it can remember and help act.';
      primaryBtn.textContent='Get started';
      primaryBtn.onclick=goOnboarding;
      secondaryLink.textContent='Explore without setup';
      secondaryLink.href=state.home_url||'/today';
    }
    unlock.hidden=!state.needs_unlock;
    if(state.needs_unlock && returning){
      // Returning over LAN: make unlock the clear path.
      primaryBtn.textContent='Unlock & continue';
    }
  }

  document.getElementById('unlockBtn').onclick=async()=>{
    unlockMsg.textContent='…'; unlockMsg.className='msg';
    try{
      const r=await fetch('/auth/unlock',{method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({token:tok.value||''})});
      const j=await r.json().catch(()=>({}));
      if(!r.ok){
        unlockMsg.textContent=j.detail||('Failed ('+r.status+')');
        unlockMsg.className='msg err';
        return;
      }
      unlockMsg.textContent='Unlocked.';
      unlockMsg.className='msg ok';
      state.needs_unlock=false;
      setTimeout(goHome, 280);
    }catch(e){
      unlockMsg.textContent='Could not unlock.';
      unlockMsg.className='msg err';
    }
  };
  tok.addEventListener('keydown',e=>{
    if(e.key==='Enter'){ e.preventDefault(); document.getElementById('unlockBtn').click(); }
  });

  (async function init(){
    try{
      const r=await fetch('/welcome/status');
      const j=await r.json();
      if(j&&j.ok!==false){
        state={
          mode:j.mode||'new',
          home_url:j.home_url||'/today',
          onboarding_url:j.onboarding_url||'/onboarding',
          user_name:j.user_name||'',
          needs_unlock:!!j.needs_unlock,
        };
      }
    }catch(e){}
    render();
  })();
})();
</script>
</body>
</html>
""")
