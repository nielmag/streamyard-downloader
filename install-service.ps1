# StreamYard Downloader — Windows Service Installer
# Right-click this file and choose "Run with PowerShell" (as Administrator)

$nssm     = "C:\Users\nielm\AppData\Local\Microsoft\WinGet\Packages\NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe\nssm-2.24-101-g897c7ad\win64\nssm.exe"
$appDir   = "C:\Users\nielm\video-pipeline\streamyard_app"
$python   = "$appDir\venv\Scripts\python.exe"
$appScript = "$appDir\app.py"
$logFile  = "$appDir\service.log"
$svcName  = "StreamYardDownloader"

Write-Host "Installing $svcName service..." -ForegroundColor Cyan

# Remove existing service if present
$existing = Get-Service -Name $svcName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Removing existing service..."
    & $nssm stop $svcName confirm
    & $nssm remove $svcName confirm
}

& $nssm install $svcName $python $appScript
& $nssm set $svcName AppDirectory $appDir
& $nssm set $svcName Start SERVICE_AUTO_START
& $nssm set $svcName AppStdout $logFile
& $nssm set $svcName AppStderr $logFile
& $nssm set $svcName AppRotateFiles 1
& $nssm set $svcName AppRotateBytes 5242880

# Start immediately
& $nssm start $svcName

$svc = Get-Service -Name $svcName -ErrorAction SilentlyContinue
if ($svc -and $svc.Status -eq "Running") {
    Write-Host "`nSuccess! StreamYard Downloader is running at http://localhost:5001" -ForegroundColor Green
    Write-Host "It will start automatically every time Windows boots."
} else {
    Write-Host "`nService installed but may not be running yet. Check service.log for errors." -ForegroundColor Yellow
    Write-Host "Log: $logFile"
}

Write-Host "`nPress Enter to close..."
Read-Host
