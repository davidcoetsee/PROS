# SPDX-License-Identifier: MPL-2.0
[CmdletBinding()]
param(
    [switch]$UpdateAuditManifests
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$VenvRoot = Join-Path $ProjectRoot ".release-venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$DistPath = Join-Path $ProjectRoot "dist"
$PyInstallerWorkPath = Join-Path $ProjectRoot "build\pyinstaller"
$BuildInfoDirectory = Join-Path $ProjectRoot "build\generated"
$BuildInfoPath = Join-Path $BuildInfoDirectory "BUILD_INFO.json"
$SpecPath = Join-Path $ProjectRoot "PROS.spec"
$LockPath = Join-Path $ProjectRoot "requirements-windows-x64.lock"
$EnvironmentVerifier = Join-Path $ProjectRoot "tools\verify_release_environment.py"
$WarningVerifier = Join-Path $ProjectRoot "tools\verify_pyinstaller_warnings.py"
$ArchiveVerifier = Join-Path $ProjectRoot "tools\verify_frozen_archive.py"
$ArtifactVerifier = Join-Path $ProjectRoot "verify_release_artifact.ps1"

function Assert-ProjectChildPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = [System.IO.Path]::GetFullPath($LiteralPath)
    $prefix = $ProjectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $resolved"
    }
    return $resolved
}

function Remove-ProjectBuildDirectory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $safePath = Assert-ProjectChildPath -LiteralPath $LiteralPath
    if (Test-Path -LiteralPath $safePath) {
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
}

$GitStatus = @(& git -C $ProjectRoot status --porcelain --untracked-files=all)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to inspect the PROS Git working tree."
}
if ($GitStatus.Count -ne 0) {
    throw "Commit all source changes before building a provenance-bound PROS.exe."
}
$BuildCommit = (& git -C $ProjectRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $BuildCommit -notmatch '^[0-9a-f]{40}$') {
    throw "Unable to determine the clean build commit."
}

$BasePython = $null
$BasePythonArguments = @()
$PythonCommand = Get-Command "python.exe" -ErrorAction SilentlyContinue
if ($null -ne $PythonCommand) {
    $CandidateVersion = (& $PythonCommand.Source -c "import platform; print(platform.python_version())").Trim()
    if ($LASTEXITCODE -eq 0 -and $CandidateVersion -eq "3.13.15") {
        $BasePython = $PythonCommand.Source
    }
}
if ($null -eq $BasePython) {
    $Launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -ne $Launcher) {
        $CandidateVersion = (& $Launcher.Source -3.13 -c "import platform; print(platform.python_version())").Trim()
        if ($LASTEXITCODE -eq 0 -and $CandidateVersion -eq "3.13.15") {
            $BasePython = $Launcher.Source
            $BasePythonArguments = @("-3.13")
        }
    }
}
if ($null -eq $BasePython) {
    throw "Python not found. Install 64-bit CPython 3.13.15, then rerun build.ps1."
}

Remove-ProjectBuildDirectory -LiteralPath $VenvRoot
Write-Host "Creating a fresh audited CPython 3.13 release environment"
& $BasePython @BasePythonArguments -m venv $VenvRoot
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Unable to create the isolated release environment."
}

$BootstrapVersion = (& $Python -c "import platform; print(platform.python_version())").Trim()
if ($LASTEXITCODE -ne 0 -or $BootstrapVersion -ne "3.13.15") {
    throw "The release build requires CPython 3.13.15; found $BootstrapVersion."
}

$InstallArguments = @(
    "-m", "pip", "install",
    "--disable-pip-version-check",
    "--require-hashes",
    "--only-binary=:all:"
)
$Wheelhouse = [Environment]::GetEnvironmentVariable("PROS_WHEELHOUSE")
if ($Wheelhouse) {
    $WheelhousePath = [System.IO.Path]::GetFullPath($Wheelhouse)
    if (-not (Test-Path -LiteralPath $WheelhousePath -PathType Container)) {
        throw "PROS_WHEELHOUSE is not a directory: $WheelhousePath"
    }
    $InstallArguments += @("--no-index", "--find-links", $WheelhousePath)
}
$InstallArguments += @("--requirement", $LockPath)

Write-Host "Installing only SHA-256-approved Windows x64 wheels"
& $Python @InstallArguments
if ($LASTEXITCODE -ne 0) {
    throw "Hash-locked release dependency installation failed."
}

Write-Host "Verifying the closed release environment and native inventory"
& $Python $EnvironmentVerifier
if ($LASTEXITCODE -ne 0) {
    throw "The release environment does not match the audited inventory."
}

$Version = (& $Python -c "from pros import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or $Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unable to determine a valid PROS version."
}

Remove-ProjectBuildDirectory -LiteralPath $BuildInfoDirectory
New-Item -ItemType Directory -Path $BuildInfoDirectory -Force | Out-Null
$BuildInfo = [ordered]@{
    schema_version = 1
    pros_version = $Version
    git_commit = $BuildCommit
}
$BuildInfoJson = $BuildInfo | ConvertTo-Json
[System.IO.File]::WriteAllText(
    $BuildInfoPath,
    $BuildInfoJson + "`n",
    [System.Text.UTF8Encoding]::new($false)
)

$TestsPath = Join-Path $ProjectRoot "tests"
if (-not (Test-Path -LiteralPath $TestsPath -PathType Container)) {
    throw "The source test directory is missing; refusing to build PROS.exe."
}
Write-Host "Running the complete source test suite"
& $Python -m pytest -q $TestsPath
if ($LASTEXITCODE -ne 0) {
    throw "Source tests failed; PROS.exe was not built."
}

Remove-ProjectBuildDirectory -LiteralPath $DistPath
Remove-ProjectBuildDirectory -LiteralPath $PyInstallerWorkPath
New-Item -ItemType Directory -Path $DistPath -Force | Out-Null
New-Item -ItemType Directory -Path $PyInstallerWorkPath -Force | Out-Null

Write-Host "Building the windowed one-file executable"
& $Python -m PyInstaller `
    --clean `
    --noconfirm `
    --distpath $DistPath `
    --workpath $PyInstallerWorkPath `
    $SpecPath
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed."
}

$Executable = Join-Path $DistPath "PROS.exe"
if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) {
    throw "Build completed without producing $Executable"
}
$DistEntries = @(Get-ChildItem -LiteralPath $DistPath -Force)
if ($DistEntries.Count -ne 1 -or $DistEntries[0].Name -ne "PROS.exe") {
    $Names = ($DistEntries | ForEach-Object Name) -join ", "
    throw "The release directory is not a single-file bundle. Found: $Names"
}

if ($UpdateAuditManifests) {
    Write-Host "Updating reviewed PyInstaller warning and frozen-archive manifests"
    & $Python $WarningVerifier --update-allowlist
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to update the PyInstaller warning allowlist."
    }
    & $Python $ArchiveVerifier --update-manifest
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to update the frozen archive manifest."
    }
}
else {
    Write-Host "Checking PyInstaller warnings and the complete frozen inventory"
    & $Python $WarningVerifier
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller warnings differ from the reviewed allowlist."
    }
    & $Python $ArchiveVerifier
    if ($LASTEXITCODE -ne 0) {
        throw "The frozen archive differs from the reviewed full allowlist."
    }
}

& $ArtifactVerifier `
    -Executable $Executable `
    -ExpectedVersion $Version `
    -ExpectedCommit $BuildCommit

$Hash = Get-FileHash -LiteralPath $Executable -Algorithm SHA256
$Size = (Get-Item -LiteralPath $Executable).Length
Write-Host "Build complete: $Executable"
Write-Host "Size: $Size bytes"
Write-Host "SHA-256: $($Hash.Hash)"
if ($UpdateAuditManifests) {
    Write-Warning "Audit manifests were updated. Review and commit them, then run build.ps1 again before release."
}
