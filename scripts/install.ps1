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
$ollama = Find-Ollama
if (-not $ollama -and $winget) {
    Warn "Ollama not found - installing via winget ..."
    winget install -e --id Ollama.Ollama --accept-source-agreements --accept-package-agreements --silent
    $ollama = Find-Ollama
}
$ollamaOk = $false
if ($ollama) {
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
& $venvPy (Join-Path $root 'scripts\download_models.py')
if ($LASTEXITCODE -ne 0) { Warn "Model pre-download hit an error - Mnemos will fetch them on first run instead." }
else { Ok "Speech + embedding models cached." }

# --- 6. .env + Anthropic API key -----------------------------------------------
Step "Configuring .env"
$envPath = Join-Path $root '.env'
if (-not (Test-Path $envPath)) { Copy-Item (Join-Path $root '.env.example') $envPath }

$keyCheck = "import sys, anthropic`ntry:`n    anthropic.Anthropic(api_key=sys.argv[1]).models.list()`n    print('    key OK')`nexcept Exception as e:`n    print('    key check failed: ' + type(e).__name__)`n    sys.exit(1)"
$key = ''
while ($true) {
    $key = Read-Host "Paste YOUR Anthropic API key (sk-ant-..., from console.anthropic.com) or press Enter to add it to .env later"
    if (-not $key) { Warn "Skipped - Mnemos needs ANTHROPIC_API_KEY in .env before chat works."; break }
    & $venvPy -c $keyCheck $key.Trim()
    if ($LASTEXITCODE -eq 0) { $key = $key.Trim(); break }
    Warn "That key didn't validate. Try again, or press Enter to skip."
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
