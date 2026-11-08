"""Seed a temporary Sparrow store, run the app, and screenshot the shell.

Frontend spec acceptance helper: both themes at desktop and phone width,
no horizontal scroll, no page errors, and one inbox decision acted on and
undone in a real browser. Nothing touches the real data dir.

  python3 scripts/ui_screenshots.py            # writes build/ui-shots/*.png
"""
import json, os, pathlib, socket, subprocess, sys, tempfile, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "build" / "ui-shots"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT))
tmp = pathlib.Path(tempfile.mkdtemp(prefix="sparrow-shots-"))
os.environ["QUILL_DATA_DIR"] = str(tmp)
os.environ["QUILL_HEADS"] = "off"
os.environ["QUILL_AGENT"] = "0"

from app.storage import Store
from app.events import Event, Modality

store = Store(db_path=tmp / "quill.db", audio_dir=tmp / "audio")
now = time.time()
def ev(raw, mod=Modality.AUDIO, src="audio.whisper", dt=0):
    return store.insert(Event(time=now - dt, modality=mod, raw=raw, summary=raw[:60], source=src, people=["Dana"]))
e1 = ev("Dana said the Atlas rollout moves to December twelfth because the vendor slipped.", dt=3600 * 5)
e2 = ev("Priya: we agreed the launch review is Thursday at ten with the platform team.", dt=3600 * 30)
e3 = ev("Reminder to send the revised budget to Marcus before Friday.", Modality.TEXT, "chat", dt=3600 * 50)
f1 = store.add_claim("Atlas rollout moves to December 12 because the vendor slipped.", source_event_id=e1,
                     source_span="the Atlas rollout moves to December twelfth because the vendor slipped", confidence=0.82, extracted_at=now - 3600 * 5)
f2 = store.add_claim("Launch review is Thursday at 10 with the platform team.", source_event_id=e2,
                     source_span="the launch review is Thursday at ten with the platform team", confidence=0.74, extracted_at=now - 3600 * 30)
f3 = store.add_claim("Send the revised budget to Marcus before Friday.", source_event_id=e3,
                     source_span="", confidence=0.9, extracted_at=now - 3600 * 50)
f4 = store.add_claim("Dana prefers the Tuesday standup moved to 9:30.", source_event_id=e1,
                     source_span="", confidence=0.6, extracted_at=now - 3600 * 80)
store.review_fact(f4, "approved")
eid = store.resolve_entity("Atlas", "project")
store.upsert_entity_alias(eid, "Atlas rollout", "atlas rollout", source="email", day="2026-11-05")
store.upsert_entity_alias(eid, "Project Atlas", "project atlas", source="meeting", day="2026-11-04")
pid = store.resolve_entity("Platform team", "team")
store.upsert_entity_alias(pid, "platform group", "platform group", source="chat", day="2026-11-06")
store.close()
from app.services import capture_consent
capture_consent.save({"mic": False, "webcam": False, "screen": False, "system_audio": False}, consented=True)
print("seeded", tmp, "consent:", capture_consent.status().get("consented"))

def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p
port = free_port(); base = f"http://127.0.0.1:{port}"
env = os.environ.copy(); env["QUILL_PORT"] = str(port)
proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
                        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
ok = False
for _ in range(120):
    try:
        urllib.request.urlopen(base + "/health", timeout=2); ok = True; break
    except Exception:
        if proc.poll() is not None: break
        time.sleep(0.5)
if not ok:
    print("server failed", (proc.stderr.read() or b"")[-2000:].decode("utf-8", "replace")); proc.kill(); sys.exit(1)
print("up", base)

from playwright.sync_api import sync_playwright
PAGES = ["/inbox", "/today", "/chat", "/memory", "/settings", "/profile", "/inspect", "/peer"]
errors = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    for theme in ("dark", "light"):
        for vw, vh, tag in ((1440, 1000, "desktop"), (390, 844, "phone")):
            ctx = browser.new_context(viewport={"width": vw, "height": vh}, device_scale_factor=1)
            ctx.add_init_script(f"localStorage.setItem('mnemos.ui.theme', JSON.stringify('{theme}'))")
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(f"{theme}/{tag} pageerror: {e}"))
            page.on("console", lambda m: errors.append(f"{theme}/{tag} console {m.type}: {m.text}") if m.type == "error" else None)
            for path in (PAGES if tag == "desktop" else ["/inbox", "/today", "/chat"]):
                page.goto(base + path, wait_until="load", timeout=30000)
                page.wait_for_timeout(600)
                name = OUT / f"{theme}-{tag}-{path.strip('/').replace('/', '_') or 'root'}.png"
                page.screenshot(path=str(name), full_page=False)
                hs = page.evaluate("document.documentElement.scrollWidth > document.documentElement.clientWidth")
                print(f"{theme:5} {tag:7} {path:10} hscroll={hs} -> {name.name}")
            if tag == "phone":
                page.goto(base + "/inbox", wait_until="load")
                page.click("#sparrowMenu"); page.wait_for_timeout(400)
                page.screenshot(path=str(OUT / f"{theme}-phone-inbox-menu.png"))
            ctx.close()
    # interaction: act + undo on the first alias row (dark desktop)
    ctx = browser.new_context(viewport={"width": 1440, "height": 1000})
    page = ctx.new_page()
    page.on("pageerror", lambda e: errors.append(f"interact pageerror: {e}"))
    page.goto(base + "/inbox", wait_until="load")
    row = page.locator(".inbox-row[data-kind='confirm']").first
    rid = row.get_attribute("data-id")
    row.locator("button[data-action='yes']").click()
    page.wait_for_selector(f".inbox-row[data-id='{rid}'].done", timeout=5000)
    done_text = page.locator(f".inbox-row[data-id='{rid}'] .ir-done").inner_text()
    page.screenshot(path=str(OUT / "interact-done.png"))
    page.locator(f".inbox-row[data-id='{rid}'] [data-undo]").click()
    page.wait_for_timeout(600)
    restored = page.locator(f".inbox-row[data-id='{rid}']:not(.done)").count()
    print("act ->", done_text.strip(), "| undo restored:", restored)
    # keyboard: focus first row, press Enter on the primary action of a review row
    page.keyboard.press("Tab")
    first = page.locator(".inbox-row[tabindex='0']").first
    first.focus(); page.keyboard.press("2")   # second action
    page.wait_for_timeout(800)
    print("keyboard 2 ->", page.locator(".inbox-row.done").count(), "row(s) done")
    page.screenshot(path=str(OUT / "interact-keyboard.png"))
    ctx.close()
    browser.close()
proc.terminate()
print("errors:", json.dumps(errors, indent=1) if errors else "none")
