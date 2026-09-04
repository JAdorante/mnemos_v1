"""Frozen-build helpers — where the app is, and where it may write.

A PyInstaller build is not a checkout, and every frozen bug in this repo so far
came from code that assumed it was:

* ``sys.executable`` is ``Sparrow.exe``, not a Python interpreter, so
  ``[sys.executable, "scripts/foo.py"]`` relaunches the whole app.
* ``__file__`` points inside the bundle, and ``parents[2]`` from a service
  module is no longer the repo root.
* The install directory is **read-only** for the user running the app. Writing
  ``data/`` relative to the working directory lands in ``C:\\Program Files``
  and fails, or lands wherever the shortcut happened to start.

Three facts, three functions, one place.

The per-user location is deliberately **Local**, not Roaming. A memory
directory is gigabytes of meeting audio and frames; on a managed corporate
network Roaming replicates the user's profile to a file server, which would
both break the install and quietly copy the one thing this product promises
never leaves the machine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_DIR_NAME = "Sparrow"
LEGACY_APP_DIR_NAME = "Mnemos"


def is_frozen() -> bool:
    """True inside a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path:
    """Where the app's read-only files live (shipped tables, docs, static).

    In a onedir build PyInstaller sets ``sys._MEIPASS`` to the directory the
    executable's payload was unpacked into; in a checkout it is the repo root.
    """
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", None) or Path(sys.executable).parent)
    return Path(__file__).resolve().parents[1]


def user_data_root() -> Path:
    """Per-user writable directory for everything this install produces."""
    override = (os.environ.get("QUILL_USER_DATA_ROOT") or "").strip()
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA")
                    or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME")
                    or Path.home() / ".local" / "share")
    new = base / APP_DIR_NAME
    old = base / LEGACY_APP_DIR_NAME
    # Keep using an existing Mnemos data dir so a rebranded build does not
    # orphan a tester's memory.
    if not new.exists() and old.exists():
        return old
    return new


def default_data_dir() -> Path:
    """What ``QUILL_DATA_DIR`` should be when nobody has set it.

    A checkout keeps writing ``./data`` — developers, tests and the scripted
    tester install all depend on that. Only the frozen build relocates.
    """
    if is_frozen():
        return user_data_root() / "data"
    return Path("data")


def apply_env_defaults() -> dict[str, str]:
    """Seed the environment for a frozen run. Call BEFORE importing app.config.

    ``app.config`` freezes its dataclasses at import, so anything set after the
    first ``from app.config import settings`` anywhere in the process is simply
    ignored. Returns what it set, for logging.
    """
    applied: dict[str, str] = {}
    if not is_frozen():
        return applied
    root = user_data_root()
    if not (os.environ.get("QUILL_DATA_DIR") or "").strip():
        data = default_data_dir()
        data.mkdir(parents=True, exist_ok=True)
        os.environ["QUILL_DATA_DIR"] = str(data)
        applied["QUILL_DATA_DIR"] = str(data)
    if not (os.environ.get("QUILL_CREDENTIALS_FILE") or "").strip():
        # icloud_account._cred_path() resolves a relative name against
        # parents[2] of a service module — the bundle in a frozen build, which
        # is read-only and replaced on upgrade. The tester's key would be
        # unwritable, or written once and silently lost on the next install.
        root.mkdir(parents=True, exist_ok=True)
        creds = root / ".credentials.env"
        os.environ["QUILL_CREDENTIALS_FILE"] = str(creds)
        applied["QUILL_CREDENTIALS_FILE"] = str(creds)
    return applied


def env_file() -> Path:
    """The tester's `.env`. Beside the checkout, or per-user when frozen.

    It holds their key, so it must never live in a directory the installer
    would replace on upgrade.
    """
    if is_frozen():
        return user_data_root() / ".env"
    return bundle_root() / ".env"
