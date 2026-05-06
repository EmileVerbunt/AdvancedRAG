---
name: draw-io
description: Generate polished draw.io diagrams for Azure architectures with WAF-aligned design, proper layering, trust boundaries, and professional styling. Use when asked for "draw.io", "architecture diagram", "Azure diagram", "solution diagram", "WAF", "landing zone", "private endpoint", "hub-spoke", or any visual diagram in .drawio format. Outputs XML with Azure color palette, proper z-ordering, and audience-aware structure.
---

# draw.io Diagram Generation Skill

## Overview

This skill enables generation of high-quality draw.io diagrams—especially **Azure architecture diagrams**—by directly editing XML. It applies:
- **Azure best practices**: Service naming, color palette, layering patterns
- **WAF (Well-Architected Framework) thinking**: Visual cues for security, reliability, performance, cost, and operations
- **Audience-aware design**: Execs see boundaries and risk; engineers see dataflows and services

## When to Choose draw.io vs Other Formats

| Format | Best For | Trade-offs |
|--------|----------|------------|
| **draw.io (XML)** | Polished diagrams with Azure icons; customer-ready exports; high control | More effort; XML to maintain |
| **Mermaid** | Quick embeds in Markdown; version-controllable; fast iteration | No Azure icons; limited styling |
| **Canvas (.canvas)** | Interactive Obsidian presentation; drag-to-tweak | Obsidian-only; harder to share |

**Use draw.io when**: Customer expects a professional diagram for slides/docs, or you need Azure icons and precise control.

## Quick Start

When creating a draw.io diagram:

1. Use **Azure icon library** (`img/lib/azure2/...`) for official Azure icons
2. Set `defaultFontFamily="Segoe UI"` in `mxGraphModel` (Azure-style)
3. Use **container patterns**: gray fill for logical groups, white for service plans
4. Place arrows (edges) BEFORE boxes (vertices) in XML for correct z-order
5. Use **solid blue edges** for data flow, **dashed edges** for monitoring/auth
6. Set `labelBackgroundColor=none;` on all elements for clean look
7. Set `page="0"` for transparent background
8. Verify with PNG export

---

## Azure Icon Library

draw.io includes official Azure icons at `img/lib/azure2/`. Always prefer these over colored rectangles for professional diagrams.

### Using Azure Icons

```xml
<!-- Azure icon with label below -->
<mxCell id="app-service" value="Web App"
  style="aspect=fixed;html=1;points=[];align=center;image;fontSize=12;image=img/lib/azure2/compute/App_Services.svg;labelPosition=center;verticalLabelPosition=bottom;verticalAlign=top;labelBackgroundColor=none;"
  vertex="1" parent="1">
  <mxGeometry x="100" y="100" width="64" height="64" as="geometry" />
</mxCell>
```

### Key Icon Paths

| Service | Icon Path | Notes |
|---------|-----------|-------|
| App Service | `img/lib/azure2/compute/App_Services.svg` | |
| Functions | `img/lib/azure2/compute/Function_Apps.svg` | |
| Container Apps / Instances | `img/lib/azure2/compute/Container_Instances.svg` | Use for both ACA and ACI |
| AKS | `img/lib/azure2/compute/Kubernetes_Services.svg` | |
| SQL Database | `img/lib/azure2/databases/SQL_Database.svg` | |
| Cosmos DB | `img/lib/azure2/databases/Azure_Cosmos_DB.svg` | |
| Redis Cache | `img/lib/azure2/databases/Cache_Redis.svg` | |
| Front Door | `img/lib/azure2/networking/Front_Doors.svg` | |
| APIM | `img/lib/azure2/app_services/API_Management_Services.svg` | |
| AI Search | `img/lib/azure2/app_services/Search_Services.svg` | |
| **AI Foundry** | `img/lib/azure2/ai_machine_learning/AI_Studio.svg` | Correct icon (not Machine_Learning) |
| Azure OpenAI | `img/lib/azure2/ai_machine_learning/Azure_OpenAI.svg` | Or use AI_Studio for Foundry models |
| Entra ID | `img/lib/azure2/identity/Azure_Active_Directory.svg` | |
| Azure Monitor | `img/lib/azure2/management_governance/Monitor.svg` | |
| Log Analytics | `img/lib/azure2/analytics/Log_Analytics_Workspaces.svg` | |
| App Insights | `img/lib/azure2/devops/Application_Insights.svg` | |
| Service Bus | `img/lib/azure2/integration/Service_Bus.svg` | |

See [reference.md](reference.md) for complete icon library paths.

**Full icon catalog**: [azure-icons.md](azure-icons.md) lists all 200+ Azure icons by category.

---

## Azure Architecture Principles

### Layering (Left-to-Right Flow)

Structure diagrams with clear horizontal layers:

```
Users/Clients → Edge/Gateway → Compute/App → Data → External/Dependencies
     ↓              ↓             ↓          ↓            ↓
  (left)     APIM/FrontDoor   App/AKS/ACA  Cosmos/SQL   3rd party
```

### Trust Boundaries (Always Show)

Every Azure diagram should show:
- **Internet / Untrusted**: External clients, public endpoints
- **Azure Subscription / VNet**: Your private network boundary
- **Data plane vs Control plane**: Where management vs runtime traffic flows
- **On-prem / Customer Network**: If hybrid connectivity exists

Use **dashed rectangles** or **grouped containers** for boundaries:

```xml
<!-- Trust boundary container -->
<mxCell id="vnet-boundary" value="Azure VNet (Private)" 
  style="rounded=0;whiteSpace=wrap;html=1;dashed=1;dashPattern=8 4;fillColor=none;strokeColor=#0078D4;strokeWidth=2;verticalAlign=top;fontFamily=Segoe UI;fontSize=14;fontStyle=1;"
  vertex="1" parent="1">
  <mxGeometry x="200" y="80" width="400" height="300" as="geometry" />
</mxCell>
```

### Non-Azure Entities (Generic Shapes)

**CRITICAL**: Don't use Azure icons for non-Azure things. Use generic shapes:

| Entity | Recommended Style | Why |
|--------|-------------------|-----|
| **On-Premises** | Gray rounded box (`fillColor=#E6E6E6;strokeColor=#666666;`) | No good "datacenter" icon in Azure library |
| **External Users** | Gray/white box or person icon (`shape=actor;`) | Not an Azure service |
| **3rd Party SaaS** | White box with border | Distinguish from Azure services |
| **Internet** | Cloud shape or labeled box | Use `shape=cloud;` if needed |

```xml
<!-- On-Premises (generic gray box) -->
<mxCell id="onprem" value="On-Premises"
  style="rounded=1;whiteSpace=wrap;html=1;fillColor=#E6E6E6;strokeColor=#666666;fontFamily=Segoe UI;fontSize=12;fontStyle=1;labelBackgroundColor=none;"
  vertex="1" parent="1">
  <mxGeometry x="40" y="145" width="100" height="50" as="geometry" />
</mxCell>

<!-- External Users -->
<mxCell id="users" value="Users"
  style="rounded=1;whiteSpace=wrap;html=1;fillColor=#F5F5F5;strokeColor=#666666;fontFamily=Segoe UI;fontSize=14;"
  vertex="1" parent="1">
  <mxGeometry x="40" y="160" width="100" height="60" as="geometry" />
</mxCell>
```

**Avoid**: `On_Premises_Data_Gateways.svg` — this is an Azure service icon (cloud-based gateway), not a datacenter!

### Azure Color Palette

| Component Type | Fill Color | Stroke Color | Use For |
|---------------|------------|--------------|---------|
| **Compute (blue)** | `#E8F4FD` | `#0078D4` | App Service, AKS, Container Apps, Functions, VMs |
| **Data (green)** | `#E6F4EA` | `#107C10` | Cosmos DB, SQL, Storage, Redis, PostgreSQL |
| **Integration (purple)** | `#F3E8FF` | `#8661C5` | Service Bus, Event Grid, Event Hubs, Logic Apps |
| **Network/Edge (teal)** | `#E0F7FA` | `#008272` | APIM, Front Door, App Gateway, Load Balancer, VNet |
| **Identity (orange)** | `#FFF4E5` | `#FF8C00` | Entra ID, Managed Identity, Key Vault |
| **AI/ML (gradient blue)** | `#E3F2FD` | `#1976D2` | Azure OpenAI, AI Foundry, Cognitive Services, ML |
| **Monitoring (yellow)** | `#FFF8E1` | `#FFC107` | Azure Monitor, App Insights, Log Analytics |
| **Security (red accent)** | `#FFEBEE` | `#D32F2F` | WAF, Defender, Private Endpoints, NSG |

### Service Naming Convention

Name boxes by **role first, then service**:
- ✅ "API Gateway (APIM)"
- ✅ "App Backend (Container Apps)"
- ✅ "Vector Store (AI Search)"
- ❌ "Azure API Management" (too generic)
- ❌ "apim-prod-westeu-001" (too specific)

### Flow Numbering

Number flows 1→N for readability. Label arrows with **intent**, not protocol:
- ✅ "1. User authenticates"
- ✅ "2. Query knowledge base"
- ✅ "3. Generate response"
- ❌ "HTTPS 443"
- ❌ "REST API call"

---

## Edge Patterns (Arrows)

Use consistent arrow styles to distinguish flow types:

### Data Flow (Solid Blue)
Primary request/response flow through the architecture:
```xml
style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;labelBackgroundColor=none;"
```

### Monitoring / Diagnostic Flow (Dashed)
Telemetry, logs, and metrics flowing to monitoring services:
```xml
style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;dashed=1;labelBackgroundColor=none;"
```

### Authentication / External Service Flow (Dashed with Label)
Calls to identity providers, DNS, external APIs:
```xml
style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;dashed=1;labelBackgroundColor=none;"
value="Authentication"
```

### Cache / Bidirectional Flow (No Arrow Head)
Data retrieval from cache, bidirectional connections:
```xml
style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;endArrow=none;endFill=0;fillColor=#dae8fc;strokeColor=#6c8ebf;labelBackgroundColor=none;"
```

### Edge Label Positioning

**CRITICAL**: Edge labels default to sitting ON TOP of the line, which is unreadable. Always offset labels above or below:

```xml
<!-- Label BELOW the line (positive y offset) -->
<mxCell id="flow1" value="1. Auth" edge="1" 
  style="edgeStyle=orthogonalEdgeStyle;rounded=0;...">
  <mxGeometry relative="1" x="-0.3" y="15" as="geometry">
    <mxPoint as="offset" />
  </mxGeometry>
</mxCell>

<!-- Label ABOVE the line (negative y offset) -->
<mxCell id="flow2" value="2. Request" edge="1" 
  style="edgeStyle=orthogonalEdgeStyle;rounded=0;...">
  <mxGeometry relative="1" y="-20" as="geometry">
    <mxPoint as="offset" />
  </mxGeometry>
</mxCell>
```

**Label offset guidelines:**
- `y="15"` to `y="20"` → label sits below the line
- `y="-15"` to `y="-21"` → label sits above the line  
- `x="-0.3"` → shifts label toward source (negative) or target (positive)
- **Always include** `<mxPoint as="offset" />` inside the geometry

### Explicit Edge Endpoints

For precise control over where arrows start/end (instead of auto-connecting to cell centers):

```xml
<mxCell id="flow4" value="4. Orchestrate" edge="1"
  style="edgeStyle=orthogonalEdgeStyle;rounded=0;...">
  <mxGeometry relative="1" y="16" as="geometry">
    <mxPoint as="offset" />
    <mxPoint x="468" y="196" as="sourcePoint" />
    <mxPoint x="560" y="196" as="targetPoint" />
  </mxGeometry>
</mxCell>
```

Use explicit sourcePoint/targetPoint when:
- Icons have inconsistent anchor points
- You need arrows to connect at specific spots (not center)
- Auto-routing creates awkward paths

### Minimizing Edge Bends

**Cleaner diagrams have fewer bends in edges.** Apply these principles:

#### 1. Align Components for Straight Lines
Position components so most connections are straight (0 bends) or have 1 bend max:
```
GOOD: Components aligned → straight arrow
┌──────┐         ┌──────┐
│  A   │────────▶│  B   │
└──────┘         └──────┘

BAD: Misaligned → multiple bends
┌──────┐
│  A   │──┐
└──────┘  │
          └──────▶┌──────┐
                  │  B   │
                  └──────┘
```

**Pro tip**: Match y-coordinates within 5px (e.g., source at y=118, target at y=115) → virtually straight horizontal arrow. Small differences are acceptable; large gaps create bends.

**For vertical connections** (exitX=0.5 → entryX=0.5 or entryY=0), align component **centers** exactly:
```
source_center_x = source.x + (source.width / 2)
target.x = source_center_x - (target.width / 2)
```
Example: Functions at x=400, width=68 → center=434. Monitor (width=64) should be at x=402 (402+32=434).

#### 2. Use Grid-Based Layout
- Place components on a consistent grid (e.g., x increments of 150-200px)
- Align vertically: all components in a row at same y-coordinate
- Align horizontally: all components in a column at same x-coordinate

#### 3. Edge Routing Style
Use `edgeStyle=orthogonalEdgeStyle` (default) but position components to minimize resulting bends:
- **Horizontal flow**: Place source and target at same y → 0 bends
- **Vertical flow**: Place source and target at same x → 0 bends
- **Diagonal relationship**: Accept 1-2 bends, not more

#### 4. When Bends Are Necessary
If a bend is unavoidable (e.g., routing around obstacles):
- Use **consistent bend points** (e.g., all horizontal segments at y=200)
- Avoid overlapping edges by staggering bend points (+10px offset each)
- Consider `curved=1` for softer appearance (but use sparingly)

```xml
<!-- Orthogonal edge with one clean bend -->
style="edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;"

<!-- Curved edge (softer, but can be harder to follow) -->
style="edgeStyle=orthogonalEdgeStyle;curved=1;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;"
```

#### 5. Layout Validation
Before finalizing:
- [ ] Most edges have 0-1 bends
- [ ] No edges with 3+ bends (redesign component positions)
- [ ] No overlapping edge segments
- [ ] Flow direction is clear (left→right or top→bottom)

---

## Container & Grouping Patterns

Enterprise diagrams use containers for logical structure:

### Resource Group Boundary (Outer Dashed)
Wraps entire diagram to show scope:
```xml
style="rounded=0;whiteSpace=wrap;html=1;fillColor=none;dashed=1;labelBackgroundColor=none;"
```

### Logical Tier Container (Gray Fill, No Stroke)
Groups related services (App Tier, Data Tier, Monitoring):
```xml
style="rounded=0;whiteSpace=wrap;html=1;dashed=1;labelBackgroundColor=none;fillColor=#E6E6E6;strokeColor=none;"
```

### Service Plan Group (White Container)
Groups service with its hosting plan (e.g., App Service in App Service Plan):
```xml
<!-- Parent group (connectable="0") -->
style="group;labelBackgroundColor=none;strokeColor=none;"

<!-- White inner container with label -->
style="rounded=0;whiteSpace=wrap;html=1;dashed=1;labelBackgroundColor=none;fillColor=#FFFFFF;labelPosition=center;verticalLabelPosition=top;align=center;verticalAlign=bottom;spacingBottom=-35;strokeColor=none;"
value="App Service Plan"
```

See [reference.md](reference.md) for complete grouping patterns with XML examples.

---

## Comparison Diagrams (Current vs Target)

When showing "before and after" or "current vs target" architectures:

### Layout Pattern
- Use **two side-by-side containers** of equal width
- Left = Current State (use orange/red border: `strokeColor=#E65100;`)
- Right = Target State (use green border: `strokeColor=#2E7D32;`)
- Add clear title labels at top of each container
- Maintain consistent vertical alignment between sides

### Color Psychology
| Color | Meaning | Stroke | Fill |
|-------|---------|--------|------|
| **Red/Orange** | Current state, problems, gaps | `#E65100` | `#FFF3E0` |
| **Green** | Target state, benefits, resolved | `#2E7D32` | `#E8F5E9` |
| **Gray** | Neutral, unchanged | `#666666` | `#F5F5F5` |
| **Blue** | Primary data flow | `#0078D4` | `#E8F4FD` |

### Annotations (Warnings, Checkmarks, Benefits)

**DON'T**: Float warning text loosely next to boxes
**DO**: Use styled annotation boxes or embed in the component

```xml
<!-- Warning annotation (red box, attached to component) -->
<mxCell id="warning1" value="⚠ No OCR&#xa;⚠ Images skipped"
  style="rounded=1;whiteSpace=wrap;html=1;fontFamily=Segoe UI;fontSize=11;fillColor=#FFEBEE;strokeColor=#D32F2F;fontColor=#C62828;align=left;spacingLeft=5;"
  vertex="1" parent="1">
  <mxGeometry x="200" y="150" width="100" height="40" as="geometry" />
</mxCell>

<!-- Benefit annotation (green box, attached to component) -->
<mxCell id="benefit1" value="✓ Full OCR + images&#xa;✓ Table extraction"
  style="rounded=1;whiteSpace=wrap;html=1;fontFamily=Segoe UI;fontSize=11;fillColor=#E8F5E9;strokeColor=#2E7D32;fontColor=#1B5E20;align=left;spacingLeft=5;"
  vertex="1" parent="1">
  <mxGeometry x="500" y="150" width="120" height="40" as="geometry" />
</mxCell>
```

---

## Text & Box Sizing Rules

Avoid text overflow and cramped layouts:

### Calculate Box Width
- **English text**: ~7-8px per character for `fontSize=12`
- **Minimum width**: 80px for any labeled box
- **Multi-line**: Use `whiteSpace=wrap;` and set a fixed width
- **Rule of thumb**: Width = (longest line chars × 8) + 20px padding

### Example: Fitting "Python Library (No OCR, No Images)"
- 35 characters × 8px = 280px + 20px padding = **~300px minimum width**
- Or split to 2 lines: "Python Library" + "(No OCR, No Images)" → width ~150px

### Nested Container Spacing
- **Minimum padding**: 15px between parent container edge and child boxes
- **Minimum gap**: 10px between sibling boxes
- **Label clearance**: 25px from top of container to first child (for title)

```xml
<!-- Container with proper padding -->
<mxCell id="container" value="Document Processing"
  style="rounded=0;whiteSpace=wrap;html=1;fillColor=#E8F5E9;strokeColor=#2E7D32;verticalAlign=top;fontFamily=Segoe UI;fontSize=14;fontStyle=1;spacingTop=5;"
  vertex="1" parent="1">
  <mxGeometry x="400" y="60" width="280" height="200" as="geometry" />
</mxCell>

<!-- First child 15px from edges, 25px from top (after label) -->
<mxCell id="child1" ...>
  <mxGeometry x="415" y="95" width="100" height="60" as="geometry" />
</mxCell>
```

### Title & Subtitle Pattern
Don't cram metadata into the title. Use a separate subtitle text element:

```xml
<!-- Main title -->
<mxCell id="title" value="CZ AI Platform: Current vs Target Architecture"
  style="text;html=1;align=center;fontFamily=Segoe UI;fontSize=20;fontStyle=1;labelBackgroundColor=none;"
  vertex="1" parent="1">
  <mxGeometry x="200" y="10" width="500" height="30" as="geometry" />
</mxCell>

<!-- Subtitle (audience, date) -->
<mxCell id="subtitle" value="Audience: D&amp;A Leadership | Jan 28, 2026"
  style="text;html=1;align=center;fontFamily=Segoe UI;fontSize=12;fontColor=#666666;labelBackgroundColor=none;"
  vertex="1" parent="1">
  <mxGeometry x="200" y="38" width="500" height="20" as="geometry" />
</mxCell>
```

---

## WAF-Aligned Design

Every diagram should visually communicate at least 2-3 WAF pillars:

### Security (Show These)
- Trust boundaries (VNet, private endpoints)
- Identity flow (Entra ID → Managed Identity → services)
- Data encryption indicators (lock icons or labels)
- Private endpoints for PaaS services

### Reliability (Show These)
- Multi-region or zone redundancy (duplicate boxes or annotations)
- Failover paths (dashed arrows for secondary routes)
- Health probes / monitoring connections
- Circuit breaker patterns

### Performance (Show These)
- Caching layers (Redis, CDN)
- Async patterns (queues, events)
- Regional distribution
- Autoscaling indicators

### Cost Optimization (Show These)
- Reserved vs on-demand annotations
- Spot VM indicators
- Tier choices (Premium vs Standard)
- Shared services

### Operational Excellence (Show These)
- Monitoring/logging connections (dotted lines to Azure Monitor)
- Deployment pipelines (optional: CI/CD flow)
- Alert paths

---

## Audience-Focused Diagrams

### For Executives
- **Emphasize**: Trust boundaries, data residency, compliance zones, business flows
- **Minimize**: Technical details, specific service names, internal connections
- **Style**: Larger boxes, fewer elements (5-8 max), clear "in/out" boundaries

### For Architects
- **Emphasize**: All 5 WAF pillars visible, service interactions, scaling hints
- **Minimize**: Implementation details, exact configurations
- **Style**: Medium detail (8-14 elements), numbered flows, boundary labels

### For Engineers
- **Emphasize**: Exact services, APIs, data flows, connection strings, ports
- **Add**: Schema hints, retry policies, timeout values
- **Style**: High detail, service-specific icons, subnet/NSG visibility

---

## Core Rules

### Font Settings

```xml
<!-- In mxGraphModel -->
<mxGraphModel defaultFontFamily="Segoe UI" page="0" ...>

<!-- In EVERY text element's style -->
<mxCell style="text;fontFamily=Segoe UI;fontSize=14;..." />
```

### Arrow Placement (Z-Order)

Arrows must be declared FIRST to render behind other elements:

```xml
<root>
  <mxCell id="0" />
  <mxCell id="1" parent="0" />

  <!-- ARROWS FIRST (renders at back) -->
  <mxCell id="arrow1" edge="1" ... />

  <!-- BOXES AFTER (renders in front) -->
  <mxCell id="box1" vertex="1" ... />
</root>
```

### Label-Arrow Spacing

Labels must be at least 20px away from arrow lines:

```xml
<!-- Arrow at Y=220 -->
<mxCell id="arrow">
  <mxGeometry>
    <mxPoint y="220" as="sourcePoint"/>
  </mxGeometry>
</mxCell>

<!-- Label at Y=180 (40px above arrow) - CORRECT -->
<mxCell id="label" value="Process">
  <mxGeometry y="180" width="60" height="20" />
</mxCell>
```

### Multi-byte Text Width

Allocate sufficient width to prevent unwanted line breaks:

```xml
<!-- 8 Japanese characters × 35px = 280px minimum -->
<mxCell id="title" value="システム構成図">
  <mxGeometry width="300" height="40" />
</mxCell>
```

---

## Instruction Template

When asked to create a draw.io diagram:

1. **Clarify audience**: Exec overview vs architect detail vs engineer spec?
2. **Identify scope**: Context (high-level) vs Container (services) vs Component (internal)?
3. **Gather components**: Which Azure services? What trust boundaries?
4. **Plan WAF elements**: Which 2-3 pillars to emphasize visually?
5. **Generate XML** with all rules applied
6. **Verify** with PNG export

## PNG Verification

Always recommend PNG export for visual verification:

```bash
# macOS
drawio -x -f png -s 2 -t -o output.png input.drawio
open output.png

# Linux
drawio -x -f png -s 2 -t -o output.png input.drawio
xdg-open output.png

# Windows (PowerShell)
& "$env:ProgramFiles\draw.io\draw.io.exe" -x -f png -s 2 -t -o output.png input.drawio
Start-Process output.png
```

---

## Supporting Files

- [reference.md](reference.md) - Complete XML structure reference with Azure shapes
- [examples.md](examples.md) - Production-ready Azure architecture examples
- [checklist.md](checklist.md) - Pre-commit validation checklist (including WAF)
