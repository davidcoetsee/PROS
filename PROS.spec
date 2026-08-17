# ruff: noqa: F821
"""PyInstaller recipe for the Windows PROS one-file executable."""

from pathlib import Path

PROJECT_ROOT = Path(SPECPATH).resolve()
ENTRY_POINT = PROJECT_ROOT / "main.py"
NOTICES = PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt"
ICON = PROJECT_ROOT / "assets" / "PROS.ico"
VERSION_INFO = PROJECT_ROOT / "packaging" / "version_info.txt"

if not ENTRY_POINT.is_file():
    raise FileNotFoundError(f"Application entry point not found: {ENTRY_POINT}")
if not NOTICES.is_file():
    raise FileNotFoundError(f"Third-party notices not found: {NOTICES}")
if not VERSION_INFO.is_file():
    raise FileNotFoundError(f"Windows version resource not found: {VERSION_INFO}")

BUNDLE_DATA = [(str(NOTICES), ".")]
if ICON.is_file():
    # The PE resource supplies Explorer's icon; Tk also needs a real file at
    # runtime for the window icon.
    BUNDLE_DATA.append((str(ICON), "assets"))


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
    excludes=[],
    noarchive=False,
    optimize=1,
)

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
    icon=str(ICON) if ICON.is_file() else None,
    version=str(VERSION_INFO),
)
