"""Windows notification capture — Phone Link / iPhone mirror support.

Microsoft does not expose a public Phone Link API. iPhone notifications mirrored
via Phone Link appear as normal Windows toast notifications from the "Phone
Link" app. This pipeline reads them with UserNotificationListener (winsdk).

One-time setup: Windows Settings → Privacy & security → Notifications →
enable notification access for Python.

Set QUILL_NOTIFICATIONS=0 to disable. QUILL_NOTIFICATION_APPS filters by app
display name (default: phone link, link to windows).
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from typing import Callable

from app.config import settings
from app.events import Event, Modality, bus

NotificationSink = Callable[[Event], None]

_DEFAULT_APPS = ("phone link", "link to windows", "your phone")


def _app_filters() -> tuple[str, ...]:
    raw = os.environ.get("QUILL_NOTIFICATION_APPS", "").strip()
    if raw:
        return tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    if settings.notifications.phone_link_only:
        return _DEFAULT_APPS
    return ()


def _matches_app(display_name: str) -> bool:
    filters = _app_filters()
    if not filters:
        return True
    name = (display_name or "").lower()
    return any(f in name for f in filters)


def _extract_text(notif) -> str:
    parts: list[str] = []
    try:
        for binding in notif.notification.visual.bindings:
            elements = binding.get_text_elements()
            for i in range(len(elements)):
                text = (elements[i].text or "").strip()
                if text and text not in parts:
                    parts.append(text)
    except Exception:
        pass
    return " — ".join(parts)


def _notif_id(notif) -> int:
    return int(notif.id)


class NotificationPipeline:
    def __init__(self, sink: NotificationSink | None = None) -> None:
        self.cfg = settings.notifications
        self._sink = sink or (lambda ev: bus.publish_nowait(ev))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seen: set[int] = set()
        self._bootstrapped = False

    def start(self) -> None:
        if os.name != "nt":
            print("[notifications] Windows only — skipped.")
            return
        if not self.cfg.enabled:
            print("[notifications] disabled (QUILL_NOTIFICATIONS=0).")
            return
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_loop())
        except Exception as exc:
            print(f"[notifications] stopped: {exc}")

    async def _async_loop(self) -> None:
        try:
            from winsdk.windows.ui.notifications.management import (
                UserNotificationListener,
                UserNotificationListenerAccessStatus,
            )
            from winsdk.windows.ui.notifications import NotificationKinds
        except ImportError:
            print("[notifications] winsdk not installed — run: pip install winsdk")
            return

        listener = UserNotificationListener.current
        access = await listener.request_access_async()
        if access != UserNotificationListenerAccessStatus.ALLOWED:
            print("[notifications] access denied. Enable in Windows Settings → "
                  "Privacy & security → Notifications → Notification access "
                  "(allow Python).")
            return

        apps = ", ".join(_app_filters()) or "all apps"
        print(f"[notifications] listening for Windows toasts ({apps}). "
              f"Poll every {self.cfg.poll_interval_s:.0f}s.")

        while not self._stop.is_set():
            try:
                notifs = await listener.get_notifications_async(NotificationKinds.TOAST)
                self._ingest_batch(notifs)
            except Exception as exc:
                print(f"[notifications] poll error: {exc}")
            await asyncio.sleep(self.cfg.poll_interval_s)

    def _ingest_batch(self, notifs) -> None:
        if not self._bootstrapped:
            for n in notifs:
                self._seen.add(_notif_id(n))
            self._bootstrapped = True
            return

        for n in notifs:
            nid = _notif_id(n)
            if nid in self._seen:
                continue
            self._seen.add(nid)
            app = ""
            try:
                app = n.app_info.display_info.display_name if n.app_info else ""
            except Exception:
                pass
            if not _matches_app(app):
                continue
            body = _extract_text(n)
            if not body:
                continue
            ts = time.time()
            summary = body.split(" — ")[0][:200]
            ev = Event(
                time=ts,
                modality=Modality.NOTIFICATION,
                raw=body,
                summary=summary,
                source="notifications.phone_link",
                meta={"app": app, "notification_id": nid},
            )
            print(f"[notification] {app}: {summary}")
            self._sink(ev)
