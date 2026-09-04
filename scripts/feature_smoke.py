#!/usr/bin/env python
"""Full-system feature smoke — tiered orchestrator.

Exercises Sparrow the way a tester would: seed memory, hit APIs, load UI pages,
and (optionally) run deeper eval harnesses.

    python scripts/feature_smoke.py              # Tier 1: API (~30s, no API key)
    python scripts/feature_smoke.py --ui         # + Playwright UI pages
    python scripts/feature_smoke.py --live       # + agent routing eval (API key)
    python scripts/feature_smoke.py --all        # standard tiers (api + ui + live)
    python scripts/feature_smoke.py --deep       # + capture, browser live, goldens
    python scripts/feature_smoke.py --everything # --all --deep
    python scripts/feature_smoke.py --staging     # read-only checks on ./data
    python scripts/feature_smoke.py --json out.json

Exit 0 when every non-skipped check passes, 1 otherwise.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import warnings
import zipfile
from dataclasses import asdict, dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Check:
    name: str
    tier: str
    status: str  # pass | fail | skip
    detail: str = ""


@dataclass
class Report:
    scratch: str
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, tier: str, ok: bool | None, detail: str = "") -> None:
        if ok is None:
            status = "skip"
        elif ok:
            status = "pass"
        else:
            status = "fail"
        self.checks.append(Check(name, tier, status, detail))

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    def exit_code(self) -> int:
        return 1 if self.failed else 0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with request.urlopen(url, timeout=1.5) as resp:
                if 200 <= resp.status < 300:
                    return True
        except (error.URLError, TimeoutError, OSError):
            time.sleep(0.15)
    return False


def _smoke_env(scratch: str) -> dict[str, str]:
    """Hermetic flags: no network checks, no agent worker, no LLM required."""
    return {
        "QUILL_DATA_DIR": scratch,
        "QUILL_AGENT": "0",
        "QUILL_WORKER": "0",
        "QUILL_UPDATE_CHECK": "0",
        "QUILL_USAGE_LEDGER": "1",
        "QUILL_SEMANTIC": "0",
        "QUILL_EXTRACT": "0",
        "QUILL_REFLECT": "0",
        "QUILL_AUTOSTART": "0",
        "QUILL_TEXT_LOCAL": "0",
        "QUILL_HOST": "127.0.0.1",
        "QUILL_MEETING_SESSION": "0",
        "QUILL_ICLOUD_SYNC": "0",
    }


@contextlib.contextmanager
def _quiet(enabled: bool):
    """Silence app startup chatter; the report is the output."""
    if not enabled:
        yield
        return
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf_out), contextlib.redirect_stderr(buf_err):
            yield


def _playwright_install_cmd() -> str:
    return f"{sys.executable} -m playwright install chromium"


def _apply_env(extra: dict[str, str]) -> None:
    os.environ.update(extra)


def _csrf_headers(client, api_auth) -> dict[str, str]:
    client.get("/auth/status")
    token = client.cookies.get(api_auth.CSRF_COOKIE)
    return {api_auth.CSRF_HEADER: token} if token else {}


def _seed_store(store) -> dict:
    from app.events import Event, Modality

    ids = []
    for i in range(8):
        ids.append(store.insert(Event(
            time=1_756_000_000.0 + i,
            modality=Modality.AUDIO,
            raw=f"smoke utterance {i} about Venture Pulse pricing",
            summary=f"summary {i}",
            source="feature_smoke",
        )))
    person_id = store.insert_person("Justin Marsh", ts=1_756_000_000.0)
    store.add_task("Follow up on Venture Pulse pricing", confidence=0.9,
                   extracted_at=1_756_000_100.0)
    return {"event_ids": ids, "person_id": person_id}


def _load_module(name: str, path: Path):
    import importlib.util
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def run_tier1(report: Report, scratch: str, *, quiet: bool = True,
              read_only: bool = False, seed: bool = True) -> None:
    tier = "api"
    _apply_env(_smoke_env(scratch))
    sys.path.insert(0, str(ROOT))

    with _quiet(quiet):
        try:
            from fastapi.testclient import TestClient
            from app.main import app
            from app.services import api_auth
            from app.storage import get_store
        except Exception as exc:
            report.add("import_app", tier, False, repr(exc))
            return

        def check(name: str, fn: Callable[[], bool], detail: str = "") -> None:
            try:
                ok = fn()
                report.add(name, tier, ok, detail)
            except Exception as exc:
                report.add(name, tier, False, repr(exc))

        store = get_store()
        seeded = {"event_ids": [], "person_id": None}
        if seed:
            seeded = _seed_store(store)

        with TestClient(app) as client:
            csrf = lambda: _csrf_headers(client, api_auth)

            check("health", lambda: (
                (r := client.get("/health")).status_code == 200
                and r.json().get("status") == "ok"
                and bool(r.json().get("version"))
            ))

            if read_only:
                check("capture_consent_status", lambda: (
                    client.get("/capture/consent").status_code == 200
                ))
            else:
                check("capture_consent_default_off", lambda: (
                    not client.get("/capture/consent").json().get("consented")
                ))

            def _consent_roundtrip() -> bool:
                r = client.post("/capture/consent",
                                json={"consented": True, "save_audio": True},
                                headers=csrf())
                if r.status_code != 200:
                    return False
                st = r.json().get("consent") or {}
                return bool(st.get("consented")) and st.get("sources", {}).get("save_audio")

            if not read_only:
                check("capture_consent_save", _consent_roundtrip)
            else:
                report.add("capture_consent_save", tier, None, "read-only staging")

            check("capture_status", lambda: client.get("/capture/status").status_code == 200)

            if seed:
                check("memory_events", lambda: (
                    (r := client.get("/memory/events")).status_code == 200
                    and len(r.json().get("events") or []) >= len(seeded["event_ids"])
                ))
            else:
                check("memory_events", lambda: (
                    client.get("/memory/events").status_code == 200
                ))

            check("memory_search", lambda: (
                (r := client.get("/memory/search", params={"q": "Venture Pulse"})).status_code == 200
                and len(r.json().get("results") or []) >= 1
            ))

            check("memory_html", lambda: (
                "Memory Console" in client.get("/memory").text
            ))

            check("chat_html", lambda: (
                "Chat" in client.get("/chat").text
            ))

            check("today_html", lambda: (
                client.get("/today").status_code == 200
            ))

            check("profile_data", lambda: (
                (r := client.get("/profile/data")).status_code == 200
                and r.json().get("ok") is not False
            ))

            if seed and seeded["person_id"] is not None:
                check("people_list", lambda: (
                    (r := client.get("/people/list")).status_code == 200
                    and any(p.get("id") == seeded["person_id"]
                            for p in (r.json().get("people") or []))
                ))
            else:
                check("people_list", lambda: (
                    client.get("/people/list").status_code == 200
                ))

            check("graph_stats", lambda: client.get("/graph/stats").status_code == 200)

            if not read_only:
                def _graph_rebuild() -> bool:
                    r = client.post("/graph/rebuild", headers=csrf())
                    return r.status_code == 200 and r.json().get("ok") is not False

                check("graph_rebuild", _graph_rebuild)
            else:
                report.add("graph_rebuild", tier, None, "read-only staging")

            if not read_only:
                check("chat_memory_only", lambda: (
                    (r := client.post("/chat", json={"message": "What is Venture Pulse?"},
                                      headers=csrf())).status_code == 200
                    and bool(r.json().get("answer"))
                ))
            else:
                report.add("chat_memory_only", tier, None, "read-only staging")

            check("chat_mode", lambda: (
                (r := client.get("/chat/mode")).status_code == 200
                and bool(r.json().get("id"))
            ))

            check("export_status", lambda: client.get("/export/status").status_code == 200)

            def _backup_zip() -> bool:
                r = client.get("/export/backup")
                if r.status_code != 200:
                    return False
                data = r.content
                if not data.startswith(b"PK"):
                    return False
                with zipfile.ZipFile(BytesIO(data)) as zf:
                    return "manifest.json" in zf.namelist()

            def _takeout_zip() -> bool:
                r = client.get("/export/takeout")
                if r.status_code != 200:
                    return False
                with zipfile.ZipFile(BytesIO(r.content)) as zf:
                    names = zf.namelist()
                    return any(n.endswith(".jsonl") for n in names) and "README.txt" in names

            if not read_only:
                check("export_backup", _backup_zip)
            else:
                report.add("export_backup", tier, None, "read-only staging")

            check("export_takeout", _takeout_zip)

            check("usage_stats", lambda: (
                (r := client.get("/usage/stats")).status_code == 200
                and bool((r.json().get("metrics") or {}).get("install_id"))
            ))

            check("usage_preview", lambda: (
                (r := client.get("/usage/preview")).status_code == 200
                and bool(r.json().get("payload"))
            ))

            check("update_status", lambda: client.get("/update/status").status_code == 200)

            check("onboarding_status", lambda: (
                client.get("/onboarding/status").status_code == 200
            ))

            check("welcome_status", lambda: (
                client.get("/welcome/status").status_code == 200
            ))

            check("first_run_status", lambda: (
                client.get("/first-run/status").status_code == 200
            ))

            check("console_readiness", lambda: (
                client.get("/console/readiness").status_code == 200
            ))

            check("help_trust", lambda: (
                "trust" in client.get("/help/trust").text.lower()
            ))

            check("help_mcp", lambda: (
                client.get("/help/mcp").status_code == 200
            ))


def _kill_proc(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_tier2(report: Report, scratch: str, port: int | None = None) -> None:
    tier = "ui"
    port = port or _free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(_smoke_env(scratch))

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        if not _wait_health(f"{base}/health"):
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            report.add("server_start", tier, False, err[-500:] or "health timeout")
            return
        report.add("server_start", tier, True)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            report.add("playwright_import", tier, None, "pip install playwright")
            return

        pages = [
            ("memory_page", "/memory", ["Memory Console", "Reflection"]),
            ("chat_page", "/chat", ["Chat"]),
            ("profile_page", "/profile", ["Profile"]),
            ("today_page", "/today", []),
            ("shell_page", "/shell", []),
            ("meetings_page", "/meetings", ["Meetings"]),
            ("peer_page", "/peer", ["Team"]),
            ("phone_page", "/phone", ["Connect a phone"]),
            ("onboarding_page", "/onboarding", ["Setup"]),
            ("welcome_page", "/welcome", []),
            ("desktop_page", "/desktop-access", ["Desktop"]),
            ("org_network_page", "/org-network", ["Org Network"]),
        ]

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page()
                for name, path, needles in pages:
                    try:
                        resp = page.goto(f"{base}{path}", wait_until="domcontentloaded",
                                         timeout=20_000)
                        ok = resp is not None and resp.ok
                        text = page.content()
                        if needles:
                            ok = ok and all(n in text for n in needles)
                        report.add(name, tier, ok,
                                   f"status={resp.status if resp else 'none'}")
                    except Exception as exc:
                        report.add(name, tier, False, repr(exc))
                browser.close()
        except Exception as exc:
            msg = str(exc)
            if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                report.add("playwright_browser", tier, None,
                           f"run: {_playwright_install_cmd()}")
            else:
                report.add("playwright_browser", tier, False, msg[:240])
    finally:
        _kill_proc(proc)


def run_tier3(report: Report, *, quiet: bool = True) -> None:
    tier = "live"
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        report.add("agent_routing", tier, None, "ANTHROPIC_API_KEY not set")
        return

    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass

    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        report.add("agent_routing", tier, None, "ANTHROPIC_API_KEY not set")
        return

    sys.path.insert(0, str(ROOT))
    try:
        mod = _load_module("eval_agent", ROOT / "scripts" / "eval_agent.py")
        run_routing = mod.run_routing
    except Exception as exc:
        report.add("agent_routing", tier, False, f"import eval_agent: {exc}")
        return

    try:
        with _quiet(quiet):
            result = run_routing()
    except Exception as exc:
        report.add("agent_routing", tier, False, repr(exc))
        return

    agg = result.get("aggregate") or {}
    fns = int(agg.get("approval_false_negatives") or 0)
    acc = float(agg.get("approval_acc") or 0)
    ok = fns == 0 and acc >= 0.85
    detail = (f"approval_acc={acc:.0%} "
              f"approval_FNs={fns} browser_acc={agg.get('browser_acc', 0):.0%}")
    report.add("agent_routing", tier, ok, detail)


def run_tier_staging_people(report: Report, scratch: str) -> None:
    tier = "staging"
    sys.path.insert(0, str(ROOT))
    try:
        from app.storage import Store
        from app.services import people_pipeline as pp
    except Exception as exc:
        report.add("people_live", tier, False, repr(exc))
        return

    if not pp.enabled():
        report.add("people_live", tier, None, "QUILL_PEOPLE_V2 off")
        return

    db = Path(scratch) / "quill.db"
    if not db.is_file():
        report.add("people_live", tier, None, "no quill.db")
        return

    try:
        store = Store(db_path=db, audio_dir=Path(scratch) / "audio")
        people = store.all_people() or []
        errors = 0
        scored = 0
        for p in people[:40]:
            name = (p.get("name") or "").strip()
            if not name:
                continue
            scored += 1
            try:
                hits = pp.score_person_candidates(name, people)
                pp.decide_from_scores(hits, relationship_boost=0.6,
                                      create_person_candidates=True)
            except Exception:
                errors += 1
        ok = scored > 0 and errors == 0
        detail = f"scored={scored} errors={errors} roster={len(people)}"
        report.add("people_live", tier, ok if scored else None, detail)
    except Exception as exc:
        report.add("people_live", tier, False, repr(exc))


def run_tier4_capture(report: Report, scratch: str, *, quiet: bool = True,
                      voice_limit: int = 5) -> None:
    tier = "capture"
    manifest = ROOT / "data" / "eval" / "manifest.jsonl"

    # --- fixture audio pipeline (eval_voice) ---------------------------------
    if not manifest.is_file():
        report.add("voice_pipeline", tier, None,
                   "run: python scripts/eval_voice.py build")
    else:
        try:
            mod = _load_module("eval_voice", ROOT / "scripts" / "eval_voice.py")
            entries = mod._load_manifest()
            with _quiet(quiet):
                result = mod.evaluate(entries, limit=voice_limit)
            overall = result.get("overall") or {}
            n = int(overall.get("n") or 0)
            fkr = overall.get("false_keep_rate")
            ok = n >= min(3, voice_limit)
            if fkr is not None:
                ok = ok and float(fkr) <= 0.34
            detail = f"n={n} false_keep={fkr} wer_true={overall.get('wer_true')}"
            report.add("voice_pipeline", tier, ok, detail)
        except Exception as exc:
            report.add("voice_pipeline", tier, False, repr(exc))

    # --- external transcript inject (no mic) ---------------------------------
    phone_dir = Path(scratch) / "phone_smoke"
    phone_dir.mkdir(parents=True, exist_ok=True)
    prev = {k: os.environ.get(k) for k in
            ("QUILL_DATA_DIR", "QUILL_EXTERNAL_CAPTURE", "QUILL_PHONE_DEVICES")}
    try:
        os.environ.update({
            "QUILL_DATA_DIR": scratch,
            "QUILL_EXTERNAL_CAPTURE": "1",
            "QUILL_PHONE_DEVICES": str(phone_dir / "devices.json"),
        })
        from app.services import phone_channel as pc
        from app.services import external_capture as ec

        pc._pairing = None
        start = pc.start_pairing()
        claim = pc.claim_pairing(start["code"], "Smoke Mic", "external")
        token = claim.get("token") or ""
        device = ec.authenticate(f"Bearer {token}")
        out = ec.ingest_transcript(device or {}, {
            "text": "smoke test follow up with Venture Pulse",
            "started_at": 1_756_000_000.0,
            "ended_at": 1_756_000_008.0,
            "kind": "external",
        })
        ok = bool(out.get("ok")) and bool(out.get("never_authorizes"))
        report.add("external_capture", tier, ok,
                   "never_authorizes" if ok else str(out))
    except Exception as exc:
        report.add("external_capture", tier, False, repr(exc))
    finally:
        for k, v in prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def run_tier5_browser(report: Report, *, quiet: bool = True) -> None:
    tier = "browser"
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
            key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
        except Exception:
            pass
    if not key:
        report.add("agent_live", tier, None, "ANTHROPIC_API_KEY not set")
        return

    sys.path.insert(0, str(ROOT))
    try:
        mod = _load_module("eval_agent", ROOT / "scripts" / "eval_agent.py")
        with _quiet(quiet):
            result = mod.run_live(headed=False)
    except Exception as exc:
        msg = str(exc)
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            report.add("agent_live", tier, None, f"run: {_playwright_install_cmd()}")
        else:
            report.add("agent_live", tier, False, repr(exc))
        return

    agg = result.get("aggregate") or {}
    rate = float(agg.get("success_rate") or 0)
    interv = int(agg.get("total_interventions") or 0)
    ok = rate >= 0.5 and interv == 0
    detail = f"success={rate:.0%} interventions={interv} cost=${agg.get('total_cost', 0)}"
    report.add("agent_live", tier, ok, detail)


def run_tier6_quality(report: Report, *, quiet: bool = True) -> None:
    tier = "quality"
    scripts = [
        ("grounding", ROOT / "scripts" / "eval_grounding.py", []),
        ("extraction", ROOT / "scripts" / "eval_extraction.py", ["--limit", "5"],
         ROOT / "data" / "bench" / "extraction" / "golden.jsonl"),
    ]
    for item in scripts:
        name, script, extra = item[0], item[1], item[2]
        golden = item[3] if len(item) > 3 else None
        if golden is not None and not golden.is_file():
            report.add(name, tier, None, f"missing {golden.name}")
            continue
        cmd = [sys.executable, str(script), *extra]
        try:
            proc = subprocess.run(
                cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
            ok = proc.returncode == 0
            tail = (proc.stdout or proc.stderr or "").strip().splitlines()
            detail = tail[-1][:200] if tail else f"exit {proc.returncode}"
            if not quiet and proc.stdout:
                print(proc.stdout)
            report.add(name, tier, ok, detail)
        except subprocess.TimeoutExpired:
            report.add(name, tier, False, "timeout")
        except Exception as exc:
            report.add(name, tier, False, repr(exc))


def run_tier7_network(report: Report) -> None:
    tier = "network"

    # --- invite redeem (mock transport) --------------------------------------
    cred_dir = Path(tempfile.mkdtemp(prefix="quill_inv_smoke_"))
    cred_path = cred_dir / ".credentials.env"
    prev_cred = os.environ.get("QUILL_CREDENTIALS_FILE")
    try:
        os.environ["QUILL_CREDENTIALS_FILE"] = str(cred_path)
        from app.services import invite

        out = invite.redeem_and_save(
            "ABCD-EFGH-JKLM",
            url="https://smoke.invalid/redeem",
            transport=lambda *a: {"provider": "anthropic", "key": "sk-ant-smoke-test"},
        )
        ok = (out.get("ok")
              and cred_path.is_file()
              and "sk-ant-smoke-test" in cred_path.read_text(encoding="utf-8"))
        report.add("invite_redeem", tier, ok, "mock transport")
    except Exception as exc:
        report.add("invite_redeem", tier, False, repr(exc))
    finally:
        if prev_cred is None:
            os.environ.pop("QUILL_CREDENTIALS_FILE", None)
        else:
            os.environ["QUILL_CREDENTIALS_FILE"] = prev_cred

    # --- update manifest check (mock transport) ------------------------------
    try:
        from unittest.mock import patch
        from app.services import update_check as uc
        from app.version import __version__

        manifest = {"latest": "99.0.0", "url": "https://example.invalid/z.zip",
                    "notes": "smoke", "min_supported": "0.1.0"}

        def transport(url, timeout):
            return dict(manifest)

        with patch.dict(os.environ, {"QUILL_UPDATE_CHECK": "1"}, clear=False), \
             patch.object(uc, "enabled", return_value=True), \
             patch.object(uc, "manifest_url",
                          return_value="https://example.invalid/manifest.json"):
            out = uc.check(transport=transport, force=True)
        ok = (out.get("state") == "update_available"
              and uc.is_newer("99.0.0", __version__))
        report.add("update_check", tier, ok, f"state={out.get('state')}")
    except Exception as exc:
        report.add("update_check", tier, False, repr(exc))


def _resolve_scratch(staging: bool) -> str:
    if staging:
        data = os.environ.get("QUILL_DATA_DIR", str(ROOT / "data"))
        return str(Path(data).resolve())
    return tempfile.mkdtemp(prefix="quill_feature_smoke_")


def _print_report(report: Report) -> None:
    by_tier: dict[str, list[Check]] = {}
    for c in report.checks:
        by_tier.setdefault(c.tier, []).append(c)

    print("=" * 62)
    print("  Sparrow feature smoke")
    print(f"  scratch: {report.scratch}")
    print("=" * 62)
    for tier, checks in by_tier.items():
        print(f"\n--- {tier.upper()} ---")
        for c in checks:
            mark = {"pass": "ok  ", "fail": "FAIL", "skip": "SKIP"}[c.status]
            line = f"  [{mark}] {c.name}"
            if c.detail:
                line += f" — {c.detail}"
            print(line)

    n_pass = sum(1 for c in report.checks if c.status == "pass")
    n_fail = sum(1 for c in report.checks if c.status == "fail")
    n_skip = sum(1 for c in report.checks if c.status == "skip")
    print(f"\n  {n_pass} passed, {n_fail} failed, {n_skip} skipped")
    if report.failed:
        print(f"  FAILED: {', '.join(c.name for c in report.failed)}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sparrow full-system feature smoke")
    ap.add_argument("--ui", action="store_true", help="Tier 2: Playwright UI pages")
    ap.add_argument("--live", action="store_true",
                    help="Tier 3: browser-agent routing eval (needs API key)")
    ap.add_argument("--deep", action="store_true",
                    help="Tiers 4–7: capture, browser live, goldens, network mocks")
    ap.add_argument("--all", action="store_true", help="api + ui + live routing")
    ap.add_argument("--everything", action="store_true", help="--all --deep")
    ap.add_argument("--staging", action="store_true",
                    help="Read-only checks against QUILL_DATA_DIR or ./data")
    ap.add_argument("--json", metavar="PATH", help="Write JSON report")
    ap.add_argument("--verbose", action="store_true",
                    help="Show app startup logs and eval harness tables")
    ap.add_argument("--install-browser", action="store_true",
                    help="Run playwright install chromium before UI/browser tiers")
    ap.add_argument("--port", type=int, default=None,
                    help="Port for Tier 2 server (default: random)")
    ap.add_argument("--voice-limit", type=int, default=5,
                    help="Golden audio clips for --deep capture tier")
    args = ap.parse_args(argv)

    if args.everything:
        args.all = True
        args.deep = True

    do_ui = args.ui or args.all
    do_live = args.live or args.all
    do_deep = args.deep
    quiet = not args.verbose
    staging = args.staging

    if staging and (do_ui or do_deep):
        print("[smoke] --staging is read-only; run without --ui/--deep for full tiers")
        do_ui = False
        do_deep = False
        do_live = False

    if args.install_browser and (do_ui or do_deep):
        print(f"[smoke] installing chromium via {_playwright_install_cmd()}")
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       cwd=str(ROOT), check=False)

    scratch = _resolve_scratch(staging)
    report = Report(scratch=scratch)

    if staging and not Path(scratch).is_dir():
        report.add("staging_data_dir", "staging", False, f"missing {scratch}")
        _print_report(report)
        return 1

    run_tier1(report, scratch, quiet=quiet,
              read_only=staging, seed=not staging)
    if staging:
        run_tier_staging_people(report, scratch)
    if do_ui:
        run_tier2(report, scratch, port=args.port)
    if do_live:
        run_tier3(report, quiet=quiet)
    if do_deep:
        run_tier4_capture(report, scratch, quiet=quiet,
                          voice_limit=args.voice_limit)
        run_tier5_browser(report, quiet=quiet)
        run_tier6_quality(report, quiet=quiet)
        run_tier7_network(report)

    _print_report(report)

    if args.json:
        Path(args.json).write_text(
            json.dumps({"scratch": report.scratch,
                        "checks": [asdict(c) for c in report.checks]},
                       indent=2),
            encoding="utf-8",
        )
        print(f"\n  wrote {args.json}")

    return report.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())
