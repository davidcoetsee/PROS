# SPDX-License-Identifier: MPL-2.0
"""Compare PyInstaller's normalized missing-module warnings with an allowlist."""

from __future__ import annotations

import argparse
import re
from collections.abc import Iterable, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WARNING_FILE = PROJECT_ROOT / "build" / "pyinstaller" / "PROS" / "warn-PROS.txt"
DEFAULT_ALLOWLIST = PROJECT_ROOT / "packaging" / "pyinstaller-warning-allowlist.txt"

_WARNING_PATTERN = re.compile(
    r"^(?P<kind>missing|excluded) module named (?P<module>.+?) - imported by(?:\s|$)"
)
_ALLOWLIST_PATTERN = re.compile(
    r"^(?P<kind>missing|excluded) module named (?P<module>\S+)$"
)
_ALLOWLIST_HEADER = (
    "# SPDX-License-Identifier: MPL-2.0\n"
    "# Normalized PyInstaller missing/excluded module warnings.\n"
    "# Update only after reviewing the warning-set changes.\n"
)


def _normalize_module_name(value: str) -> str:
    module_name = value.strip()
    if (
        len(module_name) >= 2
        and module_name[0] in {"'", '"'}
        and module_name[-1] == module_name[0]
    ):
        module_name = module_name[1:-1].strip()
    if not module_name or any(character.isspace() for character in module_name):
        raise ValueError(f"Invalid PyInstaller module name: {value!r}")
    return module_name


def _format_entry(kind: str, module_name: str) -> str:
    return f"{kind} module named {module_name}"


def parse_warning_text(contents: str) -> set[str]:
    """Return stable warning identities, excluding importer paths and context."""

    entries: set[str] = set()
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        match = _WARNING_PATTERN.match(line)
        if match is None:
            continue
        try:
            module_name = _normalize_module_name(match.group("module"))
        except ValueError as exc:
            raise ValueError(f"Warning line {line_number}: {exc}") from exc
        entries.add(_format_entry(match.group("kind"), module_name))
    return entries


def parse_warning_file(path: Path) -> set[str]:
    if not path.is_file():
        raise FileNotFoundError(f"PyInstaller warning file does not exist: {path}")
    return parse_warning_text(path.read_text(encoding="utf-8-sig"))


def parse_allowlist_text(contents: str) -> set[str]:
    """Parse the canonical one-entry-per-line allowlist format."""

    entries: set[str] = set()
    for line_number, raw_line in enumerate(contents.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _ALLOWLIST_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(
                f"Allowlist line {line_number} is not a normalized warning entry: "
                f"{line!r}"
            )
        module_name = _normalize_module_name(match.group("module"))
        entry = _format_entry(match.group("kind"), module_name)
        if entry in entries:
            raise ValueError(f"Allowlist line {line_number} duplicates {entry!r}")
        entries.add(entry)
    return entries


def load_allowlist(path: Path) -> set[str]:
    if not path.is_file():
        raise FileNotFoundError(f"PyInstaller warning allowlist does not exist: {path}")
    return parse_allowlist_text(path.read_text(encoding="utf-8-sig"))


def write_allowlist(path: Path, entries: Iterable[str]) -> None:
    normalized_entries = sorted(set(entries))
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(normalized_entries)
    contents = _ALLOWLIST_HEADER + (f"\n{body}\n" if body else "")
    path.write_text(contents, encoding="utf-8", newline="\n")


def warning_diff(
    actual: set[str], allowed: set[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return newly added warnings and allowlisted warnings no longer emitted."""

    return tuple(sorted(actual - allowed)), tuple(sorted(allowed - actual))


def _print_diff(added: Sequence[str], removed: Sequence[str]) -> None:
    if added:
        print("Added warning entries:")
        for entry in added:
            print(f"  + {entry}")
    if removed:
        print("Removed warning entries:")
        for entry in removed:
            print(f"  - {entry}")
    if not added and not removed:
        print("No warning entry changes.")


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--warning-file",
        "--warnings",
        type=Path,
        default=DEFAULT_WARNING_FILE,
        help=f"PyInstaller warning report (default: {DEFAULT_WARNING_FILE})",
    )
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=DEFAULT_ALLOWLIST,
        help=f"normalized warning allowlist (default: {DEFAULT_ALLOWLIST})",
    )
    parser.add_argument(
        "--update-allowlist",
        action="store_true",
        help="replace the allowlist with the current normalized warning set",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _argument_parser().parse_args(argv)
    warning_file = args.warning_file.resolve()
    allowlist_file = args.allowlist.resolve()

    try:
        actual = parse_warning_file(warning_file)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 1

    if args.update_allowlist:
        try:
            allowed = load_allowlist(allowlist_file) if allowlist_file.exists() else set()
            added, removed = warning_diff(actual, allowed)
            _print_diff(added, removed)
            write_allowlist(allowlist_file, actual)
        except (OSError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 1
        print(
            f"Updated PyInstaller warning allowlist at {allowlist_file} "
            f"({len(actual)} entries)."
        )
        return 0

    try:
        allowed = load_allowlist(allowlist_file)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        print("Review the warning report, then rerun with --update-allowlist.")
        return 1

    added, removed = warning_diff(actual, allowed)
    if added or removed:
        print("PyInstaller warning allowlist mismatch.")
        _print_diff(added, removed)
        print("Review the changes, then rerun with --update-allowlist if accepted.")
        return 1

    print(f"PyInstaller warning allowlist matches ({len(actual)} entries).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
