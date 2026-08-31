# Mnemos tester installer (Windows 10/11).
#
# Run via install.bat (double-click) or:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\install.ps1
#
# What it does, in order:
#   1. Finds (or installs via winget) Python 3.11
#   2. Creates .venv and installs requirements.txt
#   3. Installs Playwright Chromium (browser agent)
#   4. Installs Ollama if missing, pulls qwen2.5:7b-instruct + minicpm-v
#   5. Pre-downloads Whisper / VAD / speaker / embedding models
#   6. Creates .env from .env.example and prompts for YOUR Anthropic API key
#      (validated live against the API before it is saved)
#
# Everything is idempotent — safe to re-run if a step fails halfway.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Step($msg)  { Write-Host "`n==> $msg" -ForegroundColor Cyan }
function Ok($msg)    { Write-Host "    $msg" -ForegroundColor Green }
function Warn($msg)  { Write-Host "    $msg" -ForegroundColor Yellow }
function Fail($msg)  { Write-Host "`nERROR: $msg" -ForegroundColor Red; exit 1 }

if (-not (Test-Path (Join-Path $root 'run_all.py'))) {
    Fail "Run this from inside the Mnemos folder (run_all.py not found)."
}

# Unattended installs: CI on a clean runner, a scripted rollout, or anything
# that pipes stdin. Without this the model-account prompt blocks forever, so an
# unattended run becomes a six-hour hang rather than a pass or a clean failure.
# Redirected stdin implies it too — a prompt no one can answer is never right.
$nonInteractive = $false
if ($env:QUILL_INSTALL_NONINTERACTIVE -and
    $env:QUILL_INSTALL_NONINTERACTIVE -notin @('0', 'false', 'False', 'no')) {
    $nonInteractive = $true
} elseif ([Console]::IsInputRedirected) {
    $nonInteractive = $true
}
# Ollama is a ~10 GB decision and pointless on a throwaway CI machine.
$skipOllama = ($env:QUILL_INSTALL_SKIP_OLLAMA -and
               $env:QUILL_INSTALL_SKIP_OLLAMA -notin @('0', 'false', 'False', 'no'))
if ($nonInteractive) {
    Warn "Unattended install - no prompts. Model account from QUILL_INVITE_CODE or ANTHROPIC_API_KEY."
}

Write-Host "Mnemos tester install" -ForegroundColor White
Write-Host "Needs ~20 GB free disk (models + dependencies) and a while on first run."

$winget = Get-Command winget -ErrorAction SilentlyContinue

# --- 1. Python 3.11 -----------------------------------------------------------
Step "Locating Python 3.11"
$py = $null
foreach ($spec in @(@('py','-3.11'), @('py','-3.12'), @('python'))) {
    $exe = $spec[0]
    $pre = @()
    if ($spec.Count -gt 1) { $pre = @($spec[1..($spec.Count-1)]) }
    try {
        $v = & $exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v -match '^3\.(10|11|12)$') { $py = @($exe) + $pre; break }
    } catch {}
}
if (-not $py) {
    if (-not $winget) { Fail "Python 3.11 not found and winget unavailable. Install Python 3.11 from python.org, then re-run." }
    Warn "Python 3.11 not found - installing via winget ..."
    winget install -e --id Python.Python.3.11 --accept-source-agreements --accept-package-agreements --silent
    if ($LASTEXITCODE -ne 0) { Fail "winget could not install Python. Install 3.11 from python.org, then re-run." }
    $py = @("$env:LOCALAPPDATA\Programs\Python\Python311\python.exe")
    if (-not (Test-Path $py[0])) { Fail "Python installed but not found at the expected path. Open a NEW terminal and re-run install.bat." }
}
Ok "Python: $($py -join ' ')"

# --- 2. venv + requirements ---------------------------------------------------
Step "Creating virtual environment + installing Python packages (several minutes)"
$venvPy = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    $exe = $py[0]
    $pre = @()
    if ($py.Count -gt 1) { $pre = @($py[1..($py.Count-1)]) }
    & $exe @pre -m venv (Join-Path $root '.venv')
}
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r (Join-Path $root 'requirements.txt')
if ($LASTEXITCODE -ne 0) { Fail "pip install failed - see output above. Re-run install.bat to resume." }
Ok "Python packages installed."

# --- 3. Playwright Chromium (browser agent) -----------------------------------
Step "Installing Chromium for the browser agent"
& $venvPy -m playwright install chromium
if ($LASTEXITCODE -ne 0) { Warn "Playwright Chromium install failed - the browser agent won't work until you run: .venv\Scripts\python -m playwright install chromium" }
else { Ok "Chromium ready." }

# --- 4. Ollama + local models (~10 GB) ----------------------------------------
Step "Setting up Ollama (local text + vision models)"
function Find-Ollama {
    $c = Get-Command ollama -ErrorAction SilentlyContinue
    if ($c) { return $c.Source }
    $p = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $p) { return $p }
    return $null
}
# Guard the winget install too, not just the pulls: on a skipped run we must
# not leave a ~1 GB Ollama on the machine we were told to keep clean.
$ollama = $null
if (-not $skipOllama) {
    $ollama = Find-Ollama
    if (-not $ollama -and $winget) {
        Warn "Ollama not found - installing via winget ..."
        winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements --silent
        $ollama = Find-Ollama
    }
}
$ollamaOk = $false
if ($skipOllama) {
    Warn "Skipped (QUILL_INSTALL_SKIP_OLLAMA=1) - cloud-only."
} elseif ($ollama) {
    try { & $ollama list *> $null } catch {}
    if ($LASTEXITCODE -ne 0) {
        Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden
        Start-Sleep -Seconds 6
    }
    Warn "Pulling qwen2.5:7b-instruct (~4.7 GB) - this is the long part ..."
    & $ollama pull qwen2.5:7b-instruct
    $t = ($LASTEXITCODE -eq 0)
    Warn "Pulling minicpm-v (~5.5 GB) ..."
    & $ollama pull minicpm-v
    $ollamaOk = ($t -and $LASTEXITCODE -eq 0)
}
if ($ollamaOk) { Ok "Local models ready (text: qwen2.5:7b-instruct, vision: minicpm-v)." }
else { Warn "Ollama models unavailable - Mnemos will run cloud-only (higher API usage). Install Ollama from ollama.com, run 'ollama pull qwen2.5:7b-instruct' and 'ollama pull minicpm-v', then set QUILL_TEXT_LOCAL=1 in .env." }

# --- 5. Speech / embedding models ---------------------------------------------
Step "Pre-downloading speech + embedding models (Whisper, VAD, speaker, MiniLM)"
Write-Host "    ~700 MB on a fresh machine. Each model retries and resumes, so a"
Write-Host "    dropped connection costs the remainder, not the whole download."
& $venvPy (Join-Path $root 'scripts\download_models.py') --retries 5
if ($LASTEXITCODE -ne 0) {
  Warn "Some models did not finish downloading. Re-run install.bat when you have a steady connection - what already downloaded is kept and partial files continue where they stopped. Mnemos still starts; it fetches whatever is missing on first use."
} else { Ok "Speech + embedding models cached." }

# --- 6. .env + Anthropic API key -----------------------------------------------
Step "Configuring .env"
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) { Copy-Item (Join-Path $root '.env.example') $envPath }

$keyCheck = "import sys, anthropic`ntry:`n    anthropic.Anthropic(api_key=sys.argv[1]).models.list()`n    print('    key OK')`nexcept Exception as e:`n    print('    key check failed: ' + type(e).__name__)`n    sys.exit(1)"

# WS-D Tier 1: most testers are handed an invite code and never touch an
# Anthropic console. The code is exchanged for a key the operator pre-created;
# that key is written into THIS machine's .credentials.env and used directly,
# so nothing routes through the operator after install. The bring-your-own-key
# path below is unchanged for anyone who prefers it.
$inviteRedeem = @'
import sys
sys.path.insert(0, sys.argv[2])
from app.services.invite import InviteError, redeem_and_save
try:
    out = redeem_and_save(sys.argv[1])
    print("    invite accepted" + (" for " + out["label"] if out.get("label") else ""))
except InviteError as exc:
    print("    " + str(exc))
    sys.exit(1)
'@

$key = ''
$invited = $false
# The operator ships the vending endpoint in .env.example, so a tester never
# has to know it exists. An env var still wins for a one-off install.
$inviteUrl = $env:QUILL_INVITE_URL
if (-not $inviteUrl) {
    $inviteLine = (Get-Content $envPath -ErrorAction SilentlyContinue |
                   Where-Object { $_ -match '^QUILL_INVITE_URL=(.+)$' } |
                   Select-Object -First 1)
    if ($inviteLine) { $inviteUrl = $inviteLine -replace '^QUILL_INVITE_URL=', '' }
}
if ($nonInteractive) {
    # Same two paths as the prompt, taken from the environment instead of a
    # human: an invite code, else a key, else neither (a valid outcome - the
    # app installs and runs, chat waits for a key in .env).
    if ($inviteUrl) { $env:QUILL_INVITE_URL = $inviteUrl.Trim() }
    $envCode = $env:QUILL_INVITE_CODE
    if ($inviteUrl -and $envCode) {
        & $venvPy -c $inviteRedeem $envCode.Trim() $root
        if ($LASTEXITCODE -eq 0) { $invited = $true; Ok "Connected with your invite code." }
        else { Warn "QUILL_INVITE_CODE was refused - continuing without a model account." }
    }
    if (-not $invited) {
        $envKey = $env:ANTHROPIC_API_KEY
        if ($envKey) {
            & $venvPy -c $keyCheck $envKey.Trim()
            if ($LASTEXITCODE -eq 0) { $key = $envKey.Trim() }
            else { Warn "ANTHROPIC_API_KEY did not validate - leaving .env without a key." }
        } else {
            Warn "No QUILL_INVITE_CODE or ANTHROPIC_API_KEY set - add a key to .env before chat works."
        }
    }
} elseif ($inviteUrl) {
    $env:QUILL_INVITE_URL = $inviteUrl.Trim()
    Write-Host ""
    Write-Host "  How do you want to connect Mnemos to Claude?"
    Write-Host "    [1] I have an invite code  (recommended - nothing to sign up for)"
    Write-Host "    [2] I have my own Anthropic API key"
    Write-Host "    [3] Skip for now"
    while ($true) {
        $choice = (Read-Host "  Choose 1, 2 or 3").Trim()
        if ($choice -eq '3' -or $choice -eq '') { break }
        if ($choice -eq '2') { break }
        if ($choice -ne '1') { continue }
        $code = (Read-Host "  Invite code (like ABCD-EFGH-JKLM)").Trim()
        if (-not $code) { continue }
        & $venvPy -c $inviteRedeem $code $root
        if ($LASTEXITCODE -eq 0) { $invited = $true; Ok "Connected with your invite code."; break }
        Warn "Try again, or choose 2 to paste your own key."
    }
}

if ((-not $nonInteractive) -and (-not $invited)) {
    while ($true) {
        $key = Read-Host "Paste YOUR Anthropic API key (sk-ant-..., from console.anthropic.com) or press Enter to add it to .env later"
        if (-not $key) { Warn "Skipped - Mnemos needs ANTHROPIC_API_KEY in .env before chat works."; break }
        & $venvPy -c $keyCheck $key.Trim()
        if ($LASTEXITCODE -eq 0) { $key = $key.Trim(); break }
        Warn "That key didn't validate. Try again, or press Enter to skip."
    }
}

$lines = Get-Content $envPath
if ($key)     { $lines = $lines -replace '^ANTHROPIC_API_KEY=.*', "ANTHROPIC_API_KEY=$key" }
if ($ollamaOk) {
    $lines = $lines -replace '^#?QUILL_TEXT_LOCAL=.*', 'QUILL_TEXT_LOCAL=1'
    $lines = $lines -replace '^#?QUILL_TEXT_LOCAL_MODEL=.*', 'QUILL_TEXT_LOCAL_MODEL=qwen2.5:7b-instruct'
}
[IO.File]::WriteAllLines($envPath, $lines)
Ok ".env written (never share this file - it holds your key)."

# --- Done ----------------------------------------------------------------------
Write-Host ""
Write-Host "=============================================================" -ForegroundColor Green
Write-Host "  Mnemos is installed." -ForegroundColor Green
Write-Host "  Start it:   double-click start.bat"
Write-Host "  Then open:  http://127.0.0.1:8000"
Write-Host ""
Write-Host "  Notes for testers:"
Write-Host "   - Everything runs and stays on THIS machine (localhost only)."
Write-Host "   - Mic/screen/camera capture is OFF until you consent in the UI."
Write-Host "   - Ambient cloud spend is capped at `$2/day by default."
Write-Host "=============================================================" -ForegroundColor Green
