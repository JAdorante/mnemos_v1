"""LAN unlock form — sets an HttpOnly session cookie for browser UIs."""

from app.api.mnemos_theme import apply_plain as _plain

AUTH_PAGE = _plain("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@BRAND@@ — unlock</title>
@@FONTS@@
<style>
@@ROOT@@
body{font:15px/1.45 var(--font);max-width:28rem;margin:3rem auto;padding:0 1rem;
color:var(--text);background:var(--paper)}
h1{font-size:1.35rem;font-weight:650;margin:0 0 .5rem;color:var(--navy)}
p{color:var(--mut);margin:0 0 1rem}
input,button{font:inherit;padding:.55rem .7rem;border-radius:6px;border:1px solid var(--line);
width:100%;box-sizing:border-box;background:var(--panel);color:var(--text)}
button{margin-top:.6rem;background:var(--navy);color:var(--paper);border-color:var(--navy);cursor:pointer}
#msg{margin-top:.8rem;min-height:1.2em}
</style></head><body>
<h1>Unlock LAN access</h1>
<p>This server is reachable on the network. Paste the API token from
<code>QUILL_API_TOKEN</code> or <code>data/.api_token</code>.</p>
<input id="tok" type="password" autocomplete="off" placeholder="API token">
<button id="go" type="button">Unlock</button>
<div id="msg" role="status" aria-live="polite"></div>
<script>
const msg=document.getElementById('msg');
document.getElementById('go').onclick=async()=>{
  msg.textContent='…';
  const r=await fetch('/auth/unlock',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({token:document.getElementById('tok').value})});
  const j=await r.json().catch(()=>({}));
  if(r.ok){msg.textContent='Unlocked. You can close this tab and use the UI.';
    /* Return to the page that sent us here. Same-origin relative paths only —
       a leading "//" or a scheme would be an open redirect. */
    let next='/';
    try{
      const raw=new URLSearchParams(location.search).get('next')||'';
      if(raw.startsWith('/')&&!raw.startsWith('//')&&!raw.includes('\\')) next=raw;
    }catch(e){}
    location.href=next;}
  else msg.textContent=j.detail||('Failed ('+r.status+')');
};
</script></body></html>""")
