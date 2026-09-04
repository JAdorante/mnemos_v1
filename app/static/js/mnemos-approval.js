window.MnemosApprovals = {
  _es: null,
  _lastSig: '',
  refresh() {
    fetch('/approvals/state').then(r => r.json()).then(s => {
      this.render(s);
      try { window.dispatchEvent(new CustomEvent('mnemos:approval', {detail: s})); } catch (e) {}
    }).catch(() => {});
  },
  render(s) {
    const bar = document.getElementById('mnemosApproval');
    if (!bar) return;
    const pending = !!(s && s.pending);
    bar.classList.toggle('on', pending);
    bar.setAttribute('aria-hidden', pending ? 'false' : 'true');
    try { window.MnemosChrome && MnemosChrome.sync(); } catch (e) {}
    if (!pending) return;
    const sum = bar.querySelector('.ap-sum');
    const age = bar.querySelector('.ap-age');
    const more = bar.querySelector('.ap-more');
    if (sum) sum.textContent = s.summary || 'Sparrow needs your decision.';
    if (age) age.textContent = s.age_label || '';
    if (more) {
      const n = s.queued || 0;
      if (n > 0) {
        more.hidden = false;
        more.textContent = '+' + n + ' more';
        more.href = s.queue_href || '/chat';
      } else {
        more.hidden = true;
      }
    }
    const chat = document.getElementById('navChat');
    if (chat) chat.classList.toggle('attn', true);
  },
  connect() {
    if (this._pollTimer) { clearInterval(this._pollTimer); this._pollTimer = null; }
    if (this._es || typeof EventSource === 'undefined') {
      this.refresh();
      if (!this._es) {
        this._pollTimer = setInterval(() => {
          if (document.hidden) return;
          this.refresh();
        }, 8000);
      }
      return;
    }
    try {
      this._es = new EventSource('/approvals/stream');
      this._es.addEventListener('approval', (ev) => {
        try {
          const s = JSON.parse(ev.data);
          const sig = (s && s.sig) || '';
          if (sig === this._lastSig) return;
          this._lastSig = sig;
          this.render(s);
          try { window.dispatchEvent(new CustomEvent('mnemos:approval', {detail: s})); } catch (e) {}
        } catch (e) {}
      });
      this._es.onerror = () => { /* browser will retry */ };
    } catch (e) {
      this.refresh();
      this._pollTimer = setInterval(() => {
        if (document.hidden) return;
        this.refresh();
      }, 8000);
    }
  },
  enhanceForms() {
    document.querySelectorAll('form.approval-form').forEach(form => {
      if (form.dataset.apEnhanced) return;
      form.dataset.apEnhanced = '1';
      form.addEventListener('submit', (ev) => {
        if (!window.fetch) return;
        ev.preventDefault();
        const fd = new FormData(form);
        const body = new URLSearchParams();
        for (const [k, v] of fd.entries()) body.set(k, String(v));
        body.set('as_json', '1');
        const action = form.getAttribute('action') || '/approvals/resolve';
        fetch(action, {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
          body: body.toString(),
        }).then(async (r) => {
          let j = {};
          try { j = await r.json(); } catch (e) {}
          if (!r.ok || j.ok === false) {
            const msg = (j && j.error) || ('approval refused (' + r.status + ')');
            try { window.dispatchEvent(new CustomEvent('mnemos:approval-refused', {detail: j})); } catch (e) {}
            console.warn('[approval]', msg);
          }
          this.refresh();
          try { window.dispatchEvent(new CustomEvent('mnemos:approval-resolved')); } catch (e) {}
        }).catch(() => { form.submit(); });
      });
    });
  }
};
document.addEventListener('DOMContentLoaded', () => {
  if (window.MnemosApprovals) {
    MnemosApprovals.enhanceForms();
    MnemosApprovals.connect();
  }
});
