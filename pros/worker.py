"""Process-worker adapter that keeps PDF work outside the GUI thread."""

from __future__ import annotations

from typing import Any

from .models import JobRequest, JobResult
from .pdf_engine import process_job


def _queue_put(progress_queue: Any, event: dict[str, object]) -> None:
    if progress_queue is not None:
        progress_queue.put(event)


def run_worker(
    request: JobRequest,
    progress_queue: Any,
    cancel_event: object | None,
) -> JobResult:
    """Run a job in a spawned process and publish progress plus one terminal event."""

    result = process_job(
        request,
        progress=lambda event: _queue_put(progress_queue, event),
        cancel_event=cancel_event,
    )
    _queue_put(
        progress_queue,
        {
            "job_id": request.job_id,
            "kind": "result",
            "stage": "complete" if result.success else "cancelled" if result.cancelled else "error",
            "percent": 100 if result.success else 0,
            "message": (
                "Processing completed successfully"
                if result.success
                else result.error or "Processing failed"
            ),
            "path": None,
            "result": result,
        },
    )
    return result


__all__ = ["run_worker"]
