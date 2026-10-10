<#
.SYNOPSIS
  AIDA one-command installer for Windows 10/11. Safe to run again at any time (it only does what is missing).

.DESCRIPTION
  One line (PowerShell, no admin needed):
      irm https://raw.githubusercontent.com/stagged1-cloud/AIDA/main/install.ps1 | iex

  Does, in order: gets the project, updates it from upstream, installs Python 3.13 + Claude Code + GitHub CLI
  (winget / official installers, per-user where possible), checks the VC++ runtime and your GPU, builds a private
  Python environment, downloads the voice + speech models from their public sources, runs a short first-run
  wizard (name, voice, microphone), creates shortcuts, links YOUR OWN GitHub copy (optional), then prints a
  pass/fail checklist.

  Use -DryRun to see everything it WOULD do without changing anything.

.PARAMETER InstallDir      Where AIDA lives. Default: this folder if run from a checkout, else %USERPROFILE%\AIDA.
.PARAMETER Upstream        owner/repo of the project to install from (the "upstream" remote).
.PARAMETER RepoName        Name of YOUR copy on your GitHub account. Default aida-assistant.
.PARAMETER Visibility      private (default) or public, for your copy.
.PARAMETER DryRun          Print what would happen; change nothing. (Alias: -WhatIf)
.PARAMETER NoGitHub        Skip the GitHub step entirely (AIDA works fine without it).
.PARAMETER NoWizard        Skip the first-run questions (config.yaml gets defaults).
.PARAMETER SkipModels      Do not download the voice/speech models.
.PARAMETER Cpu             Treat this PC as having no NVIDIA GPU (never try CUDA).
.PARAMETER NoShortcut      Do not create Desktop / Start Menu shortcuts.
.PARAMETER StartWithWindows  Also start AIDA when you sign in to Windows.
.PARAMETER SyncMemory      Opt in to pushing learned memory to YOUR OWN repo (never upstream).
.PARAMETER Yes             Never ask questions; take the defaults.
.PARAMETER SkipClaude      Do not install/check Claude Code.
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$Upstream = "stagged1-cloud/AIDA",
    [string]$Branch = "main",
    [string]$RepoName = "aida-assistant",
    [ValidateSet("private", "public")][string]$Visibility = "private",
    [Alias("WhatIf")][switch]$DryRun,
    [switch]$NoGitHub,
    [switch]$NoWizard,
    [switch]$SkipModels,
    [switch]$Cpu,
    [switch]$NoShortcut,
    [switch]$StartWithWindows,
    [switch]$SyncMemory,
    [switch]$Yes,
    [switch]$SkipClaude
)

$ErrorActionPreference = "Continue"      # native tools write to stderr; exit codes are checked by hand
$ProgressPreference = "SilentlyContinue"
if ($env:AIDA_DRYRUN) { $DryRun = $true }
if ($env:AIDA_YES) { $Yes = $true }
if ($env:AIDA_DIR -and -not $InstallDir) { $InstallDir = $env:AIDA_DIR }
if ($env:AIDA_UPSTREAM -and $Upstream -match '^OWNER/') { $Upstream = $env:AIDA_UPSTREAM }

$script:Checks = New-Object System.Collections.ArrayList
$script:Next = New-Object System.Collections.ArrayList
$script:Offline = $false

# ----------------------------------------------------------------------------- helpers
function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
function Info($m) { Write-Host "    $m" }
function Add-Check($status, $name, $detail) { [void]$script:Checks.Add([pscustomobject]@{ Status = $status; Name = $name; Detail = $detail }) }
function Add-Next($m) { [void]$script:Next.Add($m) }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
function Refresh-Path {
    $extra = @("$env:USERPROFILE\.local\bin", "$env:LOCALAPPDATA\Programs\Python\Python313", "$env:LOCALAPPDATA\Programs\Python\Python313\Scripts",
               "$env:ProgramFiles\GitHub CLI", "$env:ProgramFiles\Git\cmd", "$env:LOCALAPPDATA\Programs\Git\cmd") -join ";"
    $env:Path = ([Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $extra)
}
function Ask($question, $default) {
    if ($Yes -or $DryRun -or -not [Environment]::UserInteractive) { return $default }
    $a = Read-Host "$question [$default]"
    if ([string]::IsNullOrWhiteSpace($a)) { return $default }
    return $a.Trim()
}
function Ask-YesNo($question, $defaultYes) {
    $d = if ($defaultYes) { "Y/n" } else { "y/N" }
    $a = Ask "$question ($d)" ""
    if ($a -eq "" -or $a -eq "$d") { return $defaultYes }
    return $a -match "^[yY]"
}
# Run an action, or just say what it would do in -DryRun. Failures are recorded, not fatal, unless -Critical.
function Act($desc, [scriptblock]$act, [switch]$Critical) {
    if ($DryRun) { Write-Host "    [dry-run] would: $desc" -ForegroundColor DarkYellow; return $true }
    try { & $act | Out-Host; return $true }
    catch {
        Write-Host "    FAILED: $desc : $($_.Exception.Message)" -ForegroundColor Red
        if ($Critical) { throw }
        return $false
    }
}
function Test-Online {
    try { Invoke-WebRequest "https://github.com" -Method Head -UseBasicParsing -TimeoutSec 8 | Out-Null; return $true } catch { return $false }
}
function Winget-Install($id, $extra) {
    if (-not (Have "winget")) { throw "winget is missing. Install 'App Installer' from the Microsoft Store (or install $id by hand), then run this again." }
    & winget install --id $id -e --silent --accept-package-agreements --accept-source-agreements @extra
    Refresh-Path
}
function Git-Out { param([string[]]$a) $r = & git -C $script:Dir @a 2>$null; if ($LASTEXITCODE -ne 0) { return $null }; return ($r -join "`n").Trim() }

# ----------------------------------------------------------------------------- where am I
Write-Host ""
Write-Host "AIDA installer" -ForegroundColor Green
if ($DryRun) { Write-Host "DRY RUN: nothing will be installed, downloaded, created or changed." -ForegroundColor Yellow }

$here = $PSScriptRoot
$inRepo = [bool]($here -and (Test-Path (Join-Path $here "voice_claude.py")))
$explicit = $PSBoundParameters.ContainsKey("InstallDir") -or [bool]$env:AIDA_DIR
if (-not $InstallDir) { $InstallDir = if ($inRepo) { $here } else { Join-Path $env:USERPROFILE "AIDA" } }
$InstallDir = [IO.Path]::GetFullPath($InstallDir)
$script:Dir = $InstallDir

Refresh-Path
if (-not [Environment]::Is64BitOperatingSystem) { throw "AIDA needs 64-bit Windows." }
if ([Environment]::OSVersion.Version.Major -lt 10) { throw "AIDA needs Windows 10 or 11." }
Add-Check "PASS" "Windows 64-bit, PowerShell $($PSVersionTable.PSVersion)" ""

# ----------------------------------------------------------------------------- 0. get the project
$needFetch = (-not $inRepo) -or ($explicit -and ($InstallDir.TrimEnd('\') -ne ([string]$here).TrimEnd('\')))
if ($needFetch) {
    Step "Getting AIDA into $InstallDir"
    $present = Test-Path (Join-Path $InstallDir "voice_claude.py")
    if ($present) { Info "Already there." }
    elseif ($inRepo) {
        Act "copy the project from $here to $InstallDir" {
            New-Item -ItemType Directory -Force $InstallDir | Out-Null
            & robocopy $here $InstallDir /E /XD .git .venv models __pycache__ logs tmp /XF config.yaml state.json /NFL /NDL /NJH /NJS | Out-Null
        } -Critical | Out-Null
    } else {
        if ($Upstream -match '^OWNER/') { throw "Set the project to install from: -Upstream owner/repo (or `$env:AIDA_UPSTREAM)." }
        if (-not (Test-Online)) { throw "No internet connection (could not reach github.com). Connect and run this again." }
        if (-not (Have "git") -and (Have "winget")) {
            Info "Git is missing; installing it (the Git installer may ask for permission)."
            Act "winget install Git.Git" { Winget-Install "Git.Git" @() } | Out-Null
        }
        if (Have "git") {
            Act "git clone https://github.com/$Upstream.git $InstallDir" {
                New-Item -ItemType Directory -Force (Split-Path $InstallDir) | Out-Null
                & git clone --branch $Branch "https://github.com/$Upstream.git" $InstallDir
                if ($LASTEXITCODE -ne 0) { throw "git clone failed (is the repo public, and the name right?)" }
            } -Critical | Out-Null
        } else {
            Act "download and unzip https://github.com/$Upstream/archive/refs/heads/$Branch.zip" {
                $zip = Join-Path $env:TEMP "aida-src.zip"; $tmp = Join-Path $env:TEMP "aida-src"
                Invoke-WebRequest "https://github.com/$Upstream/archive/refs/heads/$Branch.zip" -OutFile $zip -UseBasicParsing
                if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
                Expand-Archive $zip $tmp
                New-Item -ItemType Directory -Force $InstallDir | Out-Null
                Copy-Item (Join-Path (Get-ChildItem $tmp | Select-Object -First 1).FullName "*") $InstallDir -Recurse -Force
            } -Critical | Out-Null
        }
    }
    if ($DryRun) { Add-Check "INFO" "Project would be placed in $InstallDir" "dry run stops here for the fetch step"; }
    elseif (Test-Path (Join-Path $InstallDir "install.ps1")) {
        Info "Continuing with the installer inside $InstallDir"
        $fwd = @{}
        foreach ($k in $PSBoundParameters.Keys) { $fwd[$k] = $PSBoundParameters[$k] }
        $fwd["InstallDir"] = $InstallDir
        & (Join-Path $InstallDir "install.ps1") @fwd
        return
    }
}
if ($DryRun -and -not (Test-Path (Join-Path $InstallDir "voice_claude.py"))) {
    Info "(dry run) $InstallDir does not exist yet; the remaining steps are described as they would run there."
}

# ----------------------------------------------------------------------------- 1. update from upstream
$isGit = Test-Path (Join-Path $InstallDir ".git")
if ($isGit -and (Have "git")) {
    $up = Git-Out @("remote", "get-url", "upstream")
    if ($up) {
        Step "Checking for updates from upstream"
        if (-not (Test-Online)) { $script:Offline = $true; Info "Offline: skipping the update check."; Add-Check "WARN" "Update check" "offline" }
        else {
            $dirty = Git-Out @("status", "--porcelain", "--untracked-files=no")
            $cur = Git-Out @("rev-parse", "--abbrev-ref", "HEAD")
            if ($dirty) { Info "You have local changes; not touching them. To update later: git pull upstream $Branch"; Add-Check "INFO" "Update" "local changes present, skipped" }
            elseif ($cur -ne $Branch) { Info "On branch '$cur', not '$Branch'; skipping the update." }
            else {
                Act "git fetch upstream; git merge --ff-only upstream/$Branch" {
                    & git -C $InstallDir fetch upstream 2>$null
                    $before = Git-Out @("rev-parse", "HEAD")
                    & git -C $InstallDir merge --ff-only "upstream/$Branch" 2>$null | Out-Null
                    $after = Git-Out @("rev-parse", "HEAD")
                    if ($before -ne $after) { Info "Updated to the latest version." } else { Info "Already up to date." }
                } | Out-Null
                Add-Check "PASS" "Project is up to date with upstream" ""
            }
        }
    }
}

# ----------------------------------------------------------------------------- 2. Python
Step "Python (3.10 to 3.13; 3.13 recommended)"
function Find-Python {
    $cands = @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3.11"), @("py", "-3.10"), @("python"), @("python3"))
    foreach ($c in $cands) {
        if (-not (Have $c[0])) { continue }
        try {
            $args2 = @()
            if ($c.Count -gt 1) { $args2 += $c[1] }
            $args2 += @("-c", "import sys;print('%d.%d' % sys.version_info[:2]);print(sys.executable)")
            $out = & $c[0] @args2 2>$null
            if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
            $ver = [version]$out[0]
            if ($ver -ge [version]"3.10" -and $ver -le [version]"3.13") { return [pscustomobject]@{ Exe = $out[1]; Version = $out[0] } }
        } catch { }
    }
    return $null
}
$py = Find-Python
if ($py) { Info "Found Python $($py.Version) at $($py.Exe)"; Add-Check "PASS" "Python $($py.Version)" $py.Exe }
else {
    Info "No suitable Python. Installing Python 3.13 for your user (no admin needed)."
    Act "winget install Python.Python.3.13 --scope user" { Winget-Install "Python.Python.3.13" @("--scope", "user") } -Critical | Out-Null
    $py = Find-Python
    if ($py) { Add-Check "PASS" "Python $($py.Version) (just installed)" $py.Exe }
    elseif ($DryRun) { Add-Check "INFO" "Python 3.13 would be installed" "winget, per-user"; $py = [pscustomobject]@{ Exe = "python"; Version = "3.13" } }
    else { Add-Check "FAIL" "Python" "not found after install; close this window, open a new PowerShell, run the installer again"; throw "Python not found after installing." }
}

# ----------------------------------------------------------------------------- 3. VC++ runtime, GPU
Step "System checks"
$vc = $false
try { $vc = ((Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction Stop).Installed -eq 1) } catch { }
if ($vc) { Add-Check "PASS" "Visual C++ runtime" "" }
else {
    Info "The Visual C++ runtime is missing. Installing it needs ADMIN (Windows will ask for permission)."
    $ok = Act "winget install Microsoft.VCRedist.2015+.x64 (admin prompt)" { Winget-Install "Microsoft.VCRedist.2015+.x64" @() }
    Add-Check $(if ($DryRun) { "INFO" } elseif ($ok) { "PASS" } else { "WARN" }) "Visual C++ runtime" $(if ($DryRun) { "would be installed (admin)" } else { "if AIDA fails to start, install 'Microsoft Visual C++ 2015-2022 Redistributable (x64)'" })
}
$gpu = $false
if (-not $Cpu) {
    try { $gpu = [bool](Get-CimInstance Win32_VideoController -ErrorAction Stop | Where-Object { $_.Name -match "NVIDIA" }) } catch { }
    if ($gpu -and -not (Have "nvidia-smi")) { Info "NVIDIA GPU found but no driver tools (nvidia-smi); GPU speech recognition may fall back to CPU." }
}
if ($gpu) { Add-Check "PASS" "NVIDIA GPU detected" "GPU speech recognition packages will be installed (about 1.5 GB extra)" }
else { Add-Check "INFO" "No NVIDIA GPU (or -Cpu)" "speech recognition runs on the CPU; fine, just a little slower" }
$mics = $null
try { $mics = @(Get-CimInstance Win32_PnPEntity -ErrorAction Stop | Where-Object { $_.PNPClass -eq "AudioEndpoint" -and $_.Name -match "mic|input|headset|array" }) } catch { }
if ($mics -and $mics.Count) { Add-Check "PASS" "Microphone present" $mics[0].Name } else { Add-Check "WARN" "Microphone" "none detected by Windows yet; the wizard tests it later" }

# ----------------------------------------------------------------------------- 4. Claude Code
if (-not $SkipClaude) {
    Step "Claude Code (the brain; it needs YOUR Claude account)"
    if (Have "claude") { Info "claude is already installed."; Add-Check "PASS" "Claude Code installed" ((& claude --version 2>$null) -join " ") }
    else {
        $ok = Act "irm https://claude.ai/install.ps1 | iex   (official Claude Code installer, per-user)" {
            Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression
            Refresh-Path
            if (-not (Have "claude")) { throw "claude not on PATH after install" }
        }
        if ($DryRun) { Add-Check "INFO" "Claude Code would be installed" "official installer, per-user" }
        elseif ($ok -and (Have "claude")) { Add-Check "PASS" "Claude Code installed" ((& claude --version 2>$null) -join " ") }
        else { Add-Check "FAIL" "Claude Code" "install failed; see https://docs.claude.com/en/docs/claude-code/setup" }
    }
    Add-Next "Sign in to Claude once: open a terminal, run   claude   and follow the browser sign-in with YOUR OWN Claude account (type /login if it asks). A paid Claude plan or API access is required."
}

# ----------------------------------------------------------------------------- 5. venv + packages
Step "Python environment and packages"
$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    Act "$($py.Exe) -m venv $InstallDir\.venv" {
        & $py.Exe -m venv (Join-Path $InstallDir ".venv")
        if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
    } -Critical | Out-Null
} else { Info "Environment already exists." }
$reqs = @("requirements.txt"); if ($gpu) { $reqs += "requirements-gpu.txt" }
$stampFile = Join-Path $InstallDir ".venv\requirements.stamp"
$stamp = ""
foreach ($r in $reqs) { $p = Join-Path $InstallDir $r; if (Test-Path $p) { $stamp += (Get-FileHash $p -Algorithm SHA256).Hash } }
if ((Test-Path $stampFile) -and ((Get-Content $stampFile -Raw).Trim() -eq $stamp)) { Info "Packages already installed and unchanged."; Add-Check "PASS" "Python packages" "unchanged" }
else {
    $ok = Act "pip install $($reqs -join ', ') (first time: roughly 1 GB, a few minutes)" {
        & $venvPy -m pip install --upgrade pip
        foreach ($r in $reqs) {
            & $venvPy -m pip install -r (Join-Path $InstallDir $r)
            if ($LASTEXITCODE -ne 0) { throw "pip install -r $r failed" }
        }
        Set-Content -Path $stampFile -Value $stamp -Encoding ascii
    }
    Add-Check $(if ($DryRun) { "INFO" } elseif ($ok) { "PASS" } else { "FAIL" }) "Python packages" $(if ($DryRun) { "would be installed" } else { "" })
}

# ----------------------------------------------------------------------------- 6. models
$Models = @(
    @{ Name = "kokoro-v1.0.onnx"; Size = 325532387; Sha = "7D5DF8ECF7D4B1878015A32686053FD0EEBE2BC377234608764CC0EF3636A6C5"
       Url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx" },
    @{ Name = "voices-v1.0.bin"; Size = 28214398; Sha = "BCA610B8308E8D99F32E6FE4197E7EC01679264EFED0CAC9140FE9C29F1FBF7D"
       Url = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin" }
)
if ($SkipModels) { Info "Skipping models (-SkipModels)." }
else {
    Step "Voice model (Kokoro, Apache-2.0) from its public release; speech model (Whisper) from Hugging Face"
    $md = Join-Path $InstallDir "models"
    foreach ($m in $Models) {
        $dest = Join-Path $md $m.Name
        if ((Test-Path $dest) -and ((Get-Item $dest).Length -eq $m.Size)) { Info "$($m.Name): already downloaded."; Add-Check "PASS" "Model $($m.Name)" "present"; continue }
        if (-not $DryRun -and -not (Test-Online)) { $script:Offline = $true; Add-Check "WARN" "Model $($m.Name)" "offline; re-run the installer when connected (Windows voice is used meanwhile)"; continue }
        $ok = Act "download $($m.Name) ($([math]::Round($m.Size / 1MB)) MB) from $($m.Url), then verify size and SHA-256" {
            New-Item -ItemType Directory -Force $md | Out-Null
            $part = "$dest.part"
            & curl.exe -L --fail --retry 3 --retry-delay 3 -C - -o $part $m.Url
            if ($LASTEXITCODE -ne 0) { throw "download failed (curl exit $LASTEXITCODE)" }
            if ((Get-Item $part).Length -ne $m.Size) { throw "size mismatch" }
            if ((Get-FileHash $part -Algorithm SHA256).Hash -ne $m.Sha) { Remove-Item $part -Force; throw "SHA-256 mismatch (file corrupted or upstream changed)" }
            Move-Item $part $dest -Force
        }
        Add-Check $(if ($DryRun) { "INFO" } elseif ($ok) { "PASS" } else { "FAIL" }) "Model $($m.Name)" $(if ($DryRun) { "would be downloaded" } else { "" })
    }
    $wm = "small.en"
    $cfgPath = Join-Path $InstallDir "config.yaml"
    if (Test-Path $cfgPath) { $mm = Select-String -Path $cfgPath -Pattern '^whisper_model:\s*"?([\w.-]+)' | Select-Object -First 1; if ($mm) { $wm = $mm.Matches[0].Groups[1].Value } }
    if (Test-Path $venvPy) {
        $ok = Act "pre-download the Whisper speech model '$wm' (about 250 MB) into the Hugging Face cache" {
            & $venvPy -c "from faster_whisper.utils import download_model; print(download_model('$wm'))"
            if ($LASTEXITCODE -ne 0) { throw "could not fetch the Whisper model" }
        }
        Add-Check $(if ($DryRun) { "INFO" } elseif ($ok) { "PASS" } else { "WARN" }) "Speech model $wm" $(if ($DryRun) { "would be downloaded" } else { "AIDA retries on first start" })
    } else { Add-Check "INFO" "Speech model $wm" "downloaded on first start (venv not built in dry run)" }
}

# ----------------------------------------------------------------------------- 7. config + wizard
Step "Your settings"
$cfg = Join-Path $InstallDir "config.yaml"
$wizArgs = @()
if ($Cpu -or -not $gpu) { $wizArgs += "--cpu" }
if (Test-Path $cfg) { Info "config.yaml already exists; leaving your settings alone."; Add-Check "PASS" "config.yaml" "kept" }
else {
    $interactive = (-not $Yes) -and (-not $NoWizard) -and [Environment]::UserInteractive
    if (-not $interactive) { $wizArgs += "--yes" }
    $ok = Act "run setup_wizard.py ($(if ($interactive) { 'asks your name, spelling, voice, microphone' } else { 'defaults, no questions' }))" {
        & $venvPy (Join-Path $InstallDir "setup_wizard.py") @wizArgs
        if ($LASTEXITCODE -ne 0) { throw "wizard failed" }
    }
    Add-Check $(if ($DryRun) { "INFO" } elseif ($ok) { "PASS" } else { "FAIL" }) "config.yaml" $(if ($DryRun) { "would be created from config.example.yaml" } else { "" })
}

# ----------------------------------------------------------------------------- 8. shortcuts
if (-not $NoShortcut) {
    Step "Shortcuts"
    function New-Shortcut($path, $label) {
        $ws = New-Object -ComObject WScript.Shell
        $s = $ws.CreateShortcut($path)
        $s.TargetPath = Join-Path $InstallDir "start.bat"
        $s.WorkingDirectory = $InstallDir
        $s.IconLocation = "$env:SystemRoot\System32\imageres.dll,109"
        $s.Description = $label
        $s.Save()
    }
    $desk = [Environment]::GetFolderPath("Desktop")
    $menu = Join-Path ([Environment]::GetFolderPath("Programs")) "AIDA.lnk"
    Act "create Desktop and Start Menu shortcuts to start.bat" { New-Shortcut (Join-Path $desk "AIDA.lnk") "AIDA voice assistant"; New-Shortcut $menu "AIDA voice assistant" } | Out-Null
    $startup = Join-Path ([Environment]::GetFolderPath("Startup")) "AIDA.lnk"
    if ($StartWithWindows -or ((-not (Test-Path $startup)) -and (Ask-YesNo "Start AIDA automatically when you sign in to Windows?" $false))) {
        Act "create a Startup shortcut ($startup)" { New-Shortcut $startup "AIDA voice assistant" } | Out-Null
    }
    Add-Check "PASS" "Shortcuts" "Desktop + Start Menu"
}

# ----------------------------------------------------------------------------- 9. your own GitHub copy
function Setup-GitHub {
    Step "Your own GitHub copy (optional)"
    if ($NoGitHub) { Info "Skipped (-NoGitHub)."; return }
    if (-not (Test-Path (Join-Path $InstallDir ".git"))) {
        Info "This install is not a git checkout (it was unzipped), so there is nothing to link. Clone the project with git to use this."
        Add-Check "INFO" "GitHub copy" "not a git checkout"; return
    }
    if (-not (Have "git")) { Add-Check "INFO" "GitHub copy" "git not installed; skipped"; return }
    if (-not $DryRun -and -not (Test-Online)) { Add-Check "WARN" "GitHub copy" "offline; re-run the installer when connected"; return }
    Info "Gives you your own private copy of AIDA on GitHub, separate from the original, so you can change it freely."
    if (-not $DryRun -and -not $Yes -and -not (Ask-YesNo "Set up your own GitHub copy now?" $true)) { Add-Check "INFO" "GitHub copy" "skipped by you (re-run any time)"; return }

    if (-not (Have "gh")) {
        $ok = Act "winget install GitHub.cli (may show a Windows permission prompt)" { Winget-Install "GitHub.cli" @() }
        if (-not $DryRun -and (-not $ok -or -not (Have "gh"))) { Add-Check "WARN" "GitHub CLI" "install failed; get it from https://cli.github.com then re-run"; return }
    }
    $authed = $false
    if (Have "gh") { & gh auth status 2>$null | Out-Null; $authed = ($LASTEXITCODE -eq 0) }
    if (-not $authed) {
        if ($DryRun) { Info "[dry-run] would: walk you through 'gh auth login' (browser flow; 3 short steps printed first)"; $user = "<your-github-username>" }
        elseif ($Yes) { Add-Check "INFO" "GitHub sign-in" "needs you; run: gh auth login, then re-run the installer"; return }
        else {
            if (-not (Ask-YesNo "Do you already have a GitHub account?" $true)) {
                Write-Host "    Create a free account at  https://github.com/signup  (verify your email), then come back." -ForegroundColor Yellow
                Start-Process "https://github.com/signup"
                $r = Ask "Press Enter when your account is ready, or type 'skip' to carry on without GitHub" ""
                if ($r -match "^skip") { Add-Check "INFO" "GitHub copy" "skipped (no account yet); re-run the installer later"; return }
            }
            Write-Host ""
            Write-Host "    GitHub sign-in, 3 steps:" -ForegroundColor Cyan
            Write-Host "      1. A one-time code appears below. Copy it (it is also copied for you if you press Enter)."
            Write-Host "      2. Your browser opens github.com/login/device. Paste the code and click Continue."
            Write-Host "      3. Click 'Authorize github'. Come back here."
            & gh auth login --hostname github.com --git-protocol https --web
            & gh auth status 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Add-Check "WARN" "GitHub sign-in" "did not finish; re-run the installer"; return }
        }
        if (-not $DryRun) { & gh auth setup-git 2>$null | Out-Null }
    }
    if ($authed -or -not $DryRun) { $user = ((& gh api user --jq .login 2>$null) -join "").Trim() }
    if (-not $DryRun -and -not $user) { Add-Check "WARN" "GitHub" "could not read your username"; return }
    Add-Check "PASS" "GitHub signed in" $user

    # remotes: upstream = the project you installed from (read only); origin = YOUR copy
    $origin = Git-Out @("remote", "get-url", "origin")
    $upUrl = Git-Out @("remote", "get-url", "upstream")
    if (-not $upUrl -and $origin -and $origin -notmatch "github\.com[/:]$([regex]::Escape($user))/") {
        Act "git remote rename origin upstream   (the project you installed from)" { & git -C $InstallDir remote rename origin upstream 2>$null | Out-Null } | Out-Null
        $upUrl = $origin; $origin = $null
    }
    if (-not $upUrl -and -not $origin -and $Upstream -notmatch '^OWNER/') {
        Act "git remote add upstream https://github.com/$Upstream.git" { & git -C $InstallDir remote add upstream "https://github.com/$Upstream.git" } | Out-Null
        $upUrl = "https://github.com/$Upstream.git"
    }
    Act "make upstream read-only: git remote set-url --push upstream DISABLED" {
        if (Git-Out @("remote", "get-url", "upstream")) { & git -C $InstallDir remote set-url --push upstream DISABLED }
    } | Out-Null
    if ($upUrl) { Add-Check "PASS" "upstream is read-only" "push URL = DISABLED, so you cannot push to the original by accident" }
    else { Add-Check "INFO" "upstream" "none (this copy did not come from a git clone of the original)" }

    if ($origin) { Info "origin already points at $origin; leaving it."; $mine = $true }
    else {
        $name = $RepoName
        if (-not $PSBoundParameters.ContainsKey("RepoName")) { $name = Ask "Name for your copy on GitHub" $RepoName }
        $full = "$user/$name"
        $exists = $false
        if (-not $DryRun) { & gh repo view $full 2>$null | Out-Null; $exists = ($LASTEXITCODE -eq 0) }
        if ($exists) {
            Info "$full already exists; linking it as origin without pushing anything."
            Act "git remote add origin https://github.com/$full.git" { & git -C $InstallDir remote add origin "https://github.com/$full.git" } | Out-Null
            Add-Check "PASS" "Your repo $full" "already existed, linked as origin (not pushed; run 'git push -u origin $Branch' when ready)"
            $mine = $true
        } else {
            $ok = Act "gh repo create $full --$Visibility --source . --remote origin --push   (an independent copy under your account)" {
                Push-Location $InstallDir
                try { & gh repo create $full "--$Visibility" --source . --remote origin --push --description "My AIDA voice assistant" } finally { Pop-Location }
                if ($LASTEXITCODE -ne 0) { throw "gh repo create failed" }
            }
            Add-Check $(if ($DryRun) { "INFO" } elseif ($ok) { "PASS" } else { "WARN" }) "Your repo $full ($Visibility)" $(if ($DryRun) { "would be created and pushed" } else { "created; origin set" })
            $mine = $ok
        }
    }
    # memory sync opt-in
    if ($mine -and -not $DryRun) {
        $priv = ((& gh repo view (Git-Out @("remote", "get-url", "origin")) --json isPrivate --jq .isPrivate 2>$null) -join "").Trim()
        $want = [bool]$SyncMemory
        if (-not $want -and -not $Yes) { $want = Ask-YesNo "Back up what AIDA learns about you (assistant/memory/topics) to your PRIVATE repo? Off by default" $false }
        if ($want -and $priv -ne "true") { Write-Host "    Your repo is not private, so memory sync stays OFF." -ForegroundColor Yellow; $want = $false }
        if ($want) { & $venvPy (Join-Path $InstallDir "setup_wizard.py") --yes --sync on | Out-Null; Add-Check "PASS" "Memory sync" "ON, to origin only" }
        else { Add-Check "INFO" "Memory sync" "off (default). Enable with sync_memory: true in config.yaml" }
    } elseif ($DryRun) { Info "[dry-run] would ask: back up memory to your private repo? (default no; origin only, never upstream)" }
    Add-Next "To get updates from the original later:  git pull upstream $Branch    (your changes go to your repo with plain  git push )"
}
Setup-GitHub

# ----------------------------------------------------------------------------- 10. self-check
Step "Self-check"
if ($DryRun) { Info "[dry-run] would run: $venvPy doctor.py --claude" }
elseif (Test-Path $venvPy) { & $venvPy (Join-Path $InstallDir "doctor.py") $(if (-not $SkipClaude -and (Have "claude")) { "--claude" }) }

# ----------------------------------------------------------------------------- summary
Write-Host ""
Write-Host "================ INSTALL SUMMARY ================" -ForegroundColor Green
foreach ($c in $script:Checks) {
    $col = switch ($c.Status) { "PASS" { "Green" } "WARN" { "Yellow" } "FAIL" { "Red" } default { "Cyan" } }
    Write-Host ("  [{0}] {1}{2}" -f $c.Status, $c.Name, $(if ($c.Detail) { "  -  $($c.Detail)" } else { "" })) -ForegroundColor $col
}
Write-Host ""
Write-Host "WHAT TO DO NEXT" -ForegroundColor Green
$n = 1
foreach ($x in $script:Next) { Write-Host "  $n. $x"; $n++ }
Write-Host "  $n. Start AIDA with the AIDA shortcut on your Desktop (or run start.bat). Press the backtick key ( `` ) to talk, press it again to send."
if ($script:Offline) { Write-Host "  (You were offline for part of this. Re-run the installer when connected; it only does what is missing.)" }
if ($DryRun) { Write-Host "`nThat was a dry run: nothing was changed." -ForegroundColor Yellow }
$failed = @($script:Checks | Where-Object { $_.Status -eq "FAIL" }).Count
if ($failed -gt 0) { Write-Host "`n$failed item(s) failed; scroll up, fix, and run the installer again." -ForegroundColor Red }
