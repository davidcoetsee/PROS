# SPDX-License-Identifier: MPL-2.0
"""Fail-closed verification of the pinned PROS Windows release toolchain."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import platform
import re
import ssl
import struct
import subprocess
import sys
import zlib
from importlib import metadata
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENT_FILES = ("requirements.txt", "requirements-dev.txt")
WINDOWS_LOCK_FILE = "requirements-windows-x64.lock"

EXPECTED_PYTHON = "3.13.15"
EXPECTED_OPENSSL = "3.0.21"
EXPECTED_ZLIB = "1.3.1"
EXPECTED_TCL_TK = "8.6.15"
EXPECTED_PIKEPDF = "10.11.0"
EXPECTED_QPDF = "12.3.2"
EXPECTED_LXML = "6.1.1"
EXPECTED_LIBXML2 = "2.11.9"
EXPECTED_LIBXSLT = "1.1.45"
EXPECTED_PILLOW = "12.3.0"
EXPECTED_PILLOW_NATIVE = {
    "freetype2": "2.14.3",
    "littlecms2": "2.19",
    "webp": "1.6.0",
    "avif": "1.4.2",
    "libjpeg_turbo": "3.1.4.1",
    "zlib_ng": "2.3.3",
    "jpg_2000": "2.5.4",
    "libtiff": "4.7.1",
}
EXPECTED_WHEEL_FILENAMES = {
    "altgraph": "altgraph-0.17.5-py2.py3-none-any.whl",
    "colorama": "colorama-0.4.6-py2.py3-none-any.whl",
    "coverage": "coverage-7.15.4-cp313-cp313-win_amd64.whl",
    "iniconfig": "iniconfig-2.3.0-py3-none-any.whl",
    "lxml": "lxml-6.1.1-cp313-cp313-win_amd64.whl",
    "packaging": "packaging-26.3-py3-none-any.whl",
    "pefile": "pefile-2024.8.26-py3-none-any.whl",
    "pikepdf": "pikepdf-10.11.0-cp313-cp313-win_amd64.whl",
    "pillow": "pillow-12.3.0-cp313-cp313-win_amd64.whl",
    "pip": "pip-26.2.1-py3-none-any.whl",
    "pip-licenses": "pip_licenses-5.5.5-py3-none-any.whl",
    "pluggy": "pluggy-1.6.0-py3-none-any.whl",
    "prettytable": "prettytable-3.18.0-py3-none-any.whl",
    "psutil": "psutil-7.2.2-cp37-abi3-win_amd64.whl",
    "pygments": "pygments-2.21.0-py3-none-any.whl",
    "pyinstaller": "pyinstaller-6.22.1-py3-none-win_amd64.whl",
    "pyinstaller-hooks-contrib": ("pyinstaller_hooks_contrib-2026.6-py3-none-any.whl"),
    "pytest": "pytest-9.1.1-py3-none-any.whl",
    "pytest-cov": "pytest_cov-7.1.0-py3-none-any.whl",
    "pywin32-ctypes": "pywin32_ctypes-0.2.3-py3-none-any.whl",
    "ruff": "ruff-0.16.3-py3-none-win_amd64.whl",
    "setuptools": "setuptools-84.0.0-py3-none-any.whl",
    "tkinterdnd2": "tkinterdnd2-0.6.2-py3-none-any.whl",
    "wcwidth": "wcwidth-0.8.2-py3-none-any.whl",
}

_EXACT_PIN = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)$")
_WINDOWS_LOCK_ENTRY = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s;]+)"
    r"\s+--hash=sha256:(?P<sha256>[0-9a-f]{64})"
    r"\s+#\s+(?P<wheel>[^\s#]+\.whl)$"
)


def _normalize_distribution_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _strip_requirement_comment(line: str) -> str:
    return line.split(" #", 1)[0].strip()


def _requirement_include(line: str) -> str | None:
    for prefix in ("-r ", "--requirement ", "--requirement="):
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            if not value:
                raise ValueError(f"Missing path after {prefix.strip()!r}")
            return value
    return None


def _parse_requirement_file(
    path: Path,
    *,
    pins: dict[str, dict[str, Any]],
    visited: set[Path],
) -> None:
    resolved = path.resolve()
    if resolved in visited:
        return
    if not resolved.is_file():
        raise ValueError(f"Requirement file does not exist: {resolved}")
    visited.add(resolved)

    for line_number, raw_line in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = _strip_requirement_comment(raw_line)
        if not line or line.startswith("#"):
            continue
        include = _requirement_include(line)
        if include is not None:
            _parse_requirement_file(
                resolved.parent / include,
                pins=pins,
                visited=visited,
            )
            continue

        match = _EXACT_PIN.fullmatch(line)
        if match is None:
            raise ValueError(
                f"{resolved}:{line_number} is not an exact name==version pin: {line!r}"
            )
        display_name = match.group("name")
        version = match.group("version")
        canonical_name = _normalize_distribution_name(display_name)
        existing = pins.get(canonical_name)
        if existing is not None and existing["version"] != version:
            raise ValueError(
                f"Conflicting pins for {display_name}: "
                f"{existing['version']} and {version}"
            )
        if existing is None:
            pins[canonical_name] = {
                "name": display_name,
                "version": version,
                "sources": [str(resolved)],
            }
        elif str(resolved) not in existing["sources"]:
            existing["sources"].append(str(resolved))


def load_exact_pins(project_root: Path = PROJECT_ROOT) -> dict[str, dict[str, Any]]:
    """Load every direct exact pin, following local requirement includes."""

    pins: dict[str, dict[str, Any]] = {}
    visited: set[Path] = set()
    for filename in REQUIREMENT_FILES:
        _parse_requirement_file(
            Path(project_root) / filename,
            pins=pins,
            visited=visited,
        )
    if not pins:
        raise ValueError("No exact release dependency pins were found")
    return pins


def load_windows_lock(
    project_root: Path = PROJECT_ROOT,
    expected_pins: dict[str, dict[str, Any]] | None = None,
) -> dict[str, dict[str, str]]:
    """Validate the one-wheel-per-pin Windows x64 hash lock."""

    pins = load_exact_pins(project_root) if expected_pins is None else expected_pins
    lock_path = Path(project_root) / WINDOWS_LOCK_FILE
    if not lock_path.is_file():
        raise ValueError(f"Windows wheel lock does not exist: {lock_path}")

    entries: dict[str, dict[str, str]] = {}
    for line_number, raw_line in enumerate(
        lock_path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _WINDOWS_LOCK_ENTRY.fullmatch(line)
        if match is None:
            raise ValueError(
                f"{lock_path}:{line_number} must contain exactly one lowercase "
                "SHA256 hash and one wheel filename comment"
            )

        display_name = match.group("name")
        canonical_name = _normalize_distribution_name(display_name)
        if canonical_name in entries:
            raise ValueError(
                f"{lock_path}:{line_number} duplicates lock pin {display_name}"
            )

        wheel = match.group("wheel")
        expected_wheel = EXPECTED_WHEEL_FILENAMES.get(canonical_name)
        if expected_wheel is None:
            raise ValueError(
                f"{lock_path}:{line_number} has no approved wheel for {display_name}"
            )
        if wheel != expected_wheel:
            raise ValueError(
                f"{lock_path}:{line_number} wheel mismatch for {display_name}: "
                f"expected {expected_wheel}, found {wheel}"
            )

        entries[canonical_name] = {
            "name": display_name,
            "version": match.group("version"),
            "sha256": match.group("sha256"),
            "wheel": wheel,
        }

    expected_versions = {
        canonical_name: details["version"] for canonical_name, details in pins.items()
    }
    locked_versions = {
        canonical_name: details["version"]
        for canonical_name, details in entries.items()
    }
    if locked_versions != expected_versions:
        missing = sorted(set(expected_versions) - set(locked_versions))
        unexpected = sorted(set(locked_versions) - set(expected_versions))
        mismatched = {
            name: {
                "expected": expected_versions[name],
                "actual": locked_versions[name],
            }
            for name in sorted(set(expected_versions) & set(locked_versions))
            if expected_versions[name] != locked_versions[name]
        }
        raise ValueError(
            "Windows wheel lock does not exactly match requirement pins: "
            f"missing={missing}, unexpected={unexpected}, "
            f"version_mismatches={mismatched}"
        )
    return entries


def audit_installed_distribution_set(
    pins: dict[str, dict[str, Any]],
    distributions: Any = None,
) -> dict[str, Any]:
    """Require the environment to contain only and all approved distributions."""

    if distributions is None:
        distributions = metadata.distributions()

    installed: dict[str, str] = {}
    duplicate_names: list[str] = []
    invalid_names: list[str] = []
    for dist in distributions:
        display_name = str(dist.metadata.get("Name", "")).strip()
        if not display_name:
            invalid_names.append(repr(display_name))
            continue
        canonical_name = _normalize_distribution_name(display_name)
        if canonical_name in installed:
            duplicate_names.append(canonical_name)
            continue
        installed[canonical_name] = str(dist.version)

    expected_versions = {
        canonical_name: str(details["version"])
        for canonical_name, details in pins.items()
    }
    missing = sorted(set(expected_versions) - set(installed))
    unexpected = sorted(set(installed) - set(expected_versions))
    version_mismatches = {
        name: {"expected": expected_versions[name], "actual": installed[name]}
        for name in sorted(set(expected_versions) & set(installed))
        if expected_versions[name] != installed[name]
    }
    errors: list[str] = []
    if missing:
        errors.append(f"missing distributions: {', '.join(missing)}")
    if unexpected:
        errors.append(f"unexpected distributions: {', '.join(unexpected)}")
    if version_mismatches:
        errors.append(f"version mismatches: {version_mismatches}")
    if duplicate_names:
        errors.append(
            f"duplicate distributions: {', '.join(sorted(set(duplicate_names)))}"
        )
    if invalid_names:
        errors.append(f"distributions without names: {', '.join(invalid_names)}")

    return {
        "ok": not errors,
        "expected": dict(sorted(expected_versions.items())),
        "installed": dict(sorted(installed.items())),
        "missing": missing,
        "unexpected": unexpected,
        "version_mismatches": version_mismatches,
        "duplicate_names": sorted(set(duplicate_names)),
        "invalid_names": invalid_names,
        "errors": errors,
    }


def _condition_check(
    name: str,
    *,
    ok: bool,
    expected: Any,
    actual: Any,
    detail: str = "",
) -> dict[str, Any]:
    check: dict[str, Any] = {
        "name": name,
        "ok": bool(ok),
        "expected": expected,
        "actual": actual,
    }
    if detail:
        check["detail"] = detail
    return check


def _exact_check(name: str, expected: Any, actual: Any) -> dict[str, Any]:
    return _condition_check(
        name,
        ok=actual == expected,
        expected=expected,
        actual=actual,
    )


def _error_check(name: str, expected: Any, exc: BaseException) -> dict[str, Any]:
    return _condition_check(
        name,
        ok=False,
        expected=expected,
        actual=None,
        detail=f"{type(exc).__name__}: {exc}",
    )


def _hash_file(path: Path, algorithm: str) -> tuple[str, int]:
    digest = hashlib.new(algorithm)
    size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    encoded = base64.urlsafe_b64encode(digest.digest()).rstrip(b"=").decode("ascii")
    return encoded, size


def audit_distribution_record(dist: metadata.Distribution) -> dict[str, Any]:
    """Validate every hash and size supplied by an installed wheel RECORD."""

    errors: list[str] = []
    files = list(dist.files or ())
    if not files:
        errors.append("Distribution metadata exposes no RECORD entries")

    checked_hashes = 0
    unhashed_entries = 0
    for entry in files:
        entry_name = str(entry).replace("\\", "/")
        recorded_hash = entry.hash
        if recorded_hash is None:
            unhashed_entries += 1
            if not entry_name.endswith((".pyc", ".dist-info/RECORD")):
                errors.append(f"Unexpected unhashed RECORD entry: {entry_name}")
            continue

        checked_hashes += 1
        installed_path = Path(dist.locate_file(entry))
        if not installed_path.is_file():
            errors.append(f"Recorded file is missing: {entry_name}")
            continue
        try:
            actual_hash, actual_size = _hash_file(installed_path, recorded_hash.mode)
        except (OSError, ValueError) as exc:
            errors.append(f"Could not hash {entry_name}: {type(exc).__name__}: {exc}")
            continue
        if actual_hash != recorded_hash.value:
            errors.append(
                f"Hash mismatch for {entry_name}: "
                f"expected {recorded_hash.value}, found {actual_hash}"
            )
        if entry.size is not None and actual_size != entry.size:
            errors.append(
                f"Size mismatch for {entry_name}: "
                f"expected {entry.size}, found {actual_size}"
            )

    if checked_hashes == 0:
        errors.append("No hashed RECORD entries were available to validate")

    return {
        "ok": not errors,
        "distribution": dist.metadata.get("Name", ""),
        "version": dist.version,
        "record_entries": len(files),
        "hashed_files_checked": checked_hashes,
        "unhashed_entries": unhashed_entries,
        "errors": errors,
    }


def _probe_tcl_tk() -> dict[str, str]:
    code = (
        "import json\n"
        "import tkinter as tk\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "try:\n"
        "    result = {\n"
        "        'tcl': str(root.tk.call('info', 'patchlevel')),\n"
        "        'tk': str(root.tk.call('package', 'require', 'Tk')),\n"
        "    }\n"
        "    print(json.dumps(result, sort_keys=True))\n"
        "finally:\n"
        "    root.destroy()\n"
    )
    probe = subprocess.run(
        [sys.executable, "-c", code],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0:
        raise RuntimeError(probe.stderr.strip() or probe.stdout.strip())
    try:
        result = json.loads(probe.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Tk probe returned invalid JSON: {probe.stdout!r}") from exc
    return {"tcl": str(result["tcl"]), "tk": str(result["tk"])}


def _run_pip_check() -> tuple[bool, dict[str, Any]]:
    probe = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = {
        "returncode": probe.returncode,
        "stdout": probe.stdout.strip(),
        "stderr": probe.stderr.strip(),
    }
    return probe.returncode == 0, result


def verify_release_environment(
    project_root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    """Inspect the current interpreter and return a complete verification report."""

    checks: list[dict[str, Any]] = []
    record_audits: dict[str, dict[str, Any]] = {}

    checks.append(
        _exact_check(
            "python.implementation", "CPython", platform.python_implementation()
        )
    )
    checks.append(
        _exact_check("python.version", EXPECTED_PYTHON, platform.python_version())
    )
    checks.append(
        _exact_check("python.releaselevel", "final", sys.version_info.releaselevel)
    )
    openssl_parts = ssl.OPENSSL_VERSION.split()
    openssl_version = (
        openssl_parts[1] if len(openssl_parts) >= 2 else ssl.OPENSSL_VERSION
    )
    checks.append(_exact_check("openssl.runtime", EXPECTED_OPENSSL, openssl_version))
    checks.append(
        _exact_check("zlib.runtime", EXPECTED_ZLIB, zlib.ZLIB_RUNTIME_VERSION)
    )

    pointer_bits = struct.calcsize("P") * 8
    machine = platform.machine()
    system = platform.system()
    platform_actual = {
        "system": system,
        "machine": machine,
        "pointer_bits": pointer_bits,
    }
    checks.append(
        _condition_check(
            "platform.windows_x64",
            ok=(
                system == "Windows"
                and pointer_bits == 64
                and machine.lower() in {"amd64", "x86_64"}
            ),
            expected={
                "system": "Windows",
                "machine": ["AMD64", "x86_64"],
                "pointer_bits": 64,
            },
            actual=platform_actual,
        )
    )

    try:
        tcl_tk = _probe_tcl_tk()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        checks.append(
            _error_check(
                "tcl_tk.runtime",
                {"tcl": EXPECTED_TCL_TK, "tk": EXPECTED_TCL_TK},
                exc,
            )
        )
    else:
        checks.append(
            _exact_check(
                "tcl_tk.runtime",
                {"tcl": EXPECTED_TCL_TK, "tk": EXPECTED_TCL_TK},
                tcl_tk,
            )
        )

    wheel_lock: dict[str, dict[str, str]] = {}
    try:
        pins = load_exact_pins(Path(project_root))
    except (OSError, ValueError) as exc:
        pins = {}
        checks.append(_error_check("requirements.exact_pins", "all direct pins", exc))
    else:
        approved_names = set(EXPECTED_WHEEL_FILENAMES)
        checks.append(
            _condition_check(
                "requirements.exact_pins",
                ok=set(pins) == approved_names,
                expected={
                    "count": len(approved_names),
                    "names": sorted(approved_names),
                },
                actual={"count": len(pins), "names": sorted(pins)},
            )
        )

    if pins:
        try:
            wheel_lock = load_windows_lock(Path(project_root), pins)
        except (OSError, ValueError) as exc:
            checks.append(
                _error_check(
                    "requirements.windows_x64_lock",
                    "exactly one approved SHA256-pinned wheel per requirement",
                    exc,
                )
            )
        else:
            checks.append(
                _condition_check(
                    "requirements.windows_x64_lock",
                    ok=set(wheel_lock) == set(pins),
                    expected={"count": len(pins), "names": sorted(pins)},
                    actual={
                        "count": len(wheel_lock),
                        "names": sorted(wheel_lock),
                    },
                )
            )

        installed_set_audit = audit_installed_distribution_set(pins)
        checks.append(
            _condition_check(
                "packages.exact_set",
                ok=installed_set_audit["ok"],
                expected=installed_set_audit["expected"],
                actual=installed_set_audit["installed"],
                detail="; ".join(installed_set_audit["errors"]),
            )
        )
    else:
        installed_set_audit = {
            "ok": False,
            "expected": {},
            "installed": {},
            "errors": ["requirement pins could not be loaded"],
        }
        checks.append(
            _condition_check(
                "packages.exact_set",
                ok=False,
                expected="the approved requirement set",
                actual={},
                detail=installed_set_audit["errors"][0],
            )
        )

    for canonical_name, pin in pins.items():
        try:
            dist = metadata.distribution(pin["name"])
        except metadata.PackageNotFoundError as exc:
            checks.append(
                _error_check(
                    f"package.{canonical_name}",
                    pin["version"],
                    exc,
                )
            )
            continue

        checks.append(
            _exact_check(
                f"package.{canonical_name}",
                pin["version"],
                dist.version,
            )
        )
        audit = audit_distribution_record(dist)
        record_audits[canonical_name] = audit
        checks.append(
            _condition_check(
                f"record.{canonical_name}",
                ok=audit["ok"],
                expected="all available RECORD hashes and sizes match",
                actual={
                    "record_entries": audit["record_entries"],
                    "hashed_files_checked": audit["hashed_files_checked"],
                    "unhashed_entries": audit["unhashed_entries"],
                },
                detail="; ".join(audit["errors"]),
            )
        )

    try:
        pip_ok, pip_result = _run_pip_check()
    except (OSError, subprocess.SubprocessError) as exc:
        checks.append(_error_check("pip.check", "exit code 0", exc))
    else:
        checks.append(
            _condition_check(
                "pip.check",
                ok=pip_ok,
                expected={"returncode": 0},
                actual=pip_result,
            )
        )

    try:
        import pikepdf
    except Exception as exc:  # noqa: BLE001
        checks.append(_error_check("pikepdf.runtime", EXPECTED_PIKEPDF, exc))
        checks.append(_error_check("qpdf.runtime", EXPECTED_QPDF, exc))
    else:
        checks.append(
            _exact_check("pikepdf.runtime", EXPECTED_PIKEPDF, pikepdf.__version__)
        )
        checks.append(
            _exact_check("qpdf.runtime", EXPECTED_QPDF, pikepdf.__libqpdf_version__)
        )

    try:
        import lxml
        from lxml import etree
    except Exception as exc:  # noqa: BLE001
        checks.append(_error_check("lxml.runtime", EXPECTED_LXML, exc))
        checks.append(_error_check("libxml2.runtime", EXPECTED_LIBXML2, exc))
        checks.append(_error_check("libxslt.runtime", EXPECTED_LIBXSLT, exc))
    else:
        checks.append(_exact_check("lxml.runtime", EXPECTED_LXML, lxml.__version__))
        checks.append(
            _exact_check(
                "libxml2.runtime",
                EXPECTED_LIBXML2,
                ".".join(map(str, etree.LIBXML_VERSION)),
            )
        )
        checks.append(
            _exact_check(
                "libxslt.runtime",
                EXPECTED_LIBXSLT,
                ".".join(map(str, etree.LIBXSLT_VERSION)),
            )
        )

    try:
        import PIL
        from PIL import features
    except Exception as exc:  # noqa: BLE001
        checks.append(_error_check("pillow.runtime", EXPECTED_PILLOW, exc))
        for feature_name, expected_version in EXPECTED_PILLOW_NATIVE.items():
            checks.append(
                _error_check(f"pillow.native.{feature_name}", expected_version, exc)
            )
    else:
        checks.append(_exact_check("pillow.runtime", EXPECTED_PILLOW, PIL.__version__))
        for feature_name, expected_version in EXPECTED_PILLOW_NATIVE.items():
            supported = bool(features.check(feature_name))
            actual_version = features.version(feature_name) if supported else None
            checks.append(
                _condition_check(
                    f"pillow.native.{feature_name}",
                    ok=supported and actual_version == expected_version,
                    expected={"supported": True, "version": expected_version},
                    actual={"supported": supported, "version": actual_version},
                )
            )

    return {
        "schema_version": 1,
        "ok": all(check["ok"] for check in checks),
        "project_root": str(Path(project_root).resolve()),
        "executable": sys.executable,
        "checks": checks,
        "pins": pins,
        "wheel_lock": wheel_lock,
        "installed_distribution_set": installed_set_audit,
        "record_audits": record_audits,
    }


def _print_human_report(report: dict[str, Any]) -> None:
    for check in report["checks"]:
        status = "PASS" if check["ok"] else "FAIL"
        print(f"{status} {check['name']}: {check['actual']}")
        if not check["ok"]:
            print(f"  expected: {check['expected']}")
            if check.get("detail"):
                print(f"  detail: {check['detail']}")
    result = "PASSED" if report["ok"] else "FAILED"
    print(f"Release environment verification {result}.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        help="write the complete machine-readable report as JSON",
    )
    args = parser.parse_args(argv)
    report = verify_release_environment()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
