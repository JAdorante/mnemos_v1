"""CAL Stage 1 — binding lookup, cached.

This is the hot path: the question "what does this identifier mean?" is asked
for every key on every event, and the whole economic argument depends on it
answering in about a millisecond without a model. `Store.lookup_binding` is one
indexed SELECT, which is fast but not free at capture rates, so this puts an
LRU in front of it.

The cache is correctness-sensitive in one direction only. A stale MISS costs an
unnecessary escalation, which is merely expensive. A stale HIT attributes an
event to a node the graph no longer says it belongs to, which is wrong and
invisible. So minting and invalidation both punch the cache immediately, while
entries expire on a TTL regardless — a cache with no expiry is a second source
of truth that nobody remembers to reconcile.
"""
from __future__ import annotations

import time
from collections import OrderedDict

DEFAULT_MAXSIZE = 4096
DEFAULT_TTL_S = 300.0


class BindingCache:
    """LRU + TTL over `Store.lookup_binding`. Not thread-safe by design —
    capture is single-writer, and a lock here would sit on the hot path."""

    def __init__(self, store=None, *, maxsize: int = DEFAULT_MAXSIZE,
                 ttl_s: float = DEFAULT_TTL_S):
        self._store = store
        self._maxsize = int(maxsize)
        self._ttl = float(ttl_s)
        self._cache: OrderedDict = OrderedDict()
        self.hits = self.misses = self.evictions = 0

    def _get_store(self):
        if self._store is not None:
            return self._store
        from app.storage import get_store
        return get_store()

    def lookup(self, key_type: str, key_value: str, *,
               now: float | None = None) -> list[dict]:
        """Bindings for one key, strongest first. `[]` means nothing is bound."""
        t = float(now if now is not None else time.time())
        ck = (key_type, key_value)
        hit = self._cache.get(ck)
        if hit is not None and (t - hit[0]) < self._ttl:
            self._cache.move_to_end(ck)
            self.hits += 1
            return hit[1]
        self.misses += 1
        try:
            rows = self._get_store().lookup_binding(key_type, key_value)
        except Exception as exc:
            print(f"[cal.bindings] lookup failed {key_type}:{key_value} ({exc}).")
            return []
        self._cache[ck] = (t, rows)
        self._cache.move_to_end(ck)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)
            self.evictions += 1
        return rows

    def anchors(self, signal_keys, *, bindable_only: bool = True,
                now: float | None = None) -> list:
        """Signal keys → resolved anchors, skipping keys nothing is bound to.

        `bindable_only` enforces the convention-class gate: a path seen once is
        recorded as an observation but is not yet allowed to resolve an event.
        """
        from app.services.context.frames import Anchor
        out = []
        for sk in signal_keys or []:
            if sk is None:
                continue
            for row in self.lookup(sk.key_type, sk.key_value, now=now):
                if bindable_only and not row.get("bindable"):
                    continue
                out.append(Anchor(
                    row["node_type"], row["node_id"], sk.key_value,
                    min(float(row.get("strength") or 0.0), sk.strength),
                    sk.tier, sk.nameable))
        return out

    def invalidate(self, key_type: str | None = None,
                   key_value: str | None = None) -> int:
        """Drop cached answers. No arguments clears everything."""
        if key_type is None:
            n = len(self._cache)
            self._cache.clear()
            return n
        if key_value is None:
            doomed = [k for k in self._cache if k[0] == key_type]
        else:
            doomed = [(key_type, key_value)]
        for k in doomed:
            self._cache.pop(k, None)
        return len(doomed)

    def mint(self, node_type: str, node_id, key_type: str, key_value: str,
             **kw) -> bool:
        """Bind and invalidate in one call, so the ratchet cannot leave a stale
        miss behind that re-escalates the thing it just learned."""
        fresh = self._get_store().bind_node_key(
            node_type, node_id, key_type, key_value, **kw)
        self.invalidate(key_type, key_value)
        return fresh

    @property
    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"size": len(self._cache), "hits": self.hits,
                "misses": self.misses, "evictions": self.evictions,
                "hit_rate": (self.hits / total) if total else 0.0}


_default: BindingCache | None = None


def cache(store=None) -> BindingCache:
    """Process-wide cache. Pass a store in tests; the default binds lazily."""
    global _default
    if store is not None:
        return BindingCache(store)
    if _default is None:
        _default = BindingCache()
    return _default


def reset() -> None:
    global _default
    _default = None


__all__ = ["BindingCache", "cache", "reset", "DEFAULT_MAXSIZE", "DEFAULT_TTL_S"]
