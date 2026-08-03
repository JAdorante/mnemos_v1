"""Now-Context (Track A2) — the system's estimate of what the user is inside
of right now, as a sparse seed distribution over graph nodes.

Gravity is a function of (node, clock); attention is a function of context.
This module is the context: a small decaying set of (node → weight) seeds fed
by the moments the user demonstrably engages with something —

  chat        the people/facts grounding pulled in for a real user question
              (machine callers pass record_attention=False and never seed)
  engagement  field clicks, pins, evidence dwell (via the attention ledger)
  explicit    POST /field/context/observe (the UI's focus handle)

Seeds decay on the context-drift timescale (~20 min half-feel) and the set is
clamped to the strongest 64 — working memory's antechamber, not a log. The
spreading-activation engine (services/activation.py) reads `seeds()` and
lights the graph neighborhood; nothing here touches ranking directly.

In-process state, deliberately: context is a property of the present. It is
never persisted verbatim (privacy: hourly context_snapshots carry only the
app line), and a restart simply means the present starts over.
"""
from __future__ import annotations

import math
import threading
import time

# Seed half-life: how fast "what I'm inside of" drifts when nothing renews it.
TAU_S = 20 * 60.0
# The antechamber is small on purpose.
MAX_SEEDS = 64
# Below this a seed is forgotten entirely.
FLOOR = 0.05

NodeKey = tuple[str, int]


class NowContext:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seeds: dict[NodeKey, tuple[float, float]] = {}  # key -> (w, ts)
        self._generation = 0

    def observe(self, keys: list[NodeKey], *, weight: float = 1.0,
                source: str = "chat", now: float | None = None) -> None:
        """Renew/plant seeds. A re-observed node takes the max of its decayed
        weight and the new one — attention re-arrives, it doesn't stack."""
        if not keys:
            return
        now = now or time.time()
        weight = max(0.0, min(1.0, float(weight)))
        with self._lock:
            for key in keys:
                if (not isinstance(key, tuple) or len(key) != 2
                        or key[0] not in ("person", "entity", "fact")):
                    continue
                old = self._decayed(key, now)
                self._seeds[key] = (max(old, weight), now)
            self._trim(now)
            self._generation += 1

    def seeds(self, now: float | None = None) -> dict[NodeKey, float]:
        """Current seed weights, decayed to `now`; floor-pruned."""
        now = now or time.time()
        out: dict[NodeKey, float] = {}
        with self._lock:
            dead = []
            for key in self._seeds:
                w = self._decayed(key, now)
                if w < FLOOR:
                    dead.append(key)
                else:
                    out[key] = w
            for key in dead:
                del self._seeds[key]
        return out

    @property
    def generation(self) -> int:
        """Bumps on every observe — the SSE wave/change token."""
        return self._generation

    def clear(self) -> None:
        with self._lock:
            self._seeds.clear()
            self._generation += 1

    # ------------------------------ internals ----------------------------
    def _decayed(self, key: NodeKey, now: float) -> float:
        cur = self._seeds.get(key)
        if not cur:
            return 0.0
        w, ts = cur
        return w * math.exp(-(max(0.0, now - ts)) / TAU_S)

    def _trim(self, now: float) -> None:
        if len(self._seeds) <= MAX_SEEDS:
            return
        ranked = sorted(self._seeds,
                        key=lambda k: -self._decayed(k, now))
        for key in ranked[MAX_SEEDS:]:
            del self._seeds[key]


now_context = NowContext()
