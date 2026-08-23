# SPDX-License-Identifier: MPL-2.0
# ruff: noqa: F821
"""PyInstaller recipe for the Windows PROS one-file executable."""

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve()
ENTRY_POINT = PROJECT_ROOT / "main.py"
ICON = PROJECT_ROOT / "assets" / "PROS.ico"
VERSION_INFO = PROJECT_ROOT / "packaging" / "version_info.txt"
BUILD_INFO = PROJECT_ROOT / "build" / "generated" / "BUILD_INFO.json"
LEGAL_DOCUMENT_NAMES = (
    "LICENSE",
    "SOURCE_CODE.txt",
    "TRADEMARKS.md",
    "ASSET_LICENSES.md",
    "THIRD_PARTY_NOTICES.txt",
)
LEGAL_DOCUMENTS = tuple(PROJECT_ROOT / name for name in LEGAL_DOCUMENT_NAMES)
BRAND_ASSET_NAMES = (
    "PROS.ico",
    "PROS-Logo.png",
    "PROS-App-Icon.png",
    "PROS-Logo.svg",
    "PROS-App-Icon.svg",
)
BRAND_ASSETS = tuple(PROJECT_ROOT / "assets" / name for name in BRAND_ASSET_NAMES)
REDUNDANT_WINDOWS_API_SET_CONTRACTS = {
    "api-ms-win-core-fibers-l1-1-1.dll",
    "api-ms-win-core-kernel32-legacy-l1-1-1.dll",
    "api-ms-win-core-sysinfo-l1-2-0.dll",
}

if not ENTRY_POINT.is_file():
    raise FileNotFoundError(f"Application entry point not found: {ENTRY_POINT}")
if not VERSION_INFO.is_file():
    raise FileNotFoundError(f"Windows version resource not found: {VERSION_INFO}")
if not BUILD_INFO.is_file():
    raise FileNotFoundError(f"Build provenance metadata not found: {BUILD_INFO}")
for document in LEGAL_DOCUMENTS:
    if not document.is_file():
        raise FileNotFoundError(f"Legal document not found: {document}")
for brand_asset in BRAND_ASSETS:
    if not brand_asset.is_file():
        raise FileNotFoundError(f"Brand asset not found: {brand_asset}")

BUNDLE_DATA = [(str(document), ".") for document in LEGAL_DOCUMENTS]
BUNDLE_DATA.append((str(BUILD_INFO), "."))
# The ICO supplies the PE/Explorer icon and is also bundled for Tk at runtime.
# The PNG display assets and SVG masters remain available to the header, About
# dialog, documentation, and packaged self-test.
BUNDLE_DATA.extend((str(asset), "assets") for asset in BRAND_ASSETS)


a = Analysis(
    [str(ENTRY_POINT)],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=BUNDLE_DATA,
    hiddenimports=[
        # main.py dispatches this mode before starting Tk. Keep it explicit so
        # a future importlib-based dispatcher cannot silently omit it.
        "pros.selftest",
        "pikepdf",
    ],
    hookspath=[str(PROJECT_ROOT)],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # The generic lxml hook collects every submodule. PROS and pikepdf do
        # not use ISO Schematron, so omit that module and its XSL/RNG payload
        # while retaining lxml.etree/objectify and the rest of lxml.
        "lxml.isoschematron",
    ],
    noarchive=False,
    optimize=1,
)

# Windows 10 and later resolve api-ms-win-* names through the OS API-set
# schema; they are virtual contracts rather than implementation DLLs. The
# GitHub Windows Server Python toolcache exposes three physical downlevel
# forwarders that desktop Python does not, so remove only those redundant
# aliases to keep the audited one-file payload identical across builders.
# https://learn.microsoft.com/windows/win32/apiindex/windows-apisets
a.binaries = [
    entry
    for entry in a.binaries
    if Path(entry[0]).name.casefold() not in REDUNDANT_WINDOWS_API_SET_CONTRACTS
]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="PROS",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON),
    version=str(VERSION_INFO),
)
