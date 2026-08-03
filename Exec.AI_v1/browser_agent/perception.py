"""Perception (FR-PERC-1): extract the page's interactive elements from the
accessibility/ARIA layer of the DOM, assign each a stable integer element_id,
and render an indexed list as text for the model. The model acts by
element_id, never by raw selector.

We scan the DOM for interactive/ARIA-role elements (the same model a screen
reader uses) rather than Playwright's raw AX snapshot, because the indexed-
element approach maps cleanly back to a clickable locator.
"""
import hashlib

# Returns {url, title, count, truncated, elements:[{id,role,name,tag,editable,value?}]}.
# Old data-agent-id attributes are cleared first so reused ids can't collide.
SCAN_JS = """
(MAX) => {
  document.querySelectorAll('[data-agent-id]').forEach(e => e.removeAttribute('data-agent-id'));
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    const s = window.getComputedStyle(el);
    if (s.visibility === 'hidden' || s.display === 'none') return false;
    if (parseFloat(s.opacity || '1') === 0) return false;
    return true;
  };
  const accName = (el) => {
    let n = el.getAttribute('aria-label') || '';
    if (!n) {
      const lb = el.getAttribute('aria-labelledby');
      if (lb) n = lb.split(/\\s+/).map(id => {
        const e = document.getElementById(id); return e ? e.innerText : '';
      }).join(' ');
    }
    if (!n) n = el.getAttribute('placeholder') || '';
    if (!n) n = el.getAttribute('alt') || '';
    if (!n) n = el.getAttribute('title') || '';
    if (!n && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) n = el.value || '';
    if (!n) n = (el.innerText || el.textContent || '');
    n = (n || '').replace(/\\s+/g, ' ').trim();
    if (n.length > 120) n = n.slice(0, 117) + '...';
    return n;
  };
  const roleOf = (el) => {
    const r = el.getAttribute('role');
    if (r) return r;
    const t = el.tagName.toLowerCase();
    if (t === 'a') return 'link';
    if (t === 'button') return 'button';
    if (t === 'select') return 'combobox';
    if (t === 'textarea') return 'textbox';
    if (t === 'input') return (el.getAttribute('type') || 'text').toLowerCase();
    return t;
  };
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea,' +
    '[role=button], [role=link], [role=checkbox], [role=radio], [role=tab],' +
    '[role=menuitem], [role=combobox], [role=switch], [contenteditable=""],' +
    '[contenteditable="true"], summary';
  const els = Array.from(document.querySelectorAll(sel));
  const out = [];
  let i = 0;
  for (const el of els) {
    if (out.length >= MAX) break;
    if (!isVisible(el)) continue;
    el.setAttribute('data-agent-id', String(i));
    const tag = el.tagName.toLowerCase();
    const editable = (tag === 'input' || tag === 'textarea' || el.isContentEditable);
    const item = { id: i, role: roleOf(el), name: accName(el), tag: tag, editable: editable };
    if (editable) {
      // Never expose a password's plaintext (autofill puts the real value here);
      // report only whether it is filled, so it stays out of the model + logs.
      item.value = (el.type === 'password')
        ? (el.value ? '••••••' : '')
        : (el.value || '').slice(0, 80);
    }
    out.push(item);
    i++;
  }
  const docH = document.documentElement.scrollHeight || document.body.scrollHeight || 0;
  return {
    url: location.href, title: document.title,
    count: out.length, truncated: out.length >= MAX, elements: out,
    scrollY: Math.round(window.scrollY),
    scrollMax: Math.round(Math.max(0, docH - window.innerHeight))
  };
}
"""

# Returns the text of a tagged element, or the page main content if id is null.
READ_JS = """
(id) => {
  if (id !== null && id !== undefined) {
    const el = document.querySelector('[data-agent-id="' + id + '"]');
    return el ? (el.innerText || el.textContent || '').trim() : null;
  }
  const main = document.querySelector('main') || document.body;
  return (main ? (main.innerText || '') : '').trim();
}
"""


def signature(scan: dict) -> dict:
    """A cheap before/after fingerprint the Haiku verifier compares (FR-ACT-3)."""
    # include editable values so typing into a field registers as a real change
    # (otherwise a filled input keeps the same signature and verify false-fails).
    parts = []
    for e in scan.get("elements", []):
        v = e.get("value", "")
        parts.append(e.get("name", "") + (f"={v}" if v else ""))
    names = "|".join(parts)
    h = hashlib.sha1(names.encode("utf-8", "ignore")).hexdigest()[:12]
    return {
        "url": scan.get("url"),
        "title": scan.get("title"),
        "count": scan.get("count"),
        "content_hash": h,
        "scrollY": scan.get("scrollY", 0),   # so a real scroll registers as change
    }


def render_observation(scan: dict) -> str:
    """The indexed element list the executor reasons over."""
    suffix = "+ (truncated)" if scan.get("truncated") else ""
    sy, smax = scan.get("scrollY", 0), scan.get("scrollMax", 0)
    at_bottom = "  (at bottom — scrolling further will do nothing)" if sy >= smax - 4 else ""
    lines = [
        f"URL: {scan.get('url')}",
        f"Title: {scan.get('title')}",
        f"Scroll: {sy}/{smax}px{at_bottom}",
        f"Interactive elements ({scan.get('count')}{suffix}):",
    ]
    for e in scan.get("elements", []):
        val = f' value="{e["value"]}"' if e.get("value") else ""
        lines.append(f"[{e['id']}] {e['role']}: {e['name']}{val}")
    if not scan.get("elements"):
        lines.append("(no interactive elements detected)")
    return "\n".join(lines)
