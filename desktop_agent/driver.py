"""The executor — the only place that actually touches the OS.

Every public method funnels through the same discipline: resolve through an
allowlist -> classify the risk tier -> (if mutating) pass the approval gate ->
execute with `shell=False` and args as a list -> audit the outcome. A BLOCKED
action never reaches execution; a denied approval never reaches execution.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import time
from pathlib import Path
from typing import Callable

from . import config as cfg
from . import guards
from .guards import Tier


def _wants_action(fn: Callable) -> bool:
    """True if an approval callback accepts an `action`/verb keyword.

    Lets the gate pass the verb (for granular autonomy) to callbacks that want
    it, while older `(summary, details)` callbacks keep working unchanged.
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "action" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


def _default_ask(summary: str, details: str = "", action: str | None = None) -> bool:
    """CLI approval prompt. Only an explicit 'approve'/'yes' proceeds."""
    print(f"\n[APPROVAL NEEDED] {summary}")
    if details:
        print(details)
    try:
        ans = input("  type 'approve' to proceed, anything else cancels > ").strip().lower()
    except EOFError:
        return False
    return ans in ("approve", "yes", "y")


class DesktopResult(dict):
    """Thin dict result: {ok, tier, action, detail, ...}. dict for easy logging."""


class DesktopDriver:
    def __init__(self,
                 on_log: Callable[[str], None] | None = None,
                 on_approve: Callable[[str, str], bool] | None = None,
                 jail_root: Path | None = None) -> None:
        self._log = on_log or (lambda s: print(s))
        self._ask = on_approve or _default_ask
        self._ask_wants_action = _wants_action(self._ask)
        self.jail = (jail_root or cfg.JAIL_ROOT).resolve()
        self.jail.mkdir(parents=True, exist_ok=True)
        cfg.SESSIONS_ROOT.mkdir(parents=True, exist_ok=True)
        self._audit_path = cfg.SESSIONS_ROOT / "desktop_audit.jsonl"
        self.actions = 0
        # Monotonic per-driver task id so telemetry can group audited actions by
        # task (avg actions/task, budget-exhaustion rate). Bumped by new_task().
        self._task_id = 1

    # --- internals ---------------------------------------------------------
    def _audit(self, record: dict) -> None:
        record = {"ts": time.time(), "task_id": self._task_id, **record}
        try:
            with self._audit_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception:
            pass  # never let auditing failure block or crash an action

    def _refuse(self, action: str, reason: str, **extra) -> DesktopResult:
        self._log(f"   [refused] {reason}")
        res = DesktopResult(ok=False, action=action, tier=Tier.BLOCKED.value,
                            detail=reason, **extra)
        self._audit({"outcome": "blocked", **res})
        return res

    def _budget_ok(self, action: str) -> DesktopResult | None:
        if self.actions >= cfg.MAX_ACTIONS_PER_TASK:
            return self._refuse(action, f"action budget exhausted "
                                f"({cfg.MAX_ACTIONS_PER_TASK}); call new_task()")
        return None

    def _gate(self, tier: Tier, summary: str, details: str = "",
              verb: str | None = None) -> bool:
        """True if the action may proceed. Read-only auto-passes.

        `verb` is the action name; it's handed to callbacks that opt in (see
        _wants_action) so the approver can apply per-verb autonomy policy.
        """
        if tier == Tier.READ_ONLY or not cfg.REQUIRE_APPROVAL:
            return True
        if self._ask_wants_action:
            ok = bool(self._ask(summary, details, action=verb))
        else:
            ok = bool(self._ask(summary, details))
        self._log("   approved" if ok else "   denied")
        return ok

    def new_task(self) -> None:
        """Reset the per-task action budget and start a new telemetry task."""
        self.actions = 0
        self._task_id += 1

    # --- capabilities ------------------------------------------------------
    def make_dir(self, name: str) -> DesktopResult:
        """Create a project folder inside the jail."""
        if (b := self._budget_ok("make_dir")):
            return b
        target = guards.safe_child(self.jail, name)
        if target is None:
            return self._refuse("make_dir", f"path escapes jail: {name!r}")
        if not self._gate(Tier.MUTATING, f"create folder {target}",
                          verb="make_dir"):
            return DesktopResult(ok=False, action="make_dir", detail="denied")
        self.actions += 1
        target.mkdir(parents=True, exist_ok=True)
        self._log(f"   created {target}")
        res = DesktopResult(ok=True, action="make_dir", tier=Tier.MUTATING.value,
                            detail=str(target), path=str(target))
        self._audit({"outcome": "ok", **res})
        return res

    def write_file(self, path: str, content: str,
                   project: str | None = None) -> DesktopResult:
        """Create or overwrite a text file inside the jail.

        The agent's way to author source (index.html, app.js, README) WITHOUT
        smuggling content through run_command/echo — which the shell-metachar
        guard rightly refuses. Jailed, size-capped, and approval-gated like any
        mutating action. Parent folders are created as needed.
        """
        if (b := self._budget_ok("write_file")):
            return b
        base = self.jail
        if project:
            base = guards.safe_child(self.jail, project)
            if base is None:
                return self._refuse("write_file", f"bad project name {project!r}",
                                    path=path)
        target = guards.safe_child(base, path)
        if target is None:
            return self._refuse("write_file", f"path escapes jail: {path!r}",
                                path=path)
        if target.is_dir():
            return self._refuse("write_file", f"path is a directory: {target}",
                                path=path)
        # Defense in depth: never author into a secret/sensitive path, even in-jail.
        if any(m in str(target).lower() for m in cfg.SECRET_MARKERS):
            return self._refuse("write_file",
                                f"target reaches a sensitive path: {target}", path=path)
        content = content or ""
        nbytes = len(content.encode("utf-8"))
        if nbytes > cfg.MAX_FILE_BYTES:
            return self._refuse("write_file", f"file too large: {nbytes} bytes > "
                                f"{cfg.MAX_FILE_BYTES} limit", path=path)
        preview = next((ln for ln in content.splitlines() if ln.strip()), "(empty)")
        summary = (f"write {nbytes} bytes to {target} "
                   f"({'overwrite' if target.exists() else 'new'})")
        if not self._gate(Tier.MUTATING, summary, f"first line: {preview[:80]}",
                          verb="write_file"):
            return DesktopResult(ok=False, action="write_file", detail="denied",
                                 path=str(target))
        self.actions += 1
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except Exception as exc:
            return self._refuse("write_file", f"write failed: {exc}", path=path)
        self._log(f"   wrote {target} ({nbytes} bytes)")
        res = DesktopResult(ok=True, action="write_file", tier=Tier.MUTATING.value,
                            detail=str(target), path=str(target), bytes=nbytes)
        self._audit({"outcome": "ok", **res})
        return res

    def _open_targets_ok(self, key: str, args: list[str] | None) -> str | None:
        """Enforce the app's capability contract on each model-supplied target.

        Resolves the one filesystem fact the pure policy needs (is it a folder?)
        and defers the decision to guards.open_target_allowed. A path with no
        extension that doesn't exist yet is treated as a folder-open intent
        (matches the make_dir -> launch_app flow). Flags are exempt.
        """
        for a in args or []:
            s = str(a)
            if s.startswith("-"):
                continue
            p = Path(s)
            is_dir = p.is_dir() or (not p.suffix and not p.exists())
            reason = guards.open_target_allowed(key, p.suffix, is_dir)
            if reason:
                return reason
        return None

    def launch_app(self, key: str, args: list[str] | None = None) -> DesktopResult:
        """Launch a registry app — or any installed app that clears the
        discovery criteria (vetted, launch-only, human-gated on first use)."""
        if (b := self._budget_ok("launch_app")):
            return b
        from . import access
        if access.app_disabled(key):
            return self._refuse("launch_app",
                                f"app {key!r} is disabled in Desktop Access", app=key)
        args = args or []
        exe = cfg.resolve_app_path(key)
        discovered: dict | None = None
        if exe is None:
            # Not in the registry: fall through to criteria-based discovery
            # (resolve from the machine's own registration channels, then vet)
            # instead of refusing on enumeration alone.
            if not cfg.APP_DISCOVERY:
                return self._refuse("launch_app",
                                    f"app {key!r} not on allowlist or not installed",
                                    app=key)
            from . import discovery
            discovered, why = discovery.discover_app(key, self.jail)
            if discovered is None:
                if why:
                    return self._refuse(
                        "launch_app",
                        f"app {key!r} refused by discovery policy: {why}", app=key)
                return self._refuse(
                    "launch_app",
                    f"app {key!r} not found on this machine "
                    "(searched PATH, App Paths, Start Menu)", app=key)
            exe = discovered["path"]
        bad = guards.check_launch_args(args, self.jail)  # only agent args are jailed
        if bad:
            return self._refuse("launch_app", bad, app=key)
        cap = self._open_targets_ok(key, args)  # what may this app be opened ON?
        if cap:
            return self._refuse("launch_app", cap, app=key)
        # Trusted built-in args (e.g. a UWP AUMID for explorer) prefix the agent's;
        # they come from config, not the model, so they bypass the jail check but
        # are shown in the approval prompt for transparency.
        builtin = cfg.APP_LAUNCH_ARGS.get(key.lower(), [])
        launch_args = [*builtin, *args]
        summary = f"launch {key} ({exe})" + (f" with {launch_args}" if launch_args else "")
        # First use of a discovered app gates under a verb no autonomy level
        # auto-approves (desktop_autoapprove fails safe on unknown verbs), so a
        # human always sees it once; an approved launch is granted below and
        # later launches follow normal launch_app autonomy.
        verb = "launch_app"
        if discovered is not None and not access.app_granted(key):
            verb = "launch_unlisted_app"
            summary += (f"  [not in the app registry; discovered via "
                        f"{discovered['source']} — first use]")
        if not self._gate(Tier.MUTATING, summary, verb=verb):
            return DesktopResult(ok=False, action="launch_app", detail="denied")
        self.actions += 1
        from . import ghost_win
        ghosting = ghost_win.ghostable(key)
        win_before = ghost_win.snapshot_windows() if ghosting else set()
        try:
            subprocess.Popen([exe, *launch_args])  # detached; shell=False
        except Exception as exc:
            return self._refuse("launch_app", f"launch failed: {exc}", app=key)
        if discovered is not None and not access.app_granted(key):
            access.grant_app(key, exe, source=discovered["source"])
        ghost_note = ""
        if ghosting:
            g = ghost_win.park_new_windows(key, win_before)
            if g.get("ok"):
                ghost_note = (" [ghosted: window parked off-screen, streaming "
                              "to the chat pane — interact via ui_scan/"
                              "ui_invoke/ui_set_text, NOT the screenshot]")
                self._log(f"   ghosted {g['windows']} window(s) — view in chat pane")
            else:
                ghost_note = f" [not ghosted: {g.get('reason')}]"
                self._log(f"   ghost skipped ({g.get('reason')})")
        self._log(f"   launched {key}")
        res = DesktopResult(ok=True, action="launch_app", tier=Tier.MUTATING.value,
                            detail=summary + ghost_note, app=key, args=launch_args)
        self._audit({"outcome": "ok", **res})
        return res

    def run_command(self, argv: list[str], cwd: str | None = None) -> DesktopResult:
        """Run an allowlisted shell command, args-as-list, confined to the jail."""
        if (b := self._budget_ok("run_command")):
            return b
        if not argv:
            return self._refuse("run_command", "empty command")
        workdir = Path(cwd).resolve() if cwd else self.jail
        if not guards.within_jail(workdir, self.jail):
            return self._refuse("run_command", f"cwd outside jail: {workdir}",
                                argv=argv)
        tier, reason = guards.classify_command(argv)
        if tier == Tier.BLOCKED:
            return self._refuse("run_command", reason, argv=argv)
        if not self._gate(tier, f"run {' '.join(argv)}  (cwd={workdir})",
                          f"classified: {reason}", verb="run_command"):
            return DesktopResult(ok=False, action="run_command", detail="denied",
                                 argv=argv)
        self.actions += 1
        try:
            proc = subprocess.run(argv, cwd=str(workdir), shell=False,
                                  capture_output=True, text=True,
                                  timeout=cfg.COMMAND_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return self._refuse("run_command",
                                f"timed out after {cfg.COMMAND_TIMEOUT_S}s", argv=argv)
        except Exception as exc:
            return self._refuse("run_command", f"exec error: {exc}", argv=argv)
        out = (proc.stdout or "")[-4000:]
        self._log(f"   exit {proc.returncode}: {out.strip()[:200]}")
        res = DesktopResult(ok=(proc.returncode == 0), action="run_command",
                            tier=tier.value, detail=reason, code=proc.returncode,
                            stdout=out, stderr=(proc.stderr or "")[-2000:], argv=argv)
        self._audit({"outcome": "ok" if proc.returncode == 0 else "nonzero",
                     "action": "run_command", "argv": argv,
                     "code": proc.returncode, "tier": tier.value})
        return res

    def list_dir(self, name: str = "") -> DesktopResult:
        """Read-only listing of a jailed folder (no prompt)."""
        target = self.jail if not name else guards.safe_child(self.jail, name)
        if target is None:
            return self._refuse("list_dir", f"path escapes jail: {name!r}")
        if not target.exists():
            return DesktopResult(ok=False, action="list_dir", detail="not found")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in target.iterdir())
        return DesktopResult(ok=True, action="list_dir", tier=Tier.READ_ONLY.value,
                             detail=str(target), entries=entries)

    # --- pixel UI (screenshot / click / type) --------------------------------
    def screenshot_bytes(self) -> DesktopResult:
        """Capture the primary monitor (read-only)."""
        if not cfg.PIXEL_UI:
            return self._refuse("screenshot", "pixel UI disabled (QUILL_DESKTOP_UI=0)")
        try:
            from . import pixel

            data, size = pixel.screenshot_bytes()
            self._log(f"   screenshot {size[0]}x{size[1]} ({len(data)} bytes)")
            return DesktopResult(ok=True, action="screenshot",
                                 tier=Tier.READ_ONLY.value,
                                 detail=f"{size[0]}x{size[1]}",
                                 bytes=len(data), width=size[0], height=size[1],
                                 image=data)
        except Exception as exc:
            return self._refuse("screenshot", f"{type(exc).__name__}: {exc}")

    def click_at(self, x: int, y: int, button: str = "left") -> DesktopResult:
        if not cfg.PIXEL_UI:
            return self._refuse("click_at", "pixel UI disabled (QUILL_DESKTOP_UI=0)")
        if (b := self._budget_ok("click_at")):
            return b
        from . import pixel

        try:
            x, y = pixel.coerce_coords(x, y)
        except (TypeError, ValueError):
            return self._refuse(
                "click_at", f"coordinates must be integers, got ({x!r}, {y!r})")
        bad = pixel.check_coords(x, y)
        if bad:
            return self._refuse("click_at", bad)
        btn = (button or "left").lower()
        summary = f"click {btn} at ({int(x)}, {int(y)})"
        if not self._gate(Tier.MUTATING, summary, verb="click_at"):
            return DesktopResult(ok=False, action="click_at", detail="denied")
        self.actions += 1
        try:
            pixel.click_at(x, y, btn)
        except Exception as exc:
            return self._refuse("click_at", f"{type(exc).__name__}: {exc}")
        self._log(f"   {summary}")
        res = DesktopResult(ok=True, action="click_at", tier=Tier.MUTATING.value,
                            detail=summary, x=int(x), y=int(y), button=btn)
        self._audit({"outcome": "ok", **res})
        return res

    def type_text(self, text: str) -> DesktopResult:
        if not cfg.PIXEL_UI:
            return self._refuse("type_text", "pixel UI disabled (QUILL_DESKTOP_UI=0)")
        if (b := self._budget_ok("type_text")):
            return b
        from . import pixel

        bad = pixel.check_type_text(text)
        if bad:
            return self._refuse("type_text", bad)
        preview = (text or "")[:80] + ("…" if len(text or "") > 80 else "")
        summary = f"type text ({len(text)} chars): {preview!r}"
        if not self._gate(Tier.MUTATING, summary, details=text, verb="type_text"):
            return DesktopResult(ok=False, action="type_text", detail="denied")
        self.actions += 1
        try:
            pixel.type_text(text)
        except Exception as exc:
            return self._refuse("type_text", f"{type(exc).__name__}: {exc}")
        self._log(f"   typed {len(text)} chars")
        res = DesktopResult(ok=True, action="type_text", tier=Tier.MUTATING.value,
                            detail=summary, chars=len(text))
        self._audit({"outcome": "ok", **res})
        return res

    def press_key(self, key: str) -> DesktopResult:
        if not cfg.PIXEL_UI:
            return self._refuse("press_key", "pixel UI disabled (QUILL_DESKTOP_UI=0)")
        if (b := self._budget_ok("press_key")):
            return b
        from . import pixel

        bad = pixel.check_key(key)
        if bad:
            return self._refuse("press_key", bad)
        summary = f"press key {key!r}"
        if not self._gate(Tier.MUTATING, summary, verb="press_key"):
            return DesktopResult(ok=False, action="press_key", detail="denied")
        self.actions += 1
        try:
            pixel.press_key(key)
        except Exception as exc:
            return self._refuse("press_key", f"{type(exc).__name__}: {exc}")
        self._log(f"   {summary}")
        res = DesktopResult(ok=True, action="press_key", tier=Tier.MUTATING.value,
                            detail=summary, key=key)
        self._audit({"outcome": "ok", **res})
        return res

    # --- UI Automation: drive a specific app window, no mouse/focus taken ---
    def ui_scan(self, app: str, title: str = "") -> DesktopResult:
        """List an allowlisted app window's controls (read-only observation)."""
        if (b := self._budget_ok("ui_scan")):
            return b
        app = (app or "").strip().lower()
        if app not in cfg.APP_CANDIDATES:
            return self._refuse("ui_scan", f"app '{app}' is not allowlisted")
        from . import uia

        if not uia.available():
            return self._refuse("ui_scan", "UI Automation unavailable")
        try:
            s = uia.scan(app, title_hint=title or "")
        except Exception as exc:
            return self._refuse("ui_scan", f"{type(exc).__name__}: {exc}")
        if not s.get("ok"):
            return self._refuse("ui_scan", s.get("reason", "scan failed"))
        self.actions += 1
        obs = uia.render(s)
        if len(obs) > 1800:
            obs = obs[:1800] + "\n… (truncated)"
        self._ghost_frame()
        wtitle = s["window"]["title"]
        self._log(f"   ui_scan '{wtitle}': {len(s.get('controls', []))} control(s)")
        res = DesktopResult(ok=True, action="ui_scan", tier=Tier.READ_ONLY.value,
                            detail=obs, window=wtitle)
        self._audit({"outcome": "ok", "action": "ui_scan", "ok": True,
                     "tier": Tier.READ_ONLY.value, "window": wtitle,
                     "controls": len(s.get("controls", []))})
        return res

    def ui_invoke(self, control_id) -> DesktopResult:
        """Activate a control from the last ui_scan (button/menuitem/tab...)."""
        if (b := self._budget_ok("ui_invoke")):
            return b
        from . import uia

        try:
            cid = int(control_id)
        except (TypeError, ValueError):
            return self._refuse("ui_invoke", f"control_id must be an integer, "
                                f"got {control_id!r}")
        label = uia.describe(cid)
        window = uia.last_window_title() or uia._last_app or "?"
        summary = f"activate {label} in the '{window}' window (no mouse taken)"
        if not self._gate(Tier.MUTATING, summary, verb="ui_invoke"):
            return DesktopResult(ok=False, action="ui_invoke", detail="denied")
        self.actions += 1
        try:
            how = uia.invoke(cid)
        except Exception as exc:
            return self._refuse("ui_invoke", f"{type(exc).__name__}: {exc}")
        self._log(f"   {summary} — {how}")
        self._ghost_frame()
        res = DesktopResult(ok=True, action="ui_invoke", tier=Tier.MUTATING.value,
                            detail=f"{how}: {label}", control_id=cid)
        self._audit({"outcome": "ok", **res})
        return res

    def ui_set_text(self, control_id, text: str) -> DesktopResult:
        """Set an editable control's text from the last ui_scan. REPLACES the
        control's current content — the approval prompt says so explicitly."""
        if (b := self._budget_ok("ui_set_text")):
            return b
        from . import pixel, uia

        bad = pixel.check_type_text(text)
        if bad:
            return self._refuse("ui_set_text", bad)
        try:
            cid = int(control_id)
        except (TypeError, ValueError):
            return self._refuse("ui_set_text", f"control_id must be an integer, "
                                f"got {control_id!r}")
        label = uia.describe(cid)
        window = uia.last_window_title() or uia._last_app or "?"
        preview = (text or "")[:80] + ("…" if len(text or "") > 80 else "")
        summary = (f"REPLACE the text of {label} in the '{window}' window with "
                   f"({len(text)} chars): {preview!r}")
        if not self._gate(Tier.MUTATING, summary, details=text, verb="ui_set_text"):
            return DesktopResult(ok=False, action="ui_set_text", detail="denied")
        self.actions += 1
        try:
            how = uia.set_value(cid, text)
        except Exception as exc:
            return self._refuse("ui_set_text", f"{type(exc).__name__}: {exc}")
        self._log(f"   ui_set_text {label}: {how} ({len(text)} chars)")
        self._ghost_frame()
        res = DesktopResult(ok=True, action="ui_set_text", tier=Tier.MUTATING.value,
                            detail=f"{how}: {label} ({len(text)} chars)",
                            control_id=cid, chars=len(text))
        self._audit({"outcome": "ok", **res})
        return res

    def _ghost_frame(self) -> None:
        """After a UIA action, refresh the chat pane's frame — only for windows
        the ghost path parked (the user's own windows never stream)."""
        try:
            from . import ghost_win, uia
            hwnd = uia.last_window_hwnd()
            if hwnd and hwnd in ghost_win.parked_apps():
                ghost_win.publish_frame(hwnd)
        except Exception:
            pass
