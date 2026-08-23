# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk
from types import SimpleNamespace

import pytest
from PIL import Image

from pros import __version__, gui
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
    window._close_about()
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
    window._mode_confirmed = False
    window._confirmed_mode = None
    window._last_mode_click_value = ""
    window._last_mode_click_at = 0.0
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
        "drop_zone",
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
        "clear_split_button",
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
        "status_scrollbar",
        "error_area",
        "error_scrollbar",
        "open_completed_button",
        "open_destination_button",
        "clear_job_button",
        "workflow_frame",
        "left_workflow",
        "right_workflow",
        "review_process_row",
        "review_group",
        "action_bar",
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


def test_fixed_status_bar_and_processing_controls_follow_the_mockup(app: ProsApp) -> None:
    assert app.action_bar.winfo_manager() == "grid"
    assert app.action_bar.winfo_parent() != str(app.main_canvas)
    assert app.readiness_frame.winfo_parent() == str(app.action_bar)
    assert app.process_button.winfo_parent() != str(app.progress_group)
    assert app.clear_job_button.winfo_parent() == app.process_button.winfo_parent()
    assert app.grayscale_check.winfo_parent() == str(app.additional_processing_frame)
    assert app.review_heading_label.cget("text") == "Review the job"
    assert app.progress_heading_label.cget("text") == "Process the PDFs"
    assert app.review_group is not app.progress_group


def test_status_and_error_logs_are_focusable_scrollable_read_only_text(
    app: ProsApp,
) -> None:
    status_lines = "\n".join(f"Status line {index}" for index in range(12))
    error_lines = "\n".join(f"Error line {index}" for index in range(10))
    app._replace_text(app.status_area, status_lines)
    app._replace_text(app.error_area, error_lines)
    app.update_idletasks()

    for text_widget, scrollbar in (
        (app.status_area, app.status_scrollbar),
        (app.error_area, app.error_scrollbar),
    ):
        assert text_widget.cget("state") == "disabled"
        assert str(text_widget.cget("takefocus")) == "1"
        assert text_widget.cget("yscrollcommand")
        assert scrollbar.winfo_manager() == "grid"
        text_widget.yview_moveto(1)
        assert text_widget.yview()[0] > 0
        assert app._select_all_readonly_text(text_widget) == "break"
        assert tuple(str(value) for value in text_widget.tag_ranges("sel")) == (
            "1.0",
            str(text_widget.index("end-1c")),
        )

    app.deiconify()
    app.update()
    try:
        focus_path = str(app.output_base_entry)
        focus_chain: list[str] = []
        for _ in range(40):
            focus_path = str(app.tk.call("tk_focusNext", focus_path))
            focus_chain.append(focus_path)
        assert str(app.status_area) in focus_chain
        assert str(app.error_area) in focus_chain
    finally:
        app.withdraw()
        app.update_idletasks()


def test_header_combines_to_the_exact_requested_tagline(app: ProsApp) -> None:
    combined = app.title_label.cget("text") + app.tagline_label.cget("text")
    assert combined == HEADER_TAGLINE
    assert combined == (
        "PROS - Free Basic PDF Editor: [P]asswords removed · file sizes [R]educed · "
        "[O]rganise & join · [S]plit files"
    )


def test_canonical_brand_assets_have_validated_dimensions() -> None:
    assets = Path(gui.__file__).resolve().parent.parent / "assets"
    with Image.open(assets / "PROS-Logo.png") as logo:
        assert logo.size == (1200, 100)
        assert logo.mode == "RGBA"
    with Image.open(assets / "PROS-App-Icon.png") as app_icon:
        assert app_icon.size == (1024, 1024)
        assert app_icon.mode == "RGBA"
    with Image.open(assets / "PROS.ico") as icon:
        assert icon.format == "ICO"
        assert set(icon.ico.sizes()) == {
            (16, 16),
            (32, 32),
            (48, 48),
            (64, 64),
            (128, 128),
            (256, 256),
        }


def test_header_uses_retained_aspect_preserving_wordmark(app: ProsApp) -> None:
    photo = app._brand_header_photo
    assert photo is not None
    assert photo.width() <= gui.HEADER_IMAGE_MAX_SIZE[0]
    assert photo.height() <= gui.HEADER_IMAGE_MAX_SIZE[1]
    assert photo.width() >= 816
    assert photo.width() / photo.height() == pytest.approx(12.0)
    assert app.header_image_label.winfo_manager() == "grid"
    assert app.header_image_label.cget("image")
    assert app.title_label.winfo_manager() == ""
    assert app.tagline_label.winfo_manager() == ""
    # Keep a strong Python reference; Tk otherwise discards image pixels.
    assert app._brand_header_photo is photo
    assert app.header_frame.winfo_parent() == str(app.outer_frame)
    assert not app._is_widget_descendant(app.header_frame, app.main_canvas)
    assert app.header_privacy_label.cget("text") == gui.HEADER_PRIVACY_TEXT
    app._render_brand_header(844)
    try:
        assert app._brand_header_photo is not None
        assert (
            app._brand_header_photo.width(),
            app._brand_header_photo.height(),
        ) == (840, 70)
    finally:
        app._render_brand_header(gui.HEADER_IMAGE_MAX_SIZE[0])


@pytest.mark.parametrize("failure_mode", ["missing", "corrupt"])
def test_header_has_exact_text_fallback_for_unavailable_wordmark(
    app: ProsApp,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    original_resource_path = gui._resource_path
    corrupt = tmp_path / "not-an-image.png"
    corrupt.write_bytes(b"not a PNG")

    def fake_resource_path(filename: str) -> Path | None:
        if filename == "assets/PROS-Logo.png":
            return None if failure_mode == "missing" else corrupt
        return original_resource_path(filename)

    monkeypatch.setattr(gui, "_resource_path", fake_resource_path)
    app._render_brand_header()
    try:
        assert app._brand_header_photo is None
        assert app.header_image_label.winfo_manager() == ""
        assert app.title_label.winfo_manager() == "grid"
        assert app.tagline_label.winfo_manager() == "grid"
        assert (
            app.title_label.cget("text") + app.tagline_label.cget("text")
            == HEADER_TAGLINE
        )
    finally:
        monkeypatch.setattr(gui, "_resource_path", original_resource_path)
        app._render_brand_header()


def test_window_icon_prefers_canonical_asset_then_loose_fallback(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    canonical = tmp_path / "assets" / "PROS.ico"
    loose = tmp_path / "PROS.ico"
    calls: list[str] = []
    selected: list[str] = []

    def canonical_available(filename: str) -> Path | None:
        calls.append(filename)
        return canonical if filename == "assets/PROS.ico" else loose

    monkeypatch.setattr(gui, "_resource_path", canonical_available)
    monkeypatch.setattr(
        app, "iconbitmap", lambda **options: selected.append(str(options["default"]))
    )
    app._set_icon()
    assert calls == ["assets/PROS.ico"]
    assert selected == [str(canonical)]

    calls.clear()
    selected.clear()

    def canonical_missing(filename: str) -> Path | None:
        calls.append(filename)
        return None if filename == "assets/PROS.ico" else loose

    monkeypatch.setattr(gui, "_resource_path", canonical_missing)
    app._set_icon()
    assert calls == ["assets/PROS.ico", "PROS.ico"]
    assert selected == [str(loose)]

    def rejected_icon(**_options: object) -> None:
        raise tk.TclError("invalid icon")

    monkeypatch.setattr(app, "iconbitmap", rejected_icon)
    app._set_icon()  # A bad platform icon must not prevent the app from opening.


def test_about_is_branded_modal_and_all_close_routes_work(app: ProsApp) -> None:
    app._show_about()
    app.update_idletasks()
    window = app._about_window
    assert window is not None
    assert window.winfo_exists()
    assert window.title() == "About PROS"
    assert str(window.transient()) == str(app)
    assert window.grab_current() is window
    assert app._about_photo is not None
    assert (app._about_photo.width(), app._about_photo.height()) == (112, 112)
    assert app._about_image_label.cget("image")
    assert app._about_title_label.cget("text") == f"PROS v{__version__}"
    copy = str(app._about_copy_label.cget("text"))
    assert "Free Basic PDF Editor for Windows" in copy
    assert "Original source PDFs are never modified" in copy
    assert "MPL 2.0" in copy
    assert window.bind("<Escape>")
    assert window.bind("<Return>")
    assert window.protocol("WM_DELETE_WINDOW")

    assert app._close_about_from_key() == "break"
    assert app._about_window is None
    assert app._about_photo is None

    app._show_about()
    app._about_close_button.invoke()
    assert app._about_window is None

    app._show_about()
    window = app._about_window
    assert window is not None
    app.tk.call(window.protocol("WM_DELETE_WINDOW"))
    assert app._about_window is None


def test_about_uses_text_fallback_when_app_icon_is_missing(
    app: ProsApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_resource_path = gui._resource_path

    def without_app_icon(filename: str) -> Path | None:
        if filename == "assets/PROS-App-Icon.png":
            return None
        return original_resource_path(filename)

    monkeypatch.setattr(gui, "_resource_path", without_app_icon)
    app._show_about()
    try:
        assert app._about_photo is None
        assert app._about_image_label.cget("text") == "PROS"
        assert not app._about_image_label.cget("image")
        assert app._about_title_label.cget("text") == f"PROS v{__version__}"
    finally:
        app._close_about()


def test_open_source_dialog_exposes_licence_source_and_brand_terms(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    documents = {
        "SOURCE_CODE.txt": "Exact source: https://example.invalid/PROS/tree/v1.5.1",
        "LICENSE": "Mozilla Public License Version 2.0",
        "TRADEMARKS.md": "Modified builds must use different branding.",
        "ASSET_LICENSES.md": "Canonical assets are MPL-2.0 licensed files.",
    }
    paths: dict[str, Path] = {}
    for filename, contents in documents.items():
        path = tmp_path / filename
        path.write_text(contents, encoding="utf-8")
        paths[filename] = path

    monkeypatch.setattr(gui, "_resource_path", lambda filename: paths.get(filename))
    window = app._show_license_and_source()
    try:
        assert window.title() == "PROS — Open-source licence and source"
        text_widgets = [
            child
            for holder in window.winfo_children()
            for child in holder.winfo_children()
            if isinstance(child, tk.Text)
        ]
        assert len(text_widgets) == 1
        displayed = text_widgets[0].get("1.0", "end-1c")
        for contents in documents.values():
            assert contents in displayed
    finally:
        window.destroy()


def test_modes_are_exclusive_and_show_only_the_relevant_panel(app: ProsApp) -> None:
    app.mode_var.set(StructureMode.JOIN.value)
    app._on_mode_changed()
    assert app.options_group.winfo_manager() == ""
    assert app.join_options.winfo_manager() == "pack"
    assert app.output_group.grid_info()["row"] == 0
    assert app.review_process_row.grid_info()["row"] == 1
    assert app.review_group.grid_info()["row"] == 0
    assert app.review_group.grid_info()["column"] == 0
    assert app.progress_group.grid_info()["row"] == 0
    assert app.progress_group.grid_info()["column"] == 1
    assert app.output_step_badge.itemcget(app.output_step_text, "text") == "3"
    assert app.review_step_badge.itemcget(app.review_step_text, "text") == "4"
    assert app.progress_step_badge.itemcget(app.progress_step_text, "text") == "5"
    assert app.add_pdf_button.cget("text") == "+ Add PDFs"

    app.mode_var.set(StructureMode.SPLIT.value)
    app._on_mode_changed()
    assert app.options_group.winfo_manager() == "grid"
    assert app.join_options.winfo_manager() == ""
    assert app.split_options.winfo_manager() == "grid"
    assert app.output_group.grid_info()["row"] == 1
    assert app.review_process_row.grid_info()["row"] == 1
    assert app.review_group.grid_info()["row"] == 0
    assert app.progress_group.grid_info()["row"] == 0
    assert app.output_step_badge.itemcget(app.output_step_text, "text") == "4"
    assert app.review_step_badge.itemcget(app.review_step_text, "text") == "5"
    assert app.progress_step_badge.itemcget(app.progress_step_text, "text") == "6"

    app.mode_var.set(StructureMode.NEITHER.value)
    app._on_mode_changed()
    assert app.options_group.winfo_manager() == ""
    assert app.join_options.winfo_manager() == ""
    assert app.add_pdf_button.cget("text") == "+ Add PDFs"


def test_workflow_uses_two_columns_numbered_cards_and_small_width_scrolling(
    app: ProsApp,
) -> None:
    assert app.function_group.winfo_parent() == str(app.left_workflow)
    assert app.input_group.winfo_parent() == str(app.left_workflow)
    assert app.output_group.winfo_parent() == str(app.right_workflow)
    assert app.review_process_row.winfo_parent() == str(app.workflow_frame)
    assert app.review_group.winfo_parent() == str(app.review_process_row)
    assert app.progress_group.winfo_parent() == str(app.review_process_row)
    assert app.function_step_badge.itemcget(app.function_step_text, "text") == "1"
    assert app.input_step_badge.itemcget(app.input_step_text, "text") == "2"

    for mode in (StructureMode.JOIN, StructureMode.SPLIT):
        app.mode_var.set(mode.value)
        app._refresh_mode_panel()
        app._on_canvas_configure(SimpleNamespace(width=1263))
        assert int(float(app.main_canvas.itemcget(app._canvas_window, "width"))) == 1263
        assert app.main_horizontal_scrollbar.winfo_manager() == ""

    app._on_canvas_configure(SimpleNamespace(width=760))
    assert int(float(app.main_canvas.itemcget(app._canvas_window, "width"))) == 1120
    assert app.main_horizontal_scrollbar.winfo_manager() == "grid"


def test_v15_header_and_display_surfaces_are_fixed_white_and_consistent(
    app: ProsApp,
) -> None:
    app.geometry("1280x820")
    app.deiconify()
    app.update()
    try:
        assert app.header_panel.winfo_parent() == str(app.outer_frame)
        assert app.header_panel.winfo_width() == app.outer_frame.winfo_width()
        assert app.header_panel.winfo_rooty() < app.main_canvas.winfo_rooty()
        assert app.header_privacy_label.grid_info()["row"] == 1
        assert ttk.Style(app).lookup("TLabel", "background") == "#ffffff"
        assert ttk.Style(app).lookup("TCheckbutton", "background") == "#ffffff"

        displays = (
            app.range_list,
            app.output_preview,
            app.status_area,
            app.error_area,
        )
        assert {
            str(widget.cget("background")) for widget in displays
        } == {gui.DISPLAY_BACKGROUND}
        assert {str(widget.cget("relief")) for widget in displays} == {"solid"}

        header_y = app.header_panel.winfo_rooty()
        app.main_canvas.yview_moveto(1)
        app.update_idletasks()
        assert app.header_panel.winfo_rooty() == header_y
    finally:
        app.main_canvas.yview_moveto(0)
        app.withdraw()
        app.update_idletasks()


def test_v15_tree_and_output_entries_share_the_white_solid_display_family(
    app: ProsApp,
) -> None:
    style = ttk.Style(app)
    assert app.input_tree.cget("style") == "Pros.Treeview"
    assert style.lookup("Pros.Treeview", "background") == gui.DISPLAY_BACKGROUND
    assert style.lookup("Pros.Treeview", "fieldbackground") == (
        gui.DISPLAY_BACKGROUND
    )
    assert int(style.lookup("Pros.Treeview", "borderwidth")) == 1
    assert str(style.lookup("Pros.Treeview", "relief")) == "solid"
    assert style.lookup("Pros.Treeview.Heading", "background") == (
        gui.DISPLAY_BACKGROUND
    )

    assert app.output_folder_entry.cget("state") == "readonly"
    assert app.output_folder_entry.cget("readonlybackground") == (
        gui.DISPLAY_BACKGROUND
    )
    for entry in (app.output_folder_entry, app.output_base_entry):
        assert entry.cget("background") == gui.DISPLAY_BACKGROUND
        assert entry.cget("relief") == "solid"
        assert int(entry.cget("borderwidth")) == 1
        assert int(entry.cget("highlightthickness")) == 0
    assert app.output_base_entry.cget("disabledbackground") == (
        gui.DISPLAY_BACKGROUND
    )


def test_v15_review_and_process_cards_share_one_horizontal_row_without_clipping(
    app: ProsApp,
) -> None:
    app.geometry("1280x820")
    app.deiconify()
    app.update()
    try:
        for mode in (StructureMode.NEITHER, StructureMode.SPLIT):
            app.mode_var.set(mode.value)
            app._refresh_mode_panel()
            app._on_canvas_configure(SimpleNamespace(width=1263))
            app.update()
            assert app.review_process_row.grid_info()["row"] == 1
            assert app.review_process_row.grid_info()["columnspan"] == 3
            assert app.review_group.winfo_parent() == str(app.review_process_row)
            assert app.progress_group.winfo_parent() == str(app.review_process_row)
            assert app.review_group.grid_info()["column"] == 0
            assert app.progress_group.grid_info()["column"] == 1
            assert app.review_group.winfo_rootx() < app.progress_group.winfo_rootx()
            row_left = app.review_process_row.winfo_rootx()
            row_right = row_left + app.review_process_row.winfo_width()
            assert app.review_group.winfo_rootx() >= row_left
            assert (
                app.progress_group.winfo_rootx() + app.progress_group.winfo_width()
                <= row_right
            )
            assert app.review_group.winfo_width() >= 500
            assert app.progress_group.winfo_width() >= 500
            assert app.main_horizontal_scrollbar.winfo_manager() == ""

        app._on_canvas_configure(SimpleNamespace(width=760))
        assert app.main_horizontal_scrollbar.winfo_manager() == "grid"
        assert int(float(app.main_canvas.itemcget(app._canvas_window, "width"))) == 1120
        assert app.review_group.winfo_width() >= 500
        assert app.progress_group.winfo_width() >= 500
    finally:
        app.main_canvas.xview_moveto(0)
        app.withdraw()
        app.update_idletasks()


def test_v15_controls_use_one_visual_family_and_live_bottom_right(
    app: ProsApp,
) -> None:
    assert app.input_controls.pack_info()["side"] == "right"
    assert app.split_controls.grid_info()["sticky"] == "e"
    assert app.remove_pdf_button.cget("text") == "- Remove selected"
    assert app.remove_split_button.cget("text") == "- Remove split point"
    assert app.clear_list_button.cget("text") == "Clear"
    assert app.clear_split_button.cget("text") == "Clear"
    for button in (
        app.add_pdf_button,
        app.remove_pdf_button,
        app.clear_list_button,
        app.move_up_button,
        app.move_down_button,
        app.add_split_button,
        app.remove_split_button,
        app.clear_split_button,
        app.save_as_button,
        app.open_destination_button,
        app.open_completed_button,
        app.cancel_button,
        app.clear_job_button,
    ):
        assert isinstance(button, gui._SegmentButton)
        assert button.cget("relief") == "solid"
        assert int(button.cget("borderwidth")) == 1
        assert int(button.cget("highlightthickness")) == 0
        assert str(button.cget("font")) == str(app.neither_radio.cget("font"))
        assert int(button.cget("padx")) == gui.SEGMENT_PADX
        assert int(button.cget("pady")) == gui.SEGMENT_PADY
        assert button.cget("activebackground") == gui.SEGMENT_ACTIVE_BACKGROUND
        assert button.cget("activeforeground") == gui.SEGMENT_FOREGROUND
    for mode_button in app.mode_buttons:
        assert int(mode_button.cget("borderwidth")) == 1
        assert mode_button.cget("relief") == "solid"
        assert int(mode_button.grid_info()["padx"]) == 0
    assert app.process_button.cget("relief") == "solid"
    assert int(app.process_button.cget("borderwidth")) == 1
    assert int(app.process_button.cget("highlightthickness")) == 0
    assert str(app.process_button.cget("font")) == str(app.neither_radio.cget("font"))
    assert int(app.process_button.cget("padx")) == int(app.neither_radio.cget("padx"))
    assert int(app.process_button.cget("pady")) == int(app.neither_radio.cget("pady"))
    assert app.process_button.cget("activebackground") == app.neither_radio.cget(
        "activebackground"
    )


def test_v15_explicit_step_completion_and_primary_colour(
    app: ProsApp, tmp_path: Path
) -> None:
    function_card, function_badge, function_oval, _ = app._step_cards["function"]
    assert function_card.cget("highlightbackground") == gui.WORKFLOW_INCOMPLETE
    assert function_badge.itemcget(function_oval, "fill") == gui.WORKFLOW_INCOMPLETE

    # The visually selected startup mode is not complete until it is actively
    # confirmed, and Keep Separate additionally needs a processing function.
    app._on_mode_changed()
    assert function_card.cget("highlightbackground") == gui.WORKFLOW_INCOMPLETE
    app.grayscale_var.set(True)
    app._on_grayscale_changed()
    assert function_card.cget("highlightbackground") == gui.PRIMARY_BLUE

    source = tmp_path / "step-source.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output_dir = tmp_path / "step-output"
    output_dir.mkdir()
    row = _ready_row(source, 2)
    app._inputs.append(row)
    app.output_folder_var.set(str(output_dir))
    app.output_base_var.set("Step result")
    app._refresh_inputs(select_uid=row.uid)
    app._refresh_output_preview()
    app._refresh_state()

    for key in ("function", "input", "output", "review"):
        card, badge, oval, _text = app._step_cards[key]
        assert card.cget("highlightbackground") == gui.PRIMARY_BLUE
        assert badge.itemcget(oval, "fill") == gui.PRIMARY_BLUE
    process_card, process_badge, process_oval, _ = app._step_cards["process"]
    assert process_card.cget("highlightbackground") == gui.WORKFLOW_INCOMPLETE
    assert process_badge.itemcget(process_oval, "fill") == gui.WORKFLOW_INCOMPLETE
    assert app.process_button.cget("state") == "normal"
    assert app.process_button.cget("background") == app.neither_radio.cget(
        "background"
    ) == gui.PRIMARY_BLUE

    app._phase = "succeeded"
    app._refresh_state()
    assert process_card.cget("highlightbackground") == gui.PRIMARY_BLUE
    assert process_badge.itemcget(process_oval, "fill") == gui.PRIMARY_BLUE


def test_v15_split_clear_and_sequential_six_step_numbering(app: ProsApp) -> None:
    app.mode_var.set(StructureMode.SPLIT.value)
    app._on_mode_changed()
    app._split_rows[0].variable.set("2")
    app._add_split_row()
    app._split_rows[1].variable.set("4")
    app._refresh_state()
    assert app.clear_split_button.instate(["!disabled"])

    app._clear_split_points()
    assert len(app._split_rows) == 1
    assert app._split_rows[0].variable.get() == ""
    assert app.clear_split_button.instate(["disabled"])
    numbers = (
        app.function_step_badge.itemcget(app.function_step_text, "text"),
        app.input_step_badge.itemcget(app.input_step_text, "text"),
        app.options_step_badge.itemcget(app.options_step_text, "text"),
        app.output_step_badge.itemcget(app.output_step_text, "text"),
        app.review_step_badge.itemcget(app.review_step_text, "text"),
        app.progress_step_badge.itemcget(app.progress_step_text, "text"),
    )
    assert numbers == ("1", "2", "3", "4", "5", "6")


def test_v15_divider_does_not_move_when_work_status_expands(app: ProsApp) -> None:
    app.geometry("1280x820")
    app.mode_var.set(StructureMode.NEITHER.value)
    app._refresh_mode_panel()
    app.deiconify()
    app.update()
    try:
        before = (
            app.workflow_separator.winfo_rootx(),
            app.left_workflow.winfo_width(),
            app.right_workflow.winfo_width(),
        )
        app._phase = "processing"
        app.stage_var.set("Processing a PDF with a deliberately long status description " * 5)
        app._replace_text(app.status_area, "\n".join(["Long status entry"] * 30))
        app._replace_text(app.error_area, "\n".join(["Long error entry"] * 20))
        app._refresh_state()
        app.update()
        after = (
            app.workflow_separator.winfo_rootx(),
            app.left_workflow.winfo_width(),
            app.right_workflow.winfo_width(),
        )
        assert after == before
    finally:
        app._phase = "editing"
        app.stage_var.set("Ready")
        app.withdraw()
        app.update_idletasks()


def test_focus_in_reveals_offscreen_workflow_controls(
    app: ProsApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = app.output_base_entry
    callbacks: list[object] = []
    x_moves: list[float] = []
    y_moves: list[float] = []
    monkeypatch.setattr(app, "after_idle", lambda callback: callbacks.append(callback) or "idle")
    monkeypatch.setattr(app, "update_idletasks", lambda: None)
    monkeypatch.setattr(target, "winfo_exists", lambda: 1)
    monkeypatch.setattr(target, "winfo_ismapped", lambda: 1)
    monkeypatch.setattr(target, "winfo_rootx", lambda: 900)
    monkeypatch.setattr(target, "winfo_rooty", lambda: 1000)
    monkeypatch.setattr(target, "winfo_width", lambda: 180)
    monkeypatch.setattr(target, "winfo_height", lambda: 24)
    monkeypatch.setattr(app.main_content, "winfo_rootx", lambda: 0)
    monkeypatch.setattr(app.main_content, "winfo_rooty", lambda: 0)
    monkeypatch.setattr(app.main_canvas, "bbox", lambda _tag: (0, 0, 1120, 1400))
    monkeypatch.setattr(app.main_canvas, "winfo_width", lambda: 760)
    monkeypatch.setattr(app.main_canvas, "winfo_height", lambda: 400)
    monkeypatch.setattr(app.main_canvas, "canvasx", lambda _value: 0.0)
    monkeypatch.setattr(app.main_canvas, "canvasy", lambda _value: 0.0)
    monkeypatch.setattr(app.main_canvas, "xview_moveto", x_moves.append)
    monkeypatch.setattr(app.main_canvas, "yview_moveto", y_moves.append)

    app._on_descendant_focus_in(SimpleNamespace(widget=target))
    assert len(callbacks) == 1
    callbacks[0]()  # type: ignore[operator]
    assert x_moves and x_moves[-1] > 0
    assert y_moves and y_moves[-1] > 0


def test_outer_wheel_does_not_double_scroll_nested_controls(
    app: ProsApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    outer_scrolls: list[tuple[int, str]] = []
    target: list[tk.Widget] = [app.input_tree]
    monkeypatch.setattr(
        app, "winfo_containing", lambda _x_root, _y_root: target[0]
    )
    monkeypatch.setattr(
        app.main_canvas,
        "yview_scroll",
        lambda amount, unit: outer_scrolls.append((amount, unit)),
    )
    event = SimpleNamespace(x_root=10, y_root=10, delta=-120)

    nested_widgets = (
        app.input_tree,
        app.output_preview,
        app.range_list,
        app.status_area,
        app.error_area,
        app.status_scrollbar,
        app.error_scrollbar,
    )
    for widget in nested_widgets:
        target[0] = widget
        assert app._on_mouse_wheel(event) == "break"
    embedded_child = ttk.Label(app.status_area, text="embedded")
    try:
        target[0] = embedded_child
        assert app._on_mouse_wheel(event) == "break"
    finally:
        embedded_child.destroy()
    assert outer_scrolls == []

    target[0] = app.function_step_label
    assert app._on_mouse_wheel(event) == "break"
    assert outer_scrolls == [(1, "units")]

    target[0] = app.clear_job_button
    assert app._on_mouse_wheel(event) is None
    assert outer_scrolls == [(1, "units")]


def test_all_mode_drop_parses_tcl_paths_exactly_and_rechecks_state_guards(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = (
        tmp_path / "first file.pdf",
        tmp_path / "{braces} résumé.pdf",
        tmp_path / "文档.pdf",
    )
    app.mode_var.set(StructureMode.JOIN.value)
    app._refresh_mode_panel()
    app._dnd_available = True
    captured: list[tuple[tuple[str, ...], str]] = []
    monkeypatch.setattr(
        app,
        "_ingest_pdf_paths",
        lambda values, *, origin: captured.append((tuple(values), origin)) or len(values),
    )
    monkeypatch.setattr(app, "after_idle", lambda callback: callback() or "after-idle")
    data = app.tk.call("list", *(str(path) for path in paths))
    event = SimpleNamespace(data=data)

    assert app._on_dnd_enter(event) == gui.COPY
    assert app.drop_zone.cget("background") == gui.DROP_ZONE_ACTIVE_BG
    assert app._on_dnd_position(event) == gui.COPY
    assert app._on_dnd_drop(event) == gui.COPY
    assert captured == [(tuple(str(path) for path in paths), "drop")]
    assert app.drop_zone.cget("background") == gui.DROP_ZONE_BG

    captured.clear()
    invalid_paths = (tmp_path / "notes.txt", tmp_path / "image.png")
    invalid_event = SimpleNamespace(
        data=app.tk.call("list", *(str(path) for path in invalid_paths))
    )
    assert app._on_dnd_drop(invalid_event) == gui.REFUSE_DROP
    assert captured == []

    partial_paths = (
        tmp_path / "notes.txt",
        tmp_path / "kept file.PDF",
        tmp_path / "kept file.PDF",
    )
    partial_event = SimpleNamespace(
        data=app.tk.call("list", *(str(path) for path in partial_paths))
    )
    assert app._on_dnd_drop(partial_event) == gui.COPY
    assert captured == [(tuple(str(path) for path in partial_paths), "drop")]

    captured.clear()
    app._inputs.append(_ready_row(partial_paths[1], 1))
    duplicate_event = SimpleNamespace(data=app.tk.call("list", str(partial_paths[1])))
    assert app._on_dnd_drop(duplicate_event) == gui.REFUSE_DROP
    assert captured == []
    app._inputs.clear()

    app.mode_var.set(StructureMode.NEITHER.value)
    app._refresh_mode_panel()
    assert app._on_dnd_enter(event) == gui.COPY
    assert app._on_dnd_drop(event) == gui.COPY
    app.mode_var.set(StructureMode.SPLIT.value)
    app._refresh_mode_panel()
    assert app._on_dnd_enter(event) == gui.COPY
    assert app._on_dnd_drop(event) == gui.COPY
    assert [origin for _values, origin in captured] == ["drop", "drop"]
    captured.clear()
    app._phase = "processing"
    assert app._on_dnd_position(event) == gui.REFUSE_DROP
    app._phase = "editing"
    app.mode_var.set(StructureMode.JOIN.value)
    app._inputs[:] = [
        _ready_row(tmp_path / f"{index}.pdf", 1)
        for index in range(gui.MAX_JOIN_INPUTS)
    ]
    assert app._on_dnd_position(event) == gui.REFUSE_DROP
    assert captured == []


def test_drag_drop_setup_has_a_picker_fallback(
    app: ProsApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    app._teardown_drag_drop()
    with monkeypatch.context() as patch:
        patch.setattr(
            gui.TkinterDnD,
            "require",
            lambda _root: (_ for _ in ()).throw(tk.TclError("tkdnd unavailable")),
        )
        app._setup_drag_drop()
        assert app._dnd_available is False
        app.mode_var.set(StructureMode.JOIN.value)
        app._refresh_drop_zone()
        assert app.drop_zone_text_var.get() == "Click to add PDF files"
        assert not app.add_pdf_button.instate(["disabled"])
    app._setup_drag_drop()


def test_picker_and_drop_share_ordered_pdf_validation(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first with spaces.PDF"
    second = tmp_path / "{second} café.pdf"
    non_pdf = tmp_path / "notes.txt"
    directory = tmp_path / "folder.pdf"
    first.write_bytes(b"%PDF-1.7\n")
    second.write_bytes(b"%PDF-1.7\n")
    non_pdf.write_text("not a pdf", encoding="utf-8")
    directory.mkdir()
    app.mode_var.set(StructureMode.JOIN.value)
    app._refresh_mode_panel()
    monkeypatch.setattr(app, "_request_inspection", lambda immediate=False: None)

    accepted = app._ingest_pdf_paths(
        (first, second, first, non_pdf, directory), origin="drop"
    )

    assert accepted == 2
    assert [row.path for row in app._inputs] == [first, second]
    status = app.status_area.get("1.0", "end").casefold()
    assert "already in the list" in status
    assert "only .pdf" in status
    assert "not available" in status


def test_v15_picker_multiplicity_and_mode_reclick_shortcut(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    first.write_bytes(b"%PDF-1.7\n")
    second.write_bytes(b"%PDF-1.7\n")
    plural_calls: list[str] = []
    singular_calls: list[str] = []
    monkeypatch.setattr(app, "_request_inspection", lambda immediate=False: None)
    monkeypatch.setattr(
        gui.filedialog,
        "askopenfilenames",
        lambda **_options: plural_calls.append("multi") or (str(first), str(second)),
    )
    monkeypatch.setattr(
        gui.filedialog,
        "askopenfilename",
        lambda **_options: singular_calls.append("single") or str(first),
    )

    app._on_mode_changed()  # first click confirms the startup Keep mode
    app._add_pdfs()
    assert plural_calls == ["multi"]
    assert [row.path for row in app._inputs] == [first, second]

    app._inputs.clear()
    app.mode_var.set(StructureMode.SPLIT.value)
    app._on_mode_changed()
    app._add_pdfs()
    assert singular_calls == ["single"]
    assert [row.path for row in app._inputs] == [first]

    # A second activation of the already-selected segment within four seconds
    # queues the picker. A first activation or a late repeat does not.
    app._inputs.clear()
    app.mode_var.set(StructureMode.JOIN.value)
    ticks = iter((100.0, 102.0, 107.0, 108.0))
    monkeypatch.setattr(gui.time, "monotonic", lambda: next(ticks))
    queued: list[object] = []
    monkeypatch.setattr(
        app, "after_idle", lambda callback: queued.append(callback) or "idle"
    )
    app._on_mode_changed()
    assert queued == []
    app._on_mode_changed()
    assert len(queued) == 1
    app._on_mode_changed()
    assert len(queued) == 1
    app._on_mode_changed()
    assert len(queued) == 2


def test_v15_keep_separate_supports_ordered_multi_file_outputs(
    app: ProsApp, tmp_path: Path
) -> None:
    source_dir_a = tmp_path / "a"
    source_dir_b = tmp_path / "b"
    output_dir = tmp_path / "out"
    source_dir_a.mkdir()
    source_dir_b.mkdir()
    output_dir.mkdir()
    first = source_dir_a / "First source.pdf"
    second = source_dir_b / "Second source.pdf"
    first.write_bytes(b"%PDF-1.7\n")
    second.write_bytes(b"%PDF-1.7\n")

    app._on_mode_changed()
    app.compress_var.set(True)
    app._inputs[:] = [_ready_row(first, 2), _ready_row(second, 3)]
    app.output_folder_var.set(str(output_dir))
    app.output_base_var.set("Ignored for multiple inputs")
    app._refresh_inputs(select_uid=app._inputs[0].uid)
    app._refresh_output_preview()
    app._refresh_state()

    assert app._input_limit() == gui.MAX_SEPARATE_INPUTS == 12
    assert app.output_base_entry.cget("state") == "disabled"
    assert app.output_name_label.cget("text") == "File names"
    assert "Each source name" in app.output_name_hint.cget("text")
    assert [app.output_preview.get(index) for index in range(2)] == [
        "First source - Cprs.pdf",
        "Second source - Cprs.pdf",
    ]
    assert app.review_action_var.get() == (
        "Process 2 PDFs and keep them as separate files"
    )
    assert app.process_button.cget("text") == "Create 2 PDFs"
    assert app.process_button.cget("state") == "normal"
    request = app._make_job_request()
    assert request.input_paths == [first, second]
    assert list(gui.build_output_paths(request)) == [
        output_dir / "First source - Cprs.pdf",
        output_dir / "Second source - Cprs.pdf",
    ]


def test_v15_keep_separate_rejects_case_insensitive_duplicate_output_names(
    app: ProsApp, tmp_path: Path
) -> None:
    first_dir = tmp_path / "one"
    second_dir = tmp_path / "two"
    output_dir = tmp_path / "out"
    first_dir.mkdir()
    second_dir.mkdir()
    output_dir.mkdir()
    first = first_dir / "Report.pdf"
    second = second_dir / "report.PDF"
    first.write_bytes(b"%PDF-1.7\n")
    second.write_bytes(b"%PDF-1.7\n")

    app._on_mode_changed()
    app.grayscale_var.set(True)
    app._inputs[:] = [_ready_row(first, 1), _ready_row(second, 1)]
    app.output_folder_var.set(str(output_dir))
    app._refresh_inputs(select_uid=app._inputs[0].uid)
    app._refresh_output_preview()
    app._refresh_state()

    assert app.process_button.cget("state") == "disabled"
    assert "more than one output would be named" in app.error_area.get(
        "1.0", "end"
    ).casefold()
    output_card, _badge, _oval, _text = app._step_cards["output"]
    assert output_card.cget("highlightbackground") == gui.WORKFLOW_INCOMPLETE


def test_join_rows_require_order_handle_and_drag_threshold_to_reorder(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rows = [_ready_row(tmp_path / f"file-{index}.pdf", index + 1) for index in range(3)]
    app._inputs[:] = rows
    app.mode_var.set(StructureMode.JOIN.value)
    app._refresh_mode_panel()
    app._refresh_inputs(select_uid=rows[0].uid)
    assert isinstance(app.input_tree.identify_region(1, 1), str)
    hit_rows = {10: rows[0].uid, 14: rows[2].uid, 30: rows[2].uid}
    monkeypatch.setattr(app.input_tree, "identify_row", lambda y: hit_rows.get(y, ""))
    monkeypatch.setattr(app.input_tree, "identify_region", lambda _x, _y: "cell")
    monkeypatch.setattr(
        app.input_tree,
        "identify_column",
        lambda x: "#1" if x < 50 else "#6" if x > 400 else "#2",
    )

    for non_handle_x in (100, 500):
        app._on_tree_drag_start(SimpleNamespace(x=non_handle_x, y=10))
        app._on_tree_drag_motion(SimpleNamespace(x=non_handle_x, y=30))
        app._on_tree_drag_end(SimpleNamespace(x=non_handle_x, y=30))
        assert [row.uid for row in app._inputs] == [row.uid for row in rows]

    app._on_tree_drag_start(SimpleNamespace(x=10, y=10))
    app._on_tree_drag_motion(SimpleNamespace(x=10, y=14))
    app._on_tree_drag_end(SimpleNamespace(x=10, y=14))
    assert [row.uid for row in app._inputs] == [row.uid for row in rows]

    app._on_tree_drag_start(SimpleNamespace(x=10, y=10))
    app._on_tree_drag_motion(SimpleNamespace(x=10, y=30))
    app._on_tree_drag_end(SimpleNamespace(x=10, y=30))

    assert [row.uid for row in app._inputs] == [rows[1].uid, rows[2].uid, rows[0].uid]
    assert app.input_tree.selection() == (rows[0].uid,)
    assert [app.input_tree.set(row.uid, "order") for row in app._inputs] == [
        "1",
        "2",
        "3",
    ]


def test_clear_job_resets_the_workflow_without_deleting_files(
    app: ProsApp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    output = tmp_path / "completed.pdf"
    source.write_bytes(b"%PDF-1.7\n")
    output.write_bytes(b"%PDF-1.7\n")
    app._inputs.append(_ready_row(source, 3))
    app.mode_var.set(StructureMode.JOIN.value)
    app.compress_var.set(True)
    app.grayscale_var.set(True)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Draft")
    app._last_outputs = (output,)
    app.show_password_var.set(True)
    app._toggle_password_visibility()
    assert app.common_password_entry.cget("show") == ""
    assert app.per_file_password_entry.cget("show") == ""
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *args, **kwargs: True)

    app._clear_job()

    assert app._inputs == []
    assert app.mode_var.get() == StructureMode.NEITHER.value
    assert app.compress_var.get() is False
    assert app.grayscale_var.get() is False
    assert app.show_password_var.get() is False
    assert app.common_password_entry.cget("show") == "*"
    assert app.per_file_password_entry.cget("show") == "*"
    assert app.output_base_var.get() == ""
    assert app._last_outputs == ()
    assert source.is_file()
    assert output.is_file()
    assert "were not changed" in app.status_area.get("1.0", "end")


def test_review_and_primary_action_follow_the_selected_workflow(
    app: ProsApp, tmp_path: Path
) -> None:
    app._inputs[:] = [
        _ready_row(tmp_path / f"part-{index}.pdf", index + 1) for index in range(3)
    ]
    app.mode_var.set(StructureMode.JOIN.value)
    app.compress_var.set(True)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Combined")
    app._refresh_mode_panel()
    app._refresh_inputs()
    app._refresh_output_preview()
    app._refresh_state()

    assert app.review_action_var.get() == "Combine 3 PDFs into one PDF"
    assert app.review_processing_var.get() == "Reduce file size"
    assert app.review_output_var.get() == "Combined - Join - Cprs.pdf"
    assert app.process_button.cget("text") == "Combine 3 PDFs"

    app._inputs[:] = [app._inputs[-1]]
    app.mode_var.set(StructureMode.SPLIT.value)
    app._split_rows[0].variable.set("1")
    app.output_base_var.set("Split")
    app._refresh_mode_panel()
    app._refresh_inputs()
    app._refresh_ranges()
    app._refresh_output_preview()
    app._refresh_state()
    assert app.review_action_var.get() == "Split one PDF into 2 PDFs"
    assert app.process_button.cget("text") == "Create 2 PDFs"


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


def test_incomplete_or_invalid_split_never_previews_concrete_outputs(
    app: ProsApp, tmp_path: Path
) -> None:
    row = _ready_row(tmp_path / "source.pdf", 10)
    app._inputs.append(row)
    app._refresh_inputs(select_uid=row.uid)
    app.mode_var.set(StructureMode.SPLIT.value)
    app.output_folder_var.set(str(tmp_path))
    app.output_base_var.set("Split result")
    app._on_mode_changed()

    expected_hint = "Complete valid split points to preview output files."
    assert app.output_preview.get(0) == expected_hint
    assert app.review_action_var.get() == "Split one PDF into multiple PDFs"
    assert app.review_output_var.get() == expected_hint.removesuffix(".")
    assert app.process_button.cget("state") == "disabled"

    app._split_rows[0].variable.set("not-a-page")
    app._refresh_output_preview()
    app._refresh_state()
    assert app.output_preview.get(0) == expected_hint
    assert app.review_action_var.get() == "Split one PDF into multiple PDFs"
    assert not any(
        str(app.output_preview.get(index)).lower().endswith(".pdf")
        for index in range(app.output_preview.size())
    )


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
    app._on_mode_changed()
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
    app._on_mode_changed()
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
    app._on_mode_changed()
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
    assert app.readiness_canvas.itemcget(app.readiness_indicator, "fill") == gui.SUCCESS_GREEN
    assert app.readiness_text_var.get() == "Completed"

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
    clock = iter((100.0, 101.0, 102.0))
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

    # A periodic core heartbeat advances the visible status even if its
    # overall percentage is unchanged.
    app._show_progress_event(
        {
            "job_id": request.job_id,
            "stage": "write",
            "percent": 42,
            "phase_percent": 24,
            "file_index": 2,
            "file_count": 3,
            "message": "Preparing the second PDF output.",
        }
    )
    assert app._last_progress_at == 102.0
    assert app.file_progress_var.get() == 24
    assert "Preparing the second PDF output" in app.status_area.get("1.0", "end")

    app._progress_stage = "compress"
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
