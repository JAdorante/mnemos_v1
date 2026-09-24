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
  background:var(--ink);
}
a{color:var(--navy);text-decoration:none}
a:hover{opacity:.8}

.welcome-bar{
  display:flex;justify-content:flex-end;align-items:center;
  padding:16px 20px 0;max-width:720px;width:100%;margin:0 auto;
}
.stage{
  flex:1;display:flex;flex-direction:column;justify-content:center;align-items:center;
  text-align:center;
  max-width:720px;width:100%;margin:0 auto;padding:32px 24px 64px;
  animation:morningPaper .45s var(--ease) both;
}
.brand-stack{
  display:flex;flex-direction:column;align-items:center;gap:6px;
  margin:0 0 48px;
}
.company-name{
  font:600 11px/1.2 var(--sans);letter-spacing:.22em;
  text-transform:uppercase;color:var(--mut);margin:0;
}
.builds{
  font:500 9px/1.2 var(--sans);letter-spacing:.28em;
  text-transform:uppercase;color:var(--faint);margin:0;
}
.product-row{
  display:inline-flex;align-items:center;gap:10px;margin-top:10px;
  font:600 12px/1 var(--sans);letter-spacing:.2em;
  text-transform:uppercase;color:var(--text);
}
.product-dot{
  width:8px;height:8px;border-radius:50%;background:var(--violet);flex:0 0 auto;
  box-shadow:0 0 0 4px var(--violet-dim),0 0 18px var(--acc-35);
}
h1{
  font-family:var(--sans);font-weight:650;
  font-size:clamp(1.85rem,5.2vw,2.75rem);letter-spacing:var(--track-tight);
  margin:0 0 14px;max-width:18ch;color:var(--text);line-height:1.15;
}
.lead{
  color:var(--muted);
  font-size:1.05rem;line-height:1.55;max-width:38ch;margin:0 0 32px;
}
.cta{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:center}
.btn{
  appearance:none;border:1px solid transparent;cursor:pointer;font:inherit;font-weight:600;
  font-size:14px;border-radius:var(--r-sm);padding:11px 18px;
}
.btn-primary{background:var(--violet);color:var(--acc-fg)}
.btn-primary:hover:not(:disabled){filter:brightness(1.08)}
.btn-ghost{background:transparent;color:var(--mut);border-color:var(--line)}
.btn-ghost:hover:not(:disabled){color:var(--text);border-color:var(--faint)}
.btn:disabled{opacity:.45;cursor:not-allowed}
.skip{
  font-size:.92rem;color:var(--mut);padding:8px 4px;
  transition:color .22s var(--ease),transform .22s var(--ease);
}
.skip:hover{color:var(--navy);transform:translateY(-1px)}

.unlock{
  margin-top:40px;padding-top:28px;border-top:1px solid var(--ink-08);
  max-width:28rem;width:100%;text-align:left;animation:fadeUp .35s var(--ease) both;
}
.unlock h2{
  font-family:var(--display);font-weight:400;font-size:1.35rem;
  margin:0 0 8px;color:var(--navy);letter-spacing:-.02em;
}
.unlock p{color:var(--mut);font-size:.95rem;margin:0 0 14px;max-width:36ch}
.unlock label{
  display:block;font:500 13px/1.2 var(--sans);
  color:var(--mut);margin:0 0 6px;
}
.unlock input{
  width:100%;font:inherit;color:var(--text);background:var(--bg-elev);
  border:1px solid var(--line);border-radius:12px;padding:12px 14px;outline:none;
  transition:border-color .28s var(--ease),box-shadow .28s var(--ease);
}
.unlock input:focus{
  outline:2px solid var(--violet);outline-offset:-1px;border-color:transparent;
}
.unlock .row{display:flex;gap:10px;align-items:stretch;margin-top:10px}
.unlock .row .btn{flex:0 0 auto}
.unlock .msg{min-height:1.3em;margin:10px 0 0;font-size:.9rem;color:var(--mut)}
.unlock .msg.err{color:var(--danger)}
.unlock .msg.ok{color:var(--ok)}

.foot{
  padding:16px 24px 28px;text-align:center;
  font:13px/1.4 var(--sans);color:var(--faint);
}
.foot a{color:var(--mut)}
.foot a:hover{color:var(--navy)}

@media(max-width:520px){
  .stage{padding:24px 20px 48px}
  .brand-stack{margin-bottom:36px}
  .cta .btn{width:100%;text-align:center}
  .unlock .row{flex-direction:column}
}
@media(prefers-reduced-motion:reduce){
  .stage,.unlock{animation:none}
}
</style>
</head>
<body>
  <div class="welcome-bar">
    <button type="button" class="theme-toggle" id="mnemosThemeToggle"
      title="Toggle light / dark" aria-label="Toggle light and dark mode">
      <svg class="icon-sun" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <circle cx="12" cy="12" r="4" stroke="currentColor" stroke-width="1.75"/>
        <path d="M12 2v2.5M12 19.5V22M4.93 4.93l1.77 1.77M17.3 17.3l1.77 1.77M2 12h2.5M19.5 12H22M4.93 19.07l1.77-1.77M17.3 6.7l1.77-1.77"
          stroke="currentColor" stroke-width="1.75" stroke-linecap="round"/>
      </svg>
      <svg class="icon-moon" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M20 14.5A8.5 8.5 0 0 1 9.5 4 7 7 0 1 0 20 14.5z"
          stroke="currentColor" stroke-width="1.75" stroke-linejoin="round"/>
      </svg>
    </button>
  </div>
  <main class="stage" id="stage">
    <div class="brand-stack">
      <p class="company-name">@@COMPANY@@</p>
      <p class="builds">Builds</p>
      <div class="product-row">
        <span class="product-dot" aria-hidden="true"></span>
        <span>@@BRAND@@</span>
      </div>
    </div>
    <h1 id="headline">A nervous system for your company.</h1>
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
      headline.textContent='A nervous system for your company.';
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
