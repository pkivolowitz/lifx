# GlowUp installer — Windows.
#
# Self-contained standalone installer (no server flavor on Windows).
# Creates a per-user venv at %USERPROFILE%\.glowup\venv, drops a launcher
# shim at %USERPROFILE%\bin\glowup.cmd, optionally appends that directory
# to the user's PATH via the registry (with permission, idempotent), and
# seeds %USERPROFILE%\.glowup\{devices.json,groups.json,README.md}.
#
# No Administrator rights, no Windows service, no files outside the user
# profile.  Re-running this script is the upgrade path.
#
# Usage:
#   .\install.ps1                 # interactive, prompts for PATH edit
#   .\install.ps1 -NoPrompt       # skip prompts, use defaults
#
# If you see "running scripts is disabled on this system" the first time,
# bypass for this run:
#   powershell -ExecutionPolicy Bypass -File install.ps1

[CmdletBinding()]
param(
    [switch]$NoPrompt
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

$PythonMinMajor = 3
$PythonMinMinor = 11

$GlowupHome   = Join-Path $env:USERPROFILE '.glowup'
$GlowupVenv   = Join-Path $GlowupHome 'venv'
$GlowupShimDir = Join-Path $env:USERPROFILE 'bin'
$GlowupShim    = Join-Path $GlowupShimDir 'glowup.cmd'
$GlowupReadme  = Join-Path $GlowupHome 'README.md'
$GlowupDevices = Join-Path $GlowupHome 'devices.json'
$GlowupGroups  = Join-Path $GlowupHome 'groups.json'

$CloneDir   = $PSScriptRoot
$EntryPoint = Join-Path $CloneDir 'glowup.py'
$Requirements = Join-Path $CloneDir 'requirements.txt'

# ---------------------------------------------------------------------------
# Logging helpers — mirror install.py shape so the install experience feels
# the same across platforms.
# ---------------------------------------------------------------------------

function Write-Step([string]$Msg) {
    Write-Host ""
    Write-Host "==> $Msg" -ForegroundColor Cyan
}

function Write-Info([string]$Msg) {
    Write-Host "  $Msg"
}

function Write-Ok([string]$Msg) {
    Write-Host "  $([char]0x2713) $Msg" -ForegroundColor Green
}

function Write-Warn([string]$Msg) {
    Write-Warning "  $Msg"
}

function Write-Fail([string]$Msg, [int]$ExitCode = 1) {
    Write-Host ""
    Write-Host "  $([char]0x2717) $Msg" -ForegroundColor Red
    Write-Host ""
    exit $ExitCode
}

# ---------------------------------------------------------------------------
# Python discovery — prefer the `py` launcher (ships with the official
# python.org installer) which can pick a specific version with `-3.13`.
# Fall back to bare python.exe / python3.exe on PATH.
# ---------------------------------------------------------------------------

function Find-Python {
    # 1) py launcher with explicit versions, newest-first.
    $pyLauncher = Get-Command -Name 'py' -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        foreach ($ver in @('-3.13', '-3.12', '-3.11')) {
            try {
                $check = & py $ver -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $check) {
                    $parts = $check.Trim().Split('.')
                    if ([int]$parts[0] -eq $PythonMinMajor -and [int]$parts[1] -ge $PythonMinMinor) {
                        return @($pyLauncher.Source, $ver)
                    }
                }
            } catch { continue }
        }
    }

    # 2) Bare python.exe / python3.exe on PATH.
    foreach ($cand in @('python.exe', 'python3.exe', 'python')) {
        $found = Get-Command -Name $cand -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        try {
            $check = & $found.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $check) {
                $parts = $check.Trim().Split('.')
                if ([int]$parts[0] -eq $PythonMinMajor -and [int]$parts[1] -ge $PythonMinMinor) {
                    return @($found.Source, $null)
                }
            }
        } catch { continue }
    }

    return $null
}

function Assert-PythonAvailable {
    $found = Find-Python
    if (-not $found) {
        Write-Fail @"
GlowUp requires Python $PythonMinMajor.$PythonMinMinor or newer.

Download the official installer from https://www.python.org/downloads/
(or use winget: ``winget install Python.Python.3.13``), then re-run this
script.  Make sure 'Add python.exe to PATH' is checked during install.
"@
    }
    return $found  # @($exe, $launcherFlag-or-null)
}

# ---------------------------------------------------------------------------
# Venv management — per-user at $GlowupVenv.  Idempotent: if a venv exists
# with a matching Python version, reuse it (pip install --upgrade); otherwise
# rename it with a timestamp suffix and rebuild.
# ---------------------------------------------------------------------------

function Test-VenvPythonMatches([string]$VenvPath) {
    $venvPy = Join-Path $VenvPath 'Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $venvPy)) { return $false }
    try {
        $check = & $venvPy -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($LASTEXITCODE -ne 0) { return $false }
        # Compare against running Python (the one we're driving the install with).
        $expected = & $PythonExe @PythonArgs -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        return ($check.Trim() -eq $expected.Trim())
    } catch {
        return $false
    }
}

function Get-Timestamp {
    return Get-Date -Format 'yyyyMMdd-HHmmss'
}

function Initialize-Venv {
    if (-not (Test-Path -LiteralPath $GlowupHome)) {
        New-Item -ItemType Directory -Path $GlowupHome | Out-Null
    }

    if (Test-Path -LiteralPath $GlowupVenv) {
        if (Test-VenvPythonMatches $GlowupVenv) {
            Write-Ok "venv at $GlowupVenv matches Python $PythonMinMajor.$PythonMinMinor+; will pip install -U"
            return
        }
        $backup = "$GlowupVenv.bak.$(Get-Timestamp)"
        Write-Warn "existing venv at $GlowupVenv uses a different Python; renaming to $backup and rebuilding"
        Move-Item -LiteralPath $GlowupVenv -Destination $backup
    }

    Write-Step "creating venv at $GlowupVenv"
    & $PythonExe @PythonArgs -m venv $GlowupVenv
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "python -m venv failed (exit $LASTEXITCODE)" 20
    }
    Write-Ok "venv created"
}

function Install-Requirements {
    if (-not (Test-Path -LiteralPath $Requirements)) {
        Write-Fail "requirements.txt not found at $Requirements" 21
    }
    $pip = Join-Path $GlowupVenv 'Scripts\pip.exe'
    Write-Step "installing requirements from $(Split-Path -Leaf $Requirements)"
    & $pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) { Write-Fail "pip self-upgrade failed" 22 }
    & $pip install --upgrade -r $Requirements
    if ($LASTEXITCODE -ne 0) { Write-Fail "pip install failed" 23 }
    Write-Ok "requirements installed"
}

# ---------------------------------------------------------------------------
# Shim — %USERPROFILE%\bin\glowup.cmd invokes the venv's python on the
# clone's glowup.py.  Captures the clone path at install time; re-running
# install.ps1 re-renders the shim with the current clone location.
# ---------------------------------------------------------------------------

function Write-Shim {
    if (-not (Test-Path -LiteralPath $EntryPoint)) {
        Write-Fail "glowup.py entry point not found at $EntryPoint" 24
    }
    if (-not (Test-Path -LiteralPath $GlowupShimDir)) {
        New-Item -ItemType Directory -Path $GlowupShimDir | Out-Null
    }
    $venvPy = Join-Path $GlowupVenv 'Scripts\python.exe'
    # CMD batch script: %* forwards all arguments verbatim (including quoted ones).
    $body = @"
@echo off
REM GlowUp launcher — auto-generated by install.ps1.
REM Re-run install.ps1 to regenerate after moving the clone.
"$venvPy" "$EntryPoint" %*
"@
    Set-Content -LiteralPath $GlowupShim -Value $body -Encoding ASCII
    Write-Ok "wrote launcher $GlowupShim"
}

# ---------------------------------------------------------------------------
# User PATH edit via registry — permission-gated, idempotent.
# Uses [Environment]::SetEnvironmentVariable with the User scope, which
# writes to HKCU\Environment (no Administrator needed).  Idempotent because
# we check for the shim dir on the existing User PATH before appending.
# ---------------------------------------------------------------------------

function Get-UserPath {
    return [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Set-UserPath([string]$NewValue) {
    [Environment]::SetEnvironmentVariable('Path', $NewValue, 'User')
}

function Update-UserPath {
    $current = Get-UserPath
    if (-not $current) { $current = '' }

    # Normalize for idempotent comparison: split on ';', resolve each entry,
    # check whether $GlowupShimDir is already present in any form.
    $resolvedTarget = (Resolve-Path -LiteralPath $GlowupShimDir -ErrorAction SilentlyContinue).Path
    if (-not $resolvedTarget) { $resolvedTarget = $GlowupShimDir }

    $entries = $current -split ';' | Where-Object { $_ -ne '' }
    foreach ($e in $entries) {
        $resolved = (Resolve-Path -LiteralPath $e -ErrorAction SilentlyContinue).Path
        if (-not $resolved) { $resolved = $e }
        if ($resolved -ieq $resolvedTarget) {
            Write-Ok "$GlowupShimDir already on User PATH; no edit needed"
            return
        }
    }

    if (-not $NoPrompt) {
        Write-Info "GlowUp wants to add $GlowupShimDir to your User PATH (HKCU\Environment)."
        Write-Info "This is per-user (no Administrator required) and persists across sessions."
        $ans = Read-Host "Permit this edit? [Y/n]"
        if ($ans -and ($ans.Trim().ToLower() -in @('n', 'no'))) {
            Write-Info "Skipping PATH edit.  Run this manually to finish setup:"
            Write-Info "  [Environment]::SetEnvironmentVariable('Path', `"$GlowupShimDir;`" + [Environment]::GetEnvironmentVariable('Path','User'), 'User')"
            return
        }
    }

    # Backup the old value so the user can manually restore if anything goes wrong.
    $backupPath = Join-Path $GlowupHome "user_path.bak.$(Get-Timestamp).txt"
    Set-Content -LiteralPath $backupPath -Value $current -Encoding UTF8
    Write-Ok "backed up existing User PATH to $backupPath"

    $newValue = if ($current.TrimEnd(';') -eq '') {
        $GlowupShimDir
    } else {
        $current.TrimEnd(';') + ';' + $GlowupShimDir
    }
    Set-UserPath $newValue
    Write-Ok "added $GlowupShimDir to User PATH (open a new terminal for it to take effect)"
}

# ---------------------------------------------------------------------------
# Seed files — devices.json, groups.json (empty {}, only if missing) and
# README.md (overwritten on every install so doc updates land).
# ---------------------------------------------------------------------------

$StandaloneReadme = @"
# GlowUp standalone — files in this directory

This directory holds your GlowUp standalone state.  Two JSON files plus
the venv directory are all that lives here.

## ``devices.json`` — bulb registry

Created and updated by ``glowup name`` and ``glowup discover``.  Maps each
bulb's MAC address to its label, IP, and product info.

Schema:

``````json
{
  "<MAC>": {
    "label": "<human name>",
    "ip": "<IPv4 address>",
    "product": "<LIFX product name>"
  }
}
``````

Example (do **not** copy verbatim — your bulbs have different MACs and IPs):

``````json
{
  "d0:73:d5:01:23:ab": {
    "label": "Kitchen Bulb",
    "ip": "192.168.1.41",
    "product": "A19"
  }
}
``````

## ``groups.json`` — group definitions

Created and updated by ``glowup group add`` / ``glowup group rm``.  Maps each
group name to an ordered list of bulb references (label, MAC, or IP).

Schema:

``````json
{
  "<group_name>": ["<bulb ref>", "<bulb ref>", ...]
}
``````

Order matters — the first bulb is the leftmost zone of the virtual strip.

## Editing by hand

Both files are plain JSON.  You can open them in any editor, but the
runtime preserves keys starting with ``_`` on read and never writes new
ones.  That gives you a stable place for your own notes:

``````json
{
  "_note": "PORCH STRING is the long one over the bench",
  "porch": ["String 36 Porch", "Bulb Patio"]
}
``````

## venv

``venv\`` is GlowUp's Python virtual environment.  The launcher at
``%USERPROFILE%\bin\glowup.cmd`` invokes it directly.  Don't activate it
manually unless you're debugging — the launcher does the right thing.
"@

function Initialize-StandaloneFiles {
    foreach ($pair in @(@($GlowupDevices, '{}'), @($GlowupGroups, '{}'))) {
        $path = $pair[0]
        $body = $pair[1]
        if (Test-Path -LiteralPath $path) {
            Write-Ok "$path exists; leaving alone"
            continue
        }
        Set-Content -LiteralPath $path -Value $body -Encoding UTF8
        Write-Ok "seeded empty $path"
    }
    Set-Content -LiteralPath $GlowupReadme -Value $StandaloneReadme -Encoding UTF8
    Write-Ok "wrote $GlowupReadme"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

Write-Step "GlowUp standalone installer (Windows)"

$pyTuple = Assert-PythonAvailable
$script:PythonExe  = $pyTuple[0]
$script:PythonArgs = if ($pyTuple[1]) { @($pyTuple[1]) } else { @() }

# Show what we picked so the user has a record.
$pyVersion = & $PythonExe @PythonArgs -c "import sys; print(sys.version)" 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Ok "using Python: $($pyVersion.Split([Environment]::NewLine)[0].Trim())"
}

Initialize-Venv
Install-Requirements
Write-Shim
Update-UserPath
Initialize-StandaloneFiles

Write-Host ""
Write-Step "standalone install complete"
Write-Info "Run ``glowup discover`` to find your bulbs."
Write-Info "If ``glowup`` is not yet on PATH, open a new PowerShell or cmd window."
Write-Info "Files: $GlowupHome"
