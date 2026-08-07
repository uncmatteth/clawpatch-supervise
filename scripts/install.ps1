[CmdletBinding()]
param(
    [string]$Version = "0.1.22",
    [string]$Source = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise"),
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise\bin"),
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
$ClawPatchVersion = "0.7.2"
$ClawHubVersion = "0.19.1"
function Find-PathApplication {
    param([string[]]$Names)
    foreach ($directory in ($env:Path -split ";")) {
        $directory = $directory.Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($directory)) { continue }
        foreach ($name in $Names) {
            $candidate = Join-Path $directory $name
            if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        }
    }
    return $null
}
function Assert-CommandVersion {
    param(
        [string]$Name,
        [string]$ExpectedVersion,
        [string]$CommandPath,
        [string[]]$Arguments
    )
    $versionOutput = & $CommandPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The $Name command failed its version check."
    }
    $actualVersion = [string]($versionOutput | Select-Object -Last 1)
    $actualVersion = $actualVersion.Trim()
    if ([string]::IsNullOrWhiteSpace($actualVersion)) {
        $actualVersion = "unknown"
    }
    if ($actualVersion -ne $ExpectedVersion) {
        throw "$Name $ExpectedVersion is required; found $actualVersion."
    }
    return $actualVersion
}
if ([string]::IsNullOrWhiteSpace($Source)) {
    $Source = "https://github.com/uncmatteth/clawpatch-supervise/releases/download/v$Version/clawpatch_supervise-$Version-py3-none-any.whl"
}

$pyLauncher = Get-Command py -ErrorAction SilentlyContinue
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $pyLauncher) {
    $pythonVersionOutput = & $pyLauncher.Source -3 --version 2>&1
    $pythonVersionExitCode = $LASTEXITCODE
} elseif ($null -ne $python) {
    $pythonVersionOutput = & $python.Source --version 2>&1
    $pythonVersionExitCode = $LASTEXITCODE
} else {
    throw "Python 3.11 or newer is required."
}
if ($pythonVersionExitCode -ne 0) {
    throw "Python 3.11 or newer is required."
}
$pythonVersionText = [string]($pythonVersionOutput | Select-Object -Last 1)
if ($pythonVersionText -notmatch '^Python\s+(\d+)\.(\d+)(?:\.|$)') {
    throw "Python 3.11 or newer is required."
}
$pythonMajor = [int]$Matches[1]
$pythonMinor = [int]$Matches[2]
if ($pythonMajor -lt 3 -or ($pythonMajor -eq 3 -and $pythonMinor -lt 11)) {
    throw "Python 3.11 or newer is required."
}

$clawpatch = Get-Command clawpatch -ErrorAction SilentlyContinue
$npmPath = $null
if ($null -ne $clawpatch) {
    $clawpatchInstalledVersion = Assert-CommandVersion "ClawPatch" $ClawPatchVersion $clawpatch.Source @("--version")
} else {
    $npmPath = Find-PathApplication @("npm.cmd", "npm.exe")
    if ($null -eq $npmPath) {
        throw "npm is required to install ClawPatch."
    }
}

$clawHubPath = Find-PathApplication @("clawhub.cmd", "clawhub.exe")
if ($null -ne $clawHubPath) {
    $clawHubInstalledVersion = Assert-CommandVersion "ClawHub" $ClawHubVersion $clawHubPath @("--cli-version")
} else {
    if ($null -eq $npmPath) {
        $npmPath = Find-PathApplication @("npm.cmd", "npm.exe")
    }
    if ($null -eq $npmPath) {
        throw "ClawHub is missing and npm is unavailable. Install Node.js 22 or newer, then rerun this installer."
    }
}

if ($null -eq $clawpatch) {
    & $npmPath install --global "clawpatch@$ClawPatchVersion"
    if ($LASTEXITCODE -ne 0) {
        throw "npm could not install ClawPatch."
    }

    $npmPrefixOutput = & $npmPath prefix --global
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
    $clawpatchInstalledVersion = Assert-CommandVersion "ClawPatch" $ClawPatchVersion $clawpatch.Source @("--version")
}

if ($null -ne $pyLauncher) {
    & $pyLauncher.Source -3 -m venv (Join-Path $InstallRoot "venv")
} else {
    & $python.Source -m venv (Join-Path $InstallRoot "venv")
}

$venvPython = Join-Path $InstallRoot "venv\Scripts\python.exe"
$supervisor = Join-Path $InstallRoot "venv\Scripts\clawpatch-supervise.exe"
& $venvPython -m pip install --disable-pip-version-check --upgrade $Source

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$wrapper = Join-Path $BinDir "clawpatch-supervise.cmd"
$wrapperText = "@echo off`r`n`"$supervisor`" %*`r`n"
Set-Content -Path $wrapper -Value $wrapperText -Encoding Ascii -NoNewline
$env:Path = "$BinDir;$env:Path"

if ($null -eq $clawHubPath) {
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
    $clawHubInstalledVersion = Assert-CommandVersion "ClawHub" $ClawHubVersion $clawHubPath @("--cli-version")
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
Write-Output $clawHubInstalledVersion
Write-Output $clawpatchInstalledVersion
Write-Host "Installed command: $wrapper"
Write-Host "ClawHub command: $clawHubPath"
