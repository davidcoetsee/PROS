# SPDX-License-Identifier: MPL-2.0
"""Offline pikepdf/qpdf engine for atomic PROS PDF jobs."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack
from io import BytesIO
from math import ceil
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image

from .models import (
    CompressionLevel,
    EstimateResult,
    JobRequest,
    JobResult,
    PdfInfo,
    StructureMode,
)
from .naming import build_output_paths

ProgressCallback = Callable[[dict[str, object]], None]

_READ_CHUNK = 1024 * 1024
_ULTRA_JPEG_QUALITY = 65
_GRAYSCALE_ONLY_JPEG_QUALITY = 92
_ULTRA_IMAGE_DPI = 150
_MIN_IMAGE_AREA = 65_536
_MAX_ESTIMATE_SAMPLE_PIXELS = 30_000_000


class _Cancelled(RuntimeError):
    pass


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _redact_secrets(value: object, secrets: Sequence[str | None]) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _cancelled(cancel_event: object | None) -> bool:
    if cancel_event is None:
        return False
    is_set = getattr(cancel_event, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _check_cancel(cancel_event: object | None) -> None:
    if _cancelled(cancel_event):
        raise _Cancelled("The job was cancelled.")


def _emit(
    request: JobRequest,
    progress: ProgressCallback | None,
    cancel_event: object | None,
    *,
    stage: str,
    percent: float,
    message: str,
    path: Path | None = None,
    kind: str = "progress",
    file_index: int | None = None,
    file_count: int | None = None,
    phase_percent: int | None = None,
) -> None:
    _check_cancel(cancel_event)
    if progress is None:
        return
    event: dict[str, object] = {
        "job_id": request.job_id,
        "kind": kind,
        "stage": stage,
        "percent": max(0, min(100, round(percent))),
        "message": message,
        "path": str(path) if path is not None else None,
    }
    if file_index is not None:
        event["file_index"] = file_index
    if file_count is not None:
        event["file_count"] = file_count
    if phase_percent is not None:
        event["phase_percent"] = max(0, min(100, round(phase_percent)))
    progress(event)


def _compression_message(
    request: JobRequest,
    path: Path,
    phase_percent: int,
) -> str:
    """Return incremental user-facing compression or grayscale progress."""

    bounded = max(0, min(100, phase_percent))
    if request.compress_pdf:
        bar = "-" * (bounded // 2)
        return f"Compressing {path.name} [{bar}] {bounded}%"
    return f"Converting {path.name} to grayscale ({bounded}%)"


def _emit_compression(
    request: JobRequest,
    progress: ProgressCallback | None,
    cancel_event: object | None,
    *,
    final_path: Path,
    file_index: int,
    file_count: int,
    phase_percent: float,
) -> None:
    phase = max(0, min(100, int(phase_percent // 2) * 2))
    global_percent = 55 + (25 * ((file_index - 1) + phase / 100) / file_count)
    _emit(
        request,
        progress,
        cancel_event,
        stage="compress" if request.compress_pdf else "grayscale",
        percent=global_percent,
        message=_compression_message(request, final_path, phase),
        path=final_path,
        file_index=file_index,
        file_count=file_count,
        phase_percent=phase,
    )


def _has_pdf_signature(header: bytes) -> bool:
    marker = header.find(b"%PDF-")
    return 0 <= marker < 1024


def _iter_form_fields(fields: Any) -> Iterable[Any]:
    stack = list(fields or [])
    visited: set[tuple[int, int] | int] = set()
    while stack:
        field = stack.pop()
        try:
            objgen = field.objgen
            identity: tuple[int, int] | int = objgen if objgen != (0, 0) else id(field)
        except (AttributeError, ValueError):
            identity = id(field)
        if identity in visited:
            continue
        visited.add(identity)
        yield field
        try:
            kids = field.get("/Kids", [])
            stack.extend(list(kids))
        except (AttributeError, TypeError, ValueError):
            continue


def _has_internal_page_links(pdf: pikepdf.Pdf) -> bool:
    for page in pdf.pages:
        try:
            annotations = page.obj.get("/Annots", [])
        except (AttributeError, ValueError):
            continue
        for annotation in annotations:
            try:
                if annotation.get("/Subtype") != pikepdf.Name("/Link"):
                    continue
                if annotation.get("/Dest") is not None:
                    return True
                action = annotation.get("/A")
                if action is not None and action.get("/S") == pikepdf.Name("/GoTo"):
                    return True
            except (AttributeError, ValueError):
                continue
    return False


def _detect_preservation_risks(pdf: pikepdf.Pdf) -> tuple[str, ...]:
    risks: list[str] = []
    root = pdf.Root
    acroform = root.get("/AcroForm")

    signature_found = root.get("/Perms") is not None
    if acroform is not None:
        inherited_field_type: dict[tuple[int, int] | int, Any] = {}
        for field in _iter_form_fields(acroform.get("/Fields", [])):
            field_type = field.get("/FT")
            if field_type == pikepdf.Name("/Sig"):
                signature_found = True
            try:
                identity = field.objgen if field.objgen != (0, 0) else id(field)
                inherited_field_type[identity] = field_type
            except (AttributeError, ValueError):
                pass
        if acroform.get("/XFA") is not None:
            risks.append("XFA forms cannot be safely preserved by Join or Split.")
    if signature_found:
        risks.append("Digital signatures will be invalidated by PDF rewriting.")
    if root.get("/StructTreeRoot") is not None:
        risks.append("Tagged document structure may not be safely preserved by Join or Split.")
    if root.get("/Threads") is not None:
        risks.append("Article threads may not be safely preserved by Join or Split.")
    if root.get("/OCProperties") is not None:
        risks.append("Optional-content layers may not be safely preserved by Join or Split.")
    if root.get("/Collection") is not None:
        risks.append("PDF portfolio behaviour may not be safely preserved by Join or Split.")

    names = root.get("/Names")
    if names is not None:
        if names.get("/EmbeddedFiles") is not None:
            risks.append("Embedded files may not be safely preserved by Join or Split.")
        if names.get("/JavaScript") is not None:
            risks.append("Document-level JavaScript may not be safely preserved by Join or Split.")
    if root.get("/OpenAction") is not None or root.get("/AA") is not None:
        risks.append("Document-level actions may not be safely preserved by Join or Split.")
    if _has_internal_page_links(pdf):
        risks.append("Internal page links require destination remapping during Join or Split.")
    return _unique(risks)


def inspect_pdf(path: str | os.PathLike[str], password: str | None = None) -> PdfInfo:
    """Inspect one PDF without modifying it or disclosing its password."""

    pdf_path = Path(path)
    size_bytes = 0
    try:
        stat = pdf_path.stat()
        size_bytes = stat.st_size
    except OSError as exc:
        return PdfInfo(path=pdf_path, error=f"The file is unavailable ({exc.strerror or type(exc).__name__}).")
    if not pdf_path.is_file():
        return PdfInfo(path=pdf_path, size_bytes=size_bytes, error="The selected path is not a file.")
    if pdf_path.suffix.casefold() != ".pdf":
        return PdfInfo(path=pdf_path, size_bytes=size_bytes, error="Only .pdf files are accepted.")
    try:
        with pdf_path.open("rb") as stream:
            header = stream.read(1024)
    except OSError as exc:
        return PdfInfo(path=pdf_path, size_bytes=size_bytes, error=f"The file cannot be read ({exc.strerror or type(exc).__name__}).")
    if not _has_pdf_signature(header):
        return PdfInfo(path=pdf_path, size_bytes=size_bytes, error="The file does not contain a valid PDF signature.")

    try:
        with pikepdf.Pdf.open(
            pdf_path,
            password=password if password is not None else "",
            suppress_warnings=True,
            attempt_recovery=False,
        ) as pdf:
            syntax_warnings = [str(item) for item in pdf.check_pdf_syntax()]
            parser_warnings = [str(item) for item in pdf.get_warnings()]
            encrypted = bool(pdf.is_encrypted)
            password_valid = True if encrypted else None
            return PdfInfo(
                path=pdf_path,
                size_bytes=size_bytes,
                page_count=len(pdf.pages),
                encrypted=encrypted,
                password_valid=password_valid,
                warnings=_unique([*parser_warnings, *syntax_warnings]),
                risks=_detect_preservation_risks(pdf),
            )
    except pikepdf.PasswordError:
        return PdfInfo(
            path=pdf_path,
            size_bytes=size_bytes,
            encrypted=True,
            password_valid=False,
        )
    except (pikepdf.PdfError, RuntimeError, ValueError) as exc:
        detail = _redact_secrets(exc, [password]).strip()
        message = "The PDF is corrupt or uses an unsupported structure."
        if detail:
            message = f"{message} {detail}"
        return PdfInfo(path=pdf_path, size_bytes=size_bytes, error=message)
    except OSError as exc:
        return PdfInfo(path=pdf_path, size_bytes=size_bytes, error=f"The file cannot be read ({exc.strerror or type(exc).__name__}).")


def _sha256_file(path: Path, cancel_event: object | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_READ_CHUNK):
            _check_cancel(cancel_event)
            digest.update(chunk)
    return digest.hexdigest()


def _configure_lossless_compression(builder: pikepdf.JobBuilder) -> pikepdf.JobBuilder:
    return builder.compress(
        compress_streams=True,
        object_streams="generate",
        recompress_flate=True,
        compression_level=9,
        decode_level="generalized",
    )


def _configure_image_optimization(
    builder: pikepdf.JobBuilder,
    level: CompressionLevel = CompressionLevel.ULTRA,
) -> pikepdf.JobBuilder:
    # STANDARD remains accepted for saved requests, but the second revision
    # intentionally applies the former Ultra profile to all compression jobs.
    del level
    return builder.optimize_images(
        min_width=256,
        min_height=256,
        min_area=_MIN_IMAGE_AREA,
        keep_inline_images=True,
        jpeg_quality=_ULTRA_JPEG_QUALITY,
    )


def _run_qpdf_job(
    builder: pikepdf.JobBuilder,
    warnings: list[str],
    *,
    request: JobRequest | None = None,
    progress: ProgressCallback | None = None,
    cancel_event: object | None = None,
    final_path: Path | None = None,
    file_index: int = 1,
    file_count: int = 1,
    phase_start: int = 0,
    phase_end: int = 100,
) -> None:
    """Run qpdf and publish only real operation-boundary progress.

    JobBuilder can hold the Python GIL during native work, so a Python thread
    cannot provide a reliable wall-clock heartbeat. The GUI may animate a
    clearly labelled estimate between these monotonic, authoritative events.
    """

    if request is not None and final_path is not None:
        _emit_compression(
            request,
            progress,
            cancel_event,
            final_path=final_path,
            file_index=file_index,
            file_count=file_count,
            phase_percent=phase_start,
        )
    job = builder.run()
    _check_cancel(cancel_event)
    if request is not None and final_path is not None:
        _emit_compression(
            request,
            progress,
            cancel_event,
            final_path=final_path,
            file_index=file_index,
            file_count=file_count,
            phase_percent=phase_end,
        )
    if job.has_warnings:
        warnings.append("The PDF writer reported a recoverable warning while saving the file.")


def _object_identity(obj: Any) -> tuple[object, ...]:
    try:
        objgen = tuple(obj.objgen)
    except (AttributeError, TypeError, ValueError):
        return ("direct", id(obj))
    return ("indirect", *objgen) if objgen != (0, 0) else ("direct", id(obj))


def _page_dimensions(page: pikepdf.Page) -> tuple[float, float]:
    box = page.mediabox
    return abs(float(box[2]) - float(box[0])), abs(float(box[3]) - float(box[1]))


def _collect_image_targets(
    pdf: pikepdf.Pdf,
    target_dpi: int | None,
) -> list[tuple[Any, int, int]]:
    records: dict[tuple[object, ...], tuple[Any, int, int]] = {}
    for page in pdf.pages:
        page_width, page_height = _page_dimensions(page)
        for image_obj in page.get_images().values():
            width = int(image_obj.get("/Width", 0))
            height = int(image_obj.get("/Height", 0))
            if target_dpi is None:
                cap_width, cap_height = width, height
            else:
                cap_width = max(1, ceil(page_width * target_dpi / 72))
                cap_height = max(1, ceil(page_height * target_dpi / 72))
            identity = _object_identity(image_obj)
            previous = records.get(identity)
            if previous is None:
                records[identity] = (image_obj, cap_width, cap_height)
            else:
                records[identity] = (
                    previous[0],
                    max(previous[1], cap_width),
                    max(previous[2], cap_height),
                )
    return list(records.values())


def _transcode_image(
    image_obj: Any,
    *,
    max_width: int,
    max_height: int,
    jpeg_quality: int,
    grayscale: bool,
) -> tuple[bytes, pikepdf.Name, int, int] | None:
    width = int(image_obj.get("/Width", 0))
    height = int(image_obj.get("/Height", 0))
    bits = int(image_obj.get("/BitsPerComponent", 1))
    if width * height < _MIN_IMAGE_AREA or bits < 8:
        return None
    if image_obj.get("/ImageMask", False):
        return None
    if image_obj.get("/SMask") is not None or image_obj.get("/Mask") is not None:
        return None

    with pikepdf.PdfImage(image_obj).as_pil_image(apply_mask=False) as source_image:
        source_image.load()
        if "A" in source_image.getbands() or source_image.mode in {"P", "PA"}:
            return None
        target_mode = "L" if grayscale else "RGB"
        converted = source_image.convert(target_mode)
        try:
            scale = min(1.0, max_width / converted.width, max_height / converted.height)
            if scale < 1.0:
                resized = converted.resize(
                    (
                        max(1, round(converted.width * scale)),
                        max(1, round(converted.height * scale)),
                    ),
                    Image.Resampling.LANCZOS,
                )
                converted.close()
                converted = resized
            encoded = BytesIO()
            save_options: dict[str, object] = {
                "quality": jpeg_quality,
                "optimize": True,
            }
            if target_mode == "RGB":
                save_options["subsampling"] = "4:2:0"
            converted.save(encoded, format="JPEG", **save_options)
            color_space = pikepdf.Name(
                "/DeviceGray" if target_mode == "L" else "/DeviceRGB"
            )
            return encoded.getvalue(), color_space, converted.width, converted.height
        finally:
            converted.close()


def _image_is_already_gray(image_obj: Any) -> bool:
    if image_obj.get("/ImageMask", False):
        return True
    color_space = image_obj.get("/ColorSpace")
    if str(color_space) in {"/DeviceGray", "/G"}:
        return True
    if isinstance(color_space, pikepdf.Array) and color_space:
        return str(color_space[0]) in {"/DeviceGray", "/G"}
    return False


def _classify_color_space(value: Any, resources: Any) -> str:
    name = str(value)
    known = {
        "/DeviceGray": "gray",
        "/G": "gray",
        "/DeviceRGB": "rgb",
        "/RGB": "rgb",
        "/DeviceCMYK": "cmyk",
        "/CMYK": "cmyk",
    }
    if name in known:
        return known[name]
    try:
        spaces = resources.get("/ColorSpace") if resources is not None else None
        definition = spaces.get(value) if spaces is not None else None
        if definition is not None and isinstance(definition, pikepdf.Array) and definition:
            return known.get(str(definition[0]), "unsupported")
        if definition is not None:
            return known.get(str(definition), "unsupported")
    except (AttributeError, KeyError, TypeError, ValueError):
        pass
    return "unsupported"


def _gray_from_operands(operands: Any, color_space: str) -> float | None:
    try:
        values = [float(value) for value in operands]
    except (TypeError, ValueError):
        return None
    if color_space == "gray" and len(values) == 1:
        return max(0.0, min(1.0, values[0]))
    if color_space == "rgb" and len(values) == 3:
        red, green, blue = values
        return max(0.0, min(1.0, 0.2126 * red + 0.7152 * green + 0.0722 * blue))
    if color_space == "cmyk" and len(values) == 4:
        cyan, magenta, yellow, black = values
        red = 1.0 - min(1.0, cyan + black)
        green = 1.0 - min(1.0, magenta + black)
        blue = 1.0 - min(1.0, yellow + black)
        return max(0.0, min(1.0, 0.2126 * red + 0.7152 * green + 0.0722 * blue))
    return None


def _rewrite_content_stream_to_gray(owner: Any, pdf: pikepdf.Pdf, resources: Any) -> bool:
    """Convert common DeviceRGB/DeviceCMYK drawing operators in one stream."""

    commands: list[list[Any]] = []
    fill_space = "gray"
    stroke_space = "gray"
    stack: list[tuple[str, str]] = []
    unsupported = False
    for operands, operator in pikepdf.parse_content_stream(owner):
        op = str(operator)
        new_operands: Any = operands
        new_operator = operator
        if op == "q":
            stack.append((fill_space, stroke_space))
        elif op == "Q" and stack:
            fill_space, stroke_space = stack.pop()
        elif op in {"g", "rg", "k"}:
            fill_space = {"g": "gray", "rg": "rgb", "k": "cmyk"}[op]
            gray = _gray_from_operands(operands, fill_space)
            if gray is not None:
                new_operands = pikepdf.Array([round(gray, 6)])
                new_operator = pikepdf.Operator("g")
        elif op in {"G", "RG", "K"}:
            stroke_space = {"G": "gray", "RG": "rgb", "K": "cmyk"}[op]
            gray = _gray_from_operands(operands, stroke_space)
            if gray is not None:
                new_operands = pikepdf.Array([round(gray, 6)])
                new_operator = pikepdf.Operator("G")
        elif op in {"cs", "CS"} and operands:
            selected = _classify_color_space(operands[0], resources)
            if op == "cs":
                fill_space = selected
            else:
                stroke_space = selected
            if selected in {"gray", "rgb", "cmyk"}:
                new_operands = pikepdf.Array([pikepdf.Name("/DeviceGray")])
            else:
                unsupported = True
        elif op in {"sc", "scn", "SC", "SCN"}:
            selected = fill_space if op.islower() else stroke_space
            gray = _gray_from_operands(operands, selected)
            if gray is None:
                unsupported = True
            else:
                new_operands = pikepdf.Array([round(gray, 6)])
                new_operator = pikepdf.Operator("g" if op.islower() else "G")
        elif op == "sh":
            unsupported = True
        commands.append([new_operands, new_operator])

    data = pikepdf.unparse_content_stream(commands)
    if isinstance(owner, pikepdf.Page):
        owner.Contents = pdf.make_stream(data)
    else:
        owner.write(data)
    try:
        if resources is not None and (
            resources.get("/Pattern") is not None or resources.get("/Shading") is not None
        ):
            unsupported = True
        ext_gstates = resources.get("/ExtGState") if resources is not None else None
        if ext_gstates is not None:
            for state in ext_gstates.values():
                soft_mask = state.get("/SMask")
                blend_mode = state.get("/BM")
                if soft_mask is not None and str(soft_mask) != "/None":
                    unsupported = True
                if blend_mode is not None and str(blend_mode) != "/Normal":
                    unsupported = True
                for alpha_key in ("/ca", "/CA"):
                    alpha = state.get(alpha_key)
                    if alpha is not None and float(alpha) < 1.0:
                        unsupported = True
        owner_obj = owner.obj if isinstance(owner, pikepdf.Page) else owner
        group = owner_obj.get("/Group")
        if group is not None and group.get("/S") == pikepdf.Name("/Transparency"):
            unsupported = True
    except (AttributeError, TypeError, ValueError):
        unsupported = True
    return unsupported


def _convert_common_content_to_grayscale(pdf: pikepdf.Pdf) -> bool:
    unsupported = pdf.Root.get("/AcroForm") is not None
    seen_forms: set[tuple[object, ...]] = set()

    def process_forms(resources: Any) -> None:
        nonlocal unsupported
        if resources is None:
            return
        try:
            xobjects = resources.get("/XObject")
            values = list(xobjects.values()) if xobjects is not None else []
        except (AttributeError, TypeError, ValueError):
            return
        for xobject in values:
            try:
                if xobject.get("/Subtype") != pikepdf.Name("/Form"):
                    continue
            except (AttributeError, ValueError):
                continue
            identity = _object_identity(xobject)
            if identity in seen_forms:
                continue
            seen_forms.add(identity)
            form_resources = xobject.get("/Resources", resources)
            try:
                unsupported |= _rewrite_content_stream_to_gray(xobject, pdf, form_resources)
            except (pikepdf.PdfError, pikepdf.PdfParsingError, TypeError, ValueError):
                unsupported = True
            process_forms(form_resources)

    for page in pdf.pages:
        resources = page.obj.get("/Resources")
        if page.obj.get("/Annots") is not None:
            # Annotation and form-widget appearance streams are separate from
            # page/Form content and may use complex inherited resources.
            unsupported = True
        try:
            unsupported |= _rewrite_content_stream_to_gray(page, pdf, resources)
        except (pikepdf.PdfError, pikepdf.PdfParsingError, TypeError, ValueError):
            unsupported = True
        process_forms(resources)
    return unsupported


def _transform_pdf_images(
    request: JobRequest,
    source: Path,
    output: Path,
    warnings: list[str],
    progress: ProgressCallback | None,
    cancel_event: object | None,
    *,
    final_path: Path,
    file_index: int,
    file_count: int,
) -> None:
    target_dpi = _ULTRA_IMAGE_DPI if request.compress_pdf else None
    jpeg_quality = (
        _ULTRA_JPEG_QUALITY if request.compress_pdf else _GRAYSCALE_ONLY_JPEG_QUALITY
    )
    skipped_color_images = 0
    with pikepdf.Pdf.open(source, suppress_warnings=True, attempt_recovery=False) as pdf:
        if request.convert_to_grayscale:
            _emit_compression(
                request,
                progress,
                cancel_event,
                final_path=final_path,
                file_index=file_index,
                file_count=file_count,
                phase_percent=42,
            )
            if _convert_common_content_to_grayscale(pdf):
                warnings.append(
                    "Some ICC, spot, pattern, shading, or transparency-based colours could not be converted; review the grayscale output."
                )

        images = _collect_image_targets(pdf, target_dpi)
        for image_number, (image_obj, max_width, max_height) in enumerate(images, start=1):
            _check_cancel(cancel_event)
            if (
                request.convert_to_grayscale
                and not request.compress_pdf
                and _image_is_already_gray(image_obj)
            ):
                transformed = None
            else:
                try:
                    transformed = _transcode_image(
                        image_obj,
                        max_width=max_width,
                        max_height=max_height,
                        jpeg_quality=jpeg_quality,
                        grayscale=request.convert_to_grayscale,
                    )
                except (
                    OSError,
                    ValueError,
                    pikepdf.PdfError,
                    pikepdf.UnsupportedImageTypeError,
                    pikepdf.DecompressionBombError,
                ):
                    transformed = None
            if transformed is None:
                if request.convert_to_grayscale and not _image_is_already_gray(image_obj):
                    skipped_color_images += 1
            else:
                encoded, color_space, width, height = transformed
                original_size = len(image_obj.read_raw_bytes())
                must_replace = request.convert_to_grayscale
                if must_replace or len(encoded) < original_size:
                    image_obj.write(encoded, filter=pikepdf.Name("/DCTDecode"))
                    image_obj["/ColorSpace"] = color_space
                    image_obj["/BitsPerComponent"] = 8
                    image_obj["/Width"] = width
                    image_obj["/Height"] = height
                    for key in (
                        "/Decode",
                        "/DecodeParms",
                        "/DP",
                        "/JBIG2Globals",
                        "/SMaskInData",
                    ):
                        if key in image_obj:
                            del image_obj[key]
            phase = 45 + (35 * image_number / max(1, len(images)))
            _emit_compression(
                request,
                progress,
                cancel_event,
                final_path=final_path,
                file_index=file_index,
                file_count=file_count,
                phase_percent=phase,
            )

        if skipped_color_images:
            warnings.append(
                f"{skipped_color_images} masked, monochrome, or unusually encoded image(s) could not be converted to grayscale; review the output."
            )
        pdf.save(
            output,
            fix_metadata_version=False,
            encryption=None,
            compress_streams=True,
            object_stream_mode=(
                pikepdf.ObjectStreamMode.generate
                if request.compress_pdf
                else pikepdf.ObjectStreamMode.preserve
            ),
            stream_decode_level=(
                pikepdf.StreamDecodeLevel.generalized if request.compress_pdf else None
            ),
            recompress_flate=request.compress_pdf,
            progress=lambda value: _emit_compression(
                request,
                progress,
                cancel_event,
                final_path=final_path,
                file_index=file_index,
                file_count=file_count,
                phase_percent=80 + (8 * value / 100),
            ),
        )


def _encoded_stream_length(image_obj: Any) -> int:
    try:
        return max(0, int(image_obj.get("/Length", 0)))
    except (TypeError, ValueError):
        try:
            return len(image_obj.read_raw_bytes())
        except (OSError, pikepdf.PdfError):
            return 0


def _estimate_pdf_compression(
    request: JobRequest,
    path: Path,
    password: str | None,
    *,
    cancel_event: object | None,
) -> tuple[int, int, int, float, tuple[str, ...]]:
    """Return estimated bytes, image pixels, sampled pixels, sample time, warnings."""

    target_dpi = _ULTRA_IMAGE_DPI if request.compress_pdf else None
    jpeg_quality = (
        _ULTRA_JPEG_QUALITY if request.compress_pdf else _GRAYSCALE_ONLY_JPEG_QUALITY
    )
    file_size = path.stat().st_size
    warnings: list[str] = []
    with pikepdf.Pdf.open(
        path,
        password=password or "",
        suppress_warnings=True,
        attempt_recovery=False,
    ) as pdf:
        records = _collect_image_targets(pdf, target_dpi)
        stream_bytes = sum(_encoded_stream_length(record[0]) for record in records)
        total_pixels = sum(
            int(record[0].get("/Width", 0)) * int(record[0].get("/Height", 0))
            for record in records
        )
        sampled_pixels = 0
        sampled_input_bytes = 0
        sampled_output_bytes = 0
        skipped_samples = 0
        sample_started = time.perf_counter()
        for image_obj, max_width, max_height in records:
            _check_cancel(cancel_event)
            pixels = int(image_obj.get("/Width", 0)) * int(image_obj.get("/Height", 0))
            if pixels <= 0 or sampled_pixels + pixels > _MAX_ESTIMATE_SAMPLE_PIXELS:
                continue
            input_bytes = _encoded_stream_length(image_obj)
            try:
                transformed = _transcode_image(
                    image_obj,
                    max_width=max_width,
                    max_height=max_height,
                    jpeg_quality=jpeg_quality,
                    grayscale=request.convert_to_grayscale,
                )
            except (
                OSError,
                ValueError,
                pikepdf.PdfError,
                pikepdf.UnsupportedImageTypeError,
                pikepdf.DecompressionBombError,
            ):
                transformed = None
            if transformed is None:
                skipped_samples += 1
                continue
            encoded_size = len(transformed[0])
            sampled_pixels += pixels
            sampled_input_bytes += input_bytes
            sampled_output_bytes += (
                encoded_size
                if request.convert_to_grayscale
                else min(input_bytes or encoded_size, encoded_size)
            )
            if sampled_pixels >= _MAX_ESTIMATE_SAMPLE_PIXELS:
                break
        sample_elapsed = time.perf_counter() - sample_started

    if sampled_input_bytes:
        image_ratio = sampled_output_bytes / sampled_input_bytes
    else:
        image_ratio = 0.32 if request.compress_pdf else 0.82
        if request.convert_to_grayscale:
            image_ratio *= 0.65
    structural_bytes = max(0, file_size - min(file_size, stream_bytes))
    estimated_images = round(stream_bytes * max(0.03, min(1.25, image_ratio)))
    estimate = structural_bytes + estimated_images
    if not request.convert_to_grayscale:
        estimate = min(file_size, estimate)
    estimate = max(1024, estimate)
    if not records and request.compress_pdf:
        warnings.append(
            f"{path.name} contains no large raster images, so compression savings may be limited."
        )
    if request.convert_to_grayscale and skipped_samples:
        warnings.append(
            f"The grayscale estimate for {path.name} excludes some masked, monochrome, or unusually encoded images."
        )
    return estimate, total_pixels, sampled_pixels, sample_elapsed, tuple(warnings)


def estimate_job(
    request: JobRequest,
    *,
    progress: ProgressCallback | None = None,
    cancel_event: object | None = None,
) -> EstimateResult:
    """Estimate runtime and aggregate output size without creating output files."""

    from .validation import preflight

    input_size = 0
    warnings: list[str] = []
    try:
        _emit(
            request,
            progress,
            cancel_event,
            stage="estimate",
            percent=0,
            message="Checking the job before estimating",
        )
        report = preflight(request, for_estimate=True)
        input_size = sum(info.size_bytes for info in report.input_info)
        if not report.valid:
            return EstimateResult(
                job_id=request.job_id,
                success=False,
                input_size_bytes=input_size,
                error=" ".join(report.errors),
                warnings=report.warnings,
            )
        warnings.extend(report.warnings)
        file_estimates: list[int] = []
        total_pixels = 0
        sampled_pixels = 0
        sample_seconds = 0.0
        if request.compress_pdf or request.convert_to_grayscale:
            for index, (path, password) in enumerate(
                zip(request.input_paths, request.passwords, strict=True),
                start=1,
            ):
                _emit(
                    request,
                    progress,
                    cancel_event,
                    stage="estimate",
                    percent=10 + (70 * (index - 1) / len(request.input_paths)),
                    message=f"Estimating {path.name} ({index} of {len(request.input_paths)})",
                    path=path,
                    file_index=index,
                    file_count=len(request.input_paths),
                    phase_percent=0,
                )
                estimated, pixels, sampled, elapsed, file_warnings = _estimate_pdf_compression(
                    request,
                    path,
                    password,
                    cancel_event=cancel_event,
                )
                file_estimates.append(estimated)
                total_pixels += pixels
                sampled_pixels += sampled
                sample_seconds += elapsed
                warnings.extend(file_warnings)
                _emit(
                    request,
                    progress,
                    cancel_event,
                    stage="estimate",
                    percent=10 + (70 * index / len(request.input_paths)),
                    message=f"Estimated {path.name}",
                    path=path,
                    file_index=index,
                    file_count=len(request.input_paths),
                    phase_percent=100,
                )
        else:
            file_estimates = [info.size_bytes for info in report.input_info]

        output_bytes = sum(file_estimates)
        if request.structure_mode is StructureMode.SPLIT:
            output_bytes = round(output_bytes * (1 + 0.004 * len(report.output_paths)))
        elif request.structure_mode is StructureMode.JOIN:
            output_bytes = round(output_bytes * 0.995)
        output_bytes = max(1024, output_bytes)

        passes = 5 if request.compress_pdf else 3 if request.convert_to_grayscale else 2
        io_seconds = input_size * passes / (32 * 1024 * 1024)
        if sampled_pixels and sample_seconds:
            image_seconds = sample_seconds * total_pixels / sampled_pixels
            confidence = "medium"
        else:
            image_seconds = total_pixels / 12_000_000
            confidence = "low"
        estimated_seconds = round(max(1.0, io_seconds + image_seconds), 1)
        warnings.append(
            "Runtime and output size are approximate; PDF content and computer speed can change the result."
        )
        _emit(
            request,
            progress,
            cancel_event,
            stage="estimate",
            percent=100,
            message="Estimate ready",
            kind="estimate_complete",
        )
        return EstimateResult(
            job_id=request.job_id,
            success=True,
            estimated_seconds=estimated_seconds,
            estimated_output_bytes=output_bytes,
            input_size_bytes=input_size,
            confidence=confidence,
            warnings=_unique(warnings),
        )
    except _Cancelled:
        return EstimateResult(
            job_id=request.job_id,
            success=False,
            cancelled=True,
            input_size_bytes=input_size,
            error="The estimate was cancelled.",
            warnings=_unique(warnings),
        )
    except Exception as exc:  # noqa: BLE001 - estimation must return a stable result.
        detail = _redact_secrets(exc, request.passwords).strip()
        return EstimateResult(
            job_id=request.job_id,
            success=False,
            input_size_bytes=input_size,
            error=f"The estimate could not be completed. {detail}".strip(),
            warnings=_unique(warnings),
        )


def _write_single_baseline(
    request: JobRequest,
    output_path: Path,
    warnings: list[str],
    progress: ProgressCallback | None,
    cancel_event: object | None,
    final_path: Path,
    *,
    input_index: int = 0,
    file_count: int = 1,
) -> None:
    input_path = request.input_paths[input_index]
    file_index = input_index + 1
    start_percent = 15 + (35 * input_index / file_count)
    end_percent = 15 + (35 * file_index / file_count)
    _emit(
        request,
        progress,
        cancel_event,
        stage="process",
        percent=start_percent,
        message=f"Processing {input_path.name}",
        path=input_path,
        file_index=file_index,
        file_count=file_count,
        phase_percent=0,
    )
    if (
        request.convert_to_grayscale
        and not request.compress_pdf
        and not request.remove_password
    ):
        _copy_exclusive(input_path, output_path, cancel_event)
        _emit(
            request,
            progress,
            cancel_event,
            stage="process",
            percent=end_percent,
            message=f"Prepared {final_path.name} for grayscale conversion",
            path=final_path,
            file_index=file_index,
            file_count=file_count,
            phase_percent=100,
        )
        return
    password = (
        request.passwords[input_index]
        if input_index < len(request.passwords)
        else None
    )
    builder = (
        pikepdf.JobBuilder().input(input_path, password=password).output(output_path)
    )
    if request.remove_password:
        builder.decrypt()
    _run_qpdf_job(builder, warnings, cancel_event=cancel_event)
    _emit(
        request,
        progress,
        cancel_event,
        stage="process",
        percent=end_percent,
        message=(
            f"Prepared {final_path.name} for compression"
            if request.compress_pdf
            else f"Prepared {final_path.name} for grayscale conversion"
            if request.convert_to_grayscale
            else f"Prepared {final_path.name}"
        ),
        path=final_path,
        file_index=file_index,
        file_count=file_count,
        phase_percent=100,
    )


def _copy_document_metadata(destination: pikepdf.Pdf, source: pikepdf.Pdf) -> None:
    if source.trailer.get("/Info") is not None:
        destination.trailer["/Info"] = destination.copy_foreign(source.trailer["/Info"])
    if source.Root.get("/Metadata") is not None:
        destination.Root["/Metadata"] = destination.copy_foreign(source.Root["/Metadata"])
    if source.Root.get("/Lang") is not None:
        destination.Root["/Lang"] = str(source.Root["/Lang"])
    for key in ("/PageLayout", "/PageMode"):
        value = source.Root.get(key)
        if value is not None:
            destination.Root[key] = pikepdf.Name(str(value))


def _set_exact_page_labels(destination: pikepdf.Pdf, labels: Sequence[str]) -> None:
    if not labels:
        return
    tree = pikepdf.NumberTree.new(destination)
    destination.Root["/PageLabels"] = tree.obj
    for index, label in enumerate(labels):
        tree[index] = pikepdf.Dictionary(P=str(label))


def _page_index_map(pdf: pikepdf.Pdf) -> dict[tuple[int, int], int]:
    return {page.objgen: index for index, page in enumerate(pdf.pages)}


def _resolve_named_destination(pdf: pikepdf.Pdf, destination: Any) -> Any:
    if isinstance(destination, pikepdf.Array):
        return destination
    key = str(destination)
    names = pdf.Root.get("/Names")
    if names is not None and names.get("/Dests") is not None:
        try:
            tree = pikepdf.NameTree(names["/Dests"])
            for candidate_key in (key, key.lstrip("/")):
                if candidate_key in tree:
                    value = tree[candidate_key]
                    return value.get("/D", value) if hasattr(value, "get") else value
        except (KeyError, TypeError, ValueError):
            pass
    legacy = pdf.Root.get("/Dests")
    if legacy is not None:
        for candidate in (destination, pikepdf.Name(key) if key.startswith("/") else None):
            if candidate is None:
                continue
            try:
                value = legacy.get(candidate)
            except (AttributeError, TypeError, ValueError):
                value = None
            if value is not None:
                return value.get("/D", value) if hasattr(value, "get") else value
    return destination


def _outline_source_page(
    source: pikepdf.Pdf,
    item: pikepdf.OutlineItem,
    pages_by_objgen: dict[tuple[int, int], int],
) -> int | None:
    destination = item.destination
    if destination is None and item.action is not None:
        try:
            if item.action.get("/S") == pikepdf.Name("/GoTo"):
                destination = item.action.get("/D")
        except (AttributeError, ValueError):
            destination = None
    if destination is None:
        return None
    destination = _resolve_named_destination(source, destination)
    if not isinstance(destination, pikepdf.Array) or not destination:
        return None
    try:
        return pages_by_objgen.get(destination[0].objgen)
    except (AttributeError, ValueError):
        return None


def _copy_uri_action(item: pikepdf.OutlineItem) -> pikepdf.Dictionary | None:
    action = item.action
    if action is None:
        return None
    try:
        if action.get("/S") != pikepdf.Name("/URI") or action.get("/URI") is None:
            return None
        return pikepdf.Dictionary(S=pikepdf.Name("/URI"), URI=str(action["/URI"]))
    except (AttributeError, KeyError, ValueError):
        return None


def _copy_outline_item(
    source: pikepdf.Pdf,
    item: pikepdf.OutlineItem,
    pages_by_objgen: dict[tuple[int, int], int],
    *,
    destination_offset: int,
    selected_start: int | None,
    selected_end: int | None,
) -> tuple[pikepdf.OutlineItem | None, int]:
    skipped = 0
    children: list[pikepdf.OutlineItem] = []
    for child in item.children:
        copied_child, child_skipped = _copy_outline_item(
            source,
            child,
            pages_by_objgen,
            destination_offset=destination_offset,
            selected_start=selected_start,
            selected_end=selected_end,
        )
        skipped += child_skipped
        if copied_child is not None:
            children.append(copied_child)

    source_page = _outline_source_page(source, item, pages_by_objgen)
    destination_page: int | None = None
    if source_page is not None:
        if selected_start is None or selected_end is None:
            destination_page = source_page + destination_offset
        elif selected_start <= source_page <= selected_end:
            destination_page = source_page - selected_start

    external_action = _copy_uri_action(item)
    if destination_page is None and external_action is None and not children:
        return None, skipped + 1

    copied = pikepdf.OutlineItem(
        item.title,
        destination=destination_page,
        action=external_action,
        color=item.color,
        flags=item.flags,
    )
    copied.is_closed = item.is_closed
    copied.children.extend(children)
    return copied, skipped


def _read_outline_items(
    source: pikepdf.Pdf,
    *,
    destination_offset: int,
    selected_start: int | None = None,
    selected_end: int | None = None,
) -> tuple[list[pikepdf.OutlineItem], int]:
    pages_by_objgen = _page_index_map(source)
    copied: list[pikepdf.OutlineItem] = []
    skipped = 0
    with source.open_outline(strict=False) as outline:
        for item in outline.root:
            copied_item, item_skipped = _copy_outline_item(
                source,
                item,
                pages_by_objgen,
                destination_offset=destination_offset,
                selected_start=selected_start,
                selected_end=selected_end,
            )
            skipped += item_skipped
            if copied_item is not None:
                copied.append(copied_item)
    return copied, skipped


def _copy_result_warnings(result: pikepdf.PageCopyResult, source_name: str) -> list[str]:
    warnings: list[str] = []
    if result.renamed_fields:
        warnings.append(
            f"{source_name}: {len(result.renamed_fields)} form field name(s) were renamed to avoid collisions."
        )
    if result.partial_fields:
        warnings.append(
            f"{source_name}: {len(result.partial_fields)} form field tree(s) span pages outside this output."
        )
    if result.renamed_dests:
        warnings.append(
            f"{source_name}: {len(result.renamed_dests)} named destination(s) were renamed to avoid collisions."
        )
    return warnings


def _save_callback(
    request: JobRequest,
    progress: ProgressCallback | None,
    cancel_event: object | None,
    *,
    start_percent: float,
    span: float,
    display_path: Path,
    file_index: int | None = None,
    file_count: int | None = None,
) -> Callable[[int], None]:
    def callback(value: int) -> None:
        _emit(
            request,
            progress,
            cancel_event,
            stage="write",
            percent=start_percent + (span * value / 100),
            message=f"Writing {display_path.name}",
            path=display_path,
            file_index=file_index,
            file_count=file_count,
            phase_percent=value,
        )

    return callback


def _max_pdf_version(sources: Sequence[pikepdf.Pdf]) -> str:
    def key(version: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in version.split("."))
        except ValueError:
            return (1, 7)

    return max((source.pdf_version for source in sources), key=key, default="1.7")


def _build_join_baseline(
    request: JobRequest,
    output_path: Path,
    progress: ProgressCallback | None,
    cancel_event: object | None,
) -> list[str]:
    warnings: list[str] = []
    final_path = build_output_paths(request)[0]
    with ExitStack() as stack:
        sources = [
            stack.enter_context(
                pikepdf.Pdf.open(
                    path,
                    password=request.passwords[index] or "",
                    suppress_warnings=True,
                    attempt_recovery=False,
                )
            )
            for index, path in enumerate(request.input_paths)
        ]
        destination = stack.enter_context(pikepdf.Pdf.new())
        labels: list[str] = []
        offsets: list[int] = []
        page_offset = 0
        for index, (source, path) in enumerate(zip(sources, request.input_paths, strict=True)):
            _check_cancel(cancel_event)
            offsets.append(page_offset)
            result = destination.add_pages_from(source, forms="preserve")
            warnings.extend(_copy_result_warnings(result, Path(path).name))
            labels.extend(str(page.label) for page in source.pages)
            page_offset += len(source.pages)
            _emit(
                request,
                progress,
                cancel_event,
                stage="join",
                percent=15 + (30 * (index + 1) / len(sources)),
                message=f"Added {Path(path).name}",
                path=Path(path),
                file_index=index + 1,
                file_count=len(sources),
                phase_percent=100,
            )

        _copy_document_metadata(destination, sources[0])
        _set_exact_page_labels(destination, labels)
        with destination.open_outline() as destination_outline:
            for source, path, offset in zip(sources, request.input_paths, offsets, strict=True):
                try:
                    children, skipped = _read_outline_items(source, destination_offset=offset)
                    wrapper = pikepdf.OutlineItem(Path(path).name, offset)
                    wrapper.children.extend(children)
                    destination_outline.root.append(wrapper)
                    if skipped:
                        warnings.append(
                            f"{Path(path).name}: {skipped} bookmark(s) with unsupported destinations were not copied."
                        )
                except (pikepdf.OutlineStructureError, ValueError, RuntimeError):
                    destination_outline.root.append(pikepdf.OutlineItem(Path(path).name, offset))
                    warnings.append(f"{Path(path).name}: malformed bookmarks could not be copied.")

        destination.save(
            output_path,
            min_version=_max_pdf_version(sources),
            fix_metadata_version=False,
            encryption=None,
            progress=_save_callback(
                request,
                progress,
                cancel_event,
                start_percent=45,
                span=10,
                display_path=final_path,
                file_index=1,
                file_count=1,
            ),
        )
    return warnings


def _build_split_baselines(
    request: JobRequest,
    ranges: Sequence[tuple[int, int]],
    output_paths: Sequence[Path],
    progress: ProgressCallback | None,
    cancel_event: object | None,
) -> list[str]:
    warnings: list[str] = []
    final_paths = build_output_paths(request)
    password = request.passwords[0] if request.passwords else None
    with pikepdf.Pdf.open(
        request.input_paths[0],
        password=password or "",
        suppress_warnings=True,
        attempt_recovery=False,
    ) as source:
        labels = [str(page.label) for page in source.pages]
        for output_index, ((start, end), output_path) in enumerate(
            zip(ranges, output_paths, strict=True)
        ):
            _check_cancel(cancel_event)
            with pikepdf.Pdf.new() as destination:
                result = destination.add_pages_from(
                    source,
                    pages=range(start - 1, end),
                    forms="preserve",
                )
                warnings.extend(_copy_result_warnings(result, Path(request.input_paths[0]).name))
                _copy_document_metadata(destination, source)
                _set_exact_page_labels(destination, labels[start - 1 : end])
                try:
                    outline_items, skipped = _read_outline_items(
                        source,
                        destination_offset=0,
                        selected_start=start - 1,
                        selected_end=end - 1,
                    )
                    with destination.open_outline() as destination_outline:
                        destination_outline.root.extend(outline_items)
                    if skipped:
                        warnings.append(
                            f"{final_paths[output_index].name}: {skipped} bookmark(s) outside this segment were omitted."
                        )
                except (pikepdf.OutlineStructureError, ValueError, RuntimeError):
                    warnings.append(
                        f"{final_paths[output_index].name}: malformed bookmarks could not be copied."
                    )

                segment_start = 15 + (35 * output_index / len(output_paths))
                destination.save(
                    output_path,
                    min_version=source.pdf_version,
                    fix_metadata_version=False,
                    encryption=None,
                    progress=_save_callback(
                        request,
                        progress,
                        cancel_event,
                        start_percent=segment_start,
                        span=35 / len(output_paths),
                        display_path=final_paths[output_index],
                        file_index=output_index + 1,
                        file_count=len(output_paths),
                    ),
                )
    return warnings


def _compress_one(
    request: JobRequest,
    source: Path,
    work_dir: Path,
    index: int,
    warnings: list[str],
    progress: ProgressCallback | None,
    cancel_event: object | None,
    *,
    final_path: Path,
    file_count: int,
) -> Path:
    file_index = index + 1
    level = CompressionLevel.ULTRA
    _check_cancel(cancel_event)
    lossless = work_dir / f"{index:02d}-lossless.pdf"
    lossless_builder = pikepdf.JobBuilder().input(source).output(lossless)
    _configure_lossless_compression(lossless_builder)
    _run_qpdf_job(
        lossless_builder,
        warnings,
        request=request,
        progress=progress,
        cancel_event=cancel_event,
        final_path=final_path,
        file_index=file_index,
        file_count=file_count,
        phase_start=0,
        phase_end=20,
    )

    _check_cancel(cancel_event)
    optimized = work_dir / f"{index:02d}-optimized.pdf"
    candidates = [source, lossless]
    try:
        optimized_builder = pikepdf.JobBuilder().input(lossless).output(optimized)
        _configure_lossless_compression(optimized_builder)
        _configure_image_optimization(optimized_builder, level)
        _run_qpdf_job(
            optimized_builder,
            warnings,
            request=request,
            progress=progress,
            cancel_event=cancel_event,
            final_path=final_path,
            file_index=file_index,
            file_count=file_count,
            phase_start=20,
            phase_end=40,
        )
        candidates.append(optimized)
    except (pikepdf.PdfError, pikepdf.JobUsageError, RuntimeError, OSError):
        optimized.unlink(missing_ok=True)
        warnings.append(
            f"Some images in {final_path.name} could not be recompressed; safe lossless compression was retained."
        )

    transformed = work_dir / f"{index:02d}-transformed.pdf"
    _transform_pdf_images(
        request,
        lossless,
        transformed,
        warnings,
        progress,
        cancel_event,
        final_path=final_path,
        file_index=file_index,
        file_count=file_count,
    )
    transformed_candidates = [transformed]
    packed = work_dir / f"{index:02d}-transformed-packed.pdf"
    try:
        packed_builder = pikepdf.JobBuilder().input(transformed).output(packed)
        _configure_lossless_compression(packed_builder)
        _run_qpdf_job(
            packed_builder,
            warnings,
            request=request,
            progress=progress,
            cancel_event=cancel_event,
            final_path=final_path,
            file_index=file_index,
            file_count=file_count,
            phase_start=88,
            phase_end=98,
        )
        transformed_candidates.append(packed)
    except (pikepdf.PdfError, pikepdf.JobUsageError, RuntimeError, OSError):
        packed.unlink(missing_ok=True)
        warnings.append(
            f"Final packing could not reduce {final_path.name}; the verified transformed PDF was retained."
        )
    if request.convert_to_grayscale:
        candidates = transformed_candidates
    else:
        candidates.extend(transformed_candidates)

    _emit_compression(
        request,
        progress,
        cancel_event,
        final_path=final_path,
        file_index=file_index,
        file_count=file_count,
        phase_percent=100,
    )
    return min(candidates, key=lambda path: path.stat().st_size)


def _grayscale_one(
    request: JobRequest,
    source: Path,
    work_dir: Path,
    index: int,
    warnings: list[str],
    progress: ProgressCallback | None,
    cancel_event: object | None,
    *,
    final_path: Path,
    file_count: int,
) -> Path:
    """Convert colours without applying Ultra downsampling or compression."""

    file_index = index + 1
    _emit_compression(
        request,
        progress,
        cancel_event,
        final_path=final_path,
        file_index=file_index,
        file_count=file_count,
        phase_percent=0,
    )
    transformed = work_dir / f"{index:02d}-grayscale.pdf"
    _transform_pdf_images(
        request,
        source,
        transformed,
        warnings,
        progress,
        cancel_event,
        final_path=final_path,
        file_index=file_index,
        file_count=file_count,
    )
    _emit_compression(
        request,
        progress,
        cancel_event,
        final_path=final_path,
        file_index=file_index,
        file_count=file_count,
        phase_percent=100,
    )
    return transformed


def _prepare_ready_outputs(
    request: JobRequest,
    baselines: Sequence[Path],
    final_paths: Sequence[Path],
    work_dir: Path,
    warnings: list[str],
    progress: ProgressCallback | None,
    cancel_event: object | None,
) -> tuple[Path, ...]:
    ready_dir = work_dir / "ready"
    ready_dir.mkdir()
    ready: list[Path] = []
    for index, (baseline, final_path) in enumerate(zip(baselines, final_paths, strict=True)):
        _check_cancel(cancel_event)
        candidate = baseline
        if request.compress_pdf:
            candidate = _compress_one(
                request,
                baseline,
                work_dir,
                index,
                warnings,
                progress,
                cancel_event,
                final_path=final_path,
                file_count=len(baselines),
            )
            if (
                request.structure_mode is StructureMode.NEITHER
                and not request.remove_password
                and not request.convert_to_grayscale
                and request.input_paths[index].stat().st_size < candidate.stat().st_size
            ):
                candidate = request.input_paths[index]
        elif request.convert_to_grayscale:
            candidate = _grayscale_one(
                request,
                baseline,
                work_dir,
                index,
                warnings,
                progress,
                cancel_event,
                final_path=final_path,
                file_count=len(baselines),
            )

        ready_path = ready_dir / final_path.name
        shutil.copyfile(candidate, ready_path)
        ready.append(ready_path)
        _emit(
            request,
            progress,
            cancel_event,
            stage=(
                "compress"
                if request.compress_pdf
                else "grayscale"
                if request.convert_to_grayscale
                else "write"
            ),
            percent=55 + (25 * (index + 1) / len(baselines)),
            message=f"Prepared {final_path.name}",
            path=final_path,
            file_index=index + 1,
            file_count=len(baselines),
            phase_percent=100,
        )
    return tuple(ready)


def _verify_outputs(
    request: JobRequest,
    paths: Sequence[Path],
    final_paths: Sequence[Path],
    expected_page_counts: Sequence[int],
    progress: ProgressCallback | None,
    cancel_event: object | None,
) -> list[str]:
    warnings: list[str] = []
    items = zip(paths, final_paths, expected_page_counts, strict=True)
    for index, (path, final_path, expected_pages) in enumerate(items):
        _check_cancel(cancel_event)
        if not path.is_file() or path.stat().st_size == 0:
            raise RuntimeError(f"The staged output was not created: {path.name}")
        with pikepdf.Pdf.open(path, suppress_warnings=True, attempt_recovery=False) as pdf:
            if pdf.is_encrypted:
                raise RuntimeError(f"The staged output is unexpectedly encrypted: {path.name}")
            if len(pdf.pages) != expected_pages:
                raise RuntimeError(
                    f"The staged output has {len(pdf.pages)} pages; expected {expected_pages}: {path.name}"
                )
            syntax_warnings = [str(item) for item in pdf.check_pdf_syntax()]
            if syntax_warnings:
                raise RuntimeError(
                    f"The staged output failed PDF syntax validation: {path.name}: {'; '.join(syntax_warnings)}"
                )
            if pdf.Root.get("/AcroForm") is not None:
                pdf.acroform.validate()
            warnings.extend(str(item) for item in pdf.get_warnings())
        _emit(
            request,
            progress,
            cancel_event,
            stage="verify",
            percent=80 + (10 * (index + 1) / len(paths)),
            message=f"Verified {final_path.name}",
            path=final_path,
            file_index=index + 1,
            file_count=len(paths),
            phase_percent=100,
        )
    return warnings


def _copy_exclusive(
    source: Path,
    destination: Path,
    cancel_event: object | None,
) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        while chunk := reader.read(_READ_CHUNK):
            _check_cancel(cancel_event)
            writer.write(chunk)
        writer.flush()
        os.fsync(writer.fileno())


def _rename_no_replace(source: Path, destination: Path) -> None:
    if os.name == "nt":
        source.rename(destination)  # Windows rename fails if destination exists.
        return
    os.link(source, destination)
    source.unlink()


def _commit_outputs(
    request: JobRequest,
    ready_paths: Sequence[Path],
    final_paths: Sequence[Path],
    progress: ProgressCallback | None,
    cancel_event: object | None,
) -> tuple[Path, ...]:
    token = uuid.uuid4().hex
    partials: list[Path] = []
    committed: list[Path] = []
    try:
        for index, (ready, final) in enumerate(zip(ready_paths, final_paths, strict=True)):
            _check_cancel(cancel_event)
            if final.exists():
                raise FileExistsError(f"An output file already exists: {final.name}")
            partial = final.parent / f".{final.name}.{token}.partial"
            _copy_exclusive(ready, partial, cancel_event)
            partials.append(partial)
            _emit(
                request,
                progress,
                cancel_event,
                stage="commit",
                percent=90 + (5 * (index + 1) / len(ready_paths)),
                message=f"Finalizing {final.name}",
                path=final,
                file_index=index + 1,
                file_count=len(ready_paths),
                phase_percent=100,
            )

        for partial, final in zip(partials, final_paths, strict=True):
            _check_cancel(cancel_event)
            _rename_no_replace(partial, final)
            committed.append(final)
        return tuple(committed)
    except Exception:
        for path in reversed(committed):
            path.unlink(missing_ok=True)
        raise
    finally:
        for path in partials:
            path.unlink(missing_ok=True)


def process_job(
    request: JobRequest,
    *,
    progress: ProgressCallback | None = None,
    cancel_event: object | None = None,
) -> JobResult:
    """Execute one validated job and commit outputs only after full verification."""

    from .validation import preflight

    work_dir: Path | None = None
    created_staging_parent = False
    runtime_warnings: list[str] = []
    original_size = 0
    try:
        _emit(
            request,
            progress,
            cancel_event,
            stage="preflight",
            percent=0,
            message="Validating inputs and output paths",
        )
        report = preflight(request)
        original_size = sum(info.size_bytes for info in report.input_info)
        if not report.valid:
            return JobResult(
                job_id=request.job_id,
                success=False,
                error=" ".join(report.errors),
                warnings=report.warnings,
                original_size_bytes=original_size,
            )
        runtime_warnings.extend(report.warnings)
        _emit(
            request,
            progress,
            cancel_event,
            stage="preflight",
            percent=8,
            message="Preflight completed",
        )

        source_hashes = {
            path: _sha256_file(path, cancel_event) for path in request.input_paths
        }
        _emit(
            request,
            progress,
            cancel_event,
            stage="preflight",
            percent=12,
            message="Input files are ready",
        )

        staging_parent = Path(request.staging_dir)
        if not staging_parent.exists():
            staging_parent.mkdir(parents=True, exist_ok=False)
            created_staging_parent = True
        work_dir = Path(tempfile.mkdtemp(prefix="pros-job-", dir=staging_parent))

        baseline_paths = tuple(
            work_dir / f"{index:02d}-baseline.pdf"
            for index in range(len(report.output_paths))
        )
        mode = StructureMode(request.structure_mode)
        if mode is StructureMode.NEITHER:
            for input_index, (baseline_path, final_path) in enumerate(
                zip(baseline_paths, report.output_paths, strict=True)
            ):
                _write_single_baseline(
                    request,
                    baseline_path,
                    runtime_warnings,
                    progress,
                    cancel_event,
                    final_path,
                    input_index=input_index,
                    file_count=len(baseline_paths),
                )
            expected_page_counts = tuple(
                info.page_count or 0 for info in report.input_info
            )
        elif mode is StructureMode.JOIN:
            runtime_warnings.extend(
                _build_join_baseline(
                    request,
                    baseline_paths[0],
                    progress,
                    cancel_event,
                )
            )
            expected_page_counts = (
                sum(info.page_count or 0 for info in report.input_info),
            )
        else:
            runtime_warnings.extend(
                _build_split_baselines(
                    request,
                    report.split_ranges,
                    baseline_paths,
                    progress,
                    cancel_event,
                )
            )
            expected_page_counts = tuple(end - start + 1 for start, end in report.split_ranges)

        ready_paths = _prepare_ready_outputs(
            request,
            baseline_paths,
            report.output_paths,
            work_dir,
            runtime_warnings,
            progress,
            cancel_event,
        )
        runtime_warnings.extend(
            _verify_outputs(
                request,
                ready_paths,
                report.output_paths,
                expected_page_counts,
                progress,
                cancel_event,
            )
        )

        for source, before_hash in source_hashes.items():
            if _sha256_file(source, cancel_event) != before_hash:
                raise RuntimeError(f"A source file changed during processing: {source.name}")

        # Calculate all potentially failing result data before commit. After this
        # boundary, normal exceptions must not turn committed outputs into a
        # reported failure.
        output_size = sum(path.stat().st_size for path in ready_paths)
        reduction = None
        if request.compress_pdf and original_size:
            reduction = round((original_size - output_size) * 100 / original_size, 2)
        committed = _commit_outputs(
            request,
            ready_paths,
            report.output_paths,
            progress,
            cancel_event,
        )
        result = JobResult(
            job_id=request.job_id,
            success=True,
            output_paths=committed,
            warnings=_unique(runtime_warnings),
            original_size_bytes=original_size,
            output_size_bytes=output_size,
            reduction_percent=reduction,
        )
        try:
            # A late cancellation or closed UI queue must not convert an already
            # committed job into a false cancelled/failed result.
            _emit(
                request,
                progress,
                None,
                stage="complete",
                percent=100,
                message="Processing completed successfully",
                kind="complete",
            )
        except Exception:  # noqa: BLE001, S110 - terminal delivery is best-effort.
            pass
        return result
    except _Cancelled:
        return JobResult(
            job_id=request.job_id,
            success=False,
            cancelled=True,
            error="The job was cancelled and all temporary output was removed.",
            warnings=_unique(runtime_warnings),
            original_size_bytes=original_size,
        )
    except Exception as exc:  # noqa: BLE001 - engine boundary returns a JobResult.
        detail = _redact_secrets(exc, request.passwords).strip()
        return JobResult(
            job_id=request.job_id,
            success=False,
            error=f"Processing could not be completed. {detail}".strip(),
            warnings=_unique(runtime_warnings),
            original_size_bytes=original_size,
        )
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
        if created_staging_parent:
            try:
                Path(request.staging_dir).rmdir()
            except OSError:
                pass


__all__ = ["ProgressCallback", "estimate_job", "inspect_pdf", "process_job"]
