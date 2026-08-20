# PROS 1.2.0

PROS is a local Windows desktop utility for processing PDF files. It can remove
known PDF passwords, aggressively compress PDFs, convert supported colour
content to grayscale independently of compression, join multiple inputs, split
an input at validated page boundaries, and write predictably named results
without changing the source files.

The release is a single windowed `PROS.exe`. It uses Python's Tk interface and
pikepdf/qpdf; it does **not** use Qt, PySide, or PyQt.

## Run the application

1. Copy `PROS.exe` to a local folder on a 64-bit Windows 10 or Windows 11 PC.
2. Double-click it. No Python installation or administrator access is required.
3. Add one or more PDFs, supply passwords for encrypted inputs when needed,
   select processing functions and output options, and choose an output folder
   and base name.
4. Review the preflight summary, then start the job. Keep the application open
   until it reports completion.

PROS leaves input files unchanged. Outputs are staged and validated before they
are published to the selected folder.

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
`Report - Grey.pdf`.

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

## Offline and privacy behaviour

PDF processing is local. PROS does not require an internet connection and does
not upload PDFs, passwords, filenames, or usage information. A PyInstaller
one-file application extracts its embedded Python/Tcl/Tk/native libraries to a
random `%TEMP%\_MEI...` directory while it runs and removes that directory after
a normal exit. The first launch may therefore be slower and may be inspected by
antivirus software.

Building from source normally uses the internet to download pinned packages.
That is separate from the behaviour of the finished executable. For an offline
build, prepare a wheelhouse on a connected Windows x64 machine using the same
Python version:

```powershell
py -3.13 -m pip download --requirement requirements-dev.txt --dest wheelhouse
.\.venv\Scripts\python.exe -m pip install --no-index --find-links wheelhouse --requirement requirements-dev.txt
.\build.ps1 -SkipInstall
```

## Build `PROS.exe`

Prerequisites:

- 64-bit CPython 3.13 from python.org, including Tcl/Tk and the `py.exe` launcher
- Windows PowerShell 5.1 or PowerShell 7+

From the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build.ps1
```

The build script creates `.venv` if needed, installs exact versions from
`requirements-dev.txt`, runs the source test suite, removes only the project's
`dist` and PyInstaller work directories, builds the one-file windowed program,
and runs the packaged engine self-test. The release is:

```text
dist\PROS.exe
```

It prints the executable's byte size and SHA-256 hash when complete. Use
`-SkipInstall`, `-SkipTests`, or `-SkipSelfTest` only when that stage has already
been completed independently.

If `assets\PROS.ico` exists, `PROS.spec` embeds it. If it is absent, the build
uses the PyInstaller default icon. `hook-pikepdf.py` explicitly collects the
qpdf DLLs vendored by the Windows pikepdf wheel. Tkinter's Tcl/Tk libraries are
collected by PyInstaller's standard hooks.

## Packaged self-test

`main.py` dispatches `--self-test` before Tk is initialised. To rerun it:

```powershell
.\dist\PROS.exe --self-test .\build\manual-self-test
```

The test creates a unique directory under the supplied path and generates one
fixed colour image/vector PDF. It processes that source once with compression
plus grayscale and once with grayscale alone, checks the exact `Cprs - Grey`
and `Grey` names, verifies that compression downsamples while grayscale alone
preserves image dimensions, confirms that the source hash did not change,
reopens and syntax-checks both outputs, and writes `selftest-result.json`. It
makes no network calls.

For release acceptance, also test on clean, offline Windows 10 and Windows 11
x64 virtual machines that have no separate Python installation. Exercise paths
containing spaces and non-ASCII characters, an encrypted PDF, invalid/corrupt
input, a read-only output folder, cancellation, aggressive compression,
grayscale alone, compression plus grayscale, and a representative PDF larger
than 180 MiB. Reopen every resulting PDF with an independent viewer and preserve
the source SHA-256 before and after the test.

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

## Third-party software

Copyright, licence, attribution, and source-availability information is in
`THIRD_PARTY_NOTICES.txt`. The same file is embedded in `PROS.exe`. The notices
describe the pinned build represented by this repository; regenerate and review
the dependency inventory whenever any package or CPython build changes.
