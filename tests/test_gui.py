from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from pros import gui
from pros.gui import (
    FORCED_CANCELLATION_ERROR,
    HEADER_TAGLINE,
    NO_PROGRESS_CANCELLING_STATUS,
    NO_PROGRESS_CLEANUP_STATUS,
    NO_PROGRESS_FORCED_ERROR,
    PROCESS_DISABLED_BG,
    PROCESS_ENABLED_BG,
    SPLIT_INVALID_BG,
    SPLIT_NORMAL_BG,
    ProsApp,
    _InputRow,
)
from pros.models import (
    CompressionLevel,
    JobResult,
    PdfInfo,
    StructureMode,
)


@pytest.fixture(scope="module")
def _window() -> ProsApp:
    window = ProsApp()
    window.withdraw()
    window.update_idletasks()
    yield window
    if window.winfo_exists():
        window.destroy()


@pytest.fixture
def app(_window: ProsApp) -> ProsApp:
    window = _window
    if window._inspection_after is not None:
        try:
            window.after_cancel(window._inspection_after)
        except tk.TclError:  # test cleanup may race a completed callback
            pass
        window._inspection_after = None
    window._inspection_generation += 1
    window._inputs.clear()
    window._selected_input_uid = None
    window.remove_password_var.set(False)
    window.compress_var.set(False)
    window.grayscale_var.set(False)
    window.mode_var.set(StructureMode.NEITHER.value)
    window.use_common_password_var.set(False)
    window.common_password_var.set("")
    window.per_file_password_var.set("")
    window.show_password_var.set(False)
    window.output_folder_var.set(str(window.app_dir))
    window._base_user_edited = False
    window._set_output_base("")
    window._phase = "editing"
    window._runtime_error = ""
    window._last_outputs = ()
    window._last_destination = None
    window._active_request = None
    window._active_job_id = None
    window._worker_process = None
    window._worker_queue = None
    window._cancel_event = None
    window._timeout_reason = None
    window._last_progress_signature = None
    window._last_progress_log_key = None
    window.progress_var.set(0)
    window.file_progress_var.set(0)
    window.file_progress_text_var.set("No file is being processed.")
    window._reset_synthetic_progress()
    window._replace_text(window.status_area, "")
    while len(window._split_rows) > 1:
        row = window._split_rows.pop()
        if row.validate_after:
            window.after_cancel(row.validate_after)
        if row.clear_after:
            window.after_cancel(row.clear_after)
        row.frame.destroy()
    first_split = window._split_rows[0]
    if first_split.validate_after:
        window.after_cancel(first_split.validate_after)
        first_split.validate_after = None
    if first_split.clear_after:
        window.after_cancel(first_split.clear_after)
        first_split.clear_after = None
    first_split.entry.configure(background=SPLIT_NORMAL_BG)
    window._split_rows[0].variable.set("")
    window._refresh_mode_panel()
    window._refresh_inputs()
    window._refresh_ranges()
    window._refresh_output_preview()
    window._refresh_state()
    return window


def _ready_row(path: Path, pages: int, *, encrypted: bool = False) -> _InputRow:
    return _InputRow(
        path=path,
        info=PdfInfo(
            path=path,
            size_bytes=1024,
            page_count=pages,
            encrypted=encrypted,
            password_valid=not encrypted,
        ),
        inspection_pending=False,
    )


def test_initial_ui_contract(app: ProsApp) -> None:
    required_widgets = (
        "remove_password_check",
        "compress_check",
        "grayscale_check",
        "join_radio",
        "split_radio",
        "neither_radio",
        "add_pdf_button",
        "input_folder_entry",
        "input_tree",
        "remove_pdf_button",
        "clear_list_button",
        "move_up_button",
        "move_down_button",
        "password_group",
        "use_common_password_check",
        "common_password_entry",
        "per_file_password_entry",
        "show_password_check",
        "split_options",
        "add_split_button",
        "remove_split_button",
        "range_list",
        "save_as_button",
        "output_folder_entry",
        "output_base_entry",
        "process_button",
        "cancel_button",
        "readiness_canvas",
        "progress_bar",
        "file_progress_bar",
        "file_progress_label",
        "status_area",
        "error_area",
        "open_completed_button",
        "open_destination_button",
    )
    assert all(hasattr(app, name) for name in required_widgets)
    assert app.mode_var.get() == StructureMode.NEITHER.value
    assert app.password_group.cget("style") == "Disabled.TLabelframe"
    assert app.process_button.cget("state") == "disabled"
    assert app.process_button.cget("background") == PROCESS_DISABLED_BG
    assert app.cancel_button.instate(["disabled"])
    assert app.open_completed_button.instate(["disabled"])
    assert app.open_destination_button.instate(["disabled"])
    assert Path(app.input_folder_var.get()) == app.app_dir
    assert Path(app.output_folder_var.get()) == app.app_dir
    assert app.options_group.winfo_manager() == ""
    assert not hasattr(app, "standard_compression_radio")
    assert not hasattr(app, "ultra_compression_radio")
    assert not hasattr(app, "estimate_button")
    assert not hasattr(app, "estimate_result_label")


def test_readiness_and_grayscale_controls_are_beside_their_headings(app: ProsApp) -> None:
    assert app.progress_group.cget("labelwidget") == str(app.progress_heading)
    assert app.readiness_frame.winfo_parent() == str(app.progress_heading)
    assert app.progress_heading.pack_slaves()[:2] == [
        app.progress_heading_label,
        app.readiness_frame,
    ]
    assert app.output_group.cget("labelwidget") == str(app.output_heading)
    assert app.grayscale_check.winfo_parent() == str(app.output_heading)
    assert app.output_heading.pack_slaves()[:2] == [
        app.output_heading_label,
        app.grayscale_check,
    ]


def test_header_combines_to_the_exact_requested_tagline(app: ProsApp) -> None:
    combined = app.title_label.cget("text") + app.tagline_label.cget("text")
    assert combined == HEADER_TAGLINE
    assert combined == (
        "PROS - Free Basic PDF Editor: [P]asswords removed · file sizes [R]educed · "
        "[O]rganise & join · [S]plit files"
    )


def test_modes_are_exclusive_and_show_only_the_relevant_panel(app: ProsApp) -> None:
    app.mode_var.set(StructureMode.JOIN.value)
    app._on_mode_changed()
    assert app.options_group.winfo_manager() == "pack"
    assert app.join_options.winfo_manager() == "pack"
    assert app.split_options.winfo_manager() == ""

    app.mode_var.set(StructureMode.SPLIT.value)
    app._on_mode_changed()
    assert app.options_group.winfo_manager() == "pack"
    assert app.join_options.winfo_manager() == ""
    assert app.split_options.winfo_manager() == "pack"

    app.mode_var.set(StructureMode.NEITHER.value)
    app._on_mode_changed()
    assert app.options_group.winfo_manager() == ""


def test_password_masking_and_per_file_override_precedence(
    app: ProsApp, tmp_path: Path
) -> None:
    row = _ready_row(tmp_path / "protected.pdf", 2, encrypted=True)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.remove_password_var.set(True)
    app.use_common_password_var.set(True)
    app.common_password_var.set("shared")
    app._refresh_state()

    assert app._effective_password(row) == "shared"
    row.password_override = "override"
    assert app._effective_password(row) == "override"
    assert app.common_password_entry.cget("show") == "*"
    app.show_password_var.set(True)
    app._toggle_password_visibility()
    assert app.common_password_entry.cget("show") == ""
    assert app.per_file_password_entry.cget("show") == ""


def test_split_example_updates_ranges_immediately(app: ProsApp, tmp_path: Path) -> None:
    row = _ready_row(tmp_path / "source.pdf", 104)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.mode_var.set(StructureMode.SPLIT.value)
    app._on_mode_changed()
    app._split_rows[0].variable.set("65")
    app._add_split_row()
    app._split_rows[1].variable.set("73")
    app._add_split_row()
    app._split_rows[2].variable.set("102")
    app._refresh_ranges()

    values = [app.range_list.get(index) for index in range(app.range_list.size())]
    assert values == [
        "Part 1 — pages 1–65 (65 pages)",
        "Part 2 — pages 66–73 (8 pages)",
        "Part 3 — pages 74–102 (29 pages)",
        "Part 4 — pages 103–104 (2 pages)",
    ]


def test_adding_blank_split_keeps_ranges_and_invalid_idle_input_flashes_then_clears(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _ready_row(tmp_path / "source.pdf", 104)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.mode_var.set(StructureMode.SPLIT.value)
    app._on_mode_changed()
    app._split_rows[0].variable.set("65")
    app._refresh_ranges()
    original = [app.range_list.get(index) for index in range(app.range_list.size())]

    app._add_split_row()
    assert app._split_rows[1].variable.get() == ""
    assert [app.range_list.get(index) for index in range(app.range_list.size())] == original

    scheduled: list[tuple[int, object]] = []

    def fake_after(delay: int, callback: object | None = None, *args: object) -> str:
        assert callback is not None

        def invoke() -> None:
            callback(*args)  # type: ignore[operator]

        scheduled.append((delay, invoke))
        return f"after-{len(scheduled)}"

    monkeypatch.setattr(app, "after", fake_after)
    invalid = app._split_rows[1]
    invalid.variable.set("not-a-page")
    app._on_split_key(1)

    assert scheduled[0][0] == 1200
    assert invalid.variable.get() == "not-a-page"
    scheduled.pop(0)[1]()  # idle validation schedules the first flash immediately
    assert scheduled[0][0] == 0

    colors: list[str] = []
    for _ in range(4):
        scheduled.pop(0)[1]()
        colors.append(str(invalid.entry.cget("background")))
    assert colors == [
        SPLIT_INVALID_BG,
        SPLIT_NORMAL_BG,
        SPLIT_INVALID_BG,
        SPLIT_NORMAL_BG,
    ]
    scheduled.pop(0)[1]()
    assert invalid.variable.get() == ""
    assert [app.range_list.get(index) for index in range(app.range_list.size())] == original


def test_process_enables_only_for_a_complete_valid_draft(
    app: ProsApp, tmp_path: Path
) -> None:
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    row = _ready_row(source, 3)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Result")
    app.compress_var.set(True)
    app._refresh_output_preview()
    app._refresh_state()

    assert app.process_button.cget("state") == "normal"
    assert app.process_button.cget("background") == PROCESS_ENABLED_BG
    (tmp_path / "Result - Cprs.pdf").write_bytes(b"existing")
    app._refresh_state()
    assert app.process_button.cget("state") == "disabled"
    assert app.process_button.cget("background") == PROCESS_DISABLED_BG
    assert "already exists" in app.error_area.get("1.0", "end").casefold()


def test_readiness_turns_green_only_when_valid_and_locks_immediately_on_process(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    row = _ready_row(source, 3)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Ready")
    app.compress_var.set(True)
    app._refresh_output_preview()
    app._refresh_state()

    assert app.process_button.cget("state") == "normal"
    assert app.process_button.cget("background") == PROCESS_ENABLED_BG
    assert app.readiness_canvas.itemcget(app.readiness_indicator, "fill") == "#22a447"
    assert app.readiness_text_var.get() == "Ready to process"

    class NoopThread:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def start(self) -> None:
            pass

    monkeypatch.setattr(gui.threading, "Thread", NoopThread)
    app._start_process()
    assert app._phase == "preflighting"
    assert app.process_button.cget("state") == "disabled"
    assert app.process_button.cget("background") == PROCESS_DISABLED_BG
    assert app.readiness_canvas.itemcget(app.readiness_indicator, "fill") == "#e6395b"
    assert app.readiness_text_var.get() == "Working"


def test_grayscale_is_an_independent_function_and_requests_the_sole_profile(
    app: ProsApp, tmp_path: Path
) -> None:
    row = _ready_row(tmp_path / "source.pdf", 3)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Grayscale")
    app._refresh_state()
    assert app.process_button.cget("state") == "disabled"
    assert not app.grayscale_check.instate(["disabled"])

    app.grayscale_var.set(True)
    app._on_grayscale_changed()
    assert app.compress_var.get() is False
    assert app.process_button.cget("state") == "normal"
    assert app.readiness_canvas.itemcget(app.readiness_indicator, "fill") == "#22a447"
    request = app._make_job_request()
    assert request.compression_level is CompressionLevel.ULTRA
    assert request.compress_pdf is False
    assert request.convert_to_grayscale is True


def test_success_suppresses_stale_password_and_output_collision_errors(
    app: ProsApp, tmp_path: Path
) -> None:
    source = tmp_path / "protected.pdf"
    row = _ready_row(source, 3, encrypted=True)
    row.info = PdfInfo(
        path=source,
        size_bytes=1024,
        page_count=3,
        encrypted=True,
        password_valid=True,
    )
    row.password_override = "secret"
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.remove_password_var.set(True)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Finished")
    request = app._make_job_request()
    output = tmp_path / "Finished - Pwd_Rmv.pdf"
    output.write_bytes(b"%PDF-1.7\n")
    app._active_request = request
    app._active_job_id = request.job_id
    app._phase = "processing"

    app._handle_job_result(
        JobResult(job_id=request.job_id, success=True, output_paths=(output,))
    )

    errors = app.error_area.get("1.0", "end").casefold()
    assert app._phase == "succeeded"
    assert "password" not in errors
    assert "already exists" not in errors
    assert app.process_button.cget("state") == "disabled"
    assert app.process_button.cget("background") == PROCESS_DISABLED_BG
    assert app.readiness_canvas.itemcget(app.readiness_indicator, "fill") == "#e6395b"

    app.output_base_var.set("Revised")
    app._on_output_base_key()
    assert app._phase == "editing"


def test_richer_progress_uses_real_heartbeats_and_synthetic_ticks_without_regressing(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = _ready_row(tmp_path / "source.pdf", 3)
    row.info = PdfInfo(
        path=row.path,
        size_bytes=round(136.1 * 1024 * 1024),
        page_count=3,
        encrypted=False,
        password_valid=True,
    )
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Progress")
    app.compress_var.set(True)
    request = app._make_job_request()
    app._active_request = request
    app._active_job_id = request.job_id
    app._phase = "processing"
    clock = iter((100.0, 101.0))
    monkeypatch.setattr(gui.time, "monotonic", lambda: next(clock))
    app._show_progress_event(
        {
            "job_id": request.job_id,
            "stage": "compress",
            "percent": 42,
            "phase_percent": 20,
            "file_index": 2,
            "file_count": 3,
            "path": str(tmp_path / "second.pdf"),
            "message": "Compressing second.pdf",
        }
    )
    assert app.progress_var.get() == 42
    assert app.file_progress_var.get() == 20
    assert app.file_progress_text_var.get().startswith("File 2 of 3: Compressing second.pdf")
    assert "20%" in app.file_progress_text_var.get()
    assert app._compression_meter(6) == "[---] 6%"
    assert app._compression_meter(100) == f"[{'-' * 50}] 100%"
    assert app._synthetic_interval_for_active_job(1) == pytest.approx(
        136.1 / 24, rel=0.01
    )
    assert app._synthetic_progress_interval == pytest.approx(136.1 / (24 * 3), rel=0.01)

    last_real_progress = app._last_progress_at
    tick_at = app._next_synthetic_progress_at
    app._advance_synthetic_progress(now=tick_at)
    assert app.file_progress_var.get() == 22
    assert app._last_progress_at == last_real_progress

    # A delayed real event refreshes liveness but cannot move the visible 22%
    # display back to the worker's older 20% value.
    app._show_progress_event(
        {
            "job_id": request.job_id,
            "stage": "compress",
            "percent": 42,
            "phase_percent": 20,
            "file_index": 2,
            "file_count": 3,
            "path": str(tmp_path / "second.pdf"),
            "message": "Compressing second.pdf",
        }
    )
    assert app._last_progress_at == 101.0
    assert app.file_progress_var.get() == 22

    app._display_phase_percent = 97
    app._next_synthetic_progress_at = 101.0
    app._advance_synthetic_progress(now=1000.0)
    assert app.file_progress_var.get() == 98


def test_timeout_language_matches_the_requested_plain_language_examples(app: ProsApp) -> None:
    assert NO_PROGRESS_CANCELLING_STATUS == (
        "No progress was detected for five minutes. We have requested safe cancellation "
        "and are cleaning up temporary files."
    )
    assert NO_PROGRESS_CLEANUP_STATUS == (
        "Cleanup is complete. No completed output file was created."
    )
    assert NO_PROGRESS_FORCED_ERROR == (
        "No progress was detected for five minutes. The job was forcibly stopped, and all "
        "temporary files, partial files, and new output files created for this job were removed."
    )
    assert FORCED_CANCELLATION_ERROR == (
        "Processing was cancelled because it did not show progress for five minutes. The task "
        "did not stop in time, so it was forcibly ended. Any temporary, partial, or newly "
        "created output files from this task were removed."
    )
    assert app._friendly_message(NO_PROGRESS_FORCED_ERROR) == NO_PROGRESS_FORCED_ERROR
