# watchdog.ps1 — Auto-pull GitHub updates and restart bot when safe
#
# Runs forever in background. Every 15 minutes:
#   1. Checks if GitHub has newer code
#   2. If YES and no open trades  → pulls + restarts immediately
#   3. If YES but trades are open → waits until all positions close, then updates
#   4. If bot crashes             → auto-restarts it
#
# Start once on the remote machine:
#   powershell -ExecutionPolicy Bypass -WindowStyle Hidden -File watchdog.ps1

$botDir    = $PSScriptRoot
$py        = "python"
$statusFile = Join-Path $botDir "data\h_xm_live_status.json"
$checkEvery = 900   # seconds between GitHub checks (15 min)

function Write-Log($msg) {
    $ts = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd HH:mm:ss")
    Write-Host "$ts UTC | WATCHDOG | $msg"
}

function Get-HasOpenPositions {
    if (-not (Test-Path $statusFile)) { return $false }
    try {
        $s = Get-Content $statusFile -Raw | ConvertFrom-Json
        foreach ($k in $s.instruments.PSObject.Properties.Name) {
            if ($s.instruments.$k.has_position) { return $true }
        }
    } catch {}
    return $false
}

function Get-BotRunning {
    $procs = Get-WmiObject Win32_Process -Filter "Name='python.exe'"
    return ($procs | Where-Object { $_.CommandLine -like "*run_live_h_xm*" }).Count -gt 0
}

function Start-Bot {
    Start-Process -FilePath $py `
        -ArgumentList "scripts\run_live_h_xm.py" `
        -WorkingDirectory $botDir -WindowStyle Normal
    Start-Sleep -Seconds 2
    # Start dashboard only if not already running
    $dash = Get-WmiObject Win32_Process -Filter "Name='python.exe'" |
            Where-Object { $_.CommandLine -like "*web_dashboard*" }
    if (-not $dash) {
        Start-Process -FilePath $py `
            -ArgumentList "scripts\web_dashboard.py" `
            -WorkingDirectory $botDir -WindowStyle Normal
    }
    Write-Log "Bot (re)started."
}

function Get-UpdateAvailable {
    git -C $botDir fetch origin 2>$null | Out-Null
    $status = git -C $botDir status -uno 2>&1
    return ($status -match "Your branch is behind")
}

# ── Main loop ─────────────────────────────────────────────────────────────────
Write-Log "Watchdog started. Checking every $($checkEvery/60) minutes."
$pendingUpdate = $false

while ($true) {
    # ── Crash guard: restart bot if it stopped unexpectedly ───────────────────
    if (-not (Get-BotRunning)) {
        Write-Log "Bot not running — restarting..."
        Start-Bot
    }

    # ── Check GitHub for updates ──────────────────────────────────────────────
    if (Get-UpdateAvailable) {
        $pendingUpdate = $true
        Write-Log "Update available on GitHub."
    }

    # ── Apply update when safe (no open positions) ────────────────────────────
    if ($pendingUpdate) {
        if (-not (Get-HasOpenPositions)) {
            Write-Log "No open positions — applying update now..."
            & "$botDir\update.ps1"
            $pendingUpdate = $false
        } else {
            Write-Log "Update pending — waiting for open positions to close..."
        }
    }

    Start-Sleep -Seconds $checkEvery
}
