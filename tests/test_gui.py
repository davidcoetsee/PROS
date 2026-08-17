from __future__ import annotations

from pathlib import Path

import pytest

from pros.gui import ProsApp, _InputRow
from pros.models import PdfInfo, StructureMode


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
    window._inputs.clear()
    window._selected_input_uid = None
    window.remove_password_var.set(False)
    window.compress_var.set(False)
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
    while len(window._split_rows) > 1:
        row = window._split_rows.pop()
        row.frame.destroy()
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
        "progress_bar",
        "status_area",
        "error_area",
        "open_completed_button",
        "open_destination_button",
    )
    assert all(hasattr(app, name) for name in required_widgets)
    assert app.mode_var.get() == StructureMode.NEITHER.value
    assert app.password_group.cget("style") == "Disabled.TLabelframe"
    assert app.process_button.instate(["disabled"])
    assert app.cancel_button.instate(["disabled"])
    assert app.open_completed_button.instate(["disabled"])
    assert app.open_destination_button.instate(["disabled"])
    assert Path(app.input_folder_var.get()) == app.app_dir
    assert Path(app.output_folder_var.get()) == app.app_dir
    assert app.options_group.winfo_manager() == ""


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

    assert not app.process_button.instate(["disabled"])
    (tmp_path / "Result - Cprs.pdf").write_bytes(b"existing")
    app._refresh_state()
    assert app.process_button.instate(["disabled"])
    assert "already exists" in app.error_area.get("1.0", "end").casefold()
