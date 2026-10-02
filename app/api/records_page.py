"""Records review page — clear a meeting's worth of claims in one pass.

Claims are grouped by scope, then subject. Each proposed claim shows the exact
packet the approver binds to (its hash prefix), the evidence it rests on, and
the current org record value when there is one, so approve is a decision
about a visible diff. Every approval is still its own packet and hash.

Renders from JSON endpoints; dynamic text lands via textContent only. POSTs
send the double-submit CSRF header — /packets/*/decide refuses without it.
"""

from app.api.mnemos_theme import apply_plain as _plain

RECORDS_PAGE = _plain("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Records — @@BRAND@@</title>
@@FONTS@@
<style>
@@ROOT@@
body { font: 15px/1.5 var(--font); color: var(--text); max-width: 760px;
       margin: 32px auto; padding: 0 16px; background: var(--paper); }
h1 { font-size: 20px; margin-bottom: 4px; } h1 + p { color: var(--mut); margin-top: 0; }
h2 { font-size: 15px; margin: 28px 0 6px; }
h3 { font-size: 13px; color: var(--mut); font-weight: 600; margin: 14px 0 4px; }
.bar { font-size: 13px; color: var(--mut); display: flex; gap: 14px; flex-wrap: wrap; }
.card { border: 1px solid color-mix(in srgb, var(--mut) 35%, transparent);
        border-radius: 8px; padding: 12px 14px; margin: 10px 0; }
.card.conflicting { border-color: color-mix(in srgb, #c2410c 60%, transparent); }
.kind { font-size: 11px; text-transform: uppercase; letter-spacing: .04em;
        color: var(--mut); }
.value { font-weight: 600; margin: 2px 0 6px; overflow-wrap: anywhere; }
.ev { font-size: 13px; color: var(--mut); border-left: 2px solid
      color-mix(in srgb, var(--mut) 40%, transparent); padding-left: 8px;
      margin: 4px 0; overflow-wrap: anywhere; }
.ev.expired { text-decoration: line-through; }
.meta { font-size: 12px; color: var(--mut); margin-top: 6px; }
.cur { font-size: 13px; margin: 6px 0; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-top: 8px; }
button, select, input, textarea { font: inherit; font-size: 13px; }
button { padding: 6px 14px; border-radius: 6px; cursor: pointer; }
button.primary { font-weight: 600; }
textarea { width: 100%; min-height: 70px; box-sizing: border-box; }
.msg { font-size: 13px; margin-top: 6px; }
.empty { color: var(--mut); margin-top: 16px; }
code { font-size: 12px; }
</style></head><body>
<h1>Records</h1>
<p>Facts pulled from your capture stay yours until you approve them into a
team record. Approving binds to the exact record shown — nothing else is sent.</p>
<div class="bar" id="bar"></div>
<div id="join"></div>
<div id="forwarded"></div>
<div id="list"></div>
<script>
const CSRF = (document.cookie.split('; ').find(c => c.startsWith('quill_csrf=')) || '')
  .split('=')[1] || '';
let scopes = [];

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
async function post(url, body) {
  const r = await fetch(url, { method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': CSRF },
    body: JSON.stringify(body || {}) });
  let d = {}; try { d = await r.json(); } catch (e) {}
  return { ok: r.ok && d.ok !== false, d };
}
function valueText(c) {
  const v = c.value || {};
  if (c.kind === 'commitment') return (v.owner ? v.owner + ' owes: ' : '') + (v.text || '')
    + (v.counterparty ? ' → ' + v.counterparty : '') + (v.due ? ' (due ' + v.due + ')' : '');
  if (c.kind === 'fact') return (c.subject_label || c.subject_ref) + ' ' + c.predicate
    + ' ' + (v.value || '') + (v.text ? ' — ' + v.text : '');
  return JSON.stringify(v);
}
function scopeSelect(selected) {
  const s = el('select');
  scopes.filter(x => (x.permissions || []).some(p => p !== 'read')).forEach(x => {
    const o = el('option', '', x.name + ' (' + x.kind + ')'); o.value = x.id;
    if (x.id === selected) o.selected = true; s.append(o);
  });
  return s;
}
function evidence(c, host) {
  (c.evidence || []).forEach(e => {
    const d = el('div', 'ev' + (e.status === 'expired' ? ' expired' : ''),
      '“' + (e.span || '') + '”' + (e.speaker ? ' — ' + e.speaker : ''));
    d.title = e.source + ' · ' + new Date((e.t || 0) * 1000).toLocaleString()
      + (e.status === 'expired' ? ' · capture expired' : '');
    host.append(d);
  });
}
function card(c) {
  const box = el('div', 'card' + (c.status === 'conflicting' ? ' conflicting' : ''));
  box.append(el('div', 'kind', c.kind + ' · ' + c.status
    + (c.personal_only ? ' · personal' : '')));
  box.append(el('div', 'value', valueText(c)));
  evidence(c, box);
  const meta = el('div', 'meta', 'confidence ' + (c.confidence || 0).toFixed(2)
    + ' · ' + c.capture_source + (c.status_reason ? ' · ' + c.status_reason : ''));
  box.append(meta);
  const msg = el('div', 'msg');
  const row = el('div', 'row');
  const done = (r, ok) => { msg.textContent = ok ? ok : ((r.d.code || 'error')
    + (r.d.detail ? ': ' + r.d.detail : '')); if (r.ok) setTimeout(load, 700); };

  if (c.personal_only) {
    const b = el('button', '', 'Discard');
    b.onclick = async () => done(await post('/claims/' + c.id + '/discard', {}), 'Discarded');
    row.append(b);
  } else if (['draft', 'conflicting'].includes(c.status) || (c.status === 'proposed' && !c.open_packet)) {
    if (!scopes.length) { row.append(el('span', 'meta', 'Join an org to propose this.')); }
    else {
      const sel = scopeSelect(c.proposed_scope);
      const b = el('button', 'primary', c.status === 'conflicting' ? 'Pick this one' : 'Propose');
      b.onclick = async () => done(await post('/claims/' + c.id + '/propose',
        { scope_id: sel.value }), 'Proposed');
      row.append(sel, b);
    }
    const x = el('button', '', 'Discard');
    x.onclick = async () => done(await post('/claims/' + c.id + '/discard', {}), 'Discarded');
    row.append(x);
  } else if (c.open_packet && ['proposed', 'edited'].includes(c.status)) {
    const p = c.open_packet;
    const scope = scopes.find(s => s.id === c.proposed_scope);
    box.append(el('div', 'meta', 'into ' + (scope ? scope.name : c.proposed_scope)
      + ' · packet ' + p.payload_hash.slice(0, 8) + ' · expires '
      + new Date(p.expires_at * 1000).toLocaleDateString()));
    const cur = el('div', 'cur'); box.append(cur);
    fetch('/claims/' + c.id).then(r => r.json()).then(d => {
      const rec = d.current_record;
      if (rec && rec.value_json) cur.textContent = 'Currently recorded: '
        + JSON.stringify(rec.value_json) + ' (v' + rec.version + ')';
      else if (rec && rec.error) cur.textContent = 'Org record unavailable: ' + rec.error;
      else cur.textContent = 'No current record — this creates one.';
    });
    const perms = (scope && scope.permissions) || [];
    const canApprove = perms.includes('approve') || perms.includes('admin');
    if (canApprove) {
      const a = el('button', 'primary', 'Approve');
      a.onclick = async () => { a.disabled = true; done(await post('/packets/' + p.id + '/decide',
        { decision: 'approve', payload_hash: p.payload_hash, approved_via: 'button' }), 'Recorded'); };
      row.append(a);
    } else {
      const f = el('button', 'primary', 'Send to approvers');
      f.onclick = async () => done(await post('/packets/' + p.id + '/forward', {}), 'Forwarded');
      row.append(f);
    }
    const e = el('button', '', 'Edit');
    e.onclick = () => {
      const ta = el('textarea'); ta.value = JSON.stringify(c.value, null, 2);
      const save = el('button', 'primary', 'Save edit');
      save.onclick = async () => {
        let v; try { v = JSON.parse(ta.value); } catch (err) { msg.textContent = 'Not valid JSON'; return; }
        done(await post('/packets/' + p.id + '/decide', { decision: 'edit',
          payload_hash: p.payload_hash, approved_via: 'button', value: v }), 'Edited — review the new packet');
      };
      box.insertBefore(ta, msg); box.insertBefore(save, msg); e.disabled = true;
    };
    const rj = el('button', '', 'Reject');
    rj.onclick = async () => done(await post('/packets/' + p.id + '/decide',
      { decision: 'reject', payload_hash: p.payload_hash, approved_via: 'button',
        reason: prompt('Why is this wrong? (optional)') || '' }), 'Rejected');
    row.append(e, rj);
  } else if (c.status === 'failed') {
    const r = el('button', '', 'Retry');
    r.onclick = async () => done(await post('/claims/' + c.id + '/retry', {}), 'Queued');
    row.append(r);
  } else if (c.status === 'approved') {
    row.append(el('span', 'meta', 'Approved — waiting for the org service.'));
  }
  box.append(row, msg);
  return box;
}
async function loadForwarded() {
  const host = document.getElementById('forwarded'); host.textContent = '';
  const d = await (await fetch('/records/forwarded')).json();
  const items = d.packets || [];
  if (!items.length) return;
  host.append(el('h2', '', 'Waiting for your approval'));
  items.forEach(p => {
    const box = el('div', 'card');
    const pl = p.payload || {};
    box.append(el('div', 'kind', pl.kind + ' · proposed by a teammate'));
    box.append(el('div', 'value', (pl.subject_label || pl.subject_ref) + ' · '
      + pl.predicate + ' · ' + JSON.stringify(pl.value)));
    box.append(el('div', 'meta', (pl.evidence || []).length + ' evidence pointer(s) · packet '
      + p.payload_hash.slice(0, 8)));
    const msg = el('div', 'msg');
    const a = el('button', 'primary', 'Approve');
    a.onclick = async () => { a.disabled = true;
      const r = await post('/records/forwarded/' + p.packet_id + '/approve',
        { payload_hash: p.payload_hash, approved_via: 'button' });
      msg.textContent = r.ok ? 'Recorded' : (r.d.code || 'error');
      if (r.ok) setTimeout(load, 700); };
    const row = el('div', 'row'); row.append(a); box.append(row, msg);
    host.append(box);
  });
}
function joinForm(host) {
  host.append(el('h2', '', 'Join your org'));
  const url = el('input'); url.placeholder = 'Org service URL'; url.style.width = '100%';
  const code = el('input'); code.placeholder = 'Invite code'; code.style.width = '100%';
  const b = el('button', 'primary', 'Join'); const msg = el('div', 'msg');
  b.onclick = async () => { const r = await post('/records/join',
    { service_url: url.value.trim(), invite_code: code.value.trim() });
    msg.textContent = r.ok ? 'Joined' : (r.d.code || 'error') + (r.d.detail ? ': ' + r.d.detail : '');
    if (r.ok) setTimeout(load, 500); };
  const row = el('div', 'row'); row.append(b);
  host.append(url, el('div', '', ''), code, row, msg);
}
async function load() {
  const [m, q, ret] = await Promise.all([
    fetch('/records/membership').then(r => r.json()),
    fetch('/claims').then(r => r.json()),
    fetch('/retention/status').then(r => r.json()).catch(() => ({})) ]);
  scopes = m.scopes || [];
  const bar = document.getElementById('bar'); bar.textContent = '';
  bar.append(el('span', '', m.joined ? 'Org member' : 'Not in an org'));
  if (ret && ret.policy) bar.append(el('span', '', 'Capture kept '
    + ret.policy.capture_ttl_days + ' days · expiry ' + ret.mode));
  const join = document.getElementById('join'); join.textContent = '';
  if (!m.joined) joinForm(join); else loadForwarded();
  const host = document.getElementById('list'); host.textContent = '';
  const groups = q.groups || {};
  const keys = Object.keys(groups);
  if (!keys.length) { host.append(el('div', 'empty',
    'Nothing to review. Claims appear here as meetings and notes are processed.')); return; }
  keys.sort((a, b) => (a === 'personal') - (b === 'personal')).forEach(sk => {
    const scope = scopes.find(s => s.id === sk);
    host.append(el('h2', '', sk === 'personal' ? 'Not yet scoped' : (scope ? scope.name : sk)));
    Object.entries(groups[sk]).forEach(([subj, items]) => {
      host.append(el('h3', '', items[0].subject_label || subj));
      items.forEach(c => host.append(card(c)));
    });
  });
}
load();
</script>
</body></html>""")
