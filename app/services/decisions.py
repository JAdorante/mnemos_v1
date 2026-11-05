"""One record for every human verdict.

The trust layer says only a live human reply authorizes an action. Those
replies used to be stored wherever the asking surface kept its state (action
packets, promotion packets, a peer-asks JSON file, the assistant inbox DB,
review columns on facts and reflection items), and only some of them ever
reached the learning loop. Every approval surface now calls `record()` once
per verdict, which writes one `decisions` row: what was shown (by hash), the
options offered, the verdict and any edit, who decided and when, and a
pointer to the subject.

Learning pairs for human verdicts are derived here, not written by hand at
each surface: pass `learn=` and the pair is recorded with the decision's id
in its source_refs, and the decision row points back at the pair. Undoing a
decision (`undo()`) records the undo and deletes the pair it produced.

The surface keeps its own state (a packet's `decision` column, an ask's
status); this table is the shared trail, the audit view and the learning
feed. Recording never raises: a verdict the human already gave must not fail
because its bookkeeping did.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

# What a human can answer, normalized across surfaces.
VERDICTS = frozenset({
    "approved",    # yes / accept / confirm / send
    "edited",      # yes, with changes
    "rejected",    # no / deny / cancel / reject
    "dismissed",   # not useful, without saying it was wrong
    "done",        # marked complete by hand
    "converted",   # turned into something else (e.g. reflection → task)
    "undone",      # a previous decision reversed
})

# Decision verdict -> learning verdict. Verdicts not listed teach nothing.
_LEARN_VERDICT = {
    "approved": "accepted",
    "edited": "edited",
    "rejected": "rejected",
    "dismissed": "dismissed",
}

# Surface vocabularies mapped onto VERDICTS.
_ALIASES = {
    "accepted": "approved", "accept": "approved", "approve": "approved",
    "confirm": "approved", "confirmed": "approved", "yes": "approved",
    "edit": "edited",
    "reject": "rejected", "deny": "rejected", "denied": "rejected",
    "cancel": "rejected", "cancelled": "rejected", "declined": "rejected",
    "no": "rejected",
    "dismiss": "dismissed",
    "convert": "converted",
    "undo": "undone", "removed": "undone",
}


def normalize(verdict: str) -> str | None:
    v = str(verdict or "").strip().lower()
    v = _ALIASES.get(v, v)
    return v if v in VERDICTS else None


def _store(store=None):
    if store is not None:
        return store
    from app.storage import get_store
    return get_store()


def record(*, surface: str, subject: str, verdict: str,
           shown_hash: str | None = None, shown: Any = None,
           options: list | tuple | None = None, edit: Any = None,
           actor: str = "user", via: str | None = None,
           meta: dict | None = None, learn: dict | None = None,
           store=None) -> dict | None:
    """Record one human verdict. Returns {id, verdict, learning_pair_id}, or
    None when the verdict is not one of VERDICTS or the write failed.

    `learn` holds learning_store.record keyword arguments (task_type,
    input_text, final_target, local_output, ...). The pair's verdict comes
    from this decision and its verdict_source is the surface; leave `learn`
    out on surfaces whose verdicts are not a training signal yet.
    """
    v = normalize(verdict)
    if v is None:
        print(f"[decisions] unknown verdict {verdict!r} from {surface}; "
              "not recorded.")
        return None
    try:
        st = _store(store)
        row = {
            "id": uuid.uuid4().hex,
            "decided_at": time.time(),
            "surface": str(surface),
            "subject": str(subject),
            "verdict": v,
            "shown_hash": shown_hash or None,
            "shown": shown,
            "options": list(options) if options else None,
            "edit": edit,
            "actor": actor or "user",
            "via": via,
            "meta": meta or None,
        }
        st.add_decision(row)
    except Exception as exc:
        print(f"[decisions] {surface} verdict not recorded ({exc}).")
        return None
    pair_id = None
    if learn and v in _LEARN_VERDICT:
        pair_id = _derive_pair(row, learn, st)
    return {"id": row["id"], "verdict": v, "learning_pair_id": pair_id}


def _derive_pair(row: dict, learn: dict, st) -> str | None:
    try:
        from app.services import learning_store
        kw = dict(learn)
        refs = dict(kw.pop("source_refs", None) or {})
        refs["decision_id"] = row["id"]
        kw.setdefault("verdict_source", row["surface"])
        pair_id = learning_store.record(
            verdict=_LEARN_VERDICT[row["verdict"]], source_refs=refs,
            store=st, **kw)
        if pair_id:
            st.set_decision_pair(row["id"], pair_id)
        return pair_id
    except Exception as exc:
        print(f"[decisions] learning pair for {row['surface']} skipped "
              f"({exc}).")
        return None


def undo(*, surface: str, subject: str, actor: str = "user",
         via: str | None = None, meta: dict | None = None,
         store=None) -> dict | None:
    """Reverse the latest decision on `subject`: record an `undone` decision
    and delete the learning pair the reversed one produced, so a verdict the
    human took back stops teaching."""
    try:
        st = _store(store)
        prior = next((d for d in st.list_decisions(subject=str(subject),
                                                   limit=20)
                      if d["verdict"] != "undone"), None)
    except Exception:
        prior = None
    m = dict(meta or {})
    if prior:
        m["undoes"] = prior["id"]
        if prior.get("learning_pair_id"):
            try:
                from app.services import learning_store
                learning_store.delete(prior["learning_pair_id"], store=st)
                m["deleted_pair"] = prior["learning_pair_id"]
            except Exception as exc:
                print(f"[decisions] undo kept pair ({exc}).")
    return record(surface=surface, subject=subject, verdict="undone",
                  actor=actor, via=via, meta=m, store=store)


def recent(*, surface: str | None = None, subject: str | None = None,
           limit: int = 100, store=None) -> list[dict]:
    try:
        return _store(store).list_decisions(surface=surface, subject=subject,
                                            limit=limit)
    except Exception:
        return []


def summary(*, since: float | None = None, store=None) -> dict:
    """Per-surface counts and how many verdicts became learning pairs."""
    try:
        rows = _store(store).decision_counts(since=since)
    except Exception:
        rows = []
    by_surface: dict[str, dict] = {}
    for r in rows:
        s = by_surface.setdefault(r["surface"], {"n": 0, "learned": 0,
                                                 "by_verdict": {}})
        s["n"] += int(r["n"])
        s["learned"] += int(r["learned"] or 0)
        s["by_verdict"][r["verdict"]] = int(r["n"])
    return {"surfaces": by_surface,
            "n": sum(s["n"] for s in by_surface.values()),
            "learned": sum(s["learned"] for s in by_surface.values())}
