# Packaging Sparrow for testers

## Windows (`SparrowSetup.exe`)

```
pip install -r requirements.txt -r packaging/requirements-desktop.txt
pip install torch --index-url https://download.pytorch.org/whl/cpu   # see below
pyinstaller packaging/mnemos.spec                                     # -> dist/Sparrow/
iscc packaging/mnemos.iss                                             # -> dist/SparrowSetup.exe
```

The entry point is **`desktop_app.py`**, not `run_all.py`: the packaged product is a windowed app (pywebview, Edge WebView2) with a tray icon — Open / Stop capture / Quit — and `console=False`, because a terminal behind the app is the clearest "unfinished software" signal a non-technical tester gets. `run_all.py` remains the console launcher for the scripted install.

**torch is bundled, not excluded.** `silero_vad` imports torch at import time *even when loading the ONNX model*, so a bundle without it starts fine, serves `/health` fine, and hears nothing — no VAD, no utterances, no memory, and no error saying why. `speechbrain` and `sentence-transformers` need it too. Build against the **CPU-only wheel**: the CUDA stack measured 3.3 GB of a 5.2 GB bundle and not one byte is reachable (ASR is CTranslate2 int8, VAD is ONNX). The spec excludes `nvidia`/`triton` as a backstop.

`Sparrow.exe --self-test` reports whether the build can load each critical module and whether the weights are cached. Run it after every spec change; CI does.

The installer does **not** bundle model weights or Chromium. First launch opens `/bootstrap`, which calls `app.services.model_fetch` **in-process** — it cannot shell out, because `scripts/` is not in the bundle and a frozen `sys.executable` is `Sparrow.exe`. The browser agent and Org Coordinator are not part of the desktop build for the same reason (both spawn `[sys.executable, "some_script.py"]` children).

Per-user state lives in `%LOCALAPPDATA%\Sparrow` — `app/runtime.py` relocates the data dir and `.credentials.env` there before `app.config` freezes, because the install directory is read-only to the user and is replaced on upgrade. **Local, not Roaming**: a memory directory is gigabytes of meeting audio, and Roaming would replicate it to a corporate file server.

### Still required before handing this to anyone

- **Code signing.** Unsigned means "Windows protected your PC" on first launch. For a firm evaluating a tool that listens to their meetings, that is the wrong first impression. Azure Trusted Signing is wired into `release.yml` on `v*` tags; start the Ravenry identity verification today.
- A run on a machine that is not the build machine.

**Install link.** Tag a release (`git tag v0.4.1 && git push origin v0.4.1`). CI builds `SparrowSetup.exe`, signs it when the Azure secrets are present, and attaches it to the GitHub Release. The tester URL is:

`https://github.com/JAdorante/mnemos_v1/releases/latest`

Ollama is not bundled. The installer is per-user (no admin prompt). First launch pulls local text/vision models only if `ollama.exe` is already installed; otherwise the app runs cloud-only. The scripted `install.bat` path still installs Ollama via winget for developer checkouts.

Uninstall: Inno removes the app. A checkbox (default off) additionally wipes every capture directory — `{userappdata}\Sparrow`, plus the runtime-created `sessions\` and `desktop_agent\sessions\` under `{app}` (Inno does not remove runtime files on its own) — and the API key. That is their memory, and there is no server copy behind it.

The in-app path is **Privacy controls → Delete everything**, and the offline path is `uninstall.bat` (`scripts/uninstall.py`); both write a deletion receipt. The Inno checkbox is the third door onto the same list — if you add a capture directory to `app/services/wipe.py`, add it here too.

## macOS (meeting path only)

See [docs/macos-meeting.md](../docs/macos-meeting.md). Unsigned `.app` + right-click Open is acceptable for the September cohort. BlackHole is the documented system-audio path; mic-only briefs still work without it.

## Unattended install

Both installers prompt for a model account. Set `QUILL_INSTALL_NONINTERACTIVE=1` to take it from the environment instead — `QUILL_INVITE_CODE`, else `ANTHROPIC_API_KEY`, else neither (the app still installs; chat waits for a key in `.env`). A redirected stdin triggers the same behaviour on its own, because a prompt nobody can answer is never the right outcome. `QUILL_INSTALL_SKIP_OLLAMA=1` skips the ~10 GB of local models *and* the winget install behind them.

```powershell
$env:QUILL_INSTALL_NONINTERACTIVE=1; $env:QUILL_INVITE_CODE='ABCD-EFGH-JKLM'; .\install.bat
```

## CI

`.github/workflows/release.yml` builds the Windows onedir on tags and runs the meeting-path subset on `macos-latest`.

`.github/workflows/clean-install.yml` is the closest we get to the checklist's "clean machine, double-click, done": on fresh `windows-latest` / `macos-latest` runners it runs the **real** `install.bat` / `install.command`, asserts the models cached, boots `run_all.py` and waits for `/health`, then runs `scripts/uninstall.py` and asserts nothing survived. It runs on install-path PRs and weekly, since most clean-install breakage arrives from outside the repo.

It does **not** cover the double-click itself (SmartScreen/Gatekeeper never fire), winget/Homebrew installing Python (runners ship it), TCC prompts, or a real ~10 GB download. Those stay a manual gate — a snapshotted Windows 11 VM and one real Mac.
