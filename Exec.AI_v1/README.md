# Exec.AI — QUILL Autonomous Browser Agent

The **LLM + Autonomous Browser Agent** component of QUILL ([FS-BA-001](./FS-BA-001.md)).
It drives a real browser the way a human does — observe the page, reason, act —
from a natural-language goal, routed and approval-gated.

> **Scope:** it **prepares** actions freely (navigate, read, search, fill forms,
> write email drafts) but **stops before anything irreversible** — sending,
> submitting, buying, deleting, or changing a saved record needs your one-tap
> approval. It never types your password: you sign in once by hand (session
> reuse), and it reuses that session. Read-only tasks (summarize, look up a
> price, find a contact) need no login or approval at all.
>
> Built increments: **intent/action router** · **session reuse (login once)** ·
> **approval gate**. See the phase table at the bottom for what's still ahead.

## How it works

```
your request
   │
   ▼
Router (Sonnet 4.6) ── intent, requires_browser, requires_approval ──┐
   │  no web action needed?  → answer directly                       │
   ▼                                                                 │
Planner (Opus 4.8) ── task graph ──┐                                 │
                                   ▼                                 │
                 ┌──── executor loop ────────────────────┐           │
                 │  observe → act → verify               │           │
                 │  Sonnet 4.6 picks one action          │           │
                 │  Haiku 4.5 checks the post-condition  │           │
                 │  stuck 2× → escalate to Opus          │           │
                 │  stuck 3× → re-plan → ask_human        │           │
                 └───────────────┬────────────────────────┘           │
                                 ▼                                     ▼
        Playwright browser  ·  SQLite episodic log + QUILL event rows
```

- **Intent/action router** ([llm.py](browser_agent/llm.py) `route()`) — QUILL's
  planner front-end: one cheap Sonnet call per request emits `{intent,
  requires_browser, requires_user_approval, tool, site}`. No-browser requests
  (memory/conversational questions) are answered directly; browser requests flow
  into the planner. The envelope is stored as a QUILL `event` row. Inspect it
  without running anything via `/route <request>` in the chat.
- **Perception** ([perception.py](browser_agent/perception.py)) — scans the DOM/ARIA
  layer for interactive elements, assigns each a stable integer `element_id`,
  and gives Claude a numbered list. Claude acts by `element_id`, never by raw
  selector.
- **Action vocabulary** ([tools.py](browser_agent/tools.py)) — `click`, `type`,
  `select`, `scroll`, `navigate`, `go_back`, `wait_for`, `read`, `ask_human`,
  `request_approval`, `done`. Each maps to one deterministic Playwright call.
- **Approval gate** ([orchestrator.py](browser_agent/orchestrator.py)) — the
  agent may prepare freely, but an irreversible step needs a human OK. Two
  layers: the model calls `request_approval` before committing, **and** a
  non-LLM guard stops any click on a control whose name matches a commit
  pattern (Send/Submit/Buy/Delete…) even if the model didn't ask — so a
  mis-classifying model can't send or buy on its own. Approve/Deny buttons
  appear in the web UI; type `approve` in the terminal.
- **Tiered models** ([config.py](browser_agent/config.py)) — Opus plans, Sonnet
  executes, Haiku verifies; `effort` is wired as config, not a constant.
- **Episodic log** ([memory.py](browser_agent/memory.py)) — every step (action,
  redacted args, model, verification, tokens, screenshot + AX snapshot refs) is
  written to `sessions/episodic.db`, so any run is replayable.
- **Learning layer / procedural memory** ([memory.py](browser_agent/memory.py),
  [orchestrator.py](browser_agent/orchestrator.py)) — after each goal the agent
  distills the trajectory into a page-independent recipe (the winning path) plus
  the verify-failure lessons ("x.com profile → wait for render"; "Gmail draft →
  use the compose deep-link"), keyed by `(intent, site)` in a `skills` table.
  Next time that intent hits that site, the recalled path and pitfalls are fed to
  the planner — fewer steps, lower cost, no repeated mistakes. The stored recipe
  keeps trending toward the shortest success, so it compounds with use.

## Setup

```bash
pip install -r requirements.txt
playwright install chromium

# set your key (or use `ant auth login`)
cp .env.example .env        # then edit .env
```

## Run

Three entry points, same agent underneath:

| | Command | Best for |
|---|---|---|
| **Web UI** | `python webapp.py` → open http://127.0.0.1:5000 | clicking around, watching it work |
| **Terminal chat** | `python chat.py` | quick back-and-forth in the console |
| **One-shot** | `python run.py "<task>"` | scripting a single task |

### Web UI

```bash
python webapp.py                 # visible browser + chat at http://127.0.0.1:5000
python webapp.py --headless      # hide the agent's browser window
```

A chat page opens; type a task and watch the agent drive the Chromium window
beside it. The conversation continues across turns (it stays on the last page),
the **Open** box jumps to a URL, **New context** clears the conversation, and if
the agent needs you (login/CAPTCHA), it asks right in the chat box.

### Terminal chat

```bash
python chat.py                   # visible browser; type tasks at the `you >` prompt
```

### Logging in once (session reuse)

The agent never types your password (FR-SEC-1). Instead, **you** sign in by hand
once in the agent's window, and a persistent profile remembers the session so the
next run is already authenticated:

```bash
# real Chrome (rarely blocked at login) + a named profile that persists cookies
python chat.py --chrome --profile gmail --start-url https://mail.google.com
#   → a Chrome window opens on the Gmail login page
#   → sign in yourself (password, 2FA/Duo, everything)
#   → then ask the agent to read: e.g. "summarize the senders and subjects of my
#     10 most recent emails"

# next time, same flags → you're already logged in, skip straight to the task
python chat.py --chrome --profile gmail --start-url https://mail.google.com
```

- `--profile <name>` stores a dedicated browser profile under
  `sessions/profiles/<name>/` (cookies + localStorage). Use different names for
  different accounts.
- `--chrome` drives your installed Chrome instead of bundled Chromium, which
  dodges most "this browser may not be secure" login blocks. (`--channel msedge`
  for Edge; needs that browser installed.)
- Same flags work on `python webapp.py` and `python run.py`.

Once logged in it will read/prepare (draft) but stop before sending or deleting.
If a provider blocks sign-in anyway, it hands off — sign in manually and continue.

#### Or: attach to your own Chrome (best for sites that block automation)

Instead of a separate profile, point the agent at the Chrome **you already use
and are already signed into** — it drives that live session over CDP, so there's
no re-login for Google/Microsoft to block:

```powershell
# 1. Quit Chrome COMPLETELY (all windows — else the flag below is ignored)
# 2. Relaunch Chrome with a debugging port (uses your normal, logged-in profile):
& "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222
# 3. In that Chrome, open the tab you want (e.g. your Gmail inbox)
# 4. Attach the agent:
python chat.py --attach                 # = --cdp http://localhost:9222
```

- The agent acts on your **frontmost/last tab** — leave the target tab active.
- `--attach` works on `chat.py`, `webapp.py`, and `run.py`; closing the agent
  leaves your browser open.
- **Security:** the debugging port lets any local program control your browser.
  Only enable it while you need it, and close Chrome when done.

### One-shot

```bash
# headless
python run.py "Summarize the top 5 stories" --start-url https://news.ycombinator.com

# watch the browser
python run.py "Find the price of the cheapest item on this page" \
  --start-url https://example.com --headful

# tighten the step cap for a quick test
python run.py "What is the main headline?" --start-url https://example.com --max-steps 8
```

Output: the plan, each action as it happens, the final result, an estimated
cost, and token usage. Screenshots + AX snapshots land in `sessions/<id>/`.

## What's intentionally NOT here (later phases)

| Phase | Adds |
|---|---|
| P1 | ✅ session reuse (persistent profile — log in once by hand) · ⬜ credential vault + handle-based injector (secrets never enter the LLM context) · ⬜ CAPTCHA handoff |
| P2 | ✅ approval gate — model-initiated `request_approval` + non-LLM commit-click guard + Approve/Deny UI · ⬜ structured before/after diff preview |
| P3 | payment flows, full audit, ZDR confirmation |
| P4 | computer-use fallback for canvas-heavy sites |

The `redact()` hook in [memory.py](browser_agent/memory.py) and the omitted
`upload`/`request_approval` tools are the seams where P1/P2 plug in.

## Notes / limits

- Single user, single session (matches the PoC scope).
- Postgres in the spec is approximated by SQLite here for zero setup; the schema
  is Postgres-portable — swap the connection in `memory.py` to migrate.
- Prompt caching is enabled on the system+tools prefix (`cache_control`), so the
  per-step executor cost is dominated by the small fresh delta + output.
