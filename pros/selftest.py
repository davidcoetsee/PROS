"""Deterministic, non-GUI health check for source and frozen PROS builds."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any

import pikepdf

from pros.models import JobRequest, StructureMode
from pros.pdf_engine import process_job


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
    """Create a small PDF whose content is stable across repeated runs."""

    with pikepdf.Pdf.new() as pdf:
        pdf.add_blank_page(page_size=(612, 792))
        pdf.docinfo["/Title"] = "PROS packaged self-test"
        pdf.docinfo["/Creator"] = "PROS"
        pdf.save(path, deterministic_id=True)


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
    progress_events: list[dict[str, object]] = []

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

        request = JobRequest(
            job_id=f"selftest-{os.getpid()}",
            remove_password=False,
            compress_pdf=True,
            structure_mode=StructureMode.NEITHER,
            input_paths=[input_path],
            passwords=[None],
            split_points=[],
            output_dir=run_dir,
            output_base="PROS Self Test",
            staging_dir=run_dir / "staging",
        )
        result = process_job(request, progress=progress_events.append)

        if not result.success:
            raise RuntimeError(result.error or "PDF engine returned an unsuccessful result")
        if result.cancelled:
            raise RuntimeError("PDF engine unexpectedly reported cancellation")
        if len(result.output_paths) != 1:
            raise RuntimeError(
                f"Expected one output PDF, received {len(result.output_paths)}"
            )

        output_path = Path(result.output_paths[0]).resolve()
        if not output_path.is_file() or output_path.stat().st_size == 0:
            raise RuntimeError(f"Output PDF was not created correctly: {output_path}")
        if _sha256(input_path) != input_hash_before:
            raise RuntimeError("The source PDF changed during processing")

        with pikepdf.Pdf.open(output_path) as output_pdf:
            if len(output_pdf.pages) != 1:
                raise RuntimeError(
                    f"Expected one page in output, found {len(output_pdf.pages)}"
                )
            syntax_warnings = list(output_pdf.check_pdf_syntax())
        if syntax_warnings:
            raise RuntimeError(
                "Output PDF failed syntax validation: " + "; ".join(syntax_warnings)
            )

        report.update(
            {
                "status": "ok",
                "input": str(input_path),
                "input_sha256": input_hash_before,
                "output": str(output_path),
                "output_sha256": _sha256(output_path),
                "output_size_bytes": output_path.stat().st_size,
                "progress_event_count": len(progress_events),
                "warnings": list(result.warnings),
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
                "progress_event_count": len(progress_events),
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
