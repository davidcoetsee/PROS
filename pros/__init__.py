# SPDX-License-Identifier: MPL-2.0
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

__version__ = "1.5.1"

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
