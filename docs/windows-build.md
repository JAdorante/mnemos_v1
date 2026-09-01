# Building the Mnemos desktop app on Windows

Everything here has been verified on Linux except the parts marked **Windows-only
— unverified**. Those are the ones that need your machine: the spec was written
against Windows behaviour but nobody has run it there.

Assume the first build fails on something in §6. That is the expected outcome,
not a setback — the point of this pass is to find out which one.

---

## 1. Prerequisites

- **Python 3.11 or 3.12** (`py -3.11 --version`)
- **Inno Setup 6** — <https://jrsoftware.org/isdl.php>, or `winget install JRSoftware.InnoSetup`
- **Edge WebView2 Runtime** — preinstalled on Windows 11. On Windows 10, confirm
  under Settings → Apps → "Microsoft Edge WebView2 Runtime"; if absent, install
  the Evergreen Bootstrapper from Microsoft. Without it pywebview cannot open a
  window and the app falls back to a browser tab.
- ~15 GB free disk for the build tree.

## 2. Environment

```powershell
git clone <repo> mnemos; cd mnemos
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -U pip
.venv\Scripts\pip install -r requirements.txt -r packaging\requirements-desktop.txt
```

**Check the torch wheel is CPU-only** before building:

```powershell
.venv\Scripts\python -c "import torch; print(torch.__version__, torch.version.cuda)"
```

Expect `cuda` to print `None`. Windows PyPI wheels are CPU-only, so this should
pass — if it prints a version, the spec will refuse the build and tell you the
fix. Do not use `QUILL_ALLOW_CUDA_BUILD=1` for a build you intend to ship: it
adds 2–3 GB of GPU runtime the app never touches.

## 3. Verify from source first

```powershell
.venv\Scripts\python desktop_app.py --self-test
```

Every line under the first group must say `ok`. `torch` and `silero_vad` matter
most — `silero_vad` imports torch even in ONNX mode, so losing it means no VAD,
no utterances, and no memory, with no error explaining why.

The `webview` and `pystray` lines should say `ok` here. If they say `degraded`,
fix that before building — a bundle cannot add what the source environment does
not have.

Then run it for real:

```powershell
.venv\Scripts\python desktop_app.py
```

**A native window should open.** If a browser tab opens instead, pywebview is
not working — see §6.

## 4. Build

```powershell
.venv\Scripts\pyinstaller packaging\mnemos.spec
```

~5–10 minutes. Then, before anything else:

```powershell
.\dist\Mnemos\Mnemos.exe --self-test
```

This is the whole safety net. It is how the missing-torch and missing-speechbrain
bugs were found; run it after every spec change. A `MISSING` line means the
bundle is broken even though the app will start and serve pages normally.

Check the size — expect roughly 1.5–2.5 GB:

```powershell
"{0:N0} MB" -f ((Get-ChildItem -Recurse dist\Mnemos | Measure-Object Length -Sum).Sum/1MB)
```

Then launch it:

```powershell
.\dist\Mnemos\Mnemos.exe
```

Window opens, tray icon appears, no console window behind it.

## 5. Installer

```powershell
iscc packaging\mnemos.iss        # -> dist\MnemosSetup.exe
```

Install it on **a machine that is not the build machine** — a VM snapshot is
ideal, because you can roll back and repeat. What to check:

- [ ] SmartScreen appears (expected while unsigned — see §7)
- [ ] Start-menu entry and icon look right at 16px in the taskbar
- [ ] First launch shows `/bootstrap` and the models download with progress
- [ ] Onboarding asks for the API key **in the app**, not a terminal
- [ ] Data lands in `%LOCALAPPDATA%\Mnemos`, *not* under Program Files
- [ ] Mic capture works after consenting in the Privacy sheet
- [ ] Tray → Stop capture actually stops it
- [ ] Add/Remove Programs → uninstall, tick the wipe box, then confirm
      `%LOCALAPPDATA%\Mnemos` is gone

## 6. Windows-only — unverified, and where it will break

Ranked by how likely each is to bite. All were reasoned about, none observed.

**pywebview / pythonnet.** The Windows backend loads through `clr_loader` and
`pythonnet`, which PyInstaller has historically needed help with. The spec names
`webview.platforms.edgechromium`, `webview.platforms.winforms` and `clr_loader`
explicitly, because pywebview selects its platform module by string and static
analysis cannot see it. *Symptom:* the packaged app opens a browser tab instead
of a window, or dies on a `clr` import. *Likely fix:* add
`--collect-all pythonnet` / `--collect-all clr_loader`, or pin pywebview.

**comtypes and the UIA bridge.** `desktop_agent/uia.py` calls
`comtypes.client.GetModule("UIAutomationCore.dll")`, which *generates* a module
into `comtypes.gen` at runtime — frozen apps cannot write there. The desktop
build does not ship the desktop agent, so this should not be reached; if a build
error mentions `comtypes.gen`, drop `desktop_agent` from the spec's
`collect_submodules` loop rather than fighting it.

**pywin32.** `voice.py` does `import win32com.client` inside a function for the
SAPI5 offline voice. *Symptom:* TTS falls back or errors on first speak.

**winsdk.** Phone Link / toast capture uses namespace packages that PyInstaller
often misses. *Symptom:* notifications capture is silently absent. Windows-only
feature, not on the pilot's critical path.

**soundcard / WASAPI loopback.** System-audio capture binds native DLLs. Test by
enabling System audio in the Privacy sheet and playing something.

**Antivirus.** Unsigned PyInstaller onedir builds get quarantined by some
endpoint agents, which on a trading firm's managed laptops is a real risk. Worth
knowing before you send an install link.

## 7. Code signing

The last gate before anyone outside the team installs this. Unsigned means
"Windows protected your PC" on first launch — for a firm evaluating a tool that
listens to their meetings, that is the wrong first impression.

**Start Azure Trusted Signing for Mnemos Labs today.** Identity verification
takes several days and is the only item on this list that cannot be coded
around. It is the cheapest/fastest option that still integrates with
`signtool` (~$10/month). If it does not clear by the 8th, send testers a
one-line "click More info → Run anyway" note and treat SmartScreen as a
known funnel leak.

Repo wiring is already in `.github/workflows/release.yml`:

- GitHub Actions secrets: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`,
  `AZURE_CLIENT_SECRET`
- GitHub Actions variables: `AZURE_TS_ENDPOINT` (e.g.
  `https://eus.codesigning.azure.net/`), `AZURE_TS_ACCOUNT`,
  `AZURE_TS_PROFILE`

A `v*` tag with those set signs `Mnemos.exe`, rebuilds the installer around
the signed exe, signs `MnemosSetup.exe`, and publishes it as the GitHub
Release asset — that file is the install link. Tags without the secrets still
publish, unsigned.

OV/EV certificates via `signtool` remain a fallback if Trusted Signing is
blocked. Timestamping (`/tr` / `timestamp.acs.microsoft.com`) matters:
without it every signature dies when the certificate expires.

```powershell
# local fallback only — CI uses Azure Trusted Signing
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
  /a dist\Mnemos\Mnemos.exe
iscc packaging\mnemos.iss          # rebuild the installer AFTER signing the exe
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 `
  /a dist\MnemosSetup.exe
signtool verify /pa /v dist\MnemosSetup.exe
```

## 8. Feeding fixes back

The `release.yml` workflow runs steps 4 and 5 on `windows-latest` for every
packaging PR, so a spec fix you make locally is re-checked on a clean machine
automatically. If you change the spec, run `python -m unittest
tests.test_desktop_build` — it asserts the properties that are easy to
regress silently (torch bundled, `console=False`, the CUDA guard, the
`{localappdata}` wipe target).
