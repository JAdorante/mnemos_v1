"""Attention-impressions ledger — Phase 0 of the Cognitive OS roadmap.

The ranking layer has the best feedback infrastructure in the product (verdict
buttons, agent_feedback, the distill trail) and uses none of it. This module
starts fixing that the cheap way: OBSERVE first, learn later. Every node the
constellation field or chat grounding surfaces is written to
`attention_impressions` with the score decomposition it was surfaced with
(the same terms `graph.score_gravity` already computes — just written down),
and every user reaction closes the row:

  pin / unpin / hide / reclassify   constellation edit ops (strong signal)
  click / dwell                     evidence popover opened / held open
  miss                              chat asked about a person the field had
                                    NOT surfaced recently — the ground truth
                                    engagement metrics can't see

Zero behavior change: nothing reads the ledger to rank anything. When learned
ranking lands (field v2), these rows are its training data — and the replay
bench that gates promotion. All local, prunable, and off via
QUILL_ATTENTION_LEDGER=0.

Surfaces instrumented (P0 exit: field / grounding / offers):
  field       constellation render
  grounding   chat compose pull + miss join
  offer       proactive task/todo/phone offers (surfaced + accept/dismiss)

Recording is best-effort and never raises into the caller (cog_telemetry's
contract): instrumentation must not break the surfaces it measures.
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any

from app.config import settings


def _parse_node(nid: str) -> tuple[str, int] | None:
    """'person:12' -> ('person', 12); None for anything malformed."""
    try:
        node_type, raw_id = (nid or "").split(":", 1)
        node_type = node_type.strip()
        if node_type not in ("person", "entity", "fact"):
            return None
        return node_type, int(raw_id)
    except (ValueError, AttributeError):
        return None


class AttentionLedger:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # (surface, node_type, node_id) -> (ts, score, layer) — in-memory
        # throttle so the 4s version-poll refetch loop doesn't write a row per
        # node per fetch. Deliberately not persisted: after a restart one extra
        # impression per node is fine.
        self._last: dict[tuple[str, str, int], tuple[float, float, str | None]] = {}
        self._snapshot_id: int | None = None
        self._snapshot_ts: float = 0.0

    # ------------------------------ helpers ------------------------------
    @staticmethod
    def _enabled() -> bool:
        return settings.attention.enabled

    def _should_write(self, surface: str, node_type: str, node_id: int,
                      score: float, layer: str | None, now: float) -> bool:
        key = (surface, node_type, node_id)
        prev = self._last.get(key)
        if prev is None:
            return True
        p_ts, p_score, p_layer = prev
        if now - p_ts >= settings.attention.throttle_s:
            return True
        if abs(score - p_score) >= settings.attention.rescore_delta:
            return True
        if layer != p_layer:
            return True
        return False

    def _context_id(self, store) -> int | None:
        """Reuse the latest context snapshot; write a new one at most every
        snapshot_every_s. Context today is just the freshest desktop-activity
        line — the column set is the seed of the Now-Context (field v2)."""
        now = time.time()
        if (self._snapshot_id is not None
                and now - self._snapshot_ts < settings.attention.snapshot_every_s):
            return self._snapshot_id
        app_line = None
        try:
            from app.services.activity import describe_recent
            recent = describe_recent(limit=1)
            if recent:
                app_line = str(recent[0])[:200]
        except Exception:
            pass
        try:
            self._snapshot_id = store.add_context_snapshot(now, app=app_line)
            self._snapshot_ts = now
        except Exception:
            self._snapshot_id = None
        return self._snapshot_id

    # ------------------------------ recording ----------------------------
    def record_field(self, nodes: list[dict], store) -> int:
        """Log the nodes one field render surfaced. Called from the
        /graph/constellation route only (not internal constellation() reuse),
        throttled per node so polling refetches stay cheap."""
        if not self._enabled() or not nodes:
            return 0
        try:
            now = time.time()
            rows: list[dict] = []
            with self._lock:
                ctx_id = self._context_id(store)
                for n in nodes:
                    parsed = _parse_node(n.get("id") or "")
                    if not parsed:
                        continue
                    node_type, node_id = parsed
                    score = float(n.get("gravity") or 0.0)
                    layer = n.get("layer")
                    if not self._should_write("field", node_type, node_id,
                                              score, layer, now):
                        continue
                    self._last[("field", node_type, node_id)] = (now, score, layer)
                    decomp = n.get("_decomp")
                    rows.append({
                        "ts": now, "node_type": node_type, "node_id": node_id,
                        "surface": "field", "layer": layer, "score": score,
                        "decomposition": json.dumps(decomp) if decomp else None,
                        "context_id": ctx_id,
                    })
            return store.add_attention_impressions(rows)
        except Exception as exc:  # never break the field
            print(f"[attention_ledger] field record skipped ({exc}).")
            return 0

    def record_horizon(self, items: list[dict], store) -> int:
        """Log Horizon strip items (Track A4) — surface='horizon'."""
        if not self._enabled() or not items:
            return 0
        try:
            now = time.time()
            rows = []
            ctx_id = self._context_id(store)
            for it in items:
                parsed = _parse_node(it.get("id") or "")
                if not parsed and it.get("node_type") and it.get("node_id") is not None:
                    parsed = (it["node_type"], int(it["node_id"]))
                if not parsed:
                    continue
                rows.append({
                    "ts": now,
                    "node_type": parsed[0],
                    "node_id": parsed[1],
                    "surface": "horizon",
                    "layer": "horizon",
                    "score": float(it.get("p_need") or 0),
                    "decomposition": json.dumps({
                        "reasons": it.get("reason") or [],
                        "when_s": it.get("when_s"),
                        "source": it.get("source"),
                    }),
                    "context_id": ctx_id,
                })
            return store.add_attention_impressions(rows)
        except Exception as exc:
            print(f"[attention_ledger] horizon record skipped ({exc}).")
            return 0

    def record_grounding(self, person_ids: list[int], fact_ids: list[int],
                         store) -> int:
        """Log what chat grounding pulled in, and detect misses: a person the
        user asked about whom the field had NOT surfaced inside the miss
        window was needed-but-not-shown — the negative label ranking needs."""
        if not self._enabled():
            return 0
        try:
            now = time.time()
            rows: list[dict] = []
            with self._lock:
                items = ([("person", pid) for pid in person_ids]
                         + [("fact", fid) for fid in fact_ids])
                for node_type, node_id in items:
                    if not self._should_write("grounding", node_type, node_id,
                                              0.0, None, now):
                        continue
                    self._last[("grounding", node_type, node_id)] = (now, 0.0, None)
                    rows.append({
                        "ts": now, "node_type": node_type, "node_id": node_id,
                        "surface": "grounding",
                    })
                    if node_type != "person":
                        continue
                    last_field = store.last_attention_ts(node_type, node_id, "field")
                    if (last_field is None
                            or now - last_field > settings.attention.miss_window_s):
                        rows.append({
                            "ts": now, "node_type": node_type, "node_id": node_id,
                            "surface": "grounding", "outcome": "miss",
                            "detail": json.dumps({"absent_from": "field"}),
                        })
            n = store.add_attention_impressions(rows)
            # Retrieval into grounding is an access on the node's trace (A1).
            for node_type, node_id in items:
                store.record_node_access(node_type, node_id, now)
            return n
        except Exception as exc:  # never break grounding
            print(f"[attention_ledger] grounding record skipped ({exc}).")
            return 0

    def record_offer(self, *, fact_id: int | None = None,
                     text: str = "", kind: str = "task",
                     score: float | None = None,
                     risk: str | None = None, store=None) -> int:
        """Log a proactive offer that was SURFACED (or queued for surfacing).

        Offers are the third P0 surface. The fact id is the attendable node when
        known; otherwise a standalone offer row is still written so accept-rate
        and interruption cost stay measurable without a fact.
        """
        if not self._enabled():
            return 0
        try:
            if store is None:
                from app.storage import get_store
                store = get_store()
            now = time.time()
            detail = json.dumps({
                "kind": kind,
                "text": (text or "")[:120],
                "risk": risk,
            })
            row: dict[str, Any] = {
                "ts": now,
                "surface": "offer",
                "layer": "surfaced",
                "score": score,
                "detail": detail,
                "outcome": None,
            }
            if fact_id is not None:
                row["node_type"] = "fact"
                row["node_id"] = int(fact_id)
            else:
                # Synthetic node key so the row is indexable; negative ids are
                # reserved for offer-without-fact (never collide with real facts).
                import hashlib
                dig = hashlib.sha1(
                    f"{kind}|{(text or '')[:80]}".encode()).hexdigest()
                row["node_type"] = "fact"
                row["node_id"] = -(int(dig[:8], 16) % (10**9) or 1)
            with self._lock:
                n = store.add_attention_impressions([row])
            if fact_id is not None:
                store.record_node_access("fact", int(fact_id), now)
            return n
        except Exception as exc:  # never break the offer path
            print(f"[attention_ledger] offer record skipped ({exc}).")
            return 0

    def close_offer(self, *, fact_id: int | None = None, text: str = "",
                    accepted: bool, kind: str = "task", store=None) -> bool:
        """Close the newest open offer impression (accepted | dismissed)."""
        if not self._enabled():
            return False
        try:
            if store is None:
                from app.storage import get_store
                store = get_store()
            outcome = "accepted" if accepted else "dismissed"
            detail = json.dumps({"kind": kind, "text": (text or "")[:120]})
            if fact_id is not None:
                store.set_attention_outcome(
                    "fact", int(fact_id), outcome, detail=detail)
                store.record_node_access("fact", int(fact_id))
                store.bump_node_value(
                    "fact", int(fact_id),
                    "used" if accepted else "rejected")
                return True
            # No fact: close the newest open offer-surface row matching text.
            return bool(store.close_latest_offer_outcome(
                outcome, detail=detail, text_hint=(text or "")[:80]))
        except Exception as exc:
            print(f"[attention_ledger] offer close skipped ({exc}).")
            return False

    # ------------------------------ outcomes -----------------------------
    def outcome(self, node_id_str: str, outcome: str,
                *, detail: dict | None = None, store=None) -> bool:
        """Close the newest open impression for a node with a user reaction."""
        if not self._enabled():
            return False
        parsed = _parse_node(node_id_str)
        if not parsed:
            return False
        try:
            if store is None:
                from app.storage import get_store
                store = get_store()
            store.set_attention_outcome(
                parsed[0], parsed[1], outcome,
                detail=json.dumps(detail) if detail else None)
            # Engagement is an access on the trace AND moves long-run value.
            store.record_node_access(parsed[0], parsed[1])
            store.bump_node_value(parsed[0], parsed[1], outcome)
            # Pull the closed impression's decomposition for online β (A4).
            try:
                from app.services import ranking_learn
                decomp = None
                try:
                    row = store.latest_attention_decomp(
                        parsed[0], parsed[1])
                    decomp = row
                except Exception:
                    decomp = (detail or {}).get("decomposition") if isinstance(
                        detail, dict) else None
                ranking_learn.update_from_outcome(
                    store, decomp=decomp, outcome=outcome)
            except Exception:
                pass
            # Positive engagement also seeds the Now-Context (A2): touching a
            # node in the field means it's part of the present.
            if outcome in ("pin", "click", "dwell", "reclassify"):
                try:
                    from app.services.now_context import now_context
                    now_context.observe([parsed], weight=0.8,
                                        source="engagement")
                except Exception:
                    pass
                try:
                    from app.services import working_memory as _wm
                    _wm.touch_engagement(parsed)
                except Exception:
                    pass
            return True
        except Exception as exc:  # never break the edit op it observes
            print(f"[attention_ledger] outcome skipped ({exc}).")
            return False

    def close_grounding_for_row(self, distill_id: str, verdict: str,
                                *, store=None) -> int:
        """Chat-verdict join: a verdict on an answer labels the grounding
        impressions recorded when that answer was composed. The distill row's
        own timestamp anchors the window (compose runs seconds before the
        model call), so late verdicts land on the right impressions."""
        if not self._enabled():
            return 0
        outcome = {"accepted": "used", "rejected": "rejected",
                   "edited": "edited"}.get((verdict or "").strip().lower())
        if not outcome:
            return 0
        try:
            from app.services.escalate_log import escalate_log
            row = escalate_log.row_by_id(distill_id)
            if not row or not row.get("time"):
                return 0
            t = float(row["time"])
            if store is None:
                from app.storage import get_store
                store = get_store()
            closed = store.close_attention_window(
                "grounding", t - 240.0, t + 30.0, outcome,
                detail=json.dumps({"distill_id": distill_id}))
            for node_type, node_id in closed:
                store.bump_node_value(node_type, node_id, outcome)
            return len(closed)
        except Exception as exc:  # labeling must never break the verdict flow
            print(f"[attention_ledger] verdict join skipped ({exc}).")
            return 0

    def stats(self, *, days: float = 7.0, store=None) -> dict[str, Any]:
        if store is None:
            from app.storage import get_store
            store = get_store()
        out = store.attention_stats(days=days)
        out["enabled"] = self._enabled()
        return out


attention_ledger = AttentionLedger()
