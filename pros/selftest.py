"""Deterministic release health check for source and frozen PROS builds."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
import tempfile
import tkinter as tk
import traceback
import xml.etree.ElementTree as ET
from io import BytesIO
from pathlib import Path
from typing import Any

import pikepdf
import tkinterdnd2
from PIL import Image
from tkinterdnd2 import TkinterDnD

from pros.models import CompressionLevel, JobRequest, StructureMode
from pros.pdf_engine import process_job

_INPUT_IMAGE_WIDTH = 1440
_INPUT_IMAGE_HEIGHT = 1920
_BRAND_ASSET_HASHES = {
    "PROS.ico": "8cb2631a9466b44d5a794574648c1641ff4d7c40ddbb6ddb241d77a4900cbac6",
    "PROS-Logo.png": (
        "eb5493aa738cf8390aaf26c88940aca15a975310ce267174a746776733d2c006"
    ),
    "PROS-App-Icon.png": (
        "f0ce108ea5efe873f6c5a698c104ea41e709c111e715a7d866c4ef354877276e"
    ),
    "PROS-Logo.svg": (
        "a42d55bec96fada63e6de8664aa3b5a9a2c999baec75beccb298044587e7eb54"
    ),
    "PROS-App-Icon.svg": (
        "414185880632e436c027ac2d923b98da226cee27a2116aa4cc8f8828e3db741e"
    ),
}
_PNG_ASSET_SPECS = {
    "PROS-Logo.png": ((1200, 100), "RGBA"),
    "PROS-App-Icon.png": ((1024, 1024), "RGBA"),
}
_ICO_FRAME_SIZES = (
    (16, 16),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
)
_SVG_ASSET_SPECS = {
    "PROS-Logo.svg": ("1200", "100", "0 0 1200 100", "PROS application logo"),
    "PROS-App-Icon.svg": (
        "1024",
        "1024",
        "0 0 1024 1024",
        "PROS Windows application icon",
    ),
}
_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_TKINTERDND2_VERSION = "0.6.2"
_TKINTERDND2_WHEEL_SHA256 = (
    "b6a8b229d26286c022bb2fbd311c2e431e4d9bbab8133be80e9c98e7bcf9fe59"
)
_TKDND_VERSION = "2.10.1"
_TKDND_PLATFORM_DIRECTORY = "win-x64"
_TKDND_RUNTIME_HASHES = {
    "libtkdnd2.10.1.dll": (
        "d8e28ead60b627f5a0b65c677a9e76dc6a4777333ba1f0fb3543de4d65716bf9"
    ),
    "pkgIndex.tcl": (
        "b0b941a237080368ca70b5ffe2b7752978da474a171f0a6303255bddc8a26834"
    ),
    "tkdnd.tcl": (
        "0d857154d6a11a0a94a6ea7c480172fe81a3d6538acb821260fdc38f48b8ba44"
    ),
    "tkdnd_compat.tcl": (
        "87ce43002f60fb7c22c8ca6c17ed13151d21e48175048254c448ac774937b580"
    ),
    "tkdnd_generic.tcl": (
        "78a606df6864e72f1ff19aacbcee2c2559983f1a2aacad5cf21cce51ba7ec5db"
    ),
    "tkdnd_macosx.tcl": (
        "a102102f7fc23f8c401efad0c2877f1654b74365d11c3512721a79119b1071c2"
    ),
    "tkdnd_unix.tcl": (
        "e6e7c64069d975183717d0bfa0931d99403c2d56760cc18230b80712f8a52c6f"
    ),
    "tkdnd_utils.tcl": (
        "5269a7682f3a9c4a78410844d614277b21e63ac69cd30d18d666ddc7191c74c7"
    ),
    "tkdnd_windows.tcl": (
        "5774188ca4b626a8ba243e860b04b198c12bbb61efa1896bfcf4eddc07399fe4"
    ),
}


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


def _brand_assets_directory() -> Path:
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        return Path(frozen_root) / "assets"
    return Path(__file__).resolve().parents[1] / "assets"


def _inspect_brand_assets() -> dict[str, Any]:
    """Validate the canonical source/frozen brand bundle and return its manifest."""

    assets_directory = _brand_assets_directory()
    manifest: dict[str, dict[str, Any]] = {}

    for asset_name, expected_hash in _BRAND_ASSET_HASHES.items():
        asset_path = assets_directory / asset_name
        if not asset_path.is_file():
            raise RuntimeError(f"Required brand asset was not found: {asset_path}")

        actual_hash = _sha256(asset_path)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Brand asset hash mismatch for {asset_name}: "
                f"expected {expected_hash}, found {actual_hash}"
            )
        asset_report: dict[str, Any] = {
            "path": str(asset_path),
            "sha256": actual_hash,
            "size_bytes": asset_path.stat().st_size,
        }

        if asset_name in _PNG_ASSET_SPECS:
            expected_size, expected_mode = _PNG_ASSET_SPECS[asset_name]
            with Image.open(asset_path) as image:
                image.load()
                actual_format = image.format
                actual_size = tuple(image.size)
                actual_mode = image.mode
            if actual_format != "PNG":
                raise RuntimeError(
                    f"{asset_name} is not a PNG (found {actual_format!r})"
                )
            if actual_size != expected_size or actual_mode != expected_mode:
                raise RuntimeError(
                    f"Unexpected {asset_name} image metadata: expected "
                    f"{expected_size[0]}x{expected_size[1]} {expected_mode}, found "
                    f"{actual_size[0]}x{actual_size[1]} {actual_mode}"
                )
            asset_report.update(
                {
                    "format": actual_format,
                    "dimensions": list(actual_size),
                    "mode": actual_mode,
                }
            )
        elif asset_name == "PROS.ico":
            with Image.open(asset_path) as image:
                if image.format != "ICO":
                    raise RuntimeError(
                        f"PROS.ico is not a Windows icon (found {image.format!r})"
                    )
                ico_reader = getattr(image, "ico", None)
                if ico_reader is None:
                    raise RuntimeError("Pillow could not read the PROS.ico frame table")
                actual_sizes = tuple(sorted(ico_reader.sizes()))
                frame_modes: dict[str, str] = {}
                for frame_size in actual_sizes:
                    frame = ico_reader.getimage(frame_size)
                    try:
                        frame.load()
                        frame_modes[f"{frame_size[0]}x{frame_size[1]}"] = frame.mode
                    finally:
                        frame.close()
            if actual_sizes != _ICO_FRAME_SIZES:
                raise RuntimeError(
                    "Unexpected PROS.ico frame sizes: expected "
                    f"{_ICO_FRAME_SIZES}, found {actual_sizes}"
                )
            non_rgba_frames = {
                size: mode for size, mode in frame_modes.items() if mode != "RGBA"
            }
            if non_rgba_frames:
                raise RuntimeError(
                    f"PROS.ico contains non-RGBA frames: {non_rgba_frames}"
                )
            asset_report.update(
                {
                    "format": "ICO",
                    "frame_sizes": [list(size) for size in actual_sizes],
                    "frame_modes": frame_modes,
                }
            )
        elif asset_name in _SVG_ASSET_SPECS:
            expected_width, expected_height, expected_view_box, expected_title = (
                _SVG_ASSET_SPECS[asset_name]
            )
            payload = asset_path.read_bytes()
            if not payload.lstrip().startswith(b'<?xml version="1.0"'):
                raise RuntimeError(f"{asset_name} has an unexpected XML signature")
            try:
                svg_root = ET.fromstring(payload)
            except ET.ParseError as exc:
                raise RuntimeError(f"{asset_name} is not valid XML: {exc}") from exc
            if svg_root.tag != f"{{{_SVG_NAMESPACE}}}svg":
                raise RuntimeError(f"{asset_name} does not have an SVG root element")
            actual_signature = (
                svg_root.get("width"),
                svg_root.get("height"),
                svg_root.get("viewBox"),
            )
            expected_signature = (
                expected_width,
                expected_height,
                expected_view_box,
            )
            title = svg_root.find(f"{{{_SVG_NAMESPACE}}}title")
            actual_title = title.text if title is not None else None
            if actual_signature != expected_signature or actual_title != expected_title:
                raise RuntimeError(
                    f"Unexpected {asset_name} SVG signature: found "
                    f"{actual_signature!r}, title {actual_title!r}"
                )
            asset_report.update(
                {
                    "format": "SVG",
                    "dimensions": [int(expected_width), int(expected_height)],
                    "view_box": expected_view_box,
                    "title": actual_title,
                }
            )
        else:  # pragma: no cover - the release manifest is exhaustive
            raise RuntimeError(f"No brand asset inspector is defined for {asset_name}")

        manifest[asset_name] = asset_report

    return {
        "directory": str(assets_directory),
        "files": manifest,
    }


def _inspect_dnd_payload() -> dict[str, Any]:
    """Validate the exact Windows x64 TkDND payload selected by the build hook."""

    module_file = getattr(tkinterdnd2, "__file__", None)
    if not module_file:
        raise RuntimeError("tkinterdnd2 does not expose a package location")
    runtime_directory = (
        Path(module_file).resolve().parent
        / "tkdnd"
        / _TKDND_PLATFORM_DIRECTORY
    )
    files: dict[str, dict[str, Any]] = {}
    for filename, expected_hash in _TKDND_RUNTIME_HASHES.items():
        path = runtime_directory / filename
        if not path.is_file():
            raise RuntimeError(f"Required TkDND runtime file was not found: {path}")
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise RuntimeError(
                f"TkDND runtime hash mismatch for {filename}: "
                f"expected {expected_hash}, found {actual_hash}"
            )
        files[filename] = {
            "path": str(path),
            "sha256": actual_hash,
            "size_bytes": path.stat().st_size,
        }

    return {
        "wrapper_version": _TKINTERDND2_VERSION,
        "expected_wheel_sha256": _TKINTERDND2_WHEEL_SHA256,
        "platform_directory": _TKDND_PLATFORM_DIRECTORY,
        "runtime_directory": str(runtime_directory),
        "payload_size_bytes": sum(item["size_bytes"] for item in files.values()),
        "files": files,
    }


def _probe_dnd_runtime() -> dict[str, Any]:
    """Load TkDND through the same existing-root API used by the application."""

    report = _inspect_dnd_payload()
    root: tk.Tk | None = None
    try:
        root = tk.Tk()
        root.withdraw()
        tcl_version = str(root.tk.call("info", "patchlevel"))
        tk_version = str(root.tk.call("package", "require", "Tk"))
        tkdnd_version = str(TkinterDnD.require(root))
    except (RuntimeError, tk.TclError) as exc:
        raise RuntimeError(f"TkDND runtime probe failed: {exc}") from exc
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass

    if not tcl_version.startswith("8.6") or not tk_version.startswith("8.6"):
        raise RuntimeError(
            f"Expected the pinned Tcl/Tk 8.6 runtime, found "
            f"Tcl {tcl_version} and Tk {tk_version}"
        )
    if tkdnd_version != _TKDND_VERSION:
        raise RuntimeError(
            f"Expected TkDND {_TKDND_VERSION}, found {tkdnd_version}"
        )

    report.update(
        {
            "loaded": True,
            "system": platform.system(),
            "machine": platform.machine(),
            "tcl_version": tcl_version,
            "tk_version": tk_version,
            "tkdnd_version": tkdnd_version,
        }
    )
    return report


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
    return {
        "image_width": width,
        "image_height": height,
        "syntax_warning_count": len(syntax_warnings),
    }


def _inspect_reopenable_output(path: Path, *, expected_page_count: int = 1) -> dict[str, int]:
    """Require a non-empty, reopenable, syntax-clean PDF output."""

    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Output PDF was not created correctly: {path}")
    with pikepdf.Pdf.open(path) as output_pdf:
        page_count = len(output_pdf.pages)
        if page_count != expected_page_count:
            raise RuntimeError(
                f"Expected {expected_page_count} page(s) in {path.name}, "
                f"found {page_count}"
            )
        syntax_warnings = list(output_pdf.check_pdf_syntax())
    if syntax_warnings:
        raise RuntimeError(
            f"{path.name} failed syntax validation: "
            + "; ".join(map(str, syntax_warnings))
        )
    return {
        "page_count": page_count,
        "syntax_warning_count": len(syntax_warnings),
    }


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


def _run_checked_separate_job(
    request: JobRequest,
    *,
    expected_paths: tuple[Path, ...],
    input_paths: tuple[Path, ...],
    input_hashes: tuple[str, ...],
    progress_events: list[dict[str, object]],
) -> dict[str, Any]:
    """Exercise ordered multi-file Keep separate output and source safety."""

    result = process_job(request, progress=progress_events.append)
    if not result.success:
        raise RuntimeError(result.error or "PDF engine returned an unsuccessful result")
    if result.cancelled:
        raise RuntimeError("PDF engine unexpectedly reported cancellation")
    if len(result.output_paths) != len(expected_paths):
        raise RuntimeError(
            f"Expected {len(expected_paths)} output PDFs, "
            f"received {len(result.output_paths)}"
        )

    actual_paths = tuple(Path(path).resolve() for path in result.output_paths)
    resolved_expected_paths = tuple(path.resolve() for path in expected_paths)
    if actual_paths != resolved_expected_paths:
        expected_names = [path.name for path in resolved_expected_paths]
        actual_names = [path.name for path in actual_paths]
        raise RuntimeError(
            "Unexpected ordered Keep separate output names: "
            f"expected {expected_names}, found {actual_names}"
        )

    outputs: list[dict[str, Any]] = []
    for input_path, input_hash, actual_path in zip(
        input_paths,
        input_hashes,
        actual_paths,
        strict=True,
    ):
        input_hash_after = _sha256(input_path)
        if input_hash_after != input_hash:
            raise RuntimeError(
                f"The source PDF changed during Keep separate: {input_path.name}"
            )
        inspection = _inspect_reopenable_output(actual_path)
        outputs.append(
            {
                "input": str(input_path.resolve()),
                "input_sha256_before": input_hash,
                "input_sha256_after": input_hash_after,
                "output": str(actual_path),
                "output_sha256": _sha256(actual_path),
                "output_size_bytes": actual_path.stat().st_size,
                **inspection,
            }
        )

    return {
        "outputs": outputs,
        "progress_event_count": len(progress_events),
        "warnings": list(result.warnings),
    }


def run_self_test(output_dir: str | os.PathLike[str]) -> int:
    """Exercise the real PDF engine and write a machine-readable report.

    The function briefly creates and withdraws a Tk window to load the native
    TkDND extension, but does not start the application GUI or use the network.
    It creates a unique child directory below *output_dir*, so it never
    overwrites files supplied by the caller. Return ``0`` on success and ``1``
    on failure.
    """

    root = Path(output_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="pros-self-test-", dir=root))
    report_path = run_dir / "selftest-result.json"
    input_path = run_dir / "PROS Self Test First.pdf"
    second_input_path = run_dir / "PROS Self Test Second.pdf"
    output_path: Path | None = None
    compressed_progress_events: list[dict[str, object]] = []
    grayscale_progress_events: list[dict[str, object]] = []
    separate_progress_events: list[dict[str, object]] = []

    report: dict[str, Any] = {
        "status": "failed",
        "frozen": bool(getattr(sys, "frozen", False)),
        "python": sys.version.split()[0],
        "pikepdf": pikepdf.__version__,
        "qpdf": pikepdf.__libqpdf_version__,
        "run_directory": str(run_dir),
    }

    try:
        report["brand_assets"] = _inspect_brand_assets()
        report["drag_and_drop"] = _probe_dnd_runtime()
        _make_input_pdf(input_path)
        _make_input_pdf(second_input_path)
        input_hash_before = _sha256(input_path)
        second_input_hash_before = _sha256(second_input_path)

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

        separate_input_paths = (second_input_path, input_path)
        separate_input_hashes = (second_input_hash_before, input_hash_before)
        separate_request = JobRequest(
            job_id=f"selftest-separate-{os.getpid()}",
            remove_password=False,
            compress_pdf=True,
            structure_mode=StructureMode.NEITHER,
            input_paths=list(separate_input_paths),
            passwords=[None, None],
            split_points=[],
            output_dir=run_dir,
            output_base="This Multi-file Base Must Be Ignored",
            staging_dir=run_dir / "staging-separate",
            compression_level=CompressionLevel.ULTRA,
            convert_to_grayscale=False,
        )
        separate_expected = (
            run_dir / "PROS Self Test Second - Cprs.pdf",
            run_dir / "PROS Self Test First - Cprs.pdf",
        )
        output_path = separate_expected[0]
        separate_report = _run_checked_separate_job(
            separate_request,
            expected_paths=separate_expected,
            input_paths=separate_input_paths,
            input_hashes=separate_input_hashes,
            progress_events=separate_progress_events,
        )

        final_input_hashes = (_sha256(input_path), _sha256(second_input_path))
        if final_input_hashes != (input_hash_before, second_input_hash_before):
            raise RuntimeError("The source PDF changed after the self-test jobs")

        report.update(
            {
                "status": "ok",
                "input": str(input_path),
                "input_sha256": input_hash_before,
                "inputs": [
                    {"path": str(input_path), "sha256": input_hash_before},
                    {
                        "path": str(second_input_path),
                        "sha256": second_input_hash_before,
                    },
                ],
                "compression_profile": "jpeg-65-150-dpi",
                "jobs": {
                    "compression_grayscale": compressed_report,
                    "grayscale_only": grayscale_report,
                    "keep_separate_multi": separate_report,
                },
                "progress_event_count": (
                    len(compressed_progress_events) + len(grayscale_progress_events)
                    + len(separate_progress_events)
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
                    + len(separate_progress_events)
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
