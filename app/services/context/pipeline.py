"""CAL — `associate()`: one event in, one attribution out.

The seven stages of the design, with the exits that make it affordable. Most
events leave at stage 3 having resolved on an identifier alone; the scorer runs
only when identifiers disagree or run out, and a model is consulted only when
the scorer cannot separate its top candidates.

    normalize → extract → LOOK UP ─┬─ bound ──────────────► write (no model)
                                   └─ ambiguous ─► score ─┬─ clear ─► write
                                                          └─ not ──► defer

Three properties this file is responsible for, none of which live in the pieces
it calls:

  capture never waits on inference
      Escalation is recorded as `pending` and handed to a caller to run
      elsewhere. `associate()` itself makes no model call, ever. If it did, a
      slow local model would become a capture stall.

  shadow by default
      Stage 1 of the design writes attributions but surfaces nothing, so that
      coverage can be measured on real capture before anyone is shown a label.
      `shadow=False` is a deliberate act.

  excluded means excluded
      A privacy-excluded event is not attributed, not scored, not escalated and
      not stored. Not "attributed but hidden" — an event that never becomes a
      binding cannot leak through one later.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.services.context import bindings as _bindings
from app.services.context import keys as ckeys
from app.services.context import resolver as rs

# Privacy classes that must never reach attribution at all.
EXCLUDED_CLASSES = frozenset({"never_send", "never-send"})


@dataclass
class ContextResult:
    event_id: int
    band: str                       # deterministic|scored|scored_multi|pending|provisional|unbound|excluded
    anchors: tuple = ()
    decision: object | None = None
    decision_id: int | None = None
    signal_keys: tuple = ()
    latency_ms: float = 0.0
    shadow: bool = True
    reason: str = ""

    @property
    def needs_escalation(self) -> bool:
        return self.band == "pending"


def _privacy_class(event, meta: dict) -> str:
    try:
        from app.services import privacy_class as pc
        return pc.classify_event(event) or ""
    except Exception:
        return str(meta.get("privacy_class") or "")


def extract(event, meta: dict, *, store=None, index=None) -> tuple:
    """Everything this event carries: binding keys, and surface hits.

    Keys come from the identifier miner via the binding grammar; surfaces come
    from window titles and VLM headings. Deliberately separate — a key is an
    observed identifier and a surface is a resolved name, and collapsing them
    is how model output starts minting bindings.
    """
    raw = getattr(event, "raw", "") or ""
    window = str(meta.get("window") or "")
    burl = meta.get("browser_url") or (
        f"https://{meta['url_domain']}" if meta.get("url_domain") else None)
    mined = meta.get("identifiers")
    if not mined:
        try:
            from app.perception import identifiers as _idents
            mined = _idents.extract_identifiers(raw, window=window,
                                                browser_url=burl)
        except Exception:
            mined = []
    sig = tuple(sk for sk in ckeys.from_identifiers(mined)
                if sk.tier != ckeys.SUPPORTING)

    hits = ()
    if store is not None or index is not None:
        try:
            from app.services.context import surfaces as sf
            idx = index or sf.SurfaceIndex(store)
            found = list(idx.from_title(window)) if window else []
            if not found:
                from app.services.context.replay import vlm_title
                heading = vlm_title(meta)
                if heading:
                    found = list(idx.from_heading(heading))
            hits = tuple(found)
        except Exception as exc:
            print(f"[cal.pipeline] surfaces unavailable ({exc}).")
    return sig, hits


def candidates(anchors, surface_hits=(), *, frame_weights=None) -> list:
    """Group evidence into scorable candidates.

    Only strong or medium evidence may PROPOSE a candidate. Frame weight is
    attached to candidates that already exist and never creates one, which is
    the §3.2 rule expressed where it is easy to get wrong.
    """
    by_node: dict = {}

    def slot(node_type, node_id, name):
        return by_node.setdefault((node_type, node_id),
                                  {"name": name, "strong": [], "medium": [],
                                   "keys": set()})

    for a in anchors or []:
        s = slot(a.node_type, a.node_id, a.name)
        if a.tier == ckeys.STRONG:
            s["strong"].append(a.strength)
            s["keys"].add(a.name)
        else:
            s["medium"].append(a.strength)
    for h in surface_hits or []:
        s = slot(h.node_type, h.node_id, h.name)
        s["medium"].append(h.strength)

    import math
    out = []
    for (ntype, nid), s in by_node.items():
        feats = {}
        if s["strong"]:
            feats["f_key"] = max(s["strong"])
            feats["f_key_n"] = math.log(1 + len(s["keys"]))
        if s["medium"]:
            feats["f_alias"] = max(s["medium"])
        fw = (frame_weights or {}).get((ntype, nid))
        if fw:
            feats["f_frame"] = float(fw)
        if not (s["strong"] or s["medium"]):
            continue                      # supporting alone cannot propose
        out.append(rs.Candidate(ntype, nid, s["name"], feats,
                                frozenset(s["keys"])))
    return out


def associate(event, store, *, event_id: int | None = None, cache=None,
              index=None, frame_weights=None, shadow: bool = True,
              persist: bool = True, now: float | None = None) -> ContextResult:
    """Attribute one event. Deterministic, bounded, and model-free."""
    t0 = time.perf_counter()
    eid = int(event_id if event_id is not None
              else getattr(event, "id", 0) or 0)
    meta = getattr(event, "meta", None)
    meta = meta if isinstance(meta, dict) else {}

    pclass = _privacy_class(event, meta)
    if pclass in EXCLUDED_CLASSES:
        return ContextResult(eid, "excluded", reason=pclass, shadow=shadow,
                             latency_ms=(time.perf_counter() - t0) * 1000)

    bc = cache or _bindings.cache(store)
    sig, hits = extract(event, meta, store=store, index=index)
    anchors = bc.anchors(sig, now=now)

    # `nameable` deliberately does NOT gate attribution. It answers "may this
    # key TITLE a stretch of work", which OCR-derived identifiers may not,
    # because vision invents plausible-looking repos and domains. But an
    # invented identifier resolves to nothing: the binding table is the filter,
    # and a key that IS bound was minted by a trusted source, a confirmation or
    # the ratchet. Requiring nameability here would refuse perfectly good
    # attributions from exactly the surface that has the fewest.
    strong = [a for a in anchors if a.tier == ckeys.STRONG]
    nodes = {(a.node_type, a.node_id) for a in strong}
    if len(nodes) == 1:
        # ── the exit that pays for the whole design ──
        a = strong[0]
        res = ContextResult(eid, "deterministic", tuple(anchors), None, None,
                            sig, (time.perf_counter() - t0) * 1000, shadow,
                            "single_strong_key")
        if persist and eid:
            _write(store, res, [{"node_type": a.node_type, "node_id": a.node_id,
                                 "role": "project", "confidence": a.strength,
                                 "method": "key"}], now=now)
        return res

    cands = candidates(anchors, hits, frame_weights=frame_weights)
    decision = rs.decide(rs.score(cands))
    res = ContextResult(eid, decision.band, tuple(anchors), decision, None,
                        sig, (time.perf_counter() - t0) * 1000, shadow,
                        decision.method)
    if persist and eid:
        rows = [{"node_type": c.node_type, "node_id": c.node_id,
                 "role": "project", "confidence": decision.confidence,
                 "method": decision.method or decision.band}
                for c in decision.chosen]
        res.decision_id = _write(store, res, rows, now=now)
    return res


def _write(store, res: ContextResult, anchor_rows, *, now=None) -> int | None:
    """Persist the attribution and the reasoning behind it. Never raises."""
    decision_id = None
    try:
        if res.decision is not None:
            decision_id = store.record_context_decision(
                res.event_id, res.decision, latency_ms=res.latency_ms,
                weights_version=rs.WEIGHTS_VERSION, ts=now)
        if anchor_rows:
            store.add_event_context(res.event_id, anchor_rows,
                                    decision_id=decision_id,
                                    shadow=res.shadow, ts=now)
    except Exception as exc:
        print(f"[cal.pipeline] context write skipped ({exc}).")
    return decision_id


def escalate_pending(store, result: ContextResult, *, summary: str = "",
                     ask=None, now: float | None = None):
    """Run the deferred model call. Called OFF the capture path, by a job.

    Kept out of `associate` on purpose: an event is written `pending` and
    upgraded when this completes, so a slow local model can never become a
    capture stall.
    """
    from app.services.context import escalate as esc
    if not result.needs_escalation:
        return esc.Escalation(None, None, "local", 0.0, (), "not_escalatable")
    got = esc.resolve(store, result.decision, summary=summary,
                      signal_keys=result.signal_keys, ask=ask, ts=now)
    if got.chosen is not None:
        try:
            store.add_event_context(
                result.event_id,
                [{"node_type": got.chosen.node_type,
                  "node_id": got.chosen.node_id, "role": "project",
                  "confidence": got.confidence, "method": "escalated"}],
                decision_id=result.decision_id, shadow=result.shadow, ts=now)
        except Exception as exc:
            print(f"[cal.pipeline] upgrade skipped ({exc}).")
        # The ratchet minted keys; the cache must not keep answering "miss".
        _bindings.cache(store).invalidate()
    return got


__all__ = ["ContextResult", "associate", "candidates", "extract",
           "escalate_pending", "EXCLUDED_CLASSES"]
