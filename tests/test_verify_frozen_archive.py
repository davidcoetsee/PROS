# SPDX-License-Identifier: MPL-2.0
"""Focused tests for the exact frozen-archive and PE resource verifier."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tools import verify_frozen_archive as verifier


def _zip_payload(names: list[str]) -> bytes:
    payload = BytesIO()
    with zipfile.ZipFile(payload, mode="w") as archive:
        for name in names:
            archive.writestr(name, f"payload for {name}")
    return payload.getvalue()


def _sample_inventory(*, module_name: str = "pros.gui") -> dict[str, Any]:
    return {
        "carchive_entries": [
            {"name": "PYZ.pyz", "typecode": "z"},
            {"name": "base_library.zip", "typecode": "b"},
            {"name": "main", "typecode": "s"},
        ],
        "carchive_options": ["pyi-contents-directory _internal"],
        "pyz_modules": {
            "PYZ.pyz": [
                {"name": module_name, "typecode": 0},
                {"name": "pros", "typecode": 1},
            ]
        },
        "base_library_zip": {
            "base_library.zip": ["encodings/__init__.pyc", "types.pyc"]
        },
    }


def test_inventory_uses_typed_reader_layers_and_zip_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "PROS.exe"
    executable.write_bytes(b"not needed by the mocked reader")
    base_payload = _zip_payload(["types.pyc", "encodings/__init__.pyc"])

    class FakeCArchiveReader:
        def __init__(self, filename: str) -> None:
            assert filename == str(executable.resolve())
            self.options = ["z-option", "a-option"]
            self.toc = {
                "main": (0, 1, 1, 0, "s"),
                "base_library.zip": (1, 1, 1, 0, "b"),
                "PYZ.pyz": (2, 1, 1, 0, "z"),
            }

        @staticmethod
        def open_embedded_archive(name: str) -> SimpleNamespace:
            assert name == "PYZ.pyz"
            return SimpleNamespace(
                toc={"pros.gui": (0, 10, 20), "pros": (1, 30, 40)}
            )

        @staticmethod
        def extract(name: str) -> bytes:
            assert name == "base_library.zip"
            return base_payload

    monkeypatch.setattr(verifier, "CArchiveReader", FakeCArchiveReader)
    inventory = verifier.inventory_frozen_archive(executable)

    assert inventory == {
        "carchive_entries": [
            {"name": "PYZ.pyz", "typecode": "z"},
            {"name": "base_library.zip", "typecode": "b"},
            {"name": "main", "typecode": "s"},
        ],
        "carchive_options": ["a-option", "z-option"],
        "pyz_modules": {
            "PYZ.pyz": [
                {"name": "pros", "typecode": 1},
                {"name": "pros.gui", "typecode": 0},
            ]
        },
        "base_library_zip": {
            "base_library.zip": ["encodings/__init__.pyc", "types.pyc"]
        },
    }


def test_manifest_diff_names_every_added_and_removed_entry() -> None:
    expected = verifier.build_manifest(
        _sample_inventory(module_name="pros.old"),
        pros_version="1.5.1",
        pyinstaller_version="6.22.1",
    )
    actual_inventory = _sample_inventory(module_name="pros.new")
    actual_inventory["carchive_entries"].append(
        {"name": "new-runtime.dll", "typecode": "b"}
    )
    actual = verifier.build_manifest(
        actual_inventory,
        pros_version="1.5.1",
        pyinstaller_version="6.22.1",
    )

    diff = verifier.compare_manifests(expected, actual)
    assert not diff["ok"]
    assert diff["inventory_changes"]["carchive_entries"]["added"] == [
        "new-runtime.dll [typecode=b]"
    ]
    assert diff["inventory_changes"]["pyz_modules:PYZ.pyz"] == {
        "added": ["pros.new [typecode=0]"],
        "removed": ["pros.old [typecode=0]"],
    }
    diagnostic = verifier.format_manifest_diff(diff)
    assert "    + pros.new [typecode=0]" in diagnostic
    assert "    - pros.old [typecode=0]" in diagnostic


def test_manifest_is_written_only_in_explicit_update_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "PROS.exe"
    executable.write_bytes(b"mock executable")
    icon = tmp_path / "PROS.ico"
    icon.write_bytes(b"mock icon")
    manifest_path = tmp_path / "frozen_archive_manifest.json"
    inventory = _sample_inventory()

    monkeypatch.setattr(
        verifier,
        "inspect_pe_and_icon",
        lambda _executable, _icon: {"icon_resources": 2},
    )
    monkeypatch.setattr(
        verifier,
        "inventory_frozen_archive",
        lambda _executable: inventory,
    )
    monkeypatch.setattr(verifier, "PROS_VERSION", "1.5.1")

    with pytest.raises(
        verifier.FrozenArchiveVerificationError,
        match="manifest does not exist",
    ):
        verifier.verify_or_update_manifest(executable, manifest_path, icon)
    assert not manifest_path.exists()

    updated = verifier.verify_or_update_manifest(
        executable,
        manifest_path,
        icon,
        update_manifest=True,
    )
    assert updated["mode"] == "updated"
    assert manifest_path.is_file()
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["archive_inventory"] == inventory

    verified = verifier.verify_or_update_manifest(executable, manifest_path, icon)
    assert verified["mode"] == "verified"


def test_update_refuses_to_overwrite_an_invalid_existing_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "PROS.exe"
    executable.write_bytes(b"mock executable")
    icon = tmp_path / "PROS.ico"
    icon.write_bytes(b"mock icon")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text('{"schema_version": 999}\n', encoding="utf-8")

    monkeypatch.setattr(
        verifier,
        "inspect_pe_and_icon",
        lambda _executable, _icon: {"icon_resources": 2},
    )
    monkeypatch.setattr(
        verifier,
        "inventory_frozen_archive",
        lambda _executable: _sample_inventory(),
    )

    with pytest.raises(verifier.FrozenArchiveVerificationError):
        verifier.verify_or_update_manifest(
            executable,
            manifest_path,
            icon,
            update_manifest=True,
        )
    assert json.loads(manifest_path.read_text(encoding="utf-8")) == {
        "schema_version": 999
    }


def _make_ico(images: list[tuple[int, int, bytes]]) -> bytes:
    table_size = verifier._ICO_HEADER.size + len(images) * verifier._ICO_ENTRY.size
    offset = table_size
    entries = []
    payloads = []
    for width, height, payload in images:
        entries.append(
            verifier._ICO_ENTRY.pack(
                width,
                height,
                0,
                0,
                1,
                32,
                len(payload),
                offset,
            )
        )
        payloads.append(payload)
        offset += len(payload)
    return b"".join(
        [verifier._ICO_HEADER.pack(0, 1, len(images)), *entries, *payloads]
    )


def _make_group_icon(images: list[tuple[int, int, bytes]]) -> bytes:
    entries = []
    for resource_id, (width, height, payload) in enumerate(images, start=1):
        entries.append(
            verifier._GROUP_ICON_ENTRY.pack(
                width,
                height,
                0,
                0,
                1,
                32,
                len(payload),
                resource_id,
            )
        )
    return b"".join(
        [verifier._ICO_HEADER.pack(0, 1, len(images)), *entries]
    )


def _resource_type(resource_type: int, payloads: list[bytes]) -> tuple[Any, dict[int, bytes]]:
    resources = []
    data_by_rva = {}
    for resource_id, payload in enumerate(payloads, start=1):
        rva = resource_type * 10_000 + resource_id
        data_by_rva[rva] = payload
        language = SimpleNamespace(
            name=None,
            id=0,
            data=SimpleNamespace(
                struct=SimpleNamespace(OffsetToData=rva, Size=len(payload))
            ),
        )
        resources.append(
            SimpleNamespace(
                name=None,
                id=resource_id,
                directory=SimpleNamespace(entries=[language]),
            )
        )
    return (
        SimpleNamespace(
            name=None,
            id=resource_type,
            directory=SimpleNamespace(entries=resources),
        ),
        data_by_rva,
    )


def _fake_pe(
    images: list[tuple[int, int, bytes]],
    *,
    machine: int = 0x8664,
    subsystem: int = 2,
) -> Any:
    group_payload = _make_group_icon(images)
    icon_type, icon_data = _resource_type(3, [item[2] for item in images])
    group_type, group_data = _resource_type(14, [group_payload])
    data_by_rva = {**icon_data, **group_data}

    class FakePe:
        FILE_HEADER = SimpleNamespace(Machine=machine)
        OPTIONAL_HEADER = SimpleNamespace(Subsystem=subsystem)
        DIRECTORY_ENTRY_RESOURCE = SimpleNamespace(
            entries=[icon_type, group_type]
        )

        @staticmethod
        def get_data(rva: int, size: int) -> bytes:
            payload = data_by_rva[rva]
            assert len(payload) == size
            return payload

        @staticmethod
        def close() -> None:
            return None

    return FakePe()


def test_pe_icon_resources_must_exactly_match_the_canonical_ico(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    images = [(16, 16, b"first icon payload"), (32, 32, b"second icon payload")]
    executable = tmp_path / "PROS.exe"
    executable.write_bytes(b"mock PE")
    icon = tmp_path / "PROS.ico"
    icon.write_bytes(_make_ico(images))
    fake_pe = _fake_pe(images)
    monkeypatch.setattr(verifier.pefile, "PE", lambda *_args, **_kwargs: fake_pe)

    report = verifier.inspect_pe_and_icon(executable, icon)
    assert report["machine"] == "AMD64"
    assert report["subsystem"] == "WINDOWS_GUI"
    assert report["group_icon_resources"] == 1
    assert report["icon_resources"] == 2
    assert len(report["image_sha256"]) == 2

    corrupted_images = [images[0], (32, 32, b"changed icon payload")]
    fake_pe = _fake_pe(corrupted_images)
    monkeypatch.setattr(verifier.pefile, "PE", lambda *_args, **_kwargs: fake_pe)
    with pytest.raises(
        verifier.FrozenArchiveVerificationError,
        match="does not exactly match",
    ):
        verifier.inspect_pe_and_icon(executable, icon)


@pytest.mark.parametrize(
    ("machine", "subsystem", "error"),
    [
        (0x14C, 2, "PE machine mismatch"),
        (0x8664, 3, "PE subsystem mismatch"),
    ],
)
def test_pe_headers_must_be_amd64_windows_gui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    machine: int,
    subsystem: int,
    error: str,
) -> None:
    images = [(16, 16, b"icon payload")]
    executable = tmp_path / "PROS.exe"
    executable.write_bytes(b"mock PE")
    icon = tmp_path / "PROS.ico"
    icon.write_bytes(_make_ico(images))
    monkeypatch.setattr(
        verifier.pefile,
        "PE",
        lambda *_args, **_kwargs: _fake_pe(
            images,
            machine=machine,
            subsystem=subsystem,
        ),
    )

    with pytest.raises(
        verifier.FrozenArchiveVerificationError,
        match=error,
    ):
        verifier.inspect_pe_and_icon(executable, icon)


@pytest.mark.skipif(
    not verifier.DEFAULT_EXECUTABLE.is_file(),
    reason="No local frozen executable is available for reader integration",
)
def test_local_frozen_executable_uses_the_real_reader_and_icon_resources() -> None:
    inventory = verifier.inventory_frozen_archive(verifier.DEFAULT_EXECUTABLE)
    pe_report = verifier.inspect_pe_and_icon(
        verifier.DEFAULT_EXECUTABLE,
        verifier.DEFAULT_ICON,
    )
    assert "PYZ.pyz" in inventory["pyz_modules"]
    assert set(inventory["base_library_zip"]) == {"base_library.zip"}
    assert pe_report["machine_value"] == verifier.EXPECTED_PE_MACHINE
    assert pe_report["subsystem_value"] == verifier.EXPECTED_PE_SUBSYSTEM
    assert pe_report["icon_resources"] == 6
