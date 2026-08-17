from __future__ import annotations

from pathlib import Path

import pikepdf
import pytest

from pros.models import JobRequest, StructureMode
from pros.naming import build_output_paths, normalize_output_base, suggest_output_base
from pros.validation import (
    calculate_split_ranges,
    preflight,
    validate_output_base,
    validate_split_points,
)


def _request(tmp_path: Path, **overrides: object) -> JobRequest:
    values: dict[str, object] = {
        "job_id": "job-1",
        "remove_password": False,
        "compress_pdf": False,
        "structure_mode": StructureMode.NEITHER,
        "input_paths": [],
        "passwords": [],
        "split_points": [],
        "output_dir": tmp_path,
        "output_base": "Report",
        "staging_dir": tmp_path / "staging",
    }
    values.update(overrides)
    return JobRequest(**values)  # type: ignore[arg-type]


def _blank_pdf(path: Path, pages: int = 3) -> None:
    with pikepdf.Pdf.new() as pdf:
        for _ in range(pages):
            pdf.add_blank_page()
        pdf.save(path)


def test_ranked_naming_matrix(tmp_path: Path) -> None:
    source = tmp_path / "Accounts.pdf"
    request = _request(
        tmp_path,
        remove_password=True,
        compress_pdf=True,
        structure_mode=StructureMode.JOIN,
        input_paths=[source, tmp_path / "Other.pdf"],
        passwords=[None, None],
        output_base="",
    )
    assert normalize_output_base("  Report.PDF  ") == "Report"
    assert suggest_output_base(request) == "Accounts - Join - Pwd_Rmv - Cprs"
    assert build_output_paths(request) == (
        tmp_path / "Accounts - Join - Pwd_Rmv - Cprs.pdf",
    )

    request.structure_mode = StructureMode.SPLIT
    request.input_paths = [tmp_path / "Report.pdf"]
    request.passwords = [None]
    request.split_points = [2, 5]
    request.output_base = "Report"
    assert build_output_paths(request) == (
        tmp_path / "Report - Pwd_Rmv - Cprs - Part 1.pdf",
        tmp_path / "Report - Pwd_Rmv - Cprs - Part 2.pdf",
        tmp_path / "Report - Pwd_Rmv - Cprs - Part 3.pdf",
    )


@pytest.mark.parametrize(
    ("points", "expected_fragment"),
    [
        ([], "At least one"),
        ([0], "positive whole"),
        ([2, 2], "duplicates"),
        ([3, 2], "strictly increasing"),
        ([5], "less than"),
        ([1.5], "positive whole"),
        ([True], "positive whole"),
    ],
)
def test_split_validation_rejects_invalid_values(
    points: list[object], expected_fragment: str
) -> None:
    errors = validate_split_points(5, points)  # type: ignore[arg-type]
    assert any(expected_fragment in error for error in errors)


def test_split_ranges_are_inclusive_and_final_range_is_automatic() -> None:
    assert calculate_split_ranges(104, [65, 73, 102]) == (
        (1, 65),
        (66, 73),
        (74, 102),
        (103, 104),
    )


def test_windows_output_name_validation() -> None:
    assert validate_output_base("Report") == ()
    assert validate_output_base("CON")
    assert validate_output_base("bad:name")
    assert validate_output_base(".")


def test_preflight_rejects_duplicate_canonical_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    request = _request(
        tmp_path,
        structure_mode=StructureMode.JOIN,
        input_paths=[source, source.parent / "." / source.name],
        passwords=[None, None],
        output_base="Joined",
    )
    report = preflight(request)
    assert not report.valid
    assert any("same PDF path" in error for error in report.errors)
    assert any("later-source document metadata" in warning for warning in report.warnings)


def test_preflight_blocks_existing_output_without_touching_it(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    request = _request(
        tmp_path,
        compress_pdf=True,
        input_paths=[source],
        passwords=[None],
        output_base="Result",
    )
    output = build_output_paths(request)[0]
    output.write_bytes(b"keep me")
    report = preflight(request)
    assert not report.valid
    assert any("already exists" in error for error in report.errors)
    assert output.read_bytes() == b"keep me"


def test_preflight_validates_raw_base_before_suffixes(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _blank_pdf(source)
    request = _request(
        tmp_path,
        compress_pdf=True,
        input_paths=[source],
        passwords=[None],
        output_base="Report.",
    )
    report = preflight(request)
    assert not report.valid
    assert any("end with a space or full stop" in error for error in report.errors)
