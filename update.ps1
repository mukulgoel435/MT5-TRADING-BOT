# update.ps1 — Pull latest code from GitHub and restart bot safely
# Run manually: .\update.ps1

$botDir = $PSScriptRoot
$py     = "python"

Write-Host ""
Write-Host "========================================"
Write-Host "  MT5 Bot — Update + Restart"
Write-Host "========================================"

# ── Pull latest code ──────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[1/3] Pulling latest code from GitHub..."
git -C $botDir pull
if ($LASTEXITCODE -ne 0) {
    Write-Host "[FAIL] git pull failed. Check your internet or repo access."
    exit 1
}
Write-Host "[OK] Code updated."

# ── Stop running bot + dashboard ──────────────────────────────────────────────
Write-Host ""
Write-Host "[2/3] Stopping existing processes..."

Get-WmiObject Win32_Process -Filter "Name='python.exe'" | ForEach-Object {
    $cmd = $_.CommandLine
    if ($cmd -like "*run_live_h_xm*" -or $cmd -like "*web_dashboard*") {
        Write-Host "  Stopping PID $($_.ProcessId) — $($cmd.Split('\')[-1])"
        Stop-Process -Id $_.ProcessId -Force
    }
}
Start-Sleep -Seconds 3
Write-Host "[OK] Processes stopped."

# ── Start bot + dashboard ─────────────────────────────────────────────────────
Write-Host ""
Write-Host "[3/3] Starting bot and dashboard..."

Start-Process -FilePath $py `
    -ArgumentList "scripts\run_live_h_xm.py" `
    -WorkingDirectory $botDir -WindowStyle Normal

Start-Sleep -Seconds 2

Start-Process -FilePath $py `
    -ArgumentList "scripts\web_dashboard.py" `
    -WorkingDirectory $botDir -WindowStyle Normal

Write-Host "[OK] Bot started.       (run_live_h_xm.py)"
Write-Host "[OK] Dashboard started. (http://localhost:5000)"
Write-Host ""
Write-Host "========================================"
Write-Host "  Update complete."
Write-Host "========================================"
Write-Host ""
