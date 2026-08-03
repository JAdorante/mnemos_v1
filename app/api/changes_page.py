"""Memory changes page — contradiction surfacing (Track B).

When the write-time adjudicator decides new evidence UPDATES an existing
fact ("meeting moved to 3pm" supersedes "at 2pm"), the swap is automatic but
it should never be invisible: this page lists recent supersede decisions as
old → new pairs, each with a Restore button that reverses the call. Trust
feature, not a review queue — nothing here blocks the pipeline.

Renders from JSON endpoints; dynamic text lands via textContent only.
"""

CHANGES_PAGE = """<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Memory changes</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 15px/1.5 system-ui, sans-serif; max-width: 680px;
         margin: 40px auto; padding: 0 16px; }
  h1 { font-size: 20px; } h1 + p { color: gray; }
  .pair { border: 1px solid color-mix(in srgb, gray 40%, transparent);
          border-radius: 8px; padding: 12px 14px; margin: 12px 0; }
  .old { text-decoration: line-through; color: gray; }
  .new { font-weight: 600; margin-top: 4px; }
  .meta { font-size: 12px; color: gray; margin-top: 6px; }
  button { font: inherit; font-size: 13px; padding: 6px 14px;
           border-radius: 6px; cursor: pointer; margin-top: 8px; }
  .empty { color: gray; margin-top: 24px; }
</style>
<h1>Memory changes</h1>
<p>When newer evidence replaced an older fact, the swap happened automatically —
here is every recent one, reversible. Restore if the old version was right.</p>
<div id="list"></div>
<script>
async function load() {
  const d = await (await fetch('/memory/supersessions')).json();
  const host = document.getElementById('list');
  host.textContent = '';
  const items = d.supersessions || [];
  if (!items.length) {
    const e = document.createElement('div'); e.className = 'empty';
    e.textContent = 'No replacements on record — nothing has been overwritten.';
    host.append(e); return;
  }
  items.forEach(it => {
    const card = document.createElement('div'); card.className = 'pair';
    const o = document.createElement('div'); o.className = 'old';
    o.textContent = it.old_text || '(no text)';
    const n = document.createElement('div'); n.className = 'new';
    n.textContent = it.new_text || '(no text)';
    const m = document.createElement('div'); m.className = 'meta';
    m.textContent = (it.kind || 'fact') + ' · replaced '
      + new Date((it.when || 0) * 1000).toLocaleString();
    const b = document.createElement('button');
    b.textContent = 'Restore the old version';
    b.addEventListener('click', async () => {
      b.disabled = true;
      const r = await fetch('/memory/supersessions/revert', { method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ old_id: it.old_id }) });
      b.textContent = r.ok ? 'Restored ✓' : 'Restore failed';
      if (r.ok) setTimeout(load, 600);
    });
    card.append(o, n, m, b);
    host.append(card);
  });
}
load();
</script>
"""
