<#
.SYNOPSIS  Removes what install.ps1 added outside the AIDA folder (shortcuts), and optionally the folder itself.
.DESCRIPTION
  Does NOT uninstall Python, Claude Code, Git or the GitHub CLI (you may use them for other things; remove them
  from Settings > Apps if you want). Your GitHub repo, if you made one, is untouched.
  -RemoveFolder also deletes the AIDA folder (venv, models, config.yaml AND your memory). Use -DryRun first.
#>
[CmdletBinding()]
param([switch]$RemoveFolder, [Alias("WhatIf")][switch]$DryRun)

$dir = $PSScriptRoot
function Act($what, [scriptblock]$do) {
    if ($DryRun) { Write-Host "[dry-run] would: $what" -ForegroundColor DarkYellow } else { Write-Host $what; & $do }
}
$links = @(
    (Join-Path ([Environment]::GetFolderPath("Desktop")) "AIDA.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Programs")) "AIDA.lnk"),
    (Join-Path ([Environment]::GetFolderPath("Startup")) "AIDA.lnk")
)
foreach ($l in $links) { if (Test-Path $l) { Act "remove shortcut $l" { Remove-Item $l -Force } } }

if ($RemoveFolder) {
    $ans = if ($DryRun) { "y" } else { Read-Host "Delete $dir including your memory and settings? Type DELETE to confirm" }
    if ($ans -eq "DELETE" -or $DryRun) {
        Act "delete $dir (a small cleanup script runs after this window closes)" {
            Start-Process powershell -WindowStyle Hidden -ArgumentList "-NoProfile", "-Command", "Start-Sleep 3; Remove-Item -LiteralPath '$dir' -Recurse -Force"
        }
    } else { Write-Host "Folder kept." }
} else {
    Write-Host "Shortcuts removed. The folder $dir is untouched (delete it yourself, or re-run with -RemoveFolder)."
}
Write-Host "Whisper model cache (optional to delete): $env:USERPROFILE\.cache\huggingface\hub\models--Systran--faster-whisper-small.en"
