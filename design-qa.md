# PROS v1.5 Design QA

## Comparison target

- Annotated source screenshots: `tmp/pdfs/pros-v1.5-revision/embedded-001.png`, `embedded-002.png`, and `embedded-003.png`.
- Final implementation captures: `tmp/pros-v15-qa/v15-keep-implementation-150pct.png` and `v15-split-implementation-150pct.png`.
- Focused Review/Process captures: `tmp/pros-v15-qa/v15-keep-review-process-150pct.png` and `v15-split-review-process-150pct.png`.
- Same-input comparison composites: `tmp/pros-v15-qa/v15-keep-reference-composite.png` and `v15-split-reference-composite.png`.

## Viewport and state

- Native Windows Tk application captured at 150% display scaling and a 1920 x 1008 desktop viewport.
- Keep Separate: two ordered PDFs, compression and grayscale enabled, derived multi-output names visible.
- Split: one six-page PDF, five split points, calculated ranges, six output names, and an enabled six-PDF action.
- Focused captures scroll the workflow while keeping the enlarged brand/privacy header and bottom action bar fixed.

## Mandatory comparison pass

- Typography: Segoe UI hierarchy is consistent; headings, labels, table text, button text, and long review values remain legible without clipping.
- Spacing and layout: the two main columns remain stable; Review is the left half and Process the right half of a full-width shared row; formerly unused lower-left space is used deliberately.
- Viewport resilience: the 150%-scaled Keep and Split states retain the two-column hierarchy, fixed header/action bar, usable scrollbars, and unclipped Split controls. Narrow-window horizontal scrolling and focus reveal are covered by regression tests.
- Colors and states: labels and display surfaces are white; completed steps are blue; incomplete or blocked steps and their card borders are red; enabled Process matches the selected blue mode segment.
- Image and icon fidelity: the supplied logo and icon assets are rendered directly and preserve their aspect ratios. Their bytes were not edited during v1.5.
- Copy: privacy text is in the fixed header; Keep Separate, Join, Split, output naming, Review, Process, and error copy remain coherent at the captured density.
- Controls and icons: mode segments have contiguous shared edges; secondary actions share the same solid segmented family; Add, Remove, and Clear controls sit at the bottom-right of their lists; Remove controls include a leading minus sign.
- Interaction states: all-mode drop targets, picker fallback, four-second repeat-click shortcut, mode-specific input limits, step completion, fixed divider, disabled/enabled actions, progress, success, error, and keyboard traversal are covered by automated regressions.
- Accessibility: controls remain keyboard reachable; read-only logs are focusable and scrollable; visible labels and state colors have text equivalents; no required control is represented by color alone.
- AI shortcut artifacts: none. Existing raster/vector brand assets are used; no placeholder art, improvised SVG, or CSS-style drawing replaces them.

## Comparison history

1. Initial Keep/Split captures exposed Split-control clipping at Windows 150%; sizing was corrected and recaptured.
2. Independent review found vertically stacked Review/Process cards, mismatched button treatments, segment gaps, and native grey display surfaces.
3. Review/Process was rebuilt as a full-width left/right row, controls were unified to one segmented family, mode gaps were removed, and Treeview/output fields were given explicit white solid-border styling.
4. Final full-view and focused captures were placed beside the annotated references and inspected together. No P0, P1, or P2 visual finding remains.

## Verification

- GUI regression suite: 41 passed.
- Full source suite: 124 passed, 1 skipped.
- Ruff, compileall, and `git diff --check`: passed.
- Native Computer Use window enumeration remained unavailable after the prescribed recovery attempts; the final visual evidence instead uses direct native Tk window captures plus deterministic interaction tests.

final result: passed
