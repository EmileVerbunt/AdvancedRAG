# Anti-patterns and fixes

Use this to quickly diagnose common bad outputs.

## 1) Mid-word breaks

**Bad:** `Releva` / `nce`, `Custo` / `m`  
**Fix:** Use `word-break: keep-all; overflow-wrap: normal;` and reduce density with `.compact`.

## 2) Detached percent symbols

**Bad:** `92` on one line and `%` below  
**Fix:** Wrap numeric tokens with `.num` and enforce `white-space: nowrap`.

## 3) Unreadable tables in dark mode

**Bad:** light row backgrounds with low-contrast text  
**Fix:** Use dark-safe token pairing and readable foreground in header/body cells.

## 4) Overcrowded slides

**Bad:** too many cards/tables/paragraphs on one slide  
**Fix:** Keep one main message per slide; split content into multiple slides; use notes for details.

## 5) Forced table layout causing poor wraps

**Bad:** narrow fixed columns breaking labels badly  
**Fix:** Prefer `table-layout: auto` for text-heavy tables; reserve fixed layout for short metric tables.

## 6) Vertical misalignment in KPI rows

**Bad:** values and labels appear uneven across tiles  
**Fix:** Use consistent container height + centered flex layout for each KPI tile.

## 7) Styling without reuse

**Bad:** ad hoc inline styles for every block  
**Fix:** Reuse class patterns (`card`, `feature-list`, `data-table`, `kpi-row`, `pill`, `num`).
