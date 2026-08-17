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


def _automatic_suffixes(request: JobRequest) -> tuple[str, ...]:
    suffixes: list[str] = []
    if request.structure_mode is StructureMode.JOIN:
        suffixes.append("Join")
    if request.remove_password:
        suffixes.append("Pwd_Rmv")
    if request.compress_pdf:
        suffixes.append("Cprs")
    return tuple(suffixes)


def suggest_output_base(request: JobRequest) -> str:
    """Return the final single-output stem before a possible ``Part N``."""

    components = [_ranked_base(request), *_automatic_suffixes(request)]
    return " - ".join(component for component in components if component)


def build_output_paths(request: JobRequest) -> tuple[Path, ...]:
    """Calculate every final output path without touching the filesystem."""

    stem = suggest_output_base(request)
    output_dir = Path(request.output_dir)
    if request.structure_mode is StructureMode.SPLIT:
        return tuple(
            output_dir / f"{stem} - Part {part_number}.pdf"
            for part_number in range(1, len(request.split_points) + 2)
        )
    return (output_dir / f"{stem}.pdf",)


__all__ = [
    "build_output_paths",
    "normalize_output_base",
    "suggest_output_base",
]
