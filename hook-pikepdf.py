# SPDX-License-Identifier: MPL-2.0
"""Collect DLLs installed beside pikepdf by delvewheel on Windows."""

from PyInstaller.utils.hooks import collect_delvewheel_libs_directory

# Contemporary pikepdf Windows wheels keep qpdf and its dependent DLLs in the
# sibling ``pikepdf.libs`` directory. Binary dependency scanning often finds
# them, but explicitly collecting the delvewheel directory makes the one-file
# build robust across Python installers and wheel layout changes.
datas, binaries = collect_delvewheel_libs_directory("pikepdf")
