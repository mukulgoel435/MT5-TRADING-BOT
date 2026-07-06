# install_xm_mt5.ps1 - Download and install XM Global MT5 terminal
# Run: powershell -ExecutionPolicy Bypass -File scripts\install_xm_mt5.ps1

$url       = "https://download.mql5.com/cdn/web/xm.global.limited/mt5/xmglobal5setup.exe"
$dest      = "$env:USERPROFILE\Downloads\xmglobal5setup.exe"
$installed = "C:\Program Files\XM Global MT5\terminal64.exe"

Write-Host ""
Write-Host "========================================"
Write-Host "  XM Global MT5 - Auto Installer"
Write-Host "========================================"

if (Test-Path $installed) {
    Write-Host ""
    Write-Host "[OK] XM MT5 already installed at:"
    Write-Host "     $installed"
    Write-Host ""
    Write-Host "No action needed. Run check_broker.py next."
    exit 0
}

Write-Host ""
Write-Host "[1/2] Downloading XM MT5 installer (~6 MB)..."
Write-Host "      From: $url"
Write-Host "      To:   $dest"
Write-Host ""

$wc = New-Object System.Net.WebClient
$wc.Headers.Add("User-Agent", "Mozilla/5.0")
try {
    $wc.DownloadFile($url, $dest)
} catch {
    Write-Host "[FAIL] Download error: $_"
    Write-Host ""
    Write-Host "Manual option:"
    Write-Host "  1. Open https://www.xm.com/mt5 in browser"
    Write-Host "  2. Click 'Download for Windows'"
    Write-Host "  3. Run xmglobal5setup.exe"
    exit 1
}

$size = (Get-Item $dest).Length
Write-Host "[OK] Downloaded - $([math]::Round($size/1MB,1)) MB"

Write-Host ""
Write-Host "[2/2] Launching installer - accept UAC prompt to continue..."
Write-Host ""

Start-Process -FilePath $dest -ArgumentList "/auto" -Wait

Write-Host ""
if (Test-Path $installed) {
    Write-Host "========================================"
    Write-Host "[SUCCESS] XM MT5 installed at:"
    Write-Host "  $installed"
    Write-Host ""
    Write-Host "Next steps:"
    Write-Host "  1. Create config_xm.py with your XM credentials:"
    Write-Host "       LOGIN    = <your_login>"
    Write-Host "       PASSWORD = '<your_password>'"
    Write-Host "       SERVER   = 'XMGlobal-MT5 7'"
    Write-Host ""
    Write-Host "  2. Run broker check:"
    Write-Host "       py -3 scripts\check_broker.py"
    Write-Host "========================================"
} else {
    Write-Host "[NOTE] Terminal not found at default path: $installed"
    Write-Host "       If you used a custom folder, update XM_MT5_PATH in"
    Write-Host "       scripts\check_broker.py to match."
}
