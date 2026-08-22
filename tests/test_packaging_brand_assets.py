"""Release metadata and canonical brand asset regression checks."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import _pyinstaller_hooks_contrib

from pros import __version__, selftest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_ASSET_NAMES = {
    "PROS.ico",
    "PROS-Logo.png",
    "PROS-App-Icon.png",
    "PROS-Logo.svg",
    "PROS-App-Icon.svg",
}


def test_source_brand_assets_match_release_manifest() -> None:
    report = selftest._inspect_brand_assets()
    assert Path(report["directory"]) == PROJECT_ROOT / "assets"
    files = report["files"]
    assert set(files) == EXPECTED_ASSET_NAMES

    assert files["PROS-Logo.png"]["dimensions"] == [1200, 100]
    assert files["PROS-Logo.png"]["mode"] == "RGBA"
    assert files["PROS-App-Icon.png"]["dimensions"] == [1024, 1024]
    assert files["PROS-App-Icon.png"]["mode"] == "RGBA"
    assert files["PROS.ico"]["frame_sizes"] == [
        [16, 16],
        [32, 32],
        [48, 48],
        [64, 64],
        [128, 128],
        [256, 256],
    ]
    assert files["PROS-Logo.svg"]["format"] == "SVG"
    assert files["PROS-App-Icon.svg"]["format"] == "SVG"
    assert {
        name: details["sha256"] for name, details in files.items()
    } == selftest._BRAND_ASSET_HASHES


def test_tkdnd_payload_and_packaging_hook_match_the_pinned_release() -> None:
    report = selftest._inspect_dnd_payload()
    assert version("tkinterdnd2") == "0.6.2"
    assert report["wrapper_version"] == "0.6.2"
    assert report["platform_directory"] == "win-x64"
    assert report["payload_size_bytes"] == 270_852
    assert set(report["files"]) == {
        "libtkdnd2.10.1.dll",
        "pkgIndex.tcl",
        "tkdnd.tcl",
        "tkdnd_compat.tcl",
        "tkdnd_generic.tcl",
        "tkdnd_macosx.tcl",
        "tkdnd_unix.tcl",
        "tkdnd_utils.tcl",
        "tkdnd_windows.tcl",
    }
    assert len(report["files"]) == 9
    assert {
        name: details["sha256"] for name, details in report["files"].items()
    } == selftest._TKDND_RUNTIME_HASHES

    requirements = (PROJECT_ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "tkinterdnd2==0.6.2" in requirements.splitlines()

    hooks_root = Path(_pyinstaller_hooks_contrib.__file__).resolve().parent
    hook_path = hooks_root / "stdhooks" / "hook-tkinterdnd2.py"
    hook_text = hook_path.read_text(encoding="utf-8")
    assert "win-x64" in hook_text
    assert '"Windows": "*.dll"' in hook_text
    assert "src_path.glob(lib_suffix)" in hook_text
    assert 'src_path.glob("*.tcl")' in hook_text


def test_tkdnd_native_extension_loads_into_the_pinned_tk_runtime() -> None:
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from pros.selftest import _probe_dnd_runtime; "
                "print(json.dumps(_probe_dnd_runtime(), sort_keys=True))"
            ),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, probe.stderr or probe.stdout
    report = json.loads(probe.stdout)
    assert report["loaded"] is True
    assert report["tkdnd_version"] == "2.10.1"
    assert report["tcl_version"].startswith("8.6")
    assert report["tk_version"].startswith("8.6")


def test_source_selftest_covers_multifile_keep_separate_in_isolation(
    tmp_path: Path,
) -> None:
    probe = subprocess.run(
        [sys.executable, "-m", "pros.selftest", str(tmp_path)],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    report_paths = list(tmp_path.glob("pros-self-test-*/selftest-result.json"))
    assert len(report_paths) == 1, probe.stderr or probe.stdout
    report = json.loads(report_paths[0].read_text(encoding="utf-8"))
    assert probe.returncode == 0, json.dumps(report, indent=2)
    assert report["status"] == "ok"
    assert report["frozen"] is False

    assert set(report["brand_assets"]["files"]) == EXPECTED_ASSET_NAMES
    assert len(report["drag_and_drop"]["files"]) == 9
    assert set(report["jobs"]) == {
        "compression_grayscale",
        "grayscale_only",
        "keep_separate_multi",
    }
    assert Path(report["jobs"]["compression_grayscale"]["output"]).name == (
        "PROS Self Test - Cprs - Grey.pdf"
    )
    assert Path(report["jobs"]["grayscale_only"]["output"]).name == (
        "PROS Self Test - Grey.pdf"
    )

    separate_outputs = report["jobs"]["keep_separate_multi"]["outputs"]
    assert [Path(item["input"]).name for item in separate_outputs] == [
        "PROS Self Test Second.pdf",
        "PROS Self Test First.pdf",
    ]
    assert [Path(item["output"]).name for item in separate_outputs] == [
        "PROS Self Test Second - Cprs.pdf",
        "PROS Self Test First - Cprs.pdf",
    ]
    assert all(item["page_count"] == 1 for item in separate_outputs)
    assert all(item["syntax_warning_count"] == 0 for item in separate_outputs)
    assert all(
        item["input_sha256_before"] == item["input_sha256_after"]
        for item in separate_outputs
    )


def test_v150_packaging_metadata_requires_every_brand_asset() -> None:
    assert __version__ == "1.5.0"

    spec_text = (PROJECT_ROOT / "PROS.spec").read_text(encoding="utf-8")
    for asset_name in EXPECTED_ASSET_NAMES:
        assert f'"{asset_name}"' in spec_text
    assert "icon=str(ICON)" in spec_text

    version_text = (PROJECT_ROOT / "packaging" / "version_info.txt").read_text(
        encoding="utf-8"
    )
    assert "filevers=(1, 5, 0, 0)" in version_text
    assert "prodvers=(1, 5, 0, 0)" in version_text
    assert "StringStruct('FileVersion', '1.5.0')" in version_text
    assert "StringStruct('ProductVersion', '1.5.0')" in version_text

    notices_header = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt").read_text(
        encoding="utf-8"
    )[:500]
    assert "PROS 1.5.0 Windows x64 one-file build" in notices_header
    assert "22 August 2026" in notices_header

    notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt").read_text(
        encoding="utf-8"
    )
    assert "tkinterdnd2 0.6.2 — LICENSE" in notices
    assert "TkDND 2.10.1 — license.terms" in notices
    assert "notice is included verbatim in any distributions" in notices
