# Mnemos — Tester Setup (Windows)

Mnemos is a local-first personal memory assistant. Everything it captures and
remembers stays on **your** machine; it calls the Claude API (with **your** key)
only when the local models aren't confident.

## You need

- Windows 10/11, ~20 GB free disk, 16 GB RAM recommended
- Your own Anthropic API key — create one at https://console.anthropic.com
  (Mnemos never ships with a key; ambient cloud spend is capped at $2/day by default)

## Install (one time)

1. Download the Mnemos ZIP from the link you were given and extract it
   somewhere permanent (e.g. `C:\Mnemos`).
2. Double-click **`install.bat`**.
   It sets up Python, downloads the local models (~10 GB — the long part),
   and asks for your Anthropic API key. Safe to re-run if anything fails.
3. Double-click **`start.bat`** and open **http://127.0.0.1:8000** in your browser.

## What to expect

- **Capture is off by default.** Mic, screen, camera, and meeting audio only
  turn on if you consent in the Privacy controls in the UI.
- **Local only.** The app binds to 127.0.0.1 — nothing is reachable from the
  network, and your data never leaves the machine except redacted calls to the
  Claude API.
- A second window (the browser agent) opens alongside — that's normal.
- First launch is slower while indexes build.

## Updating

Download the newer ZIP, extract over the same folder (or replace it, keeping
your `data\` folder and `.env`), and re-run `install.bat` — it skips anything
already done.

## Uninstalling

Delete the folder. Optionally uninstall Ollama from Windows Settings → Apps.
All of your data lives in the folder's `data\` directory — deleting it removes
everything Mnemos remembered.
