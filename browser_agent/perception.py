"""Perception (FR-PERC-1): extract the page's interactive elements from the
accessibility/ARIA layer of the DOM, assign each a stable integer element_id,
and render an indexed list as text for the model. The model acts by
element_id, never by raw selector.

We scan the DOM for interactive/ARIA-role elements (the same model a screen
reader uses) rather than Playwright's raw AX snapshot, because the indexed-
element approach maps cleanly back to a clickable locator.
"""
import hashlib

from .surfaces import pixel_surface

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
  // JS-wired pages (games, editors, kanbans) often draw plain divs and attach
  // clicks with addEventListener — invisible to any selector. When the
  // semantic scan is this sparse the page may still be fully interactive, so
  // sweep for visible pointer-cursor elements (the CSS tell of a click
  // target) and list them as 'clickable'. Outermost only: cursor inherits, so
  // a pointer child of a pointer parent is the same target.
  const semanticCount = out.length;
  const SPARSE_AT = 8, SWEEP_CAP = 3000;
  if (semanticCount < SPARSE_AT && document.body) {
    const ptr = (el) => {
      try {
        return (el.ownerDocument.defaultView || window)
          .getComputedStyle(el).cursor === 'pointer';
      } catch (e) { return false; }
    };
    const nodes = Array.from(document.body.querySelectorAll('*')).slice(0, SWEEP_CAP);
    for (const el of nodes) {
      if (out.length >= MAX) break;
      if (el.closest('[data-agent-id]')) continue;       // self or listed ancestor
      if (!ptr(el)) continue;
      if (el.parentElement && ptr(el.parentElement)) continue;
      if (el.querySelector('[data-agent-id]')) continue; // wraps a listed element
      if (!isVisible(el)) continue;
      el.setAttribute('data-agent-id', String(i));
      let nm = accName(el);
      if (!nm) {
        const cls = (typeof el.className === 'string' && el.className.trim())
          ? '.' + el.className.trim().split(/\\s+/).slice(0, 2).join('.') : '';
        nm = el.tagName.toLowerCase() + cls;
      }
      const item = { id: i, role: 'clickable', name: nm,
                     tag: el.tagName.toLowerCase(), editable: false };
      // Selection state from the class list: JS-wired UIs mark the picked
      // card/row with a 'selected'-style class, not aria-selected. Without
      // this a select-then-place flow reads as a no-op click and spirals.
      if (typeof el.className === 'string'
          && /(?:^|\\s)(?:selected|active|current|chosen)(?:\\s|$)/.test(el.className)) {
        item.selected = true;
      }
      out.push(item);
      i++;
    }
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
  // Opaque surfaces (FR-PERC pixel fallback): regions whose contents are pixels,
  // not nodes — <canvas> games/editors/maps, <video>, plugin embeds, and
  // role=application widgets. Nothing inside them can ever be an element_id, so
  // a page dominated by one is only actionable through coordinates. Reported
  // in CSS pixels, clipped to the viewport; the Python side decides dominance.
  const OPAQUE_SEL = 'canvas, video, embed, object, [role=application]';
  const vw = window.innerWidth, vh = window.innerHeight;
  const surfaces = [];
  const collect = (root, dx, dy) => {
    for (const el of Array.from(root.querySelectorAll(OPAQUE_SEL))) {
      const r = el.getBoundingClientRect();
      if (r.width < 1 || r.height < 1) continue;
      // isVisible() is bound to this window; an element in a frame's document
      // must be measured through that document's own view.
      const cs = (el.ownerDocument.defaultView || window).getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      if (parseFloat(cs.opacity || '1') === 0) continue;
      const x = Math.max(0, Math.round(r.left + dx));
      const y = Math.max(0, Math.round(r.top + dy));
      const w = Math.min(vw, Math.round(r.right + dx)) - x;
      const h = Math.min(vh, Math.round(r.bottom + dy)) - y;
      // Decorative sparklines / spinner canvases aren't surfaces to act on.
      if (w < 120 || h < 120) continue;
      surfaces.push({
        kind: el.tagName.toLowerCase(),
        x: x, y: y, w: w, h: h,
        label: (el.getAttribute('aria-label') || el.getAttribute('title')
                || el.getAttribute('id') || '').slice(0, 60),
        // Interactive descendants mean the DOM CAN describe it (an <object>
        // that is really an inlined document) — those keep element_ids.
        inner: el.querySelectorAll(sel).length,
      });
    }
  };
  collect(document, 0, 0);
  // Same-origin frames: querySelectorAll never crosses a frame boundary, so an
  // embedded game (the usual way these are served) would otherwise be invisible
  // to us. Mouse coordinates are page-wide, so a surface found inside a frame
  // is actionable once its rect is offset by the frame's position.
  // A frame we CANNOT read (cross-origin: Google's search-page games, arcade
  // embeds) is itself an opaque surface — nothing inside it can ever get an
  // element_id, which is exactly the pixel fallback's contract. Small
  // cross-origin frames (ads, payment fields) are reported too but never
  // become the pixel target: pixel_surface() requires viewport dominance
  // (>=25%), which an embedded checkout field can't reach.
  const frameSurface = (f, fr) => {
    const x = Math.max(0, Math.round(fr.left));
    const y = Math.max(0, Math.round(fr.top));
    const w = Math.min(vw, Math.round(fr.right)) - x;
    const h = Math.min(vh, Math.round(fr.bottom)) - y;
    if (w < 120 || h < 120) return;
    let host = '';
    try { host = new URL(f.src || '', location.href).host; } catch (e) {}
    surfaces.push({ kind: 'iframe', x: x, y: y, w: w, h: h,
                    label: (f.getAttribute('aria-label') || f.title
                            || host || '').slice(0, 60),
                    inner: 0 });
  };
  for (const f of Array.from(document.querySelectorAll('iframe, frame'))) {
    try {
      const fr = f.getBoundingClientRect();
      if (fr.width < 120 || fr.height < 120) continue;
      const cs = getComputedStyle(f);
      if (cs.visibility === 'hidden' || cs.display === 'none') continue;
      let doc = null;
      try { doc = f.contentDocument; } catch (e) { /* cross-origin */ }
      if (!doc) { frameSurface(f, fr); continue; }
      collect(doc, fr.left, fr.top);
    } catch (e) {}
  }
  // A DOM-drawn board is as opaque to element_ids as a canvas when its clicks
  // live in JS listeners: the pointer sweep above finds the pieces, but drop
  // zones and click-wired containers expose no cursor cue at all. If the
  // SEMANTIC scan found almost nothing yet the body renders plenty of nodes,
  // report the visible content's bounding box so the coordinate fallback
  // works alongside the swept clickables. inner is 0 by definition — this
  // branch only exists because the DOM does not describe the page.
  if (semanticCount < SPARSE_AT && document.body) {
    let n = 0, x0 = vw, y0 = vh, x1 = 0, y1 = 0;
    for (const el of Array.from(document.body.querySelectorAll('*')).slice(0, SWEEP_CAP)) {
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) continue;
      if (!isVisible(el)) continue;
      n++;
      x0 = Math.min(x0, r.left); y0 = Math.min(y0, r.top);
      x1 = Math.max(x1, r.right); y1 = Math.max(y1, r.bottom);
    }
    if (n >= 30) {
      const x = Math.max(0, Math.round(x0)), y = Math.max(0, Math.round(y0));
      const w = Math.min(vw, Math.round(x1)) - x, h = Math.min(vh, Math.round(y1)) - y;
      if (w >= 120 && h >= 120) {
        surfaces.push({ kind: 'dom', x: x, y: y, w: w, h: h,
                        label: 'page content', inner: 0 });
      }
    }
  }
  surfaces.sort((a, b) => (b.w * b.h) - (a.w * a.h));
  const docH = document.documentElement.scrollHeight || document.body.scrollHeight || 0;
  return {
    url: location.href, title: document.title,
    surfaces: surfaces.slice(0, 4),
    viewport: { w: vw, h: vh },
    dpr: window.devicePixelRatio || 1,
    count: out.length, truncated: out.length >= MAX, elements: out,
    semantic_count: semanticCount,
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


def scan_delta(prev: dict | None, cur: dict | None, *, cap: int = 8) -> str:
    """What changed between two scans, as a short prompt block — or "".

    The executor otherwise has to infer "what did my last action do" by
    comparing two long element lists in its head; computing the diff in Python
    is cheap and much easier to act on. Elements are compared by (role, name)
    multisets so per-page ids don't produce false churn.
    """
    if not prev or not cur:
        return ""
    lines = []
    p_url, c_url = prev.get("url") or "", cur.get("url") or ""
    if p_url != c_url:
        lines.append(f"URL changed: {p_url} -> {c_url}")
    if (prev.get("title") or "") != (cur.get("title") or ""):
        lines.append(f'Title changed to "{cur.get("title")}"')
    if (prev.get("modal") or None) != (cur.get("modal") or None):
        lines.append(f'A dialog opened: "{cur["modal"]}"' if cur.get("modal")
                     else "The dialog closed")

    def _keys(scan):
        from collections import Counter
        return Counter((e.get("role", ""), e.get("name", ""))
                       for e in scan.get("elements", []))

    pk, ck = _keys(prev), _keys(cur)
    appeared = list((ck - pk).elements())
    gone = list((pk - ck).elements())
    if appeared:
        shown = ", ".join(f"{r}: {n}" for r, n in appeared[:cap])
        more = f" (+{len(appeared) - cap} more)" if len(appeared) > cap else ""
        lines.append(f"NEW elements: {shown}{more}")
    if gone:
        shown = ", ".join(f"{r}: {n}" for r, n in gone[:cap])
        more = f" (+{len(gone) - cap} more)" if len(gone) > cap else ""
        lines.append(f"GONE elements: {shown}{more}")
    # Selection moves (a picked card/row/tab): the classic select-then-place
    # flow changes nothing except which element is marked selected.
    def _sel(scan):
        return {(e.get("role", ""), e.get("name", ""))
                for e in scan.get("elements", []) if e.get("selected")}

    ps, cs = _sel(prev), _sel(cur)
    if ps != cs:
        now = ", ".join(n for _r, n in sorted(cs - ps)) or "(none)"
        lines.append(f"Selection changed: now selected — {now}")
    # Value edits on fields both scans know (compose boxes filling/clearing).
    p_vals = {(e.get("role"), e.get("name")): e.get("value", "")
              for e in prev.get("elements", []) if e.get("editable")}
    for e in cur.get("elements", []):
        if not e.get("editable"):
            continue
        k = (e.get("role"), e.get("name"))
        if k in p_vals and p_vals[k] != e.get("value", ""):
            v = (e.get("value") or "")[:60]
            lines.append(f'"{e.get("name")}" value is now "{v}"' if v
                         else f'"{e.get("name")}" was cleared')
    if not lines and prev.get("pixel_hash") and cur.get("pixel_hash") \
            and prev["pixel_hash"] != cur["pixel_hash"]:
        lines.append("No element changes, but the rendered graphics changed.")
    if not lines:
        return ""
    return "CHANGES SINCE YOUR LAST ACTION:\n" + "\n".join(f"- {ln}" for ln in lines[:10]) + "\n\n"


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
    sig = {
        "url": scan.get("url"),
        "title": scan.get("title"),
        "count": scan.get("count"),
        "content_hash": h,
        "page_hash": page_hash,
        "selected": selected[:160],
        "compose": "|".join(compose_parts)[:200],
        "scrollY": scan.get("scrollY", 0),
    }
    # On a canvas/graphics page the DOM never moves — dealing a card or dragging
    # one changes pixels only. Without this the verifier sees an identical
    # signature after every move and reports "no effect". Present only when the
    # driver captured one (a dominant opaque surface), so ordinary pages are
    # byte-for-byte unchanged.
    if scan.get("pixel_hash"):
        sig["pixel_hash"] = scan["pixel_hash"]
    return sig


def _view_area(scan: dict) -> int:
    vp = scan.get("viewport") or {}
    return int(vp.get("w") or 1280) * int(vp.get("h") or 800)


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
    surf = pixel_surface(scan)
    if surf and surf["kind"] == "dom":
        pct = int(round(100 * (surf["w"] * surf["h"]) / max(1, _view_area(scan))))
        lines.append(
            f"JS-driven surface: this page wires its clicks in scripts, so the "
            f"list below (~{pct}% of the view) is likely incomplete — targets "
            "with no cursor cue (drop zones, empty slots) have no element_id "
            "at all. When the pixel actions (click_at / drag / press_key) are "
            "offered this turn, use them for anything not listed, measured on "
            "the attached screenshot; listed elements still work by element_id.")
    elif surf:
        pct = int(round(100 * (surf["w"] * surf["h"]) / max(1, _view_area(scan))))
        label = f' "{surf["label"]}"' if surf.get("label") else ""
        lines.append(
            f"Graphics surface: <{surf['kind']}>{label} covering ~{pct}% of the "
            "view. Its contents are pixels, not elements — nothing drawn inside "
            "it can appear in the list below. When the pixel actions "
            "(click_at / drag / press_key) are offered this turn, act on it "
            "with those, measured on the attached screenshot.")
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
