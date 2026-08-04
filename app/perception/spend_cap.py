"""Hard USD/day spend cap on ambient cloud enrichment (SECURITY #2).

Nothing ambient may call a remote model unmetered. The ledger lives in
perception.db (`spend_ledger`, keyed by UTC day); enforcement happens BEFORE
the call at the two seams every ambient cloud call goes through:

  * VLMRouter.describe — every cloud escalation of a frame
  * ModelRouter._complete_claude — text tasks in the ambient set

Recording happens at the one place cost is already computed: model_log
.log_call (cloud provider + ambient task -> add to the ledger). User-initiated
work (chat, plan, the browser agent) is deliberately NOT drawn from this
budget — the cap exists so *ambient* capture can never silently run up a
bill, not to ration the user's own requests.

QUILL_CLOUD_BUDGET_USD_DAY: default 2.0. 0 (or negative) = uncapped — the
explicit escape hatch documented in MIGRATION.md, never the shipped default.
"""
from __future__ import annotations

import os


class BudgetExhausted(RuntimeError):
    """Raised at an enforcement seam when the day's cloud budget is spent."""


DEFAULT_BUDGET_USD_DAY = 2.0
# Task names as they appear in model_log rows / router calls. `vision` is the
# cloud VLM (ClaudeVLM/Gemini log task="vision"); the rest are the always-on
# text jobs. Chat/plan are absent on purpose (user-initiated).
DEFAULT_AMBIENT_TASKS = ("vision", "extract", "reflect", "activity",
                         "screen_extract", "consolidate", "enhance")


def budget_usd_day() -> float:
    """Live-read the budget so a console kill-switch takes effect without a
    restart (settings dataclasses are frozen at import)."""
    raw = os.environ.get("QUILL_CLOUD_BUDGET_USD_DAY", "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return DEFAULT_BUDGET_USD_DAY


def ambient_tasks() -> frozenset[str]:
    raw = os.environ.get("QUILL_BUDGET_AMBIENT_TASKS", "").strip()
    if raw:
        return frozenset(t.strip() for t in raw.split(",") if t.strip())
    return frozenset(DEFAULT_AMBIENT_TASKS)


class SpendCap:
    def _store(self):
        from app.perception.store import get_pstore
        return get_pstore()

    def is_ambient(self, task: str) -> bool:
        return task in ambient_tasks()

    def allow(self, task: str) -> bool:
        """True when a cloud call for `task` may proceed. Non-ambient tasks
        always pass. Fails CLOSED for ambient tasks: if the ledger cannot be
        read, the cloud call does not happen (capture keeps running local)."""
        if not self.is_ambient(task):
            return True
        budget = budget_usd_day()
        if budget <= 0:
            return True         # explicit uncapped escape hatch
        try:
            spent = float(self._store().day_spend()["total_usd"])
        except Exception as exc:
            print(f"[spend_cap] ledger unreadable ({exc}); denying cloud "
                  f"call for ambient task {task!r}.")
            return False
        if spent >= budget:
            try:
                self._store().bump_denied(task)
            except Exception:
                pass
            return False
        return True

    def check(self, task: str) -> None:
        """allow() or raise BudgetExhausted — for call sites that prefer the
        exception to thread through existing keep-local error handling."""
        if not self.allow(task):
            raise BudgetExhausted(
                f"cloud budget ${budget_usd_day():.2f}/day exhausted "
                f"(task={task}); staying local until the next UTC day")

    def record(self, usd: float, task: str) -> None:
        """Add real spend to today's ledger (called from model_log for cloud
        providers on ambient tasks). Best-effort — metering must never break
        the call it meters."""
        if usd <= 0:
            return
        try:
            self._store().add_spend(usd, task)
        except Exception as exc:
            print(f"[spend_cap] record skipped ({exc}).")

    def status(self) -> dict:
        budget = budget_usd_day()
        try:
            day = self._store().day_spend()
        except Exception as exc:
            return {"ok": False, "error": str(exc), "budget_usd_day": budget}
        remaining = (None if budget <= 0
                     else round(max(0.0, budget - day["total_usd"]), 6))
        return {"ok": True, "budget_usd_day": budget, "uncapped": budget <= 0,
                "spent_usd": day["total_usd"], "remaining_usd": remaining,
                "denied_today": day["denied"], "by_task": day["by_task"],
                "day": day["day"], "ambient_tasks": sorted(ambient_tasks())}


spend_cap = SpendCap()
