"""Deterministic, non-GUI health check for source and frozen PROS builds."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import traceback
from io import BytesIO
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image

from pros.models import CompressionLevel, JobRequest, StructureMode
from pros.pdf_engine import process_job

_INPUT_IMAGE_WIDTH = 1440
_INPUT_IMAGE_HEIGHT = 1920


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _make_input_pdf(path: Path) -> None:
    """Create a stable colour image/vector PDF for the frozen transform path."""

    red = Image.linear_gradient("L").resize(
        (_INPUT_IMAGE_WIDTH, _INPUT_IMAGE_HEIGHT),
        Image.Resampling.BILINEAR,
    )
    green = red.transpose(Image.Transpose.FLIP_TOP_BOTTOM)
    blue = Image.new("L", (_INPUT_IMAGE_WIDTH, _INPUT_IMAGE_HEIGHT), 72)
    colour = Image.merge("RGB", (red, green, blue))
    encoded = BytesIO()
    try:
        colour.save(encoded, format="JPEG", quality=96, optimize=True)
    finally:
        colour.close()
        red.close()
        green.close()
        blue.close()

    with pikepdf.Pdf.new() as pdf:
        page = pdf.add_blank_page(page_size=(612, 792))
        image_stream = pdf.make_stream(
            encoded.getvalue(),
            Type=pikepdf.Name("/XObject"),
            Subtype=pikepdf.Name("/Image"),
            Width=_INPUT_IMAGE_WIDTH,
            Height=_INPUT_IMAGE_HEIGHT,
            ColorSpace=pikepdf.Name("/DeviceRGB"),
            BitsPerComponent=8,
            Filter=pikepdf.Name("/DCTDecode"),
        )
        page.obj["/Resources"] = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im0=image_stream)
        )
        page.Contents = pdf.make_stream(
            b"q 612 0 0 792 0 0 cm /Im0 Do Q\n"
            b"1 0 0 rg 24 24 120 60 re f\n"
        )
        pdf.docinfo["/Title"] = "PROS packaged self-test"
        pdf.docinfo["/Creator"] = "PROS"
        pdf.save(path, deterministic_id=True, fix_metadata_version=False)


def _inspect_grayscale_output(
    path: Path,
    *,
    expect_downsample: bool,
) -> dict[str, int]:
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Output PDF was not created correctly: {path}")

    with pikepdf.Pdf.open(path) as output_pdf:
        if len(output_pdf.pages) != 1:
            raise RuntimeError(
                f"Expected one page in {path.name}, found {len(output_pdf.pages)}"
            )
        images = list(output_pdf.pages[0].get_images().values())
        if len(images) != 1:
            raise RuntimeError(
                f"Expected one embedded image in {path.name}, found {len(images)}"
            )
        output_image = images[0]
        output_colour_space = str(output_image.get("/ColorSpace"))
        if output_colour_space != "/DeviceGray":
            raise RuntimeError(
                f"{path.name} image was not converted to DeviceGray "
                f"(found {output_colour_space})"
            )
        width = int(output_image.get("/Width", 0))
        height = int(output_image.get("/Height", 0))
        if expect_downsample:
            if width >= _INPUT_IMAGE_WIDTH or height >= _INPUT_IMAGE_HEIGHT:
                raise RuntimeError(
                    f"Compression did not downsample the image in {path.name} "
                    f"({_INPUT_IMAGE_WIDTH}x{_INPUT_IMAGE_HEIGHT} -> {width}x{height})"
                )
        elif (width, height) != (_INPUT_IMAGE_WIDTH, _INPUT_IMAGE_HEIGHT):
            raise RuntimeError(
                f"Grayscale-only processing changed image dimensions in {path.name} "
                f"({_INPUT_IMAGE_WIDTH}x{_INPUT_IMAGE_HEIGHT} -> {width}x{height})"
            )
        operators = {
            str(operator)
            for _, operator in pikepdf.parse_content_stream(output_pdf.pages[0])
        }
        if "rg" in operators or "g" not in operators:
            raise RuntimeError(
                f"Common DeviceRGB vector content was not converted in {path.name}"
            )
        syntax_warnings = list(output_pdf.check_pdf_syntax())
    if syntax_warnings:
        raise RuntimeError(
            f"{path.name} failed syntax validation: "
            + "; ".join(map(str, syntax_warnings))
        )
    return {"image_width": width, "image_height": height}


def _run_checked_job(
    request: JobRequest,
    *,
    expected_path: Path,
    input_path: Path,
    input_hash: str,
    progress_events: list[dict[str, object]],
    expect_downsample: bool,
) -> dict[str, Any]:
    result = process_job(request, progress=progress_events.append)
    if not result.success:
        raise RuntimeError(result.error or "PDF engine returned an unsuccessful result")
    if result.cancelled:
        raise RuntimeError("PDF engine unexpectedly reported cancellation")
    if len(result.output_paths) != 1:
        raise RuntimeError(f"Expected one output PDF, received {len(result.output_paths)}")

    actual_path = Path(result.output_paths[0]).resolve()
    expected_path = expected_path.resolve()
    if actual_path != expected_path:
        raise RuntimeError(
            f"Unexpected output name: expected {expected_path.name}, found {actual_path.name}"
        )
    if _sha256(input_path) != input_hash:
        raise RuntimeError("The source PDF changed during processing")

    inspection = _inspect_grayscale_output(
        actual_path,
        expect_downsample=expect_downsample,
    )
    return {
        "output": str(actual_path),
        "output_sha256": _sha256(actual_path),
        "output_size_bytes": actual_path.stat().st_size,
        **inspection,
        "progress_event_count": len(progress_events),
        "warnings": list(result.warnings),
    }


def run_self_test(output_dir: str | os.PathLike[str]) -> int:
    """Exercise the real PDF engine and write a machine-readable report.

    The function does not initialise Tk and does not use the network. It creates
    a unique child directory below *output_dir*, so it never overwrites files
    supplied by the caller. Return ``0`` on success and ``1`` on failure.
    """

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="pros-self-test-", dir=root))
    report_path = run_dir / "selftest-result.json"
    input_path = run_dir / "input.pdf"
    output_path: Path | None = None
    compressed_progress_events: list[dict[str, object]] = []
    grayscale_progress_events: list[dict[str, object]] = []

    report: dict[str, Any] = {
        "status": "failed",
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "pikepdf": pikepdf.__version__,
        "qpdf": pikepdf.__libqpdf_version__,
        "run_directory": str(run_dir),
    }

    try:
        _make_input_pdf(input_path)
        input_hash_before = _sha256(input_path)

        compressed_request = JobRequest(
            job_id=f"selftest-compressed-{os.getpid()}",
            remove_password=False,
            compress_pdf=True,
            structure_mode=StructureMode.NEITHER,
            input_paths=[input_path],
            passwords=[None],
            split_points=[],
            output_dir=run_dir,
            output_base="PROS Self Test",
            staging_dir=run_dir / "staging-compressed",
            compression_level=CompressionLevel.ULTRA,
            convert_to_grayscale=True,
        )
        compressed_expected = run_dir / "PROS Self Test - Cprs - Grey.pdf"
        output_path = compressed_expected
        compressed_report = _run_checked_job(
            compressed_request,
            expected_path=compressed_expected,
            input_path=input_path,
            input_hash=input_hash_before,
            progress_events=compressed_progress_events,
            expect_downsample=True,
        )

        grayscale_request = JobRequest(
            job_id=f"selftest-grayscale-{os.getpid()}",
            remove_password=False,
            compress_pdf=False,
            structure_mode=StructureMode.NEITHER,
            input_paths=[input_path],
            passwords=[None],
            split_points=[],
            output_dir=run_dir,
            output_base="PROS Self Test",
            staging_dir=run_dir / "staging-grayscale",
            compression_level=CompressionLevel.ULTRA,
            convert_to_grayscale=True,
        )
        grayscale_expected = run_dir / "PROS Self Test - Grey.pdf"
        output_path = grayscale_expected
        grayscale_report = _run_checked_job(
            grayscale_request,
            expected_path=grayscale_expected,
            input_path=input_path,
            input_hash=input_hash_before,
            progress_events=grayscale_progress_events,
            expect_downsample=False,
        )
        if _sha256(input_path) != input_hash_before:
            raise RuntimeError("The source PDF changed after the self-test jobs")

        report.update(
            {
                "status": "ok",
                "input": str(input_path),
                "input_sha256": input_hash_before,
                "compression_profile": "jpeg-65-150-dpi",
                "jobs": {
                    "compression_grayscale": compressed_report,
                    "grayscale_only": grayscale_report,
                },
                "progress_event_count": (
                    len(compressed_progress_events) + len(grayscale_progress_events)
                ),
            }
        )
        _write_report(report_path, report)
        return 0
    except Exception as exc:  # noqa: BLE001  # pragma: no cover
        report.update(
            {
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
                "input": str(input_path),
                "output": str(output_path) if output_path is not None else None,
                "progress_event_count": (
                    len(compressed_progress_events) + len(grayscale_progress_events)
                ),
            }
        )
        try:
            _write_report(report_path, report)
        except OSError:
            pass
        return 1


def _main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    target = Path(args[0]) if args else Path.cwd() / "pros-self-test"
    return run_self_test(target)


if __name__ == "__main__":
    raise SystemExit(_main())
