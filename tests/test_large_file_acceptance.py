"""Opt-in acceptance test for the specification's 120 MB requirement."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pikepdf
import pytest

from pros.models import JobRequest, StructureMode
from pros.pdf_engine import process_job


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_large_valid_pdf(path: Path, content_bytes: int) -> None:
    """Stream a valid one-page PDF with a large referenced content stream.

    The content is made of PDF comment lines, so the test exercises qpdf's
    large-stream I/O and compression without allocating a 120+ MB Python
    buffer or committing a giant fixture to source control.
    """

    offsets: list[int] = [0]
    with path.open("wb") as output:
        output.write(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")

        def write_object(number: int, body: bytes) -> None:
            while len(offsets) <= number:
                offsets.append(0)
            offsets[number] = output.tell()
            output.write(f"{number} 0 obj\n".encode("ascii"))
            output.write(body)
            output.write(b"\nendobj\n")

        write_object(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        write_object(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
        write_object(
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << >> /Contents 4 0 R >>",
        )

        while len(offsets) <= 4:
            offsets.append(0)
        offsets[4] = output.tell()
        output.write(b"4 0 obj\n")
        output.write(f"<< /Length {content_bytes} >>\nstream\n".encode("ascii"))
        full_line = b"%" + (b"A" * 8190) + b"\n"
        remaining = content_bytes
        while remaining >= len(full_line):
            output.write(full_line)
            remaining -= len(full_line)
        if remaining >= 2:
            output.write(b"%" + (b"A" * (remaining - 2)) + b"\n")
        elif remaining == 1:
            output.write(b"\n")
        output.write(b"endstream\nendobj\n")

        write_object(5, b"<< /Title (PROS 120 MB acceptance fixture) >>")
        xref_offset = output.tell()
        output.write(b"xref\n0 6\n")
        output.write(b"0000000000 65535 f \n")
        for number in range(1, 6):
            output.write(f"{offsets[number]:010d} 00000 n \n".encode("ascii"))
        output.write(
            b"trailer\n<< /Size 6 /Root 1 0 R /Info 5 0 R >>\nstartxref\n"
        )
        output.write(f"{xref_offset}\n%%EOF\n".encode("ascii"))


@pytest.mark.large
def test_compresses_pdf_larger_than_120_mb_without_touching_source(
    tmp_path: Path,
) -> None:
    if os.environ.get("PROS_RUN_LARGE_TEST") != "1":
        pytest.skip("set PROS_RUN_LARGE_TEST=1 to run the 120 MB acceptance test")

    source = tmp_path / "large source.pdf"
    _write_large_valid_pdf(source, 121 * 1024 * 1024)
    assert source.stat().st_size > 120 * 1024 * 1024
    source_hash = _sha256(source)

    request = JobRequest(
        job_id="large-file-acceptance",
        remove_password=False,
        compress_pdf=True,
        structure_mode=StructureMode.NEITHER,
        input_paths=[source],
        passwords=[None],
        split_points=[],
        output_dir=tmp_path,
        output_base="large result",
        staging_dir=tmp_path / "stage",
    )
    result = process_job(request)

    assert result.success, result.error
    assert _sha256(source) == source_hash
    assert len(result.output_paths) == 1
    assert result.output_paths[0].is_file()
    with pikepdf.Pdf.open(result.output_paths[0]) as output:
        assert len(output.pages) == 1
        assert list(output.check_pdf_syntax()) == []
    assert result.output_size_bytes < result.original_size_bytes
