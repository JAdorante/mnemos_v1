"""Meeting debrief — a thank-you with what was covered, to the people on the call.

Flow:
  note lands (meeting_enhance.enhance_session) → ``offer`` puts a yes/no in
  chat → yes runs ``draft`` (one email per attendee) → the Debrief panel on the
  meeting page lists everyone on the call with their relationship to the user,
  a checkbox and an editable draft → "Send selected" → ``send_selected`` →
  ``mail_send`` through the user's own Gmail/Outlook send grant.

Rules:
- Recipients come from the meeting's roster (calendar invite or the manual
  "who's on the call"), never from transcript guesses. The user is never one.
- Nothing is sent by a model, an offer or a background job. ``send_selected``
  is the only path, the route calls it only for a live human session, and each
  row carries the hash of the body the human saw: an edit after the page
  loaded refuses the send instead of mailing something unseen.
- One send per meeting and recipient, ever (UNIQUE row; ``sent`` is final).
- The relationship shapes the tone. A user-set value (``person_attrs``
  key ``relationship``) wins; otherwise it is suggested from the email domain
  and shown as a suggestion.
- Note items backed by evidence the egress rule keeps home
  (``privacy_class``), and the user's own notepad jots, never reach a draft.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any

from app.storage import Store

RELATIONSHIPS = (
    "colleague", "manager", "direct report", "client", "prospect", "partner",
    "vendor", "investor", "advisor", "candidate", "friend", "external", "other",
)
# Item kinds a recap may cover. `note` is the user's live notepad — private.
_RECAP_KINDS = ("decision", "commitment", "next_step", "open_question")
MAX_RECIPIENTS = 12

_TONE = {
    "colleague": "a teammate: brief, friendly, first names, no ceremony",
    "manager": "the user's manager: concise, confident, lead with outcomes and the user's own next steps",
    "direct report": "someone who reports to the user: warm, clear about who owns what, encouraging",
    "client": "a client: warm and professional, appreciative of their time, clear on next steps from our side",
    "prospect": "a prospective customer: appreciative, crisp, no pressure, one clear next step",
    "partner": "a partner: collegial and professional, mutual next steps",
    "vendor": "a vendor: polite and direct, clear on what we need from them and by when",
    "investor": "an investor: concise, appreciative, crisp on what was discussed and what follows",
    "advisor": "an advisor: grateful for their input, name what the user will act on",
    "candidate": "a job candidate: warm, thankful, and promise nothing that was not said",
    "friend": "a friend: casual and warm",
    "external": "someone outside the user's organization: professional and warm",
    "other": "professional and warm",
}

_SYSTEM = """You write one short thank-you email from the user to one person who was on a call.

Rules:
- Thank them for their time in the first line. Plain text, no markdown, no headings.
- Recap only the items given, in plain words. Never invent a fact, date, number or promise.
- Lead with the items marked THEIRS (things they own or raised); then the user's own commitments; then anything else worth repeating. Skip items irrelevant to this person.
- Match the tone to the relationship described.
- Under 170 words. Open with "Hi <first name>,". End with the user's first name.
- Subject: short and specific to the meeting (no "Re:", no emoji).

Return JSON: {"subject": "...", "body": "..."}"""

_SCHEMA = {
    "type": "object",
    "properties": {
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["subject", "body"],
}


def enabled() -> bool:
    return os.environ.get("QUILL_MEETING_DEBRIEF", "1") not in ("0", "false", "False")


def _now() -> float:
    return time.time()


def body_hash(subject: str, body: str, email: str) -> str:
    """What the human approves: the exact subject, body and address."""
    raw = json.dumps([subject or "", body or "", (email or "").lower()],
                     ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def recipient_key(name: str, email: str) -> str:
    email = (email or "").strip().lower()
    return email if email else f"name:{(name or '').strip().lower()}"


# --- the note --------------------------------------------------------------------
def _day(ts: float) -> str:
    lt = time.localtime(ts)
    return f"{time.strftime('%A %B', lt)} {lt.tm_mday}"


def note_scope(store: Store, reflection_id: int) -> dict | None:
    """Title, date, summary, items and the meeting's roster for one note."""
    ref = store.get_reflection(int(reflection_id))
    if ref is None or ref.get("scope") != "meeting":
        return None
    summary = ref.get("summary") or ""
    title = summary.split("\n", 1)[0].strip()
    if " · " in title:
        title = title.split(" · ", 1)[0].strip()
    body = summary.split("\n", 1)[1].strip() if "\n" in summary else ""
    msid = None
    attendees: list[dict] = []
    if ref.get("subject_type") == "meeting_session" and ref.get("subject_id"):
        msid = int(ref["subject_id"])
        ms = store.get_meeting_session(msid) or {}
        title = (ms.get("title") or "").strip() or title
        attendees = list(ms.get("attendees") or [])
        org = ms.get("organizer")
        if isinstance(org, dict) and (org.get("email") or org.get("name")):
            attendees = [org, *attendees]
    else:
        # Legacy speech-session notes carry their roster on the session.
        from app.services import meeting_chat
        scope = meeting_chat.resolve_scope(
            store, meeting_reflection_id=int(reflection_id)) or {}
        attendees = list(scope.get("attendees") or [])
    return {
        "reflection_id": int(reflection_id),
        "meeting_session_id": msid,
        "title": title or "our meeting",
        "summary": body or summary,
        "date": _day(float(ref.get("period_start") or ref.get("created_at") or _now())),
        "attendees": attendees,
        "items": store.reflection_items(int(reflection_id)),
    }


def recap_items(store: Store, items: list[dict]) -> tuple[list[dict], int]:
    """Items a recap may carry, and how many were withheld by the egress rule."""
    from app.services import privacy_class
    fact_ids = sorted({int(f) for it in items for f in (it.get("source_fact_ids") or [])
                       if str(f).isdigit()})
    fmap = store.facts_by_ids(fact_ids) if fact_ids else {}
    out, withheld = [], 0
    for it in items:
        if it.get("kind") not in _RECAP_KINDS or it.get("review") == "dismissed":
            continue
        text = (it.get("text") or "").strip()
        if not text:
            continue
        refused = privacy_class.egress_refusal(
            f"{text}\n{it.get('detail') or ''}", source="meeting.debrief")
        for fid in it.get("source_fact_ids") or []:
            if refused:
                break
            fr = fmap.get(int(fid)) if str(fid).isdigit() else None
            if fr:
                refused = privacy_class.event_egress_refusal(
                    store, fr.get("source_event_id"))
        if refused:
            withheld += 1
            continue
        out.append(it)
    return out, withheld


# --- people ----------------------------------------------------------------------
def _user(store: Store) -> dict:
    from app.services import exhaust_ingest, identity, self_profile
    ident = identity.user_identity(store) or {}
    emails = set(exhaust_ingest._self_emails(store))
    for k in ("primary_email", "secondary_email"):
        if ident.get(k):
            emails.add(str(ident[k]).strip().lower())
    name = (ident.get("name") or "").strip()
    return {"name": name, "first": name.split()[0] if name else "",
            "emails": emails, "person_id": self_profile.self_person_id(store),
            "domains": {e.split("@", 1)[1] for e in emails if "@" in e}}


def _person_id(store: Store, name: str, email: str) -> int | None:
    pid = None
    if email:
        try:
            pid = store.find_person_by_contact("email", email.lower())
        except Exception:
            pid = None
    if pid is None and name:
        pid = store.find_person_exact(name)
    return pid


def _best_email(store: Store, pid: int | None) -> tuple[str, str]:
    """(address, source) from the person's details: user-set first, then the
    most confident active contact point. ('', '') when none is known."""
    if pid is None:
        return "", ""
    attr = (store.person_attrs(pid).get("email") or {}).get("value") or ""
    if "@" in attr:
        return attr.strip(), "people"
    for cp in store.list_contact_points(pid, type_="email") or []:
        v = (cp.get("value_display") or cp.get("value_normalized") or "").strip()
        if "@" in v:
            return v, "memory"
    return "", ""


def suggest_relationship(email: str, user_domains: set[str]) -> tuple[str, str]:
    """(relationship, why) from the address alone. ('', '') when unknowable."""
    from app.services.people_pipeline import _FREE_MAIL
    dom = email.split("@", 1)[1].lower() if "@" in (email or "") else ""
    if not dom or dom in _FREE_MAIL:
        return "", ""
    if dom in user_domains:
        return "colleague", f"same domain as you ({dom})"
    if user_domains - set(_FREE_MAIL):
        return "external", f"outside your domain ({dom})"
    return "", ""


def _graph_org(store: Store, name: str) -> str:
    try:
        from app.services import graph
        ctx = graph.context_for_person(name, store) or {}
    except Exception:
        return ""
    for a in ctx.get("affiliations") or []:
        if a.get("predicate") in ("works_at", "part_of") and a.get("name"):
            return str(a["name"]).strip()
    return ""


def attendees(store: Store, scope: dict) -> list[dict]:
    """Everyone on the call except the user, with email, person and relationship."""
    me = _user(store)
    out: list[dict] = []
    seen: set[str] = set()
    for a in scope.get("attendees") or []:
        if not isinstance(a, dict):
            continue
        name = (a.get("name") or "").strip()
        email = (a.get("email") or "").strip()
        if not name and not email:
            continue
        if email.lower() in me["emails"]:
            continue
        if name and me["name"] and name.lower() == me["name"].lower():
            continue
        pid = _person_id(store, name, email)
        if pid is not None and pid == me["person_id"]:
            continue
        # Keyed on the roster entry, not on an address found later, so a
        # draft stays attached to its person when memory supplies the email.
        key = recipient_key(name or email, email)
        email_source = "invite" if email else ""
        if not email:
            email, email_source = _best_email(store, pid)
        if key in seen:
            continue
        seen.add(key)
        attrs = store.person_attrs(pid) if pid is not None else {}
        rel = ((attrs.get("relationship") or {}).get("value") or "").strip()
        rel_source, rel_why = ("you", "") if rel else ("", "")
        if not rel:
            rel, rel_why = suggest_relationship(email, me["domains"])
            rel_source = "suggested" if rel else ""
        role = ((attrs.get("role") or {}).get("value") or "").strip()
        org = ((attrs.get("org") or {}).get("value") or "").strip()
        if not org and name:
            org = _graph_org(store, name)
        out.append({
            "key": key, "name": name or email.split("@")[0], "email": email,
            "email_source": email_source, "person_id": pid,
            "relationship": rel, "relationship_source": rel_source,
            "relationship_why": rel_why, "role": role, "org": org,
        })
        if len(out) >= MAX_RECIPIENTS:
            break
    return out


def set_relationship(store: Store, *, name: str, email: str,
                     relationship: str) -> dict:
    rel = (relationship or "").strip().lower()
    if rel and rel not in RELATIONSHIPS:
        return {"ok": False, "error": f"relationship must be one of {', '.join(RELATIONSHIPS)}"}
    pid = _person_id(store, name, email)
    if pid is None:
        if not name:
            return {"ok": False, "error": "a name is needed to remember this person"}
        pid = store.resolve_person(name, ts=_now())
    if rel:
        store.set_person_attr(int(pid), "relationship", rel, None, _now())
    else:
        store.clear_person_attr(int(pid), "relationship")
    return {"ok": True, "person_id": int(pid), "relationship": rel}


# --- the debrief rows ----------------------------------------------------------------
def _row(r) -> dict:
    d = dict(r)
    try:
        d["source_fact_ids"] = json.loads(d.get("source_fact_ids") or "[]")
    except Exception:
        d["source_fact_ids"] = []
    return d


def rows(store: Store, reflection_id: int) -> dict[str, dict]:
    with store._lock:
        rs = store._conn.execute(
            "SELECT * FROM meeting_debriefs WHERE reflection_id=?",
            (int(reflection_id),)).fetchall()
    return {r["recipient_key"]: _row(r) for r in rs}


def _upsert_draft(store: Store, *, reflection_id: int, msid: int | None,
                  person: dict, subject: str, body: str,
                  fact_ids: list[int], model: str) -> dict:
    now = _now()
    h = body_hash(subject, body, person.get("email") or "")
    with store._lock:
        store._conn.execute(
            """
            INSERT INTO meeting_debriefs
                (reflection_id, meeting_session_id, recipient_key, name, email,
                 person_id, relationship, subject, body, body_hash,
                 source_fact_ids, status, model, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft',?,?,?)
            ON CONFLICT(reflection_id, recipient_key) DO UPDATE SET
                name=excluded.name, email=excluded.email,
                person_id=excluded.person_id, relationship=excluded.relationship,
                subject=excluded.subject, body=excluded.body,
                body_hash=excluded.body_hash,
                source_fact_ids=excluded.source_fact_ids, status='draft',
                error=NULL, model=excluded.model, updated_at=excluded.updated_at
            WHERE meeting_debriefs.status != 'sent'
              AND meeting_debriefs.status != 'sending'
            """,
            (int(reflection_id), msid, person["key"], person.get("name"),
             person.get("email") or None, person.get("person_id"),
             person.get("relationship") or None, subject, body, h,
             json.dumps(fact_ids), model, now, now))
        store._conn.commit()
    return rows(store, reflection_id).get(person["key"]) or {}


def _packet(scope: dict, person: dict, items: list[dict], me: dict) -> str:
    first = (person.get("name") or "").split()[0] if person.get("name") else ""
    rel = person.get("relationship") or "other"
    who = person.get("name") or person.get("email")
    lines = [
        f"MEETING: {scope['title']} ({scope['date']})",
        f"FROM: {me['name'] or 'the user'}",
        f"TO: {who}" + (f", {person['role']}" if person.get("role") else "")
        + (f" at {person['org']}" if person.get("org") else ""),
        f"FIRST NAME: {first or who}",
        f"RELATIONSHIP: {rel} — write for {_TONE.get(rel, _TONE['other'])}",
    ]
    if scope.get("summary"):
        lines += ["", "SUMMARY:", scope["summary"][:1200]]
    lines += ["", "ITEMS:"]
    tokens = {t for t in re.split(r"\W+", (who or "").lower()) if len(t) > 1}
    for it in items:
        subj = (it.get("subject") or "").lower()
        theirs = bool(tokens and tokens & set(re.split(r"\W+", subj)))
        mark = "THEIRS " if theirs else ""
        detail = f" — {it['detail']}" if it.get("detail") else ""
        owner = f" [{it['subject']}]" if it.get("subject") else ""
        lines.append(f"- {mark}({it['kind']}){owner} {it['text']}{detail}")
    if not items:
        lines.append("- (no itemized points; thank them and reference the summary)")
    return "\n".join(lines)


def _fallback(scope: dict, person: dict, items: list[dict], me: dict) -> tuple[str, str]:
    """A plain template when no model is reachable — still reviewable."""
    first = (person.get("name") or "").split()[0] if person.get("name") else "there"
    bullets = "\n".join(f"- {it['text']}" for it in items[:8])
    body = (f"Hi {first},\n\nThanks for your time today. "
            f"A quick recap of what we covered in {scope['title']}:\n\n"
            + (bullets or "- " + (scope.get("summary") or "").split(".")[0])
            + f"\n\nBest,\n{me['first'] or ''}").rstrip()
    return f"Thanks — {scope['title']}", body


def draft(store: Store, reflection_id: int, *, keys: list[str] | None = None,
          regenerate: bool = False) -> dict[str, Any]:
    """Draft (or redraft) one email per selected attendee. Sent rows are kept."""
    scope = note_scope(store, reflection_id)
    if scope is None:
        return {"ok": False, "error": "meeting note not found"}
    people = attendees(store, scope)
    if keys:
        want = set(keys)
        people = [p for p in people if p["key"] in want]
    existing = rows(store, reflection_id)
    items, withheld = recap_items(store, scope["items"])
    fact_ids = sorted({int(f) for it in items for f in (it.get("source_fact_ids") or [])
                       if str(f).isdigit()})
    me = _user(store)
    from app.services.model_router import MODELS, router
    model = MODELS.get("debrief", "")
    drafted = 0
    for p in people:
        cur = existing.get(p["key"])
        if cur and cur.get("status") in ("sent", "sending"):
            continue
        if cur and cur.get("body") and not regenerate:
            continue
        out: dict = {}
        try:
            out = router.complete_json(
                "debrief", system=_SYSTEM,
                messages=[{"role": "user", "content": _packet(scope, p, items, me)}],
                schema=_SCHEMA, max_tokens=700, model=model)
        except Exception as exc:
            print(f"[meeting_debrief] model unavailable, using template ({exc}).")
        subject = (out.get("subject") or "").strip()
        body = (out.get("body") or "").strip()
        used = model
        if not subject or not body:
            subject, body = _fallback(scope, p, items, me)
            used = "template"
        _upsert_draft(store, reflection_id=reflection_id,
                      msid=scope["meeting_session_id"], person=p,
                      subject=subject[:200], body=body[:5000],
                      fact_ids=fact_ids, model=used)
        drafted += 1
    return {"ok": True, "drafted": drafted, "withheld": withheld,
            **state(store, reflection_id)}


def edit(store: Store, reflection_id: int, key: str, *,
         subject: str, body: str, email: str | None = None) -> dict:
    """Save the human's edit; the new hash is what a send must match."""
    cur = rows(store, reflection_id).get(key)
    if cur is None:
        return {"ok": False, "error": "no draft for that person"}
    if cur.get("status") in ("sent", "sending"):
        return {"ok": False, "error": "already sent"}
    addr = (email if email is not None else cur.get("email") or "").strip()
    if addr and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", addr):
        return {"ok": False, "error": "that is not an email address"}
    h = body_hash(subject, body, addr)
    with store._lock:
        store._conn.execute(
            "UPDATE meeting_debriefs SET subject=?, body=?, email=?, body_hash=?, "
            "status='draft', error=NULL, updated_at=? WHERE id=?",
            (subject[:200], body[:5000], addr or None, h, _now(), cur["id"]))
        store._conn.commit()
    return {"ok": True, "key": key, "body_hash": h}


def state(store: Store, reflection_id: int) -> dict[str, Any]:
    """Everything the Debrief panel shows."""
    from app.services import mail_send
    scope = note_scope(store, reflection_id)
    if scope is None:
        return {"ok": False, "error": "meeting note not found"}
    people = attendees(store, scope)
    existing = rows(store, reflection_id)
    _, withheld = recap_items(store, scope["items"])
    out = []
    for p in people:
        r = existing.get(p["key"]) or {}
        out.append({**p,
                    "email": r.get("email") or p["email"],
                    "status": r.get("status") or "none",
                    "subject": r.get("subject") or "",
                    "body": r.get("body") or "",
                    "body_hash": r.get("body_hash") or "",
                    "sent_at": r.get("sent_at"),
                    "error": r.get("error"),
                    "model": r.get("model")})
    send = mail_send.status()
    return {"ok": True, "enabled": enabled(), "reflection_id": int(reflection_id),
            "title": scope["title"], "attendees": out, "withheld": withheld,
            "relationships": list(RELATIONSHIPS), "send": send,
            "can_send": bool(send["enabled"] and send["provider"])}


# --- sending ---------------------------------------------------------------------
def send_selected(store: Store, reflection_id: int,
                  selections: list[dict], *, live_human: bool) -> dict[str, Any]:
    """Send the drafts a human selected. ``selections`` = [{key, body_hash}].

    ``live_human`` is decided by the route from the request, never a body
    field. Each row is claimed (draft→sending) atomically, so a double-click
    or two tabs cannot send twice.
    """
    from app.services import mail_send
    if not live_human:
        return {"ok": False, "error": "sending needs you, signed in, on the page"}
    provider = mail_send.active_provider()
    if not mail_send.enabled() or provider is None:
        return {"ok": False, "error": "allow sending from Gmail or Outlook first"}
    existing = rows(store, reflection_id)
    results = []
    for sel in selections or []:
        key = str(sel.get("key") or "")
        r = existing.get(key)
        if r is None:
            results.append({"key": key, "ok": False, "error": "no draft"})
            continue
        if r.get("status") == "sent":
            results.append({"key": key, "ok": False, "error": "already sent"})
            continue
        if not r.get("email"):
            results.append({"key": key, "ok": False, "error": "no email address"})
            continue
        if sel.get("body_hash") != r.get("body_hash") or \
                body_hash(r["subject"], r["body"], r["email"]) != r["body_hash"]:
            results.append({"key": key, "ok": False,
                            "error": "the draft changed — reload and review it"})
            continue
        with store._lock:
            cur = store._conn.execute(
                "UPDATE meeting_debriefs SET status='sending', updated_at=? "
                "WHERE id=? AND status IN ('draft','failed') AND body_hash=?",
                (_now(), r["id"], r["body_hash"]))
            store._conn.commit()
        if cur.rowcount != 1:
            results.append({"key": key, "ok": False, "error": "already in flight"})
            continue
        res = mail_send.send(provider, to=r["email"], subject=r["subject"],
                             body=r["body"], to_name=r.get("name"))
        now = _now()
        with store._lock:
            if res.get("ok"):
                store._conn.execute(
                    "UPDATE meeting_debriefs SET status='sent', provider=?, "
                    "provider_message_id=?, sent_at=?, updated_at=?, error=NULL "
                    "WHERE id=?",
                    (provider, res.get("message_id"), now, now, r["id"]))
            else:
                store._conn.execute(
                    "UPDATE meeting_debriefs SET status='failed', error=?, "
                    "updated_at=? WHERE id=?",
                    (str(res.get("error") or "send failed")[:500], now, r["id"]))
            store._conn.commit()
        results.append({"key": key, "ok": bool(res.get("ok")),
                        "error": res.get("error")})
    sent = sum(1 for x in results if x["ok"])
    return {"ok": sent > 0 or not results, "sent": sent, "results": results,
            "provider": provider}


# --- the offer -------------------------------------------------------------------
def offer(store: Store, reflection_id: int) -> bool:
    """When a note lands: offer (yes/no in chat) to draft the debriefs."""
    if not enabled():
        return False
    scope = note_scope(store, reflection_id)
    if scope is None:
        return False
    people = attendees(store, scope)
    if not people:
        return False
    try:
        from app.services.agent_bridge import worker
    except Exception:
        return False
    if worker is None:
        return False
    names = [p["name"] for p in people]
    who = names[0] if len(names) == 1 else ", ".join(names[:-1]) + f" and {names[-1]}"
    msg = (f"Your note for '{scope['title']}' is ready. Draft thank-you emails "
           f"to {who} with what you covered?\n\n"
           "Reply 'yes' and I'll draft them — you pick who gets one and nothing "
           "sends until you press Send on the meeting page.")
    return worker._add_offer({
        "kind": "meeting_debrief",
        "message": msg,
        "title": scope["title"],
        "reflection_id": int(reflection_id),
        "items": names,
    })


def note_href(reflection_id: int) -> str:
    return f"/meeting/note/{int(reflection_id)}#debrief"
