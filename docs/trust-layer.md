# Mnemos trust layer

A spec others can implement. Every claim points at the file that enforces it. Memory informs drafts; only a **live human reply** authorizes anything irreversible.

## 1. Threat model — perception is attacker-influenceable

Anything said in the room, shown to a camera, pushed as a notification, ingested from email/calendar metadata, a peer assistant, or a wearable can be crafted by someone who is not the user.

**Rule:** retrieved memory, peer answers, exhaust-ingest rows, and external capture **must not authorize**. They may appear in an approval packet as *Why / Source*. The authorizing act is a live human Approve / Edit / Cancel on that packet.

Code: `app/services/trust.py` (`source_can_authorize` — always `False` for memory sources; re-exported from `app/services/agent_planner.py`), `app/services/external_capture.py` (`never_authorizes_event`), `app/services/peer_channel.py` (inbound asks are observed-tier).

## 2. Risk is a table, not an LLM guess

`classify_risk(action_kind)` looks up `RISK_TABLE` (`low | medium | high | blocked`). `blocked` never reaches an execution surface (`execution_allowed`, `is_policy_blocked`). Sensitive-domain words can raise `low` → `medium`, never invent a new class.

Code: `app/services/trust.py` (`RISK_TABLE`, `classify_risk`); `app/services/agent_planner.py` (`risk_of`, `execution_allowed`) re-exports the table.

## 3. Readiness bands

One score maps to `auto | offer | review | hold`. High-risk actions never `auto`. `QUILL_AUTO_ACT` defaults off, so the system stays ask-first.

Code: `app/services/readiness.py`.

## 4. Hash-bound approvals

`QUILL_APPROVAL_BIND` = `off | shadow | enforce` (code default **enforce**).

What the hash binds: the approval packet fields the human saw (Action / To / Subject / Body / Why / Source). After Approve, the executor re-hashes the about-to-run args. Drift or expiry → fresh ask. Duplicate sends are caught.

Code: `desktop_agent/config.py` (`APPROVAL_BIND`), `app/services/trust.py` (`approval_binding_is_enforce`). Browser commit gate lives with the Exec.AI agent (`browser_agent/`).

## 5. Approval packets

A packet is the unit the human sees:

- **Action** — what will happen
- **To / Subject / Body** — when it is a message
- **Why** — grounded memory lines (context, not authority)
- **Source** — fact ids / event ids / quotes

Human verdicts (approve / edit / cancel) are recorded on the agent run.

Code: `app/services/agent_log.py`, `app/services/agent_planner.py` (compiled `ActionPacket`s).

## 6. Evidence-verified outcomes

A model claiming “I sent it” is not an outcome. Surfaces must verify with evidence (sent-state, DOM, file mtime) or report a typed failure. Failure taxonomy lives with the browser agent (plan / navigate / draft / approval / full dry-run levels).

Code: `browser_agent/` executor + verifier; `AGENT_DRY_RUN`.

## 7. Desktop allowlist-as-sandbox

The allowlist *is* the sandbox: path jail (`QUILL_DESKTOP_JAIL`), app allowlist, shell-verb allowlist, hard-block list, `shell=False`, action budgets, file-size cap. Approval still required for mutating verbs when `QUILL_DESKTOP_APPROVAL=1`.

Code: `desktop_agent/config.py`, `tests/test_desktop_guards.py`.

## 8. Source policies

`data/source_policies.json` bounds what each source class may mint (people, contacts, commitments, claims). Missing table → deny, never allow (`app/services/source_policy.py` `_DEFAULT_POLICY`). Exhaust metadata (`exhaust`) may mint candidate people and asserted `works_at`, never commitments/claims.

## 9. Implementing this elsewhere

Minimum viable port:

1. A risk table with a `blocked` class that execution cannot override.
2. An approval packet whose bytes are hashed; execute only if hash matches and unexpired.
3. A hard rule that no retrieved context bit can flip “approved” to true.
4. A source-class policy file that fails closed.

Do not put capture, memory, or model-routing imports into the approval/risk modules. The portable core is `app/services/trust.py` (`classify_risk`, `source_can_authorize`, `approval_binding_is_enforce`, `RISK_TABLE`). `agent_planner.py` re-exports those names so existing callers do not change.
