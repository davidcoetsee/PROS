[CmdletBinding()]
param(
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [switch]$SkipSelfTest
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$VenvRoot = Join-Path $ProjectRoot ".venv"
$Python = Join-Path $VenvRoot "Scripts\python.exe"
$DistPath = Join-Path $ProjectRoot "dist"
$PyInstallerWorkPath = Join-Path $ProjectRoot "build\pyinstaller"
# Keep spaces in this path intentionally: every release build verifies that
# Windows argument quoting survives the one-file launcher.
$SelfTestRoot = Join-Path $ProjectRoot "build\packaged self test"
$SpecPath = Join-Path $ProjectRoot "PROS.spec"
$RequirementsPath = Join-Path $ProjectRoot "requirements-dev.txt"

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

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    $Launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($null -eq $Launcher) {
        throw "Python launcher not found. Install 64-bit CPython 3.13, then rerun build.ps1."
    }
    Write-Host "Creating isolated Python 3.13 environment at $VenvRoot"
    & $Launcher.Source -3.13 -m venv $VenvRoot
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to create the Python virtual environment."
    }
}

if (-not $SkipInstall) {
    Write-Host "Installing pinned build dependencies"
    & $Python -m pip install --disable-pip-version-check --requirement $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed."
    }
}

if (-not $SkipTests) {
    $TestsPath = Join-Path $ProjectRoot "tests"
    if (Test-Path -LiteralPath $TestsPath -PathType Container) {
        Write-Host "Running source tests"
        & $Python -m pytest -q $TestsPath
        if ($LASTEXITCODE -ne 0) {
            throw "Source tests failed; PROS.exe was not built."
        }
    }
    else {
        Write-Warning "No tests directory was found; continuing with the packaged self-test."
    }
}

# PyInstaller does not remove unrelated files from dist. Clearing these two
# narrowly scoped project directories ensures the release really is one file.
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

if (-not $SkipSelfTest) {
    Remove-ProjectBuildDirectory -LiteralPath $SelfTestRoot
    New-Item -ItemType Directory -Path $SelfTestRoot -Force | Out-Null

    Write-Host "Running the packaged engine self-test"
    $QuotedSelfTestRoot = '"' + $SelfTestRoot + '"'
    $SelfTestProcess = Start-Process `
        -FilePath $Executable `
        -ArgumentList "--self-test $QuotedSelfTestRoot" `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($SelfTestProcess.ExitCode -ne 0) {
        $Report = Get-ChildItem -LiteralPath $SelfTestRoot -Filter "selftest-result.json" -File -Recurse |
            Sort-Object LastWriteTime -Descending |
            Select-Object -First 1
        if ($null -ne $Report) {
            Write-Host (Get-Content -LiteralPath $Report.FullName -Raw)
        }
        throw "Packaged self-test failed with exit code $($SelfTestProcess.ExitCode)."
    }

    $Report = Get-ChildItem -LiteralPath $SelfTestRoot -Filter "selftest-result.json" -File -Recurse |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if ($null -eq $Report) {
        throw "Packaged self-test exited successfully but did not write its result report."
    }
    Write-Host "Packaged self-test report: $($Report.FullName)"
}

$Hash = Get-FileHash -LiteralPath $Executable -Algorithm SHA256
$Size = (Get-Item -LiteralPath $Executable).Length
Write-Host "Build complete: $Executable"
Write-Host "Size: $Size bytes"
Write-Host "SHA-256: $($Hash.Hash)"
