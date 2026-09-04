"""Global approval affordance — one source of truth for banner / Today / Chat.

Copper here is legitimate: a human decision is required.
Yes/No are real POST forms (no-JS). JS enhances for inline dismiss + SSE.
"""
from __future__ import annotations

import html
import time
from typing import Any


APPROVAL_CSS = """\
#mnemosApproval{
  display:none;align-items:center;gap:12px;flex-wrap:wrap;
  position:relative;z-index:var(--z-banner);
  min-height:44px;padding:8px 22px;
  background:linear-gradient(180deg,var(--acc-warm) 0%,color-mix(in srgb,var(--paper) 97%,transparent) 100%);
  border-bottom:1px solid var(--acc-28);
  border-top:1px solid var(--acc-18);
  font:14px/1.35 var(--font);color:var(--navy);
  animation:approvalSlide .28s var(--ease) both;
}
#mnemosApproval.on{display:flex}
#mnemosApproval .ap-dot{
  width:8px;height:8px;border-radius:50%;background:var(--acc);flex:0 0 auto;
}
#mnemosApproval .ap-sum{flex:1;min-width:12rem}
#mnemosApproval .ap-age{
  font:11px var(--mono);color:var(--mut);white-space:nowrap;
}
#mnemosApproval .ap-more{
  font:11px var(--mono);color:var(--acc);border:1px solid var(--acc-35);
  border-radius:var(--radius-full);padding:2px 8px;text-decoration:none;white-space:nowrap;
}
#mnemosApproval .ap-actions{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
#mnemosApproval .ap-actions button,#mnemosApproval .ap-actions a{
  border-radius:var(--radius-sm);padding:7px 12px;min-height:32px;
  font:500 13px/1.2 var(--font);letter-spacing:var(--track-snug);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
  text-decoration:none;display:inline-flex;align-items:center;
  box-shadow:0 1px 2px rgba(11,19,32,.03);
}
#mnemosApproval .ap-actions .go{background:var(--navy);color:#F8F6F1;border:none}
#mnemosApproval .ap-actions .quiet{background:transparent;color:var(--mut)}
#mnemosApproval .ap-actions .review{color:var(--mut)}
@keyframes approvalSlide{
  from{opacity:0;transform:translateY(-6px)}
  to{opacity:1;transform:none}
}
@media (prefers-reduced-motion:reduce){
  #mnemosApproval{animation:none}
}
.action-detail{margin:10px 0 4px}
.action-detail > summary{
  cursor:pointer;font:500 13px var(--font);color:var(--navy);list-style:none;
}
.action-detail > summary::-webkit-details-marker{display:none}
.action-detail > summary::before{content:"▸ ";color:var(--acc);font-size:11px}
.action-detail[open] > summary::before{content:"▾ "}
.action-detail .detail-card{
  margin-top:8px;padding:12px 14px;border:1px solid var(--acc-22);
  border-radius:var(--radius-sm);background:linear-gradient(180deg,#FFFCF7 0%,var(--surface) 100%);
  border-left:3px solid var(--acc);
}
.action-detail .intent{font-size:14px;margin:0 0 10px;color:var(--text)}
.action-detail .steps{
  margin:0;padding:0;list-style:none;font:12px/1.55 var(--mono);color:var(--mut);
}
.action-detail .steps li{padding:3px 0}
.action-detail .payload{
  margin-top:10px;max-height:180px;overflow:auto;white-space:pre-wrap;
  font:13px/1.45 var(--font);color:var(--text);padding:10px 12px;
  background:var(--bg-elev);border:1px solid var(--line);border-radius:10px;
}
"""

APPROVAL_JS = r"""
<script>
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
</script>
"""


def _esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=True)


def _age_label(created_at: float | None) -> str:
    if not created_at:
        return ""
    age = max(0, int(time.time() - float(created_at)))
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age // 60}m"
    return f"{age // 3600}h"


def collect_state(agent_worker=None) -> dict[str, Any]:
    """Single snapshot for banner / SSE / SSR inject."""
    empty = {
        "pending": False,
        "summary": "",
        "age_label": "",
        "queued": 0,
        "queue_href": "/chat",
        "review_href": "/chat",
        "kind": None,
        "intent": "",
        "steps": [],
        "payload": "",
        "outbound": False,
        "created_at": None,
        "sig": "0",
        "packet_id": None,
        "payload_hash": None,
        "expires_at": None,
    }
    if agent_worker is None:
        return empty
    try:
        agent_worker.expire_stale_offers()
    except Exception:
        pass
    offer = None
    try:
        peek = getattr(agent_worker, "pending_offer", None)
        offer = peek() if callable(peek) else None
    except Exception:
        offer = None
    _, state = [], {}
    try:
        _, state = agent_worker.snapshot(10**9)
    except Exception:
        state = {}
    awaiting = bool(state.get("awaiting") or state.get("todo_pending"))
    packet = state.get("packet") if isinstance(state.get("packet"), dict) else None
    question = state.get("question") or ""
    queued = int(getattr(agent_worker, "offer_queue_len", lambda: 0)() or 0)

    if not awaiting and not offer and not packet:
        return empty

    fields = (packet or {}).get("fields") or {}
    intent = (fields.get("action") or (packet or {}).get("summary") or "").strip()
    summary = ""
    created = None
    kind = "offer"
    steps: list[str] = []
    payload = ""

    if packet and packet.get("kind") == "approval":
        kind = "approval"
        summary = intent or (packet.get("summary") or "Sparrow needs approval to act.")
        if not summary.lower().startswith("sparrow"):
            summary = f"Sparrow wants to {summary[0].lower() + summary[1:]}" if summary else summary
        if fields.get("to"):
            steps.append(f"Compose to {fields['to']}")
        if fields.get("subject"):
            steps.append(f"Subject: {fields['subject']}")
        if fields.get("action") and not steps:
            steps.append(fields["action"])
        payload = (fields.get("body") or fields.get("details") or "").strip()
        if not summary and question:
            summary = question.splitlines()[0][:160]
    elif offer:
        kind = offer.get("kind") or "offer"
        msg = (offer.get("message") or "").strip()
        title = (offer.get("title") or "").strip()
        items = list(offer.get("items") or [])
        summary = msg or title or (items[0] if items else "Sparrow has an offer waiting.")
        if len(summary) > 140:
            summary = summary[:137] + "…"
        created = offer.get("created_at")
        for i, it in enumerate(items[:6], 1):
            steps.append(f"{i}. {it}")
        intent = summary
        queued = max(queued, int(offer.get("queued_behind") or 0))
    else:
        summary = (state.get("waiting_on") or question or "Sparrow needs your decision.")[:160]
        intent = summary

    outbound = bool(
        payload
        or fields.get("to")
        or fields.get("body")
        or bool(__import__("re").search(
            r"email|message|send|post|sms|text|compose", intent or summary or "",
            __import__("re").I))
    )
    if not steps and intent:
        steps = [intent]

    packet_id = (packet or {}).get("packet_id")
    payload_hash = (packet or {}).get("payload_hash")
    expires_at = (packet or {}).get("expires_at")
    sig = f"{kind}|{summary}|{queued}|{bool(awaiting)}|{packet_id}|{payload_hash}"
    return {
        "pending": True,
        "summary": summary,
        "age_label": _age_label(created),
        "queued": queued,
        "queue_href": "/chat",
        "review_href": "/chat",
        "kind": kind,
        "intent": intent or summary,
        "steps": steps,
        "payload": payload,
        "outbound": outbound,
        "created_at": created,
        "sig": sig,
        "offer": offer,
        "packet": packet,
        "packet_id": packet_id,
        "payload_hash": payload_hash,
        "expires_at": expires_at,
    }


def render_banner_html(state: dict[str, Any] | None = None, *, next_url: str = "/") -> str:
    """SSR banner. Hidden when nothing pending; real forms always present for no-JS.

    When a bound approval packet is present, Yes/No POST to
    `/approval/{packet_id}/decide` with `payload_hash` (plan 0.6). Offers
    without a packet keep the legacy `/approvals/resolve` path.
    """
    s = state or {"pending": False}
    on = " on" if s.get("pending") else ""
    aria = "false" if s.get("pending") else "true"
    summary = _esc(s.get("summary") or "Sparrow needs your decision.")
    age = _esc(s.get("age_label") or "")
    queued = int(s.get("queued") or 0)
    more_hidden = "" if queued > 0 else " hidden"
    more_txt = f"+{queued} more" if queued > 0 else ""
    next_u = _esc(next_url)
    pid = s.get("packet_id")
    phash = _esc(s.get("payload_hash") or "")
    if pid is not None and s.get("payload_hash"):
        action = f"/approval/{int(pid)}/decide"
        yes_fields = (
            f'<input type="hidden" name="payload_hash" value="{phash}">'
            f'<input type="hidden" name="decision" value="approve">'
            f'<input type="hidden" name="approved_via" value="button">'
            f'<input type="hidden" name="next" value="{next_u}">'
        )
        no_fields = (
            f'<input type="hidden" name="payload_hash" value="{phash}">'
            f'<input type="hidden" name="decision" value="cancel">'
            f'<input type="hidden" name="approved_via" value="button">'
            f'<input type="hidden" name="next" value="{next_u}">'
        )
    else:
        action = "/approvals/resolve"
        yes_fields = (
            f'<input type="hidden" name="accept" value="1">'
            f'<input type="hidden" name="next" value="{next_u}">'
        )
        no_fields = (
            f'<input type="hidden" name="accept" value="0">'
            f'<input type="hidden" name="next" value="{next_u}">'
        )
    return f"""\
<aside id="mnemosApproval" class="{on.strip()}" aria-hidden="{aria}" role="status"
       aria-live="polite"
       data-packet-id="{_esc(pid) if pid is not None else ''}"
       data-payload-hash="{phash}">
  <span class="ap-dot" aria-hidden="true"></span>
  <span class="ap-sum">{summary}</span>
  <span class="ap-age">{age}</span>
  <a class="ap-more" href="/chat"{more_hidden}>{more_txt}</a>
  <div class="ap-actions">
    <form method="post" action="{action}" class="approval-form" style="display:inline">
      {yes_fields}
      <button type="submit" class="go">Yes — proceed</button>
    </form>
    <form method="post" action="{action}" class="approval-form" style="display:inline">
      {no_fields}
      <button type="submit" class="quiet">Not now</button>
    </form>
    <a class="review" href="/chat">Review</a>
  </div>
</aside>"""


def render_action_detail_html(state: dict[str, Any]) -> str:
    """Expandable plan/payload for mobile consent (§4)."""
    if not state.get("pending"):
        return ""
    open_attr = " open" if state.get("outbound") else ""
    intent = _esc(state.get("intent") or state.get("summary") or "")
    steps = "".join(f"<li>{_esc(st)}</li>" for st in (state.get("steps") or []))
    payload = (state.get("payload") or "").strip()
    payload_html = (
        f'<div class="payload">{_esc(payload)}</div>' if payload else ""
    )
    return f"""\
<details class="action-detail"{open_attr}>
  <summary>What will happen</summary>
  <div class="detail-card">
    <p class="intent">{intent}</p>
    <ol class="steps">{steps}</ol>
    {payload_html}
  </div>
</details>"""


def inject_page(html_page: str, *, next_url: str = "/", agent_worker=None) -> str:
    """Fill @@APPROVAL@@ (or insert after first </header>) with live banner."""
    state = collect_state(agent_worker)
    banner = render_banner_html(state, next_url=next_url)
    out = html_page
    if "@@APPROVAL@@" in out:
        out = out.replace("@@APPROVAL@@", banner)
    elif "</header>" in out:
        out = out.replace("</header>", "</header>\n" + banner, 1)
    return out


def resolve(agent_worker, accept: bool) -> dict[str, Any]:
    """Forward yes/no to the existing offer / ask_human paths.

    When a bound approval packet is pending, prefer the hash-checked decide
    path (plan 0.6) so a bare Yes cannot authorize a stale packet.
    """
    if agent_worker is None:
        return {"ok": False, "error": "agent disabled"}
    try:
        _, state = agent_worker.snapshot(10**9)
    except Exception:
        state = {}
    # Prefer structured approval / ask_human when awaiting.
    if state.get("awaiting"):
        pkt = state.get("packet") if isinstance(state.get("packet"), dict) else {}
        pid, phash = pkt.get("packet_id"), pkt.get("payload_hash")
        if pid is not None and phash and hasattr(agent_worker, "decide_approval"):
            try:
                return agent_worker.decide_approval(
                    int(pid), str(phash),
                    "approve" if accept else "cancel",
                    approved_via="button")
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        try:
            text = "yes" if accept else "no"
            return agent_worker.handle_reply(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    try:
        return agent_worker.resolve_todo(bool(accept))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def decide(agent_worker, packet_id: int, payload_hash: str, decision: str, *,
           user_edit: str | None = None, fields: dict | None = None,
           approved_via: str = "button") -> dict[str, Any]:
    """Bound decide entry point used by POST /approval/{id}/decide."""
    if agent_worker is None:
        return {"ok": False, "error": "agent disabled"}
    if not hasattr(agent_worker, "decide_approval"):
        return {"ok": False, "error": "decide_approval unavailable"}
    try:
        return agent_worker.decide_approval(
            int(packet_id), str(payload_hash or ""), decision,
            user_edit=user_edit, fields=fields, approved_via=approved_via)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
