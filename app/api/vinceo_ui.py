"""Shared Vinceo UI behaviors — Seal, Bleed, Constellation, Ambient, persistence.

Injected into pages via @@UI_JS@@ (see vinceo_theme.apply).
"""

UI_JS = r"""
<script>
/* Vinceo shared UI — instrument, not chatbot chrome */
window.VinceoMemory = {
  ns: 'vinceo.ui.',
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

window.VinceoReduceMotion = () =>
  window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

/* Shared hold primitive — Seal + Bleed. Copper progress only; no pulse.
   HOLD_MS 700; early release (≥150ms) teaches once via server-persisted tip. */
window.VinceoHold = {
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
      this._live.className = 'vinceo-hold-live';
      this._live.style.cssText = 'position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)';
      document.body.appendChild(this._live);
    }
    this._live.textContent = msg || '';
  },
  showTeach(el, text) {
    if (!el) return;
    let tip = document.getElementById('vinceoHoldTip');
    if (!tip) {
      tip = document.createElement('div');
      tip.id = 'vinceoHoldTip';
      tip.className = 'vinceo-hold-tip';
      tip.style.cssText = 'position:fixed;z-index:80;max-width:240px;padding:8px 10px;'
        + 'font:11px/1.35 "IBM Plex Mono",ui-monospace,monospace;color:var(--navy,#0B1320);'
        + 'background:rgba(255,254,251,.97);border:1px solid rgba(184,115,51,.35);'
        + 'border-radius:8px;box-shadow:0 8px 24px rgba(11,19,32,.1);pointer-events:none';
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
    const reduce = window.VinceoReduceMotion();
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
    el.addEventListener('pointerleave', end);
    el.addEventListener('pointercancel', end);
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

window.VinceoSeal = {
  HOLD_MS: 700,
  bind(btn, { onApprove, onCancel } = {}) {
    if (!btn || btn._sealBound) return;
    btn._sealBound = true;
    btn.classList.add('seal-btn');
    window.VinceoHold.bind(btn, {
      ms: this.HOLD_MS,
      fill: 'ring',
      teach: 'Hold to seal this approval',
      onComplete: () => {
        if (window.VinceoMemory.get('sound', false)) {
          try { window.VinceoInkSound && window.VinceoInkSound(); } catch (e) {}
        }
        onApprove && onApprove();
      },
      onCancel: onCancel,
    });
  }
};

window.VinceoInkSound = function () {
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

window.VinceoBleed = {
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
    window.VinceoHold.bind(el, {
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
window.VinceoEsc = esc;

window.VinceoAmbient = {
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

window.VinceoConstellation = {
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
      softIds: null,       // margin hover soft-highlight set
      emphasizeIds: null,  // margin click emphasis
      raf: 0,
      t0: performance.now(),
      onSelect: opts && opts.onSelect,
      onChange: opts && opts.onChange,
      persistKey: isThumb ? null
        : (((opts && opts.persistKey) || 'constellation.cam') + '.v6'),
      mode: mode,
    };
    const wrap = canvas.parentElement;
    let toolbar = null, panel = null, tip = null, insightEl = null, legendEl = null;
    if (!isThumb) {
      toolbar = wrap && wrap.querySelector('.const-tools');
      if (wrap && !toolbar) {
        toolbar = document.createElement('div');
        toolbar.className = 'const-tools';
        toolbar.innerHTML =
          '<button type="button" data-act="focus" title="Focus mode (F)">Focus</button>'
          + '<button type="button" data-act="filaments" title="Show relationships">Links</button>'
          + '<button type="button" data-act="correct" title="Correct connections">Correct</button>'
          + '<button type="button" data-act="diff" title="Changes since yesterday">Since yesterday</button>'
          + '<button type="button" data-act="out" title="Zoom out">−</button>'
          + '<button type="button" data-act="fit" title="Fit">Fit</button>'
          + '<button type="button" data-act="in" title="Zoom in">+</button>';
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
      if (wrap && !legendEl) {
        legendEl = document.createElement('div');
        legendEl.className = 'const-legend';
        // Self-contained styles so the key looks identical on the console + home
        // pages without touching two CSS blocks. Non-interactive (never eats a drag).
        legendEl.style.cssText = 'position:absolute;left:10px;top:10px;z-index:2;'
          + 'display:flex;flex-wrap:wrap;gap:3px 10px;max-width:min(360px,72%);'
          + 'padding:6px 9px;border-radius:10px;background:rgba(255,254,251,.9);'
          + 'border:1px solid rgba(11,19,32,.1);box-shadow:0 1px 6px rgba(11,19,32,.08);'
          + 'font:11px "Iowan Old Style",Georgia,serif;color:rgba(35,38,43,.82);'
          + 'pointer-events:none';
        wrap.appendChild(legendEl);
      }
    }
    const REDUCED_MOTION = !!(window.matchMedia
      && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    // One hue per node kind (shared by glyphs + legend, so they can't drift).
    // Muted ink family on purpose — distinct at a glance, no neon on the paper.
    const KIND_RGB = {
      person: '30,91,79',       // teal
      org: '55,88,120',         // steel blue — companies, not project diamonds
      project: '43,58,103',     // indigo
      tool: '94,71,129',        // violet
      place: '107,118,58',      // olive
      task: '184,115,51',       // copper
      commitment: '150,54,66',  // burgundy — a promise, not just a to-do
      idea: '90,86,78',         // warm gray
    };
    function kindColor(kind, alpha) {
      return 'rgba(' + (KIND_RGB[kind] || KIND_RGB.idea) + ',' + alpha + ')';
    }
    // Legend key: glyph swatch (matches drawKind) + label, per node kind present.
    const LEGEND = [
      ['person', 'circle', kindColor('person', .95)],
      ['org', 'hex', kindColor('org', .92)],
      ['project', 'diamond', kindColor('project', .9)],
      ['tool', 'round', kindColor('tool', .92)],
      ['place', 'triUp', kindColor('place', .9)],
      ['task', 'triRight', kindColor('task', .95)],
      ['commitment', 'triRight', kindColor('commitment', .95)],
      ['idea', 'dot', kindColor('idea', .55)],
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
      // Only show kinds actually on screen.
      const present = new Set((state.nodes || []).map(n => n.kind || 'idea'));
      const rows = LEGEND.filter(it => present.has(it[0]));
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
        + VinceoEsc(n.node_id || '') + '">' + VinceoEsc(n.text || '') + '</button>'
      ).join('');
    }

    function renderTip(n, clientX, clientY) {
      if (!tip || !n) { if (tip) tip.hidden = true; return; }
      const why = (n.why && n.why.length) ? n.why.join(' · ') : '';
      tip.hidden = false;
      tip.innerHTML = '<strong>' + VinceoEsc(n.label || n.id) + '</strong>'
        + '<span class="const-tip-kind">' + VinceoEsc(n.kind || '') + '</span>'
        + (why ? '<div class="const-tip-why">' + VinceoEsc(why) + '</div>' : '');
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
      let html = '<div class="const-rank">';
      html += '<div class="const-rank-title">Why is this here?</div>';
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
          + '<span class="const-rank-label">' + VinceoEsc(c.label || c.key || '')
          + '</span><span class="const-rank-val">' + VinceoEsc(String(val))
          + '</span></button>';
        html += '<div class="const-rank-ev" hidden data-rk-ev="' + i + '">';
        const refs = c.evidence_refs || [];
        if (refs.length) {
          refs.forEach(r => {
            html += '<div class="const-ev-row"><span class="const-ev-ch">ref</span>'
              + '<div>' + VinceoEsc(String(r)) + '</div></div>';
          });
        } else {
          html += '<div class="const-edit-hint">'
            + VinceoEsc(
              (c.evidence === 'none')
                ? 'Structural signal — no single event'
                : 'No evidence refs')
            + '</div>';
        }
        html += '</div>';
      });
      html += '</div></div>';
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
        const data = await (await fetch('/graph/constellation/evidence?id='
          + encodeURIComponent(id))).json();
        const n = data.node || state.byId[id] || {};
        let html = '<div class="const-edit-head"><strong>' + VinceoEsc(n.label || id)
          + '</strong><button type="button" data-act="close-ev" class="linkish">Close</button></div>';
        html += '<div class="const-tip-kind">' + VinceoEsc(n.kind || '')
          + (n.layer ? ' · ' + VinceoEsc(n.layer) : '') + '</div>';
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
            '<div>' + VinceoEsc(w) + '</div>').join('') + '</div>';
        }
        const allowed = data.allowed_kinds || [];
        if (allowed.length) {
          const cur = data.current_kind || n.kind || '';
          html += '<label class="const-edit-hint" style="display:block;margin-top:8px">Category</label>';
          html += '<select class="const-kind-select" data-act="kind-select">';
          allowed.forEach(k => {
            html += '<option value="' + VinceoEsc(k) + '"'
              + (k === cur ? ' selected' : '') + '>'
              + VinceoEsc(k) + '</option>';
          });
          html += '</select>';
          html += '<button type="button" class="const-link-btn" data-act="do-reclassify" '
            + 'style="margin-top:6px">Save category</button>';
        }
        html += '<div class="const-edit-actions">'
          + '<button type="button" class="const-link-btn" data-act="do-focus">Focus</button>'
          + '<button type="button" class="const-link-btn" data-act="do-pin">'
          + (n.pinned ? 'Unpin' : 'Pin') + '</button></div>';
        const sources = data.sources || [];
        if (sources.length) {
          html += '<div class="const-edit-list"><div class="const-edit-hint">Evidence</div>';
          sources.slice(0, 8).forEach((s, idx) => {
            html += '<div class="const-ev-row"><span class="const-ev-ch">'
              + VinceoEsc(s.modality || s.channel || 'source') + '</span>';
            html += '<div class="const-ev-body">';
            const hl = s.span_highlight;
            if (hl && (hl.match || hl.before || hl.after)) {
              html += '<div class="const-ev-transcript">'
                + VinceoEsc(hl.before || '')
                + '<mark class="span-hl">' + VinceoEsc(hl.match || '') + '</mark>'
                + VinceoEsc(hl.after || '') + '</div>';
            } else {
              html += '<div>' + VinceoEsc((s.text || s.transcript || '').slice(0, 200) || '—') + '</div>';
              if (s.source_span) {
                html += '<div class="const-ev-quote">“' + VinceoEsc(s.source_span) + '”</div>';
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
              html += '<div class="const-edit-row"><span>' + VinceoEsc(x.node.label || x.id)
                + (x.manual ? ' <em>manual</em>' : '')
                + '</span><button type="button" data-unlink="' + VinceoEsc(x.id)
                + '">Remove</button></div>';
            });
            html += '</div>';
          }
        }
        panel.innerHTML = html;
      } catch (err) {
        panel.innerHTML = '<div class="const-edit-hint" style="color:var(--danger)">'
          + VinceoEsc(err.message || err) + '</div>';
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
      const data2 = await (await fetch('/field/state?limit=28')).json();
      state.nodes = data2.nodes || [];
      state.edges = data2.edges || [];
      state.insights = data2.insights || [];
      state.breakdowns = data2.breakdowns || {};
      state.byId = {};
      state.nodes.forEach(n => { state.byId[n.id] = n; });
      layout(state);
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
      const maxPan = Math.min(state.w, state.h) * 0.18;
      state.cam.x = Math.max(-maxPan, Math.min(maxPan, state.cam.x));
      state.cam.y = Math.max(-maxPan, Math.min(maxPan, state.cam.y));
      state.cam.z = Math.max(0.9, Math.min(1.35, state.cam.z));
    };
    const saveCam = () => {
      clampCam();
      if (state.persistKey) window.VinceoMemory.set(state.persistKey, state.cam);
    };
    const fit = () => {
      state.cam = { x: 0, y: 0, z: 1 };
      saveCam();
    };

    const resize = () => {
      const r = canvas.getBoundingClientRect();
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      canvas.width = Math.max(1, Math.floor(r.width * dpr));
      canvas.height = Math.max(1, Math.floor(r.height * dpr));
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      state.w = r.width; state.h = r.height;
      layout(state);
      clampCam();
    };
    const saved = state.persistKey
      ? window.VinceoMemory.get(state.persistKey, null) : null;
    if (saved && typeof saved.z === 'number') {
      state.cam = Object.assign(state.cam, saved);
      clampCam();
    }

    function shortLabel(s) {
      s = String(s || '');
      return s.length > 16 ? s.slice(0, 15) + '…' : s;
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
      const pad = Math.max(22, Math.min(st.w, st.h) * 0.08);
      const maxR = Math.min(st.w, st.h) / 2 - pad;
      // People: stable polar anchors (spatial memory). Others: phyllotaxis seed,
      // then soft attract along edges so related work clusters near people.
      const people = st.nodes.filter(node => node.kind === 'person');
      const others = st.nodes.filter(node => node.kind !== 'person');
      people.forEach((node) => {
        const ang = (typeof node.anchor === 'number')
          ? node.anchor
          : ((sumCodes(node.id) % 997) / 997) * Math.PI * 2;
        const gScore = Math.max(0.2, Math.min(1.1, node.gravity || 0.4));
        const ring = node.layer === 'focus' ? (0.34 + (1 - gScore) * 0.12) : 0.55;
        node._x = cx + Math.cos(ang) * (maxR * ring);
        node._y = cy + Math.sin(ang) * (maxR * ring * 0.86);
        node._fixed = true;
        node._r = 6 + gScore * 5;
        node._labelDy = 0;
        node._labelSide = Math.cos(ang) >= 0 ? 1 : -1;
      });
      const golden = Math.PI * (3 - Math.sqrt(5));
      const rankedOthers = others.slice().sort((a, b) => {
        const dg = (b.gravity || 0) - (a.gravity || 0);
        if (Math.abs(dg) > 1e-6) return dg;
        return a.id < b.id ? -1 : 1;
      });
      rankedOthers.forEach((node, i) => {
        const t = rankedOthers.length === 1 ? 0.45 : Math.sqrt((i + 0.55) / rankedOthers.length);
        const ang = i * golden + (sumCodes(node.id) % 23) * 0.011;
        const ring = 0.28 + t * 0.62;
        node._x = cx + Math.cos(ang) * (maxR * ring);
        node._y = cy + Math.sin(ang) * (maxR * ring * 0.86);
        node._fixed = false;
        const gScore = Math.max(0.2, Math.min(1.1, node.gravity || 0.4));
        node._r = (node.layer === 'periphery' ? 4 : 5.5) + gScore * (node.layer === 'periphery' ? 2.2 : 4);
        node._labelDy = 0;
        node._labelSide = node._x >= cx ? 1 : -1;
      });
      const all = st.nodes;
      const minGap = Math.max(26, Math.min(st.w, st.h) * 0.08);
      for (let iter = 0; iter < 56; iter++) {
        // Attract non-people along edges (toward people / related nodes).
        (st.edges || []).forEach((e) => {
          const a = st.byId[e.source], b = st.byId[e.target];
          if (!a || !b) return;
          let dx = b._x - a._x, dy = b._y - a._y;
          const d = Math.hypot(dx, dy) || 0.01;
          const pull = Math.min(2.2, d * 0.018 * Math.min(2, e.weight || 1));
          dx /= d; dy /= d;
          if (!a._fixed) { a._x += dx * pull; a._y += dy * pull; }
          if (!b._fixed) { b._x -= dx * pull; b._y -= dy * pull; }
        });
        // Repel overlaps; people only nudge slightly so anchors stay meaningful.
        for (let i = 0; i < all.length; i++) {
          for (let j = i + 1; j < all.length; j++) {
            const a = all[i], b = all[j];
            let dx = b._x - a._x, dy = b._y - a._y;
            let d = Math.hypot(dx, dy);
            const need = minGap + (a._r + b._r) * 0.55;
            if (d < 0.01) {
              const jitter = ((sumCodes(a.id) + iter) % 7) * 0.4;
              dx = Math.cos(jitter); dy = Math.sin(jitter); d = 1;
            }
            if (d >= need) continue;
            const push = (need - d) * 0.5;
            dx /= d; dy /= d;
            const aw = a._fixed ? 0.15 : 1;
            const bw = b._fixed ? 0.15 : 1;
            const norm = aw + bw || 1;
            a._x -= dx * push * (aw / norm) * 2;
            a._y -= dy * push * (aw / norm) * 2;
            b._x += dx * push * (bw / norm) * 2;
            b._y += dy * push * (bw / norm) * 2;
          }
        }
        all.forEach((node) => {
          if (node._fixed) {
            // Soft clamp people toward their anchor home after nudges.
            const ang = (typeof node.anchor === 'number')
              ? node.anchor
              : ((sumCodes(node.id) % 997) / 997) * Math.PI * 2;
            const gScore = Math.max(0.2, Math.min(1.1, node.gravity || 0.4));
            const ring = node.layer === 'focus' ? (0.34 + (1 - gScore) * 0.12) : 0.55;
            const hx = cx + Math.cos(ang) * (maxR * ring);
            const hy = cy + Math.sin(ang) * (maxR * ring * 0.86);
            node._x = node._x * 0.72 + hx * 0.28;
            node._y = node._y * 0.72 + hy * 0.28;
          }
          node._x = Math.max(pad, Math.min(st.w - pad, node._x));
          node._y = Math.max(pad, Math.min(st.h - pad, node._y));
        });
      }
      const labeled = all.filter((node) => node.layer === 'focus');
      labeled.sort((a, b) => a._y - b._y || a._x - b._x);
      for (let i = 0; i < labeled.length; i++) {
        const a = labeled[i];
        a._labelSide = a._x >= cx ? 1 : -1;
        for (let j = 0; j < i; j++) {
          const b = labeled[j];
          if (a._labelSide !== b._labelSide) continue;
          if (Math.abs(a._x - b._x) < 100
              && Math.abs((a._y + a._labelDy) - (b._y + b._labelDy)) < 20) {
            a._labelDy = b._labelDy + 18;
          }
        }
      }
      st.t0 = performance.now();
    }

    function drawLabel(x, y, text, side, emphasis) {
      const label = shortLabel(text);
      ctx.font = (emphasis ? '600 ' : '500 ') + '11px "Iowan Old Style", Georgia, serif';
      const tw = ctx.measureText(label).width;
      const padX = 6;
      const lx = side >= 0 ? x + 10 : x - 10 - tw;
      const ly = y - 4;
      ctx.fillStyle = emphasis ? 'rgba(255,254,251,.96)' : 'rgba(255,254,251,.88)';
      ctx.beginPath();
      const rw = tw + padX * 2, rh = 16, rx = lx - padX, ry = ly - 11;
      const rad = 7;
      ctx.moveTo(rx + rad, ry);
      ctx.arcTo(rx + rw, ry, rx + rw, ry + rh, rad);
      ctx.arcTo(rx + rw, ry + rh, rx, ry + rh, rad);
      ctx.arcTo(rx, ry + rh, rx, ry, rad);
      ctx.arcTo(rx, ry, rx + rw, ry, rad);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = emphasis ? 'rgba(11,19,32,.9)' : 'rgba(35,38,43,.78)';
      ctx.textAlign = 'left';
      ctx.fillText(label, lx, ly);
    }

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
      if (kind === 'person') {
        ctx.beginPath();
        ctx.arc(x, y, r, 0, Math.PI * 2);
        ctx.fillStyle = kindColor('person', alpha);
        ctx.fill();
      } else if (kind === 'org') {
        // Hexagon — companies/orgs, distinct from project diamonds.
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
        ctx.fillStyle = kindColor('project', alpha * 0.9);
        ctx.fillRect(-r * 0.85, -r * 0.85, r * 1.7, r * 1.7);
      } else if (kind === 'tool') {
        // Rounded square — tools/platforms, not faint idea dots.
        const s = r * 1.35;
        ctx.fillStyle = kindColor('tool', alpha * 0.92);
        ctx.beginPath();
        const rx = x - s / 2, ry = y - s / 2, rad = 3.5;
        ctx.moveTo(rx + rad, ry);
        ctx.arcTo(rx + s, ry, rx + s, ry + s, rad);
        ctx.arcTo(rx + s, ry + s, rx, ry + s, rad);
        ctx.arcTo(rx, ry + s, rx, ry, rad);
        ctx.arcTo(rx, ry, rx + s, ry, rad);
        ctx.closePath();
        ctx.fill();
      } else if (kind === 'place') {
        ctx.beginPath();
        ctx.moveTo(x, y - r);
        ctx.lineTo(x + r * 0.85, y + r * 0.7);
        ctx.lineTo(x - r * 0.85, y + r * 0.7);
        ctx.closePath();
        ctx.fillStyle = kindColor('place', alpha * 0.85);
        ctx.fill();
      } else if (kind === 'commitment' || kind === 'task') {
        // Same chevron, different hue: copper = to-do, burgundy = promise.
        ctx.beginPath();
        ctx.moveTo(x - r * 0.2, y - r);
        ctx.lineTo(x + r * 0.9, y);
        ctx.lineTo(x - r * 0.2, y + r);
        ctx.closePath();
        const risk = n.prospective_risk || 0;
        ctx.fillStyle = kindColor(
          kind, risk >= 0.7 ? (0.55 + alpha * 0.4) : alpha);
        ctx.fill();
      } else {
        ctx.beginPath();
        ctx.arc(x, y, r * 0.7, 0, Math.PI * 2);
        ctx.fillStyle = kindColor('idea', alpha * 0.55);
        ctx.fill();
      }
      ctx.restore();
    }

    function edgeVisible(e, st) {
      if (st.showFilaments || st.edit) return true;
      if (st.focusId) {
        return e.source === st.focusId || e.target === st.focusId;
      }
      if (st.hover) {
        return e.source === st.hover || e.target === st.hover;
      }
      if (st.selected) {
        return e.source === st.selected || e.target === st.selected;
      }
      return false;
    }

    function draw(st, now) {
      const w = st.w, h = st.h;
      ctx.clearRect(0, 0, w, h);
      const g = ctx.createRadialGradient(w / 2, h / 2, 8, w / 2, h / 2, Math.min(w, h) * 0.55);
      g.addColorStop(0, 'rgba(184,115,51,.035)');
      g.addColorStop(0.55, 'rgba(30,91,79,.02)');
      g.addColorStop(1, 'rgba(248,246,241,0)');
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, w, h);

      ctx.save();
      ctx.translate(w / 2 + st.cam.x, h / 2 + st.cam.y);
      ctx.scale(st.cam.z, st.cam.z);
      ctx.translate(-w / 2, -h / 2);
      const breath = window.VinceoReduceMotion() ? 0
        : Math.sin((now - st.t0) / 2800) * 0.006;
      const dimFocus = !!st.focusId;

      st.edges.forEach((e) => {
        if (!edgeVisible(e, st)) return;
        const a = st.byId[e.source], b = st.byId[e.target];
        if (!a || !b) return;
        const hot = st.hover && (e.source === st.hover || e.target === st.hover);
        ctx.beginPath();
        ctx.moveTo(a._x, a._y);
        const mx = (a._x + b._x) / 2;
        const my = (a._y + b._y) / 2 - 8;
        ctx.quadraticCurveTo(mx, my, b._x, b._y);
        const conf = e.confidence != null ? e.confidence : 0.6;
        if (e.style === 'dashed' || (!e.manual && conf < 0.75)) {
          ctx.setLineDash([4, 4]);
        } else if (e.style === 'dotted' || conf < 0.45) {
          ctx.setLineDash([2, 4]);
        } else {
          ctx.setLineDash([]);
        }
        const isPromise = e.rel === 'promise' || e.rel === 'responsible_for';
        ctx.strokeStyle = hot || isPromise
          ? 'rgba(184,115,51,' + (0.28 + (e.weight || 1) * 0.05) + ')'
          : 'rgba(11,19,32,' + (0.12 + conf * 0.12) + ')';
        ctx.lineWidth = hot ? 1.7 : (e.manual ? 1.4 : 1.05);
        ctx.stroke();
        ctx.setLineDash([]);
      });

      st.nodes.forEach(n => {
        const gScore = Math.max(0.2, Math.min(1.2, n.gravity || 0.4));
        const peri = n.layer === 'periphery';
        let alpha = (peri ? 0.28 : 0.55) + gScore * 0.35;
        alpha *= (n.memory_strength != null ? (0.55 + n.memory_strength * 0.45) : 1);
        if (dimFocus && st.focusId !== n.id) {
          const linked = st.edges.some(e =>
            (e.source === st.focusId && e.target === n.id)
            || (e.target === st.focusId && e.source === n.id));
          if (!linked) alpha *= 0.18;
          else alpha *= 0.75;
        }
        // Margin soft-highlight / emphasize — prose and sky share refs.
        if (st.softIds && st.softIds.size) {
          if (st.softIds.has(n.id)) alpha = Math.max(alpha, 0.95);
          else alpha *= 0.22;
        }
        if (st.emphasizeIds && st.emphasizeIds.size) {
          if (st.emphasizeIds.has(n.id)) alpha = Math.max(alpha, 1.0);
          else alpha *= 0.28;
        }
        const r = n._r || ((peri ? 3.5 : 5.5) + gScore * (peri ? 3 : 5.5));
        const scale = 1 + breath * (n.prospective_risk >= 0.7 ? 0.9 : 0.25);
        // Soft aura — keep tight so neighbors don't melt into one blob.
        ctx.beginPath();
        ctx.arc(n._x, n._y, r * scale * 1.35, 0, Math.PI * 2);
        ctx.fillStyle = kindColor(
          n.kind, n.kind === 'person' ? (0.04 + alpha * 0.05)
                                      : (0.03 + alpha * 0.04));
        ctx.fill();
        // Aging halo — warm amber ring that grows with neglect (one encoding).
        const aging = Number(n.aging) || 0;
        if (aging > 0.05 && (n.kind === 'task' || n.kind === 'commitment')) {
          const halo = r * scale * (1.55 + aging * 0.55);
          ctx.beginPath();
          ctx.arc(n._x, n._y, halo, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(184,115,51,' + (0.22 + aging * 0.45).toFixed(2) + ')';
          ctx.lineWidth = 1 + aging * 1.5;
          ctx.stroke();
        }
        drawKind(n, r * scale, Math.min(0.92, alpha));
        if (st.selected === n.id || st.focusId === n.id || st.hover === n.id || n.pinned) {
          ctx.beginPath();
          ctx.arc(n._x, n._y, r * scale + 3, 0, Math.PI * 2);
          ctx.strokeStyle = 'rgba(184,115,51,.75)';
          ctx.lineWidth = 1.5;
          ctx.stroke();
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
          ctx.fillStyle = up ? 'rgba(30,91,79,.7)' : 'rgba(150,54,66,.65)';
          ctx.fill();
        }
        // Cluster chip — absorbed near-duplicates ("+7 related")
        if (n.layer === 'focus' && (n.cluster_n || 0) > 1) {
          const chip = '+' + (n.cluster_n - 1);
          ctx.font = '600 9px ui-sans-serif, system-ui, sans-serif';
          const tw = ctx.measureText(chip).width;
          const bx = n._x + r * scale * 0.55;
          const by = n._y - r * scale * 0.85;
          const pw = tw + 6, ph = 11;
          ctx.fillStyle = 'rgba(11,19,32,0.78)';
          ctx.fillRect(bx, by - ph + 2, pw, ph);
          ctx.fillStyle = 'rgba(250,247,242,0.95)';
          ctx.textBaseline = 'middle';
          ctx.fillText(chip, bx + 3, by - ph / 2 + 3);
        }
      });

      st.nodes.forEach(n => {
        const show = n.layer === 'focus' || st.hover === n.id || st.selected === n.id
          || st.focusId === n.id || n.pinned;
        if (!show) return;
        if (dimFocus && st.focusId !== n.id) {
          const linked = st.edges.some(e =>
            (e.source === st.focusId && e.target === n.id)
            || (e.target === st.focusId && e.source === n.id));
          if (!linked) return;
        }
        const r = n._r || 8;
        drawLabel(
          n._x + (n._labelSide || 1) * (r + 3),
          n._y + (n._labelDy || 0),
          n.label, n._labelSide || 1,
          st.hover === n.id || st.selected === n.id || st.focusId === n.id);
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
        const act = e.target && e.target.getAttribute('data-act');
        if (act === 'fit') fit();
        else if (act === 'in') { state.cam.z = Math.min(1.45, state.cam.z * 1.1); saveCam(); }
        else if (act === 'out') { state.cam.z = Math.max(0.9, state.cam.z / 1.1); saveCam(); }
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
            + VinceoEsc(err.message || err) + '</div>'); }
          return;
        }
        if (act === 'do-reclassify' && state.selected) {
          const sel = panel.querySelector('.const-kind-select');
          const kind = sel && sel.value;
          if (!kind) return;
          try { await reclassify(state.selected, kind); }
          catch (err) { panel.insertAdjacentHTML('beforeend',
            '<div class="const-edit-hint" style="color:var(--danger)">'
            + VinceoEsc(err.message || err) + '</div>'); }
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
            + VinceoEsc(err.message || err) + '</div>'); }
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
        const dx = n._x - mx, dy = n._y - my;
        if (dx * dx + dy * dy < 18 * 18) hit = n.id;
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
                + VinceoEsc(err.message || err) + '</div>');
            }
          } else {
            state.selected = state.hover;
            state.linkFrom = null;
            openEvidence(state.hover);
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
        layout(state);
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

window.VinceoParsePacket = function (text) {
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

window.VinceoRenderFolio = function (packet, opts) {
  opts = opts || {};
  const f = (packet && packet.fields) || {};
  const editable = !!opts.editable;
  const rows = [
    ['Action', 'action', f.action],
    ['To', 'to', f.to],
    ['Subject', 'subject', f.subject],
    ['Body', 'body', f.body],
    ['Why', 'why', f.why],
    ['Source', 'source', f.source],
  ].filter(r => r[2]);
  let html = '<div class="folio approval-folio" data-folio="1">';
  html += '<div class="serif-title" style="font-size:1.35rem;margin:0 0 4px 18px">Approval</div>';
  html += '<div style="margin:0 0 14px 18px;color:var(--mut);font-size:13px">'
    + esc(packet.summary || '') + '</div>';
  html += '<div class="ink-divider" style="margin-left:18px;margin-right:8px"></div>';
  rows.forEach(([label, key, val]) => {
    html += '<div style="margin:10px 0 10px 18px">';
    html += '<div class="pv-label" style="font:11px var(--mono);text-transform:uppercase;'
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
window.VinceoResponse = {
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
    let html = '<details class="rd-grounding"><summary>Grounded in '
      + g.total + ' memory source' + (g.total === 1 ? '' : 's') + '</summary>';
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
window.VinceoFieldStream = {
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

/* Today — thin helpers for the dashboard (offers stay on agent_bridge). */
window.VinceoShell = {
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
window.VinceoCapture = {
  _timer: null,
  _state: null,
  _SOURCES: [
    {key:'mic', label:'Mic', warn:''},
    {key:'webcam', label:'Camera', warn:''},
    {key:'screen', label:'Screen', warn:'Captures whatever is on your display.'},
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
  openPrivacy() {
    const el = document.getElementById('vinceoPrivacy');
    if (!el) return;
    const src = ((this._state && this._state.consent && this._state.consent.sources)
      || {});
    this._SOURCES.forEach((s) => {
      const box = document.getElementById('pv_' + s.key);
      if (box) box.checked = !!src[s.key];
    });
    const ret = (this._state && this._state.meeting_mode
      && this._state.meeting_mode.default_retention) || 'transcript_only';
    const t = document.getElementById('pv_ret_transcript');
    const r = document.getElementById('pv_ret_receipts');
    if (t) t.checked = ret !== 'keep_receipts';
    if (r) r.checked = ret === 'keep_receipts';
    el.classList.add('open');
  },
  closePrivacy() {
    const el = document.getElementById('vinceoPrivacy');
    if (el) el.classList.remove('open');
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
      this.render();
      this.closePrivacy();
    } catch (e) {}
  },
  async toggle(source) {
    if (!this._state) return;
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
    if (document.getElementById('vinceoRecBar')) return;
    // Skip on bare launch page until Continue — still mount so consent can show.
    const bar = document.createElement('div');
    bar.id = 'vinceoRecBar';
    bar.setAttribute('aria-live', 'polite');
    document.body.appendChild(bar);

    const modal = document.createElement('div');
    modal.id = 'vinceoPrivacy';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-label', 'Capture privacy');
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
      + '<div class="pv-actions">'
      + '<button type="button" class="pv-btn quiet" id="pvRevoke">Turn all off</button>'
      + '<button type="button" class="pv-btn quiet" id="pvCancel">Not now</button>'
      + '<button type="button" class="pv-btn go" id="pvSave">Save &amp; start</button>'
      + '</div></div>';
    document.body.appendChild(modal);
    modal.addEventListener('click', (e) => {
      if (e.target === modal) {
        VinceoMemory.set('capturePromptDismissed', true);
        this.closePrivacy();
      }
    });
    document.getElementById('pvCancel').onclick = () => {
      VinceoMemory.set('capturePromptDismissed', true);
      this.closePrivacy();
    };    document.getElementById('pvSave').onclick = () => this.applyPrivacy();
    document.getElementById('pvRevoke').onclick = async () => {
      try {
        this._state = await this.revoke();
        this.render();
        this.closePrivacy();
      } catch (e) {}
    };
  },
  render() {
    const bar = document.getElementById('vinceoRecBar');
    if (!bar || !this._state) return;
    const consent = this._state.consent || {};
    const sources = consent.sources || {};
    const running = this._state.running || {};
    const mm = this._state.meeting_mode || {};
    const liveKeys = ['mic', 'webcam', 'screen', 'system_audio'];
    const consented = !!consent.consented;
    let html = '';
    if (mm.active) {
      html += '<div class="rec-row meeting">'
        + '<span class="rec-chip meeting-on" title="Meeting mode — hotter capture">'
        + '<span class="dot" aria-hidden="true"></span>'
        + '<span>Meeting · capturing'
        + (mm.title ? (' · ' + String(mm.title).slice(0, 40)) : '')
        + '</span></span></div>';
    }
    if (!consented) {
      html += '<button type="button" class="rec-consent-btn" id="recOpenPrivacy">'
        + 'Enable capture…</button>';
    } else {
      html += '<div class="rec-row">';
      liveKeys.forEach((k) => {
        if (!sources[k]) return;
        const on = !!running[k];
        const meta = this._SOURCES.find((s) => s.key === k) || {label: k};
        html += '<button type="button" class="rec-chip' + (on ? '' : ' paused')
          + '" data-src="' + k + '" title="'
          + (on ? 'Pause ' : 'Resume ') + meta.label + '">'
          + '<span class="dot" aria-hidden="true"></span>'
          + '<span>' + meta.label + '</span>'
          + '<span class="act">' + (on ? 'pause' : 'resume') + '</span>'
          + '</button>';
      });
      html += '<button type="button" class="rec-chip paused" id="recOpenPrivacy" '
        + 'title="Privacy controls"><span class="act">privacy</span></button>';
      html += '</div>';
    }
    bar.innerHTML = html;
    const openBtn = document.getElementById('recOpenPrivacy');
    if (openBtn) openBtn.onclick = () => this.openPrivacy();
    bar.querySelectorAll('.rec-chip[data-src]').forEach((btn) => {
      btn.onclick = () => this.toggle(btn.getAttribute('data-src'));
    });
  },
  async tick() {
    try {
      this._state = await this.status();
      this.render();
      // First visit: force the consent sheet when nothing is allowed yet.
      if (this._state && this._state.consent
          && !this._state.consent.consented
          && !VinceoMemory.get('capturePromptDismissed', false)) {
        this.openPrivacy();
      }
    } catch (e) {}
  },
  start() {
    this.mount();
    this.tick();
    if (this._timer) clearInterval(this._timer);
    this._timer = setInterval(() => this.tick(), 4000);
  }
};

document.addEventListener('DOMContentLoaded', () => {
  try { window.VinceoCapture && window.VinceoCapture.start(); } catch (e) {}
});
</script>
"""
