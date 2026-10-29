param([string]$Root = (Split-Path -Parent $PSScriptRoot))

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("hl-runtimes-" + [guid]::NewGuid())
New-Item -ItemType Directory -Force $tmp | Out-Null

function Get-Zip($url, $name) {
    $zip = Join-Path $tmp "$name.zip"
    Write-Host "Downloading $name..."
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    $out = Join-Path $tmp $name
    Expand-Archive -Path $zip -DestinationPath $out -Force
    return $out
}

try {
    $ff = Get-Zip "https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip" "ffmpeg"
    $ffDest = Join-Path $Root "vendor\ffmpeg"
    New-Item -ItemType Directory -Force $ffDest | Out-Null
    foreach ($exe in "ffmpeg.exe", "ffprobe.exe") {
        $src = Get-ChildItem -Path $ff -Recurse -Filter $exe | Select-Object -First 1
        if (-not $src) { throw "$exe not found in the FFmpeg download" }
        Copy-Item $src.FullName (Join-Path $ffDest $exe) -Force
    }

    $mtx = Get-Zip "https://github.com/bluenviron/mediamtx/releases/download/v1.21.2/mediamtx_v1.21.2_windows_amd64.zip" "mediamtx"
    $mtxDest = Join-Path $Root "vendor\mediamtx"
    New-Item -ItemType Directory -Force $mtxDest | Out-Null
    foreach ($f in "mediamtx.exe", "LICENSE") {
        $src = Get-ChildItem -Path $mtx -Recurse -Filter $f | Select-Object -First 1
        if ($src) { Copy-Item $src.FullName (Join-Path $mtxDest $f) -Force }
    }
    if (-not (Test-Path (Join-Path $mtxDest "mediamtx.exe"))) { throw "mediamtx.exe not found in the MediaMTX download" }

    Write-Host "Done: vendor\ffmpeg and vendor\mediamtx are ready."
}
finally {
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
