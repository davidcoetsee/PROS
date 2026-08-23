# SPDX-License-Identifier: MPL-2.0
"""Pure output naming rules from the PROS ranked naming matrix."""

from __future__ import annotations

from pathlib import Path

from .models import JobRequest, StructureMode


def normalize_output_base(value: str) -> str:
    """Return a trimmed base name without a trailing PDF extension."""

    base = value.strip()
    if base.casefold().endswith(".pdf"):
        base = base[:-4].rstrip()
    return base


def _ranked_base(request: JobRequest) -> str:
    user_base = normalize_output_base(request.output_base)
    if user_base:
        return user_base
    if request.input_paths:
        return normalize_output_base(Path(request.input_paths[0]).stem)
    return ""


def _input_base(path: Path) -> str:
    """Return the normalized source stem used by multi-file Keep separate."""

    return normalize_output_base(Path(path).stem)


def _automatic_suffixes(request: JobRequest) -> tuple[str, ...]:
    suffixes: list[str] = []
    if request.structure_mode is StructureMode.JOIN:
        suffixes.append("Join")
    if request.remove_password:
        suffixes.append("Pwd_Rmv")
    if request.compress_pdf:
        suffixes.append("Cprs")
    if request.convert_to_grayscale:
        suffixes.append("Grey")
    return tuple(suffixes)


def suggest_output_base(request: JobRequest) -> str:
    """Return the first final-output stem before a possible ``Part N``.

    A multi-file Keep separate job intentionally ignores the one global
    ``output_base`` value.  Each output is named from its corresponding input,
    so this backward-compatible singular helper returns the first such stem.
    Use :func:`suggest_output_bases` when every output stem is required.
    """

    base = _ranked_base(request)
    if request.structure_mode is StructureMode.NEITHER and len(request.input_paths) > 1:
        base = _input_base(request.input_paths[0])
    components = [base, *_automatic_suffixes(request)]
    return " - ".join(component for component in components if component)


def suggest_output_bases(request: JobRequest) -> tuple[str, ...]:
    """Return ordered final stems before any Split ``Part N`` suffix.

    Keep separate with more than one input derives one stem from each input
    path and preserves input order.  All other jobs retain the historic global
    base-name behaviour.
    """

    suffixes = _automatic_suffixes(request)
    if request.structure_mode is StructureMode.NEITHER and len(request.input_paths) > 1:
        return tuple(
            " - ".join(
                component for component in (_input_base(path), *suffixes) if component
            )
            for path in request.input_paths
        )
    return (suggest_output_base(request),)


def build_output_paths(request: JobRequest) -> tuple[Path, ...]:
    """Calculate every final output path without touching the filesystem."""

    stems = suggest_output_bases(request)
    output_dir = Path(request.output_dir)
    if request.structure_mode is StructureMode.SPLIT:
        stem = stems[0]
        return tuple(
            output_dir / f"{stem} - Part {part_number}.pdf"
            for part_number in range(1, len(request.split_points) + 2)
        )
    return tuple(output_dir / f"{stem}.pdf" for stem in stems)


__all__ = [
    "build_output_paths",
    "normalize_output_base",
    "suggest_output_base",
    "suggest_output_bases",
]
