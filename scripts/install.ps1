[CmdletBinding()]
param(
    [string]$Version = "0.1.26",
    [string]$Source = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise"),
    [string]$BinDir = (Join-Path $env:LOCALAPPDATA "ClawPatchSupervise\bin"),
    [string]$VerifyRepo = "",
    [string]$Sha256 = "",
    [switch]$AddToPath
)

$ErrorActionPreference = "Stop"
$MinimumClawPatchVersion = [version]"0.7.2"
$ReleaseSha256_0_1_26 = "07169d18a3391dfaf186372d4594bc6391c5e8bffc0a9d08f3f7acf688769783"
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
function Get-CompatibleClawPatchVersion {
    param(
        [string]$CommandPath,
        [string[]]$Arguments
    )
    $versionOutput = & $CommandPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "The ClawPatch command failed its version check."
    }
    $actualVersion = [string]($versionOutput | Select-Object -Last 1)
    $actualVersion = $actualVersion.Trim()
    if ([string]::IsNullOrWhiteSpace($actualVersion)) {
        $actualVersion = "unknown"
    }
    if ($actualVersion -notmatch '(\d+)\.(\d+)\.(\d+)') {
        throw "ClawPatch returned an unreadable version: $actualVersion."
    }
    $parsedVersion = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
    if ($parsedVersion -lt $MinimumClawPatchVersion) {
        throw "ClawPatch $MinimumClawPatchVersion or newer is required; found $actualVersion."
    }
    return $actualVersion
}
$usingDefaultSource = [string]::IsNullOrWhiteSpace($Source)
if ($usingDefaultSource) {
    $Source = "https://github.com/uncmatteth/clawpatch-supervise/releases/download/v$Version/clawpatch_supervise-$Version-py3-none-any.whl"
    if ($Version -ne "0.1.26") {
        throw "No trusted SHA-256 is available for clawpatch-supervise $Version."
    }
    $Sha256 = $ReleaseSha256_0_1_26
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
function Test-CompatibleClawPatch {
    param(
        [string]$CommandPath,
        [string[]]$Arguments
    )
    $versionOutput = & $CommandPath @Arguments
    if ($LASTEXITCODE -ne 0) { return $false }
    $actualVersion = [string]($versionOutput | Select-Object -Last 1)
    if ($actualVersion.Trim() -notmatch '(\d+)\.(\d+)\.(\d+)') { return $false }
    $parsedVersion = [version]("{0}.{1}.{2}" -f $Matches[1], $Matches[2], $Matches[3])
    return $parsedVersion -ge $MinimumClawPatchVersion
}

$nodePath = Find-PathApplication @("node.exe", "node.cmd")
if ($null -eq $nodePath) {
    throw "Node.js 22 or newer is required."
}
$nodeVersionOutput = & $nodePath --version
if ($LASTEXITCODE -ne 0 -or [string]($nodeVersionOutput | Select-Object -Last 1) -notmatch '^v(\d+)(?:\.|$)') {
    throw "Node.js 22 or newer is required."
}
if ([int]$Matches[1] -lt 22) {
    throw "Node.js 22 or newer is required."
}

$clawpatch = Find-PathApplication @("clawpatch.cmd", "clawpatch.exe")
$npmPath = $null
if ($null -ne $clawpatch -and (Test-CompatibleClawPatch $clawpatch @("--version"))) {
    $clawpatchInstalledVersion = Get-CompatibleClawPatchVersion $clawpatch @("--version")
} else {
    $clawpatch = $null
    $npmPath = Find-PathApplication @("npm.cmd", "npm.exe")
    if ($null -eq $npmPath) {
        throw "npm is required to install ClawPatch."
    }
}

if ($null -eq $clawpatch) {
    $clawpatchRoot = Join-Path $InstallRoot "clawpatch"
    & $npmPath install --prefix $clawpatchRoot --no-fund --no-audit "clawpatch@latest"
    if ($LASTEXITCODE -ne 0) {
        throw "npm could not install ClawPatch."
    }
    $clawpatch = Join-Path $clawpatchRoot "node_modules\.bin\clawpatch.cmd"
    if (-not (Test-Path -LiteralPath $clawpatch -PathType Leaf)) {
        throw "ClawPatch installation did not create clawpatch.cmd."
    }
    $clawpatchInstalledVersion = Get-CompatibleClawPatchVersion $clawpatch @("--version")
}

$downloadRoot = $null
$packageToInstall = $Source
if (Test-Path -LiteralPath $Source -PathType Container) {
    $packageToInstall = (Resolve-Path -LiteralPath $Source).Path
} else {
    if ([string]::IsNullOrWhiteSpace($Sha256)) {
        throw "-Sha256 is required for a wheel URL or file."
    }
    if ($Sha256 -notmatch '^[0-9a-fA-F]{64}$') {
        throw "-Sha256 must be a 64-character hexadecimal digest."
    }
    if (Test-Path -LiteralPath $Source -PathType Leaf) {
        $packageToInstall = (Resolve-Path -LiteralPath $Source).Path
    } else {
        [Uri]$sourceUri = $null
        if (-not [Uri]::TryCreate($Source, [UriKind]::Absolute, [ref]$sourceUri) -or
            $sourceUri.Scheme -notin @("https", "http")) {
            throw "The supervisor source must be a local directory, local wheel, or HTTP(S) URL."
        }
        $downloadRoot = Join-Path ([IO.Path]::GetTempPath()) ("clawpatch-supervise-install-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Force -Path $downloadRoot | Out-Null
        $packageToInstall = Join-Path $downloadRoot ("clawpatch_supervise-$Version-py3-none-any.whl")
        Invoke-WebRequest -Uri $sourceUri -OutFile $packageToInstall
    }
    $actualSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $packageToInstall).Hash
    if ($actualSha256 -ine $Sha256) {
        throw "Artifact SHA-256 mismatch: expected $($Sha256.ToLowerInvariant()), found $($actualSha256.ToLowerInvariant())."
    }
}

try {
    if ($null -ne $pyLauncher) {
        & $pyLauncher.Source -3 -m venv (Join-Path $InstallRoot "venv")
    } else {
        & $python.Source -m venv (Join-Path $InstallRoot "venv")
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Python could not create the supervisor virtual environment."
    }

    $venvPython = Join-Path $InstallRoot "venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        $venvPython = Join-Path $InstallRoot "venv\Scripts\python.cmd"
    }
    & $venvPython -m pip install --disable-pip-version-check --upgrade $packageToInstall
    if ($LASTEXITCODE -ne 0) {
        throw "pip could not install ClawPatch Supervise."
    }
    $supervisor = Join-Path $InstallRoot "venv\Scripts\clawpatch-supervise.exe"
    if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
        $supervisor = Join-Path $InstallRoot "venv\Scripts\clawpatch-supervise.cmd"
    }
    if (-not (Test-Path -LiteralPath $supervisor -PathType Leaf)) {
        throw "pip did not create the clawpatch-supervise command."
    }
} finally {
    if ($null -ne $downloadRoot) {
        Remove-Item -LiteralPath $downloadRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

$wrapper = Join-Path $BinDir "clawpatch-supervise.cmd"
$toolDirectories = @((Split-Path -Parent $clawpatch)) | Select-Object -Unique
$toolPath = $toolDirectories -join ";"
$wrapperText = (
    "@echo off`r`n" +
    "set `"PYTHONUTF8=1`"`r`n" +
    "set `"PYTHONIOENCODING=utf-8`"`r`n" +
    "set `"NODE_DISABLE_COMPILE_CACHE=1`"`r`n" +
    "set `"PATH=$toolPath;%PATH%`"`r`n" +
    "`"$supervisor`" %*`r`n"
)
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
if (-not [string]::IsNullOrWhiteSpace($VerifyRepo)) {
    & $wrapper doctor --repo (Resolve-Path -LiteralPath $VerifyRepo).Path
    if ($LASTEXITCODE -ne 0) {
        throw "The installed supervisor did not pass its portable runtime doctor."
    }
}
Write-Output $clawpatchInstalledVersion
Write-Host "Installed command: $wrapper"
