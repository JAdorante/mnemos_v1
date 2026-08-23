"""Mnemos FastAPI entrypoint.  Run:  uvicorn app.main:app --reload"""
from __future__ import annotations

import asyncio
import os
import threading

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router, start_all, stop_all
from app.api.adoption import router as adoption_router
from app.config import settings
from app.events import bus
from app.services.api_auth import (
    CsrfProtectMiddleware,
    LanApiAuthMiddleware,
    ensure_api_token,
)
from app.services.memory import memory

app = FastAPI(title="Mnemos", version="0.1.0")
app.add_middleware(LanApiAuthMiddleware)
# Outer: CSRF runs first (plan 6.4) — cross-origin POSTs rejected.
app.add_middleware(CsrfProtectMiddleware)
app.include_router(router)
app.include_router(adoption_router)


# --- active-minute marker (WS-A) --------------------------------------------
# "Active" means the human was in front of Mnemos, not that a process was up.
# A request to chat / search / the Console / an approval marks the current UTC
# minute; the ledger dedupes minute-stamps, so a polling page still counts as
# one minute. Nothing about the request is recorded — no path, no query, no
# body — only that *some* interaction happened in that minute.
_ACTIVE_PREFIXES = ("/chat", "/memory/search", "/console/", "/approvals",
                    "/approval/", "/facts/", "/people/", "/today", "/meetings")


@app.middleware("http")
async def _mark_active_minute(request, call_next):
    try:
        path = request.url.path
        if any(path == p.rstrip("/") or path.startswith(p)
               for p in _ACTIVE_PREFIXES):
            from app.services.usage_ledger import usage
            usage.mark_active()
    except Exception:
        pass  # instrumentation must never fail a request (house rule 3)
    return await call_next(request)
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# --- tail-latency nudge -----------------------------------------------------
# After you stop talking, the extract chain runs once, sees the last turn hasn't
# settled yet, and stops — nothing re-triggers it until the NEXT audio event. So
# the last thing said before a silence can hang. The extractor reports how long
# until that turn settles; we schedule a single delayed re-enqueue of `extract`
# for that moment. Debounced (one pending timer) and self-terminating (only
# text-bearing turns report a settle time), so it can't spin.
_extract_nudge_lock = threading.Lock()
_extract_nudge_timer: threading.Timer | None = None


def _schedule_extract_nudge(delay_s: float) -> None:
    global _extract_nudge_timer
    delay_s = max(0.5, min(delay_s, 30.0))

    def _fire() -> None:
        global _extract_nudge_timer
        with _extract_nudge_lock:
            _extract_nudge_timer = None
        try:
            from app.services.worker import worker
            worker.enqueue("extract", unique=True)
        except Exception as exc:
            print(f"[extract] nudge skipped ({exc}).")

    with _extract_nudge_lock:
        if _extract_nudge_timer is not None and _extract_nudge_timer.is_alive():
            return  # one already pending; it will cover this (or reschedule)
        _extract_nudge_timer = threading.Timer(delay_s, _fire)
        _extract_nudge_timer.daemon = True
        _extract_nudge_timer.start()


@app.on_event("startup")
async def _startup() -> None:
    # Open bind (phone / Tailscale) without a loopback-only host: mint or load
    # the LAN API gate token before serving requests.
    ensure_api_token()
    bus.bind_loop(asyncio.get_running_loop())
    memory.attach()  # Memory Engine subscribes to every event
    # Pilot usage ledger (WS-A): counts an app start and begins the 60 s flush
    # timer. Numbers only, local only — see services/usage_ledger.py.
    try:
        from app.services.usage_ledger import usage
        usage.start()
    except Exception as exc:
        print(f"[usage] startup hook skipped ({exc}).")
    # Latency program, Phase 1.1: load the local text model now so the FIRST
    # user interaction is a warm call. Measured cold-start tax on the reference
    # machine is ~3.4 s (load_duration 3,571 ms cold vs 163 ms warm), which is
    # the single largest avoidable delay in the local path. Off by default and
    # on its own thread — a machine with no Ollama boots exactly as before.
    try:
        from app.config import settings as _s
        if _s.text_local.enabled and _s.text_local.warmup:
            import threading as _t
            from app.services.ollama_text import OllamaText
            _t.Thread(target=lambda: OllamaText().warmup(),
                      name="ollama-warmup", daemon=True).start()
    except Exception as exc:
        print(f"[ollama_text] warmup hook skipped ({exc}).")
    # Version manifest check (WS-C): one unconditional GET of a static file,
    # off with QUILL_UPDATE_CHECK=0. Notification only — never downloads.
    try:
        from app.services import update_check
        update_check.start_background()
    except Exception as exc:
        print(f"[update_check] startup hook skipped ({exc}).")
    # #B4: once this machine has enough of its OWN stored utterances, derive the
    # audio thresholds from them (idempotent, bounded, best-effort — no-op if a
    # calibration already exists, too few clips, or QUILL_AUTO_CALIBRATE=0).
    try:
        from app.services.calibration import maybe_autocalibrate
        adopted = maybe_autocalibrate()
        if adopted:
            print(f"[calibration] adopted machine-derived thresholds "
                  f"({adopted.get('n_clips')} clips). Restart to apply, or they "
                  "take effect on the next Settings build.")
    except Exception as exc:
        print(f"[calibration] startup hook skipped ({exc}).")
    # One-time new-user onboarding: first boot writes a profile sheet template
    # (and says so once); a boot that finds the filled sheet ingests it into
    # people/entities/facts/graph, then never asks again. Best-effort.
    try:
        from app.services.onboarding import startup_check
        startup_check()
    except Exception as exc:
        print(f"[onboarding] startup hook skipped ({exc}).")
    # Data-growth watchdog: one console line per boot; warns if any data root,
    # single file, or Lance version backlog crossed its size threshold. The
    # summary ALSO lands in the chat pane — console-only warnings went unseen
    # for 10 days while data/ sat at 34 GB.
    try:
        from app.services import data_watch

        def _notify_chat(msg: str) -> None:
            from app.services import agent_bridge
            agent_bridge.worker._emit("system", msg)

        data_watch.startup_check(notify=_notify_chat)
    except Exception as exc:
        print(f"[data_watch] startup hook skipped ({exc}).")
    # Idle LoRA retraining (Phase 3): condition-driven — enough NEW labeled
    # pairs + user idle + AC power + disk headroom, rate-capped with failure
    # backoff. Off by default (QUILL_IDLE_TRAIN=1 opts in): it occupies the
    # GPU for ~an hour, so it must be the user's choice.
    try:
        from app.services.idle_trainer import idle_trainer
        idle_trainer.start()
    except Exception as exc:
        print(f"[idle_trainer] startup hook skipped ({exc}).")
    # Durable background worker: capture enqueues jobs, the worker drains them
    # off the request/capture path. Consolidation (turns) runs here; extraction
    # (facts/tasks) registers alongside it next.
    if settings.worker.enabled:
        from app.events import Modality
        from app.services import activity, consolidation
        from app.services.worker import worker

        # Extraction (facts/tasks) runs after consolidation so it reads settled
        # turns, not raw fragments. Gated by QUILL_EXTRACT (it calls the LLM).
        extract_on = os.environ.get("QUILL_EXTRACT", "1") not in ("0", "false", "False")
        # Reflection (facts -> durable intelligence) is time-driven, not part of
        # the capture chain. Gated by QUILL_REFLECT (it calls the LLM).
        reflect_on = os.environ.get("QUILL_REFLECT", "1") not in ("0", "false", "False")
        if reflect_on:
            from app.services.reflector import reflector

            worker.register("reflect_daily", lambda _p: reflector.reflect_daily())

        # Track A1: seed missing node_dynamics + nightly priors-continuity replay.
        # Observe-only — never changes ranking (Field v2 stays off until the gate).
        from app.services import attention_replay, traces_backfill
        from app.services import ranking_promote, meta_memory, horizon as _horizon

        worker.register("traces_backfill",
                        lambda _p: traces_backfill.run())
        worker.register("attention_replay",
                        lambda _p: attention_replay.run())
        worker.register("ranking_promote",
                        lambda _p: ranking_promote.run())
        worker.register("meta_memory",
                        lambda _p: meta_memory.run(write_reflections=True))
        def _horizon_refresh_job(_p) -> None:
            _horizon.refresh()
            # Meeting Layer P5 — suggest meeting mode when a calendar event starts.
            try:
                from app.services import meeting_mode as _mm
                _mm.consider_offer(memory._ensure_store())
            except Exception as exc:
                print(f"[meeting_mode] consider skipped ({exc}).")

        worker.register("horizon_refresh", _horizon_refresh_job)

        # Track C: nightly retention sweep (observe-only unless QUILL_COMPACTION).
        from app.services import memory_economy

        worker.register("memory_economy",
                        lambda _p: memory_economy.sweep())
        try:
            memory_economy.attach()
        except Exception as exc:
            print(f"[memory_economy] attach skipped ({exc}).")

        # Track F: predictor bench (walk-forward replay) + weekly restore drill.
        from app.services import hardening, predictor_bench

        worker.register("predictor_bench",
                        lambda _p: predictor_bench.run())
        worker.register("restore_drill",
                        lambda _p: hardening.restore_drill())

        # KG v2 Change 5: batch posterior recal — sweeps posterior_stale
        # predicates in capped batches, re-enqueueing itself until clean.
        # Same single worker thread as every other writer (I-9: calm).
        def _kg_recal_job(_payload) -> None:
            from app.services import kg_beliefs
            res = kg_beliefs.recal_sweep(memory._ensure_store())
            if res.get("remaining"):
                worker.enqueue("kg_confidence_recal", unique=True)

        worker.register("kg_confidence_recal", _kg_recal_job)

        # KG v2 Change 8: nightly-style parity diff while shadow mode is on.
        # Report-only (never repairs); gates the M3 read cutover.
        from app.services import kg_parity

        worker.register("kg_parity_diff",
                        lambda _p: kg_parity.run(memory._ensure_store()))

        # People v3 WS-E: daily ambient-TTL sweep over the review queue
        # (archive-only, flag QUILL_QUEUE_TTL, default off).
        from app.services import queue_hygiene

        worker.register("queue_ttl", queue_hygiene.run_job)
        queue_hygiene.attach()

        # People v3 WS-B: nightly v1-vs-v2 connection-score shadow diff
        # (report-only, flag QUILL_SCORE_SHADOW, default off). The
        # QUILL_SCORE_V2 read cutover stays gated on 7 clean nightlies.
        from app.services import score_v2

        worker.register("score_shadow", score_v2.run_job)
        score_v2.attach()

        # Attribution provenance: late re-resolution of open person mentions
        # still referenced by unowned tasks/commitments. Cheap no-op when
        # nothing is open; re-enqueued after merges (see /people soft-merge).
        try:
            from app.services import people_pipeline as _pp
            worker.register("people_reattribute", _pp.run_reresolve_job)
        except Exception as exc:
            print(f"[worker] people_reattribute register skipped ({exc}).")

        # KG v2 M1 backfill: legacy asserted/user relations -> belief store.
        # One-shot + idempotent; enqueued via POST /kg/backfill, chased by a
        # parity run so the report reflects the new state.
        def _kg_backfill_job(_payload) -> None:
            from app.services import kg_backfill
            kg_backfill.run(memory._ensure_store())
            worker.enqueue("kg_parity_diff", unique=True)

        worker.register("kg_backfill", _kg_backfill_job)

        def _consolidate_job(_payload) -> None:
            consolidation.rebuild()
            # Regroup turns into sessions (#4). Cheap, pure, and best-effort — a
            # failure here must not stall the turns -> facts chain.
            try:
                from app.services import sessions as _sessions
                _sessions.rebuild()
            except Exception as exc:
                print(f"[sessions] rebuild skipped ({exc}).")
            if extract_on:
                worker.enqueue("extract", unique=True)  # chain: turns -> facts
            else:
                # Meeting Layer P3: still try enhance when extract is off
                # (may only have prior facts / jots).
                worker.enqueue("session_enhance", unique=True)

        worker.register("consolidate", _consolidate_job)

        # Meeting Layer P3 — settled calendar-linked / ≥5-min sessions → note.
        def _session_enhance_job(_payload) -> None:
            from app.services import meeting_enhance
            try:
                meeting_enhance.run_once()
            except Exception as exc:
                print(f"[meeting_enhance] skipped ({exc}).")

        worker.register("session_enhance", _session_enhance_job)

        # Desktop rollup ("what was I doing?"): desktop.screen/click events fold
        # into app-focus activity blocks — the desktop analog of turns->sessions.
        # Phase D: when L3 is on, do NOT chain screen_extract (same-commit rule).
        from app.perception.l3_workers import (
            enqueue_extract_for_event as _l3_enqueue_extract,
            l3_cutover_plan, register_l3_jobs,
        )
        _l3_plan = l3_cutover_plan(
            l3_enabled=bool(settings.perception.l3_enabled),
            extract_on=bool(extract_on))

        def _activity_job(_payload) -> None:
            activity.rebuild()
            # Eyes -> memory: after desktop events fold into activities, mine
            # the settled screen frames for facts/entities (the screen analog
            # of the turns -> extract chain). No-op when disabled or L3 owns it.
            if _l3_plan["chain_screen_extract_from_activity"]:
                from app.services import screen_extract
                if screen_extract.enabled():
                    worker.enqueue("screen_extract", unique=True)

        worker.register("activity", _activity_job)
        if extract_on:
            from app.services.extractor import extractor
            from app.services import graph

            def _extract_job(_payload) -> None:
                res = extractor.run_once()
                # Drain a backlog in small batches: if this pass made progress and
                # events remain, queue another. Stops when nothing new is markable.
                if res.get("events_marked") and res.get("remaining"):
                    worker.enqueue("extract", unique=True)
                worker.enqueue("graph", unique=True)   # chain: facts -> edges
                # Meeting Layer P3: enhance after facts land for settled sessions.
                worker.enqueue("session_enhance", unique=True)
                # A turn is captured but not settled yet: schedule a one-shot
                # re-run for just after it settles, so the last thing said before
                # a silence surfaces without waiting for the next sound.
                delay = res.get("next_settle_in")
                if delay is not None:
                    _schedule_extract_nudge(delay + 0.5)

            worker.register("extract", _extract_job)

            def _graph_job(_payload) -> None:
                graph.rebuild()
                # Fresh about-edges -> refresh each entity's home project.
                # Best-effort: a rollup failure must never fail the rebuild.
                try:
                    from app.services import project_rollup
                    if project_rollup.enabled():
                        project_rollup.run()
                except Exception as exc:
                    print(f"[project_rollup] skipped ({exc}).")

            worker.register("graph", _graph_job)
            # Typed chat -> memory: /chat stores a TEXT event + queues this.
            from app.services import chat_ingest
            worker.register("chat_ingest", chat_ingest.run_job)
            # Teammate answers -> memory: an answered peer ask stores a TEXT
            # event + queues this (peer_answer source class, hearsay tier).
            from app.services import peer_channel
            worker.register("peer_ingest", peer_channel.run_ingest_job)
            try:
                from app.services import team_layer
                team_layer.attach()
            except Exception as exc:
                print(f"[peer] presence attach skipped ({exc}).")

            # Screen -> memory (legacy): only when L3 is off.
            if _l3_plan["register_screen_extract"]:
                def _screen_extract_job(_payload) -> None:
                    from app.services import screen_extract
                    res = screen_extract.run_once()
                    if res.get("remaining"):
                        worker.enqueue("screen_extract", unique=True)

                worker.register("screen_extract", _screen_extract_job)

        # Org AI Network (feature-flagged): upward digests + priority pull.
        # Independent of extraction — digests read whatever facts already exist.
        from app.services import org_client as _org_client
        if _org_client.enabled():
            from app.services import org_digest, org_priority
            worker.register("org_digest", org_digest.run_digest_job)
            worker.register("org_priorities", org_priority.run_priority_job)
            print("[worker] org-network jobs registered "
                  "(digest + priorities).")
            if not _org_client.coordinator_reachable():
                print("[org-network] coordinator not reachable at "
                      f"{_org_client.status().get('coordinator_url')} — "
                      "start it via run_all.py or "
                      "`python -m org_coordinator.main`.")
            else:
                print(f"[org-network] UI http://{settings.host}:{settings.port}"
                      f"/org-network · coordinator OK")

        # Phase D: L3 semantics — mutually exclusive with screen_extract.
        if _l3_plan["register_l3"]:
            try:
                register_l3_jobs(worker)
                print("[worker] L3 jobs registered "
                      "(screen_extract scheduling off).")
            except Exception as exc:
                print(f"[worker] L3 register skipped ({exc}).")

        # Phase C: L2 frame age/budget compaction (independent of extract).
        try:
            from app.perception.compactor import run_job as _perception_compact
            worker.register("perception_compact", _perception_compact)
            worker.enqueue("perception_compact", unique=True)
        except Exception as exc:
            print(f"[worker] perception_compact register skipped ({exc}).")
        # People v3 P3 (WS-A): retroactive rebind of escrowed voice-track rows
        # (QUILL_PEOPLE_ESCROW). Registered unconditionally so a rebind queued
        # while the flag was on still completes after a restart/flag flip —
        # with the flag off no job of this kind is ever enqueued.
        try:
            from app.services import people_escrow
            worker.register("people_escrow_rebind",
                            people_escrow.run_rebind_job)
        except Exception as exc:
            print(f"[worker] people_escrow register skipped ({exc}).")
        worker.start()

        # New audio utterances re-consolidate the timeline; new desktop events
        # re-fold activities (each coalesced to one pending job so a burst
        # doesn't queue dozens of rebuilds). Audio and webcam VISION events also
        # re-fold activities: they enrich the blocks' "heard:"/"saw:" context,
        # and the rebuild is idempotent + coalesced, so this stays cheap.
        def _reconsolidate(ev) -> None:
            if not settings.consolidation.enabled:
                return
            if ev.modality == Modality.AUDIO:
                worker.enqueue("consolidate", unique=True)
                worker.enqueue("activity", unique=True)
            elif ev.source in activity.SOURCES or ev.modality == Modality.VISION:
                worker.enqueue("activity", unique=True)
            # Phase D: L1 captures carry meta.capture_id → l3_extract.
            if _l3_plan["enqueue_l3_from_captures"]:
                _l3_enqueue_extract(ev, worker)

        bus.subscribe(_reconsolidate)
        worker.enqueue("consolidate", unique=True)  # initial pass on boot
        worker.enqueue("activity", unique=True)     # cheap no-op without desktop events
        # A1: backfill traces once on boot (idempotent), then replay if due.
        worker.enqueue("traces_backfill", unique=True)
        # Attribution provenance: one re-resolve sweep per boot (idempotent).
        worker.enqueue("people_reattribute", unique=True)
        if attention_replay.due_for():
            worker.enqueue("attention_replay", unique=True)
        # KG v2: clear any posterior_stale backlog on boot (coalesced);
        # parity diff only while the shadow flag is set.
        worker.enqueue("kg_confidence_recal", unique=True)
        if kg_parity.shadow_mode():
            worker.enqueue("kg_parity_diff", unique=True)
            try:
                kg_parity.attach()
            except Exception as exc:
                print(f"[kg_parity] attach skipped ({exc}).")
        # A4: meta-memory audit + β promote gate when due; horizon refresh.
        worker.enqueue("meta_memory", unique=True)
        worker.enqueue("horizon_refresh", unique=True)
        if ranking_promote.due_for():
            worker.enqueue("ranking_promote", unique=True)
        # Track C: retention sweep + growth snapshot when stale (>20h).
        if memory_economy.due_for():
            worker.enqueue("memory_economy", unique=True)
        # Track F: bench when stale (>20h); restore drill weekly.
        if predictor_bench.due_for():
            worker.enqueue("predictor_bench", unique=True)
        if hardening.due_for_drill():
            worker.enqueue("restore_drill", unique=True)
        # No cron yet: on boot, if the last daily reflection is stale (>20h),
        # queue one. unique_pending coalesces, so this never stacks up.
        if reflect_on and reflector.due_for("daily"):
            worker.enqueue("reflect_daily", unique=True)
        if _org_client.enabled():
            from app.services import org_digest as _org_digest
            if _org_digest.due_for_digest():
                worker.enqueue("org_digest", unique=True)
            worker.enqueue("org_priorities", unique=True)
        print(f"[worker] started; queued initial consolidation"
              f"{' + extraction' if extract_on else ''}"
              f"{' + reflection' if reflect_on else ''}"
              f"{' + org-network' if _org_client.enabled() else ''}"
              f" + traces/replay/a4.")
    # iCloud calendar sync: periodic, read-only, only when the guided connect
    # flow has stored credentials. Best-effort — failure never blocks startup.
    try:
        from app.services import icloud_calendar
        if icloud_calendar.start_background():
            print("[icloud_calendar] periodic sync running "
                  f"(every {int(settings.icloud.sync_interval_s)}s when connected).")
    except Exception as exc:
        print(f"[icloud_calendar] startup hook skipped ({exc}).")
    # Proactively offer to act on detected to-do lists (via chat).
    if os.environ.get("QUILL_AGENT") not in ("0", "false", "False"):
        from app.services import todo_watcher
        todo_watcher.attach()
        try:
            from app.services import homework_watcher
            homework_watcher.attach()
        except Exception as exc:
            print(f"[homework] startup skipped ({exc}).")
        if os.environ.get("QUILL_PHONE_WATCH", "1") not in ("0", "false", "False"):
            from app.services import phone_watcher
            phone_watcher.attach()
        # Track D: commitment / relationship / scheduling reasoners (offer-gated).
        try:
            from app.services import reasoners
            reasoners.attach()
        except Exception as exc:
            print(f"[reasoners] startup skipped ({exc}).")
        # Standing triggers ("when it sees X, offer Y") — data-driven rows,
        # same calm-budget/offer posture as the reasoners.
        try:
            from app.services import triggers
            triggers.attach()
        except Exception as exc:
            print(f"[triggers] startup skipped ({exc}).")
    # A2: keep the Now-Context fed from speech / desktop / calendar.
    try:
        from app.services import context_feeder
        context_feeder.attach()
        try:
            from app.services import meeting_session as _msess
            _msess.attach()
        except Exception as exc:
            print(f"[meeting_session] startup skipped ({exc}).")
        try:
            from app.services import meeting_capture as _mc
            _mc.attach()
        except Exception as exc:
            print(f"[meeting_capture] startup skipped ({exc}).")
    except Exception as exc:
        print(f"[context_feeder] startup skipped ({exc}).")
    # Re-apply persisted privacy / kill-switch overrides onto frozen settings.
    try:
        from app.services import capture_consent, hardening
        hardening.apply_saved_overrides()
        capture_consent.apply_saved_to_runtime()
    except Exception as exc:
        print(f"[launch] consent/hardening load skipped ({exc}).")
    # QUILL_AUTOSTART=1 boots the server ready, but capture only re-arms when
    # the user has already consented in the UI (data/capture_consent.json).
    # Meeting-first mode never resumes always-on mic/webcam/screen from boot.
    if os.environ.get("QUILL_AUTOSTART") == "1":
        try:
            from app.services import capture_consent, first_run
            consent = capture_consent.load()
        except Exception:
            consent = {"consented": False, "sources": {}}
            first_run = None  # type: ignore
        src = consent.get("sources") or {}
        meeting_first = False
        try:
            from app.services import first_run as _fr
            meeting_first = _fr.is_meeting_first() and not _fr.allows_continuous("mic")
        except Exception:
            meeting_first = False
        if (not meeting_first) and consent.get("consented") and any(
                src.get(k) for k in (
                    "mic", "webcam", "screen", "system_audio", "clicks")):
            force_no_audio = os.environ.get("QUILL_AUTOSTART_AUDIO") == "0"
            force_no_vision = os.environ.get("QUILL_AUTOSTART_VISION") == "0"
            state = start_all(
                audio=bool(src.get("mic")) and not force_no_audio,
                vision=bool(src.get("webcam")) and not force_no_vision,
                notifications=os.environ.get("QUILL_AUTOSTART_NOTIFICATIONS") != "0",
                desktop_capture=bool(src.get("screen") or src.get("clicks")),
                system_audio=bool(src.get("system_audio")) and not force_no_audio,
            )
            print(f"[launch] capture resumed from consent: {state}")
        else:
            # Notifications are metadata, not A/V — still honor their autostart.
            if os.environ.get("QUILL_AUTOSTART_NOTIFICATIONS") != "0":
                start_all(audio=False, vision=False, notifications=True,
                          desktop_capture=False, system_audio=False)
            why = "meeting-first" if meeting_first else "idle"
            print(f"[launch] capture {why} — open Privacy / onboarding to opt in.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    stop_all()
    try:
        from app.services.usage_ledger import usage
        usage.stop()  # final flush: a clean exit loses no counts
    except Exception as exc:
        print(f"[usage] shutdown flush skipped ({exc}).")
    global _extract_nudge_timer
    with _extract_nudge_lock:
        if _extract_nudge_timer is not None:
            _extract_nudge_timer.cancel()
            _extract_nudge_timer = None
    if settings.worker.enabled:
        from app.services.worker import worker

        worker.stop()


@app.get("/api")
def api_stub() -> dict:
    """Machine-readable service stub (formerly served at `/`)."""
    return {
        "name": "Mnemos",
        "milestone": "Personal Intelligence Platform",
        "endpoints": [
            "GET /", "GET /welcome", "GET /welcome/status",
            "GET /today", "GET /today/state", "POST /today/offer",
            "POST /today/restore",
            "GET /shell → 301 /today",
            "GET /chat", "GET /ui → 301 /chat",
            "GET /memory", "GET /console → 301 /memory",
            "GET /memory/events", "GET /home/intelligence",
            "GET /field/state",
            "GET /field/stream",
            "GET /field/predictions",
            "GET /field/mode",
            "POST /field/mode",
            "GET /graph/constellation",
            "GET /graph/constellation/evidence",
            "POST /graph/constellation/pin",
            "POST /audio/start", "POST /audio/stop",
            "GET /memory/search?q=",
            "POST /chat", "POST /speak", "GET /health",
        ],
    }


@app.get("/")
def root():
    """Launch page — new user setup or continue / unlock on this machine."""
    from fastapi.responses import HTMLResponse
    from app.api.welcome_page import WELCOME_PAGE
    return HTMLResponse(WELCOME_PAGE)
