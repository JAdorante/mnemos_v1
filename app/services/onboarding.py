"""Onboarding — seed vinceo.ai's knowledge of a new user from a one-time profile.

vinceo.ai normally learns who "Justin" is, which names are projects, and what a
day looks like only by observing for weeks. A new user therefore starts cold:
ASR misspells names, entity resolution has no targets, and chat grounding is
empty. This module shortcuts that with a one-time PROFILE SHEET — a plain JSON
file the user fills in (or an equivalent POST) — whose answers land on the
existing rails as DATA, never as user-specific code (the generality rule):

    people + aliases   -> people table          (resolution + ASR bias, #11)
    projects/orgs/tools-> entities table        (same, plus graph anchoring)
    relationships      -> asserted graph edges  (survive graph rebuilds)
    identity/schedule/
    priorities/notes   -> claim facts, epistemic ACCEPTED (the human said so),
                          pre-approved, provenance-linked to one SYSTEM event
                          each (source="onboarding.survey"), semantically
                          indexed so chat retrieval finds them.

Asked ONCE, not on every start: the first boot writes a template sheet and
prints a single pointer; when the filled sheet appears (next boot or an
explicit /onboarding/ingest), it's ingested and the state file marks the flow
completed — after which startup stays silent forever. Ingestion is idempotent
and delta-aware: every answer has a stable content key recorded in the state
file, so re-running (or editing the sheet later and re-ingesting) adds only
what's new and never duplicates.
"""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.events import Event, Modality
from app.services import confidence as _conf
from app.storage import Store, get_store

SOURCE = "onboarding.survey"

# Bounds so a pathological sheet can't flood the DB in one call.
_MAX_ITEMS_PER_SECTION = 50
_MAX_NOTE_PARAGRAPHS = 20
_MAX_TEXT_CHARS = 500

_TEMPLATE: dict[str, Any] = {
    "_instructions": (
        "Prefer the guided UI at http://127.0.0.1:8000/onboarding. Or fill this "
        "JSON (every field optional), then restart or POST /onboarding/ingest. "
        "Edit later and re-ingest; only new/changed answers are added."),
    "identity": {
        "name": "",
        "role": "",
        "description": "",
        "primary_email": "",
        "secondary_email": "",
        "phone": "",
    },
    "people": [
        {"name": "", "aliases": [], "relationship": "", "note": ""},
    ],
    "projects": [
        {"name": "", "kind": "project", "aliases": [], "note": ""},
    ],
    "tools": [],
    "schedule": [],
    "priorities": [],
    "notes": "",
}


def _profile_path() -> Path:
    return Path(settings.onboarding.profile_path)


def _state_path() -> Path:
    return Path(settings.onboarding.state_path)


def _load_state() -> dict:
    p = _state_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[onboarding] state unreadable ({exc}); treating as fresh.")
        return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[onboarding] state save failed ({exc}).")


def _clip(s: str) -> str:
    return (s or "").strip()[:_MAX_TEXT_CHARS]


def _item_key(section: str, payload: Any) -> str:
    """Stable content fingerprint for delta ingestion (re-runs skip old keys)."""
    blob = json.dumps([section, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def _predicate(relationship: str) -> str:
    """'Works with' -> 'works_with'; empty/junk -> generic 'knows'."""
    words = [w for w in (relationship or "").lower().split() if w.isalpha()]
    return "_".join(words) if words else "knows"


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def _norm_phone(value: str) -> str:
    digits = "".join(c for c in (value or "") if c.isdigit())
    return digits


def _mirror_self_contacts(store: Store, person_id: int, ident: dict,
                          *, ts: float) -> None:
    """Write primary/secondary email + phone onto the self person node."""
    contacts = (
        ("email", _clip(ident.get("primary_email")), "primary"),
        ("email", _clip(ident.get("secondary_email")), "secondary"),
        ("phone", _clip(ident.get("phone")), "primary"),
    )
    for type_, display, role in contacts:
        if not display:
            continue
        norm = _norm_email(display) if type_ == "email" else _norm_phone(display)
        if not norm:
            continue
        try:
            store.upsert_contact_point(
                person_id=person_id, type_=type_, value_display=display,
                value_normalized=norm, confidence=0.95,
                attribution_method="onboarding.survey",
                verification_status="user_stated", source_event_id=None,
                evidence_quote=display, discourse_role=role, ts=ts,
                created_by="onboarding", pipeline_version="onboarding.v1")
        except Exception as exc:
            print(f"[onboarding] contact mirror skipped ({exc}).")


def write_template(path: Path | None = None, *, overwrite: bool = False) -> dict:
    """Create the blank profile sheet. Never clobbers an existing (possibly
    half-filled) sheet unless told to."""
    p = path or _profile_path()
    if p.is_file() and not overwrite:
        return {"created": False, "path": str(p)}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(_TEMPLATE, indent=2, ensure_ascii=False),
                 encoding="utf-8")
    return {"created": True, "path": str(p)}


def load_profile() -> dict | None:
    """Return the on-disk profile sheet, or None if missing/unreadable."""
    p = _profile_path()
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_profile(profile: dict) -> str:
    """Persist a profile dict to the sheet path (keeps JSON as a backup)."""
    p = _profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Drop UI-only noise; keep the canonical shape.
    clean = {k: v for k, v in (profile or {}).items() if not str(k).startswith("_")}
    out = dict(_TEMPLATE)
    out.update(clean)
    out["_instructions"] = (
        "Filled via the /onboarding web UI (or edited by hand). "
        "Re-ingest with POST /onboarding/ingest; only new answers are added.")
    p.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    return str(p)


def status() -> dict:
    state = _load_state()
    return {
        "enabled": settings.onboarding.enabled,
        "completed": bool(state.get("completed_at")),
        "completed_at": state.get("completed_at"),
        "profile_path": str(_profile_path()),
        "profile_exists": _profile_path().is_file(),
        "items_ingested": len(state.get("item_keys") or []),
        "ui_url": "/onboarding",
    }


def launch_status(*, authorized: bool = True, lan_gate: bool = False) -> dict:
    """Payload for the `/` welcome page: new vs returning + unlock need.

    Local-first: "returning" means this install already has a completed survey
    or a known user name — not a cloud account. `needs_unlock` is the LAN API
    token gate for non-loopback browsers.
    """
    st = status()
    name = ""
    role = ""
    try:
        from app.services.identity import user_identity
        u = user_identity()
        name = (u.get("name") or "").strip()
        role = (u.get("role") or "").strip()
    except Exception:
        pass
    returning = bool(st.get("completed") or name)
    return {
        "ok": True,
        "mode": "returning" if returning else "new",
        "user_name": name,
        "user_role": role,
        "completed": bool(st.get("completed")),
        "home_url": "/today",
        "onboarding_url": "/onboarding",
        "lan_gate": bool(lan_gate),
        "authorized": bool(authorized),
        "needs_unlock": bool(lan_gate and not authorized),
    }


class _Ingestor:
    """One ingest pass. Wraps the shared bookkeeping (provenance event + fact +
    dedup key) so each section handler stays a few lines."""

    def __init__(self, store: Store, seen: set[str], now: float) -> None:
        self.store = store
        self.seen = seen
        self.now = now
        self.counts = {"claims": 0, "people": 0, "entities": 0, "relations": 0,
                       "skipped": 0}
        self.new_keys: list[str] = []
        # Semantic indexing is best-effort and only against the LIVE store —
        # a test-injected temp store must never write the real LanceDB index.
        self._index = None
        try:
            if store is get_store():
                from app.services.memory import memory
                self._index = memory.index_fact
        except Exception:
            self._index = None

    def _fresh(self, section: str, payload: Any) -> bool:
        key = _item_key(section, payload)
        if key in self.seen:
            self.counts["skipped"] += 1
            return False
        self.seen.add(key)
        self.new_keys.append(key)
        return True

    def _provenance(self, section: str, text: str) -> int:
        """One SYSTEM event per answer — the anchor every seeded fact points
        back to, so 'where did vinceo.ai learn this?' has a real answer."""
        ev = Event(time=self.now, modality=Modality.SYSTEM, raw=text,
                   summary=f"[onboarding] {text}", source=SOURCE,
                   meta={"section": section})
        _conf.attach(ev, _conf.ACCEPTED)   # the human typed it — top trust tier
        return self.store.insert(ev)

    def claim(self, section: str, text: str) -> None:
        """An ACCEPTED, pre-approved claim fact with provenance + search index."""
        text = _clip(text)
        if not text:
            return
        eid = self._provenance(section, text)
        fid = self.store.add_claim(text, source_event_id=eid, source_span=text,
                                   confidence=1.0, extracted_at=self.now)
        self.store.review_fact(fid, "approved")
        self.counts["claims"] += 1
        if self._index is not None:
            try:
                self._index(fid, "claim", text, self.now)
            except Exception as exc:
                print(f"[onboarding] fact index skipped ({exc}).")

    def person(self, name: str, aliases: list[str]) -> int:
        pid = self.store.resolve_person(name, ts=self.now)
        for a in aliases or []:
            if _clip(a):
                self.store.touch_person(pid, ts=self.now, alias=_clip(a))
        if pid:
            self.counts["people"] += 1
        return pid

    def entity(self, name: str, kind: str, aliases: list[str]) -> int:
        eid = self.store.resolve_entity(name, kind or "project", ts=self.now)
        for a in aliases or []:
            if _clip(a):
                self.store.touch_entity(eid, ts=self.now, alias=_clip(a))
        if eid:
            self.counts["entities"] += 1
        return eid

    def relation(self, subj: tuple[str, int], predicate: str,
                 obj: tuple[str, int], source_event_id: int | None = None) -> None:
        if not (subj[1] and obj[1]):
            return
        # Asserted, not derived: the user stated it, so graph rebuilds keep it.
        self.store.add_relation(subj[0], subj[1], predicate, obj[0], obj[1],
                                origin="asserted", confidence=1.0,
                                source_event_id=source_event_id, ts=self.now)
        self.counts["relations"] += 1


def ingest(profile: dict | None = None, store: Store | None = None) -> dict:
    """Feed a profile (dict, or the sheet on disk) into vinceo.ai's knowledge.

    Idempotent and delta-aware via per-answer content keys in the state file.
    Marks onboarding completed on the first successful pass; later calls still
    work (edit the sheet, re-ingest) — completion only silences the asking.
    """
    if not settings.onboarding.enabled:
        return {"ok": False, "error": "onboarding disabled (QUILL_ONBOARDING=0)"}
    if profile is None:
        p = _profile_path()
        if not p.is_file():
            return {"ok": False, "error": f"no profile sheet at {p}"}
        try:
            profile = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"ok": False, "error": f"profile unreadable: {exc}"}
    if not isinstance(profile, dict):
        return {"ok": False, "error": "profile must be a JSON object"}
    # Keep the on-disk sheet in sync when answers arrive from the web UI.
    try:
        save_profile(profile)
    except Exception as exc:
        print(f"[onboarding] profile persist skipped ({exc}).")

    store = store or get_store()
    state = _load_state()
    seen = set(state.get("item_keys") or [])
    ing = _Ingestor(store, seen, time.time())

    # --- identity ---------------------------------------------------------
    ident = profile.get("identity") or {}
    user_name = _clip(ident.get("name") if isinstance(ident, dict) else "")
    user_pid = 0
    if user_name:
        user_pid = store.resolve_person(user_name, ts=ing.now)
    if isinstance(ident, dict):
        for field, phrase in (
            ("name", "The user's name is {}."),
            ("role", "The user works as {}."),
            ("description", "How the user describes their work: {}"),
            ("primary_email", "The user's primary email is {}."),
            ("secondary_email", "The user's secondary email is {}."),
            ("phone", "The user's phone number is {}."),
        ):
            val = _clip(ident.get(field))
            if val and ing._fresh("identity", [field, val]):
                ing.claim("identity", phrase.format(val))
        # Mirror contact onto the self person so agents/People UI can use them.
        if user_pid:
            _mirror_self_contacts(store, user_pid, ident, ts=ing.now)

    # --- people -----------------------------------------------------------
    for item in (profile.get("people") or [])[:_MAX_ITEMS_PER_SECTION]:
        if not isinstance(item, dict) or not _clip(item.get("name")):
            continue
        name = _clip(item["name"])
        if not ing._fresh("people", {k: item.get(k) for k in
                                     ("name", "aliases", "relationship", "note")}):
            continue
        pid = ing.person(name, item.get("aliases") or [])
        rel = _clip(item.get("relationship"))
        note = _clip(item.get("note"))
        eid_ev = None
        if rel or note:
            desc = f"{name}" + (f" — {rel}" if rel else "") + (f": {note}" if note else "")
            eid_ev = ing._provenance("people", desc)
        if rel and user_pid:
            ing.relation(("person", user_pid), _predicate(rel), ("person", pid),
                         source_event_id=eid_ev)
        if note:
            ing.claim("people", f"About {name}: {note}")

    # --- projects / orgs --------------------------------------------------
    for item in (profile.get("projects") or [])[:_MAX_ITEMS_PER_SECTION]:
        if not isinstance(item, dict) or not _clip(item.get("name")):
            continue
        name = _clip(item["name"])
        if not ing._fresh("projects", {k: item.get(k) for k in
                                       ("name", "kind", "aliases", "note")}):
            continue
        ent = ing.entity(name, _clip(item.get("kind")) or "project",
                         item.get("aliases") or [])
        if user_pid:
            ing.relation(("person", user_pid), "involved_in", ("entity", ent))
        note = _clip(item.get("note"))
        if note:
            ing.claim("projects", f"About {name}: {note}")

    # --- tools (plain strings or {name, note}) ------------------------------
    for item in (profile.get("tools") or [])[:_MAX_ITEMS_PER_SECTION]:
        name = _clip(item.get("name")) if isinstance(item, dict) else _clip(item)
        if not name or not ing._fresh("tools", name):
            continue
        ent = ing.entity(name, "tool", [])
        if user_pid:
            ing.relation(("person", user_pid), "uses", ("entity", ent))

    # --- schedule / priorities (plain strings -> accepted claims) ----------
    for section, phrase in (("schedule", "The user's routine: {}"),
                            ("priorities", "Current priority for the user: {}")):
        for item in (profile.get(section) or [])[:_MAX_ITEMS_PER_SECTION]:
            text = _clip(item)
            if text and ing._fresh(section, text):
                ing.claim(section, phrase.format(text))

    # --- free-form notes: one claim per paragraph ---------------------------
    notes = profile.get("notes") or ""
    if isinstance(notes, str):
        paras = [p.strip() for p in notes.split("\n\n") if p.strip()]
        for p_text in paras[:_MAX_NOTE_PARAGRAPHS]:
            if ing._fresh("notes", p_text):
                ing.claim("notes", _clip(p_text))

    ingested = sum(v for k, v in ing.counts.items() if k != "skipped")
    if ing.new_keys:
        state["item_keys"] = sorted(seen)
    if ingested and not state.get("completed_at"):
        state["completed_at"] = ing.now   # first real answers => asked no more
    if ing.new_keys or ingested:
        _save_state(state)
    print(f"[onboarding] ingest: {ing.counts}")
    return {"ok": True, **ing.counts, "completed": bool(state.get("completed_at"))}


def startup_check(store: Store | None = None) -> None:
    """The once-only boot hook (called from app startup, best-effort).

    First boot: write the template sheet and say so ONCE. A later boot that
    finds the sheet filled (and onboarding not yet completed) ingests it
    automatically. Once completed, this is silent forever — the 'ask once,
    not every start' contract.
    """
    if not settings.onboarding.enabled:
        return
    state = _load_state()
    if state.get("completed_at"):
        return
    p = _profile_path()
    if p.is_file():
        res = ingest(store=store)
        if res.get("ok") and any(res.get(k) for k in
                                 ("claims", "people", "entities", "relations")):
            print(f"[onboarding] profile ingested from {p} — vinceo.ai now knows "
                  "the basics. Edit the sheet and POST /onboarding/ingest to "
                  "add more later.")
        return
    if not state.get("template_created_at"):
        out = write_template(p)
        state["template_created_at"] = time.time()
        _save_state(state)
        if out.get("created"):
            print("[onboarding] new here? Open http://127.0.0.1:8000/onboarding "
                  "to tell vinceo.ai about your day-to-day (every field optional). "
                  "Asked once — not again. JSON sheet also at "
                  f"{p} if you prefer.")
