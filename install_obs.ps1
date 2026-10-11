# install_obs.ps1 - optional, idempotent OBS Studio setup for AIDA screen recording.
#   .\install_obs.ps1             winget install to the default location (needs install rights)
#   .\install_obs.ps1 -Portable   portable zip into %LOCALAPPDATA%\obs-studio-portable (no admin needed)
# Never overwrites an existing OBS install or WebSocket config. Does not start OBS.
param([switch]$Portable)
$ErrorActionPreference = "Stop"
$portDir = Join-Path $env:LOCALAPPDATA "obs-studio-portable"
$exe = "bin\64bit\obs64.exe"

function Write-WsConfig($file) {
    if (Test-Path $file) { Write-Host "WebSocket config already exists, leaving it alone: $file"; return }
    New-Item -ItemType Directory -Force (Split-Path $file) | Out-Null
    $json = '{"alerts_enabled":false,"auth_required":false,"first_load":false,"server_enabled":true,"server_password":"","server_port":4455}'
    [IO.File]::WriteAllText($file, $json, (New-Object Text.UTF8Encoding($false)))
    Write-Host "Wrote WebSocket config (enabled, port 4455, no password): $file"
}

if ($Portable) {
    if (Test-Path (Join-Path $portDir $exe)) { Write-Host "Portable OBS already at $portDir" }
    else {
        Write-Host "Finding latest OBS Studio release..."
        $rel = Invoke-RestMethod "https://api.github.com/repos/obsproject/obs-studio/releases/latest" -Headers @{ "User-Agent" = "aida-installer" }
        $asset = $rel.assets | Where-Object { $_.name -match '^OBS-Studio-[\d\.]+-Windows-x64\.zip$' } | Select-Object -First 1
        if (-not $asset) { throw "No portable x64 zip found in release $($rel.tag_name). Download it manually from https://github.com/obsproject/obs-studio/releases" }
        $zip = Join-Path $env:TEMP $asset.name
        Write-Host "Downloading $($asset.name)..."
        Invoke-WebRequest $asset.browser_download_url -OutFile $zip
        New-Item -ItemType Directory -Force $portDir | Out-Null
        Expand-Archive $zip -DestinationPath $portDir -Force
        Remove-Item -LiteralPath $zip -ErrorAction SilentlyContinue   # temp download only
        if (-not (Test-Path (Join-Path $portDir $exe))) { throw "Extract finished but $exe is missing." }
    }
    $flag = Join-Path $portDir "portable_mode"
    if (-not (Test-Path $flag)) { New-Item -ItemType File $flag | Out-Null }   # keeps OBS config inside the folder
    Write-WsConfig (Join-Path $portDir "config\obs-studio\plugin_config\obs-websocket\config.json")
}
else {
    $found = @("$env:ProgramFiles\obs-studio\$exe", "${env:ProgramFiles(x86)}\obs-studio\$exe", "$env:LOCALAPPDATA\Programs\obs-studio\$exe") | Where-Object { $_ -and (Test-Path $_) }
    if ($found) { Write-Host "OBS already installed: $($found[0])" }
    else {
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "winget not available. Re-run with -Portable." }
        winget install --id OBSProject.OBSStudio -e --accept-package-agreements --accept-source-agreements
        if ($LASTEXITCODE -ne 0) { throw "winget install failed (policy?). Re-run with -Portable." }
    }
    Write-WsConfig (Join-Path $env:APPDATA "obs-studio\plugin_config\obs-websocket\config.json")
}
Write-Host "`nDone. If OBS was already open, restart it so it reads the WebSocket setting."
Write-Host "Recordings go to C:\Users\<you>\AIDA Recordings (not Videos: Controlled Folder Access can block OBS there)."
Write-Host "If Defender still blocks OBS writing, allow obs64.exe under Windows Security > Ransomware protection > Controlled folder access."
