"""Executable entry point for PROS."""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path


def _self_test_argument(argv: list[str]) -> Path | None:
    if "--self-test" not in argv:
        return None
    index = argv.index("--self-test")
    if index + 1 >= len(argv):
        raise SystemExit("--self-test requires an output directory")
    return Path(argv[index + 1]).expanduser().resolve()


def main() -> int:
    self_test_dir = _self_test_argument(sys.argv[1:])
    if self_test_dir is not None:
        from pros.selftest import run_self_test

        return run_self_test(self_test_dir)

    from pros.gui import run_app

    return run_app()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
