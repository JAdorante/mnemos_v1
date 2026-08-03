"""Global approval affordance — one source of truth for banner / Today / Chat.

Copper here is legitimate: a human decision is required.
Yes/No are real POST forms (no-JS). JS enhances for inline dismiss + SSE.
"""
from __future__ import annotations

import html
import time
from typing import Any


APPROVAL_CSS = """\
#vinceoApproval{
  display:none;align-items:center;gap:12px;flex-wrap:wrap;
  min-height:44px;padding:8px 22px;
  background:linear-gradient(180deg,#FFF8F0 0%,rgba(248,246,241,.97) 100%);
  border-bottom:1px solid rgba(184,115,51,.28);
  border-top:1px solid rgba(184,115,51,.18);
  font:14px/1.35 var(--font);color:var(--navy);
  animation:approvalSlide .28s var(--ease) both;
}
#vinceoApproval.on{display:flex}
#vinceoApproval .ap-dot{
  width:8px;height:8px;border-radius:50%;background:var(--acc);flex:0 0 auto;
}
#vinceoApproval .ap-sum{flex:1;min-width:12rem}
#vinceoApproval .ap-age{
  font:11px var(--mono);color:var(--mut);white-space:nowrap;
}
#vinceoApproval .ap-more{
  font:11px var(--mono);color:var(--acc);border:1px solid rgba(184,115,51,.35);
  border-radius:999px;padding:2px 8px;text-decoration:none;white-space:nowrap;
}
#vinceoApproval .ap-actions{display:flex;gap:8px;flex-wrap:wrap;margin-left:auto}
#vinceoApproval .ap-actions button,#vinceoApproval .ap-actions a{
  border-radius:10px;padding:7px 12px;font:500 13px var(--font);cursor:pointer;
  border:1px solid var(--line);background:var(--panel);color:var(--navy);
  text-decoration:none;display:inline-flex;align-items:center;
}
#vinceoApproval .ap-actions .go{background:var(--navy);color:#F8F6F1;border:none}
#vinceoApproval .ap-actions .quiet{background:transparent;color:var(--mut)}
#vinceoApproval .ap-actions .review{color:var(--mut)}
@keyframes approvalSlide{
  from{opacity:0;transform:translateY(-6px)}
  to{opacity:1;transform:none}
}
@media (prefers-reduced-motion:reduce){
  #vinceoApproval{animation:none}
}
.action-detail{margin:10px 0 4px}
.action-detail > summary{
  cursor:pointer;font:500 13px var(--font);color:var(--navy);list-style:none;
}
.action-detail > summary::-webkit-details-marker{display:none}
.action-detail > summary::before{content:"▸ ";color:var(--acc);font-size:11px}
.action-detail[open] > summary::before{content:"▾ "}
.action-detail .detail-card{
  margin-top:8px;padding:12px 14px;border:1px solid rgba(184,115,51,.22);
  border-radius:12px;background:linear-gradient(180deg,#FFFCF7 0%,var(--surface) 100%);
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
window.VinceoApprovals = {
  _es: null,
  _lastSig: '',
  refresh() {
    fetch('/approvals/state').then(r => r.json()).then(s => {
      this.render(s);
      try { window.dispatchEvent(new CustomEvent('vinceo:approval', {detail: s})); } catch (e) {}
    }).catch(() => {});
  },
  render(s) {
    const bar = document.getElementById('vinceoApproval');
    if (!bar) return;
    const pending = !!(s && s.pending);
    bar.classList.toggle('on', pending);
    bar.setAttribute('aria-hidden', pending ? 'false' : 'true');
    if (!pending) return;
    const sum = bar.querySelector('.ap-sum');
    const age = bar.querySelector('.ap-age');
    const more = bar.querySelector('.ap-more');
    if (sum) sum.textContent = s.summary || 'Vinceo needs your decision.';
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
    if (this._es || typeof EventSource === 'undefined') {
      this.refresh();
      if (!this._es) setInterval(() => this.refresh(), 8000);
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
          try { window.dispatchEvent(new CustomEvent('vinceo:approval', {detail: s})); } catch (e) {}
        } catch (e) {}
      });
      this._es.onerror = () => { /* browser will retry */ };
    } catch (e) {
      this.refresh();
      setInterval(() => this.refresh(), 8000);
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
        fetch('/approvals/resolve', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json'},
          body: body.toString(),
        }).then(() => {
          this.refresh();
          try { window.dispatchEvent(new CustomEvent('vinceo:approval-resolved')); } catch (e) {}
        }).catch(() => { form.submit(); });
      });
    });
  }
};
document.addEventListener('DOMContentLoaded', () => {
  if (window.VinceoApprovals) {
    VinceoApprovals.enhanceForms();
    VinceoApprovals.connect();
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
        summary = intent or (packet.get("summary") or "Vinceo needs approval to act.")
        if not summary.lower().startswith("vinceo"):
            summary = f"Vinceo wants to {summary[0].lower() + summary[1:]}" if summary else summary
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
        summary = msg or title or (items[0] if items else "Vinceo has an offer waiting.")
        if len(summary) > 140:
            summary = summary[:137] + "…"
        created = offer.get("created_at")
        for i, it in enumerate(items[:6], 1):
            steps.append(f"{i}. {it}")
        intent = summary
        queued = max(queued, int(offer.get("queued_behind") or 0))
    else:
        summary = (state.get("waiting_on") or question or "Vinceo needs your decision.")[:160]
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

    sig = f"{kind}|{summary}|{queued}|{bool(awaiting)}"
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
    }


def render_banner_html(state: dict[str, Any] | None = None, *, next_url: str = "/") -> str:
    """SSR banner. Hidden when nothing pending; real forms always present for no-JS."""
    s = state or {"pending": False}
    on = " on" if s.get("pending") else ""
    aria = "false" if s.get("pending") else "true"
    summary = _esc(s.get("summary") or "Vinceo needs your decision.")
    age = _esc(s.get("age_label") or "")
    queued = int(s.get("queued") or 0)
    more_hidden = "" if queued > 0 else " hidden"
    more_txt = f"+{queued} more" if queued > 0 else ""
    next_u = _esc(next_url)
    return f"""\
<aside id="vinceoApproval" class="{on.strip()}" aria-hidden="{aria}" role="status">
  <span class="ap-dot" aria-hidden="true"></span>
  <span class="ap-sum">{summary}</span>
  <span class="ap-age">{age}</span>
  <a class="ap-more" href="/chat"{more_hidden}>{more_txt}</a>
  <div class="ap-actions">
    <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
      <input type="hidden" name="accept" value="1">
      <input type="hidden" name="next" value="{next_u}">
      <button type="submit" class="go">Yes — proceed</button>
    </form>
    <form method="post" action="/approvals/resolve" class="approval-form" style="display:inline">
      <input type="hidden" name="accept" value="0">
      <input type="hidden" name="next" value="{next_u}">
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
    """Forward yes/no to the existing offer / ask_human paths."""
    if agent_worker is None:
        return {"ok": False, "error": "agent disabled"}
    try:
        _, state = agent_worker.snapshot(10**9)
    except Exception:
        state = {}
    # Prefer structured approval / ask_human when awaiting.
    if state.get("awaiting"):
        try:
            text = "yes" if accept else "no"
            return agent_worker.handle_reply(text)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    try:
        return agent_worker.resolve_todo(bool(accept))
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
