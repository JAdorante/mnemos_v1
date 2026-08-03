"""Reproduce audit bugs C1/H1/H2/M1/M2/M3 and write NDJSON debug logs."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOG = ROOT / "debug-2e9950.log"
os.environ.setdefault("QUILL_DESKTOP_JAIL", tempfile.mkdtemp(prefix="quill_jail_"))


def log(hypothesis_id: str, location: str, message: str, data: dict, run_id: str) -> None:
    row = {
        "sessionId": "2e9950",
        "runId": run_id,
        "hypothesisId": hypothesis_id,
        "location": location,
        "message": message,
        "data": data,
        "timestamp": int(time.time() * 1000),
    }
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")


def main(run_id: str) -> None:
    # --- C1: outbox queue path is exempt for POST ---
    from app.services import api_auth
    post_exempt = api_auth.path_is_exempt("/phone/outbox/queue", "POST")
    get_exempt = api_auth.path_is_exempt("/phone/outbox", "GET")
    log("C1", "api_auth.path_is_exempt", "outbox exemption by method", {
        "post_queue_exempt": post_exempt,
        "get_outbox_exempt": get_exempt,
        "fixed_if": (not post_exempt) and get_exempt,
    }, run_id)

    # --- H1: credential newline injection ---
    from browser_agent import credentials as creds
    tmp = Path(tempfile.mkdtemp(prefix="cred_inj_"))
    cred_path = tmp / ".credentials.env"
    orig = creds.credentials_path
    creds.credentials_path = lambda: cred_path  # type: ignore
    try:
        injected_pw = "secret\nQUILL_API_TOKEN=injected-token-from-pw\nX=1"
        try:
            creds.save("evil.example", "user", injected_pw)
            save_ok = True
            save_err = None
        except Exception as exc:
            save_ok = False
            save_err = type(exc).__name__ + ":" + str(exc)
        text = cred_path.read_text(encoding="utf-8") if cred_path.is_file() else ""
        has_injected = "QUILL_API_TOKEN=injected-token-from-pw" in text
        log("H1", "credentials.save", "newline credential write", {
            "save_ok": save_ok,
            "save_err": save_err,
            "has_injected_token_line": has_injected,
            "fixed_if": (not save_ok) and (not has_injected),
        }, run_id)
    finally:
        creds.credentials_path = orig  # type: ignore

    # --- H2: atomic save + corrupt load still empty (load behavior unchanged) ---
    from app.services import phone_channel as pc
    from app.atomic_json import write_json
    import inspect
    dtmp = Path(tempfile.mkdtemp(prefix="phone_json_"))
    devices = dtmp / "devices.json"
    os.environ["QUILL_PHONE_DEVICES"] = str(devices)
    write_json(devices, {"dev1": {"name": "phone", "token_sha256": "x" * 64}})
    loaded_ok = pc._load_devices()
    src = inspect.getsource(pc._save_devices)
    # Truncated file still returns {} (fail-soft load); atomic write is the fix.
    devices.write_text('{"dev1": {"name": "phone"', encoding="utf-8")
    loaded_bad = pc._load_devices()
    log("H2", "phone_channel._save_devices", "atomic save + corrupt load", {
        "atomic_save_loaded_keys": list(loaded_ok.keys()),
        "uses_write_json": "write_json" in src or "atomic_json" in src,
        "corrupt_still_empty": loaded_bad == {},
        "fixed_if": ("write_json" in src or "atomic_json" in src)
                    and list(loaded_ok.keys()) == ["dev1"],
    }, run_id)

    # --- M1: wizard kind rejected ---
    from app.services.name_quality import normalize_entity_kind
    from app.storage import Store
    nk_default = normalize_entity_kind("wizard")
    nk_strict = normalize_entity_kind("wizard", unknown=None)
    st = Store(db_path=dtmp / "t.db", audio_dir=dtmp / "audio")
    eid = st.resolve_entity("Junk Thing", kind="idea", ts=1.0)
    ok = st.set_entity_kind(eid, "wizard")
    log("M1", "set_entity_kind", "invalid kind handling", {
        "normalize_wizard_default": nk_default,
        "normalize_wizard_strict": nk_strict,
        "set_entity_kind_ok": ok,
        "stored_kind": (st.get_entity(eid) or {}).get("kind"),
        "fixed_if": (nk_strict is None) and (ok is False),
    }, run_id)

    # --- M2: corrupt payload fails the job ---
    from app.services.worker import JobWorker
    seen = {"called": False}

    def handler(payload):
        seen["called"] = True

    w = JobWorker(store=st)
    w.register("audit_probe", handler)
    jid = st.enqueue_job("audit_probe", payload="{not-json")
    job = {"id": jid, "kind": "audit_probe", "payload": "{not-json", "attempts": 0}
    w._dispatch(job)
    row = st._conn.execute("SELECT status, error FROM jobs WHERE id=?",
                           (jid,)).fetchone()
    status = row["status"] if row else None
    err = row["error"] if row else None
    log("M2", "worker._dispatch", "corrupt payload handling", {
        "handler_called": seen["called"],
        "job_status": status,
        "last_error": err,
        "fixed_if": (not seen["called"]) and bool(err and "corrupt" in str(err).lower()),
    }, run_id)

    # --- M3: dismiss kind set ---
    from app.services.agent_bridge import AgentWorker
    src = inspect.getsource(AgentWorker._dismiss_offer)
    records_reasoner = 'startswith("reasoner_")' in src or "reasoner_" in src
    log("M3", "agent_bridge._dismiss_offer", "reasoner dismiss recording", {
        "records_reasoner_prefix": records_reasoner,
        "fixed_if": records_reasoner,
    }, run_id)

    print(f"wrote {LOG} runId={run_id}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "post-fix")
