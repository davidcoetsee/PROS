# SPDX-License-Identifier: MPL-2.0
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ProjectRoot = [System.IO.Path]::GetFullPath($PSScriptRoot)
$BuildScript = Join-Path $ProjectRoot "build.ps1"
$Python = Join-Path $ProjectRoot ".release-venv\Scripts\python.exe"
$ArtifactVerifier = Join-Path $ProjectRoot "verify_release_artifact.ps1"
$Executable = Join-Path $ProjectRoot "dist\PROS.exe"
$ReleaseRoot = Join-Path $ProjectRoot "release"

function Assert-ProjectChildPath {
    param([Parameter(Mandatory = $true)][string]$LiteralPath)

    $resolved = [System.IO.Path]::GetFullPath($LiteralPath)
    $prefix = $ProjectRoot.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $resolved.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $resolved"
    }
    return $resolved
}

function Get-LocalReleaseState {
    param([Parameter(Mandatory = $true)][string]$TagName)

    $GitStatus = @(& git -C $ProjectRoot status --porcelain --untracked-files=all)
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect the PROS Git working tree."
    }
    if ($GitStatus.Count -ne 0) {
        throw "Commit all release changes before preparing immutable assets."
    }

    $HeadCommit = (& git -C $ProjectRoot rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $HeadCommit -notmatch '^[0-9a-f]{40}$') {
        throw "Unable to identify the PROS release commit."
    }

    $TagReference = "refs/tags/$TagName^{commit}"
    $TagOutput = @(& git -C $ProjectRoot rev-parse --verify $TagReference 2>$null)
    if ($LASTEXITCODE -ne 0 -or $TagOutput.Count -ne 1) {
        throw "Create the exact release tag $TagName before preparing assets."
    }
    $TagCommit = "$($TagOutput[0])".Trim()
    if ($TagCommit -ne $HeadCommit) {
        throw "Release tag $TagName does not point to HEAD ($HeadCommit)."
    }

    return @{ HeadCommit = $HeadCommit; TagCommit = $TagCommit }
}

function Get-RemoteTagCommit {
    param([Parameter(Mandatory = $true)][string]$TagName)

    $OriginUrl = (& git -C $ProjectRoot remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or
        $OriginUrl -notmatch '^(https://github\.com/davidcoetsee/PROS(?:\.git)?|git@github\.com:davidcoetsee/PROS\.git)$') {
        throw "Git remote 'origin' must be the official davidcoetsee/PROS repository."
    }

    $DirectReference = "refs/tags/$TagName"
    $PeeledReference = "refs/tags/$TagName^{}"
    $RemoteLines = @(
        & git -C $ProjectRoot ls-remote --exit-code --tags origin $DirectReference $PeeledReference
    )
    if ($LASTEXITCODE -ne 0 -or $RemoteLines.Count -eq 0) {
        throw "The public remote does not contain release tag $TagName."
    }

    $DirectCommit = $null
    $PeeledCommit = $null
    foreach ($Line in $RemoteLines) {
        $Parts = "$Line" -split "`t", 2
        if ($Parts.Count -ne 2 -or $Parts[0] -notmatch '^[0-9a-f]{40}$') {
            continue
        }
        if ($Parts[1] -eq $PeeledReference) {
            $PeeledCommit = $Parts[0]
        }
        elseif ($Parts[1] -eq $DirectReference) {
            $DirectCommit = $Parts[0]
        }
    }
    $RemoteCommit = if ($PeeledCommit) { $PeeledCommit } else { $DirectCommit }
    if (-not $RemoteCommit) {
        throw "Unable to resolve the public commit for tag $TagName."
    }
    return $RemoteCommit
}

function Assert-SourceArchive {
    param(
        [Parameter(Mandatory = $true)][string]$LiteralPath,
        [Parameter(Mandatory = $true)][string]$ExpectedHash,
        [Parameter(Mandatory = $true)][string]$RequiredEntry
    )

    $ActualHash = (Get-FileHash -LiteralPath $LiteralPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($ActualHash -ne $ExpectedHash) {
        throw "Third-party source archive hash mismatch: $LiteralPath"
    }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $Archive = [System.IO.Compression.ZipFile]::OpenRead($LiteralPath)
    try {
        if (-not ($Archive.Entries | Where-Object FullName -eq $RequiredEntry)) {
            throw "Third-party source archive lacks required entry $RequiredEntry"
        }
    }
    finally {
        $Archive.Dispose()
    }
}

$VersionSource = Get-Content -LiteralPath (Join-Path $ProjectRoot "pros\__init__.py") -Raw
$VersionMatch = [regex]::Match($VersionSource, '__version__\s*=\s*"(?<version>\d+\.\d+\.\d+)"')
if (-not $VersionMatch.Success) {
    throw "Unable to determine a valid PROS source version."
}
$SourceVersion = $VersionMatch.Groups["version"].Value
$TagName = "v$SourceVersion"
$InitialState = Get-LocalReleaseState -TagName $TagName
$RemoteCommit = Get-RemoteTagCommit -TagName $TagName
if ($RemoteCommit -ne $InitialState.HeadCommit) {
    throw "Public tag $TagName points to $RemoteCommit, not local HEAD $($InitialState.HeadCommit)."
}

$PublishedSource = "https://github.com/davidcoetsee/PROS/tree/$TagName"
try {
    $SourceResponse = Invoke-WebRequest `
        -Uri $PublishedSource `
        -Method Head `
        -MaximumRedirection 5 `
        -UseBasicParsing
}
catch {
    throw "Matching public source is unavailable at $PublishedSource."
}
if ($SourceResponse.StatusCode -ne 200) {
    throw "Matching public source returned HTTP $($SourceResponse.StatusCode): $PublishedSource"
}

& $BuildScript

$FinalState = Get-LocalReleaseState -TagName $TagName
$RemoteCommit = Get-RemoteTagCommit -TagName $TagName
if ($FinalState.HeadCommit -ne $InitialState.HeadCommit -or
    $FinalState.TagCommit -ne $InitialState.TagCommit -or
    $RemoteCommit -ne $FinalState.HeadCommit) {
    throw "Source, local tag, or public tag changed during the release build."
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "The clean release environment is missing after the build."
}

& $ArtifactVerifier `
    -Executable $Executable `
    -ExpectedVersion $SourceVersion `
    -ExpectedCommit $FinalState.HeadCommit

$FinalReleaseDirectory = Assert-ProjectChildPath -LiteralPath (Join-Path $ReleaseRoot $TagName)
if (Test-Path -LiteralPath $FinalReleaseDirectory) {
    throw "Release assets already exist; refusing to replace $FinalReleaseDirectory"
}
New-Item -ItemType Directory -Path $ReleaseRoot -Force | Out-Null
$StagingDirectory = Assert-ProjectChildPath -LiteralPath (
    Join-Path $ReleaseRoot ".staging-$TagName-$([guid]::NewGuid().ToString('N'))"
)
New-Item -ItemType Directory -Path $StagingDirectory -Force | Out-Null

try {
    $ReleaseExecutable = Join-Path $StagingDirectory "PROS-$SourceVersion-windows-x64.exe"
    Copy-Item -LiteralPath $Executable -Destination $ReleaseExecutable
    $OriginalHash = (Get-FileHash -LiteralPath $Executable -Algorithm SHA256).Hash
    $CopiedHash = (Get-FileHash -LiteralPath $ReleaseExecutable -Algorithm SHA256).Hash
    if ($OriginalHash -ne $CopiedHash) {
        throw "Copied release executable does not match dist\PROS.exe."
    }

    $SourceArchive = Join-Path $StagingDirectory "PROS-$SourceVersion-source.zip"
    & git -C $ProjectRoot archive `
        --format=zip `
        "--prefix=PROS-$SourceVersion/" `
        "--output=$SourceArchive" `
        "refs/tags/$TagName"
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $SourceArchive -PathType Leaf)) {
        throw "Unable to create the exact tagged PROS source archive."
    }

    $ThirdPartyArchives = @(
        @{
            Name = "pikepdf-10.11.0-source.zip"
            Uri = "https://github.com/pikepdf/pikepdf/archive/refs/tags/v10.11.0.zip"
            Sha256 = "d56fde283fbd05fb854c3c42a5f5cbaecd5f079f098d6ee4c45bf297a658e385"
            RequiredEntry = "pikepdf-10.11.0/LICENSE.txt"
        },
        @{
            Name = "qpdf-12.3.2-source.zip"
            Uri = "https://github.com/qpdf/qpdf/archive/refs/tags/v12.3.2.zip"
            Sha256 = "86319e0167e7a4d74739338f5fd5af35445af81abac46ba86cf1e7ebffca3316"
            RequiredEntry = "qpdf-12.3.2/LICENSE.txt"
        }
    )
    foreach ($Archive in $ThirdPartyArchives) {
        $Destination = Join-Path $StagingDirectory $Archive.Name
        Invoke-WebRequest -Uri $Archive.Uri -OutFile $Destination -UseBasicParsing
        Assert-SourceArchive `
            -LiteralPath $Destination `
            -ExpectedHash $Archive.Sha256 `
            -RequiredEntry $Archive.RequiredEntry
    }

    $ChecksumPath = Join-Path $StagingDirectory "SHA256SUMS.txt"
    $ChecksumLines = Get-ChildItem -LiteralPath $StagingDirectory -File |
        Where-Object Name -ne "SHA256SUMS.txt" |
        Sort-Object Name |
        ForEach-Object {
            $Hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
            "$($Hash.Hash.ToLowerInvariant()) *$($_.Name)"
        }
    [System.IO.File]::WriteAllLines(
        $ChecksumPath,
        $ChecksumLines,
        [System.Text.UTF8Encoding]::new($false)
    )

    Move-Item -LiteralPath $StagingDirectory -Destination $FinalReleaseDirectory
}
finally {
    if (Test-Path -LiteralPath $StagingDirectory) {
        Remove-Item -LiteralPath $StagingDirectory -Recurse -Force
    }
}

Write-Host "Release assets prepared atomically at $FinalReleaseDirectory"
Get-ChildItem -LiteralPath $FinalReleaseDirectory -File | Sort-Object Name |
    Select-Object Name, Length
