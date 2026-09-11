"""Shared Sparrow UI behaviors — Seal, Bleed, Constellation, Ambient, persistence.

Injected into pages via @@UI_JS@@ (see mnemos_theme.apply).
"""

UI_JS = r"""
<script>
/* Sparrow shared UI — instrument, not chatbot chrome */
window.MnemosMemory = {
  ns: 'mnemos.ui.',
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(this.ns + key);
      if (raw == null) return fallback;
      return JSON.parse(raw);
    } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(this.ns + key, JSON.stringify(value)); } catch (e) {}
  },
  clear() {
    try {
      Object.keys(localStorage).filter(k => k.startsWith(this.ns))
        .forEach(k => localStorage.removeItem(k));
    } catch (e) {}
  }
};

/* Plan 6.4 — attach double-submit CSRF header on state-changing fetches. */
(function () {
  function csrfFromCookie() {
    try {
      const m = document.cookie.match(/(?:^|; )quill_csrf=([^;]*)/);
      return m ? decodeURIComponent(m[1]) : '';
    } catch (e) { return ''; }
  }
  const _fetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    init = init ? Object.assign({}, init) : {};
    const method = String(init.method || 'GET').toUpperCase();
    if (method !== 'GET' && method !== 'HEAD' && method !== 'OPTIONS') {
      const headers = new Headers(init.headers || {});
      if (!headers.has('X-CSRF-Token') && !headers.has('x-csrf-token')) {
        const tok = csrfFromCookie();
        if (tok) headers.set('X-CSRF-Token', tok);
      }
      init.headers = headers;
    }
    return _fetch(input, init);
  };
})();

/* Fetch JSON with human errors. Behind a tunnel/proxy a restarting server
   answers with an HTML error page; raw resp.json() then surfaces
   "JSON.parse: unexpected character…" to the user. Throws Error whose
   .message is safe to render. */
window.MnemosJson = async function (url, init) {
  let resp;
  try {
    resp = await fetch(url, init);
  } catch (e) {
    throw new Error('Can’t reach the server — check your connection and retry.');
  }
  const text = await resp.text();
  let data = null;
  try { data = JSON.parse(text); } catch (e) {}
  if (!resp.ok) {
    const detail = data && (typeof data.detail === 'string' ? data.detail : data.error);
    if (resp.status === 401) {
      throw new Error(detail || 'Session locked — unlock at /auth and retry.');
    }
    throw new Error(detail || ('Server unavailable (HTTP ' + resp.status
      + ') — it may be restarting. Retry in a moment.'));
  }
  if (data === null) {
    throw new Error('Server sent an unexpected reply — it may be restarting. Retry in a moment.');
  }
  return data;
};

window.MnemosReduceMotion = () =>
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Chrome height → --chrome-h so fixed layers never guess header size.
   Also measures the in-flow composer and corner dock so they cannot cover each other. */
window.MnemosChrome = (function () {
  let _timer = null;
  let _ro = null;
  function _px(n) { return Math.max(0, Math.round(n || 0)) + 'px'; }
  function sync() {
    const top = document.querySelector('header.top, .top');
    let h = top ? top.offsetHeight : 0;
    const ap = document.getElementById('mnemosApproval');
    if (ap && ap.classList.contains('on')) h += ap.offsetHeight;
    document.documentElement.style.setProperty('--chrome-h', _px(h));
    const composer = document.querySelector('body > .dock');
    const ch = composer ? composer.getBoundingClientRect().height : 0;
    document.documentElement.style.setProperty('--composer-h', _px(ch));
    const dock = document.getElementById('mnemosDockBR');
    const dh = (dock && dock.children.length) ? dock.getBoundingClientRect().height : 0;
    document.documentElement.style.setProperty('--dock-clear', _px(dh ? dh + 16 : 0));
    if (_ro && dock && !dock._chromeObserved) {
      dock._chromeObserved = true;
      _ro.observe(dock);
    }
    if (_ro && composer && !composer._chromeObserved) {
      composer._chromeObserved = true;
      _ro.observe(composer);
    }
  }
  function debounced() {
    clearTimeout(_timer);
    _timer = setTimeout(sync, 100);
  }
  function bind() {
    sync();
    if (typeof ResizeObserver !== 'undefined') {
      _ro = new ResizeObserver(debounced);
      const top = document.querySelector('header.top, .top');
      if (top) _ro.observe(top);
      const ap = document.getElementById('mnemosApproval');
      if (ap) _ro.observe(ap);
      const composer = document.querySelector('body > .dock');
      if (composer) { composer._chromeObserved = true; _ro.observe(composer); }
      const dock = document.getElementById('mnemosDockBR');
      if (dock) { dock._chromeObserved = true; _ro.observe(dock); }
    }
    window.addEventListener('resize', debounced);
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bind);
  } else {
    bind();
  }
  return { sync, bind };
})();

/* One owner per viewport corner — floating widgets register here. */
window.MnemosDock = {
  PRIORITY: { ghost: 10, toast: 20, system: 30 },
  ensure() {
    let dock = document.getElementById('mnemosDockBR');
    if (!dock) {
      dock = document.createElement('div');
      dock.id = 'mnemosDockBR';
      document.body.appendChild(dock);
    }
    return dock;
  },
  add(el, priority) {
    if (!el) return null;
    const dock = this.ensure();
    el.dataset.dockPriority = String(priority == null ? 50 : priority);
    el.style.position = 'relative';
    el.style.right = 'auto';
    el.style.bottom = 'auto';
    el.style.left = 'auto';
    el.style.zIndex = 'auto';
    if (el.parentNode !== dock) dock.appendChild(el);
    this._sort();
    return dock;
  },
  _sort() {
    const dock = document.getElementById('mnemosDockBR');
    if (!dock) return;
    const kids = Array.from(dock.children);
    kids.sort((a, b) => (+a.dataset.dockPriority || 0) - (+b.dataset.dockPriority || 0));
    kids.forEach((k) => dock.appendChild(k));
    try { window.MnemosChrome && MnemosChrome.sync(); } catch (e) {}
  },
};

/* Shared hold primitive — Seal + Bleed. Copper progress only; no pulse.
   HOLD_MS 700; early release (≥150ms) teaches once via server-persisted tip. */
window.MnemosHold = {
  HOLD_MS: 700,
  TEACH_MS: 150,
  _tipSeen: null,
  _live: null,
  async tipSeen() {
    if (this._tipSeen != null) return this._tipSeen;
    try {
      const j = await (await fetch('/ui/hold-tip')).json();
      this._tipSeen = !!j.seen;
    } catch (e) { this._tipSeen = false; }
    return this._tipSeen;
  },
  async dismissTip() {
    this._tipSeen = true;
    try {
      await fetch('/ui/hold-tip', { method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({seen: true}) });
    } catch (e) {}
  },
  announce(msg) {
    if (!this._live) {
      this._live = document.createElement('div');
      this._live.setAttribute('aria-live', 'polite');
      this._live.setAttribute('aria-atomic', 'true');
      this._live.className = 'mnemos-hold-live';
      this._live.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)';
      document.body.appendChild(this._live);
    }
    this._live.textContent = msg || '';
  },
  showTeach(el, text) {
    if (!el) return;
    let tip = document.getElementById('mnemosHoldTip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'mnemosHoldTip';
      tip.className = 'mnemos-hold-tip';
      document.body.appendChild(tip);
    }
    tip.textContent = text || 'Hold to see where this came from';
    const r = el.getBoundingClientRect();
    tip.style.left = Math.min(window.innerWidth - 260, Math.max(8, r.left)) + 'px';
    tip.style.top = Math.min(window.innerHeight - 48, r.bottom + 8) + 'px';
    tip.hidden = false;
    clearTimeout(tip._hide);
    tip._hide = setTimeout(() => { tip.hidden = true; }, 3200);
    this.dismissTip();
  },
  /**
   * @param {HTMLElement} el
   * @param {{onComplete:Function, onCancel?:Function, ms?:number,
   *          teach?:string, fill?:'ring'|'spine', clickFallback?:Function}} opts
   */
  bind(el, opts) {
    if (!el || el._holdBound) return;
    el._holdBound = true;
    opts = opts || {};
    const ms = opts.ms || this.HOLD_MS;
    const fill = opts.fill || 'ring';
    const reduce = window.MnemosReduceMotion();
    el.classList.add('holdable');
    el.setAttribute('tabindex', el.getAttribute('tabindex') || '0');
    if (!el.getAttribute('role')) el.setAttribute('role', 'button');

    let ring = el.querySelector('.hold-ring');
    if (fill === 'ring' && !ring) {
      el.insertAdjacentHTML('beforeend',
        '<svg class="hold-ring" viewBox="0 0 24 24" aria-hidden="true">'
        + '<circle cx="12" cy="12" r="9" pathLength="100"/></svg>');
      ring = el.querySelector('.hold-ring');
    }
    if (fill === 'spine') el.classList.add('hold-spine');

    let timer = null, armed = false, t0 = 0, raf = 0;
    const setProg = (p) => {
      el.style.setProperty('--hold-p', String(Math.max(0, Math.min(1, p))));
      if (ring) {
        const c = ring.querySelector('circle');
        if (c) c.style.strokeDashoffset = String(100 - p * 100);
      }
    };
    const clear = (reverse) => {
      armed = false;
      el.classList.remove('holding', 'sealed');
      if (timer) { clearTimeout(timer); timer = null; }
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      if (reverse && !reduce) {
        const start = parseFloat(el.style.getPropertyValue('--hold-p') || '0');
        const tStart = performance.now();
        const tick = (now) => {
          const u = Math.min(1, (now - tStart) / 180);
          setProg(start * (1 - u));
          if (u < 1) raf = requestAnimationFrame(tick);
          else setProg(0);
        };
        raf = requestAnimationFrame(tick);
      } else setProg(0);
    };
    const finish = () => {
      el.classList.add('sealed');
      setProg(1);
      this.announce('Complete');
      el.classList.add('hold-flash');
      setTimeout(() => el.classList.remove('hold-flash'), reduce ? 0 : 220);
      setTimeout(() => {
        clear(false);
        opts.onComplete && opts.onComplete(el);
      }, reduce ? 0 : 200);
    };
    const start = (ev) => {
      if (el.disabled || el.getAttribute('aria-disabled') === 'true') return;
      if (ev && ev.type === 'keydown' && ev.key !== 'Enter' && ev.key !== ' ') return;
      if (ev && ev.type === 'keydown') ev.preventDefault();
      if (ev && ev.button != null && ev.button !== 0) return;
      if (ev && ev.preventDefault) ev.preventDefault();
      if (ev && ev.pointerId != null && el.setPointerCapture) {
        try { el.setPointerCapture(ev.pointerId); } catch (e) {}
      }
      if (reduce) {
        setProg(0.5);
        setTimeout(() => finish(), 40);
        return;
      }
      armed = true; t0 = performance.now();
      el.classList.add('holding');
      this.announce('Holding');
      const tick = (now) => {
        if (!armed) return;
        const p = Math.min(1, (now - t0) / ms);
        setProg(p);
        if (p < 1) raf = requestAnimationFrame(tick);
      };
      raf = requestAnimationFrame(tick);
      timer = setTimeout(() => { if (armed) finish(); }, ms);
    };
    const end = async (ev) => {
      if (!armed) return;
      const held = performance.now() - t0;
      if (el.classList.contains('sealed')) return;
      clear(true);
      this.announce('Cancelled');
      opts.onCancel && opts.onCancel(el);
      if (held >= this.TEACH_MS && opts.teach !== false) {
        const seen = await this.tipSeen();
        if (!seen) this.showTeach(el, opts.teach || 'Hold to see where this came from');
      }
    };
    el.addEventListener('pointerdown', start);
    el.addEventListener('pointerup', end);
    el.addEventListener('pointercancel', end);
    el.addEventListener('lostpointercapture', end);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') {
        if (e.repeat) return;
        start(e);
      }
    });
    el.addEventListener('keyup', (e) => {
      if (e.key === 'Enter' || e.key === ' ') end(e);
    });
    // Click-only equivalent via overflow / data-hold-click
    const fb = opts.clickFallback || el.querySelector('[data-hold-click]');
    if (fb && fb !== el) {
      fb.addEventListener('click', (e) => {
        e.preventDefault(); e.stopPropagation();
        opts.onComplete && opts.onComplete(el);
      });
    }
  }
};

window.MnemosSeal = {
  HOLD_MS: 700,
  bind(btn, { onApprove, onCancel } = {}) {
    if (!btn || btn._sealBound) return;
    btn._sealBound = true;
    btn.classList.add('seal-btn');
    window.MnemosHold.bind(btn, {
      ms: this.HOLD_MS,
      fill: 'ring',
      teach: 'Hold to seal this approval',
      onComplete: () => {
        if (window.MnemosMemory.get('sound', false)) {
          try { window.MnemosInkSound && window.MnemosInkSound(); } catch (e) {}
        }
        onApprove && onApprove();
      },
      onCancel: onCancel,
    });
  }
};

window.MnemosInkSound = function () {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) return;
    const ctx = new Ctx();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = 'sine'; o.frequency.value = 180;
    g.gain.value = 0.0001;
    o.connect(g); g.connect(ctx.destination);
    const t = ctx.currentTime;
    g.gain.exponentialRampToValueAtTime(0.02, t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, t + 0.18);
    o.start(t); o.stop(t + 0.2);
    setTimeout(() => ctx.close(), 300);
  } catch (e) {}
};

window.MnemosBleed = {
  HOLD_MS: 700,
  bind(el, onReveal) {
    if (!el || el._bleedBound) return;
    el._bleedBound = true;
    // Overflow click-only equivalent
    if (!el.querySelector('[data-hold-click]')) {
      const more = document.createElement('button');
      more.type = 'button';
      more.className = 'hold-more';
      more.setAttribute('data-hold-click', '1');
      more.setAttribute('aria-label', 'Show provenance');
      more.title = 'Show provenance';
      more.textContent = '⋯';
      el.appendChild(more);
    }
    window.MnemosHold.bind(el, {
      ms: this.HOLD_MS,
      fill: 'spine',
      teach: 'Hold to see where this came from',
      onComplete: () => { onReveal && onReveal(el); },
    });
  },
  renderStack(container, steps) {
    if (!container) return;
    const html = ['<div class="provenance-stack">'];
    (steps || []).forEach((s, i) => {
      html.push('<div class="pv-step" style="animation-delay:' + (i * 0.04) + 's">');
      html.push('<div class="pv-dot"></div><div>');
      html.push('<div class="pv-label">' + (s.label || '') + '</div>');
      html.push('<div class="pv-body">' + (s.html || esc(s.body || '—')) + '</div>');
      html.push('</div></div>');
    });
    html.push('</div>');
    container.innerHTML = html.join('');
  }
};

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
window.MnemosEsc = esc;

window.MnemosRender = window.MnemosRender || {};
window.MnemosRender.empty = function (message, opts) {
  opts = opts || {};
  const cls = opts.className || 'empty-state';
  let html = '<div class="' + cls + '">' + esc(message || '');
  const link = opts.link;
  if (link && link.href) {
    html += ' <a href="' + esc(link.href) + '">' + esc(link.label || 'Learn more') + '</a>';
  }
  html += '</div>';
  return html;
};

window.MnemosDialog = (function () {
  const FOCUSABLE = 'button:not([disabled]), a[href], input:not([disabled]):not([type="hidden"]), '
    + 'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  let active = null;
  let returnFocus = null;
  let escapeFn = null;
  let prevOverflow = null;

  function focusables(root) {
    return Array.from(root.querySelectorAll(FOCUSABLE)).filter((el) => {
      if (el.disabled || el.getAttribute('aria-hidden') === 'true') return false;
      const st = window.getComputedStyle(el);
      return st.visibility !== 'hidden' && st.display !== 'none';
    });
  }

  function lockScroll(on) {
    const root = document.documentElement;
    if (on) {
      if (prevOverflow == null) prevOverflow = root.style.overflow || '';
      root.style.overflow = 'hidden';
    } else if (prevOverflow != null) {
      root.style.overflow = prevOverflow;
      prevOverflow = null;
    }
  }

  function onKeyDown(e) {
    if (!active) return;
    if (e.key === 'Escape' && escapeFn) {
      e.preventDefault();
      escapeFn(e);
      return;
    }
    if (e.key !== 'Tab') return;
    const items = focusables(active);
    if (!items.length) {
      e.preventDefault();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    const focused = document.activeElement;
    if (e.shiftKey) {
      if (focused === first || !active.contains(focused)) {
        e.preventDefault();
        last.focus();
      }
    } else if (focused === last || !active.contains(focused)) {
      e.preventDefault();
      first.focus();
    }
  }

  document.addEventListener('keydown', onKeyDown);

  const api = {
    focusables,
    isOpen(root) {
      return active === root;
    },
    open(root, opts) {
      if (!root) return;
      opts = opts || {};
      if (active && active !== root) {
        api.close(active, { restoreFocus: false });
      }
      active = root;
      returnFocus = opts.returnFocus || document.activeElement;
      escapeFn = opts.onEscape || null;
      if (opts.lockScroll) lockScroll(true);
      if (opts.markOpen !== false) root.classList.add('open');
      root.setAttribute('aria-hidden', 'false');
      let target = null;
      if (opts.focus) {
        target = typeof opts.focus === 'string' ? root.querySelector(opts.focus) : opts.focus;
      }
      if (!target) {
        const items = focusables(root);
        target = items[0];
      }
      if (target && target.focus) {
        try { target.focus(); } catch (err) {}
      }
    },
    close(root, opts) {
      if (!root) return;
      opts = opts || {};
      if (active === root) {
        active = null;
        escapeFn = null;
        lockScroll(false);
      }
      root.classList.remove('open');
      root.setAttribute('aria-hidden', 'true');
      if (opts.restoreFocus !== false) {
        const ret = returnFocus;
        returnFocus = null;
        if (ret && ret.focus) {
          try { ret.focus(); } catch (err) {}
        }
      } else {
        returnFocus = null;
      }
    },
  };
  return api;
})();
window.MnemosLayer = window.MnemosDialog;

window.MnemosAmbient = {
  render(el, notes, opts) {
    if (!el) return;
    opts = opts || {};
    if (!notes || !notes.length) {
      el.innerHTML = '<p class="ambient-note">Quiet for now — listening.</p>';
      return;
    }
    el.innerHTML = notes.map((n, i) => {
      const refs = (n.refs || []).join(',');
      const action = n.action || null;
      const clickable = !!(action || (n.refs && n.refs.length));
      const cls = 'ambient-note'
        + (n.attention ? ' attention' : '')
        + (clickable ? ' actionable' : '');
      let html = '<' + (clickable ? 'button type="button"' : 'p')
        + ' class="' + cls + '" data-ai="' + i + '"'
        + (refs ? ' data-refs="' + esc(refs) + '"' : '')
        + '>';
      html += '<span class="ambient-text">' + esc(n.text) + '</span>';
      if (action && action.label) {
        html += '<span class="ambient-act">' + esc(action.label) + '</span>';
      }
      html += clickable ? '</button>' : '</p>';
      return html;
    }).join('');
    // Stash notes for click handlers
    el._ambientNotes = notes;
    el._ambientOpts = opts;
    if (!el._ambientBound) {
      el._ambientBound = true;
      el.addEventListener('mouseover', (e) => {
        const t = e.target.closest('[data-refs]');
        if (!t) return;
        const refs = (t.getAttribute('data-refs') || '').split(',').filter(Boolean);
        const ctl = (el._ambientOpts || {}).constellation;
        if (ctl && ctl.softHighlight) ctl.softHighlight(refs);
      });
      el.addEventListener('mouseout', (e) => {
        if (e.relatedTarget && el.contains(e.relatedTarget)) return;
        const ctl = (el._ambientOpts || {}).constellation;
        if (ctl && ctl.softHighlight) ctl.softHighlight([]);
      });
      el.addEventListener('click', (e) => {
        const t = e.target.closest('[data-ai]');
        if (!t) return;
        const idx = parseInt(t.getAttribute('data-ai'), 10);
        const notes2 = el._ambientNotes || [];
        const n = notes2[idx];
        if (!n) return;
        const opts2 = el._ambientOpts || {};
        const ctl = opts2.constellation;
        const action = n.action || {};
        if (action.route) {
          window.location.href = action.route;
          return;
        }
        if (ctl && action.command === 'constellation.emphasize' && n.refs) {
          ctl.emphasize(n.refs);
          return;
        }
        if (ctl && action.command === 'constellation.compare' && n.refs) {
          ctl.emphasize(n.refs);
          if (n.refs[0]) ctl.openEvidence && ctl.openEvidence(n.refs[0]);
          return;
        }
        if (ctl && n.refs && n.refs.length) ctl.emphasize(n.refs);
        if (opts2.onAction) opts2.onAction(n);
      });
    }
  }
};

window.MnemosConstellation = {
  mount(canvas, data, opts) {
    if (!canvas) return null;
    const ctx = canvas.getContext('2d');
    const mode = (opts && opts.mode) || 'full';
    const isThumb = mode === 'thumbnail';
    const thumbHref = (opts && opts.href) || '/memory?mode=constellation';
    const state = {
      nodes: (data && data.nodes) || [],
      edges: (data && data.edges) || [],
      insights: (data && data.insights) || [],
      breakdowns: (data && data.breakdowns) || {},
      diffMode: false,
      diff: null,          // /field/diff payload when mode on
      ghosts: [],          // left_focus ids rendered faint for this session
      cam: { x: 0, y: 0, z: 1 },
      hover: null,
      selected: null,
      focusId: null,
      linkFrom: null,
      edit: false,
      showFilaments: false,
      loopsOnly: false,       // field filtered to systems with unfinished work
      softIds: null,       // margin hover soft-highlight set
      emphasizeIds: null,  // margin click emphasis
      raf: 0,
      t0: performance.now(),
      onSelect: opts && opts.onSelect,
      onChange: opts && opts.onChange,
      detailMode: !!(opts && opts.detailMode),
      rangeCutoff: null,
      persistKey: isThumb ? null
        : (((opts && opts.persistKey) || 'constellation.cam') + '.v8'),
      mode: mode,
      _fittedOnce: false,
    };
    const wrap = canvas.parentElement;
    let toolbar = null, panel = null, tip = null, insightEl = null, legendEl = null;
    if (!isThumb) {
      toolbar = wrap && wrap.querySelector('.const-tools');
      if (wrap && !toolbar) {
        toolbar = document.createElement('div');
        toolbar.className = 'const-tools';
        // The constellation is the hero: two controls stay out, everything
        // graph-shaped moves behind the overflow.
        toolbar.innerHTML =
          '<button type="button" data-act="loops" title="Only what has unfinished work">Open loops</button>'
          + '<button type="button" data-act="fit" title="Recenter the field">Recenter</button>'
          + '<details class="const-more"><summary title="More">•••</summary>'
          + '<div class="const-more-menu">'
          + '<button type="button" data-act="focus">Focus mode (F)</button>'
          + '<button type="button" data-act="filaments">Show every link</button>'
          + '<button type="button" data-act="correct">Correct connections</button>'
          + '<button type="button" data-act="diff">Since yesterday</button>'
          + '<button type="button" data-act="in">Zoom in</button>'
          + '<button type="button" data-act="out">Zoom out</button>'
          + '</div></details>';
        wrap.appendChild(toolbar);
      }
      panel = wrap && wrap.querySelector('.const-edit');
      if (wrap && !panel) {
        panel = document.createElement('div');
        panel.className = 'const-edit';
        panel.hidden = true;
        wrap.appendChild(panel);
      }
      tip = wrap && wrap.querySelector('.const-tip');
      if (wrap && !tip) {
        tip = document.createElement('div');
        tip.className = 'const-tip';
        tip.hidden = true;
        wrap.appendChild(tip);
      }
      insightEl = wrap && wrap.querySelector('.const-insight');
      if (wrap && !insightEl) {
        insightEl = document.createElement('div');
        insightEl.className = 'const-insight';
        wrap.appendChild(insightEl);
      }
      legendEl = wrap && wrap.querySelector('.const-legend');
      if (opts.legend === false) legendEl = null;
      else if (wrap && !legendEl) {
        legendEl = document.createElement('div');
        legendEl.className = 'const-legend';
        // Self-contained styles so the key looks identical on the console + home
        // pages without touching two CSS blocks. Non-interactive (never eats a drag).
        legendEl.style.cssText = 'position:absolute;left:10px;top:10px;z-index:var(--z-base);'
          + 'display:flex;flex-wrap:wrap;gap:3px 10px;max-width:min(360px,72%);'
          + 'padding:7px 11px;border-radius:14px;'
          + 'background:var(--chrome-bg,rgba(22,22,27,.94));'
          + 'backdrop-filter:var(--glass);-webkit-backdrop-filter:var(--glass);'
          + 'border:1px solid rgba(232,231,244,.12);'
          + 'box-shadow:inset 0 1px 0 rgba(233,231,226,.13),'
          + '0 1px 2px rgba(0,0,0,.36),0 14px 34px -16px rgba(0,0,0,.6);'
          + 'font:11px ui-sans-serif,system-ui,sans-serif;color:rgba(233,231,226,.8);'
          + 'pointer-events:none';
        wrap.appendChild(legendEl);
      }
    }
    const REDUCED_MOTION = !!(window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    // Three hues (constellation mockup): violet = people, amber = topics &
    // entities (shape still tells kinds apart), green = you. Shared by glyphs
    // + legend so they can't drift.
    const SELF_RGB = '116,194,148';
    const KIND_RGB = {
      person: '141,133,242',    // violet
      org: '223,179,94',        // amber family below — kinds keep their shapes
      project: '223,179,94',
      tool: '223,179,94',
      place: '223,179,94',
      task: '223,179,94',
      commitment: '223,179,94',
      idea: '223,179,94',
    };
    function kindColor(kind, alpha) {
      return 'rgba(' + (KIND_RGB[kind] || KIND_RGB.idea) + ',' + alpha + ')';
    }

    /* --- The attention field ------------------------------------------------
       The field answers "what deserves attention right now", not "how does
       Ravenry store this". Three ideas carry that:

         systems  — a primary (you, a person, a project, an org) plus the work
                    and context that hangs off it. One object at rest; it opens
                    on demand. Progressive disclosure, not a full graph dump.
         zones    — importance becomes distance. self at the centre, then NOW,
                    ACTIVE, PERIPHERAL, DORMANT. Never drawn as rings: the
                    gravity well and node opacity are what say where a band is.
         tiers    — primaries are large and always named; everything else is one
                    quiet satellite mark; evidence is never a star at all.

       The backend keeps every distinction it has. The field just doesn't give
       them equal visual weight. */
    const PRIMARY_KINDS = { person: 1, project: 1, org: 1 };
    const LOOP_KINDS = { task: 1, commitment: 1 };
    const ZONE_RING = { self: 0, now: 0.27, active: 0.53, peripheral: 0.79, dormant: 0.95 };
    const ZONE_DIM = { self: 1, now: 1, active: 0.8, peripheral: 0.32, dormant: 0.15 };
    // Things you should notice, could notice, and everything else — the field
    // is a compression, and this is where the ratio is set.
    const NOW_MIN = 3, NOW_MAX = 5;
    const SAT_RGB = '146,152,163';

    function isPrimary(n) { return !!(n && PRIMARY_KINDS[n.kind || 'idea']); }
    function isLoop(n) { return !!(n && LOOP_KINDS[n.kind || 'idea']); }

    /* Every satellite attaches to the primary it is most strongly tied to —
       edge weight × confidence, with the primary's own gravity breaking ties.
       A task that belongs to a project stops being a sixth star and becomes
       part of that project's system. */
    function buildSystems(st) {
      const prim = new Set();
      let selfId = null;
      st.nodes.forEach(n => {
        n._host = null; n._members = null; n._loops = 0;
        if (n.is_self) { selfId = n.id; prim.add(n.id); }
        else if (isPrimary(n)) prim.add(n.id);
      });
      st.nodes.forEach(n => {
        if (prim.has(n.id)) return;
        let best = null, bestW = 0;
        (st.edges || []).forEach(e => {
          let other = null;
          if (e.source === n.id) other = e.target;
          else if (e.target === n.id) other = e.source;
          if (!other || !prim.has(other)) return;
          // "You" holds your own promises, but any real project or person
          // claims the work first — otherwise the whole field collapses into
          // one star at the centre.
          if (other === selfId && !isLoop(n)) return;
          const host = st.byId[other];
          const w = ((e.weight || 1) * (e.confidence != null ? e.confidence : 0.6)
            + (host ? (host.gravity || 0) * 0.4 : 0))
            * (other === selfId ? 0.7 : 1);
          if (w > bestW) { bestW = w; best = other; }
        });
        n._host = best;
      });
      st.nodes.forEach(n => { if (prim.has(n.id)) n._members = []; });
      st.nodes.forEach(n => {
        const h = n._host ? st.byId[n._host] : null;
        if (!h || !h._members) { n._host = null; return; }
        h._members.push(n.id);
        if (isLoop(n)) h._loops++;
      });
    }

    /* Zones come off the server's own ranking: focus vs periphery is its call,
       and gravity order splits focus into the handful that define NOW. */
    function assignZones(st) {
      const prims = st.nodes.filter(n => isPrimary(n) && !n.is_self)
        .sort((a, b) => (b.gravity || 0) - (a.gravity || 0)
          || (a.id < b.id ? -1 : 1));
      const focused = prims.filter(n => n.layer === 'focus');
      const nowN = Math.min(NOW_MAX, Math.max(NOW_MIN, Math.round(focused.length * 0.45)));
      let taken = 0;
      prims.forEach(n => {
        if (n.layer === 'focus' && taken < nowN) { n._zone = 'now'; taken++; return; }
        if (n.layer === 'focus') { n._zone = 'active'; return; }
        const strength = n.memory_strength != null ? n.memory_strength : 0.5;
        n._zone = (strength < 0.35 || (n.gravity || 0) < 0.22) ? 'dormant' : 'peripheral';
      });
      st.nodes.forEach(n => {
        if (n.is_self) { n._zone = 'self'; return; }
        if (isPrimary(n)) return;
        if (n._host) { n._zone = (st.byId[n._host] || {})._zone || 'active'; return; }
        // A homeless open loop is exactly the thing that must stay findable.
        n._zone = isLoop(n) ? 'peripheral' : 'dormant';
      });
    }

    function refield(st) {
      buildSystems(st);
      assignZones(st);
      layout(st);
    }

    function sizeFor(n) {
      const g = Math.max(0.15, Math.min(1.15, n.gravity || 0.35));
      if (n.is_self) return 8;
      if (!isPrimary(n)) return 3.6 + g * 2.2;
      if (n._zone === 'now') return 12.5 + g * 6;
      if (n._zone === 'active') return 9.5 + g * 4;
      if (n._zone === 'peripheral') return 6.5 + g * 2.5;
      return 4.5 + g * 1.5;
    }

    /* A system is open while it, or one of its satellites, is the active
       object — hover, selection or focus. Correct mode and "show all links"
       open everything, because both are about the structure itself. */
    function systemOpen(st, host) {
      if (!host || !host._members || !host._members.length) return false;
      if (st.showFilaments || st.edit) return true;
      const act = st.focusId || st.selected || st.hover;
      if (!act) return false;
      if (act === host.id) return true;
      const a = st.byId[act];
      return !!(a && a._host === host.id);
    }

    function nodeVisible(st, n) {
      if (!n) return false;
      if (n.is_self || isPrimary(n)) return true;
      if (!n._host) {
        return isLoop(n) || st.showFilaments || st.edit
          || st.hover === n.id || st.selected === n.id || st.focusId === n.id;
      }
      return systemOpen(st, st.byId[n._host]);
    }

    function zoneAlpha(st, n) {
      if (n.is_self) return 0.95;
      const base = ZONE_DIM[n._zone || 'peripheral'] || 0.3;
      const ms = n.memory_strength != null ? (0.65 + n.memory_strength * 0.35) : 1;
      const tier = isPrimary(n) ? 1 : (isLoop(n) ? 0.92 : 0.68);
      return Math.max(0.05, Math.min(1, base * ms * tier));
    }

    /* Illuminate one constellation at a time: the active object, whatever it
       links to, and the system it belongs to. Everything else falls away. */
    function lensSet(st, id) {
      const set = new Set([id]);
      const n = st.byId[id];
      if (!n) return set;
      (st.edges || []).forEach(e => {
        if (e.source === id) set.add(e.target);
        else if (e.target === id) set.add(e.source);
      });
      const host = n._host ? st.byId[n._host] : n;
      if (host) {
        set.add(host.id);
        (host._members || []).forEach(m => set.add(m));
      }
      return set;
    }

    function loopish(st, n) {
      if (isLoop(n)) return true;
      if ((n._loops || 0) > 0) return true;
      const h = n._host ? st.byId[n._host] : null;
      return !!(h && (h._loops || 0) > 0);
    }
    // Legend key: glyph swatch (matches drawKind) + label, per node kind present.
    // Three entries, because there are three tiers. Anything finer belongs in
    // the inspector, not in a key the user has to memorise.
    const LEGEND = [
      ['You', 'circle', 'rgba(' + SELF_RGB + ',.95)'],
      ['People', 'circle', kindColor('person', .95)],
      ['Projects & orgs', 'diamond', kindColor('project', .92)],
      ['Open loops', 'dot', kindColor('task', .95)],
    ];
    function legendSwatch(shape, color) {
      const base = 'display:inline-block;vertical-align:middle;';
      if (shape === 'circle') return '<i style="' + base + 'width:9px;height:9px;border-radius:50%;background:' + color + '"></i>';
      if (shape === 'diamond') return '<i style="' + base + 'width:8px;height:8px;background:' + color + ';transform:rotate(45deg)"></i>';
      if (shape === 'hex') return '<i style="' + base + 'width:9px;height:8px;background:' + color + ';clip-path:polygon(25% 0%,75% 0%,100% 50%,75% 100%,25% 100%,0% 50%)"></i>';
      if (shape === 'round') return '<i style="' + base + 'width:9px;height:9px;border-radius:2px;background:' + color + '"></i>';
      if (shape === 'triUp') return '<i style="' + base + 'width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:9px solid ' + color + '"></i>';
      if (shape === 'triRight') return '<i style="' + base + 'width:0;height:0;border-top:5px solid transparent;border-bottom:5px solid transparent;border-left:9px solid ' + color + '"></i>';
      return '<i style="' + base + 'width:6px;height:6px;border-radius:50%;background:' + color + '"></i>';
    }
    function renderLegend() {
      if (!legendEl) return;
      const nodes = state.nodes || [];
      const has = {
        'You': nodes.some(n => n.is_self),
        'People': nodes.some(n => n.kind === 'person' && !n.is_self),
        'Projects & orgs': nodes.some(n => n.kind === 'project' || n.kind === 'org'),
        'Open loops': nodes.some(n => isLoop(n)),
      };
      const rows = LEGEND.filter(it => has[it[0]]);
      if (rows.length < 2) { legendEl.hidden = true; legendEl.innerHTML = ''; return; }
      legendEl.hidden = false;
      legendEl.innerHTML = rows.map(([label, shape, color]) =>
        '<span style="display:inline-flex;align-items:center;gap:5px">'
        + legendSwatch(shape, color) + label + '</span>').join('');
    }

    function setEdit(on) {
      state.edit = !!on;
      state.linkFrom = null;
      if (toolbar) {
        const b = toolbar.querySelector('[data-act=correct]');
        if (b) b.classList.toggle('on', state.edit);
      }
      wrap && wrap.classList.toggle('editing', state.edit);
      if (!state.edit && !state.selected) {
        if (panel) { panel.hidden = true; panel.innerHTML = ''; }
      } else if (state.edit) {
        renderCorrectPanel();
      }
    }

    function neighborsOf(id) {
      return state.edges.filter(e => e.source === id || e.target === id).map(e => {
        const other = e.source === id ? e.target : e.source;
        return { id: other, node: state.byId[other], manual: !!e.manual, edge: e };
      }).filter(x => x.node);
    }

    function renderInsights() {
      if (!insightEl) return;
      const notes = state.insights || [];
      if (!notes.length) { insightEl.hidden = true; insightEl.innerHTML = ''; return; }
      insightEl.hidden = false;
      insightEl.innerHTML = notes.map(n =>
        '<button type="button" class="const-insight-btn" data-nid="'
        + MnemosEsc(n.node_id || '') + '">' + MnemosEsc(n.text || '') + '</button>'
      ).join('');
    }

    function renderTip(n, clientX, clientY) {
      if (!tip || !n) { if (tip) tip.hidden = true; return; }
      const why = (n.why && n.why.length) ? n.why.join(' · ') : '';
      const tipTitle = (n.meta && n.meta.full_text) || n.label || n.id;
      tip.hidden = false;
      const held = [];
      if ((n._loops || 0) > 0) {
        held.push(n._loops + (n._loops === 1 ? ' open loop' : ' open loops'));
      }
      const rest = (n._members || []).length - (n._loops || 0);
      if (rest > 0) held.push(rest + ' connected');
      tip.innerHTML = '<strong>' + MnemosEsc(tipTitle) + '</strong>'
        + '<span class="const-tip-kind">'
        + MnemosEsc(held.length ? held.join(' · ') : (n.kind || '')) + '</span>'
        + (why ? '<div class="const-tip-why">' + MnemosEsc(why) + '</div>' : '');
      if (wrap) {
        const r = wrap.getBoundingClientRect();
        tip.style.left = Math.min(r.width - 180, Math.max(8, clientX - r.left + 12)) + 'px';
        tip.style.top = Math.min(r.height - 70, Math.max(8, clientY - r.top + 12)) + 'px';
      }
    }

    async function toggleDiffMode() {
      state.diffMode = !state.diffMode;
      const b = toolbar && toolbar.querySelector('[data-act=diff]');
      if (b) b.classList.toggle('on', state.diffMode);
      if (!state.diffMode) {
        state.diff = null;
        // Keep ghosts for the session once seen; clear markers on live nodes.
        state.nodes.forEach(n => {
          delete n._diffEnter; delete n._diffRise; delete n._diffFall;
        });
        return;
      }
      try {
        const d = await (await fetch('/field/diff?since=today')).json();
        state.diff = d;
        const entered = new Set(d.entered_focus || []);
        const rising = {};
        (d.rising || []).forEach(r => { rising[r.id] = r.delta; });
        const falling = {};
        (d.falling || []).forEach(r => { falling[r.id] = r.delta; });
        // Entrance emphasis without re-layout — reuse _born ring.
        const born = performance.now();
        state.nodes.forEach(n => {
          if (entered.has(n.id)) {
            n._diffEnter = true;
            if (!n._born) n._born = born;
          }
          if (rising[n.id] != null) n._diffRise = rising[n.id];
          if (falling[n.id] != null) n._diffFall = falling[n.id];
        });
        // Ghosts: departed focus nodes — faint for this session.
        const left = d.left_focus || [];
        left.forEach(id => {
          if (!state.ghosts.includes(id)) state.ghosts.push(id);
        });
      } catch (e) {
        state.diffMode = false;
        if (b) b.classList.remove('on');
      }
    }

    function flushDwell() {
      // Attention ledger: how long the evidence popover was actually read.
      if (!state.evId || !state.evT0) { state.evId = null; state.evT0 = 0; return; }
      const ms = Date.now() - state.evT0;
      const id = state.evId;
      state.evId = null; state.evT0 = 0;
      if (ms < 800) return;  // a bounce, not a read
      try {
        fetch('/field/feedback', { method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ id: id, outcome: 'dwell', dwell_ms: ms }) });
      } catch (e) { /* instrumentation never breaks the panel */ }
    }

    function renderRankBreakdown(bd) {
      // Quiet "Why is this here?" — muted ink segments, not a rainbow chart.
      if (!bd || !bd.components || !bd.components.length) return '';
      const comps = bd.components.slice().sort(
        (a, b) => Math.abs(b.value || 0) - Math.abs(a.value || 0));
      const total = Number(bd.total) || 0;
      const absSum = comps.reduce((s, c) => s + Math.abs(Number(c.value) || 0), 0) || 1;
      let html = '<details class="const-rank">';
      html += '<summary class="const-rank-title">Why this ranks here</summary>';
      if (bd.admitted_by === 'quota') {
        html += '<div class="const-rank-admit">Included to keep people in view.</div>';
      } else if (bd.admitted_by === 'pin') {
        html += '<div class="const-rank-admit">You pinned this.</div>';
      }
      html += '<div class="const-rank-bar" title="Gravity '
        + Math.round(total * 100) + '%">';
      comps.forEach((c, i) => {
        const pct = Math.max(3, Math.round(100 * Math.abs(Number(c.value) || 0) / absSum));
        // Single ink family — opacity encodes magnitude order, not hue.
        const op = (0.28 + 0.55 * (1 - i / Math.max(1, comps.length - 1 || 1))).toFixed(2);
        html += '<span class="const-rank-seg" style="flex:' + pct
          + ' 0 0;background:rgba(35,38,43,' + op + ')" data-rk="' + i + '"></span>';
      });
      html += '</div>';
      html += '<div class="const-rank-total">Gravity '
        + Math.round(Math.max(0, Math.min(1, total)) * 100) + '%</div>';
      html += '<div class="const-rank-list">';
      comps.forEach((c, i) => {
        const v = Number(c.value) || 0;
        const val = (v >= 0 ? '+' : '') + (Math.round(v * 1000) / 1000);
        html += '<button type="button" class="const-rank-row" data-rk="' + i + '">'
          + '<span class="const-rank-label">' + MnemosEsc(c.label || c.key || '')
          + '</span><span class="const-rank-val">' + MnemosEsc(String(val))
          + '</span></button>';
        html += '<div class="const-rank-ev" hidden data-rk-ev="' + i + '">';
        const refs = c.evidence_refs || [];
        if (refs.length) {
          refs.forEach(r => {
            html += '<div class="const-ev-row"><span class="const-ev-ch">ref</span>'
              + '<div>' + MnemosEsc(String(r)) + '</div></div>';
          });
        } else {
          html += '<div class="const-edit-hint">'
            + MnemosEsc(
              (c.evidence === 'none')
                ? 'Structural signal — no single event'
                : 'No evidence refs')
            + '</div>';
        }
        html += '</div>';
      });
      html += '</div></details>';
      return html;
    }

    async function openEvidence(id) {
      if (state.evId && state.evId !== id) flushDwell();
      state.evId = id; state.evT0 = Date.now();
      state.selected = id;
      if (!panel) return;
      panel.hidden = false;
      panel.innerHTML = '<div class="const-edit-hint">Gathering evidence…</div>';
      try {
        const data = await MnemosJson('/graph/constellation/evidence?id='
          + encodeURIComponent(id));
        const n = data.node || state.byId[id] || {};
        const fullTitle = (data.detail && data.detail.fact && data.detail.fact.text)
          || (n.meta && n.meta.full_text)
          || n.label || id;
        let html = '<div class="const-edit-head"><strong>' + MnemosEsc(fullTitle)
          + '</strong><button type="button" data-act="close-ev" class="linkish">Close</button></div>';
        html += '<div class="const-tip-kind">' + MnemosEsc(n.kind || '')
          + (n.layer ? ' · ' + MnemosEsc(n.layer) : '') + '</div>';
        // Rank breakdown first — same panel vocabulary as provenance below.
        html += renderRankBreakdown(data.breakdown
          || (state.breakdowns && state.breakdowns[id]));
        if (!data.breakdown && n.gravity != null) {
          html += '<div class="const-edit-hint">Gravity '
            + Math.round((n.gravity || 0) * 100) + '%'
            + (n.prospective_risk >= 0.7 ? ' · promise risk' : '') + '</div>';
        }
        if (data.why && data.why.length) {
          html += '<div class="const-why">' + data.why.map(w =>
            '<div>' + MnemosEsc(w) + '</div>').join('') + '</div>';
        }
        const allowed = data.allowed_kinds || [];
        if (allowed.length) {
          const cur = data.current_kind || n.kind || '';
          html += '<label class="const-edit-hint" style="display:block;margin-top:8px">Category</label>';
          html += '<select class="const-kind-select" data-act="kind-select">';
          allowed.forEach(k => {
            html += '<option value="' + MnemosEsc(k) + '"'
              + (k === cur ? ' selected' : '') + '>'
              + MnemosEsc(k) + '</option>';
          });
          html += '</select>';
          html += '<button type="button" class="const-link-btn" data-act="do-reclassify" '
            + 'style="margin-top:6px">Save category</button>';
        }
        html += '<div class="const-edit-actions">'
          + '<button type="button" class="const-link-btn" data-act="do-focus">Focus</button>'
          + '<button type="button" class="const-link-btn" data-act="do-pin">'
          + (n.pinned ? 'Unpin' : 'Pin') + '</button></div>';
        // Org / entity living brief deep-link (entity:<id> constellation nodes).
        const idStr = String(id || '');
        if (idStr.indexOf('entity:') === 0) {
          const eid = idStr.split(':')[1];
          const kind = String(n.kind || data.current_kind || '').toLowerCase();
          if (eid && (kind === 'org' || kind === 'company' || kind === 'organization'
              || kind === 'project' || kind === 'entity' || !kind)) {
            html += '<div style="margin:8px 0"><a class="const-link-btn" href="/org/'
              + MnemosEsc(eid) + '">Open living brief →</a></div>';
          }
        }
        const sources = data.sources || [];
        if (sources.length) {
          html += '<div class="const-edit-list"><div class="const-edit-hint">Evidence</div>';
          sources.slice(0, 8).forEach((s, idx) => {
            html += '<div class="const-ev-row"><span class="const-ev-ch">'
              + MnemosEsc(s.modality || s.channel || 'source') + '</span>';
            html += '<div class="const-ev-body">';
            const hl = s.span_highlight;
            if (hl && (hl.match || hl.before || hl.after)) {
              html += '<div class="const-ev-transcript">'
                + MnemosEsc(hl.before || '')
                + '<mark class="span-hl">' + MnemosEsc(hl.match || '') + '</mark>'
                + MnemosEsc(hl.after || '') + '</div>';
            } else {
              html += '<div>' + MnemosEsc((s.text || s.transcript || '').slice(0, 200) || '—') + '</div>';
              if (s.source_span) {
                html += '<div class="const-ev-quote">“' + MnemosEsc(s.source_span) + '”</div>';
              }
            }
            const play = s.play_path || s.enhanced_audio || s.audio_path;
            if (play) {
              const aid = 'const-ev-audio-' + idx;
              html += '<button type="button" class="const-link-btn const-play-moment" '
                + 'data-act="play-moment" data-audio-id="' + aid + '">Play the moment</button>';
              html += '<audio id="' + aid + '" class="const-ev-audio" controls preload="none" src="/artifact?path='
                + encodeURIComponent(play) + '"></audio>';
            }
            html += '</div></div>';
          });
          html += '</div>';
        } else {
          html += '<div class="const-edit-hint">No source snippets yet — still in the graph.</div>';
        }
        if (state.edit) {
          html += '<div class="const-edit-hint" style="margin-top:10px">Correction mode</div>';
          html += '<button type="button" class="const-link-btn" data-act="start-link">Connect to…</button>';
          const neigh = neighborsOf(id);
          if (neigh.length) {
            html += '<div class="const-edit-list">';
            neigh.forEach(x => {
              html += '<div class="const-edit-row"><span>' + MnemosEsc(x.node.label || x.id)
                + (x.manual ? ' <em>manual</em>' : '')
                + '</span><button type="button" data-unlink="' + MnemosEsc(x.id)
                + '">Remove</button></div>';
            });
            html += '</div>';
          }
        }
        panel.innerHTML = html;
      } catch (err) {
        panel.innerHTML = '<div class="const-edit-hint" style="color:var(--danger)">'
          + MnemosEsc(err.message || err) + '</div>'
          + '<div class="const-edit-actions">'
          + '<button type="button" class="const-link-btn" data-act="retry-ev">Retry</button>'
          + '<button type="button" class="linkish" data-act="close-ev">Close</button></div>';
      }
    }

    function renderCorrectPanel() {
      if (!panel) return;
      panel.hidden = false;
      if (!state.selected) {
        panel.innerHTML = '<div class="const-edit-hint">Click a node to correct links — rare, deliberate.</div>';
        return;
      }
      openEvidence(state.selected);
    }

    async function apiLink(a, b, method) {
      const url = method === 'DELETE' ? '/graph/edge/remove' : '/graph/edge';
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ source: a, target: b }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(
        (typeof j.detail === 'string' ? j.detail : j.error) || ('HTTP ' + r.status));
      return j;
    }
    async function refreshGraph() {
      // explain stays off the poll path — evidence drawer fetches breakdowns on click
      const data2 = await MnemosJson('/field/state?limit=28');
      state.nodes = data2.nodes || [];
      state.edges = data2.edges || [];
      state.insights = data2.insights || [];
      state.breakdowns = data2.breakdowns || {};
      state.byId = {};
      state.nodes.forEach(n => { state.byId[n.id] = n; });
      refield(state);
      renderInsights();
      renderLegend();
      if (state.selected) openEvidence(state.selected);
      if (state.onChange) state.onChange(data2);
    }
    async function unlinkPair(a, b) {
      await apiLink(a, b, 'DELETE');
      await refreshGraph();
    }
    async function linkPair(a, b) {
      await apiLink(a, b, 'POST');
      state.linkFrom = null;
      await refreshGraph();
    }
    async function togglePin(id) {
      const n = state.byId[id];
      const pinned = !(n && n.pinned);
      await fetch('/graph/constellation/pin', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, pinned }),
      });
      await refreshGraph();
    }
    async function reclassify(id, kind) {
      const r = await fetch('/graph/constellation/reclassify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, kind }),
      });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(
        (typeof j.detail === 'string' ? j.detail : j.error) || ('HTTP ' + r.status));
      state.selected = j.id || id;
      await refreshGraph();
      if (state.selected) openEvidence(state.selected);
    }

    const clampCam = () => {
      // Wide enough that Fit can fill sparse fields; still bounded so pan/zoom
      // can't lose the map entirely.
      const cam = state.cam;
      // A stored camera is user data and the frame may not be measured yet.
      // Both were load-bearing bugs: restoring a saved camera called this
      // before the first resize(), where Math.min(undefined, undefined) is
      // NaN — which travelled into cam.x/cam.y and blanked the whole field for
      // every returning user who had ever panned or zoomed.
      if (!Number.isFinite(cam.z)) cam.z = 1;
      if (!Number.isFinite(cam.x)) cam.x = 0;
      if (!Number.isFinite(cam.y)) cam.y = 0;
      cam.z = Math.max(0.55, Math.min(2.6, cam.z));
      if (!state.w || !state.h) return;
      const maxPan = Math.min(state.w, state.h) * (0.42 / Math.max(0.75, cam.z));
      cam.x = Math.max(-maxPan, Math.min(maxPan, cam.x));
      cam.y = Math.max(-maxPan, Math.min(maxPan, cam.y));
    };
    const saveCam = () => {
      clampCam();
      if (state.persistKey) window.MnemosMemory.set(state.persistKey, state.cam);
    };
    const fit = () => {
      // Zoom/pan to the live node bounds so a sparse field fills the frame
      // instead of sitting as a tight cluster in empty space.
      const nodes = (state.nodes || []).filter(
        n => n._x != null && n._y != null && nodeVisible(state, n));
      if (!nodes.length || !state.w || !state.h) {
        state.cam = { x: 0, y: 0, z: 1 };
        saveCam();
        return;
      }
      let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
      nodes.forEach(n => {
        const pad = (n._r || 8) + 28;
        // Primaries are always named, and a label runs well past its glyph.
        const lab = (n.is_self || isPrimary(n)) ? 72 : 0;
        minX = Math.min(minX, n._x - pad - ((n._labelSide || 1) < 0 ? lab : 0));
        minY = Math.min(minY, n._y - pad);
        maxX = Math.max(maxX, n._x + pad + ((n._labelSide || 1) >= 0 ? lab : 0));
        maxY = Math.max(maxY, n._y + pad + 18);
      });
      const bw = Math.max(40, maxX - minX);
      const bh = Math.max(40, maxY - minY);
      // Leave room for hint (top-right), tools (bottom), insight (bottom-left).
      const insetL = 16, insetR = 16, insetT = 28, insetB = 56;
      const availW = Math.max(80, state.w - insetL - insetR);
      const availH = Math.max(80, state.h - insetT - insetB);
      const z = Math.max(0.7, Math.min(2.35, Math.min(availW / bw, availH / bh) * 0.92));
      const midX = (minX + maxX) / 2;
      const midY = (minY + maxY) / 2;
      // Screen = (world - center) * z + center + cam  → solve cam for target mid.
      state.cam = {
        x: (state.w / 2 - midX) * z + (insetL - insetR) / 2,
        y: (state.h / 2 - midY) * z + (insetT - insetB) / 2,
        z: z,
      };
      saveCam();
    };

    const saved = state.persistKey
      ? window.MnemosMemory.get(state.persistKey, null) : null;
    const resize = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(r.width * dpr));
      canvas.height = Math.max(1, Math.floor(r.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      state.w = r.width; state.h = r.height;
      refield(state);
      if (!state._fittedOnce && !(saved && typeof saved.z === 'number')) {
        fit();
        state._fittedOnce = true;
      } else {
        clampCam();
      }
    };
    if (saved && Number.isFinite(saved.z)
        && Number.isFinite(saved.x) && Number.isFinite(saved.y)) {
      state.cam = Object.assign(state.cam, saved);
      clampCam();
      state._fittedOnce = true;
    }

    function shortLabel(s) {
      // Server already titleizes + word-boundary truncates; only clip if a
      // raw long string slipped through (avoid a second mid-word cut).
      s = String(s || '');
      if (s.length <= 28) return s;
      const cut = s.slice(0, 28);
      const sp = cut.lastIndexOf(' ');
      return ((sp > 8 ? cut.slice(0, sp) : cut.slice(0, 27)).replace(/[.,;:\-\s]+$/, '')) + '…';
    }

    function sumCodes(s) {
      let h = 0;
      for (let i = 0; i < String(s || '').length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
      return Math.abs(h);
    }

    function layout(st) {
      const n = st.nodes.length;
      if (!n || !st.w) return;
      const cx = st.w / 2, cy = st.h / 2;
      const pad = Math.max(36, Math.min(st.w, st.h) * 0.1);
      const maxR = Math.min(st.w, st.h) / 2 - pad;
      const unit = Math.max(1, Math.min(st.w, st.h) / 380);
      // Sparse fields push outward a little so Fit has structure to zoom into.
      const spread = st.nodes.length <= 16 ? 0.07 : (st.nodes.length <= 24 ? 0.03 : 0);
      // Angle is identity, radius is importance. Keeping the angle stable is
      // what preserves spatial memory across renders; moving the radius is
      // what lets the field say "this matters more today" without resizing
      // every glyph.
      const angleOf = (node) => (typeof node.anchor === 'number' && node.anchor)
        ? node.anchor : ((sumCodes(node.id) % 997) / 997) * Math.PI * 2;

      const self = st.nodes.find(node => node.is_self) || null;
      if (self) {
        self._x = cx; self._y = cy; self._r = 8 * unit;
        self._labelSide = 1; self._labelDy = 0; self._zone = 'self';
      }
      // Ring members are the primaries plus any satellite with no home — an
      // orphan open loop still has to be findable.
      const rings = {};
      st.nodes.forEach(node => {
        if (node === self || node._host) return;
        const z = node._zone || 'peripheral';
        (rings[z] = rings[z] || []).push(node);
      });
      Object.keys(rings).forEach(z => {
        const arr = rings[z];
        arr.forEach(node => { node._ang = angleOf(node); });
        arr.sort((a, b) => a._ang - b._ang);
        // Relax on angle only: nodes keep their side of the field, they just
        // stop sitting on top of each other.
        const gap = Math.min((Math.PI * 2) / Math.max(1, arr.length), 0.62);
        for (let pass = 0; pass < 30 && arr.length > 1; pass++) {
          for (let i = 0; i < arr.length; i++) {
            const a = arr[i], b = arr[(i + 1) % arr.length];
            let d = b._ang - a._ang;
            while (d < 0) d += Math.PI * 2;
            if (d >= gap) continue;
            const push = (gap - d) / 2;
            a._ang -= push; b._ang += push;
          }
        }
        const ring = (ZONE_RING[z] != null ? ZONE_RING[z] : 0.78)
          + (z === 'self' ? 0 : spread);
        arr.forEach(node => {
          node._x = cx + Math.cos(node._ang) * maxR * ring;
          node._y = cy + Math.sin(node._ang) * maxR * ring * 0.86;
          node._r = sizeFor(node) * unit;
          node._labelSide = node._x >= cx ? 1 : -1;
          node._labelDy = 0;
        });
      });
      // Satellites orbit their host on the side facing away from the centre,
      // so an open system never spills back over the middle of the field.
      st.nodes.forEach(node => {
        const host = node._host ? st.byId[node._host] : null;
        if (!host) return;
        const sibs = host._members || [];
        const k = Math.max(1, sibs.length);
        const i = Math.max(0, sibs.indexOf(node.id));
        const out = Math.atan2(host._y - cy, host._x - cx);
        const arc = Math.min(Math.PI * 1.3, 0.6 + k * 0.3);
        const a = out - arc / 2 + (k === 1 ? arc / 2 : arc * (i / (k - 1)));
        const d = (host._r || 10) + (24 + (i % 2) * 10) * unit;
        node._r = sizeFor(node) * unit;
        node._x = host._x + Math.cos(a) * d;
        node._y = host._y + Math.sin(a) * d * 0.9;
        node._labelSide = Math.cos(a) >= 0 ? 1 : -1;
        node._labelDy = 0;
        node._zone = host._zone || 'active';
      });
      st.nodes.forEach(node => {
        node._x = Math.max(pad * 0.6, Math.min(st.w - pad * 0.6, node._x));
        node._y = Math.max(pad * 0.6, Math.min(st.h - pad * 0.6, node._y));
      });
      // Label de-collision runs over the always-named tier only.
      const labeled = st.nodes.filter(node => node.is_self || isPrimary(node));
      labeled.sort((a, b) => a._y - b._y || a._x - b._x);
      for (let i = 0; i < labeled.length; i++) {
        const a = labeled[i];
        for (let j = 0; j < i; j++) {
          const b = labeled[j];
          if (a._labelSide !== b._labelSide) continue;
          if (Math.abs(a._x - b._x) < 110
              && Math.abs((a._y + a._labelDy) - (b._y + b._labelDy)) < 22) {
            a._labelDy = b._labelDy + 18;
          }
        }
        // Cap the cascade: a label 100px from its own star explains nothing.
        a._labelDy = Math.max(-36, Math.min(36, a._labelDy));
      }
      st.t0 = performance.now();
    }

    function drawLabel(x, y, text, side, emphasis, alpha) {
      const a = alpha == null ? 1 : Math.max(0, Math.min(1, alpha));
      const label = shortLabel(text);
      ctx.font = (emphasis ? '600 ' : '500 ') + '12px "Iowan Old Style", Georgia, serif';
      const tw = ctx.measureText(label).width;
      const padX = 6;
      const lx = side >= 0 ? x + 10 : x - 10 - tw;
      const ly = y - 4;
      ctx.fillStyle = 'rgba(22,22,27,' + ((emphasis ? 0.97 : 0.9) * a).toFixed(3) + ')';
      ctx.beginPath();
      const rw = tw + padX * 2, rh = 17, rx = lx - padX, ry = ly - 12;
      const rad = 7;
      ctx.moveTo(rx + rad, ry);
      ctx.arcTo(rx + rw, ry, rx + rw, ry + rh, rad);
      ctx.arcTo(rx + rw, ry + rh, rx, ry + rh, rad);
      ctx.arcTo(rx, ry + rh, rx, ry, rad);
      ctx.arcTo(rx, ry, rx + rw, ry, rad);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = emphasis
        ? 'rgba(242,241,247,' + (0.95 * a).toFixed(3) + ')'
        : 'rgba(220,219,226,' + (0.88 * a).toFixed(3) + ')';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(label, lx, ly);
    }

    function roundRectPath(x, y, w, h, rad) {
      ctx.beginPath();
      ctx.moveTo(x + rad, y);
      ctx.arcTo(x + w, y, x + w, y + h, rad);
      ctx.arcTo(x + w, y + h, x, y + h, rad);
      ctx.arcTo(x, y + h, x, y, rad);
      ctx.arcTo(x, y, x + w, y, rad);
      ctx.closePath();
    }

    /* Three tiers, not seven shapes. Primaries (you, people, projects, orgs)
       are the objects of the field and keep a distinct silhouette; everything
       else is one quiet satellite mark. The ontology still lives in the
       database — it just doesn't need equal visual representation. */
    function drawKind(n, r, alpha) {
      const x = n._x, y = n._y;
      const kind = n.kind || 'idea';
      // Arrival: a node that just entered memory announces itself — an
      // expanding ring in its own hue while the glyph grows in. Live updates
      // stamp _born only on genuinely NEW nodes, never on a fresh page mount.
      if (n._born) {
        const age = (performance.now() - n._born) / 1400;
        if (age >= 1) { delete n._born; }
        else if (!REDUCED_MOTION) {
          ctx.save();
          ctx.beginPath();
          ctx.arc(x, y, r + 5 + age * 30, 0, Math.PI * 2);
          ctx.strokeStyle = kindColor(kind, 0.45 * (1 - age));
          ctx.lineWidth = 1.6 * (1 - age) + 0.4;
          ctx.stroke();
          ctx.restore();
          r = r * Math.min(1, 0.35 + age * 1.3);
        }
      }
      ctx.save();
      if (n.is_self) {
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = 'rgba(' + SELF_RGB + ',' + alpha + ')';
        ctx.fill();
        ctx.beginPath();
        ctx.arc(x, y, r + 4, 0, Math.PI * 2);
        ctx.strokeStyle = 'rgba(' + SELF_RGB + ',' + (0.5 * alpha).toFixed(3) + ')';
        ctx.lineWidth = 1.4;
        ctx.stroke();
      } else if (kind === 'person') {
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = kindColor('person', alpha);
        ctx.fill();
      } else if (kind === 'org') {
        const hr = r * 1.05;
        ctx.beginPath();
        for (let i = 0; i < 6; i++) {
          const a = (Math.PI / 3) * i - Math.PI / 6;
          const px = x + hr * Math.cos(a), py = y + hr * Math.sin(a);
          if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        }
        ctx.closePath();
        ctx.fillStyle = kindColor('org', alpha * 0.92);
        ctx.fill();
      } else if (kind === 'project') {
        ctx.translate(x, y);
        ctx.rotate(Math.PI / 4);
        ctx.fillStyle = kindColor('project', alpha * 0.92);
        roundRectPath(-r * 0.82, -r * 0.82, r * 1.64, r * 1.64, r * 0.28);
        ctx.fill();
      } else {
        // One satellite mark for every secondary kind. Open work takes the
        // attention hue; concepts, tools and places stay neutral so they read
        // as context rather than as something to do.
        const urge = Math.max(Number(n.prospective_risk) || 0, Number(n.aging) || 0);
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = isLoop(n)
          ? kindColor('task', Math.min(0.95, 0.5 + alpha * 0.5))
          : 'rgba(' + SAT_RGB + ',' + (alpha * 0.75).toFixed(3) + ')';
        ctx.fill();
        // Neglect is a warm ring that thickens — one encoding, amber, matching
        // the halo hue an at-risk primary takes.
        if (isLoop(n) && urge > 0.45) {
          ctx.beginPath();
          ctx.arc(x, y, r + 3 + urge * 2, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(223,179,94,' + (0.25 + urge * 0.4).toFixed(3) + ')';
          ctx.lineWidth = 0.8 + urge * 1.4;
          ctx.stroke();
        }
      }
      ctx.restore();
    }

    function drawEdgeLabel(x, y, rel) {
      const text = String(rel || '').replace(/_/g, ' ').trim();
      if (!text || text === 'related' || text === 'manual') return;
      ctx.font = '500 10px ui-sans-serif, system-ui, sans-serif';
      const tw = ctx.measureText(text).width;
      ctx.fillStyle = 'rgba(22,22,27,.92)';
      roundRectPath(x - tw / 2 - 5, y - 8, tw + 10, 15, 7);
      ctx.fill();
      ctx.fillStyle = 'rgba(206,204,214,.92)';
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
      ctx.fillText(text, x - tw / 2, y + 3.5);
    }

    /* A collapsed system carries its unfinished work as one count on its rim
       — the app-icon idiom. The words ("3 open loops") live in the tooltip and
       the inspector, where there is room for them. */
    function drawLoopBadge(st, n, r, alpha) {
      const text = n._loops > 9 ? '9+' : String(n._loops);
      ctx.font = '600 10px ui-sans-serif, system-ui, sans-serif';
      const tw = ctx.measureText(text).width;
      const rad = Math.max(7.5, tw / 2 + 5);
      // Sit on the rim, away from the centre of the field, where there is air.
      const ox = n._x - st.w / 2, oy = n._y - st.h / 2;
      const m = Math.hypot(ox, oy);
      const ux = m > 8 ? ox / m : 0.7, uy = m > 8 ? oy / m : 0.7;
      const bx = n._x + ux * (r + rad * 0.8);
      const by = n._y + uy * (r + rad * 0.8);
      ctx.beginPath();
      ctx.arc(bx, by, rad, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(28,32,41,' + (0.95 * alpha).toFixed(3) + ')';
      ctx.fill();
      ctx.strokeStyle = 'rgba(223,179,94,' + (0.55 * alpha).toFixed(3) + ')';
      ctx.lineWidth = 1.2;
      ctx.stroke();
      ctx.fillStyle = 'rgba(223,179,94,' + (0.95 * alpha).toFixed(3) + ')';
      ctx.textAlign = 'center';
      ctx.textBaseline = 'middle';
      ctx.fillText(text, bx, by + 0.5);
      ctx.textAlign = 'left';
      ctx.textBaseline = 'alphabetic';
    }

    function edgeVisible(e, st) {
      // The resting field is stars, not wiring. One constellation is
      // illuminated at a time: hover, selection, or focus.
      if (st.showFilaments || st.edit) return true;
      const act = st.focusId || st.selected || st.hover;
      if (!act) return false;
      if (e.source === act || e.target === act) return true;
      const a = st.byId[act];
      const host = (a && a._host) ? a._host : act;
      return e.source === host || e.target === host;
    }


    function draw(st, now) {
      const w = st.w, h = st.h;
      ctx.clearRect(0, 0, w, h);
      // The light IS the ranking. A gravity well sits under the centre,
      // brightest where NOW lives and falling away through ACTIVE to the
      // dormant rim — the zones are never drawn as rings. This well and the
      // node opacity are the only things that say where a band ends.
      const slab = ctx.createLinearGradient(0, 0, 0, h);
      slab.addColorStop(0, 'rgba(233,231,226,.03)');
      slab.addColorStop(0.55, 'rgba(233,231,226,0)');
      slab.addColorStop(1, 'rgba(0,0,0,.14)');
      ctx.fillStyle = slab;
      ctx.fillRect(0, 0, w, h);
      const wx = w / 2 + st.cam.x, wy = h / 2 + st.cam.y;
      const wr = Math.min(w, h) * 0.62 * Math.max(0.7, Math.min(1.8, st.cam.z));
      const well = ctx.createRadialGradient(wx, wy, 4, wx, wy, wr);
      well.addColorStop(0, 'rgba(141,133,242,.16)');
      well.addColorStop(0.34, 'rgba(141,133,242,.055)');
      well.addColorStop(1, 'rgba(10,10,11,0)');
      ctx.fillStyle = well;
      ctx.fillRect(0, 0, w, h);
      // Off-axis warmth, so a centred well never reads as a bullseye.
      const warm = ctx.createRadialGradient(
        w * 0.12, h * 1.08, 8, w * 0.12, h * 1.08, Math.max(w, h) * 0.8);
      warm.addColorStop(0, 'rgba(223,179,94,.055)');
      warm.addColorStop(1, 'rgba(10,10,11,0)');
      ctx.fillStyle = warm;
      ctx.fillRect(0, 0, w, h);
      // Selection ripple clock — stamped on change, decays below.
      if (st._selSeen !== st.selected) { st._selSeen = st.selected; st._selT = now; }

      ctx.save();
      ctx.translate(w / 2 + st.cam.x, h / 2 + st.cam.y);
      ctx.scale(st.cam.z, st.cam.z);
      ctx.translate(-w / 2, -h / 2);
      const reduce = window.MnemosReduceMotion();
      const act = st.focusId || st.selected || st.hover || null;
      const lens = act ? lensSet(st, act) : null;

      st.edges.forEach((e) => {
        if (!edgeVisible(e, st)) return;
        const a = st.byId[e.source], b = st.byId[e.target];
        if (!a || !b || !nodeVisible(st, a) || !nodeVisible(st, b)) return;
        const hot = !!act && (e.source === act || e.target === act);
        ctx.beginPath();
        ctx.moveTo(a._x, a._y);
        const mx = (a._x + b._x) / 2;
        const my = (a._y + b._y) / 2 - 8;
        ctx.quadraticCurveTo(mx, my, b._x, b._y);
        const conf = e.confidence != null ? e.confidence : 0.6;
        if (e.style === 'dashed' || (!e.manual && conf < 0.75)) ctx.setLineDash([4, 4]);
        else if (e.style === 'dotted' || conf < 0.45) ctx.setLineDash([2, 4]);
        else ctx.setLineDash([]);
        const alpha = hot ? (0.34 + conf * 0.2) : 0.13;
        ctx.strokeStyle = hot
          ? 'rgba(141,133,242,' + alpha.toFixed(3) + ')'
          : 'rgba(232,231,244,' + alpha.toFixed(3) + ')';
        ctx.lineWidth = hot ? 1.7 : 1.05;
        ctx.stroke();
        ctx.setLineDash([]);
        // Name the relationship while it is lit: the word is the explanation
        // the bare line was withholding.
        if (hot && st.cam.z > 0.62) {
          // Offset the word perpendicular to the line, and skip it entirely
          // when the two stars are too close to seat it clear of either glyph.
          const ex = b._x - a._x, ey = b._y - a._y;
          const len = Math.hypot(ex, ey);
          if (len > (a._r || 8) + (b._r || 8) + 62) {
            const nx = -ey / len, ny = ex / len;
            drawEdgeLabel(mx + nx * 11, my + ny * 11, e.rel);
          }
        }
      });

      st.nodes.forEach(n => {
        if (!nodeVisible(st, n)) return;
        let alpha = zoneAlpha(st, n);
        if (lens) alpha *= lens.has(n.id) ? 1 : 0.18;
        if (st.loopsOnly) alpha *= loopish(st, n) ? 1 : 0.12;
        if (st.softIds && st.softIds.size) {
          if (st.softIds.has(n.id)) alpha = Math.max(alpha, 0.95);
          else alpha *= 0.22;
        }
        if (st.emphasizeIds && st.emphasizeIds.size) {
          if (st.emphasizeIds.has(n.id)) alpha = Math.max(alpha, 1.0);
          else alpha *= 0.28;
        }
        if (st.rangeCutoff && n.ts && n.ts < st.rangeCutoff) alpha *= 0.3;
        if (alpha < 0.035) return;
        const r = n._r || 8;
        // Urgency breathes; it never moves the node. A due date closing in and
        // a promise gaining age both speed the cycle up.
        const urge = Math.max(Number(n.prospective_risk) || 0, Number(n.aging) || 0);
        const period = 3400 - urge * 1700;
        const breath = reduce ? 0
          : Math.sin((now - st.t0) / period) * (0.008 + urge * 0.022);
        const want = st.hover === n.id ? 1 : 0;
        n._lift = reduce ? 0 : (n._lift || 0) + (want - (n._lift || 0)) * 0.18;
        const scale = (1 + breath) * (1 + n._lift * 0.16);
        // Gravity halo — the ranking made visible with no number in sight.
        if (n.is_self || isPrimary(n)) {
          const g = Math.max(0.15, Math.min(1.15, n.gravity || 0.35));
          const hr = r * scale * (1.9 + g * 1.4);
          const hue = urge >= 0.55 ? '223,179,94'
            : (n.is_self ? SELF_RGB : '141,133,242');
          const halo = ctx.createRadialGradient(
            n._x, n._y, r * scale * 0.75, n._x, n._y, hr);
          halo.addColorStop(0, 'rgba(' + hue + ','
            + ((0.06 + g * 0.1) * alpha).toFixed(3) + ')');
          halo.addColorStop(1, 'rgba(' + hue + ',0)');
          ctx.fillStyle = halo;
          ctx.beginPath();
          ctx.arc(n._x, n._y, hr, 0, Math.PI * 2);
          ctx.fill();
        }
        const lit = st.hover === n.id || st.selected === n.id || st.focusId === n.id;
        if (lit) {
          ctx.save();
          ctx.shadowColor = n.is_self
            ? 'rgba(' + SELF_RGB + ',.6)' : kindColor(n.kind, 0.6);
          ctx.shadowBlur = 10 + n._lift * 8;
        }
        drawKind(n, r * scale, Math.min(0.96, alpha));
        if (lit) ctx.restore();
        // Selection ripple — the field acknowledges the click, then settles.
        if (st.selected === n.id && st._selT && !reduce) {
          const age = (now - st._selT) / 900;
          if (age < 1) {
            ctx.beginPath();
            ctx.arc(n._x, n._y, r * scale + 4 + age * 26, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(141,133,242,' + (0.5 * (1 - age)).toFixed(3) + ')';
            ctx.lineWidth = 1.4 * (1 - age) + 0.3;
            ctx.stroke();
          }
        }
        if (st.selected === n.id || st.focusId === n.id || st.hover === n.id || n.pinned) {
          ctx.beginPath();
          ctx.arc(n._x, n._y, r * scale + 3.5, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(141,133,242,.82)';
          ctx.lineWidth = 1.6;
          ctx.stroke();
        }
        if (!systemOpen(st, n) && (n._loops || 0) > 0) {
          drawLoopBadge(st, n, r * scale, alpha);
        }
        // Diff mode: rising/falling arrow on hover only (calm default).
        if (st.diffMode && st.hover === n.id
            && (n._diffRise != null || n._diffFall != null)) {
          const up = n._diffRise != null;
          const ay = n._y + (up ? -r * scale - 8 : r * scale + 8);
          ctx.beginPath();
          ctx.moveTo(n._x, ay);
          ctx.lineTo(n._x - 3.5, ay + (up ? 5 : -5));
          ctx.lineTo(n._x + 3.5, ay + (up ? 5 : -5));
          ctx.closePath();
          ctx.fillStyle = up ? 'rgba(95,179,158,.7)' : 'rgba(224,113,106,.65)';
          ctx.fill();
        }
      });

      /* Labels follow the tiers: the primaries are always named, satellites
         only while their system is open or under the cursor. */
      st.nodes.forEach(n => {
        if (!nodeVisible(st, n)) return;
        const named = n.is_self || isPrimary(n)
          || (isLoop(n) && !n._host)
          || st.hover === n.id || st.selected === n.id || st.focusId === n.id
          || (n._host && systemOpen(st, st.byId[n._host]));
        if (!named) return;
        let alpha = zoneAlpha(st, n);
        if (lens && !lens.has(n.id)) alpha *= 0.18;
        if (st.loopsOnly && !loopish(st, n)) alpha *= 0.12;
        if (alpha < 0.14) return;
        const r = n._r || 8;
        drawLabel(
          n._x + (n._labelSide || 1) * (r + 3),
          n._y + (n._labelDy || 0),
          n.label + (n.is_self ? ' — you' : ''), n._labelSide || 1,
          st.hover === n.id || st.selected === n.id || st.focusId === n.id,
          alpha);
      });
      ctx.restore();
    }

    function frame(now) {
      // Glide survivors toward their post-update layout targets (_tx/_ty set
      // by update()) instead of teleporting — spatial memory stays intact.
      state.nodes.forEach(n => {
        if (n._tx == null) return;
        n._x += (n._tx - n._x) * 0.12;
        n._y += (n._ty - n._y) * 0.12;
        if (Math.abs(n._tx - n._x) + Math.abs(n._ty - n._y) < 0.4) {
          n._x = n._tx; n._y = n._ty;
          delete n._tx; delete n._ty;
        }
      });
      draw(state, now);
      state.raf = requestAnimationFrame(frame);
    }

    state.byId = {};
    state.nodes.forEach(n => { state.byId[n.id] = n; });
    resize();
    renderInsights();
    renderLegend();
    window.addEventListener('resize', resize);
    state.raf = requestAnimationFrame(frame);

    let onKey = null, onKeyUp = null;
    if (isThumb) {
      canvas.style.cursor = 'pointer';
      if (wrap) wrap.style.cursor = 'pointer';
      const goMem = () => { window.location.href = thumbHref; };
      canvas.addEventListener('click', goMem);
      if (wrap) {
        wrap.addEventListener('click', (e) => {
          if (e.target === wrap || e.target === canvas) goMem();
        });
      }
    } else if (toolbar) {
      toolbar.onclick = (e) => {
        const btn = (e.target && e.target.closest)
          ? e.target.closest('[data-act]') : null;
        const act = btn && btn.getAttribute('data-act');
        if (!act) return;
        const more = toolbar.querySelector('.const-more');
        if (more && more.open && btn.closest('.const-more')) more.open = false;
        if (act === 'loops') {
          state.loopsOnly = !state.loopsOnly;
          btn.classList.toggle('on', state.loopsOnly);
        } else if (act === 'fit') fit();
        else if (act === 'in') { state.cam.z = Math.min(2.6, state.cam.z * 1.12); saveCam(); }
        else if (act === 'out') { state.cam.z = Math.max(0.55, state.cam.z / 1.12); saveCam(); }
        else if (act === 'correct') setEdit(!state.edit);
        else if (act === 'filaments') {
          state.showFilaments = !state.showFilaments;
          const b = toolbar.querySelector('[data-act=filaments]');
          if (b) b.classList.toggle('on', state.showFilaments);
        } else if (act === 'diff') {
          toggleDiffMode();
        } else if (act === 'focus') {
          if (state.focusId) state.focusId = null;
          else if (state.selected || state.hover) state.focusId = state.selected || state.hover;
          const b = toolbar.querySelector('[data-act=focus]');
          if (b) b.classList.toggle('on', !!state.focusId);
        }
      };
    }
    if (!isThumb && insightEl) {
      insightEl.addEventListener('click', (e) => {
        const t = e.target.closest('[data-nid]');
        if (t && t.getAttribute('data-nid')) openEvidence(t.getAttribute('data-nid'));
      });
    }
    if (!isThumb && panel) {
      panel.addEventListener('click', async (e) => {
        const t = e.target;
        if (!t) return;
        const act = t.getAttribute('data-act');
        if (act === 'close-ev') {
          flushDwell();
          state.selected = null; state.linkFrom = null;
          panel.hidden = true; panel.innerHTML = ''; return;
        }
        if (act === 'retry-ev' && state.selected) {
          openEvidence(state.selected);
          return;
        }
        if (act === 'play-moment') {
          const aid = t.getAttribute('data-audio-id');
          const audio = aid && panel.querySelector('[id="' + aid + '"]');
          if (audio) {
            try { audio.play(); } catch (err) {}
            audio.scrollIntoView({block: 'nearest'});
          }
          return;
        }
        if (act === 'do-focus' && state.selected) {
          state.focusId = state.selected;
          const b = toolbar && toolbar.querySelector('[data-act=focus]');
          if (b) b.classList.add('on');
          return;
        }
        if (act === 'do-pin' && state.selected) {
          try { await togglePin(state.selected); }
          catch (err) { panel.insertAdjacentHTML('beforeend',
            '<div class="const-edit-hint" style="color:var(--danger)">'
            + MnemosEsc(err.message || err) + '</div>'); }
          return;
        }
        if (act === 'do-reclassify' && state.selected) {
          const sel = panel.querySelector('.const-kind-select');
          const kind = sel && sel.value;
          if (!kind) return;
          try { await reclassify(state.selected, kind); }
          catch (err) { panel.insertAdjacentHTML('beforeend',
            '<div class="const-edit-hint" style="color:var(--danger)">'
            + MnemosEsc(err.message || err) + '</div>'); }
          return;
        }
        if (act === 'start-link' && state.selected) {
          state.linkFrom = state.selected;
          panel.insertAdjacentHTML('beforeend',
            '<div class="const-edit-hint">Click another node to connect.</div>');
          return;
        }
        const other = t.getAttribute('data-unlink');
        if (other && state.selected) {
          try { await unlinkPair(state.selected, other); }
          catch (err) { panel.insertAdjacentHTML('beforeend',
            '<div class="const-edit-hint" style="color:var(--danger)">'
            + MnemosEsc(err.message || err) + '</div>'); }
          return;
        }
        // Rank component → reveal its evidence refs (same hold-to-reveal vocabulary).
        const rkBtn = t.closest && t.closest('[data-rk]');
        if (rkBtn && rkBtn.getAttribute('data-rk') != null
            && !rkBtn.classList.contains('const-rank-seg')) {
          const i = rkBtn.getAttribute('data-rk');
          const ev = panel.querySelector('[data-rk-ev="' + i + '"]');
          if (ev) ev.hidden = !ev.hidden;
          return;
        }
      });
    }

    if (!isThumb) {
    let drag = null;
    let lastTap = 0;
    canvas.addEventListener('pointerdown', (e) => {
      drag = { x: e.clientX, y: e.clientY, cx: state.cam.x, cy: state.cam.y };
      canvas.setPointerCapture(e.pointerId);
    });
    canvas.addEventListener('pointermove', (e) => {
      const rect = canvas.getBoundingClientRect();
      const mx = (e.clientX - rect.left - rect.width / 2 - state.cam.x) / state.cam.z
        + state.w / 2;
      const my = (e.clientY - rect.top - rect.height / 2 - state.cam.y) / state.cam.z
        + state.h / 2;
      let hit = null;
      state.nodes.forEach(n => {
        if (!nodeVisible(state, n)) return;
        const dx = n._x - mx, dy = n._y - my;
        const hitR = Math.max(20, (n._r || 8) + 12);
        if (dx * dx + dy * dy < hitR * hitR) hit = n.id;
      });
      state.hover = hit;
      canvas.style.cursor = hit ? 'pointer' : (drag ? 'grabbing' : 'grab');
      if (hit) renderTip(state.byId[hit], e.clientX, e.clientY);
      else if (tip) tip.hidden = true;
      if (drag) {
        state.cam.x = drag.cx + (e.clientX - drag.x);
        state.cam.y = drag.cy + (e.clientY - drag.y);
        clampCam();
      }
    });
    canvas.addEventListener('pointerup', async (e) => {
      const tap = drag && Math.hypot(e.clientX - drag.x, e.clientY - drag.y) < 5;
      const now = performance.now();
      const dbl = tap && (now - lastTap) < 320;
      if (tap) lastTap = now;
      if (tap && state.hover) {
        if (dbl) {
          state.focusId = state.focusId === state.hover ? null : state.hover;
          const b = toolbar && toolbar.querySelector('[data-act=focus]');
          if (b) b.classList.toggle('on', !!state.focusId);
        } else if (state.edit) {
          if (state.linkFrom && state.linkFrom !== state.hover) {
            const a = state.linkFrom, b = state.hover;
            const already = state.edges.some(ed =>
              (ed.source === a && ed.target === b) || (ed.source === b && ed.target === a));
            try {
              if (already) await unlinkPair(a, b);
              else await linkPair(a, b);
              state.selected = b;
            } catch (err) {
              if (panel) panel.insertAdjacentHTML('beforeend',
                '<div class="const-edit-hint" style="color:var(--danger)">'
                + MnemosEsc(err.message || err) + '</div>');
            }
          } else {
            state.selected = state.hover;
            state.linkFrom = null;
            openEvidence(state.hover);
          }
        } else if (state.detailMode) {
          state.selected = state.selected === state.hover ? null : state.hover;
          if (state.onSelect) {
            state.onSelect(state.selected ? state.byId[state.selected] : null);
          }
        } else {
          openEvidence(state.hover);
          if (state.onSelect) state.onSelect(state.byId[state.hover]);
        }
      }
      if (drag) saveCam();
      drag = null;
    });
    canvas.addEventListener('wheel', (e) => {
      e.preventDefault();
      state.cam.z *= (e.deltaY > 0 ? 0.97 : 1.03);
      saveCam();
    }, { passive: false });

    onKey = (e) => {
      if (e.target && /input|textarea|select/i.test(e.target.tagName)) return;
      if (e.key === 'Escape') {
        state.focusId = null; state.selected = null; state.edit = false;
        if (panel) { panel.hidden = true; panel.innerHTML = ''; }
        if (toolbar) {
          ['focus', 'correct', 'filaments'].forEach(a => {
            const b = toolbar.querySelector('[data-act=' + a + ']');
            if (b) b.classList.remove('on');
          });
        }
        state.showFilaments = false;
      } else if (e.key === 'f' || e.key === 'F') {
        if (state.hover || state.selected) {
          state.focusId = state.focusId ? null : (state.selected || state.hover);
          const b = toolbar && toolbar.querySelector('[data-act=focus]');
          if (b) b.classList.toggle('on', !!state.focusId);
        }
      } else if (e.key === 'Alt') {
        state.showFilaments = true;
      }
    };
    onKeyUp = (e) => {
      if (e.key === 'Alt' && !(toolbar && toolbar.querySelector('[data-act=filaments].on'))) {
        state.showFilaments = false;
      }
    };
    window.addEventListener('keydown', onKey);
    window.addEventListener('keyup', onKeyUp);
    }

    return {
      update(data2) {
        // Live refresh as a DIFF, not a rebuild: survivors keep their spot and
        // glide to any new position; newcomers arrive with a ring; selection,
        // hover, focus, and the camera all survive. This is what the 4s
        // version poll calls — it must never yank the map out from under the
        // user (destroy+mount did exactly that).
        const prevById = state.byId || {};
        const had = state.nodes.length > 0;
        const nodes = (data2 && data2.nodes) || [];
        const born = performance.now();
        const oldPos = {};
        nodes.forEach(n => {
          const old = prevById[n.id];
          if (old) {
            oldPos[n.id] = { x: old._x, y: old._y };
            n._labelDy = old._labelDy || 0;
            n._labelSide = old._labelSide || 1;
            n._born = old._born;
          } else if (had) {
            n._born = born;
          }
        });
        state.nodes = nodes;
        state.edges = (data2 && data2.edges) || [];
        state.insights = (data2 && data2.insights) || [];
        state.breakdowns = (data2 && data2.breakdowns) || state.breakdowns || {};
        state.byId = {};
        nodes.forEach(n => { state.byId[n.id] = n; });
        if (state.selected && !state.byId[state.selected]) {
          state.selected = null;
          if (panel) { panel.hidden = true; panel.innerHTML = ''; }
        }
        if (state.focusId && !state.byId[state.focusId]) state.focusId = null;
        if (state.hover && !state.byId[state.hover]) state.hover = null;
        if (state.linkFrom && !state.byId[state.linkFrom]) state.linkFrom = null;
        refield(state);
        nodes.forEach(n => {
          const o = oldPos[n.id];
          if (!o) return;   // newcomer: appears at its layout spot, ringed
          n._tx = n._x; n._ty = n._y;
          n._x = o.x; n._y = o.y;
          if (REDUCED_MOTION
              || Math.abs(n._tx - n._x) + Math.abs(n._ty - n._y) < 1) {
            n._x = n._tx; n._y = n._ty;
            delete n._tx; delete n._ty;
          }
        });
        if (!state._fittedOnce) {
          fit();
          state._fittedOnce = true;
        }
        renderInsights();
        renderLegend();
      },
      fit,
      softHighlight(ids) {
        state.softIds = (ids && ids.length) ? new Set(ids) : null;
      },
      emphasize(ids) {
        state.emphasizeIds = (ids && ids.length) ? new Set(ids) : null;
        if (ids && ids.length) {
          state.focusId = ids[0];
          const b = toolbar && toolbar.querySelector('[data-act=focus]');
          if (b) b.classList.add('on');
        }
      },
      openEvidence,
      select(id) {
        state.selected = id || null;
        if (state.onSelect) {
          state.onSelect(id ? state.byId[id] : null);
        }
      },
      focus(id) {
        state.focusId = (state.focusId === id) ? null : (id || null);
        const b = toolbar && toolbar.querySelector('[data-act=focus]');
        if (b) b.classList.toggle('on', !!state.focusId);
      },
      setRange(cutoffTs) {
        state.rangeCutoff = cutoffTs || null;
      },
      node(id) { return state.byId[id]; },
      /* What the field decided about a node: which zone it sits in, what its
         system holds, and who hosts it. The inspector speaks from this. */
      info(id) {
        const n = state.byId[id];
        if (!n) return null;
        return {
          zone: n._zone || 'peripheral',
          loops: n._loops || 0,
          members: (n._members || []).slice(),
          host: n._host || null,
          primary: isPrimary(n) || !!n.is_self,
        };
      },
      data() { return { nodes: state.nodes, edges: state.edges }; },
      destroy() {
        cancelAnimationFrame(state.raf);
        window.removeEventListener('resize', resize);
        if (onKey) window.removeEventListener('keydown', onKey);
        if (onKeyUp) window.removeEventListener('keyup', onKeyUp);
        if (toolbar && toolbar.parentElement) toolbar.remove();
        if (panel && panel.parentElement) panel.remove();
        if (tip && tip.parentElement) tip.remove();
        if (insightEl && insightEl.parentElement) insightEl.remove();
      }
    };
  }
};

window.MnemosParsePacket = function (text) {
  if (!text || text.indexOf('APPROVAL NEEDED') < 0) return null;
  const lines = text.split(/\r?\n/);
  const first = lines[0] || '';
  const summary = first.replace(/^APPROVAL NEEDED\s*—\s*/i, '').trim();
  const fields = {};
  let cur = null, buf = [];
  const flush = () => {
    if (cur) fields[cur] = buf.join('\n').trim();
    cur = null; buf = [];
  };
  const map = { action: 'action', to: 'to', subject: 'subject', body: 'body',
    why: 'why', source: 'source', details: 'details' };
  for (let i = 1; i < lines.length; i++) {
    const m = lines[i].match(/^(Action|To|Subject|Body|Why|Source|Details)\s*:\s*(.*)$/i);
    if (m) {
      flush();
      cur = map[m[1].toLowerCase()];
      buf = [m[2] || ''];
    } else if (/^Reply '/i.test(lines[i])) {
      flush();
    } else if (cur) {
      buf.push(lines[i]);
    }
  }
  flush();
  return { kind: 'approval', summary, fields };
};

window.MnemosRenderFolio = function (packet, opts) {
  opts = opts || {};
  const f = (packet && packet.fields) || {};
  const editable = !!opts.editable;
  const preview = (f.content && String(f.content).length > 480)
    ? String(f.content).slice(0, 480) + '\n…'
    : (f.content || '');
  const rows = [
    ['Action', 'action', f.action],
    ['Path', 'path', f.path],
    ['To', 'to', f.to],
    ['Subject', 'subject', f.subject],
    ['Body', 'body', f.body],
    ['Preview', 'content', preview],
    ['Why', 'why', f.why],
    ['Source', 'source', f.source],
    ['Details', 'details', f.details],
  ].filter(r => r[2]);
  let html = '<div class="folio approval-folio" data-folio="1">';
  html += '<div class="serif-title" style="font-size:1.35rem;margin:0 0 4px 18px">Approval</div>';
  html += '<div style="margin:0 0 14px 18px;color:var(--mut);font-size:13px">'
    + esc(packet.summary || '') + '</div>';
  html += '<div class="ink-divider" style="margin-left:18px;margin-right:8px"></div>';
  rows.forEach(([label, key, val]) => {
    html += '<div style="margin:10px 0 10px 18px">';
    html += '<div class="pv-label" style="font:11px var(--sans);'
      + 'letter-spacing:.05em;color:var(--mut)">' + label + '</div>';
    if (editable && (key === 'body' || key === 'subject')) {
      html += '<textarea data-field="' + key + '" style="width:100%;margin-top:4px;'
        + 'min-height:' + (key === 'body' ? '88' : '40') + 'px;font:inherit;'
        + 'border:1px solid var(--line);border-radius:10px;padding:8px 10px;'
        + 'background:var(--bg-elev);color:var(--text);resize:vertical">'
        + esc(val) + '</textarea>';
    } else {
      html += '<div style="margin-top:3px;white-space:pre-wrap">' + esc(val) + '</div>';
    }
    html += '</div>';
  });
  if (opts.meta) {
    html += '<div style="margin:12px 0 0 18px;font:12px var(--mono);color:var(--mut)">'
      + esc(opts.meta) + '</div>';
  }
  const pid = (packet && packet.packet_id != null) ? String(packet.packet_id) : '';
  const phash = (packet && packet.payload_hash) ? String(packet.payload_hash) : '';
  html += '<div class="seal-row" style="display:flex;gap:10px;margin:16px 0 4px 18px;'
    + 'align-items:center;flex-wrap:wrap" data-packet-id="' + esc(pid)
    + '" data-payload-hash="' + esc(phash) + '">';
  html += '<button type="button" class="seal-approve">Hold to seal</button>';
  html += '<button type="button" class="seal-cancel btn" style="background:transparent;'
    + 'border:1px solid var(--line);border-radius:10px;padding:8px 14px;cursor:pointer">'
    + 'Cancel</button>';
  html += '</div></div>';
  return html;
};

/* Response document renderer — semantic sections → editorial UI */
window.MnemosResponse = {
  CARD: {
    key_idea: {label: 'Key idea', icon: '◆'},
    concept: {label: 'Concept', icon: '◆'},
    definition: {label: 'Definition', icon: '◇'},
    example: {label: 'Example', icon: '▸'},
    warning: {label: 'Warning', icon: '!'},
    mistake: {label: 'Common mistake', icon: '!'},
    note: {label: 'Note', icon: '·'},
    summary: {label: 'Summary', icon: '◎'},
    confirmed: {label: 'Confirmed', icon: '✓'},
    likely: {label: 'Likely', icon: '~'},
    conflicting: {label: 'Conflicting', icon: '≠'},
    missing: {label: 'Missing', icon: '?'},
  },
  emphasize(text, terms) {
    let html = esc(text || '');
    const list = (terms || []).slice().sort((a, b) => b.length - a.length);
    list.forEach((term) => {
      if (!term || term.length < 3) return;
      if (term.split(/\s+/).length > 4) return;
      const safe = term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
      try {
        const re = new RegExp('\\b(' + safe + ')\\b', 'i');
        html = html.replace(re, '<span class="rd-em">$1</span>');
      } catch (e) {}
    });
    html = html.replace(/\$([^$]+)\$/g, function (_m, tex) {
      return '<span class="rd-inline-math" data-tex="' + esc(tex) + '"></span>';
    });
    return html;
  },
  card(type, text, title) {
    const meta = this.CARD[type] || this.CARD.note;
    const label = title || meta.label;
    return '<aside class="rd-card ' + type + '" role="note">'
      + '<div class="rd-card-head"><span class="rd-card-icon" aria-hidden="true">'
      + meta.icon + '</span>' + esc(label) + '</div>'
      + '<p class="rd-card-body">' + this.emphasize(text, []) + '</p></aside>';
  },
  sectionHtml(sec) {
    const t = (sec && sec.type) || 'explanation';
    if (t === 'title') {
      return '<h2 class="rd-title">' + esc(sec.text || '') + '</h2>';
    }
    if (t === 'heading') {
      return '<h3 class="rd-heading">' + esc(sec.text || '') + '</h3>';
    }
    if (t === 'takeaway') {
      return '<p class="rd-takeaway"><span class="rd-kicker">Takeaway</span>'
        + this.emphasize(sec.text || '', sec.emphasis) + '</p>';
    }
    if (t === 'formula') {
      const tex = sec.tex || sec.text || '';
      return '<div class="rd-formula" role="math" aria-label="' + esc(tex) + '" data-display="1" data-tex="'
        + esc(tex) + '"></div>';
    }
    if (t === 'code') {
      return '<pre class="rd-code" tabindex="0"><code>' + esc(sec.text || '') + '</code></pre>';
    }
    if (t === 'list' || t === 'next_actions'
        || t === 'confirmed' || t === 'likely'
        || t === 'conflicting' || t === 'missing') {
      const items = sec.items || [];
      if (!items.length) return '';
      if (t === 'next_actions') {
        return '<div class="rd-card summary"><div class="rd-card-head">'
          + '<span class="rd-card-icon" aria-hidden="true">→</span>Next steps</div>'
          + '<ul class="rd-list">' + items.map(i => '<li>' + esc(i) + '</li>').join('')
          + '</ul></div>';
      }
      if (t === 'confirmed' || t === 'likely' || t === 'conflicting' || t === 'missing') {
        const meta = this.CARD[t] || this.CARD.note;
        const label = sec.title || meta.label;
        return '<aside class="rd-card ' + t + '" role="note">'
          + '<div class="rd-card-head"><span class="rd-card-icon" aria-hidden="true">'
          + meta.icon + '</span>' + esc(label) + '</div>'
          + '<ul class="rd-list">' + items.map(i => '<li>' + esc(i) + '</li>').join('')
          + '</ul></aside>';
      }
      return '<ul class="rd-list">' + items.map(i => '<li>' + esc(i) + '</li>').join('') + '</ul>';
    }
    if (this.CARD[t]) {
      return this.card(t, sec.text || '', sec.title);
    }
    return '<p class="rd-p">' + this.emphasize(sec.text || '', sec.emphasis) + '</p>';
  },
  groundingHtml(g) {
    if (!g || !g.total) return '';
    let html = '<details class="rd-grounding"><summary>Sources</summary>';
    (g.groups || []).forEach((grp) => {
      html += '<div class="rd-g-group"><div class="rd-g-label">'
        + esc(grp.label || 'Source')
        + (grp.n > 1 ? (' · ' + grp.n) : '') + '</div>';
      (grp.items || []).forEach((it) => {
        html += '<div class="rd-g-item">— ' + esc(it) + '</div>';
      });
      html += '</div>';
    });
    html += '</details>';
    return html;
  },
  actionsHtml(actions) {
    if (!actions || !actions.length) return '';
    let html = '<div class="rd-actions" role="group" aria-label="Continue learning">';
    actions.forEach((a) => {
      html += '<button type="button" data-rd-action="' + esc(a.id || '') + '" data-rd-prompt="'
        + esc(a.prompt || a.label || '') + '">' + esc(a.label || a.id) + '</button>';
    });
    html += '</div>';
    return html;
  },
  render(compiled, opts) {
    opts = opts || {};
    if (!compiled || !compiled.sections || !compiled.sections.length) return '';
    let html = '<article class="rd" data-rd="' + esc(compiled.id || '') + '">';
    compiled.sections.forEach((sec) => { html += this.sectionHtml(sec); });
    if (opts.includeGrounding !== false) {
      html += this.groundingHtml(compiled.grounding);
    }
    html += this.actionsHtml(compiled.actions);
    html += '</article>';
    return html;
  },
  typeset(root) {
    if (!root) return;
    const paint = () => {
      root.querySelectorAll('.rd-formula[data-tex], .rd-inline-math[data-tex]').forEach((el) => {
        if (el.getAttribute('data-done')) return;
        const tex = el.getAttribute('data-tex') || '';
        const display = el.classList.contains('rd-formula');
        try {
          if (window.katex) {
            window.katex.render(tex, el, {
              throwOnError: false,
              displayMode: display,
              output: 'html',
            });
            el.setAttribute('data-done', '1');
          } else {
            el.textContent = tex;
          }
        } catch (e) {
          el.textContent = tex;
        }
      });
    };
    if (window.katex) paint();
    else setTimeout(paint, 120);
  },
  bindActions(root, sendFn) {
    if (!root || typeof sendFn !== 'function') return;
    root.querySelectorAll('[data-rd-prompt]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const prompt = btn.getAttribute('data-rd-prompt') || '';
        if (prompt) sendFn(prompt);
      });
    });
  },
  mount(host, compiled, opts) {
    if (!host || !compiled) return false;
    host.classList.add('rd-host');
    host.innerHTML = this.render(compiled, opts);
    this.typeset(host);
    if (opts && opts.onAction) this.bindActions(host, opts.onAction);
    return true;
  }
};

/* Field SSE — push channel for /field/stream (A3). Polling remains fallback. */
window.MnemosFieldStream = {
  _es: null,
  _cb: null,
  _debounce: null,
  connect(onChange) {
    this._cb = onChange;
    this.disconnect();
    if (typeof EventSource === 'undefined') return false;
    try {
      const es = new EventSource('/field/stream');
      this._es = es;
      es.addEventListener('change', (ev) => {
        let data = {};
        try { data = JSON.parse(ev.data || '{}'); } catch (e) {}
        if (typeof this._cb !== 'function') return;
        // Debounce bursts (wm + version can fire back-to-back).
        if (this._debounce) clearTimeout(this._debounce);
        this._debounce = setTimeout(() => {
          this._debounce = null;
          try { this._cb(data); } catch (e) {}
        }, 180);
      });
      es.onerror = () => { /* EventSource reconnects; poll fallback stays */ };
      return true;
    } catch (e) {
      return false;
    }
  },
  disconnect() {
    if (this._debounce) { clearTimeout(this._debounce); this._debounce = null; }
    if (this._es) {
      try { this._es.close(); } catch (e) {}
      this._es = null;
    }
  },
  connected() { return !!(this._es); }
};

/* Chat SSE — push channel for /chat/stream (S-1). Polling remains fallback. */
window.MnemosChatStream = {
  _es: null,
  _cb: null,
  connect(onChange) {
    this._cb = onChange;
    this.disconnect();
    if (typeof EventSource === 'undefined') return false;
    try {
      const es = new EventSource('/chat/stream');
      this._es = es;
      es.addEventListener('change', () => {
        if (typeof this._cb === 'function') {
          try { this._cb(); } catch (e) {}
        }
      });
      es.onerror = () => { /* EventSource reconnects; poll fallback stays */ };
      return true;
    } catch (e) {
      return false;
    }
  },
  disconnect() {
    if (this._es) {
      try { this._es.close(); } catch (e) {}
      this._es = null;
    }
  },
  connected() { return !!(this._es); }
};

/* Today — thin helpers for the dashboard (offers stay on agent_bridge). */
window.MnemosShell = {
  async state(limit) {
    const r = await fetch('/today/state?limit=' + (limit || 28));
    return r.json();
  },
  async answer(accept) {
    const r = await fetch('/today/offer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({accept: !!accept}),
    });
    return r.json();
  }
};

/* Capture privacy — consent gate + persistent recording indicator. */
window.MnemosCapture = {
  _timer: null,
  _state: null,
  _voice: null,
  _SOURCES: [
    {key:'mic', label:'Mic', warn:''},
    {key:'webcam', label:'Camera', warn:''},
    {key:'screen', label:'Screen',
     warn:'Periodic screenshots of whatever is on your display (not mouse clicks).'},
    {key:'clicks', label:'Mouse clicks',
     warn:'Logs click coordinates + a small crop. Off by default — noisy.'},
    {key:'system_audio', label:'System audio',
     warn:'Transcribes what the computer plays — including meeting participants.'},
    {key:'save_audio', label:'Save audio clips',
     warn:'Keeps WAV files on disk for provenance (optional).'}
  ],
  async status() {
    const r = await fetch('/capture/status');
    return r.json();
  },
  async saveConsent(sources) {
    const r = await fetch('/capture/consent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(Object.assign({consented: true}, sources || {})),
    });
    return r.json();
  },
  async revoke() {
    const r = await fetch('/capture/consent', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({consented: false}),
    });
    return r.json();
  },
  async pause(source) {
    const r = await fetch('/capture/pause', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source: source}),
    });
    return r.json();
  },
  async resume(source) {
    const r = await fetch('/capture/resume', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({source: source}),
    });
    return r.json();
  },
  async voiceStatus() {
    const r = await fetch('/speak/status');
    return r.json();
  },
  async setMuted(muted) {
    const r = await fetch('/speak/mute', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({muted: !!muted}),
    });
    return r.json();
  },
  async toggleVoice() {
    const v = this._voice || {};
    if (v.enabled === false) return;
    try {
      this._voice = await this.setMuted(!v.muted);
      this.render();
    } catch (e) {}
  },
  openPrivacy() {
    const el = document.getElementById('mnemosPrivacy');
    if (!el) return;
    const src = ((this._state && this._state.consent && this._state.consent.sources)
      || {});
    // WS-F: a source this OS cannot run is shown disabled with the reason,
    // not hidden — a Mac tester comparing notes with a Windows colleague
    // should be able to see the difference is expected, not a broken install.
    const support = ((this._state && this._state.support) || {}).sources || {};
    this._SOURCES.forEach((s) => {
      const box = document.getElementById('pv_' + s.key);
      if (!box) return;
      const cap = support[s.key] || {};
      const blocked = cap.available === false;
      box.checked = !blocked && !!src[s.key];
      box.disabled = blocked;
      const row = box.closest('label.pv-src');
      if (row) row.style.opacity = blocked ? '0.55' : '';
      const note = row && row.querySelector('span');
      if (note && cap.reason) note.textContent = cap.reason;
    });
    const ret = (this._state && this._state.meeting_mode
      && this._state.meeting_mode.default_retention) || 'transcript_only';
    const t = document.getElementById('pv_ret_transcript');
    const r = document.getElementById('pv_ret_receipts');
    if (t) t.checked = ret !== 'keep_receipts';
    if (r) r.checked = ret === 'keep_receipts';
    const voiceBox = document.getElementById('pv_voice');
    if (voiceBox) {
      const v = this._voice || {};
      voiceBox.disabled = v.enabled === false;
      voiceBox.checked = v.enabled !== false && !v.muted;
    }
    // Hosted only: the sheet is where capture actually starts, because the
    // server has no devices of its own.
    const dockRow = document.getElementById('pvDockRow');
    if (dockRow) {
      dockRow.hidden = !(this._state && this._state.headless);
      const hint = document.getElementById('pvDockHint');
      if (hint && this.dockOpen()) {
        hint.textContent = 'The capture window is already open — this brings '
          + 'it to the front.';
      }
    }
    this.loadSharing();
    this.loadEgress();
    this.loadConnections();
    MnemosDialog.open(el, {
      lockScroll: true,
      focus: '.pv-sheet input:not([disabled]), .pv-sheet button, .pv-sheet [href]',
      onEscape: () => {
        MnemosMemory.set('capturePromptDismissed', true);
        this.closePrivacy();
      },
    });
  },
  closePrivacy() {
    const el = document.getElementById('mnemosPrivacy');
    if (el) MnemosDialog.close(el);
  },
  /* ---- Connections ------------------------------------------------------
     Claude-style: Manage opens a categorized directory; Connect is OAuth
     (or a local MCP probe for customs); chat has a separate per-conversation
     toggle. X-Return-Path brings OAuth back to this page, not onboarding. */
  _connRow(c) {
    const E = window.MnemosEsc;
    const planned = c.availability !== 'ready';
    const id = String(c.id || '');
    const teamBlocked = c.team_allowed === false;
    let dot = 'var(--mut)', state = 'Not connected', detail = '', acts = '';
    if (planned) {
      state = 'Planned';
      detail = (c.description || 'Not available yet.').slice(0, 140);
    } else if (teamBlocked) {
      state = 'Team locked';
      detail = 'An owner must enable this connector for your team first.';
    } else if (!c.configured) {
      state = 'Unavailable';
      detail = c.kind === 'custom'
        ? 'URL missing or invalid.'
        : ('No OAuth client is configured on this install — ask whoever '
           + 'runs this Sparrow to add one.');
    } else if (c.connected) {
      dot = 'var(--ok, #1f7a4d)';
      state = 'Connected';
      const p = c.progress || {};
      detail = p.running
        ? ('Importing… ' + (p.contacts || 0) + ' contacts, ' + (p.events || 0)
           + ' events.')
        : (p.events || p.contacts)
          ? ((p.contacts || 0) + ' contacts, ' + (p.events || 0)
             + ' events imported.')
          : (c.kind === 'custom'
             ? ('Reachable at ' + (c.url || 'MCP URL') + '.')
             : 'Nothing imported yet — Re-sync to pull them in.');
      if (p.error) detail = String(p.error);
      if (c.error) detail = String(c.error);
      acts = (c.kind === 'custom' ? '' :
        '<button type="button" class="pv-btn quiet" data-conn-act="sync"'
        + ' data-conn-id="' + E(id) + '"' + (p.running ? ' disabled' : '')
        + '>Re-sync</button>')
        + '<button type="button" class="pv-btn quiet" data-conn-act="disconnect"'
        + ' data-conn-id="' + E(id) + '">Disconnect</button>';
    } else {
      detail = c.description
        ? String(c.description).slice(0, 140)
        : 'Read-only. You choose what to share on the provider’s own screen.';
      acts = '<button type="button" class="pv-btn" data-conn-act="connect"'
        + ' data-conn-id="' + E(id) + '">Connect</button>';
      if (c.kind === 'custom') {
        acts += '<button type="button" class="pv-btn quiet" data-conn-act="remove"'
          + ' data-conn-id="' + E(id) + '">Remove</button>';
      }
    }
    return '<div style="display:flex;gap:10px;align-items:flex-start;'
      + 'padding:8px 0;border-top:1px solid var(--line)">'
      + '<span aria-hidden="true" style="flex:0 0 auto;width:8px;height:8px;'
      + 'border-radius:50%;margin-top:5px;background:' + dot + '"></span>'
      + '<div style="flex:1 1 auto;min-width:0">'
      + '<b style="color:var(--navy);font-weight:600">' + E(c.label || id)
      + '</b> <span style="font-size:11px">· ' + E(state) + '</span>'
      + '<div style="margin-top:2px">' + E(detail) + '</div></div>'
      + (acts ? '<div style="flex:0 0 auto;display:flex;gap:6px;'
                + 'flex-wrap:wrap">' + acts + '</div>' : '')
      + '</div>';
  },
  async loadConnections() {
    const el = document.getElementById('pvConns');
    if (!el) return;
    let d;
    try {
      d = await MnemosJson('/connectors');
    } catch (e) {
      el.textContent = e.message;
      return;
    }
    this._connCache = d;
    // Privacy sheet shows connected + ready (not the full planned catalog).
    const show = (d.connectors || []).filter((c) =>
      c.connected || c.availability === 'ready');
    const rows = show.map((c) => this._connRow(c)).join('');
    el.innerHTML = rows
      || '<span style="color:var(--mut)">Nothing connected yet — use Manage '
      + 'connectors to browse the directory.</span>';
    const access = document.getElementById('pvToolAccess');
    if (access) access.value = d.tool_access || 'auto';
    const busy = (d.connectors || []).some((c) => (c.progress || {}).running);
    if (busy) {
      clearTimeout(this._connTimer);
      this._connTimer = setTimeout(() => this.loadConnections(), 1500);
    }
    if (window.MnemosConnectors && MnemosConnectors.refreshChat) {
      try { MnemosConnectors.refreshChat(d); } catch (e) {}
    }
  },
  async connectorAction(act, id, btn) {
    const note = document.getElementById('pvConnNote');
    const say = (m) => { if (note) note.textContent = m || ''; };
    const url = '/connectors/' + encodeURIComponent(id) + '/';
    if (act === 'disconnect'
        && !window.confirm('Disconnect ' + id + '? Sparrow deletes its tokens '
                           + 'from this machine. Already-imported people and '
                           + 'events stay — reconnect any time.')) return;
    if (act === 'remove'
        && !window.confirm('Remove this custom connector?')) return;
    if (btn) btn.disabled = true;
    say(act === 'connect' ? 'Opening the sign-in page…' : 'Working…');
    try {
      if (act === 'connect') {
        const r = await MnemosJson(url + 'connect', {
          method: 'POST',
          headers: {
            'X-Public-Base': location.origin,
            'X-Return-Path': location.pathname + location.search,
          },
        });
        if (r.mode === 'redirect' && r.auth_url) {
          window.location = r.auth_url;
          return;
        }
        if (!r.ok) throw new Error(r.error || 'Could not connect.');
        say(r.mode === 'local' ? 'Custom connector reachable.' : 'Connected. Importing…');
        if (r.mode !== 'local') {
          await MnemosJson(url + 'sync', {method: 'POST'});
        }
      } else if (act === 'sync') {
        await MnemosJson(url + 'sync', {method: 'POST'});
        say('Importing…');
      } else if (act === 'disconnect') {
        await MnemosJson(url + 'disconnect', {method: 'POST'});
        say('Disconnected.');
      } else if (act === 'remove') {
        await MnemosJson('/connectors/custom/' + encodeURIComponent(id),
                         {method: 'DELETE'});
        say('Removed.');
      }
    } catch (e) {
      say(e.message);
    }
    if (btn) btn.disabled = false;
    this.loadConnections();
    const modal = document.getElementById('mnemosConnManage');
    if (modal && modal.getAttribute('aria-hidden') === 'false') {
      this.renderConnManage();
    }
  },
  async setToolAccess(mode) {
    try {
      await MnemosJson('/connectors/tool-access', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({tool_access: mode}),
      });
    } catch (e) {
      const note = document.getElementById('pvConnNote');
      if (note) note.textContent = e.message;
    }
    this.loadConnections();
  },
  async openConnManage() {
    let modal = document.getElementById('mnemosConnManage');
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'mnemosConnManage';
      modal.setAttribute('role', 'dialog');
      modal.setAttribute('aria-modal', 'true');
      modal.setAttribute('aria-label', 'Manage connectors');
      modal.setAttribute('aria-hidden', 'true');
      modal.innerHTML =
        '<div class="pv-sheet" style="max-width:520px;max-height:min(88vh,720px);'
        + 'overflow:auto">'
        + '<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">'
        + '<h2 style="flex:1;margin:0;font-size:18px;color:var(--navy)">Connectors</h2>'
        + '<button type="button" class="pv-btn quiet" id="connManageClose">Close</button>'
        + '</div>'
        + '<p style="font-size:12px;color:var(--mut);margin:0 0 12px;line-height:1.45">'
        + 'Browse the directory, connect an account, then enable each service '
        + 'in a conversation from Chat. Connecting alone does not turn it on '
        + 'for every chat.</p>'
        + '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">'
        + '<button type="button" class="pv-btn" id="connAddCustom">+ Add custom connector</button>'
        + '</div>'
        + '<div id="connCustomForm" hidden style="margin-bottom:14px;padding:10px;'
        + 'border:1px solid var(--line);border-radius:8px">'
        + '<b style="font-size:13px;color:var(--navy)">Custom MCP server</b>'
        + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.4">'
        + 'Reached from <em>this</em> Sparrow — localhost and private networks '
        + 'are fine. (Claude custom connectors need a public URL; ours do not.)</p>'
        + '<label style="display:block;font-size:12px;margin:6px 0 2px">Name</label>'
        + '<input id="connCustLabel" type="text" placeholder="My tools" '
        + 'style="width:100%;box-sizing:border-box;padding:7px 9px;border-radius:7px;'
        + 'border:1px solid var(--line);font:inherit;font-size:13px">'
        + '<label style="display:block;font-size:12px;margin:8px 0 2px">URL</label>'
        + '<input id="connCustUrl" type="url" placeholder="http://127.0.0.1:3100" '
        + 'style="width:100%;box-sizing:border-box;padding:7px 9px;border-radius:7px;'
        + 'border:1px solid var(--line);font:inherit;font-size:13px">'
        + '<details style="margin-top:8px"><summary style="font-size:12px;cursor:pointer;'
        + 'color:var(--mut)">Advanced (OAuth client)</summary>'
        + '<label style="display:block;font-size:12px;margin:6px 0 2px">Client ID</label>'
        + '<input id="connCustCid" type="text" '
        + 'style="width:100%;box-sizing:border-box;padding:7px 9px;border-radius:7px;'
        + 'border:1px solid var(--line);font:inherit;font-size:13px">'
        + '<label style="display:block;font-size:12px;margin:6px 0 2px">Client secret</label>'
        + '<input id="connCustSecret" type="password" '
        + 'style="width:100%;box-sizing:border-box;padding:7px 9px;border-radius:7px;'
        + 'border:1px solid var(--line);font:inherit;font-size:13px">'
        + '</details>'
        + '<div style="display:flex;gap:8px;margin-top:10px">'
        + '<button type="button" class="pv-btn" id="connCustSave">Add</button>'
        + '<button type="button" class="pv-btn quiet" id="connCustCancel">Cancel</button>'
        + '</div>'
        + '<div id="connCustNote" style="font-size:12px;color:var(--mut);margin-top:6px"></div>'
        + '</div>'
        + '<div id="connManageBody">loading…</div>'
        + '<div style="margin-top:16px;padding-top:12px;border-top:1px solid var(--line)">'
        + '<b style="font-size:13px;color:var(--navy)">Tool access</b>'
        + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.4">'
        + 'With many connectors, On demand frees context — only toggled-on '
        + 'services count for each chat.</p>'
        + '<select id="connManageAccess" style="font:inherit;font-size:13px;'
        + 'padding:6px 8px;border-radius:7px;border:1px solid var(--line);'
        + 'background:var(--bg-elev);color:var(--text)">'
        + '<option value="auto">Auto — connected services on unless you turn them off</option>'
        + '<option value="on_demand">On demand — only services you enable per chat</option>'
        + '</select>'
        + '</div>'
        + '<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
        + '<b style="font-size:13px;color:var(--navy)">Team</b>'
        + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.4">'
        + 'When enabled, only allowlisted connectors can be connected. Enabling '
        + 'for the team does not grant access — each person still signs in.</p>'
        + '<label class="pv-src"><input type="checkbox" id="connTeamOn">'
        + '<div><b>Limit connectors to a team allowlist</b>'
        + '<span>Owners flip this for shared seats.</span></div></label>'
        + '<div id="connTeamList" style="margin-top:8px"></div>'
        + '<button type="button" class="pv-btn quiet" id="connTeamSave" '
        + 'style="margin-top:8px">Save team policy</button>'
        + '<div id="connTeamNote" style="font-size:12px;color:var(--mut);margin-top:6px"></div>'
        + '</div>'
        + '</div>';
      document.body.appendChild(modal);
      document.getElementById('connManageClose').onclick = (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.closeConnManage();
      };
      document.getElementById('connAddCustom').onclick = () => {
        const f = document.getElementById('connCustomForm');
        if (f) f.hidden = !f.hidden;
      };
      document.getElementById('connCustCancel').onclick = () => {
        const f = document.getElementById('connCustomForm');
        if (f) f.hidden = true;
      };
      document.getElementById('connCustSave').onclick = () => this.saveCustomConnector();
      document.getElementById('connManageAccess').onchange = (e) => {
        this.setToolAccess(e.target.value);
      };
      document.getElementById('connTeamSave').onclick = () => this.saveTeamPolicy();
      modal.addEventListener('click', (e) => {
        // Backdrop (the overlay itself) dismisses — same as Privacy/Ask.
        if (e.target === modal) {
          this.closeConnManage();
          return;
        }
        const btn = e.target.closest('button[data-conn-act]');
        if (btn) this.connectorAction(btn.dataset.connAct, btn.dataset.connId, btn);
        const detail = e.target.closest('[data-conn-detail]');
        if (detail) this.showConnDetail(detail.dataset.connDetail);
      });
    }
    MnemosDialog.open(modal, {
      lockScroll: true,
      focus: '#connManageClose',
      onEscape: () => this.closeConnManage(),
    });
    await this.renderConnManage();
  },
  closeConnManage() {
    const el = document.getElementById('mnemosConnManage');
    if (el) MnemosDialog.close(el);
  },
  async renderConnManage() {
    const body = document.getElementById('connManageBody');
    if (!body) return;
    let d;
    try {
      d = await MnemosJson('/connectors/directory');
    } catch (e) {
      body.textContent = e.message;
      return;
    }
    this._connDir = d;
    const E = window.MnemosEsc;
    const cats = d.categories || [];
    const byCat = {};
    (d.connectors || []).forEach((c) => {
      const k = c.category || 'productivity';
      (byCat[k] = byCat[k] || []).push(c);
    });
    let html = '';
    cats.forEach((cat) => {
      const rows = byCat[cat.id] || [];
      if (!rows.length) return;
      html += '<div style="margin-top:12px"><b style="font-size:12px;color:var(--mut);'
        + 'text-transform:uppercase;letter-spacing:.04em">' + E(cat.label)
        + '</b>';
      rows.forEach((c) => {
        html += '<div style="display:flex;gap:10px;align-items:flex-start;'
          + 'padding:10px 0;border-top:1px solid var(--line)">'
          + '<div style="flex:1;min-width:0">'
          + '<button type="button" data-conn-detail="' + E(c.id) + '" '
          + 'style="all:unset;cursor:pointer;font-weight:600;color:var(--navy)">'
          + E(c.label) + '</button>'
          + '<div style="font-size:12px;color:var(--mut);margin-top:2px;line-height:1.4">'
          + E((c.description || '').slice(0, 160)) + '</div></div>'
          + this._connManageActs(c)
          + '</div>';
      });
      html += '</div>';
    });
    body.innerHTML = html || 'No connectors in the directory.';
    const access = document.getElementById('connManageAccess');
    if (access) access.value = d.tool_access || 'auto';
    const team = d.team || {};
    const teamOn = document.getElementById('connTeamOn');
    if (teamOn) teamOn.checked = !!team.enabled;
    const list = document.getElementById('connTeamList');
    if (list) {
      const allowed = new Set(team.allowed || []);
      list.innerHTML = (d.connectors || []).filter((c) => c.availability === 'ready'
          || c.kind === 'custom').map((c) =>
        '<label class="pv-src" style="margin:4px 0"><input type="checkbox" '
        + 'data-team-id="' + E(c.id) + '"'
        + (allowed.has(c.id) ? ' checked' : '') + '>'
        + '<div><b>' + E(c.label) + '</b></div></label>'
      ).join('') || '<span style="font-size:12px;color:var(--mut)">No ready connectors.</span>';
    }
  },
  _connManageActs(c) {
    const E = window.MnemosEsc;
    const id = E(c.id || '');
    if (c.availability !== 'ready') {
      return '<span style="font-size:11px;color:var(--mut)">Planned</span>';
    }
    if (c.team_allowed === false) {
      return '<span style="font-size:11px;color:var(--mut)">Team locked</span>';
    }
    if (c.connected) {
      return '<button type="button" class="pv-btn quiet" data-conn-act="disconnect"'
        + ' data-conn-id="' + id + '">Disconnect</button>';
    }
    if (!c.configured && c.kind !== 'custom') {
      return '<span style="font-size:11px;color:var(--mut)">Not configured</span>';
    }
    return '<button type="button" class="pv-btn" data-conn-act="connect"'
      + ' data-conn-id="' + id + '">Connect</button>';
  },
  showConnDetail(id) {
    const c = ((this._connDir || {}).connectors || []).find((x) => x.id === id);
    if (!c) return;
    const caps = (c.capabilities || []).map((x) => '• ' + x).join('\n');
    window.alert((c.label || id) + '\n\n' + (c.description || '')
      + (caps ? '\n\n' + caps : ''));
  },
  async saveCustomConnector() {
    const note = document.getElementById('connCustNote');
    const say = (m) => { if (note) note.textContent = m || ''; };
    const label = (document.getElementById('connCustLabel') || {}).value || '';
    const url = (document.getElementById('connCustUrl') || {}).value || '';
    const oauth_client_id = (document.getElementById('connCustCid') || {}).value || '';
    const oauth_client_secret = (document.getElementById('connCustSecret') || {}).value || '';
    say('Adding…');
    try {
      const r = await MnemosJson('/connectors/custom', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({label, url, oauth_client_id, oauth_client_secret}),
      });
      if (!r.ok) throw new Error(r.error || 'Could not add.');
      say('Added. Connect it to verify reachability.');
      const f = document.getElementById('connCustomForm');
      if (f) f.hidden = true;
      await this.renderConnManage();
      this.loadConnections();
    } catch (e) {
      say(e.message);
    }
  },
  async saveTeamPolicy() {
    const note = document.getElementById('connTeamNote');
    const say = (m) => { if (note) note.textContent = m || ''; };
    const enabled = !!(document.getElementById('connTeamOn') || {}).checked;
    const allowed = Array.from(document.querySelectorAll('#connTeamList [data-team-id]'))
      .filter((el) => el.checked)
      .map((el) => el.getAttribute('data-team-id'));
    say('Saving…');
    try {
      await MnemosJson('/connectors/team', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled, allowed}),
      });
      say('Saved.');
      await this.renderConnManage();
      this.loadConnections();
    } catch (e) {
      say(e.message);
    }
  },
  async loadSharing() {
    // Reflect stored state, and say plainly when the weekly ping is impossible
    // (no operator endpoint configured) rather than offering a dead checkbox.
    try {
      const d = await (await fetch('/usage/ping/status')).json();
      const box = document.getElementById('pv_ping');
      if (box) {
        box.checked = !!d.consented;
        box.disabled = !d.url_configured;
      }
      const hint = document.getElementById('pvPingHint');
      if (hint && !d.url_configured) {
        hint.textContent = 'No operator endpoint is configured on this install, '
          + 'so nothing can be sent automatically. Use “Send my stats”.';
      }
    } catch (e) {}
    try {
      const u = await (await fetch('/update/status')).json();
      const box = document.getElementById('pv_update');
      if (box) box.checked = !!u.enabled;
    } catch (e) {}
    try {
      const b = await (await fetch('/export/status')).json();
      const note = document.getElementById('pvBackupNote');
      if (note) {
        note.textContent = b.last_backup_human
          ? ('Last backup: ' + b.last_backup_human)
          : 'No backup taken yet.';
      }
    } catch (e) {}
  },
  // Hosted: save the ticks, then hand them to the dock window — one click
  // from "what may be captured" to actually capturing. The window MUST be
  // opened synchronously in this click or the popup blocker eats it, so the
  // consent POST is awaited after the window exists.
  startDockFromSheet() {
    const want = {};
    [['mic', 'mic'], ['tab', 'system_audio'], ['screen', 'screen']]
      .forEach(([k, key]) => {
        const box = document.getElementById('pv_' + key);
        want[k] = !!(box && box.checked);
      });
    const w = this.openDock(want);
    this.applyPrivacy();
    return w;
  },
  async applyPrivacy() {
    const sources = {};
    this._SOURCES.forEach((s) => {
      const box = document.getElementById('pv_' + s.key);
      sources[s.key] = !!(box && box.checked);
    });
    try {
      this._state = await this.saveConsent(sources);
      const retEl = document.querySelector('input[name="pv_retention"]:checked');
      if (retEl && retEl.value) {
        await fetch('/meeting/retention', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({retention: retEl.value, default: true}),
        });
      }
      this._state = await this.status();
      const voiceBox = document.getElementById('pv_voice');
      if (voiceBox && !voiceBox.disabled) {
        this._voice = await this.setMuted(!voiceBox.checked);
      }
      const pingBox = document.getElementById('pv_ping');
      if (pingBox && !pingBox.disabled) {
        await fetch('/usage/ping/consent', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({consented: !!pingBox.checked}),
        });
      }
      const updBox = document.getElementById('pv_update');
      if (updBox) {
        await fetch('/update/enabled', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({enabled: !!updBox.checked}),
        });
      }
      this.render();
      this.closePrivacy();
    } catch (e) {}
  },
  // --- capture dock (browser-side capture) --------------------------------
  // A MediaStream dies with the document that created it, and this app does
  // full page navigations — so capture lives in its own small window that
  // survives them. Opening it again just focuses the one already open
  // (window.name targeting), so there is never a second dock.
  _DOCK_NAME: 'mnemosCaptureDock',
  _bus() {
    if (this.__bus !== undefined) return this.__bus;
    try { this.__bus = new BroadcastChannel('mnemos-capture'); }
    catch (e) { this.__bus = null; }
    if (this.__bus) {
      this.__bus.onmessage = (ev) => {
        const m = ev.data || {};
        if (m.type === 'dock') {
          this._dockAlive = !!m.alive;
          this._dockSeen = Date.now();
        }
      };
      try { this.__bus.postMessage({type: 'ping'}); } catch (e) {}
    }
    return this.__bus;
  },
  dockOpen() {
    // A dock that pinged in the last 12 s is open (it announces every 4 s).
    return !!this._dockAlive && (Date.now() - (this._dockSeen || 0) < 12000);
  },
  webLive() {
    const web = (this._state || {}).web || {};
    return ['mic', 'tab', 'screen']
      .some((k) => web[k] === 'recording' || web[k] === 'paused');
  },
  openDock(want) {
    // Re-opening a NAMED window navigates it — which would tear down a live
    // stream. While anything is capturing, "open the dock" means focus it.
    if (this.webLive() && this.dockOpen()) {
      this.dockCommand('focus');
      return null;
    }
    const src = want || {};
    const q = [];
    ['mic', 'tab', 'screen'].forEach((k) => { if (src[k]) q.push(k + '=1'); });
    const url = '/capture/dock' + (q.length ? ('?' + q.join('&')) : '');
    let w = null;
    try {
      w = window.open(url, this._DOCK_NAME, 'popup=yes,width=380,height=560');
    } catch (e) {}
    if (!w) {
      // Popup blocked — the full page still works, just not alongside.
      window.location.href = '/capture';
      return null;
    }
    try { w.focus(); } catch (e) {}
    this._bus();
    return w;
  },
  dockCommand(cmd) {
    const bus = this._bus();
    if (!bus) return false;
    try { bus.postMessage({type: 'cmd', cmd: cmd}); return true; }
    catch (e) { return false; }
  },
  // Open the dock armed with whatever the user has already allowed, so the
  // sheet's ticks carry straight into capture instead of being re-chosen.
  openDockFromConsent() {
    const src = ((this._state || {}).consent || {}).sources || {};
    return this.openDock({mic: !!src.mic, tab: !!src.system_audio,
                          screen: !!src.screen});
  },
  async toggle(source) {
    if (!this._state) return;
    // Hosted: capture is browser-side. Pause/resume goes to the dock window
    // that owns the stream (so the browser's own recording indicator clears
    // too); if no dock is open, opening one IS the resume.
    if (this._state.headless) {
      const web = this._state.web || {};
      const key = source === 'system_audio' ? 'tab' : source;
      const state = web[key];
      if (state === 'recording') this.dockCommand('pause');
      else if (state === 'paused') this.dockCommand('resume');
      else if (this.dockOpen()) { this.dockCommand('focus'); }
      else this.openDock({[key]: true});
      setTimeout(() => this.tick(), 900);
      return;
    }
    const running = (this._state.running || {})[source];
    try {
      if (running) await this.pause(source);
      else await this.resume(source);
      this._state = await this.status();
      this.render();
    } catch (e) {
      // Likely 403 — open consent.
      this.openPrivacy();
    }
  },
  mount() {
    if (document.getElementById('mnemosRecBar')) return;
    // Skip on bare launch page until Continue — still mount so consent can show.
    const bar = document.createElement('div');
    bar.id = 'mnemosRecBar';
    bar.setAttribute('aria-live', 'polite');
    if (window.MnemosDock) MnemosDock.add(bar, MnemosDock.PRIORITY.system);
    else document.body.appendChild(bar);

    const modal = document.createElement('div');
    modal.id = 'mnemosPrivacy';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', 'Capture privacy');
    modal.setAttribute('aria-hidden', 'true');
    const rows = this._SOURCES.map((s) =>
      '<label class="pv-src"><input type="checkbox" id="pv_' + s.key + '">'
      + '<div><b>' + s.label + '</b><span>' + (s.warn || 'Optional. Off until you allow it.')
      + '</span></div></label>'
    ).join('');
    modal.innerHTML =
      '<div class="pv-sheet">'
      + '<h2>What may be captured?</h2>'
      + '<p class="pv-lead">Nothing records until you opt in. You can pause any '
      + 'source anytime from the recording indicator.</p>'
      + rows
      + '<div class="pv-warn">System audio and screen can capture other people '
      + 'in meetings or nearby — only enable when everyone expects it.</div>'
      // Hosted: the server has no devices, so capture runs in the browser.
      // Save the ticks and hand them straight to the dock window — the sheet
      // is where the user just decided what may be captured.
      + '<div id="pvDockRow" hidden style="margin-top:12px;display:flex;'
      + 'gap:8px;flex-wrap:wrap;align-items:center">'
      + '<button type="button" class="pv-btn" id="pvStartDock">'
      + 'Save &amp; start capture</button>'
      + '<span id="pvDockHint" style="font-size:12px;color:var(--mut);'
      + 'flex:1 1 200px;line-height:1.4">Opens a small capture window that '
      + 'keeps running while you use the rest of Sparrow.</span>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<b style="font-size:13px;color:var(--navy)">After meetings</b>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Transcript-only = Granola-parity (WAVs deleted, note stays). '
      + 'Keep receipts = playback and dispute-proof memory.</p>'
      + '<label class="pv-src"><input type="radio" name="pv_retention" id="pv_ret_transcript" value="transcript_only">'
      + '<div><b>Transcript-only</b><span>Delete session audio; keep the note.</span></div></label>'
      + '<label class="pv-src"><input type="radio" name="pv_retention" id="pv_ret_receipts" value="keep_receipts">'
      + '<div><b>Keep receipts</b><span>Retain WAVs for “Play the moment”.</span></div></label>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<b style="font-size:13px;color:var(--navy)">AI voice</b>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Spoken replies. Uncheck to keep answers on screen only — you can also mute from the Voice chip.</p>'
      + '<label class="pv-src"><input type="checkbox" id="pv_voice">'
      + '<div><b>Speak replies aloud</b><span>Turn off anytime; it stays off until you turn it back on.</span></div></label>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
      + '<b style="font-size:13px;color:var(--navy);flex:1">Connections</b>'
      + '<button type="button" class="pv-btn quiet" id="pvManageConns">Manage connectors</button>'
      + '</div>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Connect an account here, then enable it per chat from the Chat composer. '
      + 'Sparrow reads only what it needs \u2014 never message bodies. Tokens stay '
      + 'on this machine.</p>'
      + '<label style="display:flex;align-items:center;gap:8px;font-size:12px;'
      + 'color:var(--mut);margin:0 0 8px">Tool access '
      + '<select id="pvToolAccess" style="font:inherit;font-size:12px;padding:4px 6px;'
      + 'border-radius:6px;border:1px solid var(--line);background:var(--bg-elev);'
      + 'color:var(--text)">'
      + '<option value="auto">Auto</option>'
      + '<option value="on_demand">On demand</option>'
      + '</select></label>'
      + '<div id="pvConns">reading&hellip;</div>'
      + '<div id="pvConnNote" style="font-size:12px;color:var(--mut);margin-top:6px"></div>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<b style="font-size:13px;color:var(--navy)">Your data</b>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Everything Sparrow remembers lives in this folder. Take a copy whenever you like.</p>'
      + '<div style="display:flex;gap:8px;flex-wrap:wrap">'
      + '<button type="button" class="pv-btn" id="pvBackup">Back up my memory</button>'
      + '<button type="button" class="pv-btn" id="pvTakeout">Export my data</button>'
      + '</div>'
      + '<div id="pvBackupNote" style="font-size:12px;color:var(--mut);margin-top:6px"></div>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<b style="font-size:13px;color:var(--navy)">Sharing &amp; updates</b>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Usage counting is local: how many searches, meetings and reviews — never '
      + 'what was said, searched or seen. Nothing is sent unless you send it.</p>'
      + '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">'
      + '<button type="button" class="pv-btn" id="pvSendStats">Send my stats</button>'
      + '<button type="button" class="pv-btn quiet" id="pvSeePayload">See exactly what would be sent</button>'
      + '</div>'
      + '<pre id="pvPayload" hidden style="max-height:180px;overflow:auto;font-size:11px;'
      + 'background:var(--ink-04);padding:8px;border-radius:8px;white-space:pre-wrap"></pre>'
      + '<label class="pv-src"><input type="checkbox" id="pv_ping">'
      + '<div><b>Send these stats weekly, automatically</b>'
      + '<span id="pvPingHint">Off by default. Only the payload above, only to the '
      + 'the endpoint your pilot operator configured.</span></div></label>'
      + '<label class="pv-src"><input type="checkbox" id="pv_update">'
      + '<div><b>Check for new versions</b><span>Downloads a small version file. '
      + 'Sends nothing about you — not even which version you run.</span></div></label>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<b style="font-size:13px;color:var(--navy)">What has left this machine</b>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Read from this machine&rsquo;s own logs, not from a promise. Recording, '
      + 'transcription and memory never leave; a frontier model is called only '
      + 'for hard questions, under a hard daily cap.</p>'
      + '<div id="pvEgress" style="font-size:12px;line-height:1.6;color:var(--mut)">'
      + 'reading the log&hellip;</div>'
      + '</div>'
      + '<div class="pv-ret" style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">'
      + '<b style="font-size:13px;color:var(--navy)">Leave nothing behind</b>'
      + '<p style="font-size:12px;color:var(--mut);margin:6px 0 8px;line-height:1.45">'
      + 'Stop every source instantly, or delete everything Sparrow has recorded '
      + 'here. Deleting cannot be undone &mdash; back up first if you might want it back.</p>'
      + '<div style="display:flex;gap:8px;flex-wrap:wrap">'
      + '<button type="button" class="pv-btn" id="pvStopAll">Stop capture now</button>'
      + '<button type="button" class="pv-btn quiet" id="pvWipe" '
      + 'style="color:#8c1d18;border-color:rgba(140,29,24,.4)">Delete everything&hellip;</button>'
      + '</div>'
      + '<div id="pvWipeBox" hidden style="margin-top:10px;padding:10px;border-radius:8px;'
      + 'border:1px solid rgba(140,29,24,.35);background:rgba(140,29,24,.04)">'
      + '<div id="pvWipeWhat" style="font-size:12px;color:var(--mut);line-height:1.5">'
      + 'measuring&hellip;</div>'
      + '<label class="pv-src" style="margin-top:8px"><input type="checkbox" id="pvWipeCreds">'
      + '<div><b>Also remove my key</b><span>You would need a new invite code to '
      + 'use the cloud tier again.</span></div></label>'
      + '<label for="pvWipeConfirm" style="display:block;font-size:12px;color:var(--mut);'
      + 'margin:8px 0 4px">Type <b>DELETE MY MEMORY</b> to confirm:</label>'
      + '<input type="text" id="pvWipeConfirm" autocomplete="off" spellcheck="false" '
      + 'style="width:100%;box-sizing:border-box;padding:7px 9px;border-radius:7px;'
      + 'border:1px solid var(--line);font:inherit;font-size:13px">'
      + '<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">'
      + '<button type="button" class="pv-btn" id="pvWipeGo" disabled '
      + 'style="color:#8c1d18;border-color:rgba(140,29,24,.4)">Delete permanently</button>'
      + '<button type="button" class="pv-btn quiet" id="pvWipeCancel">Cancel</button>'
      + '</div></div>'
      + '<div id="pvWipeNote" style="font-size:12px;color:var(--mut);margin-top:8px"></div>'
      + '</div>'
      + '<div class="pv-actions">'
      + '<button type="button" class="pv-btn quiet" id="pvRevoke">Turn all off</button>'
      + '<button type="button" class="pv-btn quiet" id="pvCancel">Not now</button>'
      + '<button type="button" class="pv-btn go" id="pvSave">Save &amp; start</button>'
      + '</div></div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        MnemosMemory.set('capturePromptDismissed', true);
        this.closePrivacy();
      }
    });
    document.getElementById('pvCancel').onclick = () => {
      MnemosMemory.set('capturePromptDismissed', true);
      this.closePrivacy();
    };    document.getElementById('pvSave').onclick = () => this.applyPrivacy();
    const seeBtn = document.getElementById('pvSeePayload');
    if (seeBtn) seeBtn.onclick = async () => {
      const pre = document.getElementById('pvPayload');
      if (!pre) return;
      if (!pre.hidden) { pre.hidden = true; return; }
      pre.textContent = 'loading…';
      pre.hidden = false;
      try {
        const d = await (await fetch('/usage/preview')).json();
        pre.textContent = d.text || JSON.stringify(d.payload, null, 2);
      } catch (e) { pre.textContent = 'could not read the payload'; }
    };
    const statsBtn = document.getElementById('pvSendStats');
    if (statsBtn) statsBtn.onclick = async () => {
      try {
        const d = await (await fetch('/usage/report', {method: 'POST'})).json();
        // Same affordance as the crash-report zip: the file is on disk, the
        // human decides whether it goes anywhere.
        window.prompt('Saved. Copy this path and email it to the pilot operator:',
                      d.path || '');
      } catch (e) { alert('Could not write the stats file.'); }
    };
    const backupBtn = document.getElementById('pvBackup');
    if (backupBtn) backupBtn.onclick = () => { window.location = '/export/backup'; };
    const takeoutBtn = document.getElementById('pvTakeout');
    if (takeoutBtn) takeoutBtn.onclick = () => { window.location = '/export/takeout'; };
    document.getElementById('pvRevoke').onclick = () => this.stopAll(true);
    const stopBtn = document.getElementById('pvStopAll');
    if (stopBtn) stopBtn.onclick = () => this.stopAll(false);
    const startDock = document.getElementById('pvStartDock');
    if (startDock) startDock.onclick = () => this.startDockFromSheet();
    const wipeBtn = document.getElementById('pvWipe');
    if (wipeBtn) wipeBtn.onclick = () => this.openWipe();
    const wipeCancel = document.getElementById('pvWipeCancel');
    if (wipeCancel) wipeCancel.onclick = () => {
      const box = document.getElementById('pvWipeBox');
      if (box) box.hidden = true;
    };
    const confirmBox = document.getElementById('pvWipeConfirm');
    if (confirmBox) confirmBox.oninput = () => {
      const go = document.getElementById('pvWipeGo');
      // The button stays dead until the phrase matches, so the destructive
      // click can never be the one a mis-aimed Enter key lands on.
      if (go) go.disabled = confirmBox.value.trim().toUpperCase() !== 'DELETE MY MEMORY';
    };
    const wipeGo = document.getElementById('pvWipeGo');
    if (wipeGo) wipeGo.onclick = () => this.runWipe();
    // Rows are re-rendered on every refresh, so delegate rather than rebind.
    const conns = document.getElementById('pvConns');
    if (conns) conns.addEventListener('click', (e) => {
      const btn = e.target.closest('button[data-conn-act]');
      if (btn) this.connectorAction(btn.dataset.connAct, btn.dataset.connId, btn);
    });
    const manage = document.getElementById('pvManageConns');
    if (manage) manage.onclick = () => this.openConnManage();
    const access = document.getElementById('pvToolAccess');
    if (access) access.onchange = () => this.setToolAccess(access.value);
  },
  async stopAll(closeAfter) {
    // Browser-side capture is owned by the dock window: only it can release
    // the mic/screen and clear the browser's own recording indicator, so
    // tell it first, then stop anything device-side.
    this.dockCommand('stop-all');
    // Revoking consent alone leaves the already-running mic thread recording
    // until restart, so this goes through /privacy/stop, which does both.
    try {
      await fetch('/privacy/stop', {method: 'POST'});
    } catch (e) {}
    try {
      this._state = await this.status();
      this.render();
    } catch (e) {}
    if (closeAfter) this.closePrivacy();
  },
  async loadEgress() {
    const el = document.getElementById('pvEgress');
    if (!el) return;
    const when = (t) => {
      if (!t) return 'never';
      try { return new Date(t * 1000).toLocaleString(); } catch (e) { return 'once'; }
    };
    let d;
    try {
      d = await (await fetch('/privacy/egress')).json();
    } catch (e) {
      el.textContent = 'Could not read the log on this machine.';
      return;
    }
    const rows = [];
    const sp = d.spend || {};
    if (sp.ok === false) {
      rows.push(['Cloud spend today', 'ledger unavailable', false]);
    } else if (sp.uncapped) {
      rows.push(['Cloud spend today',
        '$' + Number(sp.spent_usd || 0).toFixed(2) + ' — no cap set on this install', true]);
    } else {
      const spent = Number(sp.spent_usd || 0);
      const cap = Number(sp.budget_usd_day || 0);
      let line = '$' + spent.toFixed(2) + ' of the $' + cap.toFixed(2) + '/day cap';
      if (sp.denied_today) {
        // Don't say "cap reached" — the denial count is for the whole day and
        // would sit next to a spend figure below the cap, contradicting it.
        line += ' — ' + sp.denied_today + ' cloud call'
          + (sp.denied_today === 1 ? ' was' : 's were')
          + ' refused by the cap today and stayed local';
      }
      rows.push(['Cloud spend today', line, spent > 0 || !!sp.denied_today]);
    }
    const cl = d.cloud || {};
    const recent = cl.recent || [];
    if (cl.ok === false) {
      rows.push(['Questions sent to the cloud', 'call log unavailable', false]);
    } else if (!recent.length) {
      rows.push(['Questions sent to the cloud', 'none recorded', false]);
    } else {
      // by_class/max_seen count every call that reached the privacy gate,
      // including the ones it refused — so this must say "reached the gate",
      // never "was sent". Overstating egress on the privacy page is the one
      // error here that costs trust outright.
      let line = recent.length + ' recent call' + (recent.length === 1 ? '' : 's')
        + ' in the log';
      if (cl.refused) {
        line += '; ' + cl.refused + ' call' + (cl.refused === 1 ? ' was' : 's were')
          + ' refused by your privacy rules before anything was sent';
      }
      if (cl.max_seen) {
        line += '. Highest sensitivity class to reach the privacy gate: '
          + cl.max_seen;
      }
      rows.push(['Questions sent to the cloud', line, true]);
    }
    const up = d.usage_ping || {};
    rows.push(['Anonymous usage counts',
      up.consented ? ('on — last sent ' + when(up.last_ping_at))
                   : 'off — nothing sent automatically',
      !!up.consented]);
    const uc = d.update_check || {};
    rows.push(['Version check',
      uc.enabled ? ('on — last checked ' + when(uc.checked_at)
                    + ' (downloads a version file; sends nothing about you)')
                 : 'off',
      !!uc.enabled]);
    el.innerHTML = rows.map((r) =>
      '<div style="display:flex;gap:8px;padding:3px 0">'
      + '<span style="flex:0 0 auto;width:9px;height:9px;border-radius:50%;margin-top:5px;'
      + 'background:' + (r[2] ? 'var(--navy)' : 'var(--ink-20)') + '"></span>'
      + '<span><b style="color:var(--navy);font-weight:600">' + MnemosEsc(r[0])
      + '</b> — ' + MnemosEsc(r[1]) + '</span></div>').join('')
      + '<div style="margin-top:6px;padding-top:6px;border-top:1px dashed var(--line)">'
      + 'Nothing else leaves. There is no Sparrow server holding your memory.</div>';
  },
  async openWipe() {
    const box = document.getElementById('pvWipeBox');
    const what = document.getElementById('pvWipeWhat');
    const confirmBox = document.getElementById('pvWipeConfirm');
    const go = document.getElementById('pvWipeGo');
    if (!box) return;
    box.hidden = false;
    if (confirmBox) confirmBox.value = '';
    if (go) go.disabled = true;
    if (!what) return;
    what.textContent = 'measuring…';
    try {
      const d = await (await fetch('/privacy/wipe/preview')).json();
      const lines = (d.targets || []).filter((t) => t.exists && t.files)
        .map((t) => '<div>' + MnemosEsc(t.label) + ' — ' + MnemosEsc(t.human)
          + ' in ' + t.files + ' file' + (t.files === 1 ? '' : 's') + '</div>');
      what.innerHTML = '<b style="color:#8c1d18">This deletes '
        + MnemosEsc(d.total_human || '0 B') + ' across ' + (d.total_files || 0)
        + ' file' + (d.total_files === 1 ? '' : 's') + ':</b>'
        + (lines.length ? lines.join('') : '<div>Nothing captured yet.</div>')
        + '<div style="margin-top:6px">A deletion receipt is written to '
        + MnemosEsc(d.receipt_dir || '') + '.</div>';
    } catch (e) {
      what.textContent = 'Could not measure what is stored — deleting still works.';
    }
  },
  async runWipe() {
    const confirmBox = document.getElementById('pvWipeConfirm');
    const creds = document.getElementById('pvWipeCreds');
    const note = document.getElementById('pvWipeNote');
    const go = document.getElementById('pvWipeGo');
    if (go) go.disabled = true;
    if (note) note.textContent = 'Stopping capture and deleting…';
    try {
      const r = await fetch('/privacy/wipe', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          confirm: confirmBox ? confirmBox.value : '',
          credentials: !!(creds && creds.checked),
        }),
      });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || 'refused');
      const box = document.getElementById('pvWipeBox');
      if (box) box.hidden = true;
      if (note) {
        note.innerHTML = d.complete
          ? ('<b style="color:var(--navy)">Deleted.</b> Receipt: '
             + MnemosEsc(d.receipt_path || '(not written)')
             + '. You can close Sparrow and delete its folder.')
          : ('<b style="color:#8c1d18">Partly deleted.</b> Some files were in '
             + 'use: ' + MnemosEsc((d.failures || []).slice(0, 3).join('; '))
             + '. Close Sparrow and run the uninstall script.');
      }
      this._state = await this.status();
      this.render();
    } catch (e) {
      if (note) note.textContent = 'Nothing was deleted: ' + (e.message || 'refused') + '.';
      if (go) go.disabled = false;
    }
  },
  render() {
    const bar = document.getElementById('mnemosRecBar');
    if (!bar) return;
    if (!this._state) {
      bar.innerHTML = '<div class="rec-row">'
        + this._metaSepHtml()
        + this._privacyMetaHtml()
        + this._voiceChipHtml()
        + '</div>';
      this._bindBar(bar);
      return;
    }
    const consent = this._state.consent || {};
    const sources = consent.sources || {};
    const running = this._state.running || {};
    const mm = this._state.meeting_mode || {};
    const ms = this._state.meeting_session || {};
    const headless = !!this._state.headless;
    const liveKeys = ['mic', 'webcam', 'screen', 'system_audio'];
    const consented = !!consent.consented;
    const armedKeys = liveKeys.filter((k) => !!sources[k]);
    const liveCount = armedKeys.filter((k) => !!running[k]).length;
    let html = '';
    if (ms.pending) {
      html += '<div class="rec-row meeting">'
        + '<span class="rec-chip meeting-on" title="Waiting on record / skip">'
        + '<span class="dot" aria-hidden="true"></span>'
        + '<span>Meeting · waiting'
        + (ms.title ? (' · ' + String(ms.title).slice(0, 36)) : '')
        + '</span></span></div>';
    } else if (ms.active || mm.active) {
      const title = ms.title || mm.title || '';
      html += '<div class="rec-row meeting">'
        + '<span class="rec-chip meeting-on" title="'
        + (ms.channel_note || 'Meeting session capturing') + '">'
        + '<span class="dot" aria-hidden="true"></span>'
        + '<span>Meeting · capturing'
        + (title ? (' · ' + String(title).slice(0, 40)) : '')
        + '</span></span></div>';
    }
    if (!consented) {
      html += '<div class="rec-row">'
        + '<button type="button" class="rec-consent-btn" id="recOpenPrivacy">'
        + 'Enable capture…</button>'
        + this._metaSepHtml()
        + this._voiceChipHtml()
        + '</div>';
    } else {
      // Status + source toggles | Privacy + Voice. Hosted drives its chips
      // from the browser-capture state the server reports, so they are live
      // on every page even though the stream lives in the dock window.
      const web = this._state.web || {};
      const webLive = ['mic', 'tab', 'screen']
        .filter((k) => web[k] === 'recording' || web[k] === 'paused').length;
      html += '<div class="rec-row">';
      html += this._statusChipHtml(
        headless ? webLive : liveCount, headless, armedKeys.length);
      if (headless) {
        const WEB_SRC = [['mic', 'Mic', 'mic'],
                         ['tab', 'Meeting audio', 'system_audio'],
                         ['screen', 'Screen', 'screen']];
        WEB_SRC.forEach(([k, label, consentKey]) => {
          if (!sources[consentKey]) return;      // not allowed = not offered
          const st = web[k] || 'off';
          const on = st === 'recording';
          html += '<button type="button" class="rec-chip'
            + (on ? '' : ' paused') + '" data-websrc="' + k + '" title="'
            + (on ? 'Pause ' : (st === 'paused' ? 'Resume ' : 'Start '))
            + label + '" aria-pressed="' + (on ? 'true' : 'false') + '">'
            + '<span class="dot" aria-hidden="true"></span>'
            + '<span>' + label + '</span>'
            + '<span class="act">'
            + (on ? 'pause' : (st === 'paused' ? 'resume' : 'start'))
            + '</span></button>';
        });
        if (webLive > 0) {
          html += '<button type="button" class="rec-chip stop-all" id="recStopAll" '
            + 'title="Stop every capture source now">'
            + '<span>Stop all</span><span class="act">stop</span></button>';
        }
      }
      if (!headless) {
        armedKeys.forEach((k) => {
          const on = !!running[k];
          const meta = this._SOURCES.find((s) => s.key === k) || {label: k};
          html += '<button type="button" class="rec-chip' + (on ? '' : ' paused')
            + '" data-src="' + k + '" title="'
            + (on ? 'Pause ' : 'Resume ') + meta.label
            + '" aria-pressed="' + (on ? 'true' : 'false') + '">'
            + '<span class="dot" aria-hidden="true"></span>'
            + '<span>' + meta.label + '</span>'
            + '<span class="act">' + (on ? 'pause' : 'resume') + '</span>'
            + '</button>';
        });
        if (liveCount > 0) {
          html += '<button type="button" class="rec-chip stop-all" id="recStopAll" '
            + 'title="Stop every capture source now">'
            + '<span>Stop all</span><span class="act">stop</span></button>';
        }
      }
      html += this._metaSepHtml();
      html += this._privacyMetaHtml();
      html += this._voiceChipHtml();
      html += '</div>';
    }
    bar.innerHTML = html;
    this._bindBar(bar);
  },
  _bindBar(bar) {
    const openBtn = document.getElementById('recOpenPrivacy');
    if (openBtn) openBtn.onclick = () => this.openPrivacy();
    const stopAllBtn = document.getElementById('recStopAll');
    if (stopAllBtn) stopAllBtn.onclick = () => this.stopAll(false);
    const dockBtn = document.getElementById('recOpenDock');
    if (dockBtn) dockBtn.onclick = () => this.openDockFromConsent();
    bar.querySelectorAll('.rec-chip[data-src]').forEach((btn) => {
      btn.onclick = () => this.toggle(btn.getAttribute('data-src'));
    });
    bar.querySelectorAll('.rec-chip[data-websrc]').forEach((btn) => {
      // Web keys (mic|tab|screen) pass straight through — toggle() maps.
      btn.onclick = () => this.toggle(btn.getAttribute('data-websrc'));
    });
    const voiceBtn = document.getElementById('recVoice');
    if (voiceBtn) voiceBtn.onclick = () => this.toggleVoice();
    try { if (typeof window.MnemosPlaceToast === 'function') window.MnemosPlaceToast(); } catch (e) {}
  },
  _statusChipHtml(liveCount, headless, armedCount) {
    if (headless) {
      if (liveCount > 0) {
        return '<span class="rec-status live" title="'
          + liveCount + ' browser source' + (liveCount === 1 ? '' : 's')
          + ' capturing in the dock window">'
          + '<span class="dot" aria-hidden="true"></span>'
          + '<span>Listening</span></span>';
      }
      return '<button type="button" class="rec-status hosted" id="recOpenDock" '
        + 'title="Open the capture window — it keeps mic, meeting audio and '
        + 'screen running while you use the rest of Sparrow">'
        + '<span class="dot" aria-hidden="true"></span>'
        + '<span>Capture</span></button>';
    }
    if (liveCount > 0) {
      return '<span class="rec-status live" title="'
        + liveCount + ' source' + (liveCount === 1 ? '' : 's') + ' recording">'
        + '<span class="dot" aria-hidden="true"></span>'
        + '<span>Listening</span></span>';
    }
    const tip = armedCount
      ? 'Capture allowed — nothing recording right now'
      : 'Capture allowed — enable a source in Privacy';
    return '<span class="rec-status idle" title="' + tip + '">'
      + '<span class="dot" aria-hidden="true"></span>'
      + '<span>Idle</span></span>';
  },
  _metaSepHtml() {
    return '<span class="rec-sep" aria-hidden="true"></span>';
  },
  _privacyMetaHtml() {
    return '<button type="button" class="rec-meta" id="recOpenPrivacy" '
      + 'title="Privacy controls"><span>Privacy</span></button>';
  },
  _voiceChipHtml() {
    const v = this._voice || {};
    const enabled = v.enabled !== false;
    const on = enabled && !v.muted;
    let title = 'Mute AI voice';
    let cls = 'rec-meta voice-on';
    if (!enabled) {
      title = 'AI voice disabled (QUILL_TTS=off)';
      cls = 'rec-meta';
    } else if (!on) {
      title = 'Unmute AI voice';
      cls = 'rec-meta';
    }
    return '<button type="button" class="' + cls + '" id="recVoice" title="' + title + '"'
      + ' aria-pressed="' + (on ? 'true' : 'false') + '">'
      + '<span class="dot" aria-hidden="true"></span>'
      + '<span>Voice</span>'
      + '</button>';
  },
  async tick() {
    try {
      const [cap, voice] = await Promise.all([
        this.status(),
        this.voiceStatus().catch(() => null),
      ]);
      this._state = cap;
      if (voice) this._voice = voice;
      this.render();
      // First visit: force the consent sheet when nothing is allowed yet.
      if (this._state && this._state.consent
          && !this._state.consent.consented
          && !MnemosMemory.get('capturePromptDismissed', false)) {
        this.openPrivacy();
      }
    } catch (e) {}
  },
  /* Coming back from a connector's OAuth screen. Onboarding runs its own
     handler for this, so only pages that would otherwise ignore it act. */
  oauthReturn() {
    if (/^\/onboarding/.test(location.pathname)) return;
    const qp = new URLSearchParams(location.search);
    const done = qp.get('connected');
    const err = qp.get('oauth_error');
    if (!done && !err) return;
    // Drop the marker so a refresh cannot re-kick the import.
    try {
      const u = new URL(location.href);
      u.searchParams.delete('connected');
      u.searchParams.delete('oauth_error');
      history.replaceState({}, '', u.pathname + (u.search || '') + u.hash);
    } catch (e) {}
    this.openPrivacy();
    const say = (m) => {
      const note = document.getElementById('pvConnNote');
      if (note) note.textContent = m;
    };
    if (err) { say('Could not connect: ' + err); return; }
    say('Connected. Importing…');
    MnemosJson('/connectors/' + encodeURIComponent(done) + '/sync',
               {method: 'POST'})
      .then(() => this.loadConnections())
      .catch((e) => say(e.message));
  },
  start() {
    this.mount();
    this.oauthReturn();
    this._bus();          // ask any open dock to announce itself
    this.tick();
    if (this._timer) clearInterval(this._timer);
    this._timer = setInterval(() => { if (!document.hidden) this.tick(); }, 4000);
  }
};

/* Global Ask (command palette trigger) — lives in the shell on every route.
   The dialog is injected on demand; ⌘K / Ctrl+K opens it anywhere. */
window.MnemosAsk = {
  _el: null,
  _ensure() {
    if (this._el) return this._el;
    const host = document.createElement('div');
    host.id = 'mnemosAsk';
    host.setAttribute('aria-hidden', 'true');
    host.innerHTML =
      '<div class="ask-sheet" role="dialog" aria-modal="true" aria-label="Ask">'
      + '<h3>Ask Sparrow</h3>'
      + '<p class="ask-hint">Orientation only. The full thread lives in Chat.</p>'
      + '<textarea id="mnemosAskBox" placeholder="A question, a task, a follow-up…"></textarea>'
      + '<div class="ask-actions">'
      + '<button type="button" class="btn-quiet" data-ask="dismiss">Dismiss</button>'
      + '<a class="btn-ghost" href="/chat">Full chat</a>'
      + '<button type="button" class="btn-primary" data-ask="send">Send</button>'
      + '</div></div>';
    document.body.appendChild(host);
    host.addEventListener('click', (e) => { if (e.target === host) this.close(); });
    host.querySelector('[data-ask="dismiss"]').onclick = () => this.close();
    host.querySelector('[data-ask="send"]').onclick = () => this.send();
    host.querySelector('textarea').addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); this.send(); }
    });
    this._el = host;
    return host;
  },
  open() {
    const el = this._ensure();
    MnemosDialog.open(el, { lockScroll: true, focus: '#mnemosAskBox', onEscape: () => this.close() });
  },
  close() { if (this._el) MnemosDialog.close(this._el); },
  async send() {
    const box = this._el && this._el.querySelector('textarea');
    const msg = (box && box.value || '').trim();
    if (!msg) return;
    this.close();
    box.value = '';
    try {
      await fetch('/chat', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: msg}),
      });
    } catch (e) {}
    window.location.href = '/chat';
  },
  mount() {
    const trigger = document.getElementById('mnemosAskOpen');
    if (trigger) trigger.onclick = () => this.open();
    if (this._bound) return;
    this._bound = true;
    document.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        this.open();
      }
    });
  }
};

/* Worker status, collapsed to one 9px dot: green idle/healthy, amber degraded.
   Full status lives in the tooltip; pulse only on state change. Pages may add
   a context line (e.g. Memory's "107 shown · 107 total") via setExtra(). */
window.MnemosStatus = {
  _timer: null, _lastState: null, _worker: '', _extra: '',
  _dot() { return document.getElementById('mnemosStatusDot'); },
  setExtra(text) { this._extra = text || ''; this._paint(this._lastState || 'ok'); },
  _paint(state) {
    const dot = this._dot();
    if (!dot) return;
    if (this._lastState !== null && state !== this._lastState) {
      dot.classList.remove('changed');
      void dot.offsetWidth; /* restart the one-shot pulse */
      dot.classList.add('changed');
    }
    this._lastState = state;
    dot.classList.toggle('warn', state === 'warn');
    const bits = [this._worker || 'worker idle'];
    if (this._extra) bits.push(this._extra);
    dot.title = bits.join(' · ');
    dot.setAttribute('aria-label', dot.title);
  },
  async tick() {
    if (document.hidden || !this._dot()) return;
    try {
      const j = await (await fetch('/console/jobs')).json();
      const st = j.stats || {};
      const parts = [];
      if (st.pending) parts.push(st.pending + ' pending');
      if (st.running) parts.push('running');
      if (st.dead) parts.push(st.dead + ' dead');
      else if (st.error) parts.push(st.error + ' err');
      this._worker = parts.length ? ('worker: ' + parts.join(', ')) : 'worker idle';
      this._paint((st.dead || st.error) ? 'warn' : 'ok');
    } catch (e) {
      this._worker = 'worker unreachable';
      this._paint('warn');
    }
  },
  start() {
    if (!this._dot()) return;
    this.tick();
    if (this._timer) clearInterval(this._timer);
    this._timer = setInterval(() => this.tick(), 30000);
  }
};

/* Chat composer: per-conversation connector toggles (Claude-style). */
window.MnemosConnectors = {
  _session: null,
  async load() {
    try {
      this._session = await MnemosJson('/connectors/session');
    } catch (e) {
      this._session = {connectors: [], tool_access: 'auto', active: []};
    }
    this.renderChat();
    return this._session;
  },
  connectedCount() {
    return ((this._session || {}).connectors || []).length;
  },
  openManage() {
    if (window.MnemosCapture && MnemosCapture.openConnManage) {
      MnemosCapture.openConnManage();
    }
  },
  refreshChat(listPayload) {
    if (listPayload && listPayload.session) this._session = listPayload.session;
    this.renderChat();
  },
  renderChat() {
    const panel = document.getElementById('connChatPanel');
    const btn = document.getElementById('connChatBtn');
    if (!panel) return;
    const E = window.MnemosEsc;
    const s = this._session || {};
    const rows = s.connectors || [];
    const access = s.tool_access || 'auto';
    if (btn) {
      const n = (s.active || []).length;
      btn.textContent = n ? ('Connectors (' + n + ')') : 'Connectors';
      btn.title = access === 'on_demand'
        ? 'On demand — enable services for this chat'
        : 'Toggle which connected services this chat may use';
    }
    if (!rows.length) {
      panel.innerHTML = '<div class="conn-chat-empty">No accounts connected yet.</div>'
        + '<button type="button" class="linkish" id="connChatManage">'
        + 'Manage connectors…</button>';
    } else {
      panel.innerHTML = '<div class="conn-chat-head">For this conversation</div>'
        + rows.map((r) =>
          '<label class="conn-chat-row"><input type="checkbox" data-conn-toggle="'
          + E(r.id) + '"' + (r.active ? ' checked' : '') + '>'
          + '<span>' + E(r.label || r.id) + '</span></label>').join('')
        + '<button type="button" class="linkish" id="connChatManage" '
        + 'style="margin-top:8px">Manage connectors…</button>'
        + '<div class="conn-chat-hint">Mode: ' + E(access)
        + '. Connecting in settings is separate from enabling here.</div>';
    }
    const manage = document.getElementById('connChatManage');
    if (manage) manage.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      panel.classList.remove('open');
      if (btn) btn.classList.remove('on');
      panel.hidden = true;
      this.openManage();
    };
    panel.querySelectorAll('[data-conn-toggle]').forEach((el) => {
      el.onchange = async () => {
        try {
          this._session = await MnemosJson('/connectors/session', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: el.getAttribute('data-conn-toggle'),
                                  enabled: !!el.checked}),
          });
          this.renderChat();
        } catch (e) {
          el.checked = !el.checked;
        }
      };
    });
  },
  activeIds() {
    return ((this._session || {}).active || []).slice();
  },
};

document.addEventListener('DOMContentLoaded', () => {
  try { window.MnemosCapture && window.MnemosCapture.start(); } catch (e) {}
  try { window.MnemosAsk && window.MnemosAsk.mount(); } catch (e) {}
  try { window.MnemosStatus && window.MnemosStatus.start(); } catch (e) {}
});
</script>
"""
