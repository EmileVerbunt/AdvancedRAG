---
name: presentation-marp
description: Generate presentation-ready MARP markdown for PowerPoint-style decks with keyword-first slides, dark-blue default styling, shadcn-inspired color themes, branding overrides, and graph-friendly slide structure. Trigger for any request about slides, decks, presentations, storyboard flow, talk tracks, speaker narrative, or converting notes/docs into presentation content, even when MARP is not explicitly mentioned.
---

# Presentation MARP Skill

Create clean, engaging slide decks as MARP markdown with strong visual hierarchy and minimal text.

## Quick reference

| Task | Section / Guide |
|---|---|
| Build storyline from notes | Workflow + Content rules |
| Apply dark-blue + brand theming | Visual and theme rules + `references/themes.md` |
| Upgrade plain markdown to styled components | Component mapping rules + transformation matrix in `references/fancy-markdown.md` |
| Fix wrapping, overflow, and contrast | `references/qa-checklist.md` + `references/anti-patterns.md` |
| Add flashy but safe UI patterns | `references/fancy-markdown.md` |
| Add stress-tested output patterns | `evals/evals.json` |

## Core outcomes

- Produce MARP-ready `.md` output.
- Favor keywords and short bullets over paragraphs.
- Use dark-blue dark mode by default.
- Support shadcn-inspired theme presets and optional brand color overrides.
- Add graph/chart-oriented slides when data exists (and provide a fallback structure when it does not).
- Make slides feel unique with MARP-safe HTML/CSS components (cards, badges, KPI blocks, split layouts).
- Upgrade plain markdown lists/tables into shadcn-like styled components through HTML/CSS wrappers.

## Workflow

1. Clarify presentation goal, audience, duration, and desired tone.
2. Build a slide storyline (opening, problem, approach, evidence, conclusion, CTA).
3. Write concise slides with keyword-first content.
4. Apply theme preset and optional brand overrides.
5. Insert graph-friendly slides for metrics, trends, or comparisons.
6. Run a quality pass for readability, contrast, and visual interest.
7. Use `references/fancy-markdown.md` patterns for "flashy" but readable visual composition.
8. Run the required QA loop before final output.

## Content rules

- Keep each slide focused on one message.
- Target up to 3-5 bullets per slide.
- Keep bullets short (keyword phrases, not full paragraphs).
- Prefer action-oriented headings and strong nouns/verbs.
- Use speaker notes for detail instead of adding body text overload.

## Visual and theme rules

- Default style is dark-blue background with high-contrast text.
- Add accent colors for emphasis (titles, key numbers, callouts, chart highlights).
- If user provides brand colors, map them onto title/accent/link/highlight tokens.
- If user does not provide brand colors, use the default preset from `references/themes.md`.
- Prefer shadcn-style token naming (`--background`, `--foreground`, `--primary`, `--accent`) and map into MARP CSS variables.

Read `references/themes.md` when selecting or applying a palette.
Read `references/fancy-markdown.md` for reusable component blocks.
Read `references/qa-checklist.md` for pass/fail visual QA checks.
Read `references/anti-patterns.md` for known failure modes and fixes.
Use the transformation matrix in `references/fancy-markdown.md` to upgrade plain markdown into styled components.

## QA loop (required)

1. Generate draft slides.
2. Run checklist from `references/qa-checklist.md`.
3. List at least one issue category checked (Overflow, Contrast, Wrapping, Vertical Alignment, Density), even if passed.
4. Apply fixes where needed.
5. Re-check affected slides before finalizing.

## Rendering constraints (important)

- Do **not** inject JavaScript (`<script>`, inline event handlers, JS URLs). Keep output portable and renderer-safe.
- Build visual richness using only Markdown + allowed HTML + CSS (`style` directive or `<style>` block).
- Prefer stable MARP directives (`class`, `backgroundColor`, `backgroundImage`, split backgrounds) over runtime scripting.
- Note: Marp core has a helper script mode for rendering internals, but this is **not** a generic JS runtime for app components or chart libraries.

## Component mapping rules (shadcn-inspired, MARP-safe)

- Bulleted list -> render as a styled `<ul class="feature-list">` with icon/accent bullets.
- Plain table -> render as a styled `<table class="data-table">` with zebra rows and emphasis cells.
- Callout text -> render as `<div class="card">` or `<div class="badge">` blocks.
- Metrics row -> render as `<div class="kpi-row">` with token-based accents.
- Use reusable class names and CSS variables rather than ad hoc inline styles.
- If a slide gets dense, apply a `compact` class pattern to reduce font size before forcing hard wraps.
- Wrap-sensitive tokens (numbers + `%`, KPI values, trend chips) -> apply `.num` and keep `white-space: nowrap`.

## Graph guidance

- When numeric or categorical data is available, include at least one graph-oriented slide.
- Prefer:
  - trend/time -> line chart
  - category comparison -> bar chart
  - part-to-whole -> pie/donut
  - process distribution -> stacked bar
- If exact data points are missing, create a "chart-ready" placeholder slide with:
  - chart type recommendation
  - required fields
  - concise table scaffold for later data fill-in
- Do not rely on JS chart libraries in-slide; prefer:
  - styled tables with trend indicators,
  - CSS-only micro bars/progress visuals,
  - static SVG/PNG chart assets when available.

## Output format

Always output MARP markdown with frontmatter and style block:

```markdown
---
marp: true
theme: default
paginate: true
---

<style>
:root {
  --bg: #0b1f3a;
  --fg: #f8fafc;
  --muted: #cbd5e1;
  --accent: #38bdf8;
  --accent-2: #a78bfa;
}
section {
  background: var(--bg);
  color: var(--fg);
}
h1, h2, h3 { color: var(--accent); }
strong, em { color: var(--accent-2); }
</style>
```

Then render slides separated by `---`.

## Optional MCP usage (when tools are available)

- Use `marp-mcp-set_frontmatter` to standardize MARP frontmatter.
- Use `marp-mcp-generate_slide_ids` for stable slide IDs.
- Use `marp-mcp-manage_slide` for structured insert/replace operations.
- Use `shadcn` MCP to align palette decisions with shadcn-style color tokens when generating theme overrides.
- If Context7 docs are available, ground advanced MARP/shadcn syntax decisions before generating complex slide styling.

## Quality checklist

- Is each slide keyword-first and scannable?
- Is dark-blue theme applied by default?
- Are accent colors used with restraint for emphasis?
- Is at least one graph-oriented slide included when data context exists?
- Are branding overrides applied consistently when supplied?
- Are cards/tables fully inside slide bounds (no clipping/overflow)?
- Is table contrast readable in dark mode and with brand overrides?
- Does wrapping avoid splitting words awkwardly, with compact mode used when needed?
- Are `%` symbols attached to their numeric values (no detached line-wrap)?

