# Packaging Mnemos for testers

## Windows (`MnemosSetup.exe`)

1. `pip install pyinstaller`
2. `pyinstaller packaging/mnemos.spec` → `dist/Mnemos/`
3. Install [Inno Setup](https://jrsoftware.org/isinfo.php) and run `iscc packaging/mnemos.iss` → `dist/MnemosSetup.exe`

The installer does **not** bundle Whisper weights or Chromium. First launch opens `/bootstrap`, which runs `scripts/download_models.py` and `playwright install chromium` with a log the tester can watch.

Set `QUILL_PROFILE=tester` in the installed `.env` (the spec copies example config; the wizard writes the Anthropic key to `.credentials.env`).

Uninstall: Inno removes the app. A checkbox (default off) deletes the data folder — that is their memory.

## macOS (meeting path only)

See [docs/macos-meeting.md](../docs/macos-meeting.md). Unsigned `.app` + right-click Open is acceptable for the September cohort. BlackHole is the documented system-audio path; mic-only briefs still work without it.

## CI

`.github/workflows/release.yml` builds the Windows onedir on tags and runs the meeting-path subset on `macos-latest`.
