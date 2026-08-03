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
  // Topmost open modal/dialog, if any. Its elements are listed FIRST so a busy
  // page can never truncate them away, and everything outside it is occlusion-
  // checked — a popup that blocks the page must be dealt with before the page.
  const modals = Array.from(document.querySelectorAll(
    'dialog[open], [aria-modal="true"], [role="dialog"], [role="alertdialog"]'
  )).filter(isVisible);
  const modalRoot = modals.length ? modals[modals.length - 1] : null;
  const isCovered = (el) => {
    const r = el.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx < 0 || cy < 0 || cx >= window.innerWidth || cy >= window.innerHeight) return false;
    const hit = document.elementFromPoint(cx, cy);
    if (!hit) return false;
    return !(el.contains(hit) || hit.contains(el));
  };
  let els = Array.from(document.querySelectorAll(sel));
  if (modalRoot) {
    els = Array.from(modalRoot.querySelectorAll(sel))
      .concat(els.filter(e => !modalRoot.contains(e)));
  }
  const out = [];
  let i = 0;
  for (const el of els) {
    if (out.length >= MAX) break;
    if (!isVisible(el)) continue;
    el.setAttribute('data-agent-id', String(i));
    const tag = el.tagName.toLowerCase();
    const editable = (tag === 'input' || tag === 'textarea' || el.isContentEditable);
    const item = { id: i, role: roleOf(el), name: accName(el), tag: tag, editable: editable };
    if (modalRoot && modalRoot.contains(el)) item.dialog = true;
    else if (isCovered(el)) item.covered = true;
    if (editable) {
      // Never expose a password's plaintext (autofill puts the real value here);
      // report only whether it is filled, so it stays out of the model + logs.
      item.value = (el.type === 'password')
        ? (el.value ? '••••••' : '')
        : (el.value || '').slice(0, 80);
    }
    // Checkable state (checkbox/radio/switch): toggling one changes no text, so
    // without this a successful click looks like "no effect" and verify fails.
    if (item.role === 'checkbox' || item.role === 'radio' || item.role === 'switch') {
      item.checked = (typeof el.checked === 'boolean')
        ? el.checked : (el.getAttribute('aria-checked') === 'true');
    }
    // A native <select> isn't "editable" but its current option is real state.
    if (tag === 'select') item.value = (el.value || '').slice(0, 80);
    // Selection / current-item cues — SPA chat rows often toggle aria-selected
    // without changing their accessible name.
    if (el.getAttribute('aria-selected') === 'true'
        || el.getAttribute('aria-current') === 'true'
        || el.getAttribute('aria-current') === 'page') {
      item.selected = true;
    }
    out.push(item);
    i++;
  }
  // Visible page text (chat bubbles, articles) — not in the interactive list.
  // Prefer log/list/main regions; fall back to body. Cap so observations stay cheap.
  const PAGE_CAP = 2500;
  const textRoots = [];
  for (const s of ['[role="log"]', '[role="list"]', 'main', '[role="main"]',
                   'article', '[data-testid*="message"]', '[class*="message"]']) {
    try {
      document.querySelectorAll(s).forEach(el => { if (isVisible(el)) textRoots.push(el); });
    } catch (e) {}
  }
  let pageRoot = textRoots.length ? textRoots[textRoots.length - 1]
    : (document.querySelector('main') || document.body);
  let pageText = '';
  let pageTruncated = false;
  if (pageRoot) {
    pageText = (pageRoot.innerText || pageRoot.textContent || '')
      .replace(/\\s+/g, ' ').trim();
    if (pageText.length > PAGE_CAP) {
      // Keep the TAIL: chat logs / feeds append at the bottom, so the newest
      // (most relevant) text is at the end. Truncating the head, not the tail,
      // was silently hiding the latest messages.
      pageText = '…' + pageText.slice(-(PAGE_CAP - 1));
      pageTruncated = true;
    }
  }
  // Structural chat cues — lets the agent recognize an unlisted chat app
  // (self-hosted Mattermost, new SPA) without a CHAT_HOSTS entry.
  const hasLog = !!document.querySelector('[role="log"], [aria-live="polite"] [class*="message"]');
  let composers = 0;
  for (const e of out) {
    if (e.editable && (e.role === 'textbox' || e.tag === 'textarea'
        || e.tag === 'div')) composers++;
  }
  const listRows = document.querySelectorAll(
    '[role="listitem"], [role="option"], [role="row"], li[class*="conversation"],' +
    'li[class*="chat"]').length;
  let selectedName = '';
  const cur = document.querySelector(
    '[aria-current="page"], [aria-current="true"], [aria-selected="true"]');
  if (cur && isVisible(cur)) {
    selectedName = accName(cur).slice(0, 120);
  }
  const docH = document.documentElement.scrollHeight || document.body.scrollHeight || 0;
  return {
    url: location.href, title: document.title,
    count: out.length, truncated: out.length >= MAX, elements: out,
    modal: modalRoot ? (accName(modalRoot) || 'dialog') : null,
    scrollY: Math.round(window.scrollY),
    scrollMax: Math.round(Math.max(0, docH - window.innerHeight)),
    page_text: pageText,
    page_truncated: pageTruncated,
    selected: selectedName,
    chat_signals: { has_log: hasLog, composers: composers, list_rows: listRows },
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
    """A cheap before/after fingerprint the Haiku verifier compares (FR-ACT-3).

    Includes interactive chrome *and* SPA-relevant state: URL, selection,
    compose values, and a hash of visible page text so opening a chat thread
    or receiving messages registers as real change.
    """
    parts = []
    compose_parts = []
    selected_parts = []
    for e in scan.get("elements", []):
        v = e.get("value", "")
        part = e.get("name", "") + (f"={v}" if v else "")
        if "checked" in e:
            part += "[x]" if e.get("checked") else "[ ]"
        if e.get("selected"):
            part += "[sel]"
            selected_parts.append(e.get("name") or "")
        parts.append(part)
        if e.get("editable") and v:
            compose_parts.append(f"{e.get('name')}={v}")
    names = "|".join(parts)
    h = hashlib.sha1(names.encode("utf-8", "ignore")).hexdigest()[:12]
    page = (scan.get("page_text") or "")[:2000]
    page_hash = hashlib.sha1(page.encode("utf-8", "ignore")).hexdigest()[:12]
    selected = (scan.get("selected") or "") or ",".join(selected_parts[:4])
    return {
        "url": scan.get("url"),
        "title": scan.get("title"),
        "count": scan.get("count"),
        "content_hash": h,
        "page_hash": page_hash,
        "selected": selected[:160],
        "compose": "|".join(compose_parts)[:200],
        "scrollY": scan.get("scrollY", 0),
    }


def render_observation(scan: dict) -> str:
    """The indexed element list the executor reasons over, plus visible text."""
    suffix = "+ (truncated)" if scan.get("truncated") else ""
    sy, smax = scan.get("scrollY", 0), scan.get("scrollMax", 0)
    at_bottom = "  (at bottom — scrolling further will do nothing)" if sy >= smax - 4 else ""
    lines = [
        f"URL: {scan.get('url')}",
        f"Title: {scan.get('title')}",
        f"Scroll: {sy}/{smax}px{at_bottom}",
    ]
    if scan.get("selected"):
        lines.append(f'Selected/current: "{scan["selected"]}"')
    els = scan.get("elements", [])
    covered_n = sum(1 for e in els if e.get("covered"))
    if scan.get("modal"):
        lines.append(
            f'POPUP/DIALOG OPEN: "{scan["modal"]}" — elements marked [dialog] '
            "belong to it. Deal with the dialog first (usually dismiss it); "
            "elements marked (covered) are blocked until it closes.")
    elif covered_n >= 5 and covered_n > len(els) // 3:
        lines.append(
            "An overlay/popup appears to cover the page: elements marked "
            "(covered) cannot be clicked. Use the uncovered elements — likely "
            "the popup's own buttons — to dismiss it first.")
    lines.append(f"Interactive elements ({scan.get('count')}{suffix}):")
    for e in els:
        val = f' value="{e["value"]}"' if e.get("value") else ""
        chk = (" [checked]" if e.get("checked") else " [unchecked]") if "checked" in e else ""
        sel = " [selected]" if e.get("selected") else ""
        mod = " [dialog]" if e.get("dialog") else (" (covered)" if e.get("covered") else "")
        lines.append(f"[{e['id']}] {e['role']}: {e['name']}{val}{chk}{sel}{mod}")
    if not scan.get("elements"):
        lines.append("(no interactive elements detected)")
    page_text = (scan.get("page_text") or "").strip()
    if page_text:
        # Cap in the prompt so chat threads don't drown the element list.
        # Keep the TAIL — in chat logs the newest messages are at the end.
        truncated = bool(scan.get("page_truncated")) or len(page_text) > 1800
        snippet = page_text if len(page_text) <= 1800 else "…" + page_text[-1799:]
        header = "Visible page text (not interactive — use `read` for more):"
        if truncated:
            header = ("Visible page text (TRUNCATED — older text above is cut; "
                      "this is the most recent portion; use `read` for more):")
        lines.append(header)
        lines.append(snippet)
    return "\n".join(lines)
