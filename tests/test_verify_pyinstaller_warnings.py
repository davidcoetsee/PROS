# SPDX-License-Identifier: MPL-2.0
"""Focused tests for the normalized PyInstaller warning allowlist gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import verify_pyinstaller_warnings as verifier


def _warning_line(kind: str, module: str, importer: str) -> str:
    return f"{kind} module named {module} - imported by {importer} (optional)"


def test_warning_parser_ignores_absolute_importer_paths_and_normalizes_quotes() -> None:
    windows_warning = "\n".join(
        (
            "PyInstaller warning report header",
            _warning_line(
                "missing",
                "'collections.abc'",
                r"C:\Users\first\project\.venv\Lib\site-packages\example.py",
            ),
            _warning_line("excluded", "_frozen_importlib", "importlib"),
            _warning_line("missing", "pwd", "posixpath"),
        )
    )
    other_machine_warning = "\n".join(
        (
            _warning_line(
                "missing",
                "collections.abc",
                "/home/second/project/.venv/lib/python3.13/site-packages/example.py",
            ),
            _warning_line("excluded", "_frozen_importlib", "zipimport"),
            _warning_line("missing", "pwd", "shutil"),
            _warning_line("missing", "pwd", "tarfile"),
        )
    )

    expected = {
        "excluded module named _frozen_importlib",
        "missing module named collections.abc",
        "missing module named pwd",
    }
    assert verifier.parse_warning_text(windows_warning) == expected
    assert verifier.parse_warning_text(other_machine_warning) == expected


def test_check_mode_reports_exact_added_and_removed_sets(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning_file = tmp_path / "warn-PROS.txt"
    allowlist = tmp_path / "allowlist.txt"
    warning_file.write_text(
        "\n".join(
            (
                _warning_line("missing", "kept", "current importer"),
                _warning_line("missing", "new_warning", r"C:\absolute\hook.py"),
            )
        ),
        encoding="utf-8",
    )
    verifier.write_allowlist(
        allowlist,
        {
            "missing module named kept",
            "excluded module named resolved_warning",
        },
    )

    result = verifier.main(
        ["--warning-file", str(warning_file), "--allowlist", str(allowlist)]
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "PyInstaller warning allowlist mismatch" in output
    assert "Added warning entries:" in output
    assert "+ missing module named new_warning" in output
    assert "Removed warning entries:" in output
    assert "- excluded module named resolved_warning" in output
    assert "+ missing module named kept" not in output


def test_update_mode_creates_sorted_allowlist_then_check_mode_passes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning_file = tmp_path / "build" / "warn-PROS.txt"
    allowlist = tmp_path / "packaging" / "pyinstaller-warning-allowlist.txt"
    warning_file.parent.mkdir(parents=True)
    warning_file.write_text(
        "\n".join(
            (
                _warning_line("missing", "zeta", "module_b"),
                _warning_line("excluded", "alpha", "module_a"),
            )
        ),
        encoding="utf-8",
    )

    update_result = verifier.main(
        [
            "--warning-file",
            str(warning_file),
            "--allowlist",
            str(allowlist),
            "--update-allowlist",
        ]
    )

    assert update_result == 0
    assert allowlist.read_text(encoding="utf-8").splitlines() == [
        "# SPDX-License-Identifier: MPL-2.0",
        "# Normalized PyInstaller missing/excluded module warnings.",
        "# Update only after reviewing the warning-set changes.",
        "",
        "excluded module named alpha",
        "missing module named zeta",
    ]
    assert "Added warning entries:" in capsys.readouterr().out

    check_result = verifier.main(
        ["--warnings", str(warning_file), "--allowlist", str(allowlist)]
    )

    assert check_result == 0
    assert "allowlist matches (2 entries)" in capsys.readouterr().out


def test_missing_warning_file_fails_even_in_update_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing_warning_file = tmp_path / "missing-warn-PROS.txt"
    allowlist = tmp_path / "new-allowlist.txt"

    result = verifier.main(
        [
            "--warning-file",
            str(missing_warning_file),
            "--allowlist",
            str(allowlist),
            "--update-allowlist",
        ]
    )

    assert result == 1
    assert not allowlist.exists()
    assert "PyInstaller warning file does not exist" in capsys.readouterr().out


def test_missing_or_malformed_allowlist_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    warning_file = tmp_path / "warn-PROS.txt"
    allowlist = tmp_path / "allowlist.txt"
    warning_file.write_text(
        _warning_line("missing", "expected", "importer"), encoding="utf-8"
    )

    assert verifier.main(
        ["--warning-file", str(warning_file), "--allowlist", str(allowlist)]
    ) == 1
    assert "allowlist does not exist" in capsys.readouterr().out

    allowlist.write_text("missing: not-canonical\n", encoding="utf-8")
    assert verifier.main(
        ["--warning-file", str(warning_file), "--allowlist", str(allowlist)]
    ) == 1
    assert "not a normalized warning entry" in capsys.readouterr().out
