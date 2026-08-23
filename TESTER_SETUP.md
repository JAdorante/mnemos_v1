# Mnemos — Tester Setup (Windows)

> On a Mac? Use [TESTER_SETUP-macos.md](TESTER_SETUP-macos.md) — the
> install steps and the capture scope are different.

Mnemos is a local-first personal memory assistant. Everything it captures and
remembers stays on **your** machine; it calls a frontier model (Claude by
default) only when the local models aren't confident.

## You need

- Windows 10/11, ~20 GB free disk, 16 GB RAM recommended
- **An invite code** — we send you one, e.g. `ABCD-EFGH-JKLM`. You do not need
  an Anthropic account, a credit card, or an API key.
  <br>*(Prefer to use your own key? See "Bring your own key" below — that path
  is unchanged.)*

## Install (one time)

1. Download the Mnemos ZIP from the link you were given and extract it
   somewhere permanent (e.g. `C:\Mnemos`).
2. Double-click **`install.bat`**.
   It sets up Python, downloads the local models (~10 GB — the long part), then
   asks how to connect Claude. Choose **[1] I have an invite code** and paste
   the code we sent. Safe to re-run if anything fails.
3. Double-click **`start.bat`** and open **http://127.0.0.1:8000** in your
   browser.

Your invite code is exchanged once, at install time, for an API key of your
own. That key is written to `.credentials.env` in your Mnemos folder and used
from there — after install, our invite service is never contacted again. If you
reinstall and the code says it has already been used, tell us and we'll reissue
it.

### Bring your own key (optional)

Choose **[2] I have my own Anthropic API key** at the same prompt, or leave the
key blank and add `ANTHROPIC_API_KEY=sk-ant-...` to `.env` afterwards. Create
one at https://console.anthropic.com. Nothing else differs — Mnemos cannot tell
the two paths apart. Ambient cloud spend is capped at **$2/day** either way
(`QUILL_CLOUD_BUDGET_USD_DAY`).

## What to expect

- **Capture is off by default.** Mic, screen, camera, and meeting audio only
  turn on if you consent in the Privacy controls in the UI.
- **Local only.** The app binds to 127.0.0.1 — nothing is reachable from the
  network, and your data never leaves the machine except redacted model calls.
- A second window (the browser agent) opens alongside — that's normal.
- First launch is slower while indexes build.

## What leaves your machine

Short version: **nothing, unless you press a button or tick a box.** In full:

| What | When | You control it with |
|---|---|---|
| Redacted model calls | When local models aren't confident | Capped at $2/day; `QUILL_TEXT_LOCAL=1` keeps more work local |
| Invite-code redemption | Once, during install | Only if you use the invite path |
| **Version check** | On start and once a day | Privacy controls → "Check for new versions", or `QUILL_UPDATE_CHECK=0` |
| **Usage stats** | Only when you press "Send my stats", or if you tick the weekly box | Privacy controls → "Sharing & updates" |
| Crash report zip | Only when you press "Report a problem" | It's a file on your disk; you decide whether to email it |

### About the version check

Mnemos downloads one small file to see whether a newer build exists. It is an
**unconditional download of a static file** — it carries no query parameters, no
install ID, and not even which version you're running. Nothing is downloaded or
installed automatically; you get a banner with a link. Turn it off in Privacy
controls or set `QUILL_UPDATE_CHECK=0` in `.env`.

### About usage stats

Mnemos counts how you use it — number of searches, chat turns, meetings
captured, minutes active — **as numbers only**. It never records what you
searched for, what was said, who was named, or what was on screen; there is no
column in the ledger those could go in. The counts live in your own database.

Two ways to share them, both your choice:

- **"Send my stats"** (Privacy controls, or the Memory Console) writes a JSON
  file to `data\logs\` and shows you the path. Open it, read it, then email it
  to us — or don't.
- **Weekly automatic** is off by default. Before you tick it, click "See exactly
  what would be sent" — that shows the same bytes that would be transmitted.

If it helps: sending us stats is what makes the pilot measurable. It is also
entirely optional, and the app behaves identically either way.

## Backing up / taking your data out

Under Privacy controls:

- **"Back up my memory"** downloads a complete, restorable zip of your data
  folder (your API key and access tokens are excluded). Keep it somewhere other
  than this machine — right now `data\` is the only copy of everything Mnemos
  remembers.
- **"Export my data"** downloads a portable copy: your events, facts, people and
  relationships as plain `.jsonl` files with a README, readable in any text
  editor without Mnemos installed.

To restore a backup, close Mnemos first, then from the Mnemos folder:

```
.venv\Scripts\python scripts\restore_backup.py path\to\mnemos-backup-....zip data
```

It refuses to run while Mnemos is open (a restore under a running server would
corrupt the result) and swaps the folder atomically, so an interrupted restore
leaves your old data intact.

## Updating

Download the newer ZIP, extract over the same folder (or replace it, keeping
your `data\` folder, `.env` and `.credentials.env`), and re-run `install.bat` —
it skips anything already done. The version you're running is shown in the
Memory Console footer; quote it in any bug report.

## Uninstalling

Delete the folder. Optionally uninstall Ollama from Windows Settings → Apps.
All of your data lives in the folder's `data\` directory — deleting it removes
everything Mnemos remembered. Take a backup first if you might want it back.
