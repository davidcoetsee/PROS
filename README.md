# PROS 1.5.1

PROS is a free, open-source Windows desktop utility for processing PDF files.
It can remove known PDF passwords, aggressively compress PDFs, convert
supported colour content to grayscale independently of compression, join
multiple inputs, split an input at validated page boundaries, and write
predictably named results without changing the source files.

The release is a single windowed `PROS.exe`. It uses Python's Tk interface and
pikepdf/qpdf. The tkinterdnd2 wrapper and its bundled TkDND extension provide
native Windows File Explorer drag and drop. PROS does **not** use Qt, PySide, or
PyQt.

Official Windows builds are provided without charge on the
[GitHub Releases page](https://github.com/davidcoetsee/PROS/releases). Each
release keeps the one-file portable format and links the executable to its
matching source tag and checksums.

## Run the application

1. Copy `PROS.exe` to a local folder on a 64-bit Windows 10 or Windows 11 PC.
2. Double-click it. No Python installation or administrator access is required.
3. Complete the numbered workflow from top to bottom: select a file arrangement
   and any additional processing, add the input PDFs, choose the output, review
   the job, and then process it. Split inserts Choose where to split as step 3
   and renumbers the remaining cards, making a six-step workflow. Required steps
   remain red until complete and then turn blue; Process turns blue only after
   the job succeeds.
4. Add PDFs with the file picker or drag them from Windows File Explorer onto
   the input area. Keep separate and Combine accept multiple files; Split accepts
   one. Click the already-selected arrangement a second time within four seconds
   to open the picker again.
5. Supply passwords for encrypted inputs when needed, choose an output folder,
   and confirm the displayed order. A multi-file Keep separate job derives each
   output name from its corresponding source file, so the shared name field is
   disabled and labelled automatic.
6. Check the dedicated Review step, then start the job from the separate Process
   step. Keep the application open until it reports completion.

PROS leaves input files unchanged. Outputs are staged and validated before they
are published to the selected folder.

## Drag and drop in every arrangement

External file drag and drop is available in Keep separate, Combine into one PDF,
and Split one PDF. Dropped PDFs enter the same existence, regular-file, `.pdf`,
duplicate, and capacity checks as files chosen with the Add control, and both
routes preserve the submitted order. Keep separate writes one output for each
input in displayed order. Combine uses that order as the final page order; use
the Up and Down controls to adjust it. Split remains a single-input operation.
The ordinary file picker remains available as a keyboard-accessible alternative.

Keep separate is a processing operation, not a duplicate-file command: select
Remove password, Reduce file size, Convert to grayscale, or a combination before
starting it. Multi-file outputs apply the same selected processing to every
input.

Drop paths are handled as Windows/Tcl file lists, including multiple files and
paths containing spaces or non-ASCII characters. Folders, non-PDF items,
unavailable files, duplicates, and files beyond the twelve-input Keep
separate/Combine limit are not accepted. Split does not accept a second input.
Drops are refused while a job is active or the selected arrangement is full.

Run PROS normally rather than as administrator. Windows generally blocks File
Explorer, which runs at normal integrity, from dropping files into an elevated
application.

## Compression, grayscale, and progress

PROS uses one aggressive compression profile: the profile previously called
Ultra. It rewrites PDF structure, uses a JPEG quality target of 65, and may
downsample supported large raster images toward 150 DPI. Compression is lossy;
fine image detail and small scanned text may become softer. Always inspect
important outputs before discarding any separate working copies.

Grayscale converts supported embedded raster image objects and common
DeviceRGB/DeviceCMYK colour operators in page and nested Form content. Ordinary
text, vector fills, and vector strokes that use those common operators are
therefore converted. ICC-based and spot colours, patterns, shadings, some
transparency constructs, annotation or form-widget appearances, and masked or
unusually encoded images may remain in their original colour. PROS retains such
unsupported constructs and reports a warning so the output can be reviewed.
Grayscale can be selected with or without compression.

Automatic suffixes are ordered `Join`, `Pwd_Rmv`, `Cprs`, then `Grey`, followed
by `Part N` for split outputs. For example, compression plus grayscale produces
`Report - Cprs - Grey.pdf`, while grayscale alone produces
`Report - Grey.pdf`. A multi-file Keep separate job ignores the single shared
base-name field and uses each input stem with its active processing suffixes;
for example, `Invoice A.pdf` and `Invoice B.pdf` become
`Invoice A - Cprs.pdf` and `Invoice B - Cprs.pdf` when compression is selected.
If two inputs from different folders would produce the same Windows output name,
preflight blocks the job so neither result can overwrite the other.

The interface uses the same active, inactive, hover, and disabled treatment for
action and arrangement buttons. The `+ Add`, `- Remove`, and `Clear` controls are
grouped consistently at the bottom-right of the list they change; Split includes
its own Clear control. Guidance, progress, success, and attention messages use a
common bordered-panel style. Review and Process remain separate so choosing
settings cannot accidentally start a job.

PROS does not show a pre-run estimate of runtime or output size. Progress shown
at real processing boundaries and during saves comes from worker events. Between
worker reports, the per-file compression meter advances periodically as a
time-based guesstimate so the window stays responsive during native PDF work; it
is not a byte-accurate measurement and can pause or advance unevenly. Worker
events, the final completion message, and output validation are authoritative.

Keep enough free disk space for the input, staging files, compression candidates,
and final output; several times the combined input size is prudent. Compression
and grayscale processing may decode an embedded image into memory, where its
size is determined by pixel dimensions and colour channels rather than its
compressed size in the PDF. A document near the 180 MiB acceptance size can
therefore need multiple gigabytes of temporary disk space and memory when it
contains very large scans. Close other memory-intensive applications before
processing such a document.

## Brand assets

The main window header uses `assets\PROS-Logo.png`, and the About dialog uses
`assets\PROS-App-Icon.png`. `assets\PROS.ico` supplies both the Windows
executable/Explorer icon and the Tk window icon. All three runtime images are
bundled inside the one-file executable.

`assets\PROS-Logo.svg` and `assets\PROS-App-Icon.svg` are the editable vector
masters for documentation and future asset generation. They are bundled for
release verification but are not rendered directly by Tk. Their lettering uses
the Windows Segoe UI family with Arial and generic sans-serif fallbacks, so use
the supplied PNG/ICO files when an exact, portable render is required.

## Offline and privacy behaviour

PDF processing is local. PROS does not require an internet connection and does
not upload PDFs, passwords, filenames, or usage information. A PyInstaller
one-file application extracts its embedded Python/Tcl/Tk/native libraries to a
random `%TEMP%\_MEI...` directory while it runs and removes that directory after
a normal exit. The first launch may therefore be slower and may be inspected by
antivirus software.

Building from source normally uses the internet to download hash-approved
Windows wheels. That is separate from the behaviour of the finished
executable. For an offline release build, prepare a wheelhouse on a connected
Windows x64 machine using the exact Python version and audited lock file:

```powershell
py -3.13 -m pip download --require-hashes --only-binary=:all: --requirement requirements-windows-x64.lock --dest wheelhouse
$env:PROS_WHEELHOUSE = (Resolve-Path .\wheelhouse)
.\build.ps1
Remove-Item Env:\PROS_WHEELHOUSE
```

## Build `PROS.exe`

Prerequisites:

- 64-bit CPython 3.13.15 from python.org, including Tcl/Tk and the `py.exe`
  launcher
- Windows PowerShell 5.1 or PowerShell 7+

From the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

The build script requires a clean Git commit, recreates an isolated
`.release-venv`, and installs only the exact Windows wheels and SHA-256 hashes
approved by `requirements-windows-x64.lock`. It rejects extra distributions,
verifies the audited Python, package, and native-library versions, runs the full
source test suite, removes only project-scoped build directories, builds the
one-file windowed program, and runs the packaged engine self-test. The release
is:

```text
dist\PROS.exe
```

It also compares every embedded outer-archive item, Python module, standard
library ZIP entry, DLL, and data file with a reviewed allowlist. The build fails
on an unexpected PyInstaller warning; absent legal, brand, PDF, GUI, or
drag-and-drop payload; the unused ISO Schematron module or data; incorrect PE
architecture/subsystem/icon resources; or a mismatch between the embedded Git
commit and the clean source tree. No release verification stage is skippable.
The script prints the executable's byte size and SHA-256 hash when complete.

After an intentional dependency, PyInstaller, or frozen-payload change, run
`.\build.ps1 -UpdateAuditManifests` once from an otherwise clean commit.
Review the exact warning and archive changes it writes under `packaging`, commit
the approved manifests, and then run ordinary `.\build.ps1` again. The update
switch is maintenance tooling and never bypasses verification for an official
release.

`PROS.spec` requires and bundles the five canonical brand files; the MPL
licence; the exact-source, trademark, and asset notices; and the complete
third-party notices. The Windows/Tk ICO, logo and application-icon PNGs, and
both SVG masters remain available inside the one-file executable. The build
fails instead of silently substituting PyInstaller artwork or omitting a legal
document if any required file is missing.
`hook-pikepdf.py` explicitly collects the qpdf DLLs vendored by the Windows
pikepdf wheel. PyInstaller's standard Tk hooks collect Tcl/Tk, while the pinned
pyinstaller-hooks-contrib hook collects only tkinterdnd2's Windows x64 TkDND
DLL and Tcl scripts. No blanket Pillow, Tk, or multi-platform DnD collection is
used. The unused `lxml.isoschematron` module and its data resources are
explicitly excluded; normal lxml, pikepdf, qpdf, compression, and grayscale
functionality remains included.

## Prepare release assets

After committing and successfully building the exact source, create tag
`v1.5.1` at that commit and push the repository and tag publicly. Then run:

```powershell
.\prepare_release.ps1
```

The script requires a clean Git working tree and proves that the local tag,
public remote tag, and `HEAD` resolve to the same commit. It always performs a
fresh hash-locked rebuild and confirms that the EXE FileVersion, ProductVersion,
embedded commit, packaged self-test version, source files, and tag all agree.
It then validates hash-pinned pikepdf and qpdf source archives and prepares the
renamed Windows executable, exact tagged PROS source archive, third-party source
archives, and `SHA256SUMS.txt` in a unique staging directory. The completed
directory is moved into place only after every check succeeds, and an existing
version is never overwritten. The ignored `release` directory is uploaded only
to the matching tagged GitHub Release.

## Packaged self-test

`main.py` dispatches `--self-test` before the application GUI is initialised. To
rerun it:

```powershell
.\dist\PROS.exe --self-test .\build\manual-self-test
```

The test creates a unique directory under the supplied path and generates two
fixed colour image/vector PDFs. It processes the first once with compression
plus grayscale and once with grayscale alone, checks the exact `Cprs - Grey`
and `Grey` names, verifies that compression downsamples while grayscale alone
preserves image dimensions, and checks common vector/raster grayscale conversion.
It then submits both sources to Keep separate in a deliberately non-alphabetic
order, requiring one ordered, reopenable, syntax-clean `Cprs` output per input
whose name comes from that input's stem rather than the global base-name field.
Every source hash must remain unchanged.

The test validates every bundled brand asset's release hash and image/vector
metadata and writes `selftest-result.json`. The report records all five asset
hashes and all three PDF jobs. It also creates a withdrawn Tk window, loads the
bundled Windows x64 TkDND extension, verifies the exact nine-file DLL/Tcl
payload, and records the reported Tcl, Tk, and TkDND versions. No interactive
window is shown and the test makes no network calls.

For release acceptance, also test on clean, offline Windows 10 and Windows 11
x64 virtual machines that have no separate Python installation. Exercise paths
containing spaces and non-ASCII characters, an encrypted PDF, invalid/corrupt
input, a read-only output folder, cancellation, aggressive compression,
grayscale alone, compression plus grayscale, and a representative PDF larger
than 180 MiB. In the frozen executable, drag PDFs from File Explorer into every
arrangement, including paths containing spaces and non-ASCII characters.
Confirm one ordered, source-stem-named output per input for multi-file Keep
separate, the displayed and output order for Combine, single-input enforcement
for Split, and the twelve-input limit. Reopen every resulting PDF with an
independent viewer and preserve the source SHA-256 before and after the test.

## Development checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
$env:PROS_RUN_LARGE_TEST = "1"
.\.venv\Scripts\python.exe -m pytest -q tests\test_large_file_acceptance.py
Remove-Item Env:\PROS_RUN_LARGE_TEST
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m piplicenses --with-license-file --with-notice-file
```

Inspect `build\pyinstaller\PROS\warn-PROS.txt` after each dependency upgrade.
Unexpected missing imports must be resolved before release. Do not add blanket
`--collect-all PySide6` or other Qt workarounds; Qt is not a PROS dependency.

## Licence

Copyright (c) 2026 David Coetsee and PROS contributors.

Except where a file or notice states otherwise, PROS-authored source code,
documentation, and original asset files in this repository are licensed under
the [Mozilla Public License 2.0](LICENSE) (`MPL-2.0`). MPL-2.0 applies at the
file level: changes to MPL-covered files that are distributed must remain
available under MPL-2.0, while those files may be combined with separately
licensed code.

The PROS name, logo, wordmark, and application icon identify official project
releases. Their copyright is covered by MPL-2.0 where they are PROS-authored
project files, but MPL-2.0 grants no trademark rights. See
[TRADEMARKS.md](TRADEMARKS.md) for permitted brand use, including unmodified
redistribution and clearly identified forks.

Third-party components are not relicensed under MPL-2.0. They remain under
their respective terms, which are recorded in
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).

Each official binary release must identify the matching repository tag or
versioned source archive from which it was built. Anyone distributing PROS in
Executable Form must comply with MPL-2.0 Section 3.2, including informing
recipients how they can obtain the corresponding MPL-covered Source Code Form
by reasonable means in a timely manner.

## Third-party software

Copyright, licence, attribution, and source-availability information is in
`THIRD_PARTY_NOTICES.txt`. The same file is embedded in `PROS.exe`. The notices
describe the pinned build represented by this repository; regenerate and review
the dependency inventory whenever any package or CPython build changes.
