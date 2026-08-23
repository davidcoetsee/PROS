# Contributing to PROS

Bug reports, reproducible test cases, documentation improvements, and focused
code changes are welcome through the GitHub issue and pull-request workflow.

Before opening a pull request:

1. Keep PDF processing local and preserve source files unchanged.
2. Add or update tests for behavioral changes.
3. Run `.\.venv\Scripts\python.exe -m pytest -q` and
   `.\.venv\Scripts\python.exe -m ruff check .` on Windows.
4. Do not add telemetry, network processing, Qt, or an installer without prior
   project discussion.
5. Do not commit PDFs containing private or confidential information.

Unless explicitly stated otherwise, contributions intentionally submitted for
inclusion in PROS are provided under MPL-2.0. Contributors must have the right
to submit their work. Project branding remains governed by
[TRADEMARKS.md](TRADEMARKS.md).
