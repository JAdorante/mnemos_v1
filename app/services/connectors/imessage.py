"""iMessage connector — texts from Messages on a Mac, read-only.

There is no Apple API or OAuth for Messages. The only source is
``~/Library/Messages/chat.db`` on the Mac signed in to the account. It reaches
Sparrow one of two ways:

  on a Mac      this install reads the file itself (imessage_reader.py), on the
                scheduler's cadence. macOS guards it with Full Disk Access, which
                it never prompts for, so connect tests the read and hands back
                the steps on failure.
  hosted seat   QUILL_IMESSAGE_RELAY=1. A helper on the user's Mac pushes texts
                to the seat (imessage_relay.py), so they leave the Mac, and the
                user agrees to that at connect. Anywhere else it's unavailable.

Unlike the mail connectors this one lands message bodies: a text has no subject,
so metadata alone would say nothing. To hold that to a narrower line, every item
is stamped ``sensitive`` (the peer channel, team layer, fleet relay, records and
MCP all stop at that stamp), and these never land at all: reactions, business
short-code senders, one-time codes, and anything the secret scan flags.

Texts train this user's own model and no one else's (imessage_learning.py).

Nothing here can send a text.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

from app.config import settings
from app.services.connectors import imessage_learning as learning
from app.services.connectors import imessage_reader as reader
from app.services.connectors import imessage_relay as relay
from app.services.connectors import scheduler

SOURCE = "imessage.message"
MAC_ONLY_HINT = (
    "iMessage reads the Messages database on a Mac, so it only works when "
    "Sparrow runs on the Mac signed in to your account."
)
RELAY_NOTICE = (
    "Your Mac will copy new iMessage and SMS texts, including what they say, to "
    "this hosted Sparrow server about every 5 minutes, starting with the last "
    "7 days. Whoever runs the server can read them. They train your own "
    "personal model and no one else's, and a text a chat answer draws on goes "
    "to your chat model. One-time codes, reactions and business senders stay on "
    "your Mac. Disconnecting stops the copying, and you can delete the copied "
    "texts at the same time, but what your model already learned from them "
    "stays until it next retrains."
)

_sync_lock = threading.Lock()
_syncing = False


def _state_path() -> Path:
    return Path(settings.storage.data_dir) / "connectors" / "imessage" / "state.json"


def reads_locally() -> bool:
    # Not a file check: without Full Disk Access macOS refuses even the stat,
    # and that case has to reach connect so the user gets the steps.
    return sys.platform == "darwin" or bool(os.environ.get("QUILL_IMESSAGE_DB"))


def relay_mode() -> bool:
    return relay.enabled() and not reads_locally()


class ImessageConnector:
    id = "imessage"
    label = "iMessage"
    tool_names = ("iMessage", "Messages")
    availability = "ready"
    kind = "directory"
    category = "messages"
    source_prefixes = ("imessage.",)
    sync_interval_s = 300
    sync_blurb = ("new iMessage and SMS texts on this Mac, including what they "
                  "say and for training your own model,")
    description = (
        "Read-only texts from Messages on your Mac, kept in Sparrow's memory "
        "and used to train your own model. When a chat answer draws on a text, "
        "it goes to your chat model (Claude, unless you run local-only). "
        "Sparrow can't send texts."
    )
    capabilities = (
        "Remember iMessage and SMS conversations",
        "Close tasks when you text someone back",
        "Teach your own model how you write",
        "Skips one-time codes, reactions and business senders",
    )

    @property
    def push_only(self) -> bool:
        """Texts arrive from the Mac helper; the scheduler has nothing to poll."""
        return relay_mode()

    @property
    def consent_notice(self) -> str | None:
        return RELAY_NOTICE if relay_mode() else None

    def configured(self) -> bool:
        return reads_locally() or relay_mode()

    def connected(self) -> bool:
        if relay_mode():
            return relay.device() is not None
        if not reads_locally():
            return False
        try:
            return bool(json.loads(_state_path().read_text("utf-8")).get("connected"))
        except (OSError, ValueError, AttributeError):
            return False

    def status(self) -> dict[str, Any]:
        connected = self.connected()
        out = {
            "id": self.id,
            "label": self.label,
            "tool_names": list(self.tool_names),
            "availability": self.availability,
            "kind": self.kind,
            "category": self.category,
            "description": self.description,
            "capabilities": list(self.capabilities),
            "auth": "device",
            "configured": self.configured(),
            "connected": connected,
            "progress": {"running": _syncing},
            "push_only": self.push_only,
            "purgeable": True,
            "error": None,
        }
        if not self.configured():
            out["setup_hint"] = MAC_ONLY_HINT
        if not connected:
            return out
        try:
            from app.storage import get_store
            st = get_store().connector_sync_get(self.id) or {}
            n = int(st.get("items_landed") or 0)
            if relay_mode():
                out["status_detail"] = relay.status_line(n)
                out["error"] = relay.DEVICE_ERRORS.get((relay.device() or {}).get("error"))
            else:
                out["status_detail"] = (
                    f"{n} text{'' if n == 1 else 's'} kept." if n
                    else "No texts kept yet. Re-sync to pull them in.")
                out["error"] = st.get("last_error")
        except Exception as exc:
            print(f"[connectors] imessage status skipped ({exc}).")
        return out

    def begin_connect(self, *, public_base: str | None = None,
                      return_path: str | None = None) -> dict[str, Any]:
        if relay_mode():
            # Pairing goes through POST /connectors/imessage/connect, which
            # holds the consent step and knows the seat's public address.
            return {"ok": False, "error": "Connect iMessage from the webapp."}
        if not reads_locally():
            return {"ok": False, "error": MAC_ONLY_HINT}
        try:
            reader.open_messages().close()
        except reader.AccessError as exc:
            return {"ok": False, "error": str(exc), "needs_permission": True}
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"connected": True, "connected_at": time.time()}),
                       encoding="utf-8")
        tmp.replace(path)
        return {"ok": True, "mode": "device"}

    def complete_connect(self, code: str, state: str,
                         *, redirect_uri: str) -> dict[str, Any]:
        return {"ok": False, "error": "iMessage connects from a Mac, not by OAuth"}

    def sync(self) -> dict[str, Any]:
        global _syncing
        if not self.connected():
            return {"ok": False, "error": "not connected"}
        if relay_mode():
            return {"ok": True, "push_only": True}
        with _sync_lock:
            if _syncing:
                return {"ok": True, "running": True}
            _syncing = True

        def _worker():
            global _syncing
            try:
                scheduler.sync_connector(self, force=True)
            finally:
                _syncing = False

        threading.Thread(target=_worker, name="imessage-sync", daemon=True).start()
        return {"ok": True, "started": True}

    def disconnect(self) -> dict[str, Any]:
        if relay_mode():
            relay.revoke()
            return {"ok": True}
        try:
            _state_path().unlink()
        except FileNotFoundError:
            pass
        return {"ok": True}

    def after_land(self, item: dict, *, store=None) -> None:
        learning.learn_from(item, store=store)

    def purge(self, *, store=None) -> dict[str, Any]:
        """Disconnect and delete every text kept here, with what was extracted
        from them and the training data made from them. A model already trained
        keeps what it learned until it next retrains. The cursor goes too, so
        reconnecting starts over cleanly; it has to disconnect first or the next
        sync would land them straight back."""
        if store is None:
            from app.storage import get_store
            store = get_store()
        self.disconnect()
        dropped = store.purge_source(SOURCE)
        st = store.connector_sync_get(self.id) or {}
        store.connector_sync_set(
            self.id, cursor={}, clear_error=True,
            items_landed_add=-int(st.get("items_landed") or 0))
        return {"ok": True, "deleted": len(dropped.get("events") or []),
                "training_deleted": learning.forget(store=store)}

    # --- background capture (spec F1) ----------------------------------------
    def fetch_items(self, *, cursor: dict | None = None,
                    now: float | None = None) -> tuple[list[dict], dict]:
        now = float(now if now is not None else time.time())
        # The batch stays within the scheduler's per-sync limit because it
        # advances this cursor even past items it would truncate.
        items, new_cursor, _ = reader.fetch_batch(
            cursor, now, scheduler.MAX_ITEMS_PER_SYNC)
        return items, new_cursor


imessage = ImessageConnector()
