"""Offline pikepdf/qpdf engine for atomic PROS PDF jobs."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Iterable, Sequence
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pikepdf

from .models import JobRequest, JobResult, PdfInfo, StructureMode

ProgressCallback = Callable[[dict[str, object]], None]

_READ_CHUNK = 1024 * 1024


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
) -> None:
    _check_cancel(cancel_event)
    if progress is None:
        return
    progress(
        {
            "job_id": request.job_id,
            "kind": kind,
            "stage": stage,
            "percent": max(0, min(100, round(percent))),
            "message": message,
            "path": str(path) if path is not None else None,
        }
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


def _configure_image_optimization(builder: pikepdf.JobBuilder) -> pikepdf.JobBuilder:
    return builder.optimize_images(
        min_width=256,
        min_height=256,
        min_area=65_536,
        keep_inline_images=True,
        jpeg_quality=90,
    )


def _run_qpdf_job(builder: pikepdf.JobBuilder, warnings: list[str]) -> None:
    job = builder.run()
    if job.has_warnings:
        warnings.append("qpdf reported a recoverable warning while writing the PDF.")


def _write_single_baseline(
    request: JobRequest,
    output_path: Path,
    warnings: list[str],
) -> None:
    password = request.passwords[0] if request.passwords else None
    builder = pikepdf.JobBuilder().input(request.input_paths[0], password=password).output(output_path)
    if request.remove_password:
        builder.decrypt()
    _run_qpdf_job(builder, warnings)


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
    path: Path,
) -> Callable[[int], None]:
    def callback(value: int) -> None:
        _emit(
            request,
            progress,
            cancel_event,
            stage="write",
            percent=start_percent + (span * value / 100),
            message=f"Writing {path.name}",
            path=path,
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
                path=output_path,
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
                            f"{output_path.name}: {skipped} bookmark(s) outside this segment were omitted."
                        )
                except (pikepdf.OutlineStructureError, ValueError, RuntimeError):
                    warnings.append(f"{output_path.name}: malformed bookmarks could not be copied.")

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
                        path=output_path,
                    ),
                )
    return warnings


def _compress_one(
    source: Path,
    work_dir: Path,
    index: int,
    warnings: list[str],
    cancel_event: object | None,
) -> Path:
    _check_cancel(cancel_event)
    lossless = work_dir / f"{index:02d}-lossless.pdf"
    lossless_builder = pikepdf.JobBuilder().input(source).output(lossless)
    _configure_lossless_compression(lossless_builder)
    _run_qpdf_job(lossless_builder, warnings)

    _check_cancel(cancel_event)
    optimized = work_dir / f"{index:02d}-optimized.pdf"
    try:
        optimized_builder = pikepdf.JobBuilder().input(lossless).output(optimized)
        _configure_lossless_compression(optimized_builder)
        _configure_image_optimization(optimized_builder)
        _run_qpdf_job(optimized_builder, warnings)
    except (pikepdf.PdfError, pikepdf.JobUsageError, RuntimeError, OSError) as exc:
        optimized.unlink(missing_ok=True)
        warnings.append(
            f"Image recompression was skipped for {source.name}; lossless compression was retained ({type(exc).__name__})."
        )
        return lossless
    return min((source, lossless, optimized), key=lambda path: path.stat().st_size)


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
            _emit(
                request,
                progress,
                cancel_event,
                stage="compress",
                percent=55 + (25 * index / len(baselines)),
                message=f"Compressing {final_path.name}",
                path=final_path,
            )
            candidate = _compress_one(baseline, work_dir, index, warnings, cancel_event)
            if (
                request.structure_mode is StructureMode.NEITHER
                and not request.remove_password
                and request.input_paths[0].stat().st_size < candidate.stat().st_size
            ):
                candidate = request.input_paths[0]

        ready_path = ready_dir / final_path.name
        shutil.copyfile(candidate, ready_path)
        ready.append(ready_path)
        _emit(
            request,
            progress,
            cancel_event,
            stage="compress" if request.compress_pdf else "write",
            percent=55 + (25 * (index + 1) / len(baselines)),
            message=f"Prepared {final_path.name}",
            path=final_path,
        )
    return tuple(ready)


def _verify_outputs(
    request: JobRequest,
    paths: Sequence[Path],
    expected_page_counts: Sequence[int],
    progress: ProgressCallback | None,
    cancel_event: object | None,
) -> list[str]:
    warnings: list[str] = []
    for index, (path, expected_pages) in enumerate(zip(paths, expected_page_counts, strict=True)):
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
            message=f"Verified {path.name}",
            path=path,
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
                message=f"Staged {final.name} for commit",
                path=final,
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
            message="Source integrity recorded",
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
            _emit(
                request,
                progress,
                cancel_event,
                stage="process",
                percent=15,
                message=f"Processing {request.input_paths[0].name}",
                path=request.input_paths[0],
            )
            _write_single_baseline(request, baseline_paths[0], runtime_warnings)
            expected_page_counts = (report.input_info[0].page_count or 0,)
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
        return JobResult(
            job_id=request.job_id,
            success=False,
            error=f"{type(exc).__name__}: {_redact_secrets(exc, request.passwords)}",
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


__all__ = ["ProgressCallback", "inspect_pdf", "process_job"]
