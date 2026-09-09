"""Pluggable "committed URL of the foreground browser window" sources.

Two implementations of one interface:

  UiaSource    — Windows. Delegates to the SHIPPED reader
                 (app.perception.uia_url.read_url), so the harness measures
                 production code rather than a copy of it.
  AtspiSource  — Linux. The same idea over AT-SPI: find the active browser
                 frame, descend to its web document, read DocURL.

They are not the same API and the harness never pretends they are. What
transfers between them is everything that is a BROWSER behaviour rather than a
platform behaviour: whether a title-change invalidation catches a navigation,
whether same-title SPA routing is missed, what a page mid-load reports, and
what requesting the accessibility tree costs the browser process.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time

# Browser app names as they appear to each platform's accessibility layer.
BROWSER_APPS = ("chromium", "chrome", "google chrome", "firefox", "mozilla firefox",
                "microsoft edge", "msedge", "brave", "arc")

STATE_ACTIVE = 1          # ATSPI_STATE_ACTIVE
_DOC_IFACE = "org.a11y.atspi.Document"
_ACC_IFACE = "org.a11y.atspi.Accessible"


class UrlSource:
    name = "none"

    def available(self) -> tuple[bool, str]:
        return False, "not implemented"

    def read(self) -> str | None:
        """Committed URL of the foreground browser window, or None."""
        return None

    def close(self) -> None:
        pass


# ------------------------------- Windows ----------------------------------
class UiaSource(UrlSource):
    name = "uia"

    def available(self) -> tuple[bool, str]:
        if os.name != "nt":
            return False, "not Windows"
        try:
            import comtypes  # noqa: F401
        except Exception as exc:
            return False, f"comtypes missing ({exc})"
        os.environ.setdefault("QUILL_PERCEPTION_URL", "1")
        return True, "UIAutomation"

    def read(self) -> str | None:
        import ctypes

        from app.perception.uia_url import read_url
        hwnd = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        return read_url(hwnd)


# -------------------------------- Linux -----------------------------------
class AtspiSource(UrlSource):
    """AT-SPI2 reader. Runs its own asyncio loop on a dedicated thread so the
    caller stays synchronous, mirroring how uia_url keeps COM off the caller's
    thread."""

    name = "atspi"

    def __init__(self, max_nodes: int = 3000, timeout_s: float = 5.0) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._bus = None
        self._max_nodes = max_nodes
        self._timeout_s = timeout_s

    # -- lifecycle ---------------------------------------------------------
    def available(self) -> tuple[bool, str]:
        if os.name == "nt":
            return False, "Windows: use the uia source"
        try:
            import dbus_next  # noqa: F401
        except Exception as exc:
            return False, f"dbus-next missing: pip install dbus-next ({exc})"
        addr = self.bus_address()
        if not addr:
            return False, "no org.a11y.Bus address (is at-spi2-core running?)"
        try:
            self._start()
        except Exception as exc:
            return False, f"a11y bus connect failed ({exc})"
        return True, f"AT-SPI2 at {addr.split(',')[0]}"

    @staticmethod
    def bus_address() -> str | None:
        import subprocess
        try:
            out = subprocess.run(
                ["dbus-send", "--session", "--dest=org.a11y.Bus",
                 "--print-reply", "/org/a11y/bus", "org.a11y.Bus.GetAddress"],
                capture_output=True, text=True, timeout=6).stdout
        except Exception:
            return None
        for line in out.splitlines():
            line = line.strip()
            if line.startswith('string "'):
                return line[len('string "'):-1]
        return None

    @staticmethod
    def accessibility_enabled() -> bool | None:
        """org.a11y.Status.IsEnabled — the Linux analog of 'an AT client showed
        up'. This is the flag whose cost Phase -1 measures."""
        import subprocess
        try:
            out = subprocess.run(
                ["dbus-send", "--session", "--dest=org.a11y.Bus", "--print-reply",
                 "/org/a11y/bus", "org.freedesktop.DBus.Properties.Get",
                 "string:org.a11y.Status", "string:IsEnabled"],
                capture_output=True, text=True, timeout=6).stdout
        except Exception:
            return None
        if "boolean true" in out:
            return True
        if "boolean false" in out:
            return False
        return None

    @staticmethod
    def screen_reader_enabled() -> bool | None:
        """org.a11y.Status.ScreenReaderEnabled.

        MEASURED ON THIS BOX, 2026-09-09: with this false, Chromium publishes
        the application and frame but its web-contents child is an unpopulated
        stub — GetRoleName on it fails and there is no document, so no URL.
        Setting it true makes Chromium build the web tree and the document URL
        becomes readable. That is the Linux shape of Phase -1's question: the
        tree is genuinely lazy, and the signal that unlocks it is a coarse,
        DESKTOP-WIDE flag, not a per-read request.
        """
        import subprocess
        try:
            out = subprocess.run(
                ["dbus-send", "--session", "--dest=org.a11y.Bus", "--print-reply",
                 "/org/a11y/bus", "org.freedesktop.DBus.Properties.Get",
                 "string:org.a11y.Status", "string:ScreenReaderEnabled"],
                capture_output=True, text=True, timeout=6).stdout
        except Exception:
            return None
        if "boolean true" in out:
            return True
        if "boolean false" in out:
            return False
        return None

    @staticmethod
    def set_screen_reader(enabled: bool) -> bool:
        return AtspiSource._set_status("ScreenReaderEnabled", enabled)

    @staticmethod
    def set_accessibility(enabled: bool) -> bool:
        return AtspiSource._set_status("IsEnabled", enabled)

    @staticmethod
    def _set_status(prop: str, enabled: bool) -> bool:
        import subprocess
        try:
            subprocess.run(
                ["dbus-send", "--session", "--dest=org.a11y.Bus", "--print-reply",
                 "/org/a11y/bus", "org.freedesktop.DBus.Properties.Set",
                 "string:org.a11y.Status", f"string:{prop}",
                 f"variant:boolean:{'true' if enabled else 'false'}"],
                capture_output=True, text=True, timeout=6, check=True)
            return True
        except Exception:
            return False

    def _start(self) -> None:
        if self._loop is not None:
            return
        ready = threading.Event()
        err: list = []

        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._connect())
            except Exception as exc:            # pragma: no cover - env dependent
                err.append(exc)
            finally:
                ready.set()
            if not err:
                loop.run_forever()

        self._thread = threading.Thread(target=_run, daemon=True,
                                        name="ws2d-atspi")
        self._thread.start()
        ready.wait(timeout=10)
        if err:
            raise err[0]

    async def _connect(self) -> None:
        from dbus_next.aio import MessageBus
        self._bus = await MessageBus(bus_address=self.bus_address()).connect()

    def close(self) -> None:
        loop = self._loop
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        self._loop = None

    # -- dbus plumbing -----------------------------------------------------
    async def _call(self, dest, path, iface, member, sig="", body=None):
        from dbus_next import Message, MessageType
        msg = Message(destination=dest, path=path, interface=iface,
                      member=member, signature=sig, body=body or [])
        reply = await asyncio.wait_for(self._bus.call(msg), timeout=self._timeout_s)
        if reply is None or reply.message_type == MessageType.ERROR:
            raise RuntimeError(f"{member}: {reply.body if reply else 'no reply'}")
        return reply.body

    async def _prop(self, dest, path, iface, name):
        body = await self._call(dest, path, "org.freedesktop.DBus.Properties",
                                "Get", "ss", [iface, name])
        return body[0].value

    async def _children(self, dest, path):
        return (await self._call(dest, path, _ACC_IFACE, "GetChildren"))[0]

    async def _ifaces(self, dest, path):
        return (await self._call(dest, path, _ACC_IFACE, "GetInterfaces"))[0]

    async def _is_active(self, dest, path) -> bool:
        try:
            states = (await self._call(dest, path, _ACC_IFACE, "GetState"))[0]
        except Exception:
            return False
        return bool(states and (int(states[0]) >> STATE_ACTIVE) & 1)

    async def _doc_url(self, dest, path) -> str | None:
        try:
            url = (await self._call(dest, path, _DOC_IFACE, "GetAttributeValue",
                                    "s", ["DocURL"]))[0]
            if url:
                return str(url)
        except Exception:
            pass
        try:
            attrs = (await self._call(dest, path, _DOC_IFACE, "GetAttributes"))[0]
            for key in ("DocURL", "URI", "URL"):
                if attrs.get(key):
                    return str(attrs[key])
        except Exception:
            pass
        return None

    # -- the read ----------------------------------------------------------
    async def _read(self) -> str | None:
        root = ("org.a11y.atspi.Registry", "/org/a11y/atspi/accessible/root")
        try:
            apps = await self._children(*root)
        except Exception:
            return None
        for dest, path in apps:
            try:
                name = str(await self._prop(dest, path, _ACC_IFACE, "Name") or "")
            except Exception:
                continue
            if not any(b in name.lower() for b in BROWSER_APPS):
                continue
            url = await self._walk_for_url(dest, path)
            if url:
                return url
        return None

    async def _walk_for_url(self, dest, path) -> str | None:
        """Breadth-first from the application, preferring the ACTIVE frame.

        Chromium and Firefox both bury the web document several levels under
        the frame, and both keep every background tab's document in the tree —
        so an unordered walk happily returns the wrong tab. Preferring the
        active frame is the AT-SPI analog of UIA's ElementFromHandle(hwnd).
        """
        try:
            frames = await self._children(dest, path)
        except Exception:
            return None
        ordered = []
        for f in frames:
            ordered.append((await self._is_active(*f), f))
        ordered.sort(key=lambda x: not x[0])          # active frames first
        for _active, frame in ordered:
            url = await self._descend(frame)
            if url:
                return url
        return None

    async def _descend(self, node) -> str | None:
        queue, seen, budget = [node], set(), self._max_nodes
        while queue and budget > 0:
            dest, path = queue.pop(0)
            if (dest, path) in seen:
                continue
            seen.add((dest, path))
            budget -= 1
            try:
                ifaces = await self._ifaces(dest, path)
            except Exception:
                continue
            if _DOC_IFACE in ifaces:
                url = await self._doc_url(dest, path)
                if url and url.lower().startswith(("http://", "https://")):
                    return url
            try:
                queue.extend(await self._children(dest, path))
            except Exception:
                continue
        return None

    def read(self) -> str | None:
        if self._loop is None:
            return None
        fut = asyncio.run_coroutine_threadsafe(self._read(), self._loop)
        try:
            return fut.result(timeout=self._timeout_s * 3)
        except Exception:
            return None


# ------------------------------- factory ----------------------------------
def get_source(name: str = "auto") -> UrlSource:
    if name == "auto":
        name = "uia" if os.name == "nt" else "atspi"
    if name == "uia":
        return UiaSource()
    if name == "atspi":
        return AtspiSource()
    raise SystemExit(f"unknown url source {name!r} (uia|atspi|auto)")


if __name__ == "__main__":
    src = get_source()
    ok, why = src.available()
    print(f"source={src.name} available={ok} ({why})")
    if ok:
        for _ in range(3):
            print("  read ->", src.read())
            time.sleep(1)
    src.close()
