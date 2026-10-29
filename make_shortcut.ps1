$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$exe  = Join-Path $root "dist\HorizonLens\HorizonLens.exe"
$icon = Join-Path $root "assets\horizonlens.ico"

if (-not (Test-Path $exe)) {
    Write-Host "Build first: HorizonLens.exe not found at $exe" -ForegroundColor Yellow
    exit 1
}

$desktop = [Environment]::GetFolderPath("Desktop")
$lnk = Join-Path $desktop "HorizonLens.lnk"

$ws = New-Object -ComObject WScript.Shell
$sc = $ws.CreateShortcut($lnk)
$sc.TargetPath       = $exe
$sc.WorkingDirectory = Split-Path -Parent $exe
$sc.IconLocation     = $icon
$sc.Description       = "HorizonLens - phone camera for Forza Horizon 6"
$sc.Save()

Write-Host "Created desktop shortcut: $lnk" -ForegroundColor Green
