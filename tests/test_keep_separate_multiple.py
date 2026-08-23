# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import pikepdf
import pytest

import pros.pdf_engine as engine
from pros.models import JobRequest, StructureMode
from pros.naming import build_output_paths, suggest_output_base, suggest_output_bases
from pros.pdf_engine import process_job
from pros.validation import MAX_SEPARATE_INPUTS, preflight


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_pdf(
    path: Path,
    widths: list[int],
    *,
    password: str | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with pikepdf.Pdf.new() as pdf:
        for width in widths:
            page = pdf.add_blank_page(page_size=(width, 300))
            page.Contents = pdf.make_stream(
                b"1 0 0 rg 20 20 80 40 re f\n0 0 1 RG 10 10 100 100 re S\n"
            )
        pdf.docinfo["/Title"] = path.stem
        encryption = (
            pikepdf.Encryption(
                owner=f"owner-{password}",
                user=password,
                R=6,
                aes=True,
            )
            if password is not None
            else None
        )
        pdf.save(path, encryption=encryption, fix_metadata_version=False)


def _request(
    output_dir: Path,
    inputs: list[Path],
    *,
    passwords: list[str | None] | None = None,
    remove: bool = False,
    compress: bool = True,
    gray: bool = False,
    base: str = "User edited base",
) -> JobRequest:
    output_dir.mkdir(parents=True, exist_ok=True)
    return JobRequest(
        job_id="keep-separate-multiple",
        remove_password=remove,
        compress_pdf=compress,
        convert_to_grayscale=gray,
        structure_mode=StructureMode.NEITHER,
        input_paths=inputs,
        passwords=passwords if passwords is not None else [None] * len(inputs),
        split_points=[],
        output_dir=output_dir,
        output_base=base,
        staging_dir=output_dir / "staging",
    )


def test_multi_file_naming_uses_each_input_stem_and_ignores_global_base(
    tmp_path: Path,
) -> None:
    first = tmp_path / "input-a" / "Bank statements.pdf"
    second = tmp_path / "input-b" / "Insurance policy.pdf"
    _make_pdf(first, [101])
    _make_pdf(second, [201])
    output_dir = tmp_path / "output"
    request = _request(
        output_dir,
        [first, second],
        remove=True,
        compress=True,
        gray=True,
        base="bad:name is ignored",
    )

    expected_stems = (
        "Bank statements - Pwd_Rmv - Cprs - Grey",
        "Insurance policy - Pwd_Rmv - Cprs - Grey",
    )
    assert suggest_output_base(request) == expected_stems[0]
    assert suggest_output_bases(request) == expected_stems
    assert build_output_paths(request) == tuple(
        output_dir / f"{stem}.pdf" for stem in expected_stems
    )
    assert preflight(request).valid


def test_single_file_keep_separate_preserves_user_edited_base(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, [100])
    output_dir = tmp_path / "output"
    request = _request(output_dir, [source], base="My edited result")

    assert build_output_paths(request) == (output_dir / "My edited result - Cprs.pdf",)


def test_processes_two_files_independently_in_order_with_effective_passwords(
    tmp_path: Path,
) -> None:
    first = tmp_path / "sources-a" / "First.pdf"
    second = tmp_path / "sources-b" / "Second.pdf"
    _make_pdf(first, [101, 102], password="first-secret")
    _make_pdf(second, [201], password="second-secret")
    source_hashes = {first: _hash(first), second: _hash(second)}
    output_dir = tmp_path / "output"
    request = _request(
        output_dir,
        [first, second],
        passwords=["first-secret", "second-secret"],
        remove=True,
        compress=True,
        gray=True,
    )
    events: list[dict[str, object]] = []

    result = process_job(request, progress=events.append)

    assert result.success, result.error
    assert result.output_paths == (
        output_dir / "First - Pwd_Rmv - Cprs - Grey.pdf",
        output_dir / "Second - Pwd_Rmv - Cprs - Grey.pdf",
    )
    assert {first: _hash(first), second: _hash(second)} == source_hashes
    assert not request.staging_dir.exists()
    for output, widths in zip(result.output_paths, ([101, 102], [201]), strict=True):
        with pikepdf.Pdf.open(output) as pdf:
            assert not pdf.is_encrypted
            assert [int(page.mediabox[2]) for page in pdf.pages] == widths
            operators = [
                str(operator)
                for page in pdf.pages
                for _, operator in pikepdf.parse_content_stream(page)
            ]
            assert "rg" not in operators
            assert "g" in operators

    process_events = [event for event in events if event["stage"] == "process"]
    assert [
        (event["file_index"], event["phase_percent"]) for event in process_events
    ] == [
        (1, 0),
        (1, 100),
        (2, 0),
        (2, 100),
    ]
    assert all(event["file_count"] == 2 for event in process_events)
    assert Path(str(process_events[0]["path"])) == first
    assert Path(str(process_events[2]["path"])) == second


@pytest.mark.parametrize(
    ("remove", "compress", "gray", "suffix"),
    [
        (True, True, False, "Pwd_Rmv - Cprs"),
        (True, False, True, "Pwd_Rmv - Grey"),
        (False, True, True, "Cprs - Grey"),
    ],
)
def test_multi_file_password_compression_and_grayscale_combinations(
    tmp_path: Path,
    remove: bool,
    compress: bool,
    gray: bool,
    suffix: str,
) -> None:
    folder = tmp_path / suffix.replace(" ", "-")
    password = "effective-secret" if remove else None
    first = folder / "inputs" / "First.pdf"
    second = folder / "inputs" / "Second.pdf"
    _make_pdf(first, [100], password=password)
    _make_pdf(second, [200], password=password)
    output_dir = folder / "output"
    request = _request(
        output_dir,
        [first, second],
        passwords=[password, password],
        remove=remove,
        compress=compress,
        gray=gray,
    )

    result = process_job(request)

    assert result.success, result.error
    assert [path.name for path in result.output_paths] == [
        f"First - {suffix}.pdf",
        f"Second - {suffix}.pdf",
    ]
    for path, width in zip(result.output_paths, (100, 200), strict=True):
        with pikepdf.Pdf.open(path) as pdf:
            assert not pdf.is_encrypted
            assert [int(page.mediabox[2]) for page in pdf.pages] == [width]


def test_preflight_rejects_case_insensitive_duplicate_output_stems(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "Report.pdf"
    second = tmp_path / "two" / "report.pdf"
    _make_pdf(first, [100])
    _make_pdf(second, [200])
    request = _request(tmp_path / "output", [first, second])

    report = preflight(request)
    estimate_report = preflight(request, for_estimate=True)

    assert not report.valid
    assert not estimate_report.valid
    assert any("Duplicate output path" in error for error in report.errors)
    assert any("Duplicate output path" in error for error in estimate_report.errors)
    assert report.output_paths == (
        tmp_path / "output" / "Report - Cprs.pdf",
        tmp_path / "output" / "report - Cprs.pdf",
    )


def test_preflight_checks_every_existing_output_without_touching_it(
    tmp_path: Path,
) -> None:
    first = tmp_path / "one" / "First.pdf"
    second = tmp_path / "two" / "Second.pdf"
    _make_pdf(first, [100])
    _make_pdf(second, [200])
    output_dir = tmp_path / "output"
    request = _request(output_dir, [first, second])
    existing = output_dir / "Second - Cprs.pdf"
    existing.write_bytes(b"keep me")

    report = preflight(request)

    assert not report.valid
    assert any(existing.name in error for error in report.errors)
    assert existing.read_bytes() == b"keep me"


def test_preflight_rejects_an_output_that_would_overwrite_any_input(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    first = output_dir / "Alpha - Cprs.pdf"
    second = tmp_path / "other" / "Alpha.pdf"
    _make_pdf(first, [100])
    _make_pdf(second, [200])
    source_hashes = {first: _hash(first), second: _hash(second)}
    request = _request(output_dir, [first, second])

    report = preflight(request)
    result = process_job(request)

    assert not report.valid
    assert any("may not equal an input path" in error for error in report.errors)
    assert not result.success
    assert not result.output_paths
    assert {first: _hash(first), second: _hash(second)} == source_hashes


def test_keep_separate_uses_same_twelve_file_ceiling_as_join(tmp_path: Path) -> None:
    inputs: list[Path] = []
    for index in range(MAX_SEPARATE_INPUTS + 1):
        path = tmp_path / "inputs" / f"input-{index + 1}.pdf"
        _make_pdf(path, [100 + index])
        inputs.append(path)

    accepted = preflight(_request(tmp_path / "accepted", inputs[:-1]))
    rejected = preflight(_request(tmp_path / "rejected", inputs))

    assert accepted.valid, accepted.errors
    assert len(accepted.output_paths) == MAX_SEPARATE_INPUTS
    assert not rejected.valid
    assert any("between 1 and 12" in error for error in rejected.errors)


def test_cancellation_after_first_file_removes_all_staging_and_outputs(
    tmp_path: Path,
) -> None:
    first = tmp_path / "inputs" / "First.pdf"
    second = tmp_path / "inputs" / "Second.pdf"
    _make_pdf(first, [100])
    _make_pdf(second, [200])
    source_hashes = {first: _hash(first), second: _hash(second)}
    output_dir = tmp_path / "output"
    request = _request(
        output_dir,
        [first, second],
        compress=False,
        gray=True,
    )
    cancel_event = threading.Event()

    def cancel_after_first(event: dict[str, object]) -> None:
        if (
            event["stage"] == "process"
            and event.get("file_index") == 1
            and event.get("phase_percent") == 100
        ):
            cancel_event.set()

    result = process_job(
        request,
        progress=cancel_after_first,
        cancel_event=cancel_event,
    )

    assert not result.success
    assert result.cancelled
    assert not any(path.exists() for path in build_output_paths(request))
    assert not request.staging_dir.exists()
    assert {first: _hash(first), second: _hash(second)} == source_hashes


def test_second_file_processing_failure_removes_all_staging_and_outputs(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "inputs" / "First.pdf"
    second = tmp_path / "inputs" / "Second.pdf"
    _make_pdf(first, [100])
    _make_pdf(second, [200])
    source_hashes = {first: _hash(first), second: _hash(second)}
    output_dir = tmp_path / "output"
    request = _request(
        output_dir,
        [first, second],
        compress=False,
        gray=True,
    )
    original_grayscale = engine._grayscale_one

    def fail_second(*args, **kwargs):
        if args[3] == 1:
            raise RuntimeError("simulated second-file failure")
        return original_grayscale(*args, **kwargs)

    monkeypatch.setattr(engine, "_grayscale_one", fail_second)

    result = process_job(request)

    assert not result.success
    assert not result.cancelled
    assert "simulated second-file failure" in (result.error or "")
    assert not any(path.exists() for path in build_output_paths(request))
    assert not request.staging_dir.exists()
    assert {first: _hash(first), second: _hash(second)} == source_hashes


def test_partial_commit_failure_rolls_back_every_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = tmp_path / "inputs" / "First.pdf"
    second = tmp_path / "inputs" / "Second.pdf"
    _make_pdf(first, [100])
    _make_pdf(second, [200])
    output_dir = tmp_path / "output"
    request = _request(
        output_dir,
        [first, second],
        compress=False,
        gray=True,
    )
    original_rename = engine._rename_no_replace
    calls = 0

    def fail_second_rename(source: Path, destination: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated commit failure")
        original_rename(source, destination)

    monkeypatch.setattr(engine, "_rename_no_replace", fail_second_rename)

    result = process_job(request)

    assert not result.success
    assert "simulated commit failure" in (result.error or "")
    assert not any(path.exists() for path in build_output_paths(request))
    assert list(output_dir.glob(".*.partial")) == []
    assert not request.staging_dir.exists()
