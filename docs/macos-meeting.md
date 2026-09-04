# macOS meeting-first tester build (Workstream 7)

Port **only** the meeting path: calendar + meeting audio + memory + Console + MCP. Out of scope: Phone Link, desktop agent, Windows toast capture, desktop capture.

## Windows assumptions already guarded

| Piece | macOS behaviour today |
|---|---|
| `winsdk` notifications | `QUILL_NOTIFICATIONS` defaults off off-Windows (`app/config.py`) |
| Phone Link | `QUILL_PHONE_LINK` — tester profile forces 0 |
| Desktop agent / pyautogui | `sys_platform == "win32"` extras in `requirements.txt` |
| Foreground window titles | `meeting_session._foreground_title` returns `""` when `os.name != "nt"` |
| WASAPI loopback | `soundcard` / system-audio — not the mic path |

Mic capture uses PortAudio (`sounddevice`) and should work on macOS with `brew install portaudio` (or the bundled wheel).

## System audio (meeting playback)

v1 does **not** ship a virtual loopback driver. Supported path: install [BlackHole](https://github.com/ExistentialAudio/BlackHole), set it as the output (or a Multi-Output Device with BlackHole + speakers), then point `QUILL_SYSTEM_AUDIO_DEVICE` at that device and enable system audio for the meeting window.

**Mic-side capture still produces briefs** without BlackHole. Remote voices may be quiet; that is acceptable for the September cohort.

## Tester path (WS-F — shipped)

| piece | file |
|---|---|
| installer | `install.command` (bash peer of `scripts/install.ps1`) |
| launcher | `start.command` (clears the Gatekeeper quarantine on the folder) |
| tester doc | `TESTER_SETUP-macos.md` (Gatekeeper, TCC prompts, BlackHole) |
| honest degradation | `app/services/capture_support.py` → `GET /capture/status` |
| tests | `tests/test_macos_path.py` |

`capture_support` is what stops the Privacy sheet offering a Screen toggle that
only ever returns 503: unsupported sources render disabled with the reason, and
macOS system audio is offered as "needs setup" with the BlackHole instructions
rather than being silently broken. The 503 in `_resume_source` stays as the
backstop — this is about telling the tester before they click.

**Still manual:** a full dry run on a clean Mac (Gatekeeper prompt, microphone
TCC grant, a real meeting with and without BlackHole). CI covers `bash -n` on
both launchers and the capture-support map on `macos-latest`; it cannot cover
the permission dialogs.

## Packaging

```bash
pyinstaller packaging/mnemos.spec
# then wrap Dist/Sparrow.app in a DMG (unsigned is OK for a hand-distributed cohort)
```

Gatekeeper: testers right-click → Open the first time. Notarization is a fast-follow.

## CI

`.github/workflows/release.yml` runs the meeting-path subset on `macos-latest`:

```
python -m pytest tests/test_first_run.py tests/test_meeting_session.py tests/test_meeting_mode.py tests/test_mcp_tools.py -q
```
