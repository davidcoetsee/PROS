# SPDX-License-Identifier: MPL-2.0
"""Release metadata and canonical brand asset regression checks."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import _pyinstaller_hooks_contrib
import lxml
from PyInstaller.utils.hooks import collect_submodules

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


def test_source_legal_documents_match_release_version() -> None:
    report = selftest._inspect_legal_documents()
    assert Path(report["directory"]) == PROJECT_ROOT
    assert set(report["files"]) == {
        "LICENSE",
        "SOURCE_CODE.txt",
        "TRADEMARKS.md",
        "ASSET_LICENSES.md",
        "THIRD_PARTY_NOTICES.txt",
    }
    assert report["source"].endswith(f"/tree/v{__version__}")
    assert all(item["size_bytes"] > 0 for item in report["files"].values())


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


def test_pyinstaller_excludes_only_unused_lxml_isoschematron_payload() -> None:
    spec_text = (PROJECT_ROOT / "PROS.spec").read_text(encoding="utf-8")
    spec_tree = ast.parse(spec_text)
    analysis_calls = [
        node
        for node in ast.walk(spec_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    ]
    assert len(analysis_calls) == 1
    exclude_keywords = [
        keyword.value
        for keyword in analysis_calls[0].keywords
        if keyword.arg == "excludes"
    ]
    assert len(exclude_keywords) == 1
    assert ast.literal_eval(exclude_keywords[0]) == ["lxml.isoschematron"]

    hooks_root = Path(_pyinstaller_hooks_contrib.__file__).resolve().parent
    lxml_hook = (hooks_root / "stdhooks" / "hook-lxml.py").read_text(
        encoding="utf-8"
    )
    isoschematron_hook = (
        hooks_root / "stdhooks" / "hook-lxml.isoschematron.py"
    ).read_text(encoding="utf-8")
    assert "collect_submodules('lxml')" in lxml_hook
    assert "collect_data_files('lxml'" in isoschematron_hook
    assert "isoschematron', 'resources'" in isoschematron_hook
    assert [
        module
        for module in collect_submodules("lxml")
        if module == "lxml.isoschematron"
        or module.startswith("lxml.isoschematron.")
    ] == ["lxml.isoschematron"]

    isoschematron_xsl = (
        Path(lxml.__file__).resolve().parent
        / "isoschematron"
        / "resources"
        / "xsl"
    )
    assert (isoschematron_xsl / "RNG2Schtrn.xsl").is_file()
    assert (isoschematron_xsl / "XSD2Schtrn.xsl").is_file()

    import_probe = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import lxml.etree; "
                "import pikepdf; "
                "import pros.pdf_engine; "
                "import pros.gui; "
                "import pros.selftest; "
                "assert 'lxml.isoschematron' not in sys.modules; "
                "print('.'.join(map(str, lxml.etree.LXML_VERSION)))"
            ),
        ],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert import_probe.returncode == 0, import_probe.stderr or import_probe.stdout
    assert import_probe.stdout.strip() == "6.1.1.0"


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
    assert report["pros"] == __version__
    assert report["build_info"] == {
        "embedded": False,
        "pros_version": __version__,
        "git_commit": None,
    }
    assert report["runtime_inventory"]["openssl"] == (
        "OpenSSL 3.0.21 9 Jun 2026"
    )
    assert report["runtime_inventory"]["zlib"] == "1.3.1"
    assert report["runtime_inventory"]["lxml"] == "6.1.1"
    assert report["runtime_inventory"]["pillow"] == "12.3.0"

    assert set(report["brand_assets"]["files"]) == EXPECTED_ASSET_NAMES
    assert set(report["legal_documents"]["files"]) == {
        "LICENSE",
        "SOURCE_CODE.txt",
        "TRADEMARKS.md",
        "ASSET_LICENSES.md",
        "THIRD_PARTY_NOTICES.txt",
    }
    assert len(report["drag_and_drop"]["files"]) == 9
    assert report["drag_and_drop"]["image_tk_loaded"] is True
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


def test_first_party_code_and_build_files_declare_mpl_2_0() -> None:
    source_paths = [
        PROJECT_ROOT / "main.py",
        PROJECT_ROOT / "hook-pikepdf.py",
        PROJECT_ROOT / "PROS.spec",
        PROJECT_ROOT / "build.ps1",
        PROJECT_ROOT / "prepare_release.ps1",
        PROJECT_ROOT / "verify_release_artifact.ps1",
        PROJECT_ROOT / "requirements.txt",
        PROJECT_ROOT / "requirements-dev.txt",
        PROJECT_ROOT / "requirements-windows-x64.lock",
        PROJECT_ROOT / "packaging" / "version_info.txt",
        PROJECT_ROOT / ".github" / "workflows" / "windows-tests.yml",
        *sorted((PROJECT_ROOT / "pros").glob("*.py")),
        *sorted((PROJECT_ROOT / "tests").glob("*.py")),
        *sorted((PROJECT_ROOT / "tools").glob("*.py")),
    ]
    assert source_paths
    for path in source_paths:
        assert path.is_file(), path
        header = path.read_text(encoding="utf-8")[:500]
        assert "SPDX-License-Identifier: MPL-2.0" in header, path


def test_release_scripts_bind_exe_environment_source_commit_and_tag() -> None:
    build_text = (PROJECT_ROOT / "build.ps1").read_text(encoding="utf-8")
    assert '".release-venv"' in build_text
    assert '"--require-hashes"' in build_text
    assert '"--only-binary=:all:"' in build_text
    assert "git -C $ProjectRoot" in build_text
    assert "BUILD_INFO.json" in build_text
    assert "tools\\verify_release_environment.py" in build_text
    assert "tools\\verify_pyinstaller_warnings.py" in build_text
    assert "tools\\verify_frozen_archive.py" in build_text
    assert "verify_release_artifact.ps1" in build_text
    assert "-ExpectedCommit $BuildCommit" in build_text
    assert "SkipTests" not in build_text
    assert "SkipSelfTest" not in build_text
    assert "SkipInstall" not in build_text

    prepare_text = (PROJECT_ROOT / "prepare_release.ps1").read_text(
        encoding="utf-8"
    )
    assert "git -C $ProjectRoot" in prepare_text
    assert "rev-parse --verify $TagReference" in prepare_text
    assert "ls-remote --exit-code --tags origin" in prepare_text
    assert "$RemoteCommit -ne $InitialState.HeadCommit" in prepare_text
    assert "https://github.com/davidcoetsee/PROS/tree/$TagName" in prepare_text
    assert "& $BuildScript" in prepare_text
    assert "-ExpectedCommit $FinalState.HeadCommit" in prepare_text
    assert '"--output=$SourceArchive"' in prepare_text
    assert '".staging-$TagName-' in prepare_text
    assert "Third-party source archive hash mismatch" in prepare_text
    assert "SkipBuild" not in prepare_text
    assert "SkipThirdPartySources" not in prepare_text

    artifact_text = (PROJECT_ROOT / "verify_release_artifact.ps1").read_text(
        encoding="utf-8"
    )
    assert "tools\\verify_frozen_archive.py" in artifact_text
    assert "FileVersion -ne $ExpectedVersion" in artifact_text
    assert "ProductVersion -ne $ExpectedVersion" in artifact_text
    assert "WaitForExit(180000)" in artifact_text
    assert "build_info.git_commit -ne $ExpectedCommit" in artifact_text
    assert "runtime_inventory.pillow_native" in artifact_text
    assert 'pros = $ExpectedVersion' in artifact_text

    workflow_text = (
        PROJECT_ROOT / ".github" / "workflows" / "windows-tests.yml"
    ).read_text(encoding="utf-8")
    assert 'python-version: "3.13.15"' in workflow_text
    assert "run: .\\build.ps1" in workflow_text


def test_v151_packaging_metadata_requires_every_brand_and_legal_asset() -> None:
    assert __version__ == "1.5.1"

    spec_text = (PROJECT_ROOT / "PROS.spec").read_text(encoding="utf-8")
    for asset_name in EXPECTED_ASSET_NAMES:
        assert f'"{asset_name}"' in spec_text
    for document_name in (
        "LICENSE",
        "SOURCE_CODE.txt",
        "TRADEMARKS.md",
        "ASSET_LICENSES.md",
        "THIRD_PARTY_NOTICES.txt",
    ):
        assert f'"{document_name}"' in spec_text
    assert "icon=str(ICON)" in spec_text
    assert '"BUILD_INFO.json"' in spec_text

    version_text = (PROJECT_ROOT / "packaging" / "version_info.txt").read_text(
        encoding="utf-8"
    )
    assert "filevers=(1, 5, 1, 0)" in version_text
    assert "prodvers=(1, 5, 1, 0)" in version_text
    assert "StringStruct('FileVersion', '1.5.1')" in version_text
    assert "StringStruct('ProductVersion', '1.5.1')" in version_text

    notices_header = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt").read_text(
        encoding="utf-8"
    )[:500]
    assert "PROS 1.5.1 Windows x64 one-file build" in notices_header
    assert "23 August 2026" in notices_header

    notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.txt").read_text(
        encoding="utf-8"
    )
    assert "tkinterdnd2 0.6.2 — LICENSE" in notices
    assert "TkDND 2.10.1 — license.terms" in notices
    assert "notice is included verbatim in any distributions" in notices
    assert "requirements-windows-x64.lock" in notices

    source_notice = (PROJECT_ROOT / "SOURCE_CODE.txt").read_text(encoding="utf-8")
    assert "https://github.com/davidcoetsee/PROS/tree/v1.5.1" in source_notice

    asset_licences = (PROJECT_ROOT / "ASSET_LICENSES.md").read_text(
        encoding="utf-8"
    )
    for asset_name in EXPECTED_ASSET_NAMES:
        assert f"assets/{asset_name}" in asset_licences
