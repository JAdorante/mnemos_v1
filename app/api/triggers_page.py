"""Standing-triggers management page (self-serve, never a dotfile).

Lists every trigger with its stats and one-click pause/resume/retire/adopt;
points at chat for authoring. Renders entirely from the JSON endpoints; all
dynamic text lands via textContent so stored trigger names/goals are never
re-interpreted as markup.
"""

from app.api.mnemos_theme import apply_plain as _plain

TRIGGERS_PAGE = _plain("""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Triggers — @@BRAND@@</title>
@@FONTS@@
<style>
@@ROOT@@
body { font: 15px/1.5 var(--font); color: var(--text); max-width: 760px;
       margin: 40px auto; padding: 0 16px; background: var(--paper); }
h1 { font-size: 20px; } h1 + p { color: var(--mut); }
h2 { font-size: 15px; margin: 22px 0 6px; }
.hint { border: 1px dashed color-mix(in srgb, var(--mut) 45%, transparent);
        border-radius: 8px; padding: 10px 14px; color: var(--mut);
        font-size: 13px; }
.card { border: 1px solid color-mix(in srgb, var(--mut) 40%, transparent);
        border-radius: 8px; padding: 10px 14px; margin: 10px 0; }
.card .name { font-weight: 600; }
.card .when { font-size: 13px; color: var(--mut); margin-top: 2px; }
.card .stats { font-size: 12px; color: var(--mut); margin-top: 6px; }
.card button { font: inherit; font-size: 13px; padding: 4px 12px;
               border-radius: 6px; cursor: pointer; margin: 8px 6px 0 0; }
#status { color: var(--mut); font-size: 13px; margin-top: 18px; }
.empty { color: var(--mut); font-size: 13px; }
</style></head><body>
<h1>Triggers</h1>
<p>Standing “when it sees X, offer Y” watches. Everything here only ever
<em>offers</em> — irreversible steps still stop for your approval.</p>
<div class="hint">Create one by chatting, e.g. “<b>whenever I make progress on
the thesis, offer to email Dr.&nbsp;Reyes an update</b>” — you'll get a draft
with a 7-day backtest to approve before it's saved.</div>
<div id="sections"></div>
<div id="status"></div>
<script>
const VERB = a => {
  if (!a) return '';
  if (a.verb === 'run_goal') return a.goal || 'run the saved action';
  if (a.verb === 'set_status') return 'mark it ' + (a.status || 'done');
  return a.note || 'give you a heads-up';
};
function card(t, actions) {
  const d = document.createElement('div'); d.className = 'card';
  const name = document.createElement('div'); name.className = 'name';
  name.textContent = t.name; d.append(name);
  const when = document.createElement('div'); when.className = 'when';
  const cond = Object.entries(t.condition || {})
    .map(([k, v]) => k + '=' + (Array.isArray(v) ? v.join('/') : v)).join(', ');
  when.textContent = 'WHEN ' + t.signal + (cond ? ' (' + cond + ')' : '')
    + '  →  ' + VERB(t.action);
  d.append(when);
  const s = t.stats || {};
  const st = document.createElement('div'); st.className = 'stats';
  st.textContent = (t.origin || 'custom') + ' · fired ' + (s.fires || 0)
    + ' · offered ' + (s.offers || 0) + ' · accepted ' + (s.accepts || 0)
    + ' · dismissed ' + (s.dismisses || 0);
  d.append(st);
  for (const [label, status] of actions) {
    const b = document.createElement('button'); b.textContent = label;
    b.onclick = async () => {
      await fetch('/triggers/' + t.id + '/status?status=' + status,
                  {method: 'POST'});
      refresh();
    };
    d.append(b);
  }
  return d;
}
function section(root, title, rows, actions, emptyText) {
  const h = document.createElement('h2'); h.textContent = title;
  root.append(h);
  if (!rows.length) {
    const e = document.createElement('div'); e.className = 'empty';
    e.textContent = emptyText; root.append(e); return;
  }
  rows.forEach(t => root.append(card(t, actions)));
}
async function refresh() {
  const data = await (await fetch('/triggers/list')).json();
  const rows = data.triggers || [];
  const by = s => rows.filter(t => t.status === s);
  const root = document.getElementById('sections');
  root.replaceChildren();
  section(root, 'Active', by('active'),
          [['Pause', 'paused'], ['Retire', 'retired']],
          'None yet — author one in chat.');
  section(root, 'Suggested (patterns I noticed)', by('suggested'),
          [['Adopt', 'active'], ['Dismiss', 'retired']],
          'Nothing suggested right now.');
  section(root, 'Paused', by('paused'),
          [['Resume', 'active'], ['Retire', 'retired']],
          'Nothing paused.');
  const retired = by('retired').length;
  const st = document.getElementById('status');
  const last = (data.last && data.last.reason) || 'not yet run';
  st.textContent = 'Engine: ' + (data.enabled ? 'on' : 'off')
    + ' · last pass: ' + last
    + ' · calm-budget slots left today: ' + (data.daily_remaining ?? '—')
    + (retired ? (' · ' + retired + ' retired') : '');
}
refresh();
</script>
</body></html>""")
