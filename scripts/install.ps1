[CmdletBinding()]
param(
    [string]$Version = "0.1.15",
    [string]$Source = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise"),
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise\bin"),
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
$ClawPatchVersion = "0.7.2"
$ClawHubVersion = "0.19.1"
if ([string]::IsNullOrWhiteSpace($Source)) {
    $Source = "https://github.com/uncmatteth/clawpatch-supervise/releases/download/v$Version/clawpatch_supervise-$Version-py3-none-any.whl"
}

$clawpatch = Get-Command clawpatch -ErrorAction SilentlyContinue
if ($null -eq $clawpatch) {
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if ($null -eq $npm) {
        throw "npm is required to install ClawPatch."
    }

    & $npm.Source install --global "clawpatch@$ClawPatchVersion"
    if ($LASTEXITCODE -ne 0) {
        throw "npm could not install ClawPatch."
    }

    $npmPrefixOutput = & $npm.Source prefix --global
    if ($LASTEXITCODE -ne 0) {
        throw "npm could not report its global installation directory."
    }
    $npmPrefix = ($npmPrefixOutput | Select-Object -Last 1).Trim()
    if (-not [string]::IsNullOrWhiteSpace($npmPrefix)) {
        $env:Path = "$npmPrefix;$env:Path"
    }

    $clawpatch = Get-Command clawpatch -ErrorAction SilentlyContinue
    if ($null -eq $clawpatch) {
        throw "ClawPatch was installed but its command is not on PATH."
    }
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    & $pyLauncher.Source -3 -m venv (Join-Path $InstallRoot "venv")
} elseif ($null -ne $python) {
    & $python.Source -m venv (Join-Path $InstallRoot "venv")
} else {
    throw "Python 3.11 or newer is required."
}

$venvPython = Join-Path $InstallRoot "venv\Scripts\python.exe"
$supervisor = Join-Path $InstallRoot "venv\Scripts\clawpatch-supervise.exe"
& $venvPython -m pip install --disable-pip-version-check --upgrade $Source

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$wrapper = Join-Path $BinDir "clawpatch-supervise.cmd"
$wrapperText = "@echo off`r`n`"$supervisor`" %*`r`n"
Set-Content -Path $wrapper -Value $wrapperText -Encoding Ascii -NoNewline
$env:Path = "$BinDir;$env:Path"

$clawHubCommand = Get-Command clawhub.cmd, clawhub.exe, clawhub -CommandType Application -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($null -eq $clawHubCommand) {
    $npmCommand = Get-Command npm.cmd, npm.exe, npm -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $npmCommand) {
        $knownNpm = Join-Path $env:ProgramFiles "nodejs\npm.cmd"
        if (Test-Path -LiteralPath $knownNpm -PathType Leaf) {
            $npmPath = $knownNpm
        } else {
            throw "ClawHub is missing and npm is unavailable. Install Node.js 22 or newer, then rerun this installer."
        }
    } else {
        $npmPath = $npmCommand.Source
    }
    $clawHubRoot = Join-Path $InstallRoot "clawhub"
    Write-Host "ClawHub is missing; installing clawhub@$ClawHubVersion into $clawHubRoot"
    & $npmPath install --prefix $clawHubRoot --no-fund --no-audit "clawhub@$ClawHubVersion"
    if ($LASTEXITCODE -ne 0) {
        throw "npm could not install clawhub@$ClawHubVersion (exit $LASTEXITCODE)."
    }
    $installedClawHub = Join-Path $clawHubRoot "node_modules\.bin\clawhub.cmd"
    if (-not (Test-Path -LiteralPath $installedClawHub -PathType Leaf)) {
        throw "ClawHub installation did not create clawhub.cmd."
    }
    $clawHubWrapper = Join-Path $BinDir "clawhub.cmd"
    $clawHubWrapperText = "@echo off`r`ncall `"$installedClawHub`" %*`r`nexit /b %ERRORLEVEL%`r`n"
    Set-Content -Path $clawHubWrapper -Value $clawHubWrapperText -Encoding Ascii -NoNewline
    $clawHubPath = $clawHubWrapper
} else {
    $clawHubPath = $clawHubCommand.Source
}

if ($AddToPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($parts -notcontains $BinDir) {
        $newPath = (@($parts) + $BinDir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    }
}

& $supervisor --version
& $clawHubPath --cli-version
if ($LASTEXITCODE -ne 0) { throw "The ClawHub command failed its startup check." }
& $clawpatch.Source --version
if ($LASTEXITCODE -ne 0) { throw "The ClawPatch command failed its startup check." }
Write-Host "Installed command: $wrapper"
Write-Host "ClawHub command: $clawHubPath"
