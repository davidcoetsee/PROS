"""Pure validation helpers and authoritative job preflight."""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path

from .models import (
    CompressionLevel,
    JobRequest,
    PdfInfo,
    PreflightReport,
    StructureMode,
)
from .naming import (
    build_output_paths,
    normalize_output_base,
    suggest_output_bases,
)

MAX_JOIN_INPUTS = 12
MAX_SEPARATE_INPUTS = MAX_JOIN_INPUTS
MAX_SPLIT_OUTPUTS = 12
LARGE_FILE_NOTICE_BYTES = 120 * 1024 * 1024
LARGE_FILE_ACCEPTANCE_BYTES = 180 * 1024 * 1024

_INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*]|[\x00-\x1f]')
_RESERVED_WINDOWS_STEMS = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{number}" for number in range(1, 10)),
    *(f"LPT{number}" for number in range(1, 10)),
}


def _unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def validate_split_points(
    page_count: int,
    split_points: Sequence[int],
    max_outputs: int = MAX_SPLIT_OUTPUTS,
) -> tuple[str, ...]:
    """Return all errors in a proposed list of inclusive segment-ending pages."""

    errors: list[str] = []
    if isinstance(page_count, bool) or not isinstance(page_count, int) or page_count < 1:
        return ("The source PDF must have at least one page.",)
    if max_outputs < 2:
        return ("The configured split-output limit must be at least 2.",)
    if not split_points:
        errors.append("At least one split point is required to create two outputs.")
    if len(split_points) + 1 > max_outputs:
        errors.append(f"Split may create no more than {max_outputs} output PDFs.")

    previous: int | None = None
    for index, value in enumerate(split_points, start=1):
        label = f"Split point {index}"
        if isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"{label} must be a positive whole number.")
            continue
        if value <= 0:
            errors.append(f"{label} must be a positive whole number.")
        if value >= page_count:
            errors.append(
                f"{label} must be less than the source page count ({page_count})."
            )
        if previous is not None and value <= previous:
            if value == previous:
                errors.append(f"{label} duplicates the previous split point.")
            else:
                errors.append("Split points must be in strictly increasing order.")
        previous = value
    return _unique(errors)


def calculate_split_ranges(
    page_count: int,
    split_points: Sequence[int],
    max_outputs: int = MAX_SPLIT_OUTPUTS,
) -> tuple[tuple[int, int], ...]:
    """Return inclusive, one-based page ranges or raise ``ValueError``."""

    errors = validate_split_points(page_count, split_points, max_outputs)
    if errors:
        raise ValueError(" ".join(errors))
    starts = [1, *(point + 1 for point in split_points)]
    ends = [*split_points, page_count]
    return tuple(zip(starts, ends, strict=True))


def validate_output_base(value: str) -> tuple[str, ...]:
    """Validate a Windows filename stem without changing it."""

    base = normalize_output_base(value)
    errors: list[str] = []
    if not base:
        return ("An output base name is required.",)
    if base in {".", ".."}:
        errors.append("The output base name is not valid.")
    if _INVALID_WINDOWS_NAME.search(base):
        errors.append('The output base name contains an invalid character: < > : " / \\ | ? *.')
    if base.endswith((" ", ".")):
        errors.append("The output base name may not end with a space or full stop.")
    if base.split(".", 1)[0].upper() in _RESERVED_WINDOWS_STEMS:
        errors.append("The output base name is reserved by Windows.")
    if len(base) > 180:
        errors.append("The output base name is too long.")
    return _unique(errors)


def _canonical_path(path: Path) -> str:
    """Return a comparison key that follows Windows' case-insensitive semantics."""

    try:
        absolute = path.expanduser().resolve(strict=False)
    except OSError:
        absolute = path.expanduser().absolute()
    return os.path.normcase(str(absolute)).casefold()


def _nearest_existing_directory(path: Path) -> Path | None:
    candidate = path
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    return candidate if candidate.is_dir() else None


def _probe_writable_directory(path: Path) -> str | None:
    """Perform an actual create/write/flush/delete probe in *path*."""

    try:
        descriptor, probe_name = tempfile.mkstemp(prefix=".pros-write-test-", dir=path)
        try:
            os.write(descriptor, b"PROS")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
            Path(probe_name).unlink(missing_ok=True)
    except OSError as exc:
        return f"The folder cannot be used for output ({exc.strerror or type(exc).__name__})."
    return None


def _risk_requires_warning(risk: str, mode: StructureMode) -> bool:
    lowered = risk.casefold()
    return "digital signature" in lowered or mode is not StructureMode.NEITHER


def preflight(request: JobRequest, *, for_estimate: bool = False) -> PreflightReport:
    """Inspect and validate a job; estimates skip checks that require writing."""

    # Local import avoids a module cycle while keeping inspect_pdf as the public
    # engine entry point required by callers.
    from .pdf_engine import inspect_pdf

    errors: list[str] = []
    warnings: list[str] = []
    input_info: list[PdfInfo] = []
    split_ranges: tuple[tuple[int, int], ...] = ()

    try:
        mode = StructureMode(request.structure_mode)
    except ValueError:
        mode = StructureMode.NEITHER
        errors.append("Select Join, Split, or Neither.")

    if not (
        request.remove_password
        or request.compress_pdf
        or request.convert_to_grayscale
        or mode is not StructureMode.NEITHER
    ):
        errors.append("Select at least one PDF-processing function.")
    try:
        CompressionLevel(request.compression_level)
    except ValueError:
        errors.append("The saved compression setting is not supported.")

    input_count = len(request.input_paths)
    if mode is StructureMode.JOIN:
        if not 2 <= input_count <= MAX_JOIN_INPUTS:
            errors.append(f"Join requires between 2 and {MAX_JOIN_INPUTS} input PDFs.")
        if request.split_points:
            errors.append("Split points are not permitted for a Join job.")
    elif mode is StructureMode.SPLIT:
        if input_count != 1:
            errors.append("Split requires exactly one input PDF.")
    else:
        if not 1 <= input_count <= MAX_SEPARATE_INPUTS:
            errors.append(
                f"Keep separate requires between 1 and {MAX_SEPARATE_INPUTS} input PDFs."
            )
        if request.split_points:
            errors.append("Split points are permitted only when Split is selected.")

    if len(request.passwords) != input_count:
        errors.append("The password list must contain one aligned value for every input PDF.")

    passwords = [
        request.passwords[index] if index < len(request.passwords) else None
        for index in range(input_count)
    ]
    for path, password in zip(request.input_paths, passwords, strict=True):
        info = inspect_pdf(path, password)
        input_info.append(info)
        if info.error:
            errors.append(f"{Path(path).name}: {info.error}")
            continue
        if info.encrypted:
            if not request.remove_password:
                errors.append(
                    f"{Path(path).name}: select Remove password to process this protected PDF."
                )
            elif info.password_valid is not True:
                errors.append(f"{Path(path).name}: the password is missing or incorrect.")
        for warning in info.warnings:
            warnings.append(f"{Path(path).name}: {warning}")
        for risk in info.risks:
            if _risk_requires_warning(risk, mode):
                warnings.append(f"{Path(path).name}: {risk}")
        if info.size_bytes > LARGE_FILE_NOTICE_BYTES:
            warnings.append(
                f"{Path(path).name} is larger than 120 MB and may take longer to process."
            )

    input_keys_list = [_canonical_path(path) for path in request.input_paths]
    if len(set(input_keys_list)) != len(input_keys_list):
        errors.append("The input list contains the same PDF path more than once.")
    if mode is StructureMode.JOIN and input_count > 1:
        warnings.append(
            "Join retains document metadata from the first input; later-source document metadata cannot be combined into one metadata record."
        )

    if mode is StructureMode.SPLIT and input_info:
        page_count = input_info[0].page_count
        if page_count is not None:
            split_errors = validate_split_points(page_count, request.split_points)
            errors.extend(split_errors)
            if not split_errors:
                split_ranges = calculate_split_ranges(page_count, request.split_points)

    multi_file_separate = mode is StructureMode.NEITHER and input_count > 1
    if multi_file_separate:
        raw_bases = tuple(
            normalize_output_base(Path(path).stem) for path in request.input_paths
        )
    else:
        ranked_raw_base = normalize_output_base(request.output_base)
        if not ranked_raw_base and request.input_paths:
            ranked_raw_base = normalize_output_base(Path(request.input_paths[0]).stem)
        raw_bases = (ranked_raw_base,)
    for raw_base in raw_bases:
        errors.extend(validate_output_base(raw_base))

    proposed_stems = suggest_output_bases(request)
    for proposed_stem in proposed_stems:
        errors.extend(validate_output_base(proposed_stem))
    output_paths = build_output_paths(request) if proposed_stems and all(proposed_stems) else ()

    # Name collisions are pure path checks, so they apply to estimates as well
    # as runnable jobs.  Existing-file and writeability checks remain skipped
    # for estimates.
    input_keys = set(input_keys_list)
    output_keys: set[str] = set()
    for output_path in output_paths:
        output_key = _canonical_path(output_path)
        if output_key in input_keys:
            errors.append(f"Output path may not equal an input path: {output_path.name}.")
        if output_key in output_keys:
            errors.append(f"Duplicate output path was proposed: {output_path.name}.")
        output_keys.add(output_key)

    if for_estimate:
        return PreflightReport(
            valid=not errors,
            input_info=tuple(input_info),
            output_paths=tuple(output_paths),
            split_ranges=split_ranges,
            errors=_unique(errors),
            warnings=_unique(warnings),
        )

    output_dir = Path(request.output_dir)
    if not output_dir.exists():
        errors.append("The selected output folder does not exist.")
    elif not output_dir.is_dir():
        errors.append("The selected output path is not a folder.")
    elif not os.access(output_dir, os.W_OK):
        errors.append("The selected output folder is not writable.")
    else:
        probe_error = _probe_writable_directory(output_dir)
        if probe_error:
            errors.append(probe_error)

    staging_dir = Path(request.staging_dir)
    if staging_dir.exists() and not staging_dir.is_dir():
        errors.append("The staging path is not a folder.")

    total_input_size = sum(info.size_bytes for info in input_info)
    output_space_needed = max(16 * 1024 * 1024, total_input_size)
    staging_space_needed = max(
        64 * 1024 * 1024,
        total_input_size
        * (
            5
            if request.compress_pdf
            else 4
            if request.convert_to_grayscale
            else 2
        ),
    )
    if output_dir.is_dir():
        try:
            if shutil.disk_usage(output_dir).free < output_space_needed:
                errors.append("The output drive does not have enough free space for this job.")
        except OSError:
            pass
    staging_probe_dir = _nearest_existing_directory(staging_dir)
    if staging_probe_dir is None:
        errors.append("The staging folder cannot be created at the selected location.")
    else:
        if staging_dir.exists():
            probe_error = _probe_writable_directory(staging_dir)
            if probe_error:
                errors.append(f"The staging folder is not writable. {probe_error}")
        try:
            if shutil.disk_usage(staging_probe_dir).free < staging_space_needed:
                errors.append("The staging drive does not have enough free space for this job.")
        except OSError:
            pass

    for output_path in output_paths:
        if output_path.exists():
            errors.append(f"An output file already exists: {output_path.name}.")

    return PreflightReport(
        valid=not errors,
        input_info=tuple(input_info),
        output_paths=tuple(output_paths),
        split_ranges=split_ranges,
        errors=_unique(errors),
        warnings=_unique(warnings),
    )


__all__ = [
    "LARGE_FILE_ACCEPTANCE_BYTES",
    "LARGE_FILE_NOTICE_BYTES",
    "MAX_JOIN_INPUTS",
    "MAX_SEPARATE_INPUTS",
    "MAX_SPLIT_OUTPUTS",
    "calculate_split_ranges",
    "preflight",
    "validate_output_base",
    "validate_split_points",
]
