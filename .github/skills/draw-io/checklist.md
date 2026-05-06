# draw.io Diagram Checklist

Use this checklist to validate draw.io diagrams before committing.

## Pre-Commit Validation

### 1. Azure Architecture (If Applicable)

- [ ] **Azure icons**: Using `img/lib/azure2/` icon library (not colored rectangles)
- [ ] **Layering**: Left-to-right flow (Users → Edge → Compute → Data → External)
- [ ] **Trust boundaries**: Resource group or VNet boundaries shown with dashed containers
- [ ] **Container patterns**: Gray fill (`#E6E6E6`) for logical tiers, white for service plans
- [ ] **Service naming**: Role first, then service (e.g., "API Gateway (APIM)")
- [ ] **Flow numbering**: Arrows labeled 1→N with intent, not protocol
- [ ] **Edge patterns**: Solid blue for data, dashed for monitoring/auth
- [ ] **Private endpoints**: Shown for PaaS services in enterprise diagrams

### 1b. Icon Usage

- [ ] Icons use `aspect=fixed;` to maintain proportions
- [ ] Labels positioned with `verticalLabelPosition=top/bottom`
- [ ] All icons have `labelBackgroundColor=none;`
- [ ] Icon paths are correct (e.g., `img/lib/azure2/compute/App_Services.svg`)

### 2. WAF (Well-Architected Framework) Alignment

Before exporting, confirm your diagram shows **at least 2-3 WAF elements**:

**Security**
- [ ] Trust boundaries visible (VNet, private endpoints, NSG)
- [ ] Identity flow shown (Entra ID → Managed Identity → services)
- [ ] Data isolation boundaries clear

**Reliability**
- [ ] Failover paths shown (if applicable)
- [ ] Multi-region/zone indicators (if applicable)
- [ ] Health probes/monitoring connections visible

**Performance**
- [ ] Caching layers shown (Redis, CDN)
- [ ] Async patterns indicated (queues, events)
- [ ] Autoscaling hints (if applicable)

**Cost Optimization**
- [ ] Reserved vs on-demand annotations (for exec-level diagrams)
- [ ] Shared services highlighted

**Operational Excellence**
- [ ] Monitoring connections shown (dotted lines to Azure Monitor/App Insights)
- [ ] Logging/alerting path visible

### 3. Font Settings

- [ ] `mxGraphModel` has `defaultFontFamily="Segoe UI"` (or appropriate font)
- [ ] ALL text elements have `fontFamily=Segoe UI;` in style
- [ ] Font size is 12px+ (14px recommended for body, 18px for headers)
- [ ] Multi-byte text uses appropriate font (Noto Sans JP, etc.) with sufficient width

### 3b. Grouping & Container Patterns

- [ ] Logical tiers use gray containers (`fillColor=#E6E6E6;strokeColor=none;`)
- [ ] Service groups (e.g., App Service Plan) use white containers with `connectable="0"` parent
- [ ] Resource group boundary is dashed with `fillColor=none;`
- [ ] Container labels use `verticalLabelPosition=top/bottom` with `spacingBottom=-35` trick
- [ ] Parent groups have `style="group;labelBackgroundColor=none;strokeColor=none;"`

### 4. Arrow Placement (Z-Order)

- [ ] All `edge="1"` elements (arrows) appear BEFORE `vertex="1"` elements (boxes) in XML
- [ ] Arrow labels are at least 20px away from arrow lines
- [ ] Explicit coordinates used for text element connections

### 4b. Edge Patterns

- [ ] Data flows use solid blue (`fillColor=#dae8fc;strokeColor=#6c8ebf;`)
- [ ] Monitoring/diagnostic flows use `dashed=1;` without fill color
- [ ] Authentication/external flows use `dashed=1;` with descriptive labels
- [ ] Bidirectional/cache flows use `endArrow=none;endFill=0;`
- [ ] All edges have `labelBackgroundColor=none;`
- [ ] Edge labels use `edgeStyle=orthogonalEdgeStyle;` for right-angle routing

### 4c. Edge Label Positioning (CRITICAL)

Edge labels default to sitting ON the line, which is unreadable. Always offset:

- [ ] **Labels have y offset**: `y="15"` (below) or `y="-20"` (above)
- [ ] **Geometry includes offset point**: `<mxPoint as="offset" />` inside `<mxGeometry>`
- [ ] **Labels don't overlap lines**: Verify visually after render
- [ ] **Consistent positioning**: All horizontal labels offset the same direction (above or below)
- [ ] **Labels near their subject**: Position labels close to the element/arrow they describe (not floating in empty space)

Example of correct edge label:
```xml
<mxCell id="flow1" value="1. Request" edge="1" ...>
  <mxGeometry relative="1" y="15" as="geometry">
    <mxPoint as="offset" />
  </mxGeometry>
</mxCell>
```

### 4d. Explicit Edge Endpoints (When Needed)

For precise arrow positioning (icons with inconsistent anchors):

- [ ] Use `mxPoint` with `as="sourcePoint"` and `as="targetPoint"` for control
- [ ] Verify arrows connect where intended (not just cell centers)

### 4e. Minimizing Edge Bends

Cleaner diagrams have fewer bends in edges:

- [ ] **Most edges have 0-1 bends** (straight or single turn)
- [ ] **No edges with 3+ bends** — if found, reposition components
- [ ] **Components aligned on grid**: Same y for horizontal rows, same x for vertical columns
- [ ] **No overlapping edge segments** — stagger by 10px if multiple edges follow similar paths
- [ ] **Horizontal flow alignment**: Source and target within 5px y-coordinate (e.g., y=118 and y=115 → virtually straight)
- [ ] **Vertical flow alignment**: Source and target within 5px x-coordinate
- [ ] **Flow direction is consistent**: Left→right or top→bottom throughout diagram

Layout tips:
- Use 150-200px horizontal spacing between components
- Align components to shared y-coordinate for straight horizontal edges
- If bend is needed, keep it to a single 90° turn
- Consider explicit `sourcePoint`/`targetPoint` to force cleaner paths

### 5. Text Element Sizing

- [ ] Box width calculated: (longest line × 8px) + 20px padding minimum
- [ ] Multi-line text uses `whiteSpace=wrap;` with fixed width
- [ ] No text overflows box boundaries
- [ ] No unintended line breaks in labels
- [ ] Multi-byte text width: 30-40px per character minimum

### 5b. Nested Container Spacing

- [ ] Minimum 15px padding between parent edge and child boxes
- [ ] Minimum 10px gap between sibling boxes
- [ ] 25px clearance from container top to first child (for title label)
- [ ] Containers don't touch or overlap

### 5c. Comparison Diagrams (Current vs Target)

- [ ] Two side-by-side containers of equal width
- [ ] Current state uses orange/red border (`#E65100`)
- [ ] Target state uses green border (`#2E7D32`)
- [ ] Clear title labels at top of each container
- [ ] Consistent vertical alignment between sides

### 5d. Annotations (Warnings, Checkmarks)

- [ ] Warnings use red annotation boxes (`fillColor=#FFEBEE;strokeColor=#D32F2F`)
- [ ] Benefits use green annotation boxes (`fillColor=#E8F5E9;strokeColor=#2E7D32`)
- [ ] Annotations are anchored near their related component (not floating)
- [ ] Text inside annotations is left-aligned with small font (11-12px)

### 5e. Title & Metadata

- [ ] Title is separate text element (not crammed with metadata)
- [ ] Subtitle (audience, date) in smaller gray text below title
- [ ] Title uses `fontSize=18-24;fontStyle=1;` (bold)
- [ ] Subtitle uses `fontSize=12;fontColor=#666666;`

### 6. Page Settings

- [ ] `page="0"` is set in `mxGraphModel` (for transparent background)
- [ ] Appropriate canvas size (`dx`, `dy`)

### 7. Element IDs

- [ ] All `mxCell` elements have unique `id` attributes
- [ ] IDs are descriptive (e.g., `apim-gateway`, `cosmos-db`, `vnet-boundary`)
- [ ] Root cells `id="0"` and `id="1"` are present

### 8. Visual Verification

- [ ] PNG export generated successfully
- [ ] All text is readable at intended display size
- [ ] Arrows do not overlap with labels
- [ ] Colors are consistent with Azure palette
- [ ] Layout is balanced and professional
- [ ] Trust boundaries are clearly distinguishable

---

## Audience-Specific Checks

### For Executive Diagrams
- [ ] 5-8 elements maximum
- [ ] Business flows emphasized over technical details
- [ ] Data residency/compliance zones visible
- [ ] Cost indicators present (if relevant)

### For Architect Diagrams
- [ ] 8-14 elements
- [ ] All 5 WAF pillars represented
- [ ] Service interactions clear
- [ ] Scaling hints included

### For Engineer Diagrams
- [ ] Exact service names/SKUs
- [ ] API contracts hinted
- [ ] Subnet/NSG visibility
- [ ] Connection details (ports, protocols if relevant)

---

## Automated Validation Script

Run the Python validator:

```bash
python tests/test_drawio_skill.py
```

Or use pytest:

```bash
pytest tests/test_drawio_skill.py -v
```

## PNG Export Verification

```bash
# macOS
drawio -x -f png -s 2 -t -o diagram.png diagram.drawio
open diagram.png

# Linux
drawio -x -f png -s 2 -t -o diagram.png diagram.drawio
xdg-open diagram.png

# Windows (PowerShell)
& "$env:ProgramFiles\draw.io\draw.io.exe" -x -f png -s 2 -t -o diagram.png diagram.drawio
Start-Process diagram.png
```

## Common Issues and Fixes

### Issue: Font not rendering correctly in PNG

**Cause**: `fontFamily` missing from individual elements

**Fix**: Add `fontFamily=FontName;` to every text element's style

```xml
<!-- Before -->
<mxCell style="text;html=1;fontSize=18;" />

<!-- After -->
<mxCell style="text;html=1;fontSize=18;fontFamily=Noto Sans JP;" />
```

### Issue: Arrow appears in front of boxes

**Cause**: Edge element declared after vertex elements

**Fix**: Move all edge elements to appear before vertex elements in XML

### Issue: Label overlaps with arrow

**Cause**: Label positioned too close to arrow line

**Fix**: Adjust label Y coordinate to be at least 20px away from arrow Y

```xml
<!-- Arrow at Y=200 -->
<mxPoint y="200" as="sourcePoint"/>

<!-- Label should be at Y=170 or less (above), or Y=230 or more (below) -->
<mxGeometry y="170" />
```

### Issue: Japanese text wraps unexpectedly

**Cause**: Geometry width too narrow for text content

**Fix**: Increase width (30-40px per Japanese character)

```xml
<!-- 6 Japanese characters = 180-240px minimum -->
<mxGeometry width="220" />
```

### Issue: Background not transparent in PNG

**Cause**: `page` attribute not set to `"0"`

**Fix**: Add `page="0"` to `mxGraphModel`

```xml
<mxGraphModel page="0" ... />
```

## Quick Reference: Style String Format

```
style="property1=value1;property2=value2;property3=value3;"
```

### Essential Properties

| Property | Required | Example |
|----------|----------|---------|
| `fontFamily` | Yes (text) | `fontFamily=Noto Sans JP;` |
| `fontSize` | Recommended | `fontSize=18;` |
| `html` | Yes (text) | `html=1;` |
| `whiteSpace` | Recommended | `whiteSpace=wrap;` |
| `rounded` | Optional | `rounded=1;` |
| `fillColor` | Optional | `fillColor=#dae8fc;` |
| `strokeColor` | Optional | `strokeColor=#6c8ebf;` |

## Pre-Commit Hook Integration

Add to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: local
    hooks:
      - id: validate-drawio
        name: Validate draw.io files
        entry: python tests/test_drawio_skill.py
        language: python
        files: \.drawio$
        additional_dependencies: [pytest]

      - id: convert-drawio-to-png
        name: Convert draw.io to PNG
        entry: bash scripts/convert-drawio-to-png.sh
        language: system
        files: \.drawio$
        pass_filenames: true
```

## Review Questions

Before submitting a diagram, ask yourself:

1. Can I read all text clearly at the intended display size?
2. Are the arrows clearly showing the flow direction?
3. Do labels clearly associate with their respective arrows/elements?
4. Is the color scheme consistent and accessible?
5. Does the PNG export look the same as the draw.io preview?
