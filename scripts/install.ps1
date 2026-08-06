[CmdletBinding()]
param(
    [string]$Version = "0.1.4",
    [string]$Source = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise"),
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise\bin"),
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
if ([string]::IsNullOrWhiteSpace($Source)) {
    $Source = "https://github.com/uncmatteth/clawpatch-supervise/releases/download/v$Version/clawpatch_supervise-$Version-py3-none-any.whl"
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

if ($AddToPath) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($userPath -split ";" | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    if ($parts -notcontains $BinDir) {
        $newPath = (@($parts) + $BinDir) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    }
}

& $supervisor --version
Write-Host "Installed command: $wrapper"
if ($null -eq (Get-Command clawpatch -ErrorAction SilentlyContinue)) {
    Write-Warning "ClawPatch is not on PATH yet. Install ClawPatch before running a queue."
}
