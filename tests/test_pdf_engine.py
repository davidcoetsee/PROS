from __future__ import annotations

import hashlib
import queue
import threading
from pathlib import Path

import pikepdf
import pytest

from pros.models import JobRequest, StructureMode
from pros.pdf_engine import inspect_pdf, process_job
from pros.worker import run_worker


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _make_pdf(
    path: Path,
    widths: list[int],
    *,
    title: str,
    password: str | None = None,
    field_name: str | None = None,
    bookmarks: list[tuple[str, int]] | None = None,
    labels: list[str] | None = None,
) -> None:
    with pikepdf.Pdf.new() as pdf:
        pages = [pdf.add_blank_page(page_size=(width, 300)) for width in widths]
        pdf.docinfo["/Title"] = title
        if field_name is not None:
            widget = pdf.make_indirect(
                pikepdf.Dictionary(
                    Type=pikepdf.Name("/Annot"),
                    Subtype=pikepdf.Name("/Widget"),
                    FT=pikepdf.Name("/Tx"),
                    T=field_name,
                    V=f"value-{title}",
                    Rect=[20, 20, 180, 45],
                    P=pages[0].obj,
                )
            )
            pages[0].obj["/Annots"] = [widget]
            pdf.Root["/AcroForm"] = pdf.make_indirect(
                pikepdf.Dictionary(Fields=[widget], NeedAppearances=True)
            )
        if bookmarks:
            with pdf.open_outline() as outline:
                outline.root.extend(
                    pikepdf.OutlineItem(name, page_index)
                    for name, page_index in bookmarks
                )
        if labels:
            label_tree = pikepdf.NumberTree.new(pdf)
            pdf.Root["/PageLabels"] = label_tree.obj
            for index, label in enumerate(labels):
                label_tree[index] = pikepdf.Dictionary(P=label)
        encryption = None
        if password is not None:
            encryption = pikepdf.Encryption(
                owner=f"owner-{password}",
                user=password,
                R=6,
                aes=True,
            )
        pdf.save(path, encryption=encryption, fix_metadata_version=False)


def _request(
    tmp_path: Path,
    *,
    inputs: list[Path],
    passwords: list[str | None],
    mode: StructureMode,
    remove: bool = False,
    compress: bool = False,
    split_points: list[int] | None = None,
    base: str = "Result",
) -> JobRequest:
    return JobRequest(
        job_id="engine-test",
        remove_password=remove,
        compress_pdf=compress,
        structure_mode=mode,
        input_paths=inputs,
        passwords=passwords,
        split_points=split_points or [],
        output_dir=tmp_path,
        output_base=base,
        staging_dir=tmp_path / "staging",
    )


def test_inspect_and_remove_password_with_compression(tmp_path: Path) -> None:
    source = tmp_path / "secret.pdf"
    _make_pdf(source, [200, 210], title="Secret", password="correct")
    source_hash = _hash(source)

    wrong = inspect_pdf(source, "wrong")
    assert wrong.encrypted is True
    assert wrong.password_valid is False
    assert wrong.error is None

    inspected = inspect_pdf(source, "correct")
    assert inspected.page_count == 2
    assert inspected.encrypted is True
    assert inspected.password_valid is True

    events: list[dict[str, object]] = []
    result = process_job(
        _request(
            tmp_path,
            inputs=[source],
            passwords=["correct"],
            mode=StructureMode.NEITHER,
            remove=True,
            compress=True,
            base="Secret",
        ),
        progress=events.append,
    )
    assert result.success, result.error
    assert result.reduction_percent is not None
    assert result.output_paths == (tmp_path / "Secret - Pwd_Rmv - Cprs.pdf",)
    assert _hash(source) == source_hash
    assert not (tmp_path / "staging").exists()
    with pikepdf.Pdf.open(result.output_paths[0]) as output:
        assert not output.is_encrypted
        assert len(output.pages) == 2
        assert str(output.docinfo["/Title"]) == "Secret"
    assert events[-1]["kind"] == "complete"


def test_join_is_form_aware_and_preserves_order_metadata_and_bookmarks(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.pdf"
    _make_pdf(
        first,
        [101, 102],
        title="First metadata",
        field_name="shared",
        bookmarks=[("First bookmark", 1)],
    )
    _make_pdf(
        second,
        [201],
        title="Second metadata",
        field_name="shared",
        bookmarks=[("Second bookmark", 0)],
    )
    hashes = {_hash(first), _hash(second)}
    result = process_job(
        _request(
            tmp_path,
            inputs=[first, second],
            passwords=[None, None],
            mode=StructureMode.JOIN,
            base="Joined",
        )
    )
    assert result.success, result.error
    assert result.output_paths == (tmp_path / "Joined - Join.pdf",)
    assert {_hash(first), _hash(second)} == hashes
    assert any("later-source document metadata" in warning for warning in result.warnings)
    assert any("form field" in warning for warning in result.warnings)

    with pikepdf.Pdf.open(result.output_paths[0]) as output:
        widths = [int(page.mediabox[2]) for page in output.pages]
        assert widths == [101, 102, 201]
        assert str(output.docinfo["/Title"]) == "First metadata"
        field_names = [field.fully_qualified_name for field in output.acroform.fields]
        assert len(field_names) == 2
        assert len(set(field_names)) == 2
        with output.open_outline() as outline:
            assert [item.title for item in outline.root] == ["first.pdf", "second.pdf"]
            assert outline.root[0].children[0].title == "First bookmark"
            assert outline.root[1].children[0].title == "Second bookmark"


def test_split_preserves_page_order_labels_metadata_and_relevant_bookmarks(
    tmp_path: Path,
) -> None:
    source = tmp_path / "book.pdf"
    _make_pdf(
        source,
        [101, 102, 103, 104, 105],
        title="Book metadata",
        bookmarks=[("Start", 0), ("Middle", 2), ("End", 4)],
        labels=["i", "ii", "1", "2", "A-1"],
    )
    source_hash = _hash(source)
    result = process_job(
        _request(
            tmp_path,
            inputs=[source],
            passwords=[None],
            mode=StructureMode.SPLIT,
            split_points=[2, 4],
            base="Book",
        )
    )
    assert result.success, result.error
    assert [path.name for path in result.output_paths] == [
        "Book - Part 1.pdf",
        "Book - Part 2.pdf",
        "Book - Part 3.pdf",
    ]
    assert _hash(source) == source_hash

    expected = [([101, 102], ["i", "ii"], ["Start"]), ([103, 104], ["1", "2"], ["Middle"]), ([105], ["A-1"], ["End"])]
    for path, (widths, labels, outline_titles) in zip(result.output_paths, expected, strict=True):
        with pikepdf.Pdf.open(path) as output:
            assert [int(page.mediabox[2]) for page in output.pages] == widths
            assert [str(page.label) for page in output.pages] == labels
            assert str(output.docinfo["/Title"]) == "Book metadata"
            with output.open_outline() as outline:
                assert [item.title for item in outline.root] == outline_titles


class _AlreadyCancelled:
    def is_set(self) -> bool:
        return True


def test_cooperative_cancel_leaves_no_outputs_or_staging(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, [100], title="Source")
    request = _request(
        tmp_path,
        inputs=[source],
        passwords=[None],
        mode=StructureMode.NEITHER,
        compress=True,
        base="Cancelled",
    )
    result = process_job(request, cancel_event=_AlreadyCancelled())
    assert not result.success
    assert result.cancelled
    assert not (tmp_path / "Cancelled - Cprs.pdf").exists()
    assert not request.staging_dir.exists()


def test_worker_publishes_terminal_result_event(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, [100], title="Source")
    request = _request(
        tmp_path,
        inputs=[source],
        passwords=[None],
        mode=StructureMode.NEITHER,
        compress=True,
        base="Worker",
    )
    messages: queue.SimpleQueue[dict[str, object]] = queue.SimpleQueue()
    result = run_worker(request, messages, None)
    assert result.success, result.error
    drained: list[dict[str, object]] = []
    while not messages.empty():
        drained.append(messages.get())
    assert drained[-1]["kind"] == "result"
    assert drained[-1]["result"] == result


@pytest.mark.parametrize(
    ("remove", "compress", "mode"),
    [
        (True, False, StructureMode.NEITHER),
        (False, True, StructureMode.NEITHER),
        (False, False, StructureMode.JOIN),
        (False, False, StructureMode.SPLIT),
        (True, True, StructureMode.NEITHER),
        (True, False, StructureMode.JOIN),
        (True, False, StructureMode.SPLIT),
        (False, True, StructureMode.JOIN),
        (False, True, StructureMode.SPLIT),
        (True, True, StructureMode.JOIN),
        (True, True, StructureMode.SPLIT),
    ],
)
def test_every_permitted_function_combination(
    tmp_path: Path,
    remove: bool,
    compress: bool,
    mode: StructureMode,
) -> None:
    input_count = 2 if mode is StructureMode.JOIN else 1
    inputs: list[Path] = []
    passwords: list[str | None] = []
    source_hashes: dict[Path, str] = {}
    for index in range(input_count):
        path = tmp_path / f"input-{index + 1}.pdf"
        password = f"password-{index + 1}" if remove else None
        page_widths = [100 + index] if mode is StructureMode.JOIN else [100, 101]
        _make_pdf(path, page_widths, title=f"Input {index + 1}", password=password)
        inputs.append(path)
        passwords.append(password)
        source_hashes[path] = _hash(path)

    request = _request(
        tmp_path,
        inputs=inputs,
        passwords=passwords,
        mode=mode,
        remove=remove,
        compress=compress,
        split_points=[1] if mode is StructureMode.SPLIT else [],
        base="Combination",
    )
    result = process_job(request)
    assert result.success, result.error
    assert len(result.output_paths) == (2 if mode is StructureMode.SPLIT else 1)
    assert {path: _hash(path) for path in inputs} == source_hashes
    for path in result.output_paths:
        with pikepdf.Pdf.open(path) as output:
            assert not output.is_encrypted
            assert len(output.pages) >= 1


def test_late_cancel_and_terminal_callback_error_do_not_reverse_commit(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.pdf"
    _make_pdf(source, [100], title="Source")
    cancel_event = threading.Event()

    def progress(event: dict[str, object]) -> None:
        if event["kind"] == "complete":
            cancel_event.set()
            raise RuntimeError("simulated closed UI queue")

    request = _request(
        tmp_path,
        inputs=[source],
        passwords=[None],
        mode=StructureMode.NEITHER,
        compress=True,
        base="Late Cancel",
    )
    result = process_job(request, progress=progress, cancel_event=cancel_event)
    assert result.success, result.error
    assert not result.cancelled
    assert result.output_paths[0].is_file()
