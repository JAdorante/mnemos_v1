#!/usr/bin/env bash
# Mnemos tester installer (macOS 13+, Intel or Apple Silicon).
#
# Double-click in Finder, or run:  bash install.command
#
# The macOS build is the MEETING PATH only (see docs/macos-meeting.md):
# calendar + meeting audio + memory + Console + MCP. Screen capture, the
# desktop agent, Phone Link and Windows toast capture are Windows-only and stay
# off — the app already guards them, so nothing here has to disable them.
#
# What it does, in order:
#   1. Finds Python 3.11/3.12 (Homebrew, python.org, or pyenv)
#   2. Creates .venv and installs requirements.txt
#   3. Installs Playwright Chromium (browser agent)
#   4. Installs PortAudio if missing (the mic path needs it)
#   5. Optional Ollama + local models (~10 GB) — skipped if not installed
#   6. Pre-downloads Whisper / VAD / speaker / embedding models
#   7. Creates .env and connects your model account (invite code or your key)
#
# Everything is idempotent — safe to re-run if a step fails halfway.
set -u

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT" || exit 1

BOLD=$'\033[1m'; CYAN=$'\033[36m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'
RED=$'\033[31m'; RESET=$'\033[0m'
step() { printf '\n%s==> %s%s\n' "$CYAN" "$1" "$RESET"; }
ok()   { printf '    %s%s%s\n' "$GREEN" "$1" "$RESET"; }
warn() { printf '    %s%s%s\n' "$YELLOW" "$1" "$RESET"; }

# Unattended installs: CI on a clean runner, a scripted rollout, or anything
# that pipes stdin. Without this the model-account prompt and the closing
# "press return" block forever, so an unattended run becomes a hang rather than
# a pass or a clean failure. A redirected stdin implies it too — a prompt no
# one can answer is never the right behaviour.
NONINTERACTIVE=0
case "${QUILL_INSTALL_NONINTERACTIVE:-0}" in 0|""|false|False|no|No) ;; *) NONINTERACTIVE=1 ;; esac
[ -t 0 ] || NONINTERACTIVE=1

# Ollama is a ~10 GB decision and pointless on a throwaway CI machine.
SKIP_OLLAMA=0
case "${QUILL_INSTALL_SKIP_OLLAMA:-0}" in 0|""|false|False|no|No) ;; *) SKIP_OLLAMA=1 ;; esac

hold() { [ "$NONINTERACTIVE" = "1" ] || read -r -p "Press return to close." _; }
fail() { printf '\n%sERROR: %s%s\n' "$RED" "$1" "$RESET"; hold; exit 1; }

[ -f "$ROOT/run_all.py" ] || fail "Run this from inside the Mnemos folder (run_all.py not found)."

printf '%sMnemos tester install (macOS)%s\n' "$BOLD" "$RESET"
echo "Needs ~20 GB free disk (models + dependencies) and a while on first run."

# --- 1. Python ----------------------------------------------------------------
step "Locating Python 3.11 or 3.12"
PY=""
for cand in python3.12 python3.11 python3; do
    exe="$(command -v "$cand" 2>/dev/null)" || continue
    v="$("$exe" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" || continue
    case "$v" in 3.10|3.11|3.12) PY="$exe"; break ;; esac
done
if [ -z "$PY" ]; then
    if command -v brew >/dev/null 2>&1; then
        warn "Python 3.11 not found - installing with Homebrew ..."
        brew install python@3.11 || fail "Homebrew could not install Python 3.11."
        PY="$(brew --prefix)/opt/python@3.11/bin/python3.11"
    else
        fail "Python 3.11 not found. Install it from python.org (or install Homebrew and re-run)."
    fi
fi
ok "Python: $PY ($("$PY" -c 'import sys;print(sys.version.split()[0])'))"

# --- 2. venv + requirements ---------------------------------------------------
step "Creating virtual environment + installing Python packages (several minutes)"
VENV_PY="$ROOT/.venv/bin/python"
[ -x "$VENV_PY" ] || "$PY" -m venv "$ROOT/.venv" || fail "Could not create .venv."
"$VENV_PY" -m pip install --upgrade pip --quiet
"$VENV_PY" -m pip install -r "$ROOT/requirements.txt" \
    || fail "pip install failed - see output above. Re-run install.command to resume."
ok "Python packages installed."

# --- 3. Playwright Chromium ---------------------------------------------------
step "Installing Chromium for the browser agent"
if "$VENV_PY" -m playwright install chromium; then
    ok "Chromium ready."
else
    warn "Playwright Chromium install failed - the browser agent will not work until you run:"
    warn "  .venv/bin/python -m playwright install chromium"
fi

# --- 4. PortAudio (the mic path) ----------------------------------------------
# sounddevice binds PortAudio at import. Without it, meeting capture — the whole
# point of the macOS build — fails at start with an opaque OSError.
step "Checking the microphone backend (PortAudio)"
if "$VENV_PY" -c 'import sounddevice' >/dev/null 2>&1; then
    ok "PortAudio present."
elif command -v brew >/dev/null 2>&1; then
    warn "PortAudio missing - installing with Homebrew ..."
    brew install portaudio >/dev/null 2>&1
    "$VENV_PY" -m pip install --force-reinstall --no-cache-dir sounddevice >/dev/null 2>&1
    if "$VENV_PY" -c 'import sounddevice' >/dev/null 2>&1; then
        ok "PortAudio installed."
    else
        warn "Still cannot load PortAudio. Meetings will not record until: brew install portaudio"
    fi
else
    warn "PortAudio missing and Homebrew not installed. Meeting capture will not work."
    warn "Install Homebrew from brew.sh, then run: brew install portaudio"
fi

# --- 5. Ollama (optional) -----------------------------------------------------
# Optional on macOS by design: without it Mnemos runs cloud-only, which costs
# more per day but works. Never installed silently — it is a 10 GB decision.
step "Checking for Ollama (optional local models)"
OLLAMA_OK=0
if [ "$SKIP_OLLAMA" = "1" ]; then
    warn "Skipped (QUILL_INSTALL_SKIP_OLLAMA=1) - cloud-only."
elif command -v ollama >/dev/null 2>&1; then
    ollama list >/dev/null 2>&1 || { (ollama serve >/dev/null 2>&1 &) ; sleep 6; }
    warn "Pulling qwen2.5:7b-instruct (~4.7 GB) - this is the long part ..."
    if ollama pull qwen2.5:7b-instruct; then
        warn "Pulling minicpm-v (~5.5 GB) ..."
        ollama pull minicpm-v && OLLAMA_OK=1
    fi
fi
if [ "$OLLAMA_OK" = "1" ]; then
    ok "Local models ready (text: qwen2.5:7b-instruct, vision: minicpm-v)."
else
    warn "Ollama not set up - Mnemos will run cloud-only (higher API usage)."
    warn "Optional: install from ollama.com, run 'ollama pull qwen2.5:7b-instruct',"
    warn "then set QUILL_TEXT_LOCAL=1 in .env."
fi

# --- 6. Speech / embedding models ---------------------------------------------
step "Pre-downloading speech + embedding models (Whisper, VAD, speaker, MiniLM)"
echo "    ~700 MB on a fresh machine. Each model retries and resumes, so a"
echo "    dropped connection costs the remainder, not the whole download."
if "$VENV_PY" "$ROOT/scripts/download_models.py" --retries 5; then
    ok "Speech + embedding models cached."
else
    warn "Some models did not finish downloading. Re-run install.command when you have a steady connection - what already downloaded is kept and partial files continue where they stopped. Mnemos still starts; it fetches whatever is missing on first use."
fi

# --- 7. .env + model account --------------------------------------------------
step "Configuring .env"
ENV_PATH="$ROOT/.env"
[ -f "$ENV_PATH" ] || cp "$ROOT/.env.example" "$ENV_PATH"

# Same two paths as the Windows installer: an invite code (nothing to sign up
# for) or your own key. The invite branch only exists when this build ships an
# endpoint, so a BYO-only build looks exactly as it did before.
INVITE_URL="${QUILL_INVITE_URL:-}"
if [ -z "$INVITE_URL" ]; then
    INVITE_URL="$(sed -n 's/^QUILL_INVITE_URL=\(.*\)$/\1/p' "$ENV_PATH" | head -n 1)"
fi

INVITED=0
if [ "$NONINTERACTIVE" = "1" ]; then
    # Same two paths as the prompt, taken from the environment instead of a
    # human: an invite code, else a key, else neither (a valid outcome - the
    # app installs and runs, chat waits for a key in .env).
    if [ -n "$INVITE_URL" ] && [ -n "${QUILL_INVITE_CODE:-}" ]; then
        if "$VENV_PY" - "$QUILL_INVITE_CODE" "$ROOT" <<'PYCODE'
import sys
sys.path.insert(0, sys.argv[2])
from app.services.invite import InviteError, redeem_and_save
try:
    out = redeem_and_save(sys.argv[1])
    print("    invite accepted" + (" for " + out["label"] if out.get("label") else ""))
except InviteError as exc:
    print("    " + str(exc))
    sys.exit(1)
PYCODE
        then INVITED=1; ok "Connected with your invite code."
        else warn "QUILL_INVITE_CODE was refused - continuing without a model account."
        fi
    fi
    if [ "$INVITED" = "0" ] && [ -n "${ANTHROPIC_API_KEY:-}" ]; then
        if "$VENV_PY" - "$ANTHROPIC_API_KEY" <<'PYCODE'
import sys, anthropic
try:
    anthropic.Anthropic(api_key=sys.argv[1]).models.list()
    print("    key OK")
except Exception as e:
    print("    key check failed: " + type(e).__name__)
    sys.exit(1)
PYCODE
        then sed -i '' "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY|" "$ENV_PATH"
        else warn "ANTHROPIC_API_KEY did not validate - leaving .env without a key."
        fi
    elif [ "$INVITED" = "0" ]; then
        warn "No QUILL_INVITE_CODE or ANTHROPIC_API_KEY set - add a key to .env before chat works."
    fi
elif [ -n "$INVITE_URL" ]; then
    export QUILL_INVITE_URL="$INVITE_URL"
    echo ""
    echo "  How do you want to connect Mnemos to Claude?"
    echo "    [1] I have an invite code  (recommended - nothing to sign up for)"
    echo "    [2] I have my own Anthropic API key"
    echo "    [3] Skip for now"
    while :; do
        read -r -p "  Choose 1, 2 or 3: " choice
        case "$choice" in
            3|"") break ;;
            2) break ;;
            1)
                read -r -p "  Invite code (like ABCD-EFGH-JKLM): " code
                [ -n "$code" ] || continue
                if "$VENV_PY" - "$code" "$ROOT" <<'PYCODE'
import sys
sys.path.insert(0, sys.argv[2])
from app.services.invite import InviteError, redeem_and_save
try:
    out = redeem_and_save(sys.argv[1])
    print("    invite accepted" + (" for " + out["label"] if out.get("label") else ""))
except InviteError as exc:
    print("    " + str(exc))
    sys.exit(1)
PYCODE
                then INVITED=1; ok "Connected with your invite code."; break
                else warn "Try again, or choose 2 to paste your own key."
                fi
                ;;
            *) ;;
        esac
    done
fi

if [ "$NONINTERACTIVE" != "1" ] && [ "$INVITED" = "0" ]; then
    while :; do
        read -r -p "Paste YOUR Anthropic API key (sk-ant-..., from console.anthropic.com) or press return to add it to .env later: " key
        if [ -z "$key" ]; then
            warn "Skipped - Mnemos needs ANTHROPIC_API_KEY in .env before chat works."
            break
        fi
        if "$VENV_PY" - "$key" <<'PYCODE'
import sys, anthropic
try:
    anthropic.Anthropic(api_key=sys.argv[1]).models.list()
    print("    key OK")
except Exception as e:
    print("    key check failed: " + type(e).__name__)
    sys.exit(1)
PYCODE
        then
            # macOS sed needs an explicit backup suffix for -i.
            sed -i '' "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=$key|" "$ENV_PATH"
            break
        fi
        warn "That key didn't validate. Try again, or press return to skip."
    done
fi

if [ "$OLLAMA_OK" = "1" ]; then
    sed -i '' 's|^#\{0,1\}QUILL_TEXT_LOCAL=.*|QUILL_TEXT_LOCAL=1|' "$ENV_PATH"
    sed -i '' 's|^#\{0,1\}QUILL_TEXT_LOCAL_MODEL=.*|QUILL_TEXT_LOCAL_MODEL=qwen2.5:7b-instruct|' "$ENV_PATH"
fi
ok ".env written (never share this file - it holds your key)."

chmod +x "$ROOT/start.command" 2>/dev/null

printf '\n%s=============================================================%s\n' "$GREEN" "$RESET"
printf '%s  Mnemos is installed.%s\n' "$GREEN" "$RESET"
echo "  Start it:   double-click start.command"
echo "  Then open:  http://127.0.0.1:8000"
echo ""
echo "  Notes for testers:"
echo "   - Everything runs and stays on THIS Mac (localhost only)."
echo "   - macOS will ask for Microphone permission the first time you record."
echo "   - Screen capture and the desktop agent are Windows-only; meetings,"
echo "     memory, search and the Console all work here."
echo "   - Remote voices are quiet without BlackHole - see TESTER_SETUP-macos.md."
echo "   - Ambient cloud spend is capped at \$2/day by default."
printf '%s=============================================================%s\n' "$GREEN" "$RESET"
hold
