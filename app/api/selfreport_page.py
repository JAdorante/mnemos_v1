"""Weekly self-report page — the subjective half of the Phase 0 harness.

Three 1..5 questions (cognitive load, trust, interruptions) plus a free note,
asked once a week. Self-serve web form (never a dotfile), rendering entirely
from JSON endpoints; all dynamic text lands via textContent so nothing the
user typed is ever re-interpreted as markup.
"""

from app.api.mnemos_theme import apply_plain as _plain

SELFREPORT_PAGE = _plain("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Weekly check-in — @@BRAND@@</title>
@@FONTS@@
<style>
@@ROOT@@
body { font: 15px/1.5 var(--font); color: var(--text); max-width: 560px;
       margin: 40px auto; padding: 0 16px; background: var(--paper); }
h1 { font-size: 20px; } h1 + p { color: var(--mut); }
fieldset { border: 1px solid color-mix(in srgb, var(--mut) 40%, transparent);
           border-radius: 8px; margin: 14px 0; padding: 10px 14px; }
legend { font-weight: 600; font-size: 14px; }
.scale { display: flex; gap: 6px; margin-top: 6px; }
.scale label { flex: 1; text-align: center; padding: 8px 0;
               border: 1px solid color-mix(in srgb, var(--mut) 40%, transparent);
               border-radius: 6px; cursor: pointer; user-select: none; }
.scale input { display: none; }
.scale input:checked + span { font-weight: 700; }
.scale label:has(input:checked) { border-color: currentColor; }
.ends { display: flex; justify-content: space-between; font-size: 12px;
        color: var(--mut); margin-top: 4px; }
textarea { width: 100%; min-height: 70px; margin-top: 6px;
           font: inherit; border-radius: 6px; padding: 8px; }
button { font: inherit; padding: 10px 22px; border-radius: 8px;
         cursor: pointer; margin-top: 10px; }
#msg { margin-top: 12px; font-weight: 600; }
table { border-collapse: collapse; margin-top: 26px; width: 100%; }
td, th { padding: 4px 10px 4px 0; text-align: left; font-size: 13px; }
th { color: var(--mut); font-weight: 600; }
</style></head><body>
<h1>Weekly check-in</h1>
<p id="due"></p>
<form id="f">
  <fieldset><legend>Did Sparrow lighten your mental load this week?</legend>
    <div class="scale" data-name="load"></div>
    <div class="ends"><span>made it heavier</span><span>much lighter</span></div>
  </fieldset>
  <fieldset><legend>Did you trust what it remembered and surfaced?</legend>
    <div class="scale" data-name="trust"></div>
    <div class="ends"><span>not at all</span><span>completely</span></div>
  </fieldset>
  <fieldset><legend>When it spoke up, was it welcome?</legend>
    <div class="scale" data-name="interrupt"></div>
    <div class="ends"><span>annoying</span><span>always welcome</span></div>
  </fieldset>
  <fieldset><legend>Anything it should have caught, or shouldn't have said?</legend>
    <textarea id="note" placeholder="optional"></textarea>
  </fieldset>
  <button type="submit">Save this week's check-in</button>
  <div id="msg"></div>
</form>
<table id="hist" hidden>
  <thead><tr><th>When</th><th>Load</th><th>Trust</th><th>Interrupts</th><th>Note</th></tr></thead>
  <tbody></tbody>
</table>
<script>
document.querySelectorAll('.scale').forEach(el => {
  const name = el.dataset.name;
  for (let i = 1; i <= 5; i++) {
    const lab = document.createElement('label');
    const inp = document.createElement('input');
    inp.type = 'radio'; inp.name = name; inp.value = i;
    const sp = document.createElement('span'); sp.textContent = i;
    lab.append(inp, sp); el.append(lab);
  }
});
async function refresh() {
  const s = await (await fetch('/selfreport/status')).json();
  document.getElementById('due').textContent = s.due
    ? 'A check-in is due — 60 seconds, three taps.'
    : 'Last check-in ' + Math.round(s.days_since) + ' day(s) ago. You can still update this week.';
  const hist = await (await fetch('/selfreport/list')).json();
  const rows = hist.reports || [];
  const tb = document.querySelector('#hist tbody');
  tb.textContent = '';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const d = new Date(r.ts * 1000).toLocaleDateString();
    [d, r.load_score, r.trust_score, r.interrupt_score, r.note || ''].forEach(v => {
      const td = document.createElement('td'); td.textContent = v == null ? '—' : v;
      tr.append(td);
    });
    tb.append(tr);
  });
  document.getElementById('hist').hidden = !rows.length;
}
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  const pick = n => { const c = document.querySelector('input[name=' + n + ']:checked');
                      return c ? parseInt(c.value, 10) : null; };
  const body = { load: pick('load'), trust: pick('trust'),
                 interruptions: pick('interrupt'),
                 note: document.getElementById('note').value.trim() || null };
  const r = await fetch('/selfreport', { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  document.getElementById('msg').textContent =
    r.ok ? 'Saved — thank you. See you next week.' : 'Save failed: ' + r.status;
  if (r.ok) refresh();
});
refresh();
</script>
</body></html>""")
