# SPDX-License-Identifier: MPL-2.0
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [Parameter(Mandatory = $true)][string]$ExpectedVersion,
    [Parameter(Mandatory = $true)][string]$ExpectedCommit
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$Python = Join-Path $ProjectRoot ".release-venv\Scripts\python.exe"
$ArchiveVerifier = Join-Path $ProjectRoot "tools\verify_frozen_archive.py"
$VerificationRoot = Join-Path $ProjectRoot "build\release artifact verification"

function Assert-ProjectChildPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = [System.IO.Path]::GetFullPath($LiteralPath)
    $prefix = $ProjectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to inspect or modify a path outside the project: $resolved"
    }
    return $resolved
}

function Remove-ProjectDirectory {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $safePath = Assert-ProjectChildPath -LiteralPath $LiteralPath
    if (Test-Path -LiteralPath $safePath) {
        Remove-Item -LiteralPath $safePath -Recurse -Force
    }
}

if ($ExpectedVersion -notmatch '^\d+\.\d+\.\d+$') {
    throw "ExpectedVersion must use semantic version form, for example 1.5.1."
}
if ($ExpectedCommit -notmatch '^[0-9a-f]{40}$') {
    throw "ExpectedCommit must be a full lowercase Git commit ID."
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "The project virtual environment is missing: $Python"
}

$ExecutablePath = Assert-ProjectChildPath -LiteralPath $Executable
if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "The one-file executable is missing: $ExecutablePath"
}

$VersionInfo = (Get-Item -LiteralPath $ExecutablePath).VersionInfo
if ($VersionInfo.FileVersion -ne $ExpectedVersion) {
    throw "EXE FileVersion mismatch: expected $ExpectedVersion, found $($VersionInfo.FileVersion)."
}
if ($VersionInfo.ProductVersion -ne $ExpectedVersion) {
    throw "EXE ProductVersion mismatch: expected $ExpectedVersion, found $($VersionInfo.ProductVersion)."
}

Write-Host "Verifying the complete frozen archive, PE format, and icon resources"
& $Python $ArchiveVerifier --executable $ExecutablePath
if ($LASTEXITCODE -ne 0) {
    throw "The executable failed its complete frozen-archive or PE audit."
}

Remove-ProjectDirectory -LiteralPath $VerificationRoot
New-Item -ItemType Directory -Path $VerificationRoot -Force | Out-Null

Write-Host "Running the packaged release-integrity self-test"
$QuotedVerificationRoot = '"' + $VerificationRoot + '"'
$SelfTestProcess = Start-Process `
    -FilePath $ExecutablePath `
    -ArgumentList "--self-test $QuotedVerificationRoot" `
    -WindowStyle Hidden `
    -PassThru
if (-not $SelfTestProcess.WaitForExit(180000)) {
    Stop-Process -Id $SelfTestProcess.Id -Force -ErrorAction SilentlyContinue
    $SelfTestProcess.WaitForExit(10000) | Out-Null
    throw "Packaged self-test exceeded the 180-second release deadline."
}
if ($SelfTestProcess.ExitCode -ne 0) {
    throw "Packaged self-test failed with exit code $($SelfTestProcess.ExitCode)."
}

$Reports = @(
    Get-ChildItem -LiteralPath $VerificationRoot -Filter "selftest-result.json" -File -Recurse
)
if ($Reports.Count -ne 1) {
    throw "Expected exactly one packaged self-test report, found $($Reports.Count)."
}
$Report = Get-Content -LiteralPath $Reports[0].FullName -Raw | ConvertFrom-Json

$ExpectedReportValues = @{
    status = "ok"
    pros = $ExpectedVersion
    python = "3.13.15"
    pikepdf = "10.11.0"
    qpdf = "12.3.2"
}
foreach ($Name in $ExpectedReportValues.Keys) {
    $Actual = $Report.$Name
    $Expected = $ExpectedReportValues[$Name]
    if ($Actual -ne $Expected) {
        throw "Packaged self-test $Name mismatch: expected $Expected, found $Actual."
    }
}
if ($Report.frozen -ne $true) {
    throw "The packaged self-test did not report a frozen executable."
}
if ($Report.build_info.embedded -ne $true -or
    $Report.build_info.pros_version -ne $ExpectedVersion -or
    $Report.build_info.git_commit -ne $ExpectedCommit) {
    throw "The executable's embedded build commit/version does not match the release source."
}
if ($Report.drag_and_drop.wrapper_version -ne "0.6.2" -or
    $Report.drag_and_drop.tkdnd_version -ne "2.10.1" -or
    $Report.drag_and_drop.tcl_version -ne "8.6.15" -or
    $Report.drag_and_drop.tk_version -ne "8.6.15") {
    throw "The packaged drag-and-drop or Tcl/Tk runtime does not match the audited release."
}
if ($Report.drag_and_drop.image_tk_loaded -ne $true) {
    throw "The packaged Pillow ImageTk/Tk image bridge did not load."
}

$ExpectedRuntimeInventory = @{
    openssl = "OpenSSL 3.0.21 9 Jun 2026"
    zlib = "1.3.1"
    lxml = "6.1.1"
    libxml2 = "2.11.9"
    libxslt = "1.1.45"
    pillow = "12.3.0"
}
foreach ($Name in $ExpectedRuntimeInventory.Keys) {
    $Actual = $Report.runtime_inventory.$Name
    $Expected = $ExpectedRuntimeInventory[$Name]
    if ($Actual -ne $Expected) {
        throw "Packaged runtime $Name mismatch: expected $Expected, found $Actual."
    }
}
$ExpectedPillowNative = @{
    freetype2 = "2.14.3"
    littlecms2 = "2.19"
    webp = "1.6.0"
    avif = "1.4.2"
    libjpeg_turbo = "3.1.4.1"
    zlib_ng = "2.3.3"
    jpg_2000 = "2.5.4"
    libtiff = "4.7.1"
}
foreach ($Name in $ExpectedPillowNative.Keys) {
    $Actual = $Report.runtime_inventory.pillow_native.$Name
    $Expected = $ExpectedPillowNative[$Name]
    if ($Actual -ne $Expected) {
        throw "Packaged Pillow native component $Name mismatch: expected $Expected, found $Actual."
    }
}
if ($Report.runtime_inventory.pillow_flags.libjpeg_turbo -ne $true -or
    $Report.runtime_inventory.pillow_flags.zlib_ng -ne $true) {
    throw "The packaged Pillow build lacks an audited native codec implementation."
}

$ExpectedLegalFiles = @(
    "ASSET_LICENSES.md",
    "LICENSE",
    "SOURCE_CODE.txt",
    "THIRD_PARTY_NOTICES.txt",
    "TRADEMARKS.md"
)
$ActualLegalFiles = @($Report.legal_documents.files.PSObject.Properties.Name | Sort-Object)
if (($ActualLegalFiles -join "|") -ne (($ExpectedLegalFiles | Sort-Object) -join "|")) {
    throw "The packaged legal-document set does not match the release manifest."
}
foreach ($Filename in $ExpectedLegalFiles) {
    $SourcePath = Join-Path $ProjectRoot $Filename
    $SourceHash = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $PackagedHash = $Report.legal_documents.files.$Filename.sha256
    if ($PackagedHash -ne $SourceHash) {
        throw "Packaged legal document does not match the release source: $Filename"
    }
}

$ExpectedBrandFiles = @(
    "PROS-App-Icon.png",
    "PROS-App-Icon.svg",
    "PROS-Logo.png",
    "PROS-Logo.svg",
    "PROS.ico"
)
$ActualBrandFiles = @($Report.brand_assets.files.PSObject.Properties.Name | Sort-Object)
if (($ActualBrandFiles -join "|") -ne (($ExpectedBrandFiles | Sort-Object) -join "|")) {
    throw "The packaged brand-asset set does not match the release manifest."
}
foreach ($Filename in $ExpectedBrandFiles) {
    $SourcePath = Join-Path $ProjectRoot "assets\$Filename"
    $SourceHash = (Get-FileHash -LiteralPath $SourcePath -Algorithm SHA256).Hash.ToLowerInvariant()
    $PackagedHash = $Report.brand_assets.files.$Filename.sha256
    if ($PackagedHash -ne $SourceHash) {
        throw "Packaged brand asset does not match the release source: $Filename"
    }
}

$Hash = Get-FileHash -LiteralPath $ExecutablePath -Algorithm SHA256
$Size = (Get-Item -LiteralPath $ExecutablePath).Length
Write-Host "Release artifact verified: $ExecutablePath"
Write-Host "Packaged self-test report: $($Reports[0].FullName)"
Write-Host "Size: $Size bytes"
Write-Host "SHA-256: $($Hash.Hash)"
