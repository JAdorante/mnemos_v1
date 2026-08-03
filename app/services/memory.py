"""M3 — the Memory Engine.

Persistent (SQLite) + semantic (LanceDB). Every Event is written to SQLite and,
when semantic search is enabled, embedded and indexed in the vector store so the
timeline is searchable by meaning. The timeline reloads on startup and any
un-indexed events are backfilled into the vector store.

    search(query)  ->  semantic (embeddings + LanceDB)  ->  fallback substring

Retrieval is lifecycle-aware: superseded and dismissed facts are filtered out
at hydration (their vectors stay in the index — the store row is authoritative),
and ranking blends cosine with recency so a stale fact and its fresh correction
stop competing as equals.
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.config import settings
from app.events import Event, bus
from app.storage import Store, get_store

# Facts share the one LanceDB index with episodic events so `search()` returns
# both. Their ids are offset into a disjoint range so an event id and a fact id
# can never collide; a hit with id >= this offset is a fact (fact_id = id - off).
FACT_ID_OFFSET = 1_000_000_000


def fact_is_retrievable(f: dict | None) -> bool:
    """A stored fact may surface in search only while it is the living version:
    not superseded by a newer fact, not dismissed by the human."""
    if f is None:
        return False
    if (f.get("state") or "active") != "active":
        return False
    return f.get("review") != "dismissed"


def recency_adjusted(score: float, age_days: float, *,
                     weight: float | None = None,
                     half_life_days: float | None = None) -> float:
    """Blend cosine similarity with recency: score + weight * 0.5^(age/half).
    The bonus is small (default 0.08 max) — it breaks ties and lifts the fresh
    correction over its stale twin without letting recency beat relevance."""
    cfg = settings.facts
    w = cfg.recency_weight if weight is None else weight
    hl = cfg.recency_half_life_days if half_life_days is None else half_life_days
    if w <= 0 or hl <= 0:
        return score
    return score + w * (0.5 ** (max(age_days, 0.0) / hl))


class MemoryEngine:
    def __init__(self, store: Store | None = None) -> None:
        self._lock = threading.Lock()
        self._store: Store | None = store
        self._events: list[Event] = []
        self._semantic = settings.memory.semantic
        self._vectors = None

    def _ensure_store(self) -> Store:
        if self._store is None:
            self._store = get_store()
        return self._store

    def _ensure_vectors(self):
        if self._vectors is None and self._semantic:
            try:
                from app.vectorstore import get_vectorstore

                self._vectors = get_vectorstore()
            except Exception as exc:
                print(f"[memory] semantic index unavailable ({exc}); using substring search.")
                self._semantic = False
        return self._vectors

    def _embed(self, text: str):
        from app.services.embeddings import embedder

        return embedder.encode(text)

    def attach(self) -> None:
        """Subscribe to the bus, load the timeline, and backfill the index."""
        store = self._ensure_store()
        with self._lock:
            self._events = store.all()
        if self._events:
            print(f"[memory] loaded {len(self._events)} event(s) from {store.db_path}")
        self._backfill()
        # Warm the embedder on THIS (startup) thread, before capture threads
        # spin up. Its first-ever import must complete before SpeechBrain loads
        # on the audio thread, or the two race and the encode fails (see
        # Embedder.warmup). Backfill already warms it when it runs; this covers
        # the (now common) case where backfill is skipped as already-indexed.
        if self._semantic:
            try:
                from app.services.embeddings import embedder

                embedder.warmup()
            except Exception as exc:
                print(f"[memory] embedder warmup skipped ({exc})")
        bus.subscribe(self._on_event)

    def _backfill(self) -> None:
        """Reconcile Lance ids with SQLite events (incremental, not sticky).

        Old behavior returned immediately whenever vectors.count() > 0, so a
        crash mid-index left permanent gaps. Now we diff id sets and only
        embed missing event rows; orphaned event vectors (id < FACT_ID_OFFSET)
        are deleted. Fact vectors (id >= FACT_ID_OFFSET) are left alone.
        """
        vectors = self._ensure_vectors()
        if not vectors:
            return
        try:
            rows = self._ensure_store().all_with_ids()
            event_ids = {int(eid) for eid, _ in rows}
            existing = vectors.list_ids()
            event_existing = {i for i in existing if i < FACT_ID_OFFSET}
            missing_ids = event_ids - event_existing
            orphans = event_existing - event_ids
            if orphans:
                n = vectors.delete_ids(sorted(orphans))
                print(f"[memory] dropped {n} orphaned event vector(s).")
            if not missing_ids:
                if event_ids:
                    print(f"[memory] semantic index ok ({len(event_ids)} event(s)).")
                return
            by_id = {int(eid): ev for eid, ev in rows}
            todo = [(eid, by_id[eid]) for eid in sorted(missing_ids) if eid in by_id]
            print(f"[memory] indexing {len(todo)} missing event(s) for semantic search ...")
            from app.services.embeddings import embedder

            # Batch to keep peak RAM bounded on large catch-up runs.
            batch = 256
            total = 0
            for i in range(0, len(todo), batch):
                chunk = todo[i:i + batch]
                texts = [(ev.summary or ev.raw or "") for _, ev in chunk]
                vecs = embedder.encode_many(texts)
                payload = [
                    {"id": int(eid), "time": float(ev.time),
                     "modality": ev.modality.value, "text": text,
                     "vector": vec.tolist()}
                    for (eid, ev), text, vec in zip(chunk, texts, vecs)
                ]
                vectors.add_many(payload)
                total += len(payload)
            print(f"[memory] semantic index ready (+{total} indexed).")
        except Exception as exc:
            print(f"[memory] backfill error ({exc}); using substring search.")
            self._semantic = False

    def _on_event(self, event: Event) -> None:
        store = self._ensure_store()
        eid = store.insert(event)
        with self._lock:
            self._events.append(event)
        vectors = self._ensure_vectors()
        if vectors:
            try:
                text = event.summary or event.raw
                vectors.add(eid, event.time, event.modality.value, text, self._embed(text))
            except Exception as exc:
                print(f"[memory] index error: {exc}")

    def add(self, event: Event) -> None:
        self._on_event(event)

    def index_fact(self, fact_id: int, kind: str, text: str, ts: float) -> None:
        """Index an extracted fact into the shared vector store so semantic
        search surfaces facts alongside episodes. Best-effort."""
        vectors = self._ensure_vectors()
        if not vectors or not (text or "").strip():
            return
        try:
            vectors.add(FACT_ID_OFFSET + int(fact_id), ts, f"fact:{kind}",
                        text, self._embed(text))
        except Exception as exc:
            print(f"[memory] fact index error: {exc}")

    def similar_facts(self, kind: str, text: str,
                      k: int = 4) -> list[tuple[int, float, str]]:
        """Nearest ACTIVE facts of `kind` by cosine — the write-time dedup /
        supersede probe (see services/fact_gate.py). [(fact_id, score, text)],
        best first; empty when the vector index is unavailable (the gate then
        degrades to plain insert)."""
        vectors = self._ensure_vectors()
        if not vectors or not (text or "").strip():
            return []
        try:
            hits = vectors.search(self._embed(text), k=k,
                                  modality=f"fact:{kind}")
            store = self._ensure_store()
            ids = [int(h["id"]) - FACT_ID_OFFSET for h in hits
                   if int(h["id"]) >= FACT_ID_OFFSET]
            fmap = store.facts_by_ids(ids) if ids else {}
            out = []
            for h in hits:
                hid = int(h["id"])
                if hid < FACT_ID_OFFSET:
                    continue
                f = fmap.get(hid - FACT_ID_OFFSET)
                if not fact_is_retrievable(f):
                    continue
                out.append((int(f["fact_id"]), float(h.get("score") or 0.0),
                            f.get("text") or ""))
            return out
        except Exception:
            return []

    def all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._events]

    def search(self, query: str, limit: int = 20, modality: str | None = None) -> list[dict[str, Any]]:
        if not query.strip():
            with self._lock:
                evs = self._events[-limit:]
            return [e.to_dict() for e in evs]
        vectors = self._ensure_vectors()
        if vectors:
            try:
                # Overfetch: lifecycle filtering drops superseded/dismissed
                # facts after the ANN query, and recency re-ranking needs a
                # pool wider than the final cut to actually change anything.
                k = min(max(limit * 3, 12), 60)
                hits = vectors.search(self._embed(query), k=k, modality=modality)
                store = self._ensure_store()
                # Split hits into episodic events and extracted facts (offset ids).
                ev_ids = [int(h["id"]) for h in hits if int(h["id"]) < FACT_ID_OFFSET]
                fact_ids = [int(h["id"]) - FACT_ID_OFFSET for h in hits
                            if int(h["id"]) >= FACT_ID_OFFSET]
                emap = store.by_ids_map(ev_ids)
                fmap = store.facts_by_ids(fact_ids) if fact_ids else {}
                now = time.time()
                ranked: list[tuple[float, dict]] = []
                for h in hits:
                    hid = int(h["id"])
                    if hid >= FACT_ID_OFFSET:
                        f = fmap.get(hid - FACT_ID_OFFSET)
                        if not fact_is_retrievable(f):
                            continue
                        d = {"modality": f"fact:{f['kind']}", "raw": f.get("text", ""),
                             "summary": f.get("text", ""), "kind": f["kind"],
                             "fact_id": f["fact_id"], "status": f.get("status"),
                             "source_span": f.get("source_span"), "is_fact": True}
                        ts = float(f.get("updated_at") or h.get("time") or 0)
                    else:
                        ev = emap.get(hid)
                        if ev is None:
                            continue
                        d = ev.to_dict()
                        ts = float(h.get("time") or 0)
                    d["score"] = h.get("score")
                    age_days = (now - ts) / 86400.0 if ts else 3650.0
                    ranked.append(
                        (recency_adjusted(float(h.get("score") or 0.0), age_days), d))
                ranked.sort(key=lambda t: -t[0])
                return [d for _, d in ranked[:limit]]
            except Exception as exc:
                print(f"[memory] semantic search error ({exc}); falling back to substring.")
        # Substring fallback: distilled facts first (they used to vanish
        # entirely on this path), then raw events, capped at `limit`.
        store = self._ensure_store()
        out: list[dict[str, Any]] = []
        if modality is None or modality.startswith("fact"):
            try:
                for f in store.search_facts_like(query, limit=max(4, limit // 2)):
                    out.append({"modality": f"fact:{f['kind']}",
                                "raw": f.get("text", ""),
                                "summary": f.get("text", ""), "kind": f["kind"],
                                "fact_id": f["fact_id"], "status": f.get("status"),
                                "source_span": f.get("source_span"),
                                "is_fact": True})
            except Exception:
                pass
        out.extend(e.to_dict() for e in store.search(query, limit))
        return out[:limit]


memory = MemoryEngine()
