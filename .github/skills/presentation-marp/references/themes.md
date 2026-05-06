# Theme presets

Use these presets unless the user provides branding colors.

## Default preset: dark-blue

- background: `#0b1f3a`
- foreground: `#f8fafc`
- muted text: `#cbd5e1`
- accent primary: `#38bdf8`
- accent secondary: `#a78bfa`
- success: `#22c55e`
- warning: `#f59e0b`

## Alternate preset: deep-ocean

- background: `#0a192f`
- foreground: `#e2e8f0`
- muted text: `#94a3b8`
- accent primary: `#22d3ee`
- accent secondary: `#f472b6`

## Alternate preset: graphite-indigo

- background: `#111827`
- foreground: `#f9fafb`
- muted text: `#9ca3af`
- accent primary: `#60a5fa`
- accent secondary: `#818cf8`

## Brand override mapping

If users provide brand colors, map to:

- `brand_primary` -> heading/accent primary
- `brand_secondary` -> accent secondary
- `brand_background` -> slide background
- `brand_text` -> body text

Keep WCAG-friendly contrast for readability.

## Shadcn token bridge for MARP

Use shadcn-style token names in the CSS block so palettes stay consistent with UI branding language:

- `--background` -> slide background
- `--foreground` -> default text
- `--primary` / `--primary-foreground` -> headings and primary callouts
- `--accent` / `--accent-foreground` -> emphasis badges and highlights
- `--muted` / `--muted-foreground` -> secondary copy
- `--chart-1` ... `--chart-5` -> chart series colors

Example:

```css
:root {
  --background: #0b1f3a;
  --foreground: #f8fafc;
  --primary: #38bdf8;
  --primary-foreground: #081225;
  --accent: #a78bfa;
  --accent-foreground: #0b1020;
  --muted: #173356;
  --muted-foreground: #cbd5e1;
  --chart-1: #38bdf8;
  --chart-2: #22d3ee;
  --chart-3: #a78bfa;
  --chart-4: #22c55e;
  --chart-5: #f59e0b;
}
```

