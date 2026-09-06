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
.wrap{max-width:1120px;margin:0 auto;padding:26px 24px 80px}
h1{font-family:var(--display);font-weight:400;font-size:2rem;letter-spacing:-.02em;color:var(--navy);margin:0 0 6px}
.lead{color:var(--mut);margin:0 0 22px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:20px;box-shadow:var(--shadow);margin-bottom:18px}
.panel h2{font-family:var(--display);font-weight:400;font-size:1.35rem;margin:0 0 10px;color:var(--navy)}
.btn{appearance:none;cursor:pointer;font:inherit;font-weight:500;border-radius:var(--r-sm);
  padding:10px 18px;background:transparent;color:var(--mut);border:1px solid var(--line)}
.btn:hover{color:var(--text);border-color:var(--faint)}
.btn.primary{background:var(--violet);color:var(--acc-fg);border-color:transparent;font-weight:600}
.btn.primary:hover{filter:brightness(1.08);color:var(--acc-fg)}
.btn-ghost{background:transparent;color:var(--mut);border:1px solid var(--line)}
.row{display:flex;gap:10px;flex-wrap:wrap;margin:10px 0}
input,select,textarea{font:inherit;padding:10px 12px;border:1px solid var(--line);border-radius:12px;background:var(--bg-elev);color:var(--text)}
input,select{flex:1;min-width:160px}
textarea{width:100%;min-height:80px}
.muted{color:var(--mut);font-size:.92rem}
.ok{color:var(--ok);font-weight:600}
/* Guidance, not an alert: amber left border, quiet fill (spec §7). */
.note{border:1px solid var(--line);border-left:3px solid var(--amber);
  background:var(--raised);border-radius:var(--r-md);padding:12px 14px;
  font-size:.92rem;margin:12px 0}
.note.setup{border-left-color:var(--violet)}
.note .code-step{
  display:block;margin-top:8px;font:13px/1.6 var(--mono);color:var(--text);
  background:var(--ink);border:1px solid var(--line);border-radius:var(--r-sm);
  padding:8px 12px;width:fit-content;max-width:100%;overflow-x:auto;
}
dl.status{margin:0;display:grid;grid-template-columns:auto 1fr;gap:6px 18px;font-size:14px}
dl.status dt{color:var(--mut)}
dl.status dd{margin:0;color:var(--text)}
dl.status dd .ok-dot,dl.status dd .warn-dot{
  display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:7px;vertical-align:baseline}
dl.status dd .ok-dot{background:var(--green)}
dl.status dd .warn-dot{background:var(--amber)}
pre{white-space:pre-wrap;font-family:var(--mono);font-size:.82rem;background:var(--ink);
  border:1px solid var(--line);border-radius:var(--r-sm);padding:12px;max-height:280px;
  max-width:100%;overflow:auto}
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
  <div class="note" id="flagNote" hidden></div>

  <div class="panel">
    <h2>Status</h2>
    <dl class="status" id="statusList"><dt>Status</dt><dd>Loading…</dd></dl>
    <details class="disclosure" style="margin-top:10px">
      <summary>Show raw status</summary>
      <pre id="statusBox">Loading…</pre>
    </details>
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
      <button class="btn primary" id="regBtn" type="button">Register</button>
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
function esc(t){return String(t==null?'':t).replace(/[&<>"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function renderStatus(s){
  const reach = s.coordinator_reachable;
  const rows = [
    ['Status', s.enabled
      ? (reach ? '<span class="ok-dot"></span>Connected'
               : '<span class="warn-dot"></span>On, coordinator unreachable')
      : 'Not connected'],
    ['Coordinator', esc(s.coordinator_url || '—') + (s.enabled && !reach ? ' · unreachable' : '')],
    ['Role', esc((s.role || 'ic').toUpperCase() === 'IC' ? 'IC' : (s.role||'—'))],
    ['Reports to', esc(s.reports_to || '—')],
    ['Node', esc(s.node_id || '—')],
  ];
  document.getElementById('statusList').innerHTML =
    rows.map(([k,v])=>'<dt>'+k+'</dt><dd>'+v+'</dd>').join('');
}
async function refresh(){
  const s = await j('/org-network/status');
  document.getElementById('statusBox').textContent = JSON.stringify(s, null, 2);
  renderStatus(s);
  const note = document.getElementById('flagNote');
  note.hidden = false;
  if(s.enabled && s.coordinator_reachable){
    note.hidden = true;
  } else if(s.enabled && !s.coordinator_reachable){
    note.className = 'note';
    note.innerHTML = 'The coordinator is not reachable at <code>'+esc(s.coordinator_url||'')+'</code>.'
      +' Restart with <span class="code-step">python run_all.py</span>'
      +' (auto-starts it) or run <span class="code-step">python -m org_coordinator.main</span>';
  } else {
    note.className = 'note setup';
    note.innerHTML = '<b>Org Network is off.</b> It shares roles, goals, and redacted digests '
      +'with a lightweight coordinator — your full memory stays on this machine, and capture, '
      +'peer consent, and approval gates are unchanged. To turn it on, add this to <code>.env</code> '
      +'and restart:<span class="code-step">QUILL_ORG_NETWORK=1</span>';
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
