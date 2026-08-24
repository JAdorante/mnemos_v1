"""Desktop Access panel — app permissions and recent actions."""

from app.api.mnemos_theme import apply as _mnemos

DESKTOP_ACCESS_PAGE = _mnemos(r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>@@BRAND@@ — Desktop Access</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
@@FONTS@@
<style>
@@ROOT@@
@@INK@@
@@CHROME@@
*{box-sizing:border-box}
body{
  margin:0;font:14px/1.55 var(--font);color:var(--text);
  background:
    radial-gradient(900px 480px at 6% -8%, var(--acc-05), transparent 55%),
    radial-gradient(700px 400px at 95% 5%, rgba(30,91,79,.04), transparent 50%),
    var(--paper);
  min-height:100vh;
}
.top{
  display:flex;align-items:center;gap:18px;flex-wrap:wrap;
  padding:14px 24px;
}
.page-sub{margin-left:-4px}
#msg{font-family:var(--mono);font-size:12px;color:var(--mut)}
.lead{
  color:var(--mut);font-size:13px;padding:16px 24px 0;max-width:1100px;
}
main{padding:16px 24px 40px;max-width:1100px}
.env{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:8px}
.chip{
  background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:8px 12px;font-size:12px;box-shadow:var(--shadow);
  transition:border-color .28s var(--ease),transform .22s var(--ease),box-shadow .28s var(--ease);
  animation:fadeUp .3s var(--ease) both;
}
.chip:hover{transform:translateY(-1px);box-shadow:0 4px 14px rgba(11,19,32,.07)}
.chip b{color:var(--text)}.chip .k{color:var(--mut)}
.chip.warn{border-color:rgba(199,138,44,.4)}.chip.ok{border-color:rgba(46,111,87,.4)}
table{width:100%;border-collapse:collapse;font-size:13px;background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);overflow:hidden;box-shadow:var(--shadow)}
th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:none}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.04em;background:var(--panel-2)}
tbody tr{transition:background .22s var(--ease)}
tbody tr:hover{background:var(--acc-05)}
.badge{display:inline-block;border-radius:999px;padding:2px 9px;font-size:11px;font-weight:600}
.b-ok{background:rgba(46,111,87,.1);color:var(--ok);border:1px solid rgba(46,111,87,.28)}
.b-no{background:rgba(166,71,71,.1);color:var(--danger);border:1px solid rgba(166,71,71,.28)}
.b-off{background:rgba(199,138,44,.1);color:var(--warn);border:1px solid rgba(199,138,44,.28)}
.path{font-family:var(--mono);font-size:11px;color:var(--mut);word-break:break-all;cursor:pointer;transition:color .22s var(--ease)}
.path:hover{color:var(--navy)}
.caps{color:var(--mut);font-size:11px}
button{
  background:var(--panel);color:var(--text);border:1px solid var(--line);
  border-radius:10px;padding:6px 11px;font-size:12px;font-family:var(--font);
  cursor:pointer;margin-right:6px;
}
button:hover{background:var(--panel-2);border-color:var(--acc-28)}
button.danger{border-color:rgba(166,71,71,.4);color:var(--danger)}
button.danger:hover{background:rgba(166,71,71,.1);border-color:rgba(166,71,71,.55);box-shadow:0 4px 14px rgba(166,71,71,.12)}
h2{
  font-family:var(--display);font-size:1.15rem;color:var(--navy);
  font-weight:400;letter-spacing:-.01em;margin:28px 0 10px;text-transform:none;
}
h2 .caps{font-family:var(--font);font-size:12px;letter-spacing:0;text-transform:none;font-weight:500}
.rec{
  font-size:12px;border-bottom:1px solid var(--line);padding:9px 0;
  display:flex;gap:14px;flex-wrap:wrap;animation:fadeUp .3s var(--ease) both;
}
.rec .when{color:var(--mut);font-family:var(--mono);white-space:nowrap}
.rec .out-ok{color:var(--ok)}.rec .out-blocked{color:var(--danger)}.rec .out-nonzero{color:var(--warn)}
.stats{display:flex;flex-wrap:wrap;gap:10px;margin:4px 0 10px}
.stat{
  background:var(--panel);border:1px solid var(--line);border-radius:var(--radius);
  padding:14px 16px;min-width:104px;box-shadow:var(--shadow);animation:fadeUp .35s var(--ease) both;
}
.stat .n{font-family:var(--display);font-size:1.65rem;font-weight:400;letter-spacing:-.02em;color:var(--navy)}
.stat .l{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.03em;margin-top:3px}
.safety{margin:2px 0 8px;font-size:12px;color:var(--mut)}
.safety .s-ok{color:var(--ok)}
.safety .s-tag{
  display:inline-block;background:rgba(166,71,71,.08);color:var(--danger);
  border:1px solid rgba(166,71,71,.28);border-radius:999px;padding:2px 9px;
  margin:2px 6px 2px 0;font-size:11px;
}
@media(max-width:720px){
  .top{padding:10px 14px;gap:10px}
  .lead{padding:12px 14px 0}
  main{padding:12px 14px 32px}
  table{display:block;overflow-x:auto;-webkit-overflow-scrolling:touch}
  .stats{gap:8px}
  .stat{min-width:calc(50% - 8px);flex:1 1 calc(50% - 8px)}
  .rec{flex-direction:column;gap:6px}
}
</style></head><body>
<header class="top">
  <a class="brand" href="/">@@MARK@@ @@BRAND@@</a>
  <span class="page-sub">Desktop</span>
  @@NAV@@
  <span class="spacer"></span>
  <span id="msg"></span>
</header>
<p class="lead">What the desktop agent may launch and do on this machine — the allowlist, made visible.</p>
<main>
  <div class="env" id="env"></div>
  <h2>Reliability <span class="caps" style="text-transform:none;letter-spacing:0">— measured from the audit log</span></h2>
  <div class="stats" id="stats"></div>
  <div class="safety" id="safety"></div>
  <h2>Apps</h2>
  <p class="lead" style="padding-top:0;margin-top:-4px">Shipped apps, apps you remembered on first use, and one-time launches — in plain language.</p>
  <table><thead><tr>
    <th>App</th><th>Status</th><th>What it can do</th><th>Resolved path</th><th>UI control</th>
    <th>Risk</th><th>Actions</th>
  </tr></thead><tbody id="apps"></tbody></table>
  <h2>Recent actions</h2>
  <div id="recent"></div>
</main>
<script>
const msg = document.getElementById('msg');
function note(t){ msg.textContent = t; if(t) setTimeout(()=>{if(msg.textContent===t)msg.textContent='';}, 4000); }

function envChip(k,v,cls){ return `<div class="chip ${cls||''}"><span class="k">${k}:</span> <b>${v}</b></div>`; }

async function load(){
  const s = await (await fetch('/console/desktop-access')).json();
  const e = s.environment;
  document.getElementById('env').innerHTML =
    envChip('jail', e.jail) +
    envChip('pixel UI', e.pixel_ui?'on':'off', e.pixel_ui?'ok':'off') +
    envChip('approval', e.approval_required?'required':'autonomous') +
    envChip('autonomy', e.autonomy_desktop) +
    envChip('shell auto', e.autonomy_shell?'on':'off', e.autonomy_shell?'warn':'') +
    envChip('auto-run', (e.auto_verbs||[]).join(', ')||'none') +
    envChip('needs approval', (e.gated_verbs||[]).join(', ')||'none');
  document.getElementById('apps').innerHTML = s.apps.map(row).join('');
  loadMetrics();
  loadRecent();
}
function tile(n,l){ return `<div class="stat"><div class="n">${n}</div><div class="l">${l}</div></div>`; }
async function loadMetrics(){
  const m = await (await fetch('/console/desktop-metrics')).json();
  const pct = x => Math.round((x||0)*100)+'%';
  document.getElementById('stats').innerHTML =
    tile(m.totals.records, 'actions') +
    tile(pct(m.launch.success_rate), 'launch success') +
    tile(pct(m.run_command.success_rate), 'run_cmd exit-0') +
    tile(pct(m.totals.refusal_rate), 'refusal rate') +
    tile(m.per_task.avg_actions, 'avg actions/task') +
    tile(m.repeated_failures, 'repeat-fail loops');
  const unsafe = Object.entries(m.safety).filter(([k,v])=>v>0);
  document.getElementById('safety').innerHTML = 'Safety refusals: ' + (unsafe.length
    ? unsafe.map(([k,v])=>`<span class="s-tag">${k.replace(/_/g,' ')}: ${v}</span>`).join('')
    : '<span class="s-ok">none recorded</span>');
}
function statusBadge(a){
  if(a.disabled) return '<span class="badge b-no">Disabled</span>';
  if(!a.installed) return '<span class="badge b-off">Not found</span>';
  let s = '<span class="badge b-ok">Launch allowed</span>';
  if(a.remembered) s += ' <span class="badge b-ok">Remembered</span>';
  else if(a.discovered) s += ' <span class="badge b-off">Launch-only</span>';
  return s;
}
function ui(a){
  if(a.special) return '<span class="caps">special (SMS)</span>';
  if(a.ui_control==='n/a') return '<span class="caps">n/a</span>';
  return a.ui_control==='on' ? '<span class="badge b-ok">on</span>'
                            : '<span class="badge b-off">off</span>';
}
function caps(a){
  return `<span class="caps">${a.capability_summary||a.notes||'launch only'}</span>`;
}
function row(a){
  const p = a.resolved_path ? `<span class="path" title="click to copy" onclick="navigator.clipboard.writeText('${a.resolved_path.replace(/\\/g,'\\\\')}');note('path copied')">${a.resolved_path}</span>` : '<span class="caps">—</span>';
  const toggle = a.disabled
    ? `<button onclick="toggle('${a.key}',false)">Enable</button>`
    : `<button class="danger" onclick="toggle('${a.key}',true)">Disable</button>`;
  const test = a.launch_allowed ? `<button onclick="testLaunch('${a.key}')">Test launch</button>` : '';
  const forget = a.remembered ? `<button class="danger" onclick="revoke('${a.key}')">Forget</button>` : '';
  return `<tr>
    <td><b>${a.display_name}</b><br><span class="caps">${a.key}${a.template_label?' · '+a.template_label:''}</span></td>
    <td>${statusBadge(a)}</td><td>${caps(a)}</td><td>${p}</td><td>${ui(a)}</td>
    <td class="caps">${a.risk}</td>
    <td>${toggle}${test}${forget}</td></tr>`;
}
async function revoke(app){
  await fetch('/console/desktop-access/revoke',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({app})});
  note(`${app} removed from remembered apps`);
  load();
}
async function toggle(app, disabled){
  await fetch('/console/desktop-access/toggle',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({app,disabled})});
  note(disabled?`${app} disabled`:`${app} enabled`);
  load();
}
async function testLaunch(app){
  note(`launching ${app}…`);
  const r = await (await fetch('/console/desktop-access/test-launch',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({app})})).json();
  note(r.ok ? `${app} launched` : `refused: ${r.detail}`);
  loadRecent();
}
async function loadRecent(){
  const r = await (await fetch('/console/desktop-access/recent?limit=10')).json();
  document.getElementById('recent').innerHTML = (r.recent||[]).map(a=>
    `<div class="rec"><span class="when">${a.when||''}</span>
     <span class="out-${a.outcome||''}">${a.action}</span>
     <span>${a.target||''}</span>
     <span class="caps">${a.detail||''}</span></div>`).join('') || '<div class="caps">no actions yet</div>';
}
load();
</script>
@@UI_JS@@
</body></html>""")
