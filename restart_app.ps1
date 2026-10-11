# Restarts the voice app and closes the old console window (if there is one) so no "Press any key" box is left behind.
# The app now normally runs windowless (pythonw.exe, started by start.bat); the '^python' pattern matches both
# python.exe and pythonw.exe. Run detached.
#
# It waits for the assistant to FINISH TALKING before killing it (bug 10 Oct 2026: a fixed delay cut AIDA off in
# the middle of its own reply). The app writes state\app_status.json twice a second ({t, working, speaking}); we wait
# until it has been idle (not working, not speaking) for $QuietSeconds in a row, after at least $Delay seconds.
# If the status file is stale (app hung or already dead) or $MaxWait passes, we go ahead anyway.
param([int]$Delay = 5, [int]$QuietSeconds = 3, [int]$MaxWait = 120)
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Start-Sleep $Delay
$statusFile = Join-Path $root 'state\app_status.json'
$started = Get-Date
$quietSince = $null
while (((Get-Date) - $started).TotalSeconds -lt $MaxWait) {
    $idle = $false
    try {
        $s = Get-Content $statusFile -Raw -ErrorAction Stop | ConvertFrom-Json
        $age = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - [double]$s.t
        if ($age -gt 10) { $idle = $true }                          # stale: nothing left to wait for
        elseif (-not $s.working -and -not $s.speaking) { $idle = $true }
    } catch { $idle = $true }                                        # no status file (old app): do not wait forever
    if ($idle) {
        if ($null -eq $quietSince) { $quietSince = Get-Date }
        if (((Get-Date) - $quietSince).TotalSeconds -ge $QuietSeconds) { break }
    } else { $quietSince = $null }
    Start-Sleep -Milliseconds 400
}
$apps = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'voice_claude\.py' -and $_.Name -match '^python' }
$kill = @()
foreach ($a in $apps) {
    $kill += $a.ProcessId
    $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($a.ParentProcessId)" -ErrorAction SilentlyContinue
    # the parent is the launcher python (venv shim) or the cmd.exe running start.bat; take cmd.exe parents too
    while ($p -and $p.Name -match '^(python|cmd)') {
        $kill += $p.ProcessId
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
    }
}
$kill | Sort-Object -Unique | ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }
Start-Sleep 3
# start.bat shows package installs in a console, then (chat_window: true) starts pythonw and closes itself.
Start-Process -FilePath (Join-Path $root 'start.bat') -WorkingDirectory $root
