# setup.ps1 — Full new-machine setup for MT5 Trading Bot
# Run once after cloning:
#   powershell -ExecutionPolicy Bypass -File setup.ps1

Write-Host ""
Write-Host "================================================"
Write-Host "  MT5 Trading Bot — New Machine Setup"
Write-Host "================================================"
Write-Host ""

# ── Step 1: Python packages ───────────────────────────────────────────────────
Write-Host "[1/4] Installing Python packages..."
python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] pip install failed. Make sure Python 3.11+ is installed."
    exit 1
}
Write-Host "[OK] Python packages installed."
Write-Host ""

# ── Step 2: XM MT5 terminal ───────────────────────────────────────────────────
Write-Host "[2/4] Checking XM MT5 terminal..."
powershell -ExecutionPolicy Bypass -File scripts\install_xm_mt5.ps1
Write-Host ""

# ── Step 3: Data directories ──────────────────────────────────────────────────
Write-Host "[3/4] Creating data directories..."
New-Item -ItemType Directory -Force -Path data       | Out-Null
New-Item -ItemType Directory -Force -Path data\logs  | Out-Null
Write-Host "[OK] data\ and data\logs\ ready."
Write-Host ""

# ── Step 4: Credentials file ──────────────────────────────────────────────────
Write-Host "[4/4] Checking config_xm.py..."
if (Test-Path "config_xm.py") {
    Write-Host "[OK] config_xm.py already exists."
} else {
    Write-Host "[MISSING] config_xm.py not found — creating template..."
    @"
# XM Global MT5 credentials — fill in your details
LOGIN    = 0           # your XM login number
PASSWORD = ""          # your XM password
SERVER   = "XMGlobal-MT5 7"
"@ | Out-File -FilePath "config_xm.py" -Encoding utf8
    Write-Host "[OK] config_xm.py created — EDIT IT NOW with your real credentials."
}

Write-Host ""
Write-Host "================================================"
Write-Host "  Setup complete. Next steps:"
Write-Host ""
Write-Host "  1. Edit config_xm.py with your XM credentials"
Write-Host "  2. Open XM MT5 terminal and log in once manually"
Write-Host "  3. Start the bot:"
Write-Host "       python scripts\run_live_h_xm.py"
Write-Host "  4. Start the dashboard:"
Write-Host "       python scripts\web_dashboard.py"
Write-Host "================================================"
Write-Host ""
