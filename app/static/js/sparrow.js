/* Sparrow shell — one bundle for every page (frontend spec, 2026-11-06).
   No framework, no client state store: the server is the state. This file
   adds the rail/menu behaviour, the top-bar search, the capture label, the
   sync module (one SSE connection per browser, partial refetches), the Inbox
   interactions (act, 10 s undo, keyboard) and citation popovers. */
(function () {
  'use strict';
  var S = window.Sparrow = window.Sparrow || {};
  var NS = 'mnemos.ui.';

  var store = {
    get: function (k, f) {
      try { var r = localStorage.getItem(NS + k); return r == null ? f : JSON.parse(r); }
      catch (e) { return f; }
    },
    set: function (k, v) { try { localStorage.setItem(NS + k, JSON.stringify(v)); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(NS + k); } catch (e) {} }
  };

  function csrf() {
    var m = document.cookie.match(/(?:^|; )quill_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }
  function post(url, body) {
    var h = { 'Content-Type': 'application/json' };
    var t = csrf(); if (t) h['X-CSRF-Token'] = t;
    return fetch(url, { method: 'POST', headers: h, credentials: 'same-origin',
                        body: JSON.stringify(body || {}) });
  }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function $$(sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); }
  function reducedMotion() {
    try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; }
  }

  /* ------------------------------------------------------------ shell */
  function initShell() {
    var shell = $('#sparrowShell'), rail = $('#sparrowRail'), menu = $('#sparrowMenu');
    if (!shell || !rail) return;
    if (menu) {
      menu.addEventListener('click', function () {
        var open = shell.classList.toggle('rail-open');
        menu.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) { var first = rail.querySelector('a'); if (first) first.focus(); }
      });
      document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape' && shell.classList.contains('rail-open')) {
          shell.classList.remove('rail-open'); menu.setAttribute('aria-expanded', 'false'); menu.focus();
        }
      });
      document.addEventListener('click', function (e) {
        if (shell.classList.contains('rail-open') && !rail.contains(e.target) && !menu.contains(e.target)) {
          shell.classList.remove('rail-open'); menu.setAttribute('aria-expanded', 'false');
        }
      });
    }
    // Server marks the active item; this is the fallback for pages that
    // were served without the per-request pass.
    if (!rail.querySelector('a.on, a[aria-current="page"]')) {
      var p = (location.pathname || '/').replace(/\/$/, '') || '/';
      var key = null;
      if (p === '/' || p === '/today' || p === '/shell') key = 'today';
      else if (p.indexOf('/chat') === 0) key = 'chat';
      else if (p.indexOf('/inbox') === 0) key = 'inbox';
      else if (p === '/memory' || p === '/console' || p.indexOf('/meeting') === 0) key = 'memory';
      else if (p === '/profile' || /^\/(people|projects|org\/|entities\/)/.test(p)) key = 'people';
      else if (/^\/(settings|capture|peer|onboarding|phone|desktop|assistants|org-network|teams)/.test(p)) key = 'settings';
      var a = key && rail.querySelector('a[data-nav="' + key + '"]');
      if (a) { a.classList.add('on'); a.setAttribute('aria-current', 'page'); }
    }
  }

  /* ----------------------------------------------------------- search */
  function initSearch() {
    var form = $('#sparrowSearch'), box = $('#sparrowSearchBox');
    if (form && box) {
      form.addEventListener('submit', function (e) {
        var q = (box.value || '').trim();
        if (!q) { e.preventDefault(); return; }
        if (q.charAt(0) === '?') {
          e.preventDefault();
          var ask = q.slice(1).trim();
          if (window.MnemosAsk && typeof window.MnemosAsk.open === 'function') {
            try {
              window.MnemosAsk.open();
              var ab = $('#mnemosAskBox'); if (ab) { ab.value = ask; }
              return;
            } catch (err) {}
          }
          location.href = '/chat?q=' + encodeURIComponent(ask);
        }
      });
    }
    // Memory page: honour ?q= from the top bar.
    if (location.pathname === '/memory') {
      try {
        var qp = new URLSearchParams(location.search), q = qp.get('q');
        var input = $('#q');
        if (q && input && !input.value) {
          input.value = q;
          input.dispatchEvent(new Event('input', { bubbles: true }));
          input.dispatchEvent(new Event('change', { bubbles: true }));
          if (typeof window.load === 'function') { try { window.load(); } catch (err) {} }
        }
      } catch (err) {}
    }
  }

  /* ---------------------------------------------------------- capture */
  function initCapture() {
    var el = $('#sparrowCapture'); if (!el) return;
    var label = el.querySelector('.cap-label');
    function paint(st) {
      var running = (st && st.running) || {};
      var on = [];
      if (running.mic) on.push('mic');
      if (running.system_audio) on.push('system');
      if (running.screen) on.push('screen');
      if (running.webcam) on.push('camera');
      el.classList.toggle('on', on.length > 0);
      if (label) label.textContent = on.length ? 'Capture: on · ' + on.join(' · ') : 'Capture: off';
      el.title = on.length ? 'Capturing ' + on.join(', ') : 'Capture is off';
    }
    function refresh() {
      fetch('/capture/status', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { if (j) paint(j); })
        .catch(function () {});
    }
    refresh();
    S.sync.onChange(function () { refresh(); });
    setInterval(refresh, 30000);
  }

  /* ------------------------------------------------------------- sync
     One /field/stream connection per browser: tabs elect a leader through
     a localStorage lease and the leader relays every change over a
     BroadcastChannel. Fallback: poll /graph/version every 30 s. */
  S.sync = (function () {
    var listeners = [], timer = null, pending = {}, es = null, bc = null;
    var tabId = Math.random().toString(36).slice(2);
    var LEASE = 'sse-lease', LEASE_TTL = 8000;
    var lastVersion = null;

    function emit(changed, kinds) {
      listeners.forEach(function (fn) { try { fn(changed, kinds); } catch (e) {} });
      schedule(changed);
    }
    function onChange(fn) { listeners.push(fn); }

    function lease() { return store.get(LEASE, null); }
    function iAmLeader() { var l = lease(); return !!(l && l.id === tabId); }
    function tryLead() {
      var l = lease(), now = Date.now();
      if (!l || now - (l.ts || 0) > LEASE_TTL || l.id === tabId) {
        store.set(LEASE, { id: tabId, ts: now });
        if (!es) connect();
      } else if (es) { es.close(); es = null; }
    }
    function connect() {
      if (!('EventSource' in window)) return;
      try { es = new EventSource('/field/stream'); } catch (e) { es = null; return; }
      es.addEventListener('change', function (ev) {
        var d = {};
        try { d = JSON.parse(ev.data || '{}'); } catch (e) {}
        var changed = d.changed || ['*'];
        var kinds = d.kinds || [d.type || 'change'];
        if (d.version) lastVersion = d.version;
        if (bc) { try { bc.postMessage({ changed: changed, kinds: kinds, version: d.version }); } catch (e) {} }
        emit(changed, kinds);
      });
      es.onerror = function () { /* browser reconnects; the poll covers gaps */ };
    }
    function schedule(changed) {
      (changed || ['*']).forEach(function (k) { pending[k] = true; });
      clearTimeout(timer);
      timer = setTimeout(function () {
        var keys = Object.keys(pending); pending = {};
        refetch(keys);
      }, 400);
    }
    function watches(card, keys) {
      if (keys.indexOf('*') >= 0) return true;
      var w = (card.getAttribute('data-watch') || '').split(',').map(function (s) { return s.trim(); });
      return keys.some(function (k) { return w.indexOf(k) >= 0 || w.indexOf('entity:' + k) >= 0; });
    }
    function refetch(keys) {
      $$('[data-partial]').forEach(function (card) {
        if (!watches(card, keys)) return;
        if (card.matches(':focus-within') || card.hasAttribute('data-busy')) {
          card.setAttribute('data-stale', '1'); return;
        }
        swap(card);
      });
      refreshCounts();
    }
    function swap(card) {
      var url = card.getAttribute('data-partial'); if (!url) return;
      fetch(url, { cache: 'no-store', credentials: 'same-origin', headers: { 'Accept': 'text/html' } })
        .then(function (r) { return r.ok ? r.text() : null; })
        .then(function (html) {
          if (html == null) return;
          var body = card.querySelector('.card-b'); if (!body) return;
          var focused = document.activeElement && document.activeElement.id;
          body.innerHTML = html;
          card.removeAttribute('data-stale');
          if (focused) { var again = document.getElementById(focused); if (again) { try { again.focus(); } catch (e) {} } }
          if (!reducedMotion()) { card.classList.add('flash'); setTimeout(function () { card.classList.remove('flash'); }, 600); }
          document.dispatchEvent(new CustomEvent('sparrow:partial', { detail: { card: card } }));
        })
        .catch(function () {});
    }
    function refreshCounts() {
      if (!$('[data-count="inbox"]')) return;
      fetch('/inbox/count', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) { if (j) setCount('inbox', j.needs_you); })
        .catch(function () {});
    }
    function setCount(name, n) {
      $$('[data-count="' + name + '"]').forEach(function (el) {
        el.textContent = String(n || 0);
        if (n) el.removeAttribute('hidden'); else el.setAttribute('hidden', '');
      });
    }
    function poll() {
      fetch('/graph/version', { cache: 'no-store', credentials: 'same-origin' })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (j) {
          if (!j) return;
          if (lastVersion !== null && j.version !== lastVersion) emit(['*'], ['version']);
          lastVersion = j.version;
        })
        .catch(function () {});
    }
    function start() {
      if ('BroadcastChannel' in window) {
        try {
          bc = new BroadcastChannel('sparrow');
          bc.onmessage = function (ev) {
            if (iAmLeader()) return;
            var d = ev.data || {};
            if (d.version) lastVersion = d.version;
            emit(d.changed || ['*'], d.kinds || ['change']);
          };
        } catch (e) { bc = null; }
      }
      tryLead();
      setInterval(function () {
        if (iAmLeader()) store.set(LEASE, { id: tabId, ts: Date.now() }); else tryLead();
      }, 3000);
      window.addEventListener('pagehide', function () { if (iAmLeader()) store.del(LEASE); if (es) es.close(); });
      setInterval(poll, 30000);
    }
    return { onChange: onChange, start: start, refetch: refetch, swap: swap, setCount: setCount, emit: emit };
  })();

  /* ------------------------------------------------------------ inbox */
  function initInbox() {
    var root = document; if (!$('.inbox-row')) return;
    function rows() { return $$('.inbox-row:not(.done)'); }
    function rove() {
      var rs = rows();
      rs.forEach(function (r, i) { r.tabIndex = i === 0 ? 0 : -1; });
    }
    rove();
    document.addEventListener('sparrow:partial', rove);

    function actionButtons(row) { return $$('[data-action]', row).filter(function (b) { return !b.closest('.ir-done'); }); }
    function primary(row) { return $('.btn-primary[data-action]', row) || actionButtons(row)[0]; }

    root.addEventListener('submit', function (e) {
      var form = e.target.closest && e.target.closest('form[data-inbox-actions]');
      if (!form) return;
      e.preventDefault();
      var btn = e.submitter || $('button[type="submit"]', form);
      if (!btn) return;
      act(form.closest('.inbox-row'), btn.value || btn.getAttribute('data-action'), btn);
    });
    root.addEventListener('click', function (e) {
      var local = e.target.closest && e.target.closest('[data-local]');
      if (local) { e.preventDefault(); collapse(local.closest('.inbox-row'), 'Later', null); return; }
      var undo = e.target.closest && e.target.closest('[data-undo]');
      if (undo) { e.preventDefault(); doUndo(undo.closest('.inbox-row'), undo.getAttribute('data-undo')); }
    });
    root.addEventListener('keydown', function (e) {
      var row = e.target.closest && e.target.closest('.inbox-row');
      if (!row || row.classList.contains('done')) return;
      var inField = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
      if (inField) return;
      var rs = rows(), i = rs.indexOf(row);
      if (e.key === 'ArrowDown' && rs[i + 1]) { e.preventDefault(); rs[i + 1].focus(); }
      else if (e.key === 'ArrowUp' && rs[i - 1]) { e.preventDefault(); rs[i - 1].focus(); }
      else if (e.key === 'Enter' && e.target === row) { e.preventDefault(); var p = primary(row); if (p) p.click(); }
      else if (/^[1-4]$/.test(e.key) && e.target === row) {
        var b = actionButtons(row)[parseInt(e.key, 10) - 1]; if (b) { e.preventDefault(); b.click(); }
      }
    });

    function act(row, action, btn) {
      if (!row || !action) return;
      var id = row.getAttribute('data-id');
      var buttons = actionButtons(row);
      buttons.forEach(function (b) { b.disabled = true; });
      if (btn) btn.classList.add('loading');
      row.setAttribute('data-busy', '1');
      post('/inbox/' + encodeURIComponent(id) + '/' + encodeURIComponent(action), {})
        .then(function (r) { return r.json().then(function (j) { j._status = r.status; return j; }); })
        .then(function (j) {
          row.removeAttribute('data-busy');
          if (!j.ok) { fail(row, j.error || ('could not ' + action)); buttons.forEach(function (b) { b.disabled = false; }); if (btn) btn.classList.remove('loading'); return; }
          collapse(row, j.label || action, j.undo_token || null, j.undo_s || 10);
          bump(-1, row);
        })
        .catch(function () {
          row.removeAttribute('data-busy');
          fail(row, 'Sparrow did not answer — try again');
          buttons.forEach(function (b) { b.disabled = false; }); if (btn) btn.classList.remove('loading');
        });
    }
    function fail(row, msg) {
      var el = $('.ir-error', row);
      if (!el) { el = document.createElement('div'); el.className = 'ir-error'; el.setAttribute('role', 'alert'); $('.ir-body', row).appendChild(el); }
      el.textContent = msg;
    }
    function collapse(row, label, token, secs) {
      var body = $('.ir-body', row); if (!body) return;
      row._saved = body.innerHTML;
      row.classList.add('done');
      var n = Math.round(secs || 10);
      body.innerHTML = '<span class="ir-done">' + escapeHtml(label) +
        (token ? ' · <button type="button" class="ir-undo" data-undo="' + escapeHtml(token) + '">Undo</button>' : '') +
        ' <span class="ir-count" aria-hidden="true">' + n + '</span></span>';
      var count = $('.ir-count', row);
      var next = rows()[0]; if (next && document.activeElement === row) next.focus();
      rove();
      row._timer = setInterval(function () {
        n -= 1; if (count) count.textContent = String(n);
        if (n <= 0) { clearInterval(row._timer); remove(row); }
      }, 1000);
    }
    function remove(row) {
      var list = row.parentNode;
      if (reducedMotion()) { row.remove(); } else {
        row.classList.add('leaving'); setTimeout(function () { row.remove(); emptyCheck(list); }, 300); return;
      }
      emptyCheck(list);
    }
    function emptyCheck(list) {
      if (list && list.classList.contains('inbox-list') && !list.querySelector('.inbox-row')) {
        var p = document.createElement('p'); p.className = 'empty-line'; p.textContent = 'Nothing here right now.';
        list.replaceWith(p);
      }
    }
    function doUndo(row, token) {
      clearInterval(row._timer);
      post('/inbox/undo/' + encodeURIComponent(token), {})
        .then(function (r) { return r.json(); })
        .then(function (j) {
          if (!j.ok) { fail(row, j.error || 'undo window passed'); return; }
          row.classList.remove('done');
          $('.ir-body', row).innerHTML = row._saved || '';
          bump(1, row);
          rove(); row.focus();
        })
        .catch(function () { fail(row, 'undo did not reach Sparrow'); });
    }
    function bump(delta, row) {
      var group = row.closest('[data-inbox-group]');
      var needs = group && group.getAttribute('data-inbox-group') === 'needs_you';
      if (needs) {
        var pill = $('[data-count="inbox"]');
        var cur = pill ? parseInt(pill.textContent || '0', 10) || 0 : 0;
        S.sync.setCount('inbox', Math.max(0, cur + delta));
      }
      var line = $('[data-count-line]');
      if (line) {
        var a = $$('[data-inbox-group="needs_you"] .inbox-row:not(.done)').length;
        var b = $$('[data-inbox-group="can_wait"] .inbox-row:not(.done)').length;
        line.textContent = line.textContent.replace(/^\d+ need you · \d+ can wait/, a + ' need you · ' + b + ' can wait');
      }
    }
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  /* ------------------------------------------------------------- cites */
  function initCites() {
    var pop = null;
    function show(a) {
      hide();
      pop = document.createElement('div');
      pop.className = 'cite-pop'; pop.setAttribute('role', 'tooltip');
      var kind = a.getAttribute('data-chip') || 'extracted';
      pop.innerHTML = '<span class="chip chip-' + escapeHtml(kind) + '">' + escapeHtml(kind) + '</span>' +
        '<span class="chip-source">' + escapeHtml(a.getAttribute('data-source') || '') + '</span>' +
        (a.getAttribute('data-quote') ? '<blockquote>' + escapeHtml(a.getAttribute('data-quote')) + '</blockquote>' : '');
      document.body.appendChild(pop);
      var r = a.getBoundingClientRect();
      pop.style.left = Math.max(8, Math.min(window.innerWidth - pop.offsetWidth - 8, r.left + window.scrollX)) + 'px';
      pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
      pop.style.position = 'absolute';
      a.setAttribute('aria-describedby', 'cite-pop');
      pop.id = 'cite-pop';
    }
    function hide() { if (pop) { pop.remove(); pop = null; } }
    document.addEventListener('mouseover', function (e) { var a = e.target.closest && e.target.closest('.cite'); if (a) show(a); });
    document.addEventListener('mouseout', function (e) { if (e.target.closest && e.target.closest('.cite')) hide(); });
    document.addEventListener('focusin', function (e) { var a = e.target.closest && e.target.closest('.cite'); if (a) show(a); });
    document.addEventListener('focusout', function (e) { if (e.target.closest && e.target.closest('.cite')) hide(); });
    document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hide(); });
  }

  function init() {
    initShell();
    initSearch();
    S.sync.start();
    initCapture();
    initInbox();
    initCites();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
