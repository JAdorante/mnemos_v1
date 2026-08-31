# Mnemos — Tester Setup (macOS)

Mnemos is a local-first personal memory assistant. Everything it captures and
remembers stays on **your** Mac; it calls a frontier model (Claude by default)
only when the local models aren't confident.

> **Read this first: the Mac build is the meeting build.** It captures
> meetings, builds your memory, and gives you search, the timeline and the
> Console. It does **not** capture your screen, log mouse clicks, drive desktop
> apps, or mirror phone notifications — those are Windows-only. This is a
> deliberate scope, not a bug: see the table at the bottom.

## You need

- macOS 13 (Ventura) or newer, Intel or Apple Silicon
- ~20 GB free disk, 16 GB RAM recommended
- **An invite code** — we send you one, e.g. `ABCD-EFGH-JKLM`. You do not need
  an Anthropic account, a credit card, or an API key.
  <br>*(Prefer your own key? See "Bring your own key" below.)*

## Install (one time)

1. Download the Mnemos ZIP from the link you were given and move the extracted
   folder somewhere permanent (e.g. your home folder). **Do not run it from
   Downloads** — macOS treats that folder specially and it will fight you.
2. Open the folder and **double-click `install.command`**.
   - The first time, macOS will refuse: *"install.command cannot be opened
     because it is from an unidentified developer."* This is Gatekeeper, and it
     is expected — we hand-distribute this build, so it is not signed yet.
   - **Right-click `install.command` → Open → Open.** You only do this once.
   - If you do not see "Open" in the menu: **System Settings → Privacy &
     Security**, scroll to Security, and click **Open Anyway** next to the
     message about `install.command`.
3. The installer sets up Python, downloads the local models (~10 GB — the long
   part), then asks how to connect Claude. Choose **[1] I have an invite code**
   and paste the code we sent. Safe to re-run if anything fails.
4. **Double-click `start.command`** and open **http://127.0.0.1:8000**.
   (Same right-click → Open the first time.)

### The permission prompts you will see

macOS asks the *user*, not the app, so these appear the first time each thing
is used. All of them are optional except the first:

| Prompt | When | If you decline |
|---|---|---|
| **Microphone** | First time you record a meeting | Meetings cannot be captured — this is the one to allow |
| **Calendar** | If you connect iCloud calendar | Meetings are detected from window titles instead |
| **Camera** | Only if you turn on the optional webcam source | Nothing else changes |

If you dismissed a prompt by accident: **System Settings → Privacy & Security →
Microphone**, and enable the entry for Terminal (or Mnemos, if you started it
from the app bundle).

### Bring your own key (optional)

Choose **[2] I have my own Anthropic API key** at the same prompt, or leave it
blank and add `ANTHROPIC_API_KEY=sk-ant-...` to `.env` afterwards. Create one at
https://console.anthropic.com. Nothing else differs. Ambient cloud spend is
capped at **$2/day** either way (`QUILL_CLOUD_BUDGET_USD_DAY`).

## Hearing the other side of a call

Your Mac's microphone records **your** side of a meeting perfectly. It also
picks up remote voices through your speakers, but quietly — good enough for a
brief, not great for quotes.

macOS has no built-in way for an app to record what the system is playing. If
you want the remote side captured properly:

1. Install [BlackHole](https://github.com/ExistentialAudio/BlackHole)
   (`brew install blackhole-2ch`).
2. Open **Audio MIDI Setup** → create a **Multi-Output Device** containing both
   BlackHole 2ch and your normal speakers, and select it as your output.
3. Add `QUILL_SYSTEM_AUDIO_DEVICE=BlackHole 2ch` to `.env` and enable **System
   audio** in the Privacy controls.

**This is optional.** Skip it and meetings still work — the Privacy controls
say as much next to the toggle.

## What works here, and what doesn't

The Privacy controls show this too: anything Windows-only appears greyed out
with the reason, rather than as a switch that fails when you flip it.

| | macOS | Why |
|---|---|---|
| Meeting capture + briefs | ✅ | The core of this build |
| Memory, search, timeline, Console | ✅ | Platform-independent |
| Chat, extraction, commitments, people | ✅ | |
| Calendar (iCloud) | ✅ | |
| Backup / export / restore | ✅ | |
| Browser agent | ✅ | Playwright Chromium |
| Microphone capture | ✅ | Allow the permission prompt |
| Webcam capture | ✅ | Optional, off by default |
| System audio (remote voices) | ⚠️ | Needs BlackHole — see above |
| Screen capture | ❌ | Windows-only in this build |
| Mouse-click capture | ❌ | Windows-only in this build |
| Desktop agent (drives apps) | ❌ | Windows-only in this build |
| Phone notification mirror | ❌ | Windows Phone Link only |

## What leaves your Mac

Identical to the Windows build — **nothing, unless you press a button or tick a
box.** See the table in [TESTER_SETUP.md](TESTER_SETUP.md#what-leaves-your-machine)
for the full list; the version check, usage stats, and crash reports all behave
the same way here.

## Backing up / taking your data out

Under Privacy controls: **"Back up my memory"** (restorable zip, secrets
excluded) and **"Export my data"** (portable `.jsonl` readable in any editor).
Keep a backup somewhere other than this Mac — `data/` is the only copy.

To restore, quit Mnemos first, then from the Mnemos folder:

```bash
.venv/bin/python scripts/restore_backup.py ~/Downloads/mnemos-backup-....zip data
```

It refuses to run while Mnemos is open and swaps the folder atomically, so an
interrupted restore leaves your old data intact.

## Updating

Download the newer ZIP, replace the folder (keeping your `data/`, `.env` and
`.credentials.env`), and re-run `install.command` — it skips anything already
done. If macOS complains after an update, run this once in Terminal from the
Mnemos folder:

```bash
xattr -dr com.apple.quarantine .
```

The version you're running is in the Memory Console footer — quote it in any
bug report.

## Stopping capture

Two clicks, either of them instant:

* **Stop all** on the recording bar (bottom of any Mnemos page) — halts every
  source immediately and leaves them off until you turn them back on.
* **Privacy controls → Stop capture now** — the same thing, next to the
  explanation of what each source does.

Both revoke the allow-list *and* stop the running pipelines. Closing Mnemos
also stops everything. Revoking the microphone in System Settings → Privacy &
Security works too, and Mnemos will say the source is unavailable rather than
failing silently.

## Deleting your data

**In the app:** Privacy controls → **Delete everything**. It shows you how much
is stored and where, asks you to type `DELETE MY MEMORY`, then empties every
directory Mnemos writes and saves a **deletion receipt** — a small JSON file
listing each directory and its size before deletion. Nothing personal is in the
receipt; it exists so you can prove the deletion happened.

**With Mnemos closed** (also the fix if a file was in use and the in-app delete
reported it):

```bash
.venv/bin/python scripts/uninstall.py            # asks before deleting
.venv/bin/python scripts/uninstall.py --yes --all
```

or right-click → Open on `uninstall.command`. `--all` additionally removes your
API key, the shipped config and the `.venv`.

There is no server-side copy of anything, so a deletion here is the only
deletion there is. **Take a backup first if you might want it back** — this
cannot be undone.

## Uninstalling

Run `uninstall.command` (above), then delete the folder. Optionally
`brew uninstall ollama portaudio`, and remove BlackHole with its own
uninstaller if you installed it.

Your data lives in three places inside the folder, which is why deleting
`data/` alone is not enough:

| Folder | What's in it |
|---|---|
| `data/` | Memory, transcripts, audio, frames, indexes |
| `sessions/` | Browser-agent page captures (screenshots, page trees) |
| `desktop_agent/sessions/` | Desktop-agent action audit log |

`uninstall.command` clears all three. Deleting the whole folder also does it.

macOS also remembers the permissions you granted. To clear those, remove Mnemos
(or Terminal) from System Settings → Privacy & Security → Microphone / Screen
Recording.
