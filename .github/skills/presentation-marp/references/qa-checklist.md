# QA checklist (required)

Use this checklist before finalizing slide markdown.

## 1) Overflow and bounds

- [ ] No card, table, badge, or KPI block extends outside slide boundaries.
- [ ] No clipped text at right/bottom edges.
- [ ] Grid uses safe widths (`minmax(0, 1fr)`), and dense slides use `.compact`.

## 2) Wrapping quality

- [ ] No mid-word breaks (e.g., `Qualit` / `y`).
- [ ] Numeric tokens remain attached (`92%`, `4.7/5`, `▼ 42%`) via `.num` and `nowrap`.
- [ ] Long prose is shortened or moved to notes before forcing wraps.

## 3) Contrast and readability

- [ ] Table header/body text is readable in dark mode.
- [ ] Brand overrides still preserve readable foreground/background contrast.
- [ ] Accent colors are visible but not overpowering.

## 4) Vertical alignment and spacing

- [ ] KPI cells align vertically and feel balanced.
- [ ] Table cells are vertically centered (`vertical-align: middle`).
- [ ] Spacing is consistent between related blocks.

## 5) Density and fallback behavior

- [ ] If slide feels dense, apply compact mode first.
- [ ] If still dense, simplify copy (keyword-first) before reducing size further.
- [ ] Prefer splitting a crowded slide over forcing unreadable content.

## Required verification loop

1. Draft output.
2. Checklist pass.
3. Fix identified issues.
4. Re-check affected slides.
5. Finalize only after no critical failures remain.
