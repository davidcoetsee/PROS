# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import hashlib
import queue
from io import BytesIO
from pathlib import Path

import pikepdf
from PIL import Image

import pros.pdf_engine as engine
from pros.models import CompressionLevel, JobRequest, PdfInfo, StructureMode
from pros.pdf_engine import estimate_job, process_job
from pros.validation import LARGE_FILE_ACCEPTANCE_BYTES, preflight
from pros.worker import run_estimate_worker


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_image_pdf(path: Path, *, width: int = 1800, height: int = 2400) -> None:
    red = Image.linear_gradient("L").resize((width, height))
    green = red.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    blue = Image.effect_noise((width, height), 40)
    image = Image.merge("RGB", (red, green, blue))
    encoded = BytesIO()
    image.save(encoded, format="JPEG", quality=96, optimize=True)
    image.close()
    red.close()
    green.close()
    blue.close()

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        image_stream = pdf.make_stream(
            encoded.getvalue(),
            Type=pikepdf.Name("/XObject"),
            Subtype=pikepdf.Name("/Image"),
            Width=width,
            Height=height,
            ColorSpace=pikepdf.Name("/DeviceRGB"),
            BitsPerComponent=8,
            Filter=pikepdf.Name("/DCTDecode"),
        )
        page.obj["/Resources"] = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im0=image_stream)
        )
        page.Contents = pdf.make_stream(
            b"q 612 0 0 792 0 0 cm /Im0 Do Q\n1 0 0 rg 24 24 120 60 re f\n"
        )
        pdf.save(path, fix_metadata_version=False)


def _request(
    tmp_path: Path,
    source: Path,
    *,
    base: str,
    level: CompressionLevel = CompressionLevel.ULTRA,
    grayscale: bool = False,
    compress: bool = True,
) -> JobRequest:
    return JobRequest(
        job_id=f"revision-{base}",
        remove_password=False,
        compress_pdf=compress,
        structure_mode=StructureMode.NEITHER,
        input_paths=[source],
        passwords=[None],
        split_points=[],
        output_dir=tmp_path,
        output_base=base,
        staging_dir=tmp_path / f"stage-{base}",
        compression_level=level,
        convert_to_grayscale=grayscale,
    )


def test_ultra_is_default_and_legacy_level_field_remains_accepted(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_image_pdf(source, width=300, height=400)
    request = JobRequest(
        job_id="legacy-construction",
        remove_password=False,
        compress_pdf=True,
        structure_mode=StructureMode.NEITHER,
        input_paths=[source],
        passwords=[None],
        split_points=[],
        output_dir=tmp_path,
        output_base="Legacy",
        staging_dir=tmp_path / "stage",
    )
    assert request.compression_level is CompressionLevel.ULTRA
    assert request.convert_to_grayscale is False


def test_ultra_downsamples_and_grayscale_converts_images_and_common_vectors(
    tmp_path: Path,
) -> None:
    source = tmp_path / "colour source.pdf"
    _make_image_pdf(source)
    source_hash = _hash(source)
    events: list[dict[str, object]] = []
    request = _request(
        tmp_path,
        source,
        base="Ultra Gray",
        level=CompressionLevel.ULTRA,
        grayscale=True,
    )
    result = process_job(request, progress=events.append)
    assert result.success, result.error
    assert _hash(source) == source_hash

    with pikepdf.Pdf.open(result.output_paths[0]) as output:
        image_obj = next(iter(output.pages[0].get_images().values()))
        assert str(image_obj["/ColorSpace"]) == "/DeviceGray"
        assert int(image_obj["/Width"]) <= 1275
        assert int(image_obj["/Height"]) <= 1650
        operators = [str(operator) for _, operator in pikepdf.parse_content_stream(output.pages[0])]
        assert "rg" not in operators
        assert "g" in operators

    compression_events = [event for event in events if event["stage"] == "compress"]
    phases = [int(event["phase_percent"]) for event in compression_events]
    assert phases[0] == 0
    assert phases[-1] == 100
    assert len(set(phases)) >= 6
    assert phases == sorted(phases)
    assert all(phase % 2 == 0 for phase in phases)
    assert all("00-baseline.pdf" not in str(event["message"]) for event in events)
    assert all(event.get("file_index") == 1 for event in compression_events)


def test_legacy_standard_selection_uses_the_same_ultra_profile(tmp_path: Path) -> None:
    source = tmp_path / "scan.pdf"
    _make_image_pdf(source)
    standard = process_job(
        _request(tmp_path, source, base="Standard", level=CompressionLevel.STANDARD)
    )
    ultra = process_job(
        _request(tmp_path, source, base="Ultra", level=CompressionLevel.ULTRA)
    )
    assert standard.success, standard.error
    assert ultra.success, ultra.error
    with pikepdf.Pdf.open(standard.output_paths[0]) as standard_pdf:
        standard_image = next(iter(standard_pdf.pages[0].get_images().values()))
        standard_pixels = int(standard_image["/Width"]) * int(standard_image["/Height"])
    with pikepdf.Pdf.open(ultra.output_paths[0]) as ultra_pdf:
        ultra_image = next(iter(ultra_pdf.pages[0].get_images().values()))
        ultra_pixels = int(ultra_image["/Width"]) * int(ultra_image["/Height"])
    assert ultra_pixels == standard_pixels
    assert ultra_pixels < 1800 * 2400


def test_grayscale_never_falls_back_to_a_smaller_colour_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"small-colour-source")
    baseline = tmp_path / "baseline.pdf"
    baseline.write_bytes(b"larger-grayscale-candidate" * 20)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    request = _request(tmp_path, source, base="Gray", grayscale=True)

    def fake_compress(*args, **kwargs):
        return baseline

    monkeypatch.setattr(engine, "_compress_one", fake_compress)
    ready = engine._prepare_ready_outputs(
        request,
        [baseline],
        [tmp_path / "Gray - Cprs.pdf"],
        work_dir,
        [],
        None,
        None,
    )
    assert ready[0].read_bytes() == baseline.read_bytes()
    assert ready[0].read_bytes() != source.read_bytes()


def test_grayscale_only_is_valid_converts_color_and_does_not_downsample(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _make_image_pdf(source)
    request = _request(tmp_path, source, base="Gray Only", grayscale=True, compress=False)
    report = preflight(request)
    assert report.valid, report.errors
    events: list[dict[str, object]] = []
    result = process_job(request, progress=events.append)
    assert result.success, result.error
    assert result.output_paths == (tmp_path / "Gray Only - Grey.pdf",)
    with pikepdf.Pdf.open(result.output_paths[0]) as output:
        image_obj = next(iter(output.pages[0].get_images().values()))
        assert str(image_obj["/ColorSpace"]) == "/DeviceGray"
        assert int(image_obj["/Width"]) == 1800
        assert int(image_obj["/Height"]) == 2400
    grayscale_events = [event for event in events if event["stage"] == "grayscale"]
    assert grayscale_events
    assert any("Converting" in str(event["message"]) for event in grayscale_events)
    assert all("Compressing" not in str(event["message"]) for event in grayscale_events)
    assert all("[" not in str(event["message"]) for event in grayscale_events)


def test_files_over_180_mib_remain_accepted_with_large_file_warning(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "large.pdf"
    _make_image_pdf(source, width=300, height=400)
    reported_size = LARGE_FILE_ACCEPTANCE_BYTES + 1024 * 1024

    def fake_inspect(path: Path, password: str | None = None) -> PdfInfo:
        return PdfInfo(
            path=Path(path),
            size_bytes=reported_size,
            page_count=1,
            encrypted=False,
        )

    monkeypatch.setattr(engine, "inspect_pdf", fake_inspect)
    report = preflight(_request(tmp_path, source, base="Large"))
    assert report.valid, report.errors
    assert any("larger than 120 MB" in warning for warning in report.warnings)
    assert not any("180 MB" in error for error in report.errors)


def test_estimate_api_and_worker_result_schema(tmp_path: Path) -> None:
    source = tmp_path / "estimate.pdf"
    _make_image_pdf(source, width=900, height=1200)
    request = _request(
        tmp_path,
        source,
        base="Estimate",
        level=CompressionLevel.ULTRA,
        grayscale=True,
    )
    events: list[dict[str, object]] = []
    estimate = estimate_job(request, progress=events.append)
    assert estimate.success, estimate.error
    assert estimate.estimated_seconds is not None and estimate.estimated_seconds > 0
    assert estimate.estimated_output_bytes is not None and estimate.estimated_output_bytes > 0
    assert estimate.input_size_bytes == source.stat().st_size
    assert estimate.confidence in {"low", "medium"}
    assert events[-1]["kind"] == "estimate_complete"
    assert not (tmp_path / "Estimate - Cprs.pdf").exists()

    messages: queue.SimpleQueue[dict[str, object]] = queue.SimpleQueue()
    worker_result = run_estimate_worker(request, messages, None)
    assert worker_result.success
    drained: list[dict[str, object]] = []
    while not messages.empty():
        drained.append(messages.get())
    assert drained[-1]["kind"] == "estimate_result"
    assert drained[-1]["result"] == worker_result


def test_estimate_ignores_existing_output_and_does_not_overwrite_it(tmp_path: Path) -> None:
    source = tmp_path / "estimate collision.pdf"
    _make_image_pdf(source, width=400, height=500)
    request = _request(tmp_path, source, base="Existing")
    existing = tmp_path / "Existing - Cprs.pdf"
    existing.write_bytes(b"keep me")
    result = estimate_job(request)
    assert result.success, result.error
    assert existing.read_bytes() == b"keep me"


def test_grayscale_marks_transparency_resources_for_review() -> None:
    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(200, 200))
        page.obj["/Resources"] = pikepdf.Dictionary(
            ExtGState=pikepdf.Dictionary(
                GS1=pikepdf.Dictionary(
                    Type=pikepdf.Name("/ExtGState"),
                    BM=pikepdf.Name("/Multiply"),
                    ca=0.5,
                )
            )
        )
        page.Contents = pdf.make_stream(b"/GS1 gs 1 0 0 rg 0 0 100 100 re f\n")
        assert engine._convert_common_content_to_grayscale(pdf) is True
