# SPDX-License-Identifier: MPL-2.0
"""Verify the exact PyInstaller and Windows PE inventory of frozen PROS."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import zipfile
from collections import Counter
from dataclasses import dataclass
from importlib import metadata
from io import BytesIO
from pathlib import Path
from typing import Any

import pefile
from PyInstaller.archive.readers import (
    PKG_ITEM_PYZ,
    CArchiveReader,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pros import __version__ as PROS_VERSION

DEFAULT_EXECUTABLE = PROJECT_ROOT / "dist" / "PROS.exe"
DEFAULT_MANIFEST = PROJECT_ROOT / "packaging" / "frozen_archive_manifest.json"
DEFAULT_ICON = PROJECT_ROOT / "assets" / "PROS.ico"

MANIFEST_SCHEMA_VERSION = 1
EXPECTED_PE_MACHINE = 0x8664  # IMAGE_FILE_MACHINE_AMD64
EXPECTED_PE_SUBSYSTEM = 2  # IMAGE_SUBSYSTEM_WINDOWS_GUI
RT_ICON = 3
RT_GROUP_ICON = 14

_SEMANTIC_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_ICO_HEADER = struct.Struct("<HHH")
_ICO_ENTRY = struct.Struct("<BBBBHHII")
_GROUP_ICON_ENTRY = struct.Struct("<BBBBHHIH")


class FrozenArchiveVerificationError(RuntimeError):
    """Raised when a frozen executable or its manifest is invalid."""


class FrozenArchiveManifestMismatch(FrozenArchiveVerificationError):
    """Raised when the frozen inventory differs from its committed manifest."""

    def __init__(self, diff: dict[str, Any]):
        self.diff = diff
        super().__init__(format_manifest_diff(diff))


@dataclass(frozen=True)
class _IconImage:
    metadata: tuple[int, int, int, int, int, int, int]
    payload: bytes


@dataclass(frozen=True)
class _ResourcePayload:
    resource_id: int
    language_id: int
    payload: bytes


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _entry_typecode(toc_entry: Any, *, archive_name: str) -> str:
    try:
        typecode = toc_entry[-1]
    except (IndexError, KeyError, TypeError) as exc:
        raise FrozenArchiveVerificationError(
            f"Malformed CArchive TOC entry for {archive_name!r}"
        ) from exc
    if not isinstance(typecode, str) or len(typecode) != 1:
        raise FrozenArchiveVerificationError(
            f"Invalid CArchive type code for {archive_name!r}: {typecode!r}"
        )
    return typecode


def _pyz_typecode(toc_entry: Any, *, module_name: str) -> int:
    try:
        typecode = toc_entry[0]
    except (IndexError, KeyError, TypeError) as exc:
        raise FrozenArchiveVerificationError(
            f"Malformed PYZ TOC entry for {module_name!r}"
        ) from exc
    if not isinstance(typecode, int):
        raise FrozenArchiveVerificationError(
            f"Invalid PYZ type code for {module_name!r}: {typecode!r}"
        )
    return typecode


def inventory_frozen_archive(executable: Path) -> dict[str, Any]:
    """Inventory the outer CArchive, every nested PYZ, and base_library.zip."""

    executable = Path(executable).resolve()
    if not executable.is_file():
        raise FrozenArchiveVerificationError(
            f"Frozen executable does not exist: {executable}"
        )

    try:
        reader = CArchiveReader(str(executable))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise FrozenArchiveVerificationError(
            f"Unable to read the PyInstaller CArchive in {executable}: {exc}"
        ) from exc

    carchive_entries: list[dict[str, Any]] = []
    pyz_names: list[str] = []
    for name, toc_entry in reader.toc.items():
        typecode = _entry_typecode(toc_entry, archive_name=name)
        carchive_entries.append({"name": str(name), "typecode": typecode})
        if typecode == PKG_ITEM_PYZ:
            pyz_names.append(str(name))
    carchive_entries.sort(key=lambda item: (item["name"], item["typecode"]))

    if not pyz_names:
        raise FrozenArchiveVerificationError(
            "The CArchive contains no nested PYZ archive"
        )

    pyz_modules: dict[str, list[dict[str, Any]]] = {}
    for pyz_name in sorted(pyz_names):
        try:
            pyz_reader = reader.open_embedded_archive(pyz_name)
        except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise FrozenArchiveVerificationError(
                f"Unable to read nested PYZ archive {pyz_name!r}: {exc}"
            ) from exc
        modules = [
            {
                "name": str(module_name),
                "typecode": _pyz_typecode(toc_entry, module_name=module_name),
            }
            for module_name, toc_entry in pyz_reader.toc.items()
        ]
        modules.sort(key=lambda item: (item["name"], item["typecode"]))
        pyz_modules[pyz_name] = modules

    base_entries = [
        item for item in carchive_entries if item["name"] == "base_library.zip"
    ]
    if len(base_entries) != 1:
        raise FrozenArchiveVerificationError(
            "Expected exactly one base_library.zip CArchive entry, "
            f"found {len(base_entries)}"
        )

    try:
        base_payload = reader.extract("base_library.zip")
        if not isinstance(base_payload, bytes):
            raise TypeError("CArchiveReader.extract() did not return bytes")
        with zipfile.ZipFile(BytesIO(base_payload)) as base_zip:
            base_names = sorted(base_zip.namelist())
            corrupt_name = base_zip.testzip()
    except (KeyError, OSError, RuntimeError, TypeError, zipfile.BadZipFile) as exc:
        raise FrozenArchiveVerificationError(
            f"Unable to inventory base_library.zip: {exc}"
        ) from exc
    if corrupt_name is not None:
        raise FrozenArchiveVerificationError(
            f"base_library.zip failed its CRC check at {corrupt_name!r}"
        )
    if len(base_names) != len(set(base_names)):
        raise FrozenArchiveVerificationError(
            "base_library.zip contains duplicate member names"
        )

    options = sorted(str(option) for option in reader.options)
    return {
        "carchive_entries": carchive_entries,
        "carchive_options": options,
        "pyz_modules": pyz_modules,
        "base_library_zip": {"base_library.zip": base_names},
    }


def _parse_ico(payload: bytes) -> list[_IconImage]:
    if len(payload) < _ICO_HEADER.size:
        raise FrozenArchiveVerificationError("PROS.ico is shorter than an ICO header")
    reserved, image_type, image_count = _ICO_HEADER.unpack_from(payload)
    if reserved != 0 or image_type != 1 or image_count < 1:
        raise FrozenArchiveVerificationError(
            "PROS.ico does not contain a valid Windows icon directory"
        )
    table_end = _ICO_HEADER.size + image_count * _ICO_ENTRY.size
    if table_end > len(payload):
        raise FrozenArchiveVerificationError("PROS.ico has a truncated image table")

    images: list[_IconImage] = []
    for index in range(image_count):
        offset = _ICO_HEADER.size + index * _ICO_ENTRY.size
        fields = _ICO_ENTRY.unpack_from(payload, offset)
        width, height, colors, entry_reserved, planes, bit_count, size, image_offset = (
            fields
        )
        image_end = image_offset + size
        if size < 1 or image_offset < table_end or image_end > len(payload):
            raise FrozenArchiveVerificationError(
                f"PROS.ico image {index + 1} has an invalid offset or size"
            )
        metadata_fields = (
            width,
            height,
            colors,
            entry_reserved,
            planes,
            bit_count,
            size,
        )
        images.append(
            _IconImage(metadata=metadata_fields, payload=payload[image_offset:image_end])
        )
    return images


def _parse_group_icon(payload: bytes) -> list[tuple[tuple[int, ...], int]]:
    if len(payload) < _ICO_HEADER.size:
        raise FrozenArchiveVerificationError(
            "RT_GROUP_ICON resource is shorter than its header"
        )
    reserved, image_type, image_count = _ICO_HEADER.unpack_from(payload)
    expected_size = _ICO_HEADER.size + image_count * _GROUP_ICON_ENTRY.size
    if reserved != 0 or image_type != 1 or image_count < 1:
        raise FrozenArchiveVerificationError(
            "RT_GROUP_ICON does not contain a valid icon directory"
        )
    if len(payload) != expected_size:
        raise FrozenArchiveVerificationError(
            "RT_GROUP_ICON has an unexpected size: "
            f"expected {expected_size}, found {len(payload)}"
        )

    entries: list[tuple[tuple[int, ...], int]] = []
    for index in range(image_count):
        offset = _ICO_HEADER.size + index * _GROUP_ICON_ENTRY.size
        fields = _GROUP_ICON_ENTRY.unpack_from(payload, offset)
        entries.append((tuple(fields[:-1]), fields[-1]))
    return entries


def _resource_identifier(entry: Any, *, description: str) -> int:
    name = getattr(entry, "name", None)
    identifier = getattr(entry, "id", None)
    if name is not None or not isinstance(identifier, int):
        raise FrozenArchiveVerificationError(
            f"{description} uses a non-numeric resource identifier"
        )
    return identifier


def _extract_resource_payloads(pe: Any, resource_type: int) -> list[_ResourcePayload]:
    resource_root = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    type_entries = getattr(resource_root, "entries", None)
    if type_entries is None:
        raise FrozenArchiveVerificationError(
            "The executable has no parsed Windows resource directory"
        )

    matching_types = [
        entry
        for entry in type_entries
        if getattr(entry, "name", None) is None
        and getattr(entry, "id", None) == resource_type
    ]
    if len(matching_types) != 1:
        raise FrozenArchiveVerificationError(
            f"Expected one resource type {resource_type}, found {len(matching_types)}"
        )

    payloads: list[_ResourcePayload] = []
    seen: set[tuple[int, int]] = set()
    name_entries = getattr(matching_types[0].directory, "entries", ())
    for name_entry in name_entries:
        resource_id = _resource_identifier(
            name_entry,
            description=f"Resource type {resource_type}",
        )
        language_entries = getattr(name_entry.directory, "entries", ())
        for language_entry in language_entries:
            language_id = _resource_identifier(
                language_entry,
                description=f"Resource type {resource_type}, ID {resource_id}",
            )
            key = (resource_id, language_id)
            if key in seen:
                raise FrozenArchiveVerificationError(
                    f"Duplicate resource type {resource_type}, ID/language {key}"
                )
            seen.add(key)
            try:
                data_struct = language_entry.data.struct
                data_rva = int(data_struct.OffsetToData)
                data_size = int(data_struct.Size)
                payload = pe.get_data(data_rva, data_size)
            except (AttributeError, TypeError, ValueError) as exc:
                raise FrozenArchiveVerificationError(
                    f"Malformed resource type {resource_type}, ID/language {key}"
                ) from exc
            if not isinstance(payload, bytes) or len(payload) != data_size:
                raise FrozenArchiveVerificationError(
                    f"Truncated resource type {resource_type}, ID/language {key}"
                )
            payloads.append(_ResourcePayload(resource_id, language_id, payload))
    if not payloads:
        raise FrozenArchiveVerificationError(
            f"The executable contains no resource payloads of type {resource_type}"
        )
    return payloads


def _verify_icon_resources(
    source_images: list[_IconImage],
    icon_resources: list[_ResourcePayload],
    group_resources: list[_ResourcePayload],
) -> dict[str, Any]:
    icons_by_key = {
        (item.resource_id, item.language_id): item.payload for item in icon_resources
    }
    if len(icons_by_key) != len(icon_resources):
        raise FrozenArchiveVerificationError("RT_ICON contains duplicate resources")

    expected_metadata = [image.metadata for image in source_images]
    referenced_icons: set[tuple[int, int]] = set()
    groups_report: list[dict[str, Any]] = []
    for group in group_resources:
        group_entries = _parse_group_icon(group.payload)
        actual_metadata = [entry[0] for entry in group_entries]
        if actual_metadata != expected_metadata:
            raise FrozenArchiveVerificationError(
                "RT_GROUP_ICON metadata does not exactly match assets/PROS.ico"
            )

        group_hashes: list[str] = []
        for index, ((_, icon_id), source_image) in enumerate(
            zip(group_entries, source_images, strict=True),
            start=1,
        ):
            key = (icon_id, group.language_id)
            resource_payload = icons_by_key.get(key)
            if resource_payload is None:
                raise FrozenArchiveVerificationError(
                    "RT_GROUP_ICON references missing RT_ICON "
                    f"ID {icon_id}, language {group.language_id}"
                )
            if resource_payload != source_image.payload:
                raise FrozenArchiveVerificationError(
                    f"RT_ICON image {index} does not exactly match assets/PROS.ico"
                )
            referenced_icons.add(key)
            group_hashes.append(_sha256_bytes(resource_payload))
        groups_report.append(
            {
                "resource_id": group.resource_id,
                "language_id": group.language_id,
                "image_sha256": group_hashes,
            }
        )

    unreferenced = sorted(set(icons_by_key) - referenced_icons)
    if unreferenced:
        raise FrozenArchiveVerificationError(
            f"Unreferenced extra RT_ICON resources are present: {unreferenced}"
        )
    return {
        "group_icon_resources": len(group_resources),
        "icon_resources": len(icon_resources),
        "image_sha256": [_sha256_bytes(image.payload) for image in source_images],
        "groups": groups_report,
    }


def inspect_pe_and_icon(executable: Path, icon_path: Path) -> dict[str, Any]:
    """Verify PE architecture/subsystem and its exact Windows icon payloads."""

    executable = Path(executable).resolve()
    icon_path = Path(icon_path).resolve()
    if not executable.is_file():
        raise FrozenArchiveVerificationError(
            f"Frozen executable does not exist: {executable}"
        )
    if not icon_path.is_file():
        raise FrozenArchiveVerificationError(f"Canonical ICO does not exist: {icon_path}")

    icon_payload = icon_path.read_bytes()
    source_images = _parse_ico(icon_payload)
    try:
        pe = pefile.PE(str(executable), fast_load=False)
    except (OSError, pefile.PEFormatError) as exc:
        raise FrozenArchiveVerificationError(
            f"Unable to parse Windows PE executable {executable}: {exc}"
        ) from exc

    try:
        machine = int(pe.FILE_HEADER.Machine)
        subsystem = int(pe.OPTIONAL_HEADER.Subsystem)
        if machine != EXPECTED_PE_MACHINE:
            raise FrozenArchiveVerificationError(
                "PE machine mismatch: expected AMD64 "
                f"0x{EXPECTED_PE_MACHINE:04X}, found 0x{machine:04X}"
            )
        if subsystem != EXPECTED_PE_SUBSYSTEM:
            raise FrozenArchiveVerificationError(
                "PE subsystem mismatch: expected Windows GUI "
                f"{EXPECTED_PE_SUBSYSTEM}, found {subsystem}"
            )
        icon_resources = _extract_resource_payloads(pe, RT_ICON)
        group_resources = _extract_resource_payloads(pe, RT_GROUP_ICON)
        icon_report = _verify_icon_resources(
            source_images,
            icon_resources,
            group_resources,
        )
    except AttributeError as exc:
        raise FrozenArchiveVerificationError(
            f"Windows PE headers or resources are malformed: {exc}"
        ) from exc
    finally:
        pe.close()

    return {
        "machine": "AMD64",
        "machine_value": machine,
        "subsystem": "WINDOWS_GUI",
        "subsystem_value": subsystem,
        "canonical_icon": str(icon_path),
        "canonical_icon_sha256": _sha256_bytes(icon_payload),
        **icon_report,
    }


def build_manifest(
    inventory: dict[str, Any],
    *,
    pros_version: str = PROS_VERSION,
    pyinstaller_version: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic, versioned frozen-inventory manifest."""

    if pyinstaller_version is None:
        pyinstaller_version = metadata.version("PyInstaller")
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "pros_version": pros_version,
        "pyinstaller_version": pyinstaller_version,
        "archive_inventory": inventory,
    }
    validate_manifest(manifest)
    return manifest


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        raise FrozenArchiveVerificationError(
            f"Invalid manifest keys at {path}: missing={missing}, unknown={unknown}"
        )


def _validate_typed_entries(
    entries: Any,
    *,
    path: str,
    typecode_type: type,
) -> None:
    if not isinstance(entries, list):
        raise FrozenArchiveVerificationError(f"Manifest {path} must be a list")
    seen_names: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise FrozenArchiveVerificationError(
                f"Manifest {path}[{index}] must be an object"
            )
        _require_exact_keys(entry, {"name", "typecode"}, f"{path}[{index}]")
        name = entry["name"]
        typecode = entry["typecode"]
        if not isinstance(name, str) or not name:
            raise FrozenArchiveVerificationError(
                f"Manifest {path}[{index}].name must be a non-empty string"
            )
        if type(typecode) is not typecode_type:
            raise FrozenArchiveVerificationError(
                f"Manifest {path}[{index}].typecode has the wrong type"
            )
        if typecode_type is str and len(typecode) != 1:
            raise FrozenArchiveVerificationError(
                f"Manifest {path}[{index}].typecode must be one character"
            )
        if name in seen_names:
            raise FrozenArchiveVerificationError(
                f"Manifest {path} contains duplicate name {name!r}"
            )
        seen_names.add(name)


def _validate_name_lists(mapping: Any, *, path: str) -> None:
    if not isinstance(mapping, dict) or not mapping:
        raise FrozenArchiveVerificationError(
            f"Manifest {path} must be a non-empty object"
        )
    for archive_name, names in mapping.items():
        if not isinstance(archive_name, str) or not archive_name:
            raise FrozenArchiveVerificationError(
                f"Manifest {path} contains an invalid archive name"
            )
        if not isinstance(names, list) or not names or not all(
            isinstance(name, str) and name for name in names
        ):
            raise FrozenArchiveVerificationError(
                f"Manifest {path}.{archive_name} must be a non-empty list of names"
            )
        if len(names) != len(set(names)):
            raise FrozenArchiveVerificationError(
                f"Manifest {path}.{archive_name} contains duplicate names"
            )


def validate_manifest(manifest: Any) -> None:
    """Validate the complete committed-manifest schema, failing on unknown data."""

    if not isinstance(manifest, dict):
        raise FrozenArchiveVerificationError("Frozen archive manifest must be an object")
    _require_exact_keys(
        manifest,
        {
            "schema_version",
            "pros_version",
            "pyinstaller_version",
            "archive_inventory",
        },
        "$",
    )
    if (
        type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != MANIFEST_SCHEMA_VERSION
    ):
        raise FrozenArchiveVerificationError(
            "Unsupported frozen archive manifest schema: "
            f"expected {MANIFEST_SCHEMA_VERSION}, found "
            f"{manifest['schema_version']!r}"
        )
    if not isinstance(manifest["pros_version"], str) or not _SEMANTIC_VERSION.fullmatch(
        manifest["pros_version"]
    ):
        raise FrozenArchiveVerificationError(
            "Manifest pros_version must have numeric major.minor.patch form"
        )
    if not isinstance(manifest["pyinstaller_version"], str) or not manifest[
        "pyinstaller_version"
    ]:
        raise FrozenArchiveVerificationError(
            "Manifest pyinstaller_version must be a non-empty string"
        )

    inventory = manifest["archive_inventory"]
    if not isinstance(inventory, dict):
        raise FrozenArchiveVerificationError("Manifest archive_inventory must be an object")
    _require_exact_keys(
        inventory,
        {
            "carchive_entries",
            "carchive_options",
            "pyz_modules",
            "base_library_zip",
        },
        "$.archive_inventory",
    )
    _validate_typed_entries(
        inventory["carchive_entries"],
        path="$.archive_inventory.carchive_entries",
        typecode_type=str,
    )
    options = inventory["carchive_options"]
    if not isinstance(options, list) or not all(
        isinstance(option, str) and option for option in options
    ):
        raise FrozenArchiveVerificationError(
            "Manifest archive_inventory.carchive_options must be a list of strings"
        )
    pyz_modules = inventory["pyz_modules"]
    if not isinstance(pyz_modules, dict) or not pyz_modules:
        raise FrozenArchiveVerificationError(
            "Manifest archive_inventory.pyz_modules must be a non-empty object"
        )
    for archive_name, entries in pyz_modules.items():
        if not isinstance(archive_name, str) or not archive_name:
            raise FrozenArchiveVerificationError(
                "Manifest pyz_modules contains an invalid archive name"
            )
        _validate_typed_entries(
            entries,
            path=f"$.archive_inventory.pyz_modules.{archive_name}",
            typecode_type=int,
        )
        if not entries:
            raise FrozenArchiveVerificationError(
                f"Manifest PYZ archive {archive_name!r} contains no modules"
            )
    _validate_name_lists(
        inventory["base_library_zip"],
        path="$.archive_inventory.base_library_zip",
    )
    if set(inventory["base_library_zip"]) != {"base_library.zip"}:
        raise FrozenArchiveVerificationError(
            "Manifest must inventory exactly the base_library.zip archive"
        )


def _inventory_categories(manifest: dict[str, Any]) -> dict[str, list[str]]:
    inventory = manifest["archive_inventory"]
    categories = {
        "carchive_entries": [
            f"{entry['name']} [typecode={entry['typecode']}]"
            for entry in inventory["carchive_entries"]
        ],
        "carchive_options": list(inventory["carchive_options"]),
    }
    for archive_name, entries in inventory["pyz_modules"].items():
        categories[f"pyz_modules:{archive_name}"] = [
            f"{entry['name']} [typecode={entry['typecode']}]" for entry in entries
        ]
    for archive_name, names in inventory["base_library_zip"].items():
        categories[f"base_library_zip:{archive_name}"] = list(names)
    return categories


def compare_manifests(
    expected: dict[str, Any],
    actual: dict[str, Any],
) -> dict[str, Any]:
    """Return metadata mismatches and exact added/removed inventory entries."""

    validate_manifest(expected)
    validate_manifest(actual)
    metadata_mismatches = []
    for field in ("schema_version", "pros_version", "pyinstaller_version"):
        if expected[field] != actual[field]:
            metadata_mismatches.append(
                {
                    "field": field,
                    "expected": expected[field],
                    "actual": actual[field],
                }
            )

    expected_categories = _inventory_categories(expected)
    actual_categories = _inventory_categories(actual)
    inventory_changes: dict[str, dict[str, list[str]]] = {}
    for category in sorted(set(expected_categories) | set(actual_categories)):
        expected_counter = Counter(expected_categories.get(category, ()))
        actual_counter = Counter(actual_categories.get(category, ()))
        added = sorted((actual_counter - expected_counter).elements())
        removed = sorted((expected_counter - actual_counter).elements())
        if added or removed:
            inventory_changes[category] = {"added": added, "removed": removed}

    return {
        "ok": not metadata_mismatches and not inventory_changes,
        "metadata_mismatches": metadata_mismatches,
        "inventory_changes": inventory_changes,
    }


def format_manifest_diff(diff: dict[str, Any]) -> str:
    lines = ["Frozen archive manifest mismatch:"]
    for mismatch in diff["metadata_mismatches"]:
        lines.append(
            f"  {mismatch['field']}: expected {mismatch['expected']!r}, "
            f"found {mismatch['actual']!r}"
        )
    for category, changes in diff["inventory_changes"].items():
        lines.append(f"  {category}:")
        for entry in changes["added"]:
            lines.append(f"    + {entry}")
        for entry in changes["removed"]:
            lines.append(f"    - {entry}")
    return "\n".join(lines)


def load_manifest(path: Path) -> dict[str, Any]:
    path = Path(path).resolve()
    if not path.is_file():
        raise FrozenArchiveVerificationError(
            f"Frozen archive manifest does not exist: {path}. "
            "Create it only after an audited build with --update-manifest."
        )
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FrozenArchiveVerificationError(
            f"Unable to read frozen archive manifest {path}: {exc}"
        ) from exc
    validate_manifest(manifest)
    return manifest


def _write_manifest_atomic(path: Path, manifest: dict[str, Any]) -> None:
    validate_manifest(manifest)
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary_name = stream.name
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _inventory_counts(inventory: dict[str, Any]) -> dict[str, Any]:
    return {
        "carchive_entries": len(inventory["carchive_entries"]),
        "carchive_options": len(inventory["carchive_options"]),
        "pyz_modules": {
            name: len(entries) for name, entries in inventory["pyz_modules"].items()
        },
        "base_library_zip": {
            name: len(entries)
            for name, entries in inventory["base_library_zip"].items()
        },
    }


def verify_or_update_manifest(
    executable: Path,
    manifest_path: Path,
    icon_path: Path,
    *,
    update_manifest: bool = False,
) -> dict[str, Any]:
    """Verify an executable, or explicitly update its deterministic inventory."""

    pe_report = inspect_pe_and_icon(executable, icon_path)
    inventory = inventory_frozen_archive(executable)
    actual_manifest = build_manifest(inventory)

    if update_manifest:
        previous_manifest = None
        diff = None
        if Path(manifest_path).is_file():
            previous_manifest = load_manifest(manifest_path)
            diff = compare_manifests(previous_manifest, actual_manifest)
        _write_manifest_atomic(manifest_path, actual_manifest)
        return {
            "ok": True,
            "mode": "updated",
            "manifest": str(Path(manifest_path).resolve()),
            "previous_manifest_present": previous_manifest is not None,
            "changes": diff,
            "inventory_counts": _inventory_counts(inventory),
            "pe": pe_report,
        }

    expected_manifest = load_manifest(manifest_path)
    diff = compare_manifests(expected_manifest, actual_manifest)
    if not diff["ok"]:
        raise FrozenArchiveManifestMismatch(diff)
    return {
        "ok": True,
        "mode": "verified",
        "manifest": str(Path(manifest_path).resolve()),
        "inventory_counts": _inventory_counts(inventory),
        "pe": pe_report,
    }


def _print_human_report(report: dict[str, Any]) -> None:
    action = "updated" if report["mode"] == "updated" else "verified"
    print(f"Frozen archive manifest {action}: {report['manifest']}")
    counts = report["inventory_counts"]
    print(f"Outer CArchive entries: {counts['carchive_entries']}")
    print(
        "Nested PYZ modules: "
        + ", ".join(
            f"{name}={count}" for name, count in counts["pyz_modules"].items()
        )
    )
    print(
        "base_library.zip entries: "
        + ", ".join(
            f"{name}={count}" for name, count in counts["base_library_zip"].items()
        )
    )
    print(
        "PE: AMD64, Windows GUI, "
        f"{report['pe']['icon_resources']} exact icon images"
    )
    changes = report.get("changes")
    if changes is not None and not changes["ok"]:
        print(format_manifest_diff(changes))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--executable",
        type=Path,
        default=DEFAULT_EXECUTABLE,
        help=f"frozen executable to inspect (default: {DEFAULT_EXECUTABLE})",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help=f"committed inventory manifest (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--icon",
        type=Path,
        default=DEFAULT_ICON,
        help=f"canonical ICO to compare with PE resources (default: {DEFAULT_ICON})",
    )
    parser.add_argument(
        "--update-manifest",
        action="store_true",
        help="explicitly replace the manifest after all PE/icon checks pass",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="write a machine-readable result",
    )
    args = parser.parse_args(argv)

    try:
        report = verify_or_update_manifest(
            args.executable,
            args.manifest,
            args.icon,
            update_manifest=args.update_manifest,
        )
    except (FrozenArchiveVerificationError, OSError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        else:
            print(f"Frozen archive verification FAILED: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
