# Sparrow PyInstaller spec (onedir) — the desktop build.
#
#   pyinstaller packaging/mnemos.spec
#
# Entry point is desktop_app.py, not run_all.py: the packaged product is a
# windowed app with a tray icon, and run_all.py is the console launcher for the
# scripted install. See desktop_app.py for what the desktop build deliberately
# leaves out (browser agent, Org Coordinator — both spawn
# `[sys.executable, "some_script.py"]` children, which re-executes Sparrow.exe
# when frozen).
#
# torch is BUNDLED, not excluded. It is ~900 MB and it is not optional:
# `silero_vad` imports torch at import time even when loading the ONNX model,
# so excluding it produces an app that cannot run VAD — no utterances, no
# memory, and no obvious error saying why. speechbrain (speaker ID) and
# sentence-transformers (semantic search) need it too.
#
# Model weights and Chromium are still NOT bundled; first launch fetches the
# weights via /bootstrap, which now calls app.services.model_fetch in-process.
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

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
# speechbrain is here because it discovers its own modules at import time by
# listing its package directory — a plain hiddenimport is not enough, and
# without it `import speechbrain` dies on
# "No such file or directory: .../_internal/speechbrain/utils" and the build
# silently has no speaker identification.
for pkg in ("app", "desktop_agent", "browser_agent", "mcp_server", "speechbrain"):
    try:
        hidden += collect_submodules(pkg)
    except Exception:
        hidden.append(pkg)

# Packages that load non-Python files at runtime. Missing these does not fail
# the build — it fails at first use, on the tester's machine.
datas = [
    (str(ROOT / "data" / "source_policies.json"), "data"),
    (str(ROOT / "data" / "model_prices.json"), "data"),
    # score_v2 raises ScoreV2NotReady without this; the people ranking then
    # degrades silently rather than loudly.
    (str(ROOT / "data" / "score_config.json"), "data"),
    (str(ROOT / "docs"), "docs"),
    (str(ROOT / "app" / "static"), "app/static"),
]
for pkg in ("silero_vad", "sentence_transformers", "lancedb", "faster_whisper",
            # pywebview ships JS bridge files it reads off disk at runtime.
            "webview"):
    try:
        datas += collect_data_files(pkg)
    except Exception as exc:              # pragma: no cover - build-time only
        print(f"[spec] no data files collected for {pkg}: {exc}")
try:
    # include_py_files: speechbrain reads its own source tree off disk, so the
    # .py files have to be laid out as real files beside the bundle, not only
    # frozen into the archive.
    datas += collect_data_files("speechbrain", include_py_files=True)
except Exception as exc:                  # pragma: no cover - build-time only
    print(f"[spec] no data files collected for speechbrain: {exc}")

# --- torch build guard -------------------------------------------------------
# A CUDA torch wheel drags 2-3 GB of GPU runtime into a bundle a tester
# downloads over a home connection, and none of it is reachable: ASR is
# CTranslate2 int8 on CPU, VAD is ONNX, the embedders are CPU.
#
# It cannot be stripped after the fact. Filtering the CUDA entries out of
# a.binaries builds cleanly and then fails at first import with
# "libtorch_cuda.so: cannot open shared object file" — libtorch links it
# directly. Measured on a real build here, not theorised. The wheel is the
# only lever, so refuse the build rather than ship 4 GB by accident.
#
# Windows PyPI wheels are already CPU-only, so a normal Windows build passes.
def _check_torch_build() -> None:
    import os as _os
    if _os.environ.get("QUILL_ALLOW_CUDA_BUILD"):
        print("[spec] QUILL_ALLOW_CUDA_BUILD set - bundling the installed torch.")
        return
    try:
        import torch
    except Exception as exc:
        raise SystemExit(f"packaging: torch must be importable to build ({exc})")
    cuda = getattr(getattr(torch, "version", None), "cuda", None)
    if cuda:
        raise SystemExit(
            f"packaging: this environment has a CUDA torch build "
            f"(torch {torch.__version__}, cuda {cuda}).\n"
            "  It adds ~2-3 GB of GPU runtime the app never uses, and it cannot\n"
            "  be removed from the bundle afterwards.\n"
            "  Install the CPU wheel, then rebuild:\n"
            "    pip install --force-reinstall torch torchaudio \\\n"
            "        --index-url https://download.pytorch.org/whl/cpu\n"
            "  (QUILL_ALLOW_CUDA_BUILD=1 bundles it anyway)")


_check_torch_build()


a = Analysis(  # noqa: F821
    [str(ROOT / "desktop_app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden + [
        "uvicorn", "fastapi", "anthropic", "lancedb", "sounddevice",
        "faster_whisper", "silero_vad", "torch", "torchaudio", "speechbrain",
        "sentence_transformers",
        "app.main",
        # The desktop shell. Optional at runtime (desktop_app degrades to the
        # browser), but the packaged build is the whole reason they exist.
        # pywebview picks its platform module by string at import, so the
        # Windows backend is invisible to PyInstaller's analysis; without it
        # the packaged app silently falls back to opening a browser tab.
        "webview", "webview.platforms.edgechromium", "webview.platforms.winforms",
        "clr_loader", "pystray", "pystray._win32", "PIL",
        # Windows integrations. Named explicitly because each is reached
        # through a late or dynamic import that static analysis misses:
        # voice.py does `import win32com.client` inside a function, the
        # notification mirror uses winsdk's namespace packages, and the UIA
        # bridge generates comtypes.gen modules at runtime.
        "win32com", "win32com.client", "pythoncom", "pywintypes",
        "winsdk", "comtypes", "comtypes.client", "pynput", "mss", "soundcard",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # TensorFlow is genuinely unused; torch is not — see the header.
    # `triton` is a GPU-only JIT and drops cleanly (measured: -646 MB).
    # Note that excluding "nvidia" here does NOT remove torch's CUDA payload —
    # that arrives as *binaries*, which a module exclude cannot touch. The
    # torch build guard below is what keeps it out.
    excludes=["tensorflow", "triton"],
    noarchive=False,
)
pyz = PYZ(a.pure)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Sparrow",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # No console: a terminal window behind the app is the single clearest
    # "this is unfinished" signal a non-technical tester gets.
    console=False,
    icon=str(ROOT / "packaging" / "mnemos.ico"),
)
coll = COLLECT(  # noqa: F821
    exe, a.binaries, a.datas,
    strip=False, upx=False, name="Sparrow",
)
if sys.platform == "darwin":
    app = BUNDLE(  # noqa: F821
        coll, name="Sparrow.app",
        icon=str(ROOT / "packaging" / "mnemos.ico"),
        bundle_identifier="app.ravenry.sparrow",
        info_plist={
            # macOS refuses the capture the product exists for unless the app
            # says why, and the refusal is silent — the prompt simply never
            # appears and the mic returns zeros.
            "NSMicrophoneUsageDescription":
                "Sparrow transcribes meetings you explicitly start recording.",
            "NSCameraUsageDescription":
                "Sparrow uses the camera only for capture sources you turn on.",
            "LSMinimumSystemVersion": "13.0",
        },
    )
