"""Exemplar store — retrieval-first learning (Workstream C).

The PRIMARY learning mechanism at current data scale: every confirmed positive
LearningPair (human-edited > human-accepted > shadow-confirmed) is embedded
into a LanceDB `exemplars` table and injected as a few-shot demonstration the
next time a similar input hits the local model. An edit made this morning
improves this afternoon's extraction — no GPU, no WSL2, no promotion gate;
rollback = delete a row.

Reuses the ONE embedding stack (app/services/embeddings.py — the same model
that embeds memory-search queries) and the existing LanceDB directory. The
legacy few_shot distill retrieval stays as fallback when this store is off or
returns nothing, so behavior is unchanged until QUILL_EXEMPLARS=1.

Anti-pollution rules (C.4):
  * only confirmed positive pairs are ever ingested; a deleted pair cascades
    here (learning_store.delete), so rejected/deleted rows can't be retrieved
  * a single exemplar can't dominate a type: among near-tie candidates the
    LEAST-used wins, so ties rotate
  * exemplars ride ONLY the local prompt (model_router keeps the parent/cloud
    prompt clean), so personal-classed exemplar text never rides an escalation
  * per-type gates (data/exemplar_type_gates.json): a type whose A/B delta is
    negative gets auto-disabled by scripts/eval_exemplars.py; "_all" is the
    Console kill switch (mirrors QUILL_EXEMPLARS=0 without editing .env)

Every failure path degrades to "no exemplars" — the local call must never
break because retrieval did.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from app.config import settings

TABLE = "exemplars"

# Descending trust: the tier decides ranking ties and curation weight (E.1).
QUALITY_TIERS = ("human_edited", "human_accepted",
                 "shadow_confirmed", "shadow_autotrust")
_TIER_BONUS = {"human_edited": 0.06, "human_accepted": 0.03,
               "shadow_confirmed": 0.015, "shadow_autotrust": 0.0}

# Near-tie window for the rotation rule: candidates within this cosine band
# are considered equivalent and the least-used one wins.
_TIE_BAND = 0.03

# Per-side cap inside the rendered block (storage stays full-fidelity).
_MAX_EXAMPLE_CHARS = 1500

# Router task -> exemplar task_types it may draw from (C.3 "supported types").
ROUTER_TASK_TYPES: dict[str, tuple[str, ...]] = {
    "chat": ("escalation.text",),
    "extract": ("extraction.task", "extraction.commitment", "extraction.claim"),
}


def _cfg():
    return settings.exemplars


def enabled() -> bool:
    """Env read at call time so tests/console toggles apply without restart."""
    v = os.environ.get("QUILL_EXEMPLARS")
    if v is not None:
        return v not in ("0", "false", "False")
    return bool(_cfg().enabled)


def tier_for(pair: dict) -> str:
    """Quality tier from a LearningPair row (C.1). Edits are the strongest
    signal and must win retrieval ties."""
    if str(pair.get("verdict")) == "edited":
        return "human_edited"
    if str(pair.get("verdict_source") or "").startswith("shadow"):
        return ("shadow_confirmed" if pair.get("human_confirmed")
                else "shadow_autotrust")
    return "human_accepted"


def _embed(texts: list[str]):
    from app.services.embeddings import embedder
    return embedder.encode_many(texts)


class ExemplarStore:
    def __init__(self, path: str | None = None) -> None:
        self.path = Path(path or settings.memory.lance_dir)
        self._lock = threading.Lock()
        self._db = None
        self._table = None

    # ------------------------------ plumbing ------------------------------
    def _ensure(self, dim: int | None = None):
        """Open (or create, when dim is known) the exemplars table."""
        if self._table is not None:
            return self._table
        import lancedb
        import pyarrow as pa

        self.path.mkdir(parents=True, exist_ok=True)
        if self._db is None:
            self._db = lancedb.connect(str(self.path))
        if TABLE in self._db.table_names():
            self._table = self._db.open_table(TABLE)
            return self._table
        if dim is None:
            return None                      # nothing to open, nothing to make
        schema = pa.schema([
            pa.field("exemplar_id", pa.string()),
            pa.field("learning_pair_id", pa.string()),
            pa.field("task_type", pa.string()),
            pa.field("input_text", pa.string()),
            pa.field("target_text", pa.string()),
            pa.field("quality_tier", pa.string()),
            pa.field("created_at", pa.float64()),
            pa.field("use_count", pa.int64()),
            pa.field("last_used_at", pa.float64()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
        ])
        self._table = self._db.create_table(TABLE, schema=schema)
        return self._table

    # ------------------------------ gates ---------------------------------
    def gates(self) -> dict:
        try:
            p = Path(_cfg().gates_path)
            if p.is_file():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def set_gate(self, task_type: str, off: bool, *, reason: str = "") -> dict:
        """Gate a type off (or back on). task_type '_all' = kill switch."""
        g = self.gates()
        if off:
            g[task_type] = {"off": True, "reason": reason or "manual",
                            "ts": time.time()}
        else:
            g.pop(task_type, None)
        p = Path(_cfg().gates_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(g, indent=2), encoding="utf-8")
        return g

    def _gated(self, task_type: str) -> bool:
        g = self.gates()
        return bool((g.get("_all") or {}).get("off")
                    or (g.get(task_type) or {}).get("off"))

    # ------------------------------ ingest --------------------------------
    def ingest_pair(self, pair: dict, store=None) -> str | None:
        """Embed + upsert one confirmed positive pair (C.2). Idempotent per
        pair id (re-ingest replaces). Never raises."""
        try:
            if not enabled():
                return None
            if pair.get("verdict") not in ("accepted", "edited",
                                           "shadow_disagree"):
                return None
            if not pair.get("human_confirmed"):
                # Unconfirmed shadow pairs only enter under explicit autotrust
                # (documented as lowering label quality).
                if os.environ.get("QUILL_SHADOW_AUTOTRUST", "0") in \
                        ("0", "false", "False"):
                    return None
            text = str(pair.get("input_text") or "")
            target = str(pair.get("final_target") or "")
            if not text or not target:
                return None
            vec = _embed([text])[0]
            eid = uuid.uuid4().hex
            with self._lock:
                table = self._ensure(len(vec))
                table.delete(
                    f"learning_pair_id = '{str(pair.get('id'))}'")
                table.add([{
                    "exemplar_id": eid,
                    "learning_pair_id": str(pair.get("id")),
                    "task_type": str(pair.get("task_type")),
                    "input_text": text,
                    "target_text": target,
                    "quality_tier": tier_for(pair),
                    "created_at": float(pair.get("created_at") or time.time()),
                    "use_count": 0,
                    "last_used_at": 0.0,
                    "vector": [float(x) for x in vec],
                }])
            try:
                if store is not None:
                    store.set_learning_embedding(str(pair.get("id")), eid)
                else:
                    from app.storage import get_store
                    get_store().set_learning_embedding(str(pair.get("id")), eid)
            except Exception:
                pass
            return eid
        except Exception as exc:
            print(f"[exemplar_store] ingest skipped ({exc}).")
            return None

    def delete_for_pair(self, pair_id: str) -> None:
        """Cascade from learning_store.delete — a deleted pair's exemplar must
        never be retrievable again (C.4)."""
        try:
            with self._lock:
                table = self._ensure()
                if table is None:
                    return
                table.delete(f"learning_pair_id = '{str(pair_id)}'")
        except Exception as exc:
            print(f"[exemplar_store] cascade delete skipped ({exc}).")

    def delete(self, exemplar_id: str) -> None:
        try:
            with self._lock:
                table = self._ensure()
                if table is None:
                    return
                table.delete(f"exemplar_id = '{str(exemplar_id)}'")
        except Exception as exc:
            print(f"[exemplar_store] delete skipped ({exc}).")

    # ------------------------------ retrieval ------------------------------
    def examples(self, task_types: tuple[str, ...] | list[str],
                 query: str, *, k: int | None = None,
                 min_sim: float | None = None,
                 token_budget: int | None = None,
                 exclude_pair_ids: frozenset[str] | set[str] = frozenset(),
                 ) -> list[dict[str, Any]]:
        """Top-k same-type exemplars above the similarity floor, quality-tier
        weighted, near-ties rotated by use_count, capped by the added-token
        budget. Returns few_shot-shaped dicts ({id, prompt, answer, sim,
        outcome}) so few_shot.render() and the router's evidence path work
        unchanged. [] on any miss/failure — never raises."""
        try:
            if not enabled() or not query:
                return []
            cfg = _cfg()
            k = k if k is not None else cfg.k
            min_sim = min_sim if min_sim is not None else cfg.min_sim
            budget_chars = 4 * (token_budget if token_budget is not None
                                else cfg.token_budget)
            types = tuple(t for t in task_types if not self._gated(t))
            if not types or k <= 0:
                return []
            vec = _embed([query])[0]
            with self._lock:
                table = self._ensure()
                if table is None:
                    return []
                where = ("task_type IN (" +
                         ",".join(f"'{t}'" for t in types) + ")")
                rows = (table.search([float(x) for x in vec])
                        .metric("cosine").where(where)
                        .limit(max(k * 4, 12)).to_list())
            cands = []
            for r in rows:
                sim = 1.0 - float(r.get("_distance", 1.0))
                if sim < min_sim:
                    continue
                if str(r.get("learning_pair_id") or "") in exclude_pair_ids:
                    continue          # eval contamination guard (C.5)
                cands.append({
                    "id": str(r["exemplar_id"]),
                    "pair_id": str(r.get("learning_pair_id") or ""),
                    "prompt": str(r.get("input_text") or ""),
                    "answer": str(r.get("target_text") or ""),
                    "tier": str(r.get("quality_tier") or "human_accepted"),
                    "use_count": int(r.get("use_count") or 0),
                    "sim": round(sim, 4),
                    "outcome": ("edited" if r.get("quality_tier") ==
                                "human_edited" else "accepted"),
                })
            # Quality-tier-weighted rank; inside a near-tie band the least-used
            # exemplar wins so one example can't dominate a type (C.4).
            cands.sort(key=lambda c: -(c["sim"] + _TIER_BONUS.get(c["tier"], 0)))
            picked: list[dict] = []
            spent = 0
            i = 0
            while i < len(cands) and len(picked) < k:
                j = i
                band = [cands[i]]
                while (j + 1 < len(cands)
                       and cands[i]["sim"] - cands[j + 1]["sim"] <= _TIE_BAND):
                    j += 1
                    band.append(cands[j])
                band.sort(key=lambda c: (c["use_count"], -c["sim"]))
                for c in band:
                    cost = min(len(c["prompt"]), _MAX_EXAMPLE_CHARS) + \
                        min(len(c["answer"]), _MAX_EXAMPLE_CHARS)
                    if spent + cost > budget_chars:
                        continue
                    picked.append(c)
                    spent += cost
                    if len(picked) >= k:
                        break
                i = j + 1
            return picked
        except Exception as exc:
            print(f"[exemplar_store] recall skipped ({exc}).")
            return []

    def mark_used(self, examples: list[dict], *, task: str = "") -> None:
        """Bump use counters + append the per-call use log (C.3/C.5 feed)."""
        if not examples:
            return
        now = time.time()
        try:
            with self._lock:
                table = self._ensure()
                if table is not None:
                    for ex in examples:
                        try:
                            table.update(
                                where=f"exemplar_id = '{ex['id']}'",
                                values={"use_count": int(ex.get("use_count", 0)) + 1,
                                        "last_used_at": now})
                        except Exception:
                            break            # older lancedb: counters best-effort
        except Exception:
            pass
        try:
            p = Path(_cfg().uses_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps({
                    "ts": now, "task": task,
                    "exemplar_ids": [ex["id"] for ex in examples],
                    "pair_ids": [ex.get("pair_id") for ex in examples],
                    "sims": [ex.get("sim") for ex in examples],
                }) + "\n")
        except Exception:
            pass

    # ------------------------------ console --------------------------------
    def list_rows(self, *, task_type: str | None = None,
                  limit: int = 200) -> list[dict]:
        try:
            with self._lock:
                table = self._ensure()
                if table is None:
                    return []
                try:
                    lance_tbl = table.to_lance()
                    cols = ["exemplar_id", "learning_pair_id", "task_type",
                            "input_text", "target_text", "quality_tier",
                            "created_at", "use_count", "last_used_at"]
                    rows = lance_tbl.to_table(columns=cols).to_pylist()
                except Exception:
                    rows = [{k: v for k, v in r.items() if k != "vector"}
                            for r in table.to_pandas().to_dict("records")]
            if task_type:
                rows = [r for r in rows if r.get("task_type") == task_type]
            rows.sort(key=lambda r: -float(r.get("created_at") or 0))
            return rows[:max(1, int(limit))]
        except Exception as exc:
            print(f"[exemplar_store] list skipped ({exc}).")
            return []

    def stats(self) -> dict:
        rows = self.list_rows(limit=100000)
        by: dict[str, int] = {}
        for r in rows:
            by[str(r.get("task_type"))] = by.get(str(r.get("task_type")), 0) + 1
        return {"enabled": enabled(), "count": len(rows), "by_type": by,
                "gates": self.gates()}


exemplar_store = ExemplarStore()


# Module-level API (what learning_store and the router import).
def ingest_pair(pair: dict, store=None) -> str | None:
    return exemplar_store.ingest_pair(pair, store=store)


def delete_for_pair(pair_id: str) -> None:
    exemplar_store.delete_for_pair(pair_id)


def router_examples(task: str, messages: list | None,
                    cfg=None) -> list[dict[str, Any]]:
    """Exemplars for a ModelRouter task (C.3): same-type retrieval keyed on the
    question part of the last user message. Empty when off/gated/unsupported —
    the router then falls back to legacy few_shot unchanged."""
    try:
        types = ROUTER_TASK_TYPES.get(task)
        if not types or not enabled():
            return []
        from app.services.few_shot import query_focus, query_text
        q = query_focus(query_text(messages))
        if not q:
            return []
        k = getattr(cfg, "fewshot_k", None) if cfg is not None else None
        ex = exemplar_store.examples(types, q, k=k)
        if ex:
            exemplar_store.mark_used(ex, task=task)
        return ex
    except Exception as exc:
        print(f"[exemplar_store] router recall skipped ({exc}).")
        return []
