"""Public data contracts shared by the PROS UI, worker, and PDF engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class StructureMode(str, Enum):
    """The mutually exclusive structural operation selected for a job."""

    NEITHER = "neither"
    JOIN = "join"
    SPLIT = "split"


class CompressionLevel(str, Enum):
    """Legacy serialized values; all compression now uses the Ultra profile."""

    STANDARD = "standard"
    ULTRA = "ultra"


@dataclass(frozen=True, slots=True)
class PdfInfo:
    """Read-only facts discovered while inspecting one input PDF."""

    path: Path
    size_bytes: int = 0
    page_count: int | None = None
    encrypted: bool | None = None
    password_valid: bool | None = None
    error: str | None = None
    warnings: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        """Whether the file is structurally readable with the supplied password."""

        return self.error is None and self.page_count is not None


@dataclass(slots=True, kw_only=True)
class JobRequest:
    """Complete, serialisable request for one atomic PROS job.

    ``output_base`` is the raw user/ranked stem. It excludes ``.pdf`` and the
    automatic ``Join``, ``Pwd_Rmv``, ``Cprs``, ``Grey`` and ``Part N`` suffixes.
    Passwords are aligned by index with ``input_paths`` and deliberately hidden
    from ``repr`` so normal diagnostics cannot expose them.
    """

    job_id: str
    remove_password: bool
    compress_pdf: bool
    structure_mode: StructureMode
    input_paths: list[Path]
    passwords: list[str | None] = field(repr=False)
    split_points: list[int]
    output_dir: Path
    output_base: str
    staging_dir: Path
    compression_level: CompressionLevel = CompressionLevel.ULTRA
    convert_to_grayscale: bool = False

    def __post_init__(self) -> None:
        self.structure_mode = StructureMode(self.structure_mode)
        self.compression_level = CompressionLevel(self.compression_level)
        self.input_paths = [Path(path) for path in self.input_paths]
        self.passwords = list(self.passwords)
        self.split_points = list(self.split_points)
        self.output_dir = Path(self.output_dir)
        self.staging_dir = Path(self.staging_dir)


@dataclass(frozen=True, slots=True)
class PreflightReport:
    """Authoritative validation result used to enable or reject processing."""

    valid: bool
    input_info: tuple[PdfInfo, ...] = ()
    output_paths: tuple[Path, ...] = ()
    split_ranges: tuple[tuple[int, int], ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JobResult:
    """Terminal result returned by the engine and worker process."""

    job_id: str
    success: bool
    cancelled: bool = False
    output_paths: tuple[Path, ...] = ()
    error: str | None = None
    warnings: tuple[str, ...] = ()
    original_size_bytes: int = 0
    output_size_bytes: int = 0
    reduction_percent: float | None = None


@dataclass(frozen=True, slots=True)
class EstimateResult:
    """A bounded, best-effort estimate made without creating final outputs."""

    job_id: str
    success: bool
    cancelled: bool = False
    estimated_seconds: float | None = None
    estimated_output_bytes: int | None = None
    input_size_bytes: int = 0
    confidence: str = "low"
    error: str | None = None
    warnings: tuple[str, ...] = ()
