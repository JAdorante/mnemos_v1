# FS-BA-001 — Autonomous Browser Agent (Anthropic-backed)

**Status:** Draft v0.1
**Owner:** Justin
**LLM backing:** Anthropic Claude (Messages API)
**Last updated:** 2026-06-30

---

## 1. Purpose & Scope

Define the functional requirements for a proof-of-concept agent that completes
real-world tasks on arbitrary websites by driving a real browser the way a human
does — observing the page, reasoning, and acting (click / type / scroll / select /
upload / navigate) — driven entirely by natural-language prompts.

**In scope (PoC):** read-only tasks (e.g. "summarize my inbox"), search/booking
flows (flights, hotels, food orders), and bill-pay style flows gated behind human
approval. Single user, single concurrent session.

**Out of scope (PoC):** multi-tenant scale, per-site hardcoded scripts, CAPTCHA
auto-solving, headless anti-bot evasion at scale, mobile-app automation.

**Hard constraint:** all reasoning is performed by the Anthropic Messages API. No
secondary LLM provider. No per-website service APIs — the agent interacts only
through the browser surface.

---

## 2. Model Strategy (Anthropic)

Current-generation models and rates (per MTok, standard input/output):

| Model | API ID | Rate | Role in system |
|---|---|---|---|
| Opus 4.8 | `claude-opus-4-8` | $5 / $25 | Planner; hard visual/recovery reasoning; computer-use executor on difficult pages |
| Sonnet 4.6 | `claude-sonnet-4-6` | $3 / $15 | Default executor (per-step action selection) |
| Haiku 4.5 | `claude-haiku-4-5` | $1 / $5 | Cheap classifiers: page-state detection, "is this a login wall?", action-success verification |

**FR-MODEL-1 — Tiered routing.** The system MUST route by task difficulty, not use
a single model for everything. Planner calls (infrequent, high-leverage) use Opus
4.8; the per-step executor loop (frequent) defaults to Sonnet 4.6; high-volume
yes/no perception checks use Haiku 4.5.

**FR-MODEL-2 — Effort control.** Opus 4.8 exposes an `effort` parameter
(low / medium / high / xhigh / max). Planner calls SHOULD run at higher effort;
routine executor steps SHOULD run at low/medium to control token burn. Effort is
the single biggest cost knob — wire it as config, not a constant. `effort` is
nested under `output_config`. Note: Sonnet 4.6 supports low/medium/high/max (not
xhigh); Haiku 4.5 does not support `effort` at all.

**FR-MODEL-3 — Escalation path.** When the executor (Sonnet) reports it is stuck on
the same sub-goal twice, the orchestrator MUST escalate that step to Opus 4.8 before
declaring failure or re-planning.

**FR-MODEL-4 — Computer-use compatibility.** Any model used for the visual
computer-use tool MUST be a compatible model. Confirm the current computer-use tool
`type` + beta header and per-model support against live docs before relying on the
visual path (Opus 4.8 recommended; verify Sonnet 4.6).

---

## 3. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Orchestrator (your code — the agent loop)                   │
│                                                              │
│   User prompt ─► Planner (Opus 4.8) ─► task graph            │
│                       │                                       │
│                       ▼                                       │
│        ┌──────── Executor loop ────────┐                      │
│        │  observe → act → verify       │  ◄── Haiku verifier  │
│        │  (Sonnet 4.6, escalate→Opus)  │                      │
│        └───────────────┬───────────────┘                      │
│                        │ tool calls                           │
│                        ▼                                       │
│   Browser driver (Playwright)  ◄─► Credential vault           │
│   AX-tree + DOM extractor                                     │
│   Screenshot capture                                          │
│                        │                                       │
│   Approval gate ◄──────┘   Episodic log (Postgres)            │
└─────────────────────────────────────────────────────────────┘
```

The orchestrator owns the loop. Claude is the policy. Playwright is the actuator.

**Build decision — Client SDK vs Agent SDK.** Use the **Client SDK** (raw Messages
API) and implement the tool loop yourself. A browser agent needs custom perception
(AX tree), custom action tools, and a custom approval gate — you want full control
of the loop.

---

## 4. Perception

Hybrid, **AX-first** design.

### 4.1 Primary — semantic tools over the accessibility tree
**FR-PERC-1.** Each step, extract interactive elements from the accessibility/ARIA
layer (role, accessible name, state), assign each a stable integer `element_id`,
and pass the indexed list to Claude as text. Claude acts by `element_id`, never by
raw CSS selector.

**FR-PERC-2.** DOM is consulted only when the AX tree is insufficient.

### 4.2 Secondary — vision (screenshots as image blocks)
**FR-PERC-3.** Attach a viewport screenshot as a base64 `image` block for visual
disambiguation, thin-AX canvas/SVG pages, or recovery.

### 4.3 Fallback — Anthropic computer-use tool
**FR-PERC-4.** For pages where DOM/AX is unusable, fall back to the native
computer-use tool (screenshot + mouse/keyboard). Client-side and ZDR-eligible
posture is load-bearing for the credential story (§8) — confirm ZDR enablement at
the account level. The AX-first path is the default because it is cheaper, faster,
and more deterministic.

---

## 5. Planning & Task Decomposition

**FR-PLAN-1.** The Planner (Opus 4.8) MUST convert a goal into an ordered task graph,
marking sub-goals that need approval (§8) or human handoff (e.g. CAPTCHA).
**FR-PLAN-2.** Produce structured output (JSON) via structured output / tool use.
**FR-PLAN-3.** Re-planning is first-class — a normal transition, not an error.

---

## 6. Action Execution (Tool Schema)

**FR-ACT-1.** Expose exactly this action vocabulary (extend only with cause):
`click(element_id)`, `type(element_id, text)`, `select(element_id, option)`,
`scroll(direction, amount?)`, `navigate(url)`, `upload(element_id, file_ref)`,
`go_back`, `wait_for(condition)`, `read(element_id?)`, `ask_human(question)`,
`request_approval(summary, params)`, `done(result)`. `text` must never contain a
raw secret (§8).

**FR-ACT-2.** Each tool maps to a deterministic Playwright call. The LLM never
writes selectors or code.

**FR-ACT-3 — Verify every action.** After each action, re-observe and a Haiku check
confirms the expected post-condition. Failed verification → retry-with-wait, then
escalate, then re-plan. Most failures are timing/race conditions.

---

## 7. Memory & State

**FR-MEM-1 — Working memory (in-context).** Current task graph + recent
action/observation history; summarize/evict old steps to keep context bounded.
**FR-MEM-2 — Episodic memory (durable).** Every step logged: session_id, step, url,
action, args (secrets redacted), screenshot hash, AX snapshot ref, verification,
model, token counts, timestamp, approvals. The audit trail, debugger, and future
training set.
**FR-MEM-3 — Procedural memory (learned skills).** Successful trajectories per
domain stored and retrieved later. Optional for PoC; design the log schema so it is
harvestable.

---

## 8. Security, Credentials & Approvals

**FR-SEC-1 — Secrets never enter the LLM context.** Passwords, card numbers, OTPs,
account numbers MUST NOT appear in any Messages API request/response. The model
references a secret by *handle* (e.g. `{{cred:comcast.password}}`); a deterministic
non-LLM injector substitutes the real value into the browser field, outside the
model's view.
**FR-SEC-2 — Credential vault.** Secrets live in a dedicated vault (cloud KMS /
secrets manager). Prefer reusing stored browser sessions/cookies.
**FR-SEC-3 — Approval gate on irreversible actions.** Auto: read/search/navigate/
filter. Requires confirmation: submit payment, send message, confirm booking,
delete, change settings — pause and surface a structured diff for one-tap approval.
**FR-SEC-4 — CAPTCHA = handoff, not solve.** Detect and pause to the human; no
solving service in the PoC.
**FR-SEC-5 — Isolation & teardown.** Fresh sandboxed browser context per session;
credentials scoped to the task; context torn down after completion.
**FR-SEC-6 — ZDR posture.** Where sensitive screenshots are sent, rely on
computer-use ZDR eligibility and confirm ZDR enablement on the account for all
features in the loop.
**FR-SEC-7 — Immutable audit.** Every action + approval recorded per FR-MEM-2,
append-only.

---

## 9. Error Recovery

| Failure | Handler |
|---|---|
| Action did nothing / post-condition missing | retry with `wait_for`; re-observe |
| Stale element / race | network-idle wait before re-attempt |
| Unexpected redirect / layout | re-observe, re-plan |
| Login wall | detect password field in AX tree → §8 login sub-flow |
| CAPTCHA | detect → `ask_human` handoff (FR-SEC-4) |
| UI drift | tolerated by AX/vision perception; no per-site selectors |
| Repeated stall (2×) | escalate Sonnet→Opus, then re-plan, then `ask_human` |

**Design principle:** an agent that completes 80% and cleanly asks for help on the
rest beats one that silently fails 20% of the time.

---

## 10. Context & Cost Engineering (Anthropic-specific)

**FR-CTX-1 — Prompt caching.** System prompt, tool definitions, and stable prefix
MUST be marked for caching (cached input ~90% cheaper than fresh). Keep tool defs +
system byte-stable; mind cache expiry on long human-approval pauses (5-min TTL
default; 1-hour TTL available).
**FR-CTX-2 — Compaction.** Compact older turns into summaries for long sessions.
**FR-CTX-3 — Tool Search Tool.** Load tool definitions on-demand if the library
grows large.
**FR-CTX-4 — Programmatic Tool Calling.** For deterministic loops over tool results,
keep large intermediates in code, out of context.
**FR-CTX-5 — Batch for offline.** Non-interactive work uses the Batch API (50% off).

---

## 11. Non-Functional Requirements

- **NFR-1 Reliability (PoC):** ≥80% task success on a fixed ~20-task eval; 100% of
  failures end in a clean `ask_human`, never a silent wrong action.
- **NFR-2 Latency:** observe→act→verify under ~6s p50 on the AX path.
- **NFR-3 Safety:** 0 irreversible actions without a recorded approval.
- **NFR-4 Cost:** track cost-per-task; alert on >2× rolling median.
- **NFR-5 Observability:** every session replayable from the episodic log.
- **NFR-6 Loop guard:** hard cap on steps (e.g. 40) and re-plans (e.g. 3) before
  forced `ask_human`.

---

## 12. Recommended Implementation Stack

- **Browser control:** Playwright.
- **Perception/loop:** hand-rolled over Playwright (this repo) or Browser Use.
- **LLM:** Anthropic Messages API (Client SDK), tiered per §2; computer-use fallback.
- **Vault:** cloud secrets manager / KMS.
- **State:** Postgres (episodic + procedural). PoC uses SQLite (portable schema).
- **Hosted browsers (post-PoC):** Browserbase or Steel.

---

## 13. Phased Build

1. **P0 — Read-only loop.** AX perception + action tools + verify + episodic log.
2. **P1 — Login + sessions.** Vault + injector + session reuse + CAPTCHA handoff.
3. **P2 — Approval gate.** Structured diff + one-tap confirm; booking up to payment.
4. **P3 — Payment flows.** Approval-gated bill-pay; full audit; ZDR confirmation.
5. **P4 — Generality.** Computer-use fallback; procedural-memory capture; eval loop.

---

## 14. Open Questions

- Confirm Sonnet 4.6 computer-use support, or restrict the visual path to Opus 4.8.
- ZDR enablement scope across every feature touched in the loop.
- Procedural-memory retrieval design — embeddings vs. domain keying.
- Where the human-approval UX lives and its latency budget vs. cache-expiry.
- Anti-bot / ToS posture per target site.
