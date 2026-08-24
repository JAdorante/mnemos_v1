"""Org Network page (/org-network): register with the Org Coordinator, set
role / reporting line, run digests, pull priorities, and create CEO goals."""

from app.api.mnemos_theme import apply as _mnemos

ORG_NETWORK_PAGE = _mnemos(r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>@@BRAND@@ — Org Network</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{margin:0;font:16px/1.55 var(--font);color:var(--text);background:var(--paper)}
.top{position:sticky;top:0;display:flex;gap:14px;align-items:center;padding:10px 20px;z-index:var(--z-raised);background:var(--chrome-bg);backdrop-filter:blur(10px)}
.wrap{max-width:860px;margin:0 auto;padding:26px 20px 80px}
h1{font-family:var(--display);font-weight:400;font-size:2rem;letter-spacing:-.02em;color:var(--navy);margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 22px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px;box-shadow:var(--shadow);margin-bottom:18px}
.panel h2{font-family:var(--display);font-weight:400;font-size:1.35rem;margin:0 0 10px;color:var(--navy)}
.btn{appearance:none;border:0;cursor:pointer;font:inherit;font-weight:600;border-radius:12px;
  padding:11px 20px;background:var(--navy);color:#F8F6F1}
.btn-ghost{background:transparent;color:var(--text);border:1px solid var(--line)}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
input,select,textarea{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev);color:var(--text)}
input,select{flex:1;min-width:160px}
textarea{width:100%;min-height:80px}
.muted{color:var(--mut);font-size:.92rem}
.ok{color:var(--ok);font-weight:600}
.note{border:1px solid rgba(199,138,44,.35);background:rgba(199,138,44,.08);
  border-radius:12px;padding:12px 14px;font-size:.92rem;margin:12px 0}
pre{white-space:pre-wrap;font-family:var(--mono);font-size:.82rem;background:var(--bg-elev);
  border:1px solid var(--line);border-radius:12px;padding:12px;max-height:280px;overflow:auto}
@media(max-width:640px){
  .top{padding:8px 14px;gap:10px;flex-wrap:wrap}
  .wrap{padding:18px 14px 64px}
  h1{font-size:clamp(1.5rem,6vw,2rem)}
  .panel{padding:16px}
  .row{flex-direction:column;align-items:stretch}
  input,select{min-width:0;width:100%}
  pre{max-height:220px;font-size:.78rem}
}
</style>
</head>
<body>
<div class="top"><a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Org Network</span>
  @@NAV@@
  <span class="spacer"></span></div>
<div class="wrap">
  <h1>Org Network</h1>
  <p class="lead">Hybrid company intelligence: this machine keeps your full memory local.
  A lightweight Org Coordinator holds roles, goals, and redacted digests — never raw clips.</p>
  <div class="note" id="flagNote">Feature flag <code>QUILL_ORG_NETWORK</code> must be on.
  Capture, peer consent, and approval gates are unchanged.</div>

  <div class="panel">
    <h2>Status</h2>
    <pre id="statusBox">Loading…</pre>
  </div>

  <div class="panel">
    <h2>Register with coordinator</h2>
    <div class="row">
      <input id="nodeId" placeholder="node id (e.g. alice-ic)">
      <input id="displayName" placeholder="display name">
      <select id="role">
        <option value="ic">IC</option>
        <option value="manager">Manager</option>
        <option value="exec">Exec</option>
        <option value="ceo">CEO</option>
      </select>
    </div>
    <div class="row">
      <input id="reportsTo" placeholder="reports_to node id">
      <input id="managerPeer" placeholder="manager peer_id (from /peer)">
      <input id="coordUrl" placeholder="coordinator URL">
    </div>
    <div class="row">
      <button class="btn" id="regBtn" type="button">Register</button>
      <button class="btn btn-ghost" id="refreshBtn" type="button">Refresh status</button>
    </div>
    <div id="regMsg" class="muted"></div>
  </div>

  <div class="panel">
    <h2>Upward digest</h2>
    <p class="muted">Summarize open work + blockers and ship to the coordinator (and manager peer if set).</p>
    <button class="btn" id="digestBtn" type="button">Run digest now</button>
    <pre id="digestOut" hidden></pre>
  </div>

  <div class="panel">
    <h2>Downward priorities</h2>
    <p class="muted">Pull cascaded company goals into local guidance (chat grounding).</p>
    <button class="btn" id="priBtn" type="button">Pull priorities</button>
    <pre id="priOut" hidden></pre>
  </div>

  <div class="panel">
    <h2>CEO / exec goal</h2>
    <p class="muted">Create a company goal on the coordinator (manager+ roles).</p>
    <div class="row">
      <input id="goalTitle" placeholder="Goal title">
      <input id="goalHorizon" placeholder="Horizon (e.g. Q3 launch)">
    </div>
    <textarea id="goalDetail" placeholder="Detail"></textarea>
    <div class="row">
      <button class="btn" id="goalBtn" type="button">Create goal</button>
      <button class="btn btn-ghost" id="cascadeBtn" type="button">Cascade priorities</button>
    </div>
    <pre id="goalOut" hidden></pre>
  </div>
</div>
<script>
async function j(url, opts){
  const r = await fetch(url, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  return r.json();
}
async function refresh(){
  const s = await j('/org-network/status');
  document.getElementById('statusBox').textContent = JSON.stringify(s, null, 2);
  const note = document.getElementById('flagNote');
  if(s.enabled && s.coordinator_reachable){
    note.classList.add('ok');
    note.textContent = 'Org network on · coordinator reachable. Register below, then run digests / pull priorities.';
  } else if(s.enabled && !s.coordinator_reachable){
    note.classList.remove('ok');
    note.innerHTML = 'Org network is <b>on</b> but the coordinator is not reachable at <code>'+
      (s.coordinator_url||'')+'</code>. Restart with <code>python run_all.py</code> (auto-starts it) or run <code>python -m org_coordinator.main</code>.';
  } else {
    note.classList.remove('ok');
    note.innerHTML = 'Set <code>QUILL_ORG_NETWORK=1</code> in <code>.env</code> and restart. Capture / peer / approvals stay unchanged.';
  }
  if(s.coordinator_url) document.getElementById('coordUrl').value = s.coordinator_url;
  if(s.node_id) document.getElementById('nodeId').value = s.node_id;
  if(s.role) document.getElementById('role').value = s.role;
  if(s.reports_to) document.getElementById('reportsTo').value = s.reports_to;
  if(s.manager_peer_id) document.getElementById('managerPeer').value = s.manager_peer_id;
}
document.getElementById('refreshBtn').onclick = refresh;
document.getElementById('regBtn').onclick = async () => {
  const body = {
    node_id: document.getElementById('nodeId').value.trim(),
    display_name: document.getElementById('displayName').value.trim(),
    role: document.getElementById('role').value,
    reports_to: document.getElementById('reportsTo').value.trim(),
    manager_peer_id: document.getElementById('managerPeer').value.trim(),
    coordinator_url: document.getElementById('coordUrl').value.trim(),
  };
  const res = await j('/org-network/register', {method:'POST', body: JSON.stringify(body)});
  document.getElementById('regMsg').textContent = res.ok ? 'Registered.' : (res.error || res.detail || 'failed');
  refresh();
};
document.getElementById('digestBtn').onclick = async () => {
  const res = await j('/org-network/digest', {method:'POST', body:'{}'});
  const el = document.getElementById('digestOut'); el.hidden=false;
  el.textContent = JSON.stringify(res, null, 2);
};
document.getElementById('priBtn').onclick = async () => {
  const res = await j('/org-network/priorities', {method:'POST', body:'{}'});
  const el = document.getElementById('priOut'); el.hidden=false;
  el.textContent = JSON.stringify(res, null, 2);
};
document.getElementById('goalBtn').onclick = async () => {
  const body = {
    title: document.getElementById('goalTitle').value.trim(),
    detail: document.getElementById('goalDetail').value.trim(),
    horizon: document.getElementById('goalHorizon').value.trim(),
  };
  const res = await j('/org-network/goals', {method:'POST', body: JSON.stringify(body)});
  const el = document.getElementById('goalOut'); el.hidden=false;
  el.textContent = JSON.stringify(res, null, 2);
};
document.getElementById('cascadeBtn').onclick = async () => {
  const res = await j('/org-network/cascade', {method:'POST', body:'{}'});
  const el = document.getElementById('goalOut'); el.hidden=false;
  el.textContent = JSON.stringify(res, null, 2);
};
refresh();
</script>
</body>
</html>
""")
