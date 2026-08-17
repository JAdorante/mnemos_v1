# Mnemos PyInstaller spec (onedir). Do not bundle Whisper weights or Chromium —
# first launch downloads them via /bootstrap.
#   pyinstaller packaging/mnemos.spec
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent  # noqa: F821 — PyInstaller injects SPECPATH

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
