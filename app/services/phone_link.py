"""Phone Link integration — control the Windows Phone Link app from Sparrow.

Microsoft does not ship a public Phone Link API. This module drives the
installed Phone Link UI via PowerShell + UI Automation (scripts adapted from
https://github.com/Heartran/phonelink-mcp-server, MIT).

Typical flow: you say "text <name> I'll be late" → router picks surface
phone_link → this module launches Phone Link and sends the SMS (with approval).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Callable

_SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts" / "phone_link"


def _example_recipient() -> str | None:
    """A real recent contact from the user's OWN data, for a help string — never a
    hardcoded name. Prefers an enrolled speaker, then the top person in the
    vocabulary graph. Returns None on a fresh/empty data dir so the caller can fall
    back to neutral, name-free phrasing. Fully lazy + guarded: this module is
    Windows-facing and must degrade with no graph/speakers present."""
    try:
        from app.services.speakers import speakers as _spk
        names = _spk.enrolled_names()
        if names:
            return names[0]
    except Exception:
        pass
    try:
        from app.services.vocabulary import vocabulary as _vocab
        people = (_vocab.get_bias_terms() or {}).get("people") or []
        if people:
            return people[0]
    except Exception:
        pass
    return None


def _enabled() -> bool:
    if os.name != "nt":
        return False
    return os.environ.get("QUILL_PHONE_LINK", "1") not in ("0", "false", "False")


def _ps() -> list[str]:
    exe = os.environ.get("QUILL_PHONE_LINK_PS", "powershell.exe")
    return [exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]


def _run_script(name: str, **params) -> dict:
    script = _SCRIPTS / name
    if not script.is_file():
        return {"ok": False, "error": f"missing script: {script}"}
    args = _ps() + [str(script)]
    for k, v in params.items():
        if v is None:
            continue
        args.append(f"-{k}")
        args.append(str(v))
    try:
        # The PowerShell core sets [Console]::OutputEncoding to UTF-8 (no BOM), so
        # decode as UTF-8 — NOT the Windows locale default (cp1252), which chokes
        # on the emoji/curly-quotes/em-dashes in real message + notification text
        # and would crash the subprocess reader thread, blanking stdout ("exit 0").
        # utf-8-sig tolerates a stray BOM; errors="replace" keeps one odd byte from
        # ever losing the whole response.
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=120, cwd=str(_SCRIPTS),
            encoding="utf-8-sig", errors="replace",
        )
        out = (proc.stdout or "").strip()
        if out:
            try:
                data = json.loads(out)
                data["ok"] = not data.get("error") and proc.returncode == 0
                return data
            except json.JSONDecodeError:
                pass
        err = (proc.stderr or proc.stdout or "").strip()
        return {"ok": False, "error": err or f"exit {proc.returncode}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Phone Link script timed out"}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def launch() -> dict:
    """Bring Phone Link to the foreground (or start it)."""
    return _run_script("launch.ps1")


def send_sms(recipient: str, message: str) -> dict:
    return _run_script("send-message.ps1", Recipient=recipient, MessageText=message)


def get_messages(contact: str | None = None) -> dict:
    args: dict = {}
    if contact:
        args["ConversationName"] = contact
    return _run_script("get-messages.ps1", **args)


def list_contacts() -> dict:
    """Best-effort list of real contact names to ground voice-transcribed
    recipients against. Combines two sources so a flaky scrape still yields
    something usable: recent conversation threads (via the tested get-messages
    scrape) and Phone Link's "Suggested contacts" (via list-contacts.ps1).
    Returns {"ok": bool, "contacts": [name, ...]} — app/notification rows are
    filtered out and names are cleaned of message-preview/timestamp noise."""
    from browser_agent.voice_correct import clean_contact_name, is_person

    def _accumulate(raw_names, seen, contacts):
        for entry in raw_names:
            name = clean_contact_name(str(entry))
            key = name.lower()
            if name and is_person(name) and key not in seen:
                seen.add(key)
                contacts.append(name)

    seen: set[str] = set()
    contacts: list[str] = []

    # Primary: the new-message compose view surfaces "Recent" + "Suggested
    # contacts" — the actual people you text (where Abby Nengel / Dad live).
    # It also scrapes the Notifications panel (Reddit, Uber Eats, …), which the
    # is_person denylist filters out.
    try:
        res = _run_script("list-contacts.ps1")
        _accumulate(res.get("contacts") or [], seen, contacts)
    except Exception:
        pass

    # Fallback/supplement: recent conversation threads via the get-messages
    # scrape. Noisier (mixes in the notifications feed), so only lean on it when
    # the compose scrape came up short.
    if len(contacts) < 3:
        try:
            gm = get_messages()
            rows = [(c.get("contact") if isinstance(c, dict) else c)
                    for c in (gm.get("conversations") or [])]
            _accumulate([r for r in rows if r], seen, contacts)
        except Exception:
            pass

    return {"ok": bool(contacts), "contacts": contacts}


def execute_goal(
    goal: str,
    parsed: dict,
    *,
    on_log: Callable[[str], None] | None = None,
    on_approve: Callable[[str, str], bool] | None = None,
) -> tuple[str, str]:
    """Run a parsed phone_link goal. Returns (result_text, status)."""
    log = on_log or (lambda s: print(s))
    approve = on_approve or (lambda _s, _d="": True)

    if not _enabled():
        return "Phone Link is disabled (QUILL_PHONE_LINK=0).", "phone_link_disabled"

    action = (parsed.get("action") or "open").lower()
    recipient = (parsed.get("recipient") or "").strip()
    message = (parsed.get("message") or "").strip()

    if action in ("open", "launch"):
        log("Opening Phone Link …")
        res = launch()
        if res.get("ok") or res.get("launched"):
            return "Phone Link is open.", "success"
        return f"Could not open Phone Link: {res.get('error', res)}", "failed"

    if action == "read_messages":
        log("Reading messages from Phone Link …")
        launch()
        res = get_messages(recipient or None)
        if res.get("error"):
            return f"Could not read messages: {res['error']}", "failed"
        convos = res.get("conversations") or res.get("messages") or res
        return f"Messages:\n{json.dumps(convos, indent=2)[:3000]}", "success"

    if action in ("send_sms", "text", "message", "reply"):
        if not recipient:
            who = _example_recipient()
            if who:
                hint = f"Who should I text? Try: 'text {who} that I'll be late'."
            else:
                hint = ("Who should I text? Say the person's name and what to "
                        "send, e.g. 'text <name> that I'll be late'.")
            return hint, "needs_details"
        if not message:
            # The user named a recipient but didn't say what to send. Ask — never
            # invent a body (that's how an unspoken message almost got sent).
            return (f"What would you like me to text {recipient}?"), "needs_details"
        # #11: annotate the recipient against the user's known vocabulary so the
        # human reviewer sees "✓ known contact" vs "⚠ not a recognized name"
        # before approving a send — a cheap mis-address guard. Display only; it
        # never changes the recipient or blocks the send (the human decides).
        recip_note = ""
        try:
            from app.services.vocabulary import vocabulary as _vocab
            rec = _vocab.recognize(recipient)
            if rec.get("known"):
                canon = rec.get("canonical") or recipient
                recip_note = (f"  (✓ known contact: {canon})"
                              if canon.lower() != recipient.lower()
                              else "  (✓ known contact)")
            else:
                recip_note = "  (⚠ not a recognized name — double-check)"
        except Exception:
            pass
        summary = f"Send SMS to {recipient}{recip_note}:\n{message}"
        # Show any voice-typo corrections (recipient grounding + body cleanup) so
        # the human confirms the *corrected* text/recipient, never a silent guess.
        corrections = parsed.get("_corrections") or []
        if corrections:
            summary = ("Voice corrections applied:\n  - "
                       + "\n  - ".join(corrections) + "\n\n" + summary)
        if not approve(f"Send this text message via Phone Link?", summary):
            return "SMS send cancelled.", "cancelled"
        log(f"Sending SMS to {recipient} via Phone Link …")
        launch()
        res = send_sms(recipient, message)
        if res.get("sent"):
            return f"Sent text to {recipient}.", "success"
        return f"SMS failed: {res.get('error', res)}", "failed"

    return f"Unknown phone action: {action}", "failed"
