# Fancy markdown patterns (MARP-safe)

Use these patterns to make slides feel premium without JavaScript.

## Transformation matrix (markdown -> styled component)

| Plain markdown input | Transform to | Class pattern | Notes |
|---|---|---|---|
| `- bullet list` | `<ul><li>...</li></ul>` | `feature-list` | Replace default bullets with accent-card list items. |
| Standard markdown table | `<table><thead>...` | `data-table` | Add zebra rows, strong header, trend pills. |
| `> quote / callout text` | `<div>...</div>` | `card` or `badge` | Promote key statement into callout block. |
| `## KPI: value` rows | `<div><span>...</span></div>` | `kpi-row`, `kpi` | Use for compact metric strip. |
| Comparison values | `<span>...</span>` | `pill up/down` | Inline trend badges for delta context. |
| Region/value list | `<div><div><i style="width:X%"></i>` | `bars`, `bar` | JS-free micro bar chart alternative. |
| Image + text section | `![bg left/right:%](...)` + heading/body | n/a (directive-driven) | Use split backgrounds for premium hero slides. |

## Layout safety and readability guardrails

- Keep to 2 cards per row and 3 KPI cells per row unless using a `compact` variant.
- Prefer short labels in headers/cells; move long explanations to speaker notes.
- Avoid overflow by using responsive CSS classes (`minmax(0, 1fr)`, `max-width:100%`, `overflow:hidden`).
- Prevent mid-word splitting with `word-break: keep-all` + `overflow-wrap: normal`.
- Keep numeric tokens (`92%`, `4.7/5`, trend chips) on one line via `.num` and `white-space: nowrap`.
- If content is still dense, apply `.compact` to reduce font-size before additional wrapping.

## 1) Card grid slide

```markdown
<!-- _class: cards -->

## Outcomes

<div class="grid grid-2">
  <div class="card">
    <p class="kicker">Speed</p>
    <h3>Release lead time</h3>
    <p class="value">-38%</p>
  </div>
  <div class="card">
    <p class="kicker">Quality</p>
    <h3>Change failure rate</h3>
    <p class="value">-21%</p>
  </div>
</div>
```

## 2) Badge + KPI strip

```markdown
<div class="badge">Q2 Highlights</div>
<div class="kpi-row">
  <div><span class="kpi num">92%</span><br/>Adoption</div>
  <div><span class="kpi num">14d</span><br/>Time to value</div>
  <div><span class="kpi num">4.7/5</span><br/>Satisfaction</div>
</div>
```

## 3) Split hero with background image

```markdown
![bg right:42%](https://images.unsplash.com/photo-1451187580459-43490279c0fa?auto=format&fit=crop&w=1200&q=80)

# Product Vision
## AI-native workflow for every engineer

- Faster onboarding
- Fewer regressions
- Measurable outcomes
```

## 4) Chart-ready placeholder (when data is incomplete)

```markdown
## Regional adoption trend

> Recommended chart: **Line chart** (monthly active teams by region)

| Month | EMEA | AMER | APAC |
|------:|-----:|-----:|-----:|
| Jan   | TBD  | TBD  | TBD  |
| Feb   | TBD  | TBD  | TBD  |
| Mar   | TBD  | TBD  | TBD  |
```

## 5) Shadcn-style feature list (replacement for plain bullets)

```markdown
## Key capabilities

<ul class="feature-list">
  <li><strong>Faster onboarding</strong><span> - prebuilt workflows and templates</span></li>
  <li><strong>Higher quality</strong><span> - regression gates and guardrails</span></li>
  <li><strong>Lower risk</strong><span> - measurable rollout with checkpoints</span></li>
</ul>
```

## 6) Fancy table (replacement for plain markdown table)

```markdown
## Delivery metrics (Q2)

<table class="data-table">
  <thead>
    <tr><th>Metric</th><th>Before</th><th>After</th><th>Trend</th></tr>
  </thead>
  <tbody>
    <tr><td>Lead time</td><td class="num">12d</td><td class="num">7d</td><td><span class="pill up num">▼ 42%</span></td></tr>
    <tr><td>Failure rate</td><td class="num">18%</td><td class="num">11%</td><td><span class="pill up num">▼ 39%</span></td></tr>
    <tr><td>MTTR</td><td class="num">9h</td><td class="num">5h</td><td><span class="pill up num">▼ 44%</span></td></tr>
  </tbody>
</table>
```

## 7) JS-free graph-ish row (micro bars)

```markdown
## Adoption by region

<div class="bars">
  <div><span>EMEA</span><div class="bar"><i style="width:78%"></i></div><b class="num">78%</b></div>
  <div><span>AMER</span><div class="bar"><i style="width:64%"></i></div><b class="num">64%</b></div>
  <div><span>APAC</span><div class="bar"><i style="width:52%"></i></div><b class="num">52%</b></div>
</div>
```

## 8) Suggested CSS block

```css
section { background: var(--background); color: var(--foreground); line-height: 1.2; }
h1, h2, h3 { color: var(--primary); letter-spacing: .01em; text-wrap: balance; }
p, li, td, th, span { word-break: keep-all; overflow-wrap: normal; hyphens: none; }
.badge { display:inline-block; padding:.2rem .7rem; border-radius:999px; background:var(--accent); color:var(--accent-foreground); font-weight:700; }
.grid { display:grid; gap:1rem; align-items:stretch; }
.grid-2 { grid-template-columns:repeat(2, minmax(0, 1fr)); }
.card { max-width:100%; min-height:0; overflow:hidden; display:flex; flex-direction:column; justify-content:center; background: color-mix(in srgb, var(--muted) 68%, transparent); border:1px solid color-mix(in srgb, var(--primary) 40%, #ffffff22); border-radius:16px; padding:1rem; }
.kicker { color:var(--muted-foreground); text-transform:uppercase; font-size:.7rem; letter-spacing:.08em; margin:0; }
.value { color:var(--accent); font-size:clamp(1.35rem, 2.7vw, 2rem); font-weight:800; margin:.2rem 0 0; }
.kpi-row { display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:1rem; margin-top:.8rem; align-items:stretch; }
.kpi-row > div { min-height:74px; display:flex; flex-direction:column; justify-content:center; padding:.55rem .7rem; border-radius:12px; background:color-mix(in srgb, var(--muted) 66%, transparent); }
.kpi { font-size:clamp(1.25rem, 2.2vw, 1.85rem); font-weight:800; color:var(--chart-1); line-height:1.05; }
.num, .value, .kpi, .pill, .bars b { white-space:nowrap; font-variant-numeric: tabular-nums; }
.feature-list { list-style:none; padding:0; margin:.5rem 0 0; display:grid; gap:.6rem; }
.feature-list li { padding:.7rem .9rem; border-radius:12px; background:color-mix(in srgb, var(--muted) 72%, transparent); border-left:4px solid var(--accent); }
.feature-list li span { color:var(--muted-foreground); }
.data-table { width:100%; table-layout:auto; border-collapse:separate; border-spacing:0; font-size:clamp(.74rem, 1.28vw, .9rem); overflow:hidden; border-radius:12px; border:1px solid color-mix(in srgb, var(--primary) 26%, #ffffff22); background:color-mix(in srgb, var(--background) 92%, #000 8%); color:var(--foreground); }
.data-table th { text-align:left; padding:.58rem .75rem; background:color-mix(in srgb, var(--primary) 24%, var(--background) 76%); color:var(--foreground); vertical-align:middle; }
.data-table td { padding:.52rem .75rem; color:var(--foreground); border-top:1px solid color-mix(in srgb, var(--muted) 48%, #ffffff1a); vertical-align:middle; }
.data-table tbody tr:nth-child(even) { background:color-mix(in srgb, var(--background) 82%, var(--muted) 18%); }
.pill { display:inline-block; padding:.15rem .55rem; border-radius:999px; font-size:.75rem; font-weight:700; }
.pill.up { background:color-mix(in srgb, var(--chart-4) 30%, transparent); color:var(--chart-4); }
.bars { display:grid; gap:.7rem; margin-top:.7rem; }
.bars > div { display:grid; grid-template-columns:90px 1fr 50px; align-items:center; gap:.65rem; }
.bar { height:10px; border-radius:999px; background:color-mix(in srgb, var(--muted) 65%, transparent); overflow:hidden; }
.bar i { display:block; height:100%; border-radius:999px; background:linear-gradient(90deg, var(--chart-1), var(--accent)); }
.compact { font-size:.9em; }
.compact .data-table { font-size:clamp(.66rem, 1.12vw, .8rem); }
.compact .kpi, .compact .value { font-size:clamp(1.05rem, 1.7vw, 1.45rem); }
.compact .feature-list li { padding:.55rem .75rem; }
.wrap-token { overflow-wrap:anywhere; word-break:break-word; }
```

## Notes

- Keep text short even in styled blocks.
- Prefer one standout visual motif per slide (not all motifs at once).
- No `<script>` tags or JS-driven widgets.
- Use JS-free chart alternatives unless you can embed a static SVG/PNG asset.
