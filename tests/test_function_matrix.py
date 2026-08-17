from __future__ import annotations

import hashlib
from pathlib import Path

import pikepdf
import pytest

from pros.models import JobRequest, StructureMode
from pros.pdf_engine import process_job
from pros.validation import preflight


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _make_pdf(path: Path, page_count: int, password: str | None = None) -> None:
    with pikepdf.Pdf.new() as pdf:
        for index in range(page_count):
            pdf.add_blank_page(page_size=(200 + index, 300))
        encryption = (
            pikepdf.Encryption(user=password, owner=f"owner-{password}", R=6)
            if password is not None
            else None
        )
        pdf.save(path, encryption=encryption)


def _request(
    folder: Path,
    *,
    remove: bool,
    compress: bool,
    mode: StructureMode,
    inputs: list[Path],
    passwords: list[str | None],
    split_points: list[int] | None = None,
) -> JobRequest:
    return JobRequest(
        job_id=f"matrix-{mode.value}-{int(remove)}-{int(compress)}",
        remove_password=remove,
        compress_pdf=compress,
        structure_mode=mode,
        input_paths=inputs,
        passwords=passwords,
        split_points=split_points or [],
        output_dir=folder,
        output_base="Matrix",
        staging_dir=folder / "stage",
    )


@pytest.mark.parametrize(
    ("remove", "compress", "mode"),
    [
        (True, False, StructureMode.NEITHER),
        (False, True, StructureMode.NEITHER),
        (True, True, StructureMode.NEITHER),
        (False, False, StructureMode.JOIN),
        (True, False, StructureMode.JOIN),
        (False, True, StructureMode.JOIN),
        (True, True, StructureMode.JOIN),
        (False, False, StructureMode.SPLIT),
        (True, False, StructureMode.SPLIT),
        (False, True, StructureMode.SPLIT),
        (True, True, StructureMode.SPLIT),
    ],
)
def test_every_permitted_function_combination(
    tmp_path: Path,
    remove: bool,
    compress: bool,
    mode: StructureMode,
) -> None:
    folder = tmp_path / f"{mode.value}-{int(remove)}-{int(compress)}"
    folder.mkdir()
    first = folder / "first.pdf"
    second = folder / "second.pdf"
    _make_pdf(first, 4)
    inputs = [first]
    passwords: list[str | None] = [None]
    split_points: list[int] = []
    expected_outputs = 1
    expected_pages = [4]
    if mode is StructureMode.JOIN:
        _make_pdf(second, 2)
        inputs.append(second)
        passwords.append(None)
        expected_pages = [6]
    elif mode is StructureMode.SPLIT:
        split_points = [1, 3]
        expected_outputs = 3
        expected_pages = [1, 2, 1]

    before = {path: _sha256(path) for path in inputs}
    result = process_job(
        _request(
            folder,
            remove=remove,
            compress=compress,
            mode=mode,
            inputs=inputs,
            passwords=passwords,
            split_points=split_points,
        )
    )

    assert result.success, result.error
    assert len(result.output_paths) == expected_outputs
    assert {path: _sha256(path) for path in inputs} == before
    for output_path, page_count in zip(result.output_paths, expected_pages, strict=True):
        with pikepdf.Pdf.open(output_path) as output:
            assert len(output.pages) == page_count
            assert not output.is_encrypted


def test_mixed_protected_inputs_with_distinct_effective_passwords(tmp_path: Path) -> None:
    unprotected = tmp_path / "plain.pdf"
    common = tmp_path / "common.pdf"
    override = tmp_path / "override.pdf"
    _make_pdf(unprotected, 1)
    _make_pdf(common, 1, "shared-secret")
    _make_pdf(override, 1, "different-secret")
    request = _request(
        tmp_path,
        remove=True,
        compress=False,
        mode=StructureMode.JOIN,
        inputs=[unprotected, common, override],
        passwords=[None, "shared-secret", "different-secret"],
    )

    result = process_job(request)

    assert result.success, result.error
    with pikepdf.Pdf.open(result.output_paths[0]) as output:
        assert len(output.pages) == 3
        assert not output.is_encrypted


def test_wrong_password_blocks_job_before_output(tmp_path: Path) -> None:
    source = tmp_path / "protected.pdf"
    _make_pdf(source, 1, "correct")
    request = _request(
        tmp_path,
        remove=True,
        compress=False,
        mode=StructureMode.NEITHER,
        inputs=[source],
        passwords=["wrong"],
    )

    report = preflight(request)
    result = process_job(request)

    assert not report.valid
    assert any("password" in error.casefold() for error in report.errors)
    assert not result.success
    assert not result.output_paths
    assert list(tmp_path.glob("Matrix*.pdf")) == []


def test_join_twelve_inputs_and_split_twelve_outputs(tmp_path: Path) -> None:
    join_folder = tmp_path / "join"
    join_folder.mkdir()
    join_inputs: list[Path] = []
    for index in range(12):
        path = join_folder / f"input-{index + 1}.pdf"
        _make_pdf(path, 1)
        join_inputs.append(path)
    joined = process_job(
        _request(
            join_folder,
            remove=False,
            compress=False,
            mode=StructureMode.JOIN,
            inputs=join_inputs,
            passwords=[None] * 12,
        )
    )
    assert joined.success, joined.error
    with pikepdf.Pdf.open(joined.output_paths[0]) as output:
        assert len(output.pages) == 12

    split_folder = tmp_path / "split"
    split_folder.mkdir()
    source = split_folder / "source.pdf"
    _make_pdf(source, 12)
    split = process_job(
        _request(
            split_folder,
            remove=False,
            compress=False,
            mode=StructureMode.SPLIT,
            inputs=[source],
            passwords=[None],
            split_points=list(range(1, 12)),
        )
    )
    assert split.success, split.error
    assert len(split.output_paths) == 12
    for output_path in split.output_paths:
        with pikepdf.Pdf.open(output_path) as output:
            assert len(output.pages) == 1


def test_no_function_is_prohibited(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, 1)
    request = _request(
        tmp_path,
        remove=False,
        compress=False,
        mode=StructureMode.NEITHER,
        inputs=[source],
        passwords=[None],
    )
    report = preflight(request)
    assert not report.valid
    assert any("at least one" in error.casefold() for error in report.errors)
