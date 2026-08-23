# Mnemos PyInstaller spec (onedir). Do not bundle Whisper weights or Chromium —
# first launch downloads them via /bootstrap.
#   pyinstaller packaging/mnemos.spec
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — PyInstaller injects SPECPATH

# WS-C: one version constant. Read app/version.py textually rather than
# importing app (PyInstaller runs this spec before the app is importable), so
# the installer and the running app can never drift.
def _app_version() -> str:
    import re as _re
    text = (ROOT / "app" / "version.py").read_text(encoding="utf-8")
    m = _re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        raise SystemExit("packaging: could not read __version__ from app/version.py")
    return m.group(1)


VERSION = _app_version()
(ROOT / "dist").mkdir(exist_ok=True)
# The Inno script reads this file so `iscc` needs no separate bump.
(ROOT / "dist" / "VERSION.txt").write_text(VERSION, encoding="utf-8")

hidden = []
for pkg in ("app", "desktop_agent", "browser_agent", "mcp_server"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        hidden.append(pkg)

a = Analysis(  # noqa: F821
    [str(ROOT / "run_all.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / "data" / "source_policies.json"), "data"),
        (str(ROOT / "data" / "model_prices.json"), "data"),
        (str(ROOT / "docs"), "docs"),
        (str(ROOT / "app" / "static"), "app/static"),
    ],
    hiddenimports=hidden + [
        "uvicorn", "fastapi", "anthropic", "lancedb", "sounddevice",
        "faster_whisper", "silero_vad",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["torch", "tensorflow"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Mnemos",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    icon=None,
)
coll = COLLECT(  # noqa: F821
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Mnemos",
)
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll, name="Mnemos.app", icon=None, bundle_identifier="local.mnemos",
    )
