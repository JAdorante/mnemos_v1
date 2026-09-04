"""Phase 5 substrate — recording agent runs, action packets, and feedback.

The Personal Agent Layer sits above the browser and desktop agents; this module
is the thin, surface-agnostic recorder both of them write through, so every run,
every compiled action packet, and every human verdict (approve / edit / cancel)
lands in Sparrow's canonical store (data/quill.db) next to facts, people, and the
graph — instead of evaporating inside the browser orchestrator.

Design mirrors the rest of app/services:
  * best-effort — a logging side-effect must never break an agent run, so every
    method swallows its own errors (same posture as task_offer / the mem calls);
  * store injected or lazy — takes an explicit Store for tests, else the shared
    canonical singleton;
  * no hard dependency from browser_agent — the orchestrator receives a Recorder
    the same way it receives its memory_provider, keeping browser_agent
    importable on its own.

One Recorder is created per Agent. A single agent runs one goal at a time (the
browser worker is single-threaded by design), so the active run id is held on
the instance and implicitly threads through record_* calls.
"""
from __future__ import annotations

from dataclasses import dataclass, field


def canonicalize_packet_fields(fields: dict | None) -> str:
    """Stable JSON for executable packet args (delegates to storage)."""
    from app.storage import canonicalize_packet_fields as _canon
    return _canon(fields)


def hash_packet_payload(fields: dict | None) -> str:
    """sha256 of canonical fields — mint at record, re-check at commit."""
    from app.storage import hash_packet_payload as _hash
    return _hash(fields)


@dataclass
class ActionPacket:
    """The structured, source-grounded unit the brain hands to the hands.

    A superset of the browser agent's approval packet: the same what/why/source
    fields, plus the routing and safety metadata (risk, surface, success
    criteria, fallback) the Personal Agent Layer compiles from memory. Persisted
    once via Recorder.record_packet, then annotated with the human's decision.
    """

    goal: str = ""
    summary: str = ""
    fields: dict = field(default_factory=dict)       # action/to/subject/body/why/source
    context: list = field(default_factory=list)      # memories used to ground it
    source_fact_ids: list = field(default_factory=list)
    approval_required: bool = True
    risk_level: str | None = None
    suggested_agent: str | None = None
    execution_surface: str | None = None
    success_criteria: list = field(default_factory=list)
    fallback: str | None = None


class Recorder:
    """Best-effort writer for agent_runs / action_packets / agent_steps /
    agent_feedback. Held per Agent; the active run id lives on the instance."""

    # decision -> feedback_type. The `edit` verdict carries the revision text,
    # which is the single richest training signal and used to be discarded.
    _FB = {"approve": "approved", "edit": "edited", "cancel": "cancelled"}

    def __init__(self, store=None):
        self._store = store
        self.current_run_id: int | None = None

    def _s(self):
        if self._store is None:
            from app.storage import get_store
            self._store = get_store()
        return self._store

    # --- run lifecycle -----------------------------------------------------
    def start_run(self, goal: str, *, surface: str | None = None,
                  dry_run: str | None = None, agent_type: str | None = None,
                  intent: str | None = None, risk_level: str | None = None,
                  source_fact_ids: list | None = None,
                  correlation_id: str | None = None) -> int | None:
        try:
            rid = self._s().start_agent_run(
                goal, surface=surface, dry_run=dry_run, agent_type=agent_type,
                intent=intent, risk_level=risk_level,
                source_fact_ids=source_fact_ids, correlation_id=correlation_id)
            self.current_run_id = rid
            return rid
        except Exception as exc:
            print(f"[agent-log] start_run skipped ({exc}).")
            self.current_run_id = None
            return None

    def annotate_run(self, **fields) -> None:
        try:
            if self.current_run_id is not None:
                self._s().annotate_agent_run(self.current_run_id, **fields)
        except Exception as exc:
            print(f"[agent-log] annotate_run skipped ({exc}).")

    def finish_run(self, *, status: str, cost: float | None = None,
                   steps: int | None = None, success_score: float | None = None,
                   failure_reason: str | None = None) -> None:
        try:
            if self.current_run_id is not None:
                self._s().finish_agent_run(
                    self.current_run_id, status=status, cost=cost, steps=steps,
                    success_score=success_score, failure_reason=failure_reason)
        except Exception as exc:
            print(f"[agent-log] finish_run skipped ({exc}).")
        finally:
            self.current_run_id = None

    # --- packets + verdicts ------------------------------------------------
    def record_packet(self, *, summary: str = "", fields: dict | None = None,
                      goal: str = "", context: list | None = None,
                      source_fact_ids: list | None = None,
                      approval_required: bool = True, risk_level: str | None = None,
                      suggested_agent: str | None = None,
                      execution_surface: str | None = None,
                      success_criteria: list | None = None,
                      fallback: str | None = None) -> int | None:
        try:
            return self._s().record_action_packet(
                agent_run_id=self.current_run_id, goal=goal, summary=summary,
                fields=fields, context=context, source_fact_ids=source_fact_ids,
                approval_required=approval_required, risk_level=risk_level,
                suggested_agent=suggested_agent, execution_surface=execution_surface,
                success_criteria=success_criteria, fallback=fallback)
        except Exception as exc:
            print(f"[agent-log] record_packet skipped ({exc}).")
            return None

    def record_from_packet(self, packet: ActionPacket) -> int | None:
        """Persist a fully-formed ActionPacket (the brain's compiled unit)."""
        return self.record_packet(
            summary=packet.summary, fields=packet.fields, goal=packet.goal,
            context=packet.context, source_fact_ids=packet.source_fact_ids,
            approval_required=packet.approval_required, risk_level=packet.risk_level,
            suggested_agent=packet.suggested_agent,
            execution_surface=packet.execution_surface,
            success_criteria=packet.success_criteria, fallback=packet.fallback)

    def record_decision(self, packet_id: int | None, decision: str, *,
                        user_edit: str | None = None,
                        approved_via: str | None = None) -> None:
        """Stamp the packet's decision and log the matching feedback row. On an
        `edit`, user_edit is the revision instruction — the signal we keep.
        `approved_via` is `button` or `typed` (plan 0.6)."""
        try:
            s = self._s()
            if packet_id is not None:
                s.set_packet_decision(packet_id, decision,
                                      approved_via=approved_via)
            s.record_agent_feedback(
                self.current_run_id, self._FB.get(decision, decision),
                packet_id=packet_id, user_edit=(user_edit or None))
        except Exception as exc:
            print(f"[agent-log] record_decision skipped ({exc}).")

    def set_executed_hash(self, packet_id: int | None, executed_hash: str) -> None:
        """Stamp verified commit hash for duplicate-send refusal (plan 0.8)."""
        try:
            if packet_id is not None and executed_hash:
                self._s().set_packet_executed_hash(packet_id, executed_hash)
        except Exception as exc:
            print(f"[agent-log] set_executed_hash skipped ({exc}).")

    def find_recent_executed(self, executed_hash: str, *,
                             within_s: float = 3600.0) -> dict | None:
        """Lookup a verified same-hash send in the last `within_s` seconds."""
        try:
            if executed_hash:
                return self._s().find_recent_executed_hash(
                    executed_hash, within_s=within_s)
        except Exception as exc:
            print(f"[agent-log] find_recent_executed skipped ({exc}).")
        return None

    def record_feedback(self, feedback_type: str, *, packet_id: int | None = None,
                        user_edit: str | None = None, notes: str | None = None) -> None:
        try:
            self._s().record_agent_feedback(
                self.current_run_id, feedback_type, packet_id=packet_id,
                user_edit=user_edit, notes=notes)
        except Exception as exc:
            print(f"[agent-log] record_feedback skipped ({exc}).")

    # --- steps -------------------------------------------------------------
    def record_steps(self, steps: list[dict]) -> None:
        try:
            if self.current_run_id is not None and steps:
                self._s().record_agent_steps(self.current_run_id, steps)
        except Exception as exc:
            print(f"[agent-log] record_steps skipped ({exc}).")
