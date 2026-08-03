"""Pre-pixel privacy gate — rule-based, hard-blocking, LABELED.

Runs before any frame bytes are encoded, saved, OCR'd, or shown to a model.
On a match: no pixels leave RAM, no OCR happens, and a
`captures(kind='excluded', exclusion_rule=<id>)` row is written so the
timeline shows a labeled redaction instead of an unexplained hole.

Default-excluded surfaces are ONLY credential/financial ones (password
managers, key files, OS credential dialogs, private-browsing windows, known
banking domains). Whole categories like terminals are NOT silently skipped —
broader exclusions are the user's call via the editable blocklist
(data/privacy_blocklist.json, also exposed in the UI).
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

from app.services import redact as _secrets

# OS credential surfaces — small and specific on purpose.
_CREDENTIAL_DIALOG = re.compile(
    r"(?i)^(?:windows security|user account control|credential manager)\b")
_PRIVATE_BROWSING = re.compile(
    r"(?i)\b(?:inprivate|incognito|private browsing)\b")

# Registrable domains that are credential/financial by nature. Used once L0
# supplies url_domain (Phase B); harmless before that.
_BANKING_DOMAINS = frozenset({
    "chase.com", "bankofamerica.com", "wellsfargo.com", "citi.com",
    "capitalone.com", "usbank.com", "pnc.com", "fidelity.com", "schwab.com",
    "vanguard.com", "etrade.com", "robinhood.com", "paypal.com", "venmo.com",
    "wise.com", "coinbase.com", "kraken.com",
})


class PrivacyGate:
    def __init__(self, blocklist_path: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._explicit_path = Path(blocklist_path) if blocklist_path else None
        self._user: dict[str, list[str]] = {"titles": [], "apps": [],
                                            "domains": []}
        self._loaded = False

    def _path(self) -> Path:
        if self._explicit_path is not None:
            return self._explicit_path
        from app.config import settings
        return Path(settings.storage.data_dir) / "privacy_blocklist.json"

    def _load(self, force: bool = False) -> None:
        with self._lock:
            if self._loaded and not force:
                return
            out = {"titles": [], "apps": [], "domains": []}
            try:
                p = self._path()
                if p.is_file():
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    for k in out:
                        vals = raw.get(k) or []
                        out[k] = [str(v).strip().lower() for v in vals
                                  if str(v).strip()]
            except Exception as exc:
                print(f"[privacy_gate] blocklist load skipped ({exc}).")
            self._user = out
            self._loaded = True

    # ------------------------------ the gate ------------------------------
    def check(self, window_title: str = "", app_exe: str = "",
              url_domain: str | None = None) -> str | None:
        """Rule id when this surface must not be captured, else None."""
        title = window_title or ""
        if _secrets.is_sensitive_window(title):
            return "builtin:sensitive_title"
        if _CREDENTIAL_DIALOG.search(title):
            return "builtin:credential_dialog"
        if _PRIVATE_BROWSING.search(title):
            return "builtin:private_browsing"
        dom = (url_domain or "").strip().lower()
        if dom and (dom in _BANKING_DOMAINS
                    or any(dom.endswith("." + b) for b in _BANKING_DOMAINS)):
            return "builtin:banking_domain"
        self._load()
        tl, al = title.lower(), (app_exe or "").lower()
        with self._lock:
            for s in self._user["titles"]:
                if s in tl:
                    return f"user:title:{s}"
            for s in self._user["apps"]:
                if s and s in al:
                    return f"user:app:{s}"
            for s in self._user["domains"]:
                if dom and (dom == s or dom.endswith("." + s)):
                    return f"user:domain:{s}"
        return None

    def record_exclusion(self, rule_id: str, *, window_id: str = "",
                         ts_ms: int | None = None,
                         meta_event_id: int | None = None) -> str | None:
        """Write the labeled `excluded` capture row. Best-effort: the gate's
        BLOCK decision already happened; a store hiccup must not unblock it.
        Deliberately stores no title/app text — the row says 'something was
        here and rule X hid it', nothing more."""
        try:
            from app.perception.schemas import Capture
            from app.perception.store import get_pstore
            cap = Capture(ts_utc=int(ts_ms if ts_ms is not None
                                     else time.time() * 1000),
                          window_id=str(window_id or ""),
                          meta_event_id=meta_event_id,
                          kind="excluded", trigger="privacy_gate",
                          exclusion_rule=rule_id)
            return get_pstore().insert_capture(cap)
        except Exception as exc:
            print(f"[privacy_gate] exclusion record skipped ({exc}).")
            return None

    # ------------------------------ user rules ----------------------------
    def list_rules(self) -> dict:
        self._load(force=True)
        with self._lock:
            return {
                "builtin": ["builtin:sensitive_title",
                            "builtin:credential_dialog",
                            "builtin:private_browsing",
                            "builtin:banking_domain"],
                "user": {k: list(v) for k, v in self._user.items()},
            }

    def add_user_rule(self, kind: str, value: str) -> dict:
        if kind not in ("titles", "apps", "domains"):
            raise ValueError(f"kind must be titles|apps|domains, got {kind!r}")
        v = (value or "").strip().lower()
        if not v:
            raise ValueError("empty blocklist value")
        self._load(force=True)
        with self._lock:
            if v not in self._user[kind]:
                self._user[kind].append(v)
            self._save_locked()
        self._supervise("exclusion_added", kind, v)
        return self.list_rules()

    def remove_user_rule(self, kind: str, value: str) -> dict:
        if kind not in ("titles", "apps", "domains"):
            raise ValueError(f"kind must be titles|apps|domains, got {kind!r}")
        v = (value or "").strip().lower()
        self._load(force=True)
        with self._lock:
            self._user[kind] = [x for x in self._user[kind] if x != v]
            self._save_locked()
        return self.list_rules()

    def _save_locked(self) -> None:
        try:
            from app.atomic_json import write_json
            write_json(self._path(), self._user, sort_keys=True)
        except Exception as exc:
            print(f"[privacy_gate] blocklist save failed ({exc}).")

    @staticmethod
    def _supervise(kind: str, target_type: str, target_id: str) -> None:
        try:
            from app.perception.schemas import SupervisionEvent, now_ms
            from app.perception.store import get_pstore
            get_pstore().add_supervision(SupervisionEvent(
                ts_utc=now_ms(), kind=kind, target_type=target_type,
                target_id=target_id))
        except Exception:
            pass


gate = PrivacyGate()
