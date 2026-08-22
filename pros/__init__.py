"""PROS PDF processing application core package."""

from .models import (
    CompressionLevel,
    EstimateResult,
    JobRequest,
    JobResult,
    PdfInfo,
    PreflightReport,
    StructureMode,
)

__version__ = "1.5.0"

__all__ = [
    "CompressionLevel",
    "EstimateResult",
    "JobRequest",
    "JobResult",
    "PdfInfo",
    "PreflightReport",
    "StructureMode",
    "__version__",
]
