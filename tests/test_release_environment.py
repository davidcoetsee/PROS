# SPDX-License-Identifier: MPL-2.0
"""Focused regression tests for the fail-closed release environment gate."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import verify_release_environment as verifier

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_PINS = {
    "altgraph": "0.17.5",
    "colorama": "0.4.6",
    "coverage": "7.15.4",
    "iniconfig": "2.3.0",
    "lxml": "6.1.1",
    "packaging": "26.3",
    "pefile": "2024.8.26",
    "pikepdf": "10.11.0",
    "pillow": "12.3.0",
    "pip": "26.2.1",
    "pip-licenses": "5.5.5",
    "pluggy": "1.6.0",
    "prettytable": "3.18.0",
    "psutil": "7.2.2",
    "pygments": "2.21.0",
    "pyinstaller": "6.22.1",
    "pyinstaller-hooks-contrib": "2026.6",
    "pywin32-ctypes": "0.2.3",
    "pytest": "9.1.1",
    "pytest-cov": "7.1.0",
    "ruff": "0.16.3",
    "setuptools": "84.0.0",
    "tkinterdnd2": "0.6.2",
    "wcwidth": "0.8.2",
}


def test_requirement_parser_resolves_every_exact_direct_pin() -> None:
    pins = verifier.load_exact_pins(PROJECT_ROOT)
    assert {
        canonical_name: details["version"] for canonical_name, details in pins.items()
    } == EXPECTED_PINS
    assert set(verifier.EXPECTED_WHEEL_FILENAMES) == set(EXPECTED_PINS)


def test_windows_x64_lock_matches_every_exact_pin_and_wheel() -> None:
    pins = verifier.load_exact_pins(PROJECT_ROOT)
    lock = verifier.load_windows_lock(PROJECT_ROOT, pins)
    assert {
        canonical_name: details["version"] for canonical_name, details in lock.items()
    } == EXPECTED_PINS
    assert {
        canonical_name: details["wheel"] for canonical_name, details in lock.items()
    } == verifier.EXPECTED_WHEEL_FILENAMES
    assert all(len(details["sha256"]) == 64 for details in lock.values())


def test_requirement_parser_rejects_non_exact_entries(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text(
        "example-package>=1.0\n", encoding="utf-8"
    )
    (tmp_path / "requirements-dev.txt").write_text(
        "-r requirements.txt\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="is not an exact name==version pin"):
        verifier.load_exact_pins(tmp_path)


def test_current_release_environment_passes_every_exact_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pins = verifier.load_exact_pins(PROJECT_ROOT)
    approved_distributions = [
        dist
        for dist in verifier.metadata.distributions()
        if verifier._normalize_distribution_name(dist.metadata["Name"]) in pins
    ]
    monkeypatch.setattr(
        verifier.metadata,
        "distributions",
        lambda: approved_distributions,
    )
    report = verifier.verify_release_environment(PROJECT_ROOT)
    assert report["ok"], json.dumps(report, indent=2, sort_keys=True)
    assert all(check["ok"] for check in report["checks"])
    assert set(report["record_audits"]) == set(EXPECTED_PINS)
    assert set(report["wheel_lock"]) == set(EXPECTED_PINS)
    assert all(
        audit["hashed_files_checked"] > 0 for audit in report["record_audits"].values()
    )
    assert all(audit["ok"] for audit in report["record_audits"].values())

    actual_by_name = {check["name"]: check["actual"] for check in report["checks"]}
    assert actual_by_name["python.version"] == "3.13.15"
    assert actual_by_name["openssl.runtime"] == "3.0.21"
    assert actual_by_name["zlib.runtime"] == "1.3.1"
    assert actual_by_name["tcl_tk.runtime"] == {"tcl": "8.6.15", "tk": "8.6.15"}
    assert actual_by_name["pikepdf.runtime"] == "10.11.0"
    assert actual_by_name["qpdf.runtime"] == "12.3.2"
    assert actual_by_name["lxml.runtime"] == "6.1.1"
    assert actual_by_name["libxml2.runtime"] == "2.11.9"
    assert actual_by_name["libxslt.runtime"] == "1.1.45"
    assert actual_by_name["pillow.runtime"] == "12.3.0"
    for feature_name, version in verifier.EXPECTED_PILLOW_NATIVE.items():
        assert actual_by_name[f"pillow.native.{feature_name}"] == {
            "supported": True,
            "version": version,
        }


def test_installed_distribution_audit_rejects_an_extra_package() -> None:
    pins = verifier.load_exact_pins(PROJECT_ROOT)
    installed = [
        SimpleNamespace(metadata={"Name": details["name"]}, version=details["version"])
        for details in pins.values()
    ]
    assert verifier.audit_installed_distribution_set(pins, installed)["ok"]

    installed.append(
        SimpleNamespace(metadata={"Name": "unexpected-package"}, version="9.9")
    )
    audit = verifier.audit_installed_distribution_set(pins, installed)
    assert not audit["ok"]
    assert audit["unexpected"] == ["unexpected-package"]
    assert "unexpected distributions: unexpected-package" in audit["errors"]


@pytest.mark.parametrize(
    ("old", "new", "error"),
    [
        (
            "altgraph==0.17.5",
            "altgraph==0.17.4",
            "does not exactly match requirement pins",
        ),
        (
            "--hash=sha256:f3a22400bce1b0c701683820ac4f3b159cd301acab067c51c653e06961600597",
            (
                "--hash=sha256:"
                "f3a22400bce1b0c701683820ac4f3b159cd301acab067c51c653e06961600597 "
                "--hash=sha256:"
                "0000000000000000000000000000000000000000000000000000000000000000"
            ),
            "must contain exactly one lowercase SHA256 hash",
        ),
        (
            "# altgraph-0.17.5-py2.py3-none-any.whl",
            "# altgraph-0.17.5-py3-none-any.whl",
            "wheel mismatch for altgraph",
        ),
    ],
)
def test_windows_lock_rejects_pin_hash_and_wheel_mismatches(
    tmp_path: Path,
    old: str,
    new: str,
    error: str,
) -> None:
    lock_text = (PROJECT_ROOT / verifier.WINDOWS_LOCK_FILE).read_text(encoding="utf-8")
    assert old in lock_text
    (tmp_path / verifier.WINDOWS_LOCK_FILE).write_text(
        lock_text.replace(old, new, 1), encoding="utf-8"
    )
    pins = verifier.load_exact_pins(PROJECT_ROOT)
    with pytest.raises(ValueError, match=error):
        verifier.load_windows_lock(tmp_path, pins)


def test_record_audit_detects_an_installed_file_change(tmp_path: Path) -> None:
    payload = tmp_path / "demo.bin"
    payload.write_bytes(b"original wheel payload")
    recorded_hash, recorded_size = verifier._hash_file(payload, "sha256")
    entry = SimpleNamespace(
        hash=SimpleNamespace(mode="sha256", value=recorded_hash),
        size=recorded_size,
    )

    class FakeDistribution:
        def __init__(self) -> None:
            self.files = [entry]
            self.metadata = {"Name": "demo"}
            self.version = "1.0"

        @staticmethod
        def locate_file(_entry: object) -> Path:
            return payload

    distribution = FakeDistribution()
    assert verifier.audit_distribution_record(distribution)["ok"]
    payload.write_bytes(b"changed payload")
    audit = verifier.audit_distribution_record(distribution)
    assert not audit["ok"]
    assert any("Hash mismatch" in error for error in audit["errors"])


def test_cli_returns_failure_when_any_check_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed_report = {
        "ok": False,
        "checks": [verifier._exact_check("python.version", "3.13.15", "3.13.14")],
    }
    monkeypatch.setattr(
        verifier,
        "verify_release_environment",
        lambda: failed_report,
    )
    assert verifier.main([]) == 1
    output = capsys.readouterr().out
    assert "FAIL python.version" in output
    assert "Release environment verification FAILED." in output
