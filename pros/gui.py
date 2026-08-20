"""Tk/ttk desktop interface for the portable PROS PDF application.

The GUI deliberately owns no PDF mutation logic.  It keeps an editable job
draft, performs responsive inspection/preflight work on background threads,
and launches the authoritative PDF worker in a separate spawned process.
"""

from __future__ import annotations

import multiprocessing
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import Enum
from itertools import pairwise
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

from .models import JobRequest, JobResult, PdfInfo, StructureMode

try:
    from .models import CompressionLevel
except ImportError:  # pragma: no cover - permits mixed-version source trees
    class CompressionLevel(str, Enum):
        STANDARD = "standard"
        ULTRA = "ultra"
from .naming import build_output_paths, normalize_output_base, suggest_output_base
from .pdf_engine import inspect_pdf
from .validation import (
    LARGE_FILE_NOTICE_BYTES,
    MAX_JOIN_INPUTS,
    MAX_SPLIT_OUTPUTS,
    calculate_split_ranges,
    preflight,
    validate_output_base,
    validate_split_points,
)

try:
    # The settled core API is ``run_worker``.  Keeping the local alias makes
    # the process boundary explicit and remains compatible with early builds.
    from .worker import run_worker as worker_entry
except ImportError:  # pragma: no cover - compatibility with an early snapshot
    from .worker import worker_entry  # type: ignore[no-redef]

TOTAL_JOB_TIMEOUT_SECONDS = 30 * 60
NO_PROGRESS_TIMEOUT_SECONDS = 5 * 60
CANCEL_GRACE_SECONDS = 12
POLL_INTERVAL_MS = 100
PASSWORD_DEBOUNCE_MS = 350

HEADER_TAGLINE = (
    "PROS - Free Basic PDF Editor: [P]asswords removed · file sizes [R]educed · "
    "[O]rganise & join · [S]plit files"
)
LARGE_FILE_STATUS = "This file is larger than 120 MB and may take longer than usual to process"
NO_PROGRESS_CANCELLING_STATUS = (
    "No progress was detected for five minutes. We have requested safe cancellation "
    "and are cleaning up temporary files."
)
NO_PROGRESS_CLEANUP_STATUS = "Cleanup is complete. No completed output file was created."
NO_PROGRESS_FORCED_ERROR = (
    "No progress was detected for five minutes. The job was forcibly stopped, and all "
    "temporary files, partial files, and new output files created for this job were removed."
)
FORCED_CANCELLATION_ERROR = (
    "Processing was cancelled because it did not show progress for five minutes. The task "
    "did not stop in time, so it was forcibly ended. Any temporary, partial, or newly "
    "created output files from this task were removed."
)
PROCESS_DISABLED_BG = "#d1d5db"
PROCESS_DISABLED_FG = "#6b7280"
PROCESS_ENABLED_BG = "#1769aa"
PROCESS_ENABLED_FG = "#ffffff"
SPLIT_INVALID_BG = "#ff5a5f"
SPLIT_NORMAL_BG = "#ffffff"


def _application_dir() -> Path:
    """Return the portable executable directory, never PyInstaller _MEIPASS."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _resource_candidates(filename: str) -> Iterable[Path]:
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        yield Path(bundle) / filename
    yield _application_dir() / filename
    yield Path(__file__).resolve().parent / filename
    yield Path(__file__).resolve().parent.parent / filename


def _resource_path(filename: str) -> Path | None:
    for candidate in _resource_candidates(filename):
        if candidate.is_file():
            return candidate
    return None


def _format_size(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _canonical_path(path: Path) -> str:
    try:
        absolute = path.expanduser().resolve(strict=False)
    except OSError:
        absolute = path.expanduser().absolute()
    return os.path.normcase(str(absolute)).casefold()


@dataclass(slots=True)
class _InputRow:
    path: Path
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)
    info: PdfInfo | None = None
    password_override: str = field(default="", repr=False)
    inspection_pending: bool = True


@dataclass(slots=True)
class _SplitRow:
    frame: ttk.Frame
    label: ttk.Label
    variable: tk.StringVar
    entry: tk.Entry
    clear_after: str | None = None
    validate_after: str | None = None


class ProsApp(tk.Tk):
    """Complete UI1-UI32 desktop application window."""

    def __init__(self) -> None:
        super().__init__(className="PROS")
        self.title("PROS — Portable PDF Processing")
        self.geometry("1080x860")
        self.minsize(880, 680)
        self.option_add("*tearOff", False)

        self.app_dir = _application_dir()
        self.input_dir = self.app_dir
        self._inputs: list[_InputRow] = []
        self._split_rows: list[_SplitRow] = []
        self._selected_split_index = 0
        self._selected_input_uid: str | None = None
        self._loading_password = False
        self._setting_base = False
        self._base_user_edited = False
        self._phase = "editing"
        self._runtime_error = ""
        self._last_outputs: tuple[Path, ...] = ()
        self._last_destination: Path | None = None
        self._last_status_line = ""
        self._last_progress_log_key: tuple[str, int, str] | None = None
        self._last_progress_signature: tuple[str, int, float] | None = None
        self._progress_stage = ""
        self._progress_file_index = 0
        self._progress_file_count = 0
        self._progress_target = "current PDF"
        self._display_phase_percent = 0.0
        self._synthetic_progress_interval = 1.0
        self._next_synthetic_progress_at = 0.0

        self._inspection_generation = 0
        self._inspection_queue: queue.Queue[tuple[int, str, PdfInfo]] = queue.Queue()
        self._inspection_semaphore = threading.Semaphore(2)
        self._inspection_after: str | None = None
        self._authoritative_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        self._worker_process: multiprocessing.Process | None = None
        self._worker_queue: Any = None
        self._cancel_event: Any = None
        self._active_request: JobRequest | None = None
        self._active_job_id: str | None = None
        self._job_started_at = 0.0
        self._job_started_wall = 0.0
        self._last_progress_at = 0.0
        self._cancel_requested_at: float | None = None
        self._timeout_reason: str | None = None
        self._terminal_received = False
        self._dead_process_polls = 0
        self.remove_password_var = tk.BooleanVar(value=False)
        self.compress_var = tk.BooleanVar(value=False)
        self.grayscale_var = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value=StructureMode.NEITHER.value)
        self.input_folder_var = tk.StringVar(value=str(self.input_dir))
        self.use_common_password_var = tk.BooleanVar(value=False)
        self.common_password_var = tk.StringVar(value="")
        self.per_file_password_var = tk.StringVar(value="")
        self.show_password_var = tk.BooleanVar(value=False)
        self.split_selected_var = tk.IntVar(value=0)
        self.output_folder_var = tk.StringVar(value=str(self.app_dir))
        self.output_base_var = tk.StringVar(value="")
        self.stage_var = tk.StringVar(value="Ready")
        self.progress_var = tk.DoubleVar(value=0.0)
        self.file_progress_var = tk.DoubleVar(value=0.0)
        self.file_progress_text_var = tk.StringVar(value="No file is being processed.")
        self.readiness_text_var = tk.StringVar(value="Not ready")

        self._configure_style()
        self._build_menu()
        self._build_ui()
        self._set_icon()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(POLL_INTERVAL_MS, self._poll_background_events)
        self._refresh_mode_panel()
        self._refresh_inputs()
        self._refresh_ranges()
        self._refresh_output_preview()
        self._refresh_state()

    # ------------------------------------------------------------------ setup
    def _configure_style(self) -> None:
        style = ttk.Style(self)
        available = style.theme_names()
        for candidate in ("vista", "xpnative", "clam"):
            if candidate in available:
                try:
                    style.theme_use(candidate)
                    break
                except tk.TclError:
                    continue
        style.configure("Title.TLabel", font=("Segoe UI", 18, "bold"))
        style.configure("Subtitle.TLabel", foreground="#4a5568")
        style.configure("Section.TLabelframe", padding=10)
        style.configure("Section.TLabelframe.Label", font=("Segoe UI", 10, "bold"))
        style.configure("Disabled.TLabelframe", padding=10)
        style.configure(
            "Disabled.TLabelframe.Label",
            font=("Segoe UI", 10, "bold"),
            foreground="#777777",
        )
        style.configure("Primary.TButton", font=("Segoe UI", 9, "bold"))
        style.map("Primary.TButton", foreground=[("disabled", "#777777")])
        style.configure("Danger.TLabel", foreground="#9b1c1c")
        style.configure("Hint.TLabel", foreground="#5f6b7a")
        style.configure("SectionHeading.TLabel", font=("Segoe UI", 10, "bold"))

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_menu = tk.Menu(menubar)
        file_menu.add_command(label="Exit", command=self._on_close)
        menubar.add_cascade(label="File", menu=file_menu)
        help_menu = tk.Menu(menubar)
        help_menu.add_command(label="About PROS", command=self._show_about)
        help_menu.add_command(
            label="Third-party notices", command=self._show_third_party_notices
        )
        menubar.add_cascade(label="Help", menu=help_menu)
        self.configure(menu=menubar)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)
        self.main_canvas = tk.Canvas(outer, highlightthickness=0, borderwidth=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=self.main_canvas.yview)
        self.main_canvas.configure(yscrollcommand=scrollbar.set)
        self.main_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.main_content = ttk.Frame(self.main_canvas, padding=(18, 14, 18, 18))
        self._canvas_window = self.main_canvas.create_window(
            (0, 0), window=self.main_content, anchor="nw"
        )
        self.main_content.bind("<Configure>", self._on_content_configure)
        self.main_canvas.bind("<Configure>", self._on_canvas_configure)
        self.main_canvas.bind_all("<MouseWheel>", self._on_mouse_wheel, add="+")

        header = ttk.Frame(self.main_content)
        header.pack(fill="x", pady=(0, 10))
        header.columnconfigure(1, weight=1)
        self.title_label = ttk.Label(header, text="PROS", style="Title.TLabel")
        self.title_label.grid(row=0, column=0, sticky="nw")
        self.tagline_label = ttk.Label(
            header,
            text=HEADER_TAGLINE.removeprefix("PROS"),
            style="Subtitle.TLabel",
            wraplength=840,
            justify="left",
        )
        self.tagline_label.grid(row=0, column=1, sticky="ew", padx=(3, 0), pady=(7, 0))

        self._build_function_group()
        self._build_input_group()
        self._build_password_group()
        self._build_options_group()
        self._build_output_group()
        self._build_progress_group()

    def _build_function_group(self) -> None:
        group = ttk.LabelFrame(
            self.main_content, text="Function Selection", style="Section.TLabelframe"
        )
        group.pack(fill="x", pady=5)
        group.columnconfigure(5, weight=1)
        self.function_group = group

        self.remove_password_check = ttk.Checkbutton(
            group,
            text="Remove password",
            variable=self.remove_password_var,
            command=self._on_password_function_changed,
        )
        self.remove_password_check.grid(row=0, column=0, sticky="w", padx=(0, 22))
        self.compress_check = ttk.Checkbutton(
            group,
            text="Compress PDF",
            variable=self.compress_var,
            command=self._on_compression_changed,
        )
        self.compress_check.grid(row=0, column=1, sticky="w", padx=(0, 30))

        ttk.Label(group, text="Structure:").grid(row=0, column=2, sticky="e", padx=(0, 8))
        self.join_radio = ttk.Radiobutton(
            group,
            text="Join",
            value=StructureMode.JOIN.value,
            variable=self.mode_var,
            command=self._on_mode_changed,
        )
        self.split_radio = ttk.Radiobutton(
            group,
            text="Split",
            value=StructureMode.SPLIT.value,
            variable=self.mode_var,
            command=self._on_mode_changed,
        )
        self.neither_radio = ttk.Radiobutton(
            group,
            text="Neither",
            value=StructureMode.NEITHER.value,
            variable=self.mode_var,
            command=self._on_mode_changed,
        )
        self.join_radio.grid(row=0, column=3, sticky="w", padx=4)
        self.split_radio.grid(row=0, column=4, sticky="w", padx=4)
        self.neither_radio.grid(row=0, column=5, sticky="w", padx=4)

    def _build_input_group(self) -> None:
        group = ttk.LabelFrame(
            self.main_content, text="Input Files", style="Section.TLabelframe"
        )
        group.pack(fill="both", expand=True, pady=5)
        group.columnconfigure(1, weight=1)
        group.rowconfigure(1, weight=1)
        self.input_group = group

        ttk.Label(group, text="Current input folder:").grid(row=0, column=0, sticky="w")
        self.input_folder_entry = ttk.Entry(
            group, textvariable=self.input_folder_var, state="readonly"
        )
        self.input_folder_entry.grid(row=0, column=1, sticky="ew", padx=8)
        self.add_pdf_button = ttk.Button(group, text="[+] Add PDF", command=self._add_pdfs)
        self.add_pdf_button.grid(row=0, column=2, sticky="e")

        columns = ("order", "filename", "protection", "size", "pages", "status")
        self.input_tree = ttk.Treeview(
            group,
            columns=columns,
            show="headings",
            height=6,
            selectmode="browse",
        )
        headings = {
            "order": ("#", 42, "center"),
            "filename": ("Filename", 290, "w"),
            "protection": ("Protection", 105, "center"),
            "size": ("Size", 85, "e"),
            "pages": ("Pages", 65, "center"),
            "status": ("File check", 230, "w"),
        }
        for column, (title, width, anchor) in headings.items():
            self.input_tree.heading(column, text=title)
            self.input_tree.column(
                column,
                width=width,
                minwidth=40,
                stretch=column in {"filename", "status"},
                anchor=anchor,
            )
        tree_scroll = ttk.Scrollbar(group, orient="vertical", command=self.input_tree.yview)
        self.input_tree.configure(yscrollcommand=tree_scroll.set)
        self.input_tree.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(9, 6))
        tree_scroll.grid(row=1, column=3, sticky="ns", pady=(9, 6))
        self.input_tree.bind("<<TreeviewSelect>>", self._on_input_selected)

        buttons = ttk.Frame(group)
        buttons.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.remove_pdf_button = ttk.Button(
            buttons, text="[-] Remove PDF", command=self._remove_selected_pdf
        )
        self.remove_pdf_button.pack(side="left")
        self.clear_list_button = ttk.Button(
            buttons, text="Clear List", command=self._clear_inputs
        )
        self.clear_list_button.pack(side="left", padx=7)
        ttk.Label(
            buttons,
            text="Only local PDF files are accepted; original files are never changed.",
            style="Hint.TLabel",
        ).pack(side="right")

    def _build_password_group(self) -> None:
        group = ttk.LabelFrame(
            self.main_content, text="Password", style="Disabled.TLabelframe"
        )
        group.pack(fill="x", pady=5)
        group.columnconfigure(1, weight=1)
        self.password_group = group

        self.use_common_password_check = ttk.Checkbutton(
            group,
            text="Use one password for all protected PDFs",
            variable=self.use_common_password_var,
            command=self._on_common_password_mode_changed,
        )
        self.use_common_password_check.grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.common_password_entry = ttk.Entry(
            group, textvariable=self.common_password_var, show="*", width=30
        )
        self.common_password_entry.grid(row=0, column=1, sticky="ew", padx=(0, 12))

        self.selected_password_file_label = ttk.Label(
            group, text="Per-file password: select a protected PDF"
        )
        self.selected_password_file_label.grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.per_file_password_entry = ttk.Entry(
            group, textvariable=self.per_file_password_var, show="*", width=30
        )
        self.per_file_password_entry.grid(
            row=1, column=1, sticky="ew", padx=(0, 12), pady=(8, 0)
        )
        self.show_password_check = ttk.Checkbutton(
            group,
            text="Show password",
            variable=self.show_password_var,
            command=self._toggle_password_visibility,
        )
        self.show_password_check.grid(row=0, column=2, rowspan=2, sticky="w")

        self.common_password_entry.bind("<KeyRelease>", self._on_password_key)
        self.per_file_password_entry.bind("<KeyRelease>", self._on_per_file_password_key)

    def _build_options_group(self) -> None:
        group = ttk.LabelFrame(
            self.main_content, text="Join or Split Options", style="Section.TLabelframe"
        )
        group.pack(fill="x", pady=5)
        self.options_group = group

        self.join_options = ttk.Frame(group)
        ttk.Label(
            self.join_options,
            text="PDFs will be joined from top to bottom in the displayed file order.",
        ).pack(side="left")
        self.move_down_button = ttk.Button(
            self.join_options, text="Down", width=10, command=lambda: self._move_input(1)
        )
        self.move_down_button.pack(side="right")
        self.move_up_button = ttk.Button(
            self.join_options, text="Up", width=10, command=lambda: self._move_input(-1)
        )
        self.move_up_button.pack(side="right", padx=7)

        self.split_options = ttk.Frame(group)
        self.split_options.columnconfigure(0, weight=1)
        left = ttk.Frame(self.split_options)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 15))
        right = ttk.Frame(self.split_options)
        right.grid(row=0, column=1, sticky="nsew")
        ttk.Label(
            left,
            text="Enter the last page of each segment (the final segment is automatic):",
        ).pack(anchor="w")
        self.split_entries_frame = ttk.Frame(left)
        self.split_entries_frame.pack(fill="x", pady=5)
        split_buttons = ttk.Frame(left)
        split_buttons.pack(fill="x")
        self.add_split_button = ttk.Button(
            split_buttons, text="[+] Add Split", command=self._add_split_row
        )
        self.add_split_button.pack(side="left")
        self.remove_split_button = ttk.Button(
            split_buttons, text="[-] Remove Split", command=self._remove_split_row
        )
        self.remove_split_button.pack(side="left", padx=7)

        ttk.Label(right, text="Calculated output ranges:").pack(anchor="w")
        range_holder = ttk.Frame(right)
        range_holder.pack(fill="both", expand=True, pady=5)
        self.range_list = tk.Listbox(
            range_holder,
            height=5,
            width=48,
            activestyle="none",
            exportselection=False,
        )
        range_scroll = ttk.Scrollbar(
            range_holder, orient="vertical", command=self.range_list.yview
        )
        self.range_list.configure(yscrollcommand=range_scroll.set)
        self.range_list.pack(side="left", fill="both", expand=True)
        range_scroll.pack(side="right", fill="y")
        self._add_split_row(initial=True)

    def _build_output_group(self) -> None:
        self.output_heading = ttk.Frame(self.main_content)
        self.output_heading_label = ttk.Label(
            self.output_heading, text="Output", style="SectionHeading.TLabel"
        )
        self.output_heading_label.pack(side="left")
        self.grayscale_check = ttk.Checkbutton(
            self.output_heading,
            text="Convert to Grayscale",
            variable=self.grayscale_var,
            command=self._on_grayscale_changed,
        )
        self.grayscale_check.pack(side="left", padx=(14, 0))
        group = ttk.LabelFrame(
            self.main_content,
            labelwidget=self.output_heading,
            style="Section.TLabelframe",
        )
        group.pack(fill="x", pady=5)
        group.columnconfigure(1, weight=1)
        self.output_group = group

        ttk.Label(group, text="Output folder:").grid(row=0, column=0, sticky="w")
        self.output_folder_entry = ttk.Entry(
            group, textvariable=self.output_folder_var, state="readonly"
        )
        self.output_folder_entry.grid(row=0, column=1, sticky="ew", padx=8)
        self.save_as_button = ttk.Button(group, text="Save As…", command=self._save_as)
        self.save_as_button.grid(row=0, column=2, sticky="e")

        ttk.Label(group, text="Output base name:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        self.output_base_entry = ttk.Entry(group, textvariable=self.output_base_var)
        self.output_base_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=(8, 0))
        ttk.Label(group, text=".pdf added automatically", style="Hint.TLabel").grid(
            row=1, column=2, sticky="w", pady=(8, 0)
        )
        self.output_base_entry.bind("<KeyRelease>", self._on_output_base_key)
        self.output_base_entry.bind("<FocusOut>", self._on_output_base_focus_out)

        ttk.Label(group, text="Output preview:").grid(row=2, column=0, sticky="nw", pady=(8, 0))
        preview_holder = ttk.Frame(group)
        preview_holder.grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), pady=(8, 0))
        self.output_preview = tk.Listbox(
            preview_holder,
            height=3,
            activestyle="none",
            exportselection=False,
        )
        preview_scroll = ttk.Scrollbar(
            preview_holder, orient="vertical", command=self.output_preview.yview
        )
        self.output_preview.configure(yscrollcommand=preview_scroll.set)
        self.output_preview.pack(side="left", fill="both", expand=True)
        preview_scroll.pack(side="right", fill="y")

    def _build_progress_group(self) -> None:
        self.progress_heading = ttk.Frame(self.main_content)
        self.progress_heading_label = ttk.Label(
            self.progress_heading, text="Progress", style="SectionHeading.TLabel"
        )
        self.progress_heading_label.pack(side="left")
        self.readiness_frame = ttk.Frame(self.progress_heading)
        self.readiness_frame.pack(side="left", padx=(8, 0))
        self.readiness_canvas = tk.Canvas(
            self.readiness_frame,
            width=21,
            height=21,
            highlightthickness=0,
            borderwidth=0,
            background=self.cget("background"),
        )
        self.readiness_indicator = self.readiness_canvas.create_oval(
            2, 2, 19, 19, fill="#e6395b", outline="#000000", width=2
        )
        self.readiness_canvas.pack(side="left")
        self.readiness_label = ttk.Label(
            self.readiness_frame, textvariable=self.readiness_text_var
        )
        self.readiness_label.pack(side="left", padx=(4, 0))
        group = ttk.LabelFrame(
            self.main_content,
            labelwidget=self.progress_heading,
            style="Section.TLabelframe",
        )
        group.pack(fill="both", expand=True, pady=5)
        group.columnconfigure(0, weight=1)
        self.progress_group = group

        self.stage_label = ttk.Label(group, textvariable=self.stage_var)
        self.stage_label.grid(row=0, column=0, sticky="w")
        self.progress_bar = ttk.Progressbar(
            group, variable=self.progress_var, maximum=100, mode="determinate"
        )
        self.progress_bar.grid(row=1, column=0, sticky="ew", pady=(4, 6))

        self.file_progress_label = ttk.Label(
            group, textvariable=self.file_progress_text_var, style="Hint.TLabel"
        )
        self.file_progress_label.grid(row=2, column=0, sticky="w")
        self.file_progress_bar = ttk.Progressbar(
            group, variable=self.file_progress_var, maximum=100, mode="determinate"
        )
        self.file_progress_bar.grid(row=3, column=0, sticky="ew", pady=(3, 8))

        ttk.Label(group, text="Status:").grid(row=4, column=0, sticky="w")
        self.status_area = tk.Text(
            group,
            height=4,
            wrap="word",
            state="disabled",
            background="#f7f8fa",
            relief="solid",
            borderwidth=1,
        )
        self.status_area.grid(row=5, column=0, sticky="ew", pady=(3, 7))
        ttk.Label(group, text="Errors:", style="Danger.TLabel").grid(
            row=6, column=0, sticky="w"
        )
        self.error_area = tk.Text(
            group,
            height=3,
            wrap="word",
            state="disabled",
            foreground="#8b1a1a",
            background="#fff5f5",
            relief="solid",
            borderwidth=1,
        )
        self.error_area.grid(row=7, column=0, sticky="ew", pady=(3, 8))

        actions = ttk.Frame(group)
        actions.grid(row=8, column=0, sticky="ew")
        self.process_button = tk.Button(
            actions,
            text="Process",
            command=self._start_process,
            state="disabled",
            background=PROCESS_DISABLED_BG,
            foreground=PROCESS_DISABLED_FG,
            disabledforeground=PROCESS_DISABLED_FG,
            activebackground=PROCESS_ENABLED_BG,
            activeforeground=PROCESS_ENABLED_FG,
            font=("Segoe UI", 9, "bold"),
            relief="raised",
            borderwidth=1,
            padx=12,
            pady=3,
        )
        self.process_button.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="Cancel", command=self._cancel_process)
        self.cancel_button.pack(side="left", padx=7)
        self.open_destination_button = ttk.Button(
            actions,
            text="Open destination folder",
            command=self._open_destination_folder,
        )
        self.open_destination_button.pack(side="right")
        self.open_completed_button = ttk.Button(
            actions, text="Open completed file", command=self._open_completed_file
        )
        self.open_completed_button.pack(side="right", padx=7)

    # ---------------------------------------------------------- view utilities
    def _set_icon(self) -> None:
        icon = _resource_path("PROS.ico") or _resource_path("assets/PROS.ico")
        if icon:
            try:
                self.iconbitmap(default=str(icon))
            except tk.TclError:
                pass

    def _on_content_configure(self, _event: tk.Event[Any]) -> None:
        self.main_canvas.configure(scrollregion=self.main_canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event[Any]) -> None:
        self.main_canvas.itemconfigure(self._canvas_window, width=event.width)
        if hasattr(self, "tagline_label"):
            self.tagline_label.configure(wraplength=max(320, event.width - 135))

    def _on_mouse_wheel(self, event: tk.Event[Any]) -> None:
        if self.winfo_containing(event.x_root, event.y_root) is not None:
            self.main_canvas.yview_scroll(int(-event.delta / 120), "units")

    @staticmethod
    def _set_widget_enabled(widget: tk.Widget, enabled: bool) -> None:
        try:
            if enabled:
                widget.state(["!disabled"])  # type: ignore[attr-defined]
            else:
                widget.state(["disabled"])  # type: ignore[attr-defined]
        except (AttributeError, tk.TclError):
            try:
                widget.configure(state="normal" if enabled else "disabled")
            except tk.TclError:
                pass

    def _set_process_enabled(self, enabled: bool) -> None:
        """Set both the Process lock and an OS-theme-independent visual state."""

        self.process_button.configure(
            state="normal" if enabled else "disabled",
            background=PROCESS_ENABLED_BG if enabled else PROCESS_DISABLED_BG,
            foreground=PROCESS_ENABLED_FG if enabled else PROCESS_DISABLED_FG,
            disabledforeground=PROCESS_DISABLED_FG,
        )

    def _replace_text(self, widget: tk.Text, value: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        if value:
            widget.insert("1.0", value)
        widget.configure(state="disabled")
        if widget is getattr(self, "status_area", None):
            self._last_status_line = value.splitlines()[-1] if value else ""

    def _append_status(self, message: str) -> None:
        for raw_line in str(message).splitlines() or (str(message),):
            safe = self._friendly_message(raw_line).strip()
            if not safe or safe == self._last_status_line:
                continue
            self.status_area.configure(state="normal")
            if self.status_area.index("end-1c") != "1.0":
                self.status_area.insert("end", "\n")
            self.status_area.insert("end", safe)
            self.status_area.see("end")
            self.status_area.configure(state="disabled")
            self._last_status_line = safe

    def _safe_message(self, message: object) -> str:
        text = str(message)
        secrets = [self.common_password_var.get()]
        secrets.extend(row.password_override for row in self._inputs)
        for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
            text = text.replace(secret, "••••")
        return text

    def _friendly_message(self, message: object) -> str:
        """Translate internal engine vocabulary into actionable user language."""

        text = self._safe_message(message).strip()
        if not text:
            return ""
        exact = {
            "Validating inputs and output paths": "Checking input files, passwords, and the output folder.",
            "Preflight completed": "All input and output checks passed.",
            "Source integrity recorded": "The original files were checked and will remain unchanged.",
            "Processing completed successfully": "Processing completed successfully.",
            "Running authoritative preflight…": "Checking the complete job before processing…",
            "Preflight passed. Processing started.": "All checks passed. Processing started.",
            "Timeout cleanup is complete. No final output was retained.": NO_PROGRESS_CLEANUP_STATUS,
        }
        if text in exact:
            return exact[text]
        if "larger than 120 MB" in text:
            return LARGE_FILE_STATUS
        if text.startswith("Writing ") and "baseline" in text.casefold():
            return "Preparing the PDF output."
        if text.startswith("Staged ") and text.endswith(" for commit"):
            filename = text.removeprefix("Staged ").removesuffix(" for commit")
            return f"Finalizing {filename}."
        if text.startswith("Verified "):
            return f"Checked {text.removeprefix('Verified ')} and confirmed that it opens correctly."
        if text.startswith("Added "):
            return f"Added {text.removeprefix('Added ')} to the joined PDF."
        if text.startswith("An output file already exists:"):
            filename = text.split(":", 1)[1].strip().rstrip(".")
            return f"A file named {filename} already exists in the output folder. Choose another name or folder."
        if "password is missing or incorrect" in text.casefold():
            filename = text.split(":", 1)[0] if ":" in text else "This PDF"
            return f"{filename}: enter the password that currently opens this protected PDF."
        if "valid pdf signature" in text.casefold():
            return "This file does not appear to be a valid PDF. Choose another PDF file."
        if "corrupt or uses an unsupported structure" in text.casefold():
            return "This PDF is damaged or uses a feature that PROS cannot read. Try opening and resaving it in a PDF viewer."
        if "file is unavailable" in text.casefold():
            return "The selected PDF is no longer available. Restore it or add the file again."
        if "selected output folder does not exist" in text.casefold():
            return "The output folder no longer exists. Choose another folder."
        if "selected output folder is not writable" in text.casefold():
            return "PROS cannot save files in this output folder. Choose a folder where you have permission to save."
        # Never expose Python exception class names as the main explanation.
        text = re.sub(
            r"^(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)):\s*",
            "",
            text,
        )
        return text

    def _set_runtime_error(self, message: str = "") -> None:
        self._runtime_error = self._friendly_message(message)
        self._refresh_state()

    def _mark_draft_edited(self) -> None:
        """Leave the completed-result view only after a real user edit."""

        if self._phase == "succeeded":
            self._phase = "editing"
            self.stage_var.set("Ready" if not any(row.inspection_pending for row in self._inputs) else "Checking files")
            for row in self._inputs:
                if row.info is not None and row.info.encrypted:
                    row.info = PdfInfo(
                        path=row.info.path,
                        size_bytes=row.info.size_bytes,
                        page_count=None,
                        encrypted=True,
                        password_valid=False,
                        warnings=row.info.warnings,
                        risks=row.info.risks,
                    )
            self._request_inspection()

    # ------------------------------------------------------------ input files
    def _mode(self) -> StructureMode:
        try:
            return StructureMode(self.mode_var.get())
        except ValueError:
            return StructureMode.NEITHER

    def _input_limit(self) -> int:
        return MAX_JOIN_INPUTS if self._mode() is StructureMode.JOIN else 1

    def _add_pdfs(self) -> None:
        if self._phase not in {"editing", "failed", "succeeded"}:
            return
        limit = self._input_limit()
        remaining = limit - len(self._inputs)
        if remaining <= 0:
            return
        options = {
            "title": "Select PDF files" if self._mode() is StructureMode.JOIN else "Select a PDF file",
            "initialdir": str(self.input_dir),
            "filetypes": (("PDF files", "*.pdf"),),
        }
        if self._mode() is StructureMode.JOIN:
            selected: Sequence[str] = filedialog.askopenfilenames(parent=self, **options)
        else:
            one = filedialog.askopenfilename(parent=self, **options)
            selected = (one,) if one else ()
        if not selected:
            return
        self._mark_draft_edited()

        existing = {_canonical_path(row.path) for row in self._inputs}
        accepted: list[Path] = []
        rejected: list[str] = []
        for raw in selected:
            path = Path(raw)
            if path.suffix.casefold() != ".pdf":
                rejected.append(f"{path.name}: only .pdf files are accepted.")
                continue
            key = _canonical_path(path)
            if key in existing or any(_canonical_path(item) == key for item in accepted):
                rejected.append(f"{path.name}: this file is already in the list.")
                continue
            if len(accepted) >= remaining:
                rejected.append(f"{path.name}: the {limit}-file limit has been reached.")
                continue
            accepted.append(path)

        for path in accepted:
            self._inputs.append(_InputRow(path=path))
        if accepted:
            self.input_dir = accepted[-1].parent
            self.input_folder_var.set(str(self.input_dir))
            self._refresh_auto_base()
            self._runtime_error = ""
            self._request_inspection(immediate=True)
        if rejected:
            self._append_status("\n".join(rejected))
        self._refresh_inputs()
        self._refresh_output_preview()
        self._refresh_state()

    def _selected_input_index(self) -> int | None:
        selected = self.input_tree.selection()
        if not selected:
            return None
        uid = selected[0]
        return next((i for i, row in enumerate(self._inputs) if row.uid == uid), None)

    def _remove_selected_pdf(self) -> None:
        index = self._selected_input_index()
        if index is None:
            return
        self._mark_draft_edited()
        removed = self._inputs.pop(index)
        removed.password_override = ""
        self._selected_input_uid = None
        self.per_file_password_var.set("")
        self._inspection_generation += 1
        self._refresh_auto_base()
        self._refresh_inputs()
        self._refresh_ranges()
        self._refresh_output_preview()
        self._refresh_state()

    def _clear_inputs(self) -> None:
        if not self._inputs:
            return
        if not messagebox.askyesno(
            "Clear input list",
            "Remove all selected PDFs from this job?\n\nThe source files will not be deleted.",
            parent=self,
        ):
            return
        self._mark_draft_edited()
        self._clear_passwords()
        self._inputs.clear()
        self._selected_input_uid = None
        self._inspection_generation += 1
        self._base_user_edited = False
        self._set_output_base("")
        self._refresh_inputs()
        self._refresh_ranges()
        self._refresh_output_preview()
        self._refresh_state()

    def _move_input(self, delta: int) -> None:
        if self._mode() is not StructureMode.JOIN:
            return
        index = self._selected_input_index()
        if index is None:
            return
        target = index + delta
        if not 0 <= target < len(self._inputs):
            return
        self._mark_draft_edited()
        row = self._inputs.pop(index)
        self._inputs.insert(target, row)
        self._refresh_auto_base()
        self._refresh_inputs(select_uid=row.uid)
        self._refresh_output_preview()
        self._refresh_state()

    def _refresh_inputs(self, select_uid: str | None = None) -> None:
        selected = select_uid or self._selected_input_uid
        for item in self.input_tree.get_children():
            self.input_tree.delete(item)
        for index, row in enumerate(self._inputs, start=1):
            info = row.info
            if row.inspection_pending:
                protection, pages, status = "Checking…", "—", "Inspecting…"
            elif info is None:
                protection, pages, status = "Unknown", "—", "Waiting to be checked"
            else:
                protection = (
                    "Protected" if info.encrypted is True else "Not protected" if info.encrypted is False else "Unknown"
                )
                pages = str(info.page_count) if info.page_count is not None else "—"
                if info.error:
                    status = self._friendly_message(info.error)
                elif info.encrypted and info.password_valid is not True:
                    status = "Password required or incorrect"
                else:
                    status = "Ready"
            try:
                size = info.size_bytes if info is not None else row.path.stat().st_size
            except OSError:
                size = 0
            self.input_tree.insert(
                "",
                "end",
                iid=row.uid,
                values=(index, row.path.name, protection, _format_size(size), pages, status),
            )
        if selected and self.input_tree.exists(selected):
            self.input_tree.selection_set(selected)
            self.input_tree.focus(selected)
            self._selected_input_uid = selected
        elif self._inputs:
            first = self._inputs[0].uid
            self.input_tree.selection_set(first)
            self._selected_input_uid = first
        else:
            self._selected_input_uid = None
        self._load_selected_password()

    # --------------------------------------------------------------- passwords
    def _effective_password(self, row: _InputRow) -> str | None:
        if not self.remove_password_var.get():
            return None
        if row.password_override:
            return row.password_override
        if self.use_common_password_var.get() and self.common_password_var.get():
            return self.common_password_var.get()
        return None

    def _on_input_selected(self, _event: tk.Event[Any] | None = None) -> None:
        selected = self.input_tree.selection()
        self._selected_input_uid = selected[0] if selected else None
        self._load_selected_password()
        self._refresh_state()

    def _load_selected_password(self) -> None:
        index = self._selected_input_index()
        self._loading_password = True
        try:
            if index is None:
                self.selected_password_file_label.configure(
                    text="Per-file password: select a protected PDF"
                )
                self.per_file_password_var.set("")
            else:
                row = self._inputs[index]
                self.selected_password_file_label.configure(
                    text=f"Password for {row.path.name} (optional common-password override):"
                )
                self.per_file_password_var.set(row.password_override)
        finally:
            self._loading_password = False

    def _on_password_function_changed(self) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        self._request_inspection()
        self._refresh_output_preview()
        self._refresh_state()

    def _on_common_password_mode_changed(self) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        self._request_inspection()
        self._refresh_state()

    def _on_password_key(self, _event: tk.Event[Any] | None = None) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        self._request_inspection()
        self._refresh_state()

    def _on_per_file_password_key(self, _event: tk.Event[Any] | None = None) -> None:
        if self._loading_password:
            return
        index = self._selected_input_index()
        if index is not None:
            self._mark_draft_edited()
            self._inputs[index].password_override = self.per_file_password_var.get()
            self._runtime_error = ""
            self._request_inspection()
            self._refresh_state()

    def _toggle_password_visibility(self) -> None:
        show = "" if self.show_password_var.get() else "*"
        self.common_password_entry.configure(show=show)
        self.per_file_password_entry.configure(show=show)

    def _clear_passwords(self, *, invalidate_encrypted: bool = True) -> None:
        self.common_password_var.set("")
        self.per_file_password_var.set("")
        for row in self._inputs:
            row.password_override = ""
            if invalidate_encrypted and row.info is not None and row.info.encrypted:
                row.info = PdfInfo(
                    path=row.info.path,
                    size_bytes=row.info.size_bytes,
                    page_count=None,
                    encrypted=True,
                    password_valid=False,
                    error="A password is required.",
                    warnings=row.info.warnings,
                    risks=row.info.risks,
                )

    # ------------------------------------------------------------ live inspect
    def _request_inspection(self, immediate: bool = False) -> None:
        if self._inspection_after is not None:
            try:
                self.after_cancel(self._inspection_after)
            except tk.TclError:
                pass
        delay = 0 if immediate else PASSWORD_DEBOUNCE_MS
        self._inspection_after = self.after(delay, self._start_inspection)

    def _start_inspection(self) -> None:
        self._inspection_after = None
        self._inspection_generation += 1
        generation = self._inspection_generation
        for row in self._inputs:
            row.inspection_pending = True
            password = self._effective_password(row)
            thread = threading.Thread(
                target=self._inspect_one,
                args=(generation, row.uid, row.path, password),
                daemon=True,
                name=f"pros-preflight-{row.uid[:6]}",
            )
            thread.start()
        self.stage_var.set("Checking selected PDF files" if self._inputs else "Ready")
        self._refresh_inputs()
        self._refresh_state()

    def _inspect_one(
        self, generation: int, uid: str, path: Path, password: str | None
    ) -> None:
        with self._inspection_semaphore:
            try:
                info = inspect_pdf(path, password)
            except Exception as exc:  # noqa: BLE001 - thread boundary must stay alive
                del exc
                info = PdfInfo(
                    path=path,
                    error="PROS could not check this PDF. Close any program that is using it and try again.",
                )
            self._inspection_queue.put((generation, uid, info))

    def _drain_inspection_queue(self) -> None:
        changed = False
        while True:
            try:
                generation, uid, info = self._inspection_queue.get_nowait()
            except queue.Empty:
                break
            if generation != self._inspection_generation:
                continue
            row = next((item for item in self._inputs if item.uid == uid), None)
            if row is None:
                continue
            row.info = info
            row.inspection_pending = False
            changed = True
            if info.size_bytes > LARGE_FILE_NOTICE_BYTES:
                self._append_status(LARGE_FILE_STATUS)
        if changed:
            if (
                self._inputs
                and not any(row.inspection_pending for row in self._inputs)
                and self._phase != "succeeded"
            ):
                self.stage_var.set("File checks complete")
            self._refresh_inputs(select_uid=self._selected_input_uid)
            self._refresh_ranges()
            self._refresh_output_preview()
            self._refresh_state()

    # ---------------------------------------------------------- mode and split
    def _on_mode_changed(self) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        if self._mode() is StructureMode.SPLIT and not self._split_rows:
            self._add_split_row(initial=True)
        self._refresh_mode_panel()
        self._refresh_auto_base()
        self._refresh_ranges()
        self._refresh_output_preview()
        self._refresh_state()

    def _refresh_mode_panel(self) -> None:
        self.join_options.pack_forget()
        self.split_options.pack_forget()
        mode = self._mode()
        if mode is StructureMode.JOIN:
            pack_options: dict[str, object] = {"fill": "x", "pady": 5}
            if hasattr(self, "output_group"):
                pack_options["before"] = self.output_group
            self.options_group.pack(**pack_options)
            self.join_options.pack(fill="x")
        elif mode is StructureMode.SPLIT:
            pack_options = {"fill": "x", "pady": 5}
            if hasattr(self, "output_group"):
                pack_options["before"] = self.output_group
            self.options_group.pack(**pack_options)
            self.split_options.pack(fill="both", expand=True)
        else:
            self.options_group.pack_forget()

    def _add_split_row(self, initial: bool = False) -> None:
        if len(self._split_rows) >= MAX_SPLIT_OUTPUTS - 1:
            return
        index = len(self._split_rows)
        frame = ttk.Frame(self.split_entries_frame)
        frame.pack(fill="x", pady=2)
        frame.columnconfigure(2, weight=1)
        selector = ttk.Radiobutton(
            frame,
            variable=self.split_selected_var,
            value=index,
            command=lambda i=index: self._select_split_row(i),
        )
        selector.grid(row=0, column=0, padx=(0, 4))
        label = ttk.Label(frame, text=f"Split point {index + 1}")
        label.grid(row=0, column=1, sticky="w", padx=(0, 8))
        variable = tk.StringVar(value="")
        # A classic Tk entry is intentional here: native ttk themes on Windows
        # can ignore fieldbackground changes, which made invalid-value flashes
        # invisible. This entry always renders the requested red/white sequence.
        entry = tk.Entry(
            frame,
            textvariable=variable,
            width=18,
            background=SPLIT_NORMAL_BG,
            disabledbackground="#f3f4f6",
            relief="solid",
            borderwidth=1,
        )
        entry.grid(row=0, column=2, sticky="ew")
        row = _SplitRow(frame=frame, label=label, variable=variable, entry=entry)
        self._split_rows.append(row)
        entry.bind("<FocusIn>", lambda _event, i=index: self._select_split_row(i))
        entry.bind("<KeyRelease>", lambda _event, i=index: self._on_split_key(i))
        entry.bind("<FocusOut>", lambda _event, i=index: self._commit_split_row(i))
        entry.bind("<Return>", lambda _event, i=index: self._commit_split_row(i))
        if not initial:
            self._mark_draft_edited()
            self._select_split_row(index)
            entry.focus_set()
        self._reindex_split_rows()
        # The initial row is created while the rest of the window is still
        # being constructed, before the Output and Progress widgets exist.
        if hasattr(self, "range_list"):
            self._refresh_ranges()
        if hasattr(self, "output_preview"):
            self._refresh_output_preview()
        if hasattr(self, "process_button"):
            self._refresh_state()

    def _select_split_row(self, index: int) -> None:
        if 0 <= index < len(self._split_rows):
            self._selected_split_index = index
            self.split_selected_var.set(index)

    def _remove_split_row(self) -> None:
        if not self._split_rows:
            return
        self._mark_draft_edited()
        index = min(self._selected_split_index, len(self._split_rows) - 1)
        row = self._split_rows[index]
        if row.clear_after:
            try:
                self.after_cancel(row.clear_after)
            except tk.TclError:
                pass
            row.clear_after = None
        if row.validate_after:
            try:
                self.after_cancel(row.validate_after)
            except tk.TclError:
                pass
            row.validate_after = None
        if len(self._split_rows) == 1:
            row.variable.set("")
            row.entry.configure(background=SPLIT_NORMAL_BG)
            row.entry.focus_set()
        else:
            row.frame.destroy()
            self._split_rows.pop(index)
            self._selected_split_index = max(0, index - 1)
        self._reindex_split_rows()
        self._refresh_ranges()
        self._refresh_output_preview()
        self._refresh_state()

    def _reindex_split_rows(self) -> None:
        for index, row in enumerate(self._split_rows):
            row.label.configure(text=f"Split point {index + 1}")
            for child in row.frame.winfo_children():
                if isinstance(child, ttk.Radiobutton):
                    child.configure(value=index, command=lambda i=index: self._select_split_row(i))
            row.entry.bind("<FocusIn>", lambda _event, i=index: self._select_split_row(i))
            row.entry.bind("<KeyRelease>", lambda _event, i=index: self._on_split_key(i))
            row.entry.bind("<FocusOut>", lambda _event, i=index: self._commit_split_row(i))
            row.entry.bind("<Return>", lambda _event, i=index: self._commit_split_row(i))
        self._select_split_row(min(self._selected_split_index, max(0, len(self._split_rows) - 1)))

    def _on_split_key(self, index: int) -> None:
        self._mark_draft_edited()
        self._select_split_row(index)
        self._runtime_error = ""
        row = self._split_rows[index]
        if row.validate_after:
            try:
                self.after_cancel(row.validate_after)
            except tk.TclError:
                pass
            row.validate_after = None
        if row.clear_after:
            try:
                self.after_cancel(row.clear_after)
            except tk.TclError:
                pass
            row.clear_after = None
        row.entry.configure(background=SPLIT_NORMAL_BG)
        if row.variable.get().strip():
            row.validate_after = self.after(1200, lambda i=index: self._debounced_split_validation(i))
        self._refresh_ranges()
        self._refresh_output_preview()
        self._refresh_state()

    def _parsed_split_points(self) -> tuple[list[int], list[str]]:
        values: list[int] = []
        errors: list[str] = []
        for index, row in enumerate(self._split_rows, start=1):
            text = row.variable.get().strip()
            if not text:
                errors.append(f"Split point {index} is required.")
                continue
            if not re.fullmatch(r"[1-9][0-9]*", text):
                errors.append(f"Split point {index} must be a positive whole number.")
                continue
            values.append(int(text))
        if len(values) == len(self._split_rows):
            page_count = self._split_page_count()
            if page_count is not None:
                errors.extend(validate_split_points(page_count, values))
            else:
                for previous, current in pairwise(values):
                    if current <= previous:
                        errors.append("Split points must be in strictly increasing order.")
                        break
        return values, list(dict.fromkeys(errors))

    def _split_page_count(self) -> int | None:
        if len(self._inputs) != 1 or self._inputs[0].info is None:
            return None
        return self._inputs[0].info.page_count

    def _debounced_split_validation(self, index: int) -> None:
        if not 0 <= index < len(self._split_rows):
            return
        self._split_rows[index].validate_after = None
        # The idle debounce itself is the requested 1–1.5 second pause. Once
        # it expires, begin the visual rejection immediately if the value is
        # invalid instead of adding a second delay.
        self._commit_split_row(index, invalid_delay_ms=0)

    def _commit_split_row(self, index: int, *, invalid_delay_ms: int = 1100) -> None:
        if not 0 <= index < len(self._split_rows):
            return
        row = self._split_rows[index]
        if row.validate_after:
            try:
                self.after_cancel(row.validate_after)
            except tk.TclError:
                pass
            row.validate_after = None
        text = row.variable.get().strip()
        if not text:
            return
        if not re.fullmatch(r"[1-9][0-9]*", text):
            self._mark_split_invalid(
                index,
                f"Split point {index + 1} must be a positive whole number.",
                delay_ms=invalid_delay_ms,
            )
            return
        value = int(text)
        page_count = self._split_page_count()
        previous = None
        following = None
        if index > 0 and self._split_rows[index - 1].variable.get().strip().isdigit():
            previous = int(self._split_rows[index - 1].variable.get().strip())
        if index + 1 < len(self._split_rows) and self._split_rows[index + 1].variable.get().strip().isdigit():
            following = int(self._split_rows[index + 1].variable.get().strip())
        if previous is not None and value <= previous:
            reason = "duplicates the previous split point" if value == previous else "must be greater than the previous split point"
            self._mark_split_invalid(
                index, f"Split point {index + 1} {reason}.", delay_ms=invalid_delay_ms
            )
        elif following is not None and value >= following:
            reason = "duplicates the next split point" if value == following else "must be less than the next split point"
            self._mark_split_invalid(
                index, f"Split point {index + 1} {reason}.", delay_ms=invalid_delay_ms
            )
        elif page_count is not None and value >= page_count:
            self._mark_split_invalid(
                index,
                f"Split point {index + 1} must be less than the source page count ({page_count}).",
                delay_ms=invalid_delay_ms,
            )

    def _mark_split_invalid(self, index: int, message: str, *, delay_ms: int = 1100) -> None:
        if not 0 <= index < len(self._split_rows):
            return
        row = self._split_rows[index]
        if row.validate_after:
            try:
                self.after_cancel(row.validate_after)
            except tk.TclError:
                pass
            row.validate_after = None
        # Leave the value readable for a moment, then flash red/white twice so
        # the user can see which field is being rejected before it is cleared.
        row.entry.configure(background=SPLIT_NORMAL_BG)
        self._runtime_error = message
        self._refresh_state()
        if row.clear_after:
            try:
                self.after_cancel(row.clear_after)
            except tk.TclError:
                pass

        def flash(step: int = 0) -> None:
            if not row.entry.winfo_exists():
                return
            colors = (
                SPLIT_INVALID_BG,
                SPLIT_NORMAL_BG,
                SPLIT_INVALID_BG,
                SPLIT_NORMAL_BG,
            )
            if step < len(colors):
                row.entry.configure(background=colors[step])
                row.clear_after = self.after(180, lambda: flash(step + 1))
                return
            row.variable.set("")
            row.entry.configure(background=SPLIT_NORMAL_BG)
            row.entry.focus_set()
            row.clear_after = None
            self._refresh_ranges()
            self._refresh_output_preview()
            self._refresh_state()

        row.clear_after = self.after(delay_ms, flash)

    def _refresh_ranges(self) -> None:
        self.range_list.delete(0, "end")
        if self._mode() is not StructureMode.SPLIT:
            return
        page_count = self._split_page_count()
        if page_count is None:
            self.range_list.insert("end", "Ranges will appear after the PDF and password checks.")
            return

        # A newly added trailing field is intentionally blank.  Keep showing
        # the ranges calculated from the completed points until the user types
        # the next value; the blank still blocks Process through full validation.
        completed: list[int] = []
        malformed = False
        for row in self._split_rows:
            text = row.variable.get().strip()
            if not text:
                continue
            if not re.fullmatch(r"[1-9][0-9]*", text):
                malformed = True
                break
            completed.append(int(text))
        if malformed or not completed:
            self.range_list.insert("end", "Enter valid, increasing split points.")
            return
        errors = validate_split_points(page_count, completed)
        if errors:
            self.range_list.insert("end", "Enter valid, increasing split points.")
            return
        try:
            ranges = calculate_split_ranges(page_count, completed)
        except ValueError:
            self.range_list.insert("end", "Enter valid, increasing split points.")
            return
        for part, (start, end) in enumerate(ranges, start=1):
            count = end - start + 1
            self.range_list.insert(
                "end", f"Part {part} — pages {start}–{end} ({count} page{'s' if count != 1 else ''})"
            )

    # --------------------------------------------------------------- output UI
    def _set_output_base(self, value: str) -> None:
        self._setting_base = True
        try:
            self.output_base_var.set(value)
        finally:
            self._setting_base = False

    def _refresh_auto_base(self) -> None:
        if self._base_user_edited:
            return
        value = normalize_output_base(self._inputs[0].path.stem) if self._inputs else ""
        self._set_output_base(value)

    def _on_output_base_key(self, _event: tk.Event[Any] | None = None) -> None:
        self._mark_draft_edited()
        if not self._setting_base:
            self._base_user_edited = bool(self.output_base_var.get().strip())
        self._runtime_error = ""
        self._refresh_output_preview()
        self._refresh_state()

    def _on_output_base_focus_out(self, _event: tk.Event[Any] | None = None) -> None:
        normalized = normalize_output_base(self.output_base_var.get())
        if not normalized and self._inputs:
            self._base_user_edited = False
            self._refresh_auto_base()
        else:
            self._set_output_base(normalized)
        self._refresh_output_preview()
        self._refresh_state()

    def _save_as(self) -> None:
        request = self._preview_request()
        suggested = suggest_output_base(request) if request else normalize_output_base(self.output_base_var.get())
        selected = filedialog.asksaveasfilename(
            parent=self,
            title="Choose output folder and base name",
            initialdir=self.output_folder_var.get() or str(self.app_dir),
            initialfile=f"{suggested or 'Output'}.pdf",
            defaultextension=".pdf",
            filetypes=(("PDF files", "*.pdf"),),
        )
        if not selected:
            return
        self._mark_draft_edited()
        path = Path(selected)
        self.output_folder_var.set(str(path.parent))
        stem = normalize_output_base(path.name)
        # The dialog preview may already contain the active automatic suffixes.
        # Strip only the exact generated tail so suffixes are not duplicated.
        if request:
            automatic = suggest_output_base(request)
            raw = normalize_output_base(request.output_base) or (request.input_paths[0].stem if request.input_paths else "")
            tail = automatic[len(raw) :] if automatic.startswith(raw) else ""
            if tail and stem.endswith(tail):
                stem = stem[: -len(tail)]
        self._base_user_edited = True
        self._set_output_base(stem)
        self._runtime_error = ""
        self._refresh_output_preview()
        self._refresh_state()

    def _preview_request(self) -> JobRequest | None:
        if not self._inputs and not self.output_base_var.get().strip():
            return None
        points, _errors = self._parsed_split_points()
        job_id = self._active_job_id or "preview"
        return self._create_request(job_id=job_id, points=points)

    def _create_request(self, *, job_id: str, points: Sequence[int]) -> JobRequest:
        kwargs: dict[str, object] = {
            "job_id": job_id,
            "remove_password": self.remove_password_var.get(),
            "compress_pdf": self.compress_var.get(),
            "structure_mode": self._mode(),
            "input_paths": [row.path for row in self._inputs],
            "passwords": [self._effective_password(row) for row in self._inputs],
            "split_points": points if self._mode() is StructureMode.SPLIT else [],
            "output_dir": Path(self.output_folder_var.get() or self.app_dir),
            "output_base": normalize_output_base(self.output_base_var.get()),
            "staging_dir": Path(tempfile.gettempdir()) / f"PROS-{job_id}",
        }
        request_fields = getattr(JobRequest, "__dataclass_fields__", {})
        if "compression_level" in request_fields:
            # The second-revision UI exposes one compression profile only.
            kwargs["compression_level"] = CompressionLevel.ULTRA
        if "convert_to_grayscale" in request_fields:
            kwargs["convert_to_grayscale"] = bool(self.grayscale_var.get())
        return JobRequest(**kwargs)  # type: ignore[arg-type]

    def _refresh_output_preview(self) -> None:
        self.output_preview.delete(0, "end")
        request = self._preview_request()
        if request is None:
            self.output_preview.insert("end", "Select an input PDF to calculate output names.")
            return
        try:
            paths = build_output_paths(request)
        except (OSError, TypeError, ValueError):
            paths = ()
        for path in paths:
            self.output_preview.insert("end", path.name)

    # ------------------------------------------------------------- validation
    def _blocking_errors(self) -> list[str]:
        errors: list[str] = []
        mode = self._mode()
        if not (
            self.remove_password_var.get()
            or self.compress_var.get()
            or self.grayscale_var.get()
            or mode is not StructureMode.NEITHER
        ):
            errors.append("Select at least one PDF-processing function.")

        count = len(self._inputs)
        if mode is StructureMode.JOIN:
            if not 2 <= count <= MAX_JOIN_INPUTS:
                errors.append(f"Join requires between 2 and {MAX_JOIN_INPUTS} input PDFs.")
        elif mode is StructureMode.SPLIT:
            if count != 1:
                errors.append("Split requires exactly one input PDF.")
        elif count != 1:
            errors.append(
                "Password removal, compression, or grayscale conversion without Join or Split requires one input PDF."
            )

        for row in self._inputs:
            if row.inspection_pending:
                errors.append(f"{row.path.name}: PROS is still checking this file.")
                continue
            info = row.info
            if info is None:
                errors.append(f"{row.path.name}: this file has not been checked yet.")
                continue
            if info.error:
                errors.append(f"{row.path.name}: {self._safe_message(info.error)}")
                continue
            if info.encrypted:
                if not self.remove_password_var.get():
                    errors.append(f"{row.path.name}: select Remove password for this protected PDF.")
                elif info.password_valid is not True:
                    errors.append(f"{row.path.name}: the password is missing or incorrect.")

        if mode is StructureMode.SPLIT:
            _points, point_errors = self._parsed_split_points()
            errors.extend(point_errors)

        request = self._preview_request()
        if request is not None:
            raw_base = request.output_base or (request.input_paths[0].stem if request.input_paths else "")
            errors.extend(validate_output_base(raw_base))
            output_dir = request.output_dir
            if not output_dir.exists():
                errors.append("The selected output folder does not exist.")
            elif not output_dir.is_dir():
                errors.append("The selected output path is not a folder.")
            elif not os.access(output_dir, os.W_OK):
                errors.append("The selected output folder is not writable.")
            input_keys = {_canonical_path(row.path) for row in self._inputs}
            try:
                proposed = build_output_paths(request)
            except (OSError, TypeError, ValueError) as exc:
                errors.append(f"Output names could not be calculated ({type(exc).__name__}).")
                proposed = ()
            for path in proposed:
                if _canonical_path(path) in input_keys:
                    errors.append(f"Output path may not equal an input path: {path.name}.")
                if path.exists():
                    errors.append(f"An output file already exists: {path.name}.")
        else:
            errors.append("An output base name is required.")
        return list(dict.fromkeys(self._friendly_message(item) for item in errors if item))

    def _refresh_state(self) -> None:
        active = self._phase in {"preflighting", "processing", "cancelling"}
        completed = self._phase == "succeeded"
        idle_for_job = self._phase in {"editing", "failed"}
        mode = self._mode()
        errors = [] if active or completed else self._blocking_errors()
        shown_errors = [] if completed else ([self._runtime_error] if self._runtime_error else []) + errors
        self._replace_text(self.error_area, "\n".join(dict.fromkeys(shown_errors)))

        for widget in (
            self.remove_password_check,
            self.compress_check,
            self.join_radio,
            self.split_radio,
            self.neither_radio,
            self.input_tree,
            self.remove_pdf_button,
            self.clear_list_button,
            self.save_as_button,
            self.output_base_entry,
            self.grayscale_check,
        ):
            self._set_widget_enabled(widget, not active)

        add_allowed = not active and len(self._inputs) < self._input_limit()
        self._set_widget_enabled(self.add_pdf_button, add_allowed)
        selected_index = self._selected_input_index()
        self._set_widget_enabled(self.remove_pdf_button, not active and selected_index is not None)
        self._set_widget_enabled(self.clear_list_button, not active and bool(self._inputs))

        join_active = not active and mode is StructureMode.JOIN and selected_index is not None
        self._set_widget_enabled(
            self.move_up_button, bool(join_active and selected_index is not None and selected_index > 0)
        )
        self._set_widget_enabled(
            self.move_down_button,
            bool(join_active and selected_index is not None and selected_index < len(self._inputs) - 1),
        )

        password_enabled = not active and self.remove_password_var.get()
        self.password_group.configure(
            style="Section.TLabelframe" if password_enabled else "Disabled.TLabelframe"
        )
        self._set_widget_enabled(self.use_common_password_check, password_enabled)
        self._set_widget_enabled(self.show_password_check, password_enabled)
        self._set_widget_enabled(
            self.common_password_entry,
            password_enabled and self.use_common_password_var.get(),
        )
        selected_row = self._inputs[selected_index] if selected_index is not None else None
        per_file_allowed = bool(
            password_enabled
            and selected_row is not None
            and (selected_row.info is None or selected_row.info.encrypted is not False)
        )
        self._set_widget_enabled(self.per_file_password_entry, per_file_allowed)

        for row in self._split_rows:
            self._set_widget_enabled(row.entry, not active and mode is StructureMode.SPLIT)
        self._set_widget_enabled(
            self.add_split_button,
            not active and mode is StructureMode.SPLIT and len(self._split_rows) < MAX_SPLIT_OUTPUTS - 1,
        )
        self._set_widget_enabled(
            self.remove_split_button,
            not active and mode is StructureMode.SPLIT and bool(self._split_rows),
        )

        positively_valid = idle_for_job and not errors
        self._set_process_enabled(positively_valid)
        self._set_widget_enabled(
            self.cancel_button, self._phase in {"preflighting", "processing"}
        )
        indicator_color = "#22a447" if positively_valid else "#e6395b"
        self.readiness_canvas.itemconfigure(self.readiness_indicator, fill=indicator_color)
        if positively_valid:
            readiness_text = "Ready to process"
        elif completed:
            readiness_text = "Completed"
        elif active:
            readiness_text = "Working"
        else:
            readiness_text = "Not ready"
        self.readiness_text_var.set(readiness_text)
        self._set_widget_enabled(
            self.open_completed_button,
            not active and len(self._last_outputs) == 1 and self._last_outputs[0].is_file(),
        )
        self._set_widget_enabled(
            self.open_destination_button,
            not active and self._last_destination is not None and self._last_destination.is_dir(),
        )

    def _on_draft_changed(self) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        self._refresh_output_preview()
        self._refresh_state()

    def _on_compression_changed(self) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        self._refresh_output_preview()
        self._refresh_state()

    def _on_grayscale_changed(self) -> None:
        self._mark_draft_edited()
        self._runtime_error = ""
        self._refresh_output_preview()
        self._refresh_state()

    # ------------------------------------------------------------ job process
    def _make_job_request(self) -> JobRequest:
        points, point_errors = self._parsed_split_points()
        if point_errors and self._mode() is StructureMode.SPLIT:
            raise ValueError(point_errors[0])
        job_id = uuid.uuid4().hex
        return self._create_request(job_id=job_id, points=points)

    def _start_process(self) -> None:
        # Lock immediately, before any validation or request construction, to
        # make a rapid double-click harmless.
        self._set_process_enabled(False)
        self.readiness_canvas.itemconfigure(self.readiness_indicator, fill="#e6395b")
        self.readiness_text_var.set("Working")
        if self._blocking_errors():
            self._refresh_state()
            return
        try:
            request = self._make_job_request()
        except (OSError, ValueError) as exc:
            self._set_runtime_error(str(exc))
            return
        self._active_request = request
        self._active_job_id = request.job_id
        self._phase = "preflighting"
        self._runtime_error = ""
        self._terminal_received = False
        self._last_outputs = ()
        self._last_destination = None
        self.progress_var.set(0)
        self.file_progress_var.set(0)
        self.file_progress_text_var.set("Checking the complete job…")
        self._last_progress_signature = None
        self._last_progress_log_key = None
        self._reset_synthetic_progress()
        now = time.monotonic()
        self._job_started_at = now
        self._last_progress_at = now
        self._job_started_wall = time.time()
        self.stage_var.set("Checking the complete job")
        self._replace_text(self.status_area, "Checking the complete job before processing…")
        self._refresh_state()

        thread = threading.Thread(
            target=self._run_authoritative_preflight,
            args=(request,),
            daemon=True,
            name=f"pros-authoritative-{request.job_id[:6]}",
        )
        thread.start()

    def _run_authoritative_preflight(self, request: JobRequest) -> None:
        try:
            report = preflight(request)
            self._authoritative_queue.put((request.job_id, report))
        except Exception:  # noqa: BLE001 - background boundary reports safe error
            self._authoritative_queue.put(
                (
                    request.job_id,
                    RuntimeError(
                        "PROS could not finish checking this job. Confirm that the input files and output folder are still available, then try again."
                    ),
                )
            )

    def _drain_authoritative_queue(self) -> None:
        while True:
            try:
                job_id, payload = self._authoritative_queue.get_nowait()
            except queue.Empty:
                return
            if job_id != self._active_job_id or self._phase != "preflighting":
                continue
            if isinstance(payload, Exception):
                self._finish_failed(str(payload))
                continue
            report = payload
            if not getattr(report, "valid", False):
                self._finish_failed(
                    "\n".join(getattr(report, "errors", ()))
                    or "The final job checks did not pass. Review the errors and try again."
                )
                continue
            warnings = tuple(getattr(report, "warnings", ()))
            for warning in warnings:
                self._append_status(f"Warning: {warning}")
            preservation = [
                item
                for item in warnings
                if any(
                    marker in item.casefold()
                    for marker in ("cannot be safely", "may not be safely", "digital signature", "require destination remapping")
                )
            ]
            if preservation and not messagebox.askokcancel(
                "PDF preservation warning",
                "The following PDF features may change during processing:\n\n"
                + "\n".join(f"• {item}" for item in preservation)
                + "\n\nContinue with this job?",
                parent=self,
                icon="warning",
            ):
                self._finish_failed("Processing was not started because the preservation warning was declined.")
                continue
            self._launch_worker()

    def _launch_worker(self) -> None:
        request = self._active_request
        if request is None:
            self._finish_failed("The job request is unavailable.")
            return
        try:
            context = multiprocessing.get_context("spawn")
            self._worker_queue = context.Queue()
            self._cancel_event = context.Event()
            self._worker_process = context.Process(
                target=worker_entry,
                args=(request, self._worker_queue, self._cancel_event),
                name=f"PROS-{request.job_id[:8]}",
                # Core compression may use a short-lived child process to keep
                # native qpdf calls observable; daemonic processes cannot do so.
                daemon=False,
            )
            self._worker_process.start()
        except (OSError, RuntimeError, TypeError, ValueError):
            self._finish_failed(
                "PROS could not start processing. Close other PROS windows and try again."
            )
            return
        now = time.monotonic()
        self._last_progress_at = now
        self._cancel_requested_at = None
        self._timeout_reason = None
        self._last_progress_signature = None
        self._last_progress_log_key = None
        self._dead_process_polls = 0
        self._phase = "processing"
        self.stage_var.set("Processing")
        self._append_status("All checks passed. Processing started.")
        self._refresh_state()

    def _cancel_process(self) -> None:
        if self._phase == "preflighting":
            # A thread cannot be force-stopped, but invalidating the id makes its
            # eventual result harmless and no PDF mutation has started.
            request = self._active_request
            self._active_job_id = None
            self._phase = "failed"
            self.stage_var.set("Cancelled")
            self._append_status("Job checks were cancelled. No output file was created.")
            if request:
                self._cleanup_staging(request)
            self._clear_passwords()
            self._finish_worker_handles()
            self._request_inspection()
            self._refresh_inputs()
            self._refresh_state()
            return
        if self._phase != "processing" or self._cancel_event is None:
            return
        self._phase = "cancelling"
        self._cancel_requested_at = time.monotonic()
        self.stage_var.set("Cancelling")
        self._append_status("Cancellation requested. PROS is safely cleaning up temporary files…")
        try:
            self._cancel_event.set()
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
        self._refresh_state()

    @staticmethod
    def _compression_meter(percent: float) -> str:
        bounded = max(0, min(100, int(percent)))
        marks = "-" * (bounded // 2)
        return f"[{marks}] {bounded}%"

    def _reset_synthetic_progress(self) -> None:
        self._progress_stage = ""
        self._progress_file_index = 0
        self._progress_file_count = 0
        self._progress_target = "current PDF"
        self._display_phase_percent = 0.0
        self._synthetic_progress_interval = 1.0
        self._next_synthetic_progress_at = 0.0

    def _synthetic_interval_for_active_job(self, file_count: int) -> float:
        """Return a conservative size-aware cadence for display-only progress."""

        total_bytes = 0
        for row in self._inputs:
            if row.info is not None and row.info.size_bytes:
                total_bytes += row.info.size_bytes
                continue
            try:
                total_bytes += row.path.stat().st_size
            except OSError:
                pass
        bytes_per_output = total_bytes / max(1, file_count)
        size_mb = bytes_per_output / (1024 * 1024)
        # Calibrated against a 136.1 MiB / roughly 290-second real workload:
        # 2% every ~5.7 seconds reaches the 98% display cap near completion.
        return max(0.5, min(10.0, size_mb / 24.0))

    def _render_compression_progress(self) -> None:
        percent = self._display_phase_percent
        prefix = (
            f"File {self._progress_file_index} of {self._progress_file_count}: "
            if self._progress_file_index and self._progress_file_count
            else ""
        )
        detail = f"Compressing {self._progress_target} {self._compression_meter(percent)}"
        self.file_progress_var.set(percent)
        self.file_progress_text_var.set(prefix + detail)

        milestone = int(percent // 10 * 10)
        log_key = ("compress", milestone, self._progress_target)
        if milestone > 0 and log_key != self._last_progress_log_key:
            self._append_status(prefix + detail)
            self._last_progress_log_key = log_key

    def _advance_synthetic_progress(self, now: float | None = None) -> None:
        """Advance the compression display without claiming worker liveness."""

        current = time.monotonic() if now is None else now
        if (
            self._phase != "processing"
            or self._progress_stage != "compress"
            or self._next_synthetic_progress_at <= 0
            or current < self._next_synthetic_progress_at
            or self._display_phase_percent >= 98
        ):
            return
        elapsed = current - self._next_synthetic_progress_at
        steps = 1 + int(elapsed // self._synthetic_progress_interval)
        self._display_phase_percent = min(
            98.0, self._display_phase_percent + 2.0 * steps
        )
        self._next_synthetic_progress_at += steps * self._synthetic_progress_interval
        self._render_compression_progress()

    def _show_progress_event(self, event: dict[str, object]) -> None:
        # Any event reaching this method came from the worker and therefore is
        # genuine liveness. Display-only ticks use _advance_synthetic_progress
        # and deliberately never update this safety timestamp.
        received_at = time.monotonic()
        self._last_progress_at = received_at
        stage = str(event.get("stage") or "process").casefold()
        overall = event.get("percent")
        phase_value = event.get("phase_percent", overall)
        phase_percent = (
            max(0.0, min(100.0, float(phase_value)))
            if isinstance(phase_value, (int, float))
            else 0.0
        )
        if isinstance(overall, (int, float)):
            self.progress_var.set(max(0, min(100, float(overall))))

        file_index = int(event.get("file_index") or 0)
        file_count = int(event.get("file_count") or 0)
        signature = (stage, file_index, phase_percent)
        self._last_progress_signature = signature

        stage_labels = {
            "preflight": "Checking files and output location",
            "join": "Joining PDFs",
            "split": "Creating split PDF files",
            "compress": "Compressing PDF",
            "write": "Preparing PDF output",
            "verify": "Checking completed PDF",
            "commit": "Saving completed output",
            "complete": "Completed",
        }
        self.stage_var.set(stage_labels.get(stage, stage.replace("_", " ").title()))

        raw_message = self._safe_message(event.get("message") or "")
        path_value = event.get("path")
        filename = Path(str(path_value)).name if path_value else ""
        prefix = f"File {file_index} of {file_count}: " if file_index and file_count else ""
        if stage == "compress":
            target = filename or raw_message.removeprefix("Compressing ") or "current PDF"
            same_file = (
                self._progress_stage == "compress"
                and self._progress_file_index == file_index
                and self._progress_target == target
            )
            if not same_file:
                self._last_progress_log_key = None
                self._display_phase_percent = phase_percent
            else:
                # A delayed real event must never move the visible meter back.
                self._display_phase_percent = max(
                    self._display_phase_percent, phase_percent
                )
            self._progress_stage = "compress"
            self._progress_file_index = file_index
            self._progress_file_count = file_count
            self._progress_target = target
            self._synthetic_progress_interval = self._synthetic_interval_for_active_job(
                file_count
            )
            self._next_synthetic_progress_at = (
                received_at + self._synthetic_progress_interval
            )
            self._render_compression_progress()
            return
        else:
            self._progress_stage = stage
            self._next_synthetic_progress_at = 0.0
            self._display_phase_percent = phase_percent
            self.file_progress_var.set(phase_percent)
            detail = self._friendly_message(raw_message) or stage_labels.get(stage, "Working")
        self.file_progress_text_var.set(prefix + detail)

        if raw_message:
            self._append_status(raw_message)

    def _drain_worker_queue(self) -> None:
        if self._worker_queue is None:
            return
        while True:
            try:
                event = self._worker_queue.get_nowait()
            except queue.Empty:
                break
            except (EOFError, OSError):
                break
            if not isinstance(event, dict):
                continue
            if event.get("job_id") not in {None, self._active_job_id}:
                continue
            kind = str(event.get("kind", "progress"))
            if kind == "result":
                self._terminal_received = True
                result = event.get("result")
                if isinstance(result, JobResult):
                    self._handle_job_result(result)
                else:
                    self._finish_failed(self._safe_message(event.get("message", "Processing failed.")))
                return
            self._show_progress_event(event)

    def _check_worker(self) -> None:
        now = time.monotonic()
        if self._phase == "preflighting":
            if now - self._job_started_at > TOTAL_JOB_TIMEOUT_SECONDS:
                self._finish_failed(
                    "The 30-minute job limit was reached while PROS was checking the files and output folder. No output file was created.",
                    timed_out=True,
                )
            elif now - self._last_progress_at > NO_PROGRESS_TIMEOUT_SECONDS:
                self._finish_failed(
                    "No progress was detected for five minutes while PROS was checking the job. No output file was created.",
                    timed_out=True,
                )
            return
        process = self._worker_process
        if process is None or self._phase not in {"processing", "cancelling"}:
            return
        if self._phase == "processing" and self._timeout_reason is None:
            if now - self._job_started_at > TOTAL_JOB_TIMEOUT_SECONDS:
                self._begin_timeout("total")
            elif now - self._last_progress_at > NO_PROGRESS_TIMEOUT_SECONDS:
                self._begin_timeout("no_progress")

        if (
            self._phase == "cancelling"
            and self._cancel_requested_at is not None
            and process.is_alive()
            and now - self._cancel_requested_at > CANCEL_GRACE_SECONDS
        ):
            try:
                process.terminate()
                process.join(timeout=2)
            except (OSError, ValueError):
                pass
            if self._timeout_reason == "no_progress":
                self._finish_failed(NO_PROGRESS_FORCED_ERROR, timed_out=True, forced=True)
            elif self._timeout_reason == "total":
                self._finish_failed(
                    "Processing reached the 30-minute limit and did not stop in time, so it was forcibly ended. Any temporary, partial, or newly created output files from this task were removed.",
                    timed_out=True,
                    forced=True,
                )
            else:
                self._finish_failed(FORCED_CANCELLATION_ERROR, forced=True)
            return

        if not process.is_alive() and not self._terminal_received:
            self._dead_process_polls += 1
            if self._dead_process_polls >= 3:
                self._drain_worker_queue()
                if not self._terminal_received and self._worker_process is not None:
                    code = self._worker_process.exitcode
                    self._finish_failed(
                        f"Processing stopped unexpectedly (code {code}). No completed output file was created."
                    )

    def _begin_timeout(self, reason: str) -> None:
        self._timeout_reason = reason
        self._phase = "cancelling"
        self._cancel_requested_at = time.monotonic()
        self.stage_var.set("Stopping safely and cleaning up")
        if reason == "no_progress":
            self._append_status(NO_PROGRESS_CANCELLING_STATUS)
        else:
            self._append_status(
                "Processing reached the 30-minute limit. We have requested safe cancellation and are cleaning up temporary files."
            )
        try:
            if self._cancel_event is not None:
                self._cancel_event.set()
        except (BrokenPipeError, EOFError, OSError, ValueError):
            pass
        self._refresh_state()

    def _handle_job_result(self, result: JobResult) -> None:
        if result.job_id != self._active_job_id:
            return
        timeout_reason = self._timeout_reason
        for warning in result.warnings:
            self._append_status(f"Warning: {warning}")
        if result.success:
            self._phase = "succeeded"
            self.progress_var.set(100)
            self.file_progress_var.set(100)
            self.file_progress_text_var.set("All output files were created and checked successfully.")
            self.stage_var.set("Completed")
            self._last_outputs = tuple(Path(path) for path in result.output_paths)
            self._last_destination = self._last_outputs[0].parent if self._last_outputs else None
            summary = "Processing completed successfully."
            if result.original_size_bytes or result.output_size_bytes:
                summary += (
                    f" Original size: {_format_size(result.original_size_bytes)};"
                    f" output size: {_format_size(result.output_size_bytes)}."
                )
            if result.reduction_percent is not None:
                summary += f" Reduction: {result.reduction_percent:.2f}%."
            self._append_status(summary)
            self._runtime_error = ""
        elif result.cancelled:
            self._phase = "failed"
            self.progress_var.set(0)
            self.file_progress_var.set(0)
            self.stage_var.set("Cancelled")
            if timeout_reason == "no_progress":
                self._append_status(NO_PROGRESS_CLEANUP_STATUS)
                self._runtime_error = (
                    "No progress was detected for five minutes. Processing was cancelled safely, "
                    "temporary files were removed, and no completed output file was created."
                )
            else:
                self._append_status("Cancellation and cleanup are complete. No completed output file was created.")
                self._runtime_error = self._friendly_message(result.error or "The job was cancelled.")
        else:
            self._phase = "failed"
            self.progress_var.set(0)
            self.file_progress_var.set(0)
            self.stage_var.set("Failed")
            self._runtime_error = self._friendly_message(result.error or "Processing failed.")
        request = self._active_request
        self._clear_passwords(invalidate_encrypted=not result.success)
        if request:
            self._cleanup_staging(request)
        self._finish_worker_handles()
        if not result.success:
            self._request_inspection()
        self._refresh_inputs()
        self._refresh_state()

    def _finish_failed(
        self, message: str, timed_out: bool = False, forced: bool = False
    ) -> None:
        request = self._active_request
        self._phase = "failed"
        self.progress_var.set(0)
        self.file_progress_var.set(0)
        self.file_progress_text_var.set("No file is being processed.")
        self.stage_var.set("Timed out" if timed_out else "Failed")
        self._runtime_error = self._friendly_message(message)
        if timed_out:
            if self._timeout_reason == "no_progress":
                self._append_status(NO_PROGRESS_CLEANUP_STATUS)
            else:
                self._append_status("Cleanup is complete. No completed output file was created.")
        self._clear_passwords()
        if request:
            self._cleanup_staging(request, remove_possible_outputs=forced)
        self._finish_worker_handles()
        self._request_inspection()
        self._refresh_inputs()
        self._refresh_state()

    def _cleanup_staging(
        self, request: JobRequest, *, remove_possible_outputs: bool = False
    ) -> None:
        staging = Path(request.staging_dir)
        try:
            temp_root = Path(tempfile.gettempdir()).resolve()
            resolved = staging.resolve(strict=False)
            if resolved.parent == temp_root and resolved.name.startswith("PROS-"):
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass
        # A force-terminated worker cannot execute its ``finally`` blocks.
        # Its destination-side partials have an engine-defined, narrowly
        # scoped name based on the exact proposed output filename.
        try:
            for output in build_output_paths(request):
                literal_prefix = f".{output.name}."
                for partial in output.parent.iterdir():
                    # Do not use a glob containing the user-entered base name:
                    # otherwise literal '[' and ']' characters would be
                    # interpreted as a wildcard character class.
                    if (
                        partial.name.startswith(literal_prefix)
                        and partial.name.endswith(".partial")
                        and partial.is_file()
                    ):
                        partial.unlink(missing_ok=True)
                if remove_possible_outputs and output.is_file():
                    try:
                        created_during_job = output.stat().st_mtime >= self._job_started_wall - 2
                    except OSError:
                        created_during_job = False
                    if created_during_job:
                        output.unlink(missing_ok=True)
        except OSError:
            pass

    def _finish_worker_handles(self) -> None:
        process = self._worker_process
        if process is not None:
            try:
                process.join(timeout=0.2)
            except (AssertionError, OSError, ValueError):
                pass
        if self._worker_queue is not None:
            try:
                self._worker_queue.close()
                self._worker_queue.cancel_join_thread()
            except (AttributeError, OSError, ValueError):
                pass
        if self._active_request is not None:
            self._active_request.passwords[:] = [None] * len(self._active_request.passwords)
        self._worker_process = None
        self._worker_queue = None
        self._cancel_event = None
        self._active_request = None
        self._active_job_id = None
        self._cancel_requested_at = None
        self._timeout_reason = None

    def _poll_background_events(self) -> None:
        try:
            self._drain_inspection_queue()
            self._drain_authoritative_queue()
            self._drain_worker_queue()
            self._advance_synthetic_progress()
            self._check_worker()
        finally:
            if self.winfo_exists():
                self.after(POLL_INTERVAL_MS, self._poll_background_events)

    # ------------------------------------------------------------- result/open
    def _open_path(self, path: Path) -> None:
        try:
            if os.name == "nt":
                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":  # pragma: no cover - Windows target
                import subprocess

                subprocess.Popen(["open", str(path)])
            else:  # pragma: no cover - Windows target
                import subprocess

                subprocess.Popen(["xdg-open", str(path)])
        except OSError as exc:
            self._set_runtime_error(f"Could not open {path.name} ({exc.strerror or type(exc).__name__}).")

    def _open_completed_file(self) -> None:
        if len(self._last_outputs) == 1 and self._last_outputs[0].is_file():
            self._open_path(self._last_outputs[0])

    def _open_destination_folder(self) -> None:
        if self._last_destination and self._last_destination.is_dir():
            self._open_path(self._last_destination)

    # --------------------------------------------------------------- help/exit
    def _show_about(self) -> None:
        try:
            from . import __version__
        except ImportError:  # pragma: no cover
            __version__ = "1.0"
        messagebox.showinfo(
            "About PROS",
            f"PROS {__version__}\n\nPortable, offline PDF processing for Windows.\n"
            "Password removal · Compression · Order/Join · Split\n\n"
            "Original source PDFs are never modified.",
            parent=self,
        )

    def _show_third_party_notices(self) -> None:
        path = _resource_path("THIRD_PARTY_NOTICES.txt")
        try:
            contents = path.read_text(encoding="utf-8") if path else ""
        except OSError as exc:
            contents = f"Third-party notices could not be read ({type(exc).__name__})."
        if not contents:
            contents = "THIRD_PARTY_NOTICES.txt was not found in this application bundle."

        window = tk.Toplevel(self)
        window.title("PROS — Third-party notices")
        window.geometry("800x600")
        window.minsize(540, 360)
        holder = ttk.Frame(window, padding=12)
        holder.pack(fill="both", expand=True)
        text = tk.Text(holder, wrap="word")
        scroll = ttk.Scrollbar(holder, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        text.insert("1.0", contents)
        text.configure(state="disabled")
        ttk.Button(window, text="Close", command=window.destroy).pack(pady=(0, 12))
        window.transient(self)
        window.focus_set()

    def _on_close(self) -> None:
        active = self._phase in {"preflighting", "processing", "cancelling"}
        if active and not messagebox.askyesno(
            "Exit PROS",
            "A job is active. Cancel it, remove temporary output, and exit?",
            parent=self,
        ):
            return
        if self._cancel_event is not None:
            try:
                self._cancel_event.set()
            except (BrokenPipeError, EOFError, OSError, ValueError):
                pass
        process = self._worker_process
        if process is not None and process.is_alive():
            process.join(timeout=2)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)
        if self._active_request:
            self._cleanup_staging(self._active_request)
        self._clear_passwords()
        self._finish_worker_handles()
        try:
            self.main_canvas.unbind_all("<MouseWheel>")
        except tk.TclError:
            pass
        self.destroy()


# Friendly aliases for integration and tests.
MainWindow = ProsApp


def _enable_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def run_app() -> int:
    """Create the main window and run the Tk event loop."""

    _enable_windows_dpi_awareness()
    app = ProsApp()
    app.mainloop()
    return 0


def run() -> int:
    """Alias used by embedders that prefer a shorter entry point."""

    return run_app()


def main() -> int:
    return run_app()


__all__ = ["MainWindow", "ProsApp", "main", "run", "run_app"]
