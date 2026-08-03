"""Reflection v1 test — schema, grounding, review/convert, HTTP surface.

Self-contained: runs against a throwaway DB dir and STUBS the ModelRouter, so
there's no paid LLM call and no mic/camera needed. Never touches data/quill.db.

    python -u scripts/test_reflection.py

Exit code 0 = all pass. The HTTP block is best-effort: if importing the full app
pulls a dep that's missing in this env, it's skipped (the core logic still runs).
"""
import os, sys, time, tempfile

# Isolate BEFORE importing app.config (settings are frozen at import time).
SCRATCH = tempfile.mkdtemp(prefix="quill_reflect_test_")
os.environ.update({
    "QUILL_DATA_DIR": SCRATCH, "QUILL_WORKER": "0", "QUILL_AGENT": "0",
    "QUILL_VISION": "0", "QUILL_SEMANTIC": "0", "QUILL_EXTRACT": "0",
    "QUILL_REFLECT": "0",
})
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.storage import Store
from app.services import model_router
from app.services.reflector import Reflector

FAILS = []
def check(name, cond):
    print(("  ok   " if cond else "  FAIL ") + name)
    if not cond:
        FAILS.append(name)


def test_core():
    print("[core] schema / grounding / review / convert")
    db = os.path.join(SCRATCH, "core.db")
    store = Store(db_path=db, audio_dir=os.path.join(SCRATCH, "audio"))
    now = time.time()

    tabs = {r["name"] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    check("reflections + reflection_items tables created",
          {"reflections", "reflection_items"} <= tabs)

    t1 = store.add_task("Book the venue", confidence=0.9, extracted_at=now)
    c1 = store.add_commitment("Send Chris the deck", source_span="I'll send Chris the deck",
                              confidence=0.8, extracted_at=now)
    store.add_claim("The demo is Monday", confidence=0.7, extracted_at=now)

    # Stub the LLM: cite one real id + one INVENTED id (99999) + a bad kind.
    model_router.router.complete_json = lambda *a, **k: {
        "summary": "Fundraising prep; a pricing follow-up is the open loop.",
        "confidence": 0.83,
        "items": [
            {"kind": "open_loop", "text": "Pricing deck for Chris is unsent",
             "detail": "Draft a follow-up", "subject": "Chris", "confidence": 0.8,
             "source_fact_ids": [c1, 99999]},
            {"kind": "recommendation", "text": "Prepare a vinceo.ai status memo",
             "detail": "", "subject": "vinceo.ai", "confidence": 0.6, "source_fact_ids": [t1]},
            {"kind": "bogus_kind", "text": "coerced to change", "detail": "",
             "subject": "", "confidence": 0.5, "source_fact_ids": []},
        ],
    }

    refl = Reflector(store=store)
    res = refl.reflect_daily()
    check("reflection persisted with 3 items",
          bool(res.get("reflection_id")) and res.get("items") == 3)

    items = {i["text"]: i for i in store.reflection_items(res["reflection_id"])}
    ol = items.get("Pricing deck for Chris is unsent", {})
    check("grounding drops invented id 99999, keeps the real one",
          ol.get("source_fact_ids") == [c1])
    check("unknown kind coerced to 'change'",
          items.get("coerced to change", {}).get("kind") == "change")

    rec = items["Prepare a vinceo.ai status memo"]
    store.edit_reflection_item_text(rec["id"], "Prepare a vinceo.ai board memo")
    edited = store.get_reflection_item(rec["id"])
    check("edit updates text + marks 'edited'",
          edited["text"] == "Prepare a vinceo.ai board memo" and edited["review"] == "edited")

    before = len(store.open_tasks())
    fid = store.add_task(edited["text"], source_span=edited["text"],
                         confidence=edited["confidence"], extracted_at=time.time())
    store.set_reflection_item_converted(rec["id"], fid)
    conv = store.get_reflection_item(rec["id"])
    check("convert links task + creates a real open task",
          conv["converted_fact_id"] == fid and len(store.open_tasks()) == before + 1)

    # Empty period must skip WITHOUT an LLM call.
    store2 = Store(db_path=os.path.join(SCRATCH, "empty.db"),
                   audio_dir=os.path.join(SCRATCH, "audio2"))
    model_router.router.complete_json = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("LLM must not be called on an empty period"))
    res2 = Reflector(store=store2).reflect_daily()
    check("empty period skips without calling the LLM", res2.get("reflection_id") is None)
    check("due_for True when none exists, False right after one",
          Reflector(store=store2).due_for("daily") and not refl.due_for("daily"))


def test_http():
    print("[http] routes + console (best-effort)")
    try:
        from fastapi.testclient import TestClient
        from app.storage import get_store
        from app.main import app
    except Exception as exc:
        print(f"  SKIP (heavy import unavailable): {exc!r}")
        return

    store = get_store()
    now = time.time()
    t1 = store.add_task("Book the venue", confidence=0.9, extracted_at=now)
    c1 = store.add_commitment("Send Chris the deck", source_span="I'll send Chris the deck",
                              confidence=0.8, extracted_at=now)
    model_router.router.complete_json = lambda *a, **k: {
        "summary": "Venue + pricing follow-up are live.", "confidence": 0.8,
        "items": [
            {"kind": "open_loop", "text": "Pricing deck unsent", "detail": "Follow up",
             "subject": "Chris", "confidence": 0.8, "source_fact_ids": [c1]},
            {"kind": "recommendation", "text": "Confirm the venue booking", "detail": "",
             "subject": "", "confidence": 0.7, "source_fact_ids": [t1]},
        ],
    }
    with TestClient(app) as client:
        r = client.post("/reflect/run?scope=daily")
        check("POST /reflect/run -> 200 with a reflection",
              r.status_code == 200 and bool(r.json().get("reflection")))
        latest = client.get("/reflections?scope=daily").json().get("reflection", {})
        check("GET /reflections hydrates 2 insights + evidence",
              len(latest.get("items", [])) == 2
              and any(i["evidence"] for i in latest["items"]))
        rid = next(i["id"] for i in latest["items"] if i["kind"] == "recommendation")
        check("approve + convert -> 200, task created",
              client.post(f"/reflection_items/{rid}/approve").status_code == 200
              and client.post(f"/reflection_items/{rid}/convert").status_code == 200)
        check("double-convert rejected (409)",
              client.post(f"/reflection_items/{rid}/convert").status_code == 409)
        html = client.get("/console").text
        check("console renders the Reflection view",
              "🧠 Reflection" in html and "loadReflect" in html)


if __name__ == "__main__":
    test_core()
    test_http()
    print()
    print("RESULT:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
