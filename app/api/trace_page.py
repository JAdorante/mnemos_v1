"""Correlation trace page (plan 1.6) — read-only, server-rendered.

Renders the full audit chain for one correlation_id: source events -> the raw
fact_candidates rows -> materialized facts -> agent_runs, so a Console user
can follow one utterance all the way to whatever it produced. Text is escaped
and inlined server-side (no client JS, nothing re-interpreted as markup) —
deliberately the plainest possible page, mirroring triggers_page.py.
"""
from __future__ import annotations

import html


def _esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def _table(rows: list[dict], columns: list[tuple[str, str]]) -> str:
    if not rows:
        return '<div class="empty">none</div>'
    head = "".join(f"<th>{_esc(label)}</th>" for _, label in columns)
    body = []
    for r in rows:
        cells = "".join(f"<td>{_esc(r.get(key, ''))}</td>" for key, _ in columns)
        body.append(f"<tr>{cells}</tr>")
    return f'<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table>'


def render_trace_page(correlation_id: str, chain: dict) -> str:
    events = chain.get("events") or []
    candidates = chain.get("candidates") or []
    facts = chain.get("facts") or []
    agent_runs = chain.get("agent_runs") or []

    events_html = _table(events, [
        ("id", "id"), ("time", "time"), ("modality", "modality"),
        ("source", "source"), ("raw", "raw"),
    ])
    candidates_html = _table(candidates, [
        ("id", "id"), ("kind", "kind"), ("status", "status"),
        ("assertion", "assertion"), ("confidence", "confidence"),
        ("verdict_reason", "verdict_reason"), ("source_span", "source_span"),
    ])
    facts_html = _table(facts, [
        ("id", "id"), ("kind", "kind"), ("text", "text"),
        ("confidence", "confidence"), ("source_event_id", "source_event_id"),
        ("state", "state"),
    ])
    runs_html = _table(agent_runs, [
        ("id", "id"), ("goal", "goal"), ("status", "status"),
        ("agent_type", "agent_type"), ("started_at", "started_at"),
    ])

    return f"""<!doctype html>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trace {_esc(correlation_id)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 980px;
         margin: 40px auto; padding: 0 16px; }}
  h1 {{ font-size: 20px; }}
  h1 + p {{ color: gray; font: 13px/1.4 ui-monospace, monospace; }}
  h2 {{ font-size: 15px; margin: 26px 0 8px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 8px; vertical-align: top;
            border-bottom: 1px solid color-mix(in srgb, gray 30%, transparent); }}
  th {{ color: gray; font-weight: 600; }}
  td {{ max-width: 360px; overflow-wrap: anywhere; }}
  .empty {{ color: gray; font-size: 13px; padding: 6px 0; }}
  .count {{ color: gray; font-size: 12px; font-weight: 500; }}
</style>
<h1>Trace</h1>
<p>correlation_id: {_esc(correlation_id)}</p>

<h2>Events <span class="count">({len(events)})</span></h2>
{events_html}

<h2>Fact candidates <span class="count">({len(candidates)})</span></h2>
{candidates_html}

<h2>Facts <span class="count">({len(facts)})</span></h2>
{facts_html}

<h2>Agent runs <span class="count">({len(agent_runs)})</span></h2>
{runs_html}
"""
