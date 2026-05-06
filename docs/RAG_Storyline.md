---
marp: true
theme: default
header: Beyond Naive RAG - Internal Brainstorm
paginate: true
style: |

  /* ===== Rich Style for Marp ===== */

  /* --- Utility Classes --- */
  .text-large { font-size: 1.3em; }
  .text-center { text-align: center; }

  /* --- Grid Layouts --- */
  .grid-2col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    margin-top: 1rem;
  }
  .grid-3col {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 1.2rem;
    margin-top: 1rem;
  }

  /* --- Panel --- */
  .panel {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 1.2rem;
  }
  .panel h3 {
    margin-top: 0;
    color: #1f2937;
    font-size: 1.1em;
    border-bottom: 2px solid #e5e7eb;
    padding-bottom: 0.4rem;
  }
  .panel ul {
    margin: 0.5rem 0 0 0;
    padding-left: 1.2rem;
  }
  .panel li {
    margin-bottom: 0.3rem;
    color: #374151;
  }
  .panel-accent {
    background: linear-gradient(135deg, #3b82f6 0%, #2563eb 100%);
    border: none;
    color: #fff;
  }
  .panel-accent h3 {
    color: #fff;
    border-bottom-color: rgba(255,255,255,0.3);
  }
  .panel-accent li {
    color: rgba(255,255,255,0.95);
  }

  /* --- Image Split --- */
  .image-split {
    display: grid;
    grid-template-columns: 2fr 3fr;
    gap: 1.5rem;
    align-items: center;
    margin-top: 1rem;
  }
  .image-split img {
    width: 100%;
    border-radius: 12px;
    object-fit: cover;
  }
  .split-content ul {
    padding-left: 1.2rem;
  }
  .split-content li {
    margin-bottom: 0.4rem;
    color: #374151;
  }

  /* --- Card Grid --- */
  .card-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 1rem;
    margin-top: 1rem;
  }
  .card {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 1.2rem;
    text-align: center;
    transition: box-shadow 0.2s;
  }
  .card-icon {
    font-size: 2em;
    margin-bottom: 0.5rem;
  }
  .card h4 {
    margin: 0.3rem 0;
    color: #1f2937;
    font-size: 1em;
  }
  .card p {
    margin: 0;
    color: #4b5563;
    font-size: 0.85em;
  }

  /* --- Timeline --- */
  .timeline {
    position: relative;
    padding-left: 36px;
    margin-top: 1rem;
  }
  .timeline::before {
    content: '';
    position: absolute;
    left: 13px;
    top: 0;
    bottom: 0;
    width: 2px;
    background: linear-gradient(180deg, #3b82f6 0%, #10b981 100%);
    border-radius: 2px;
  }
  .timeline-item {
    position: relative;
    margin-bottom: 1.2rem;
  }
  .timeline-item::before {
    content: '';
    position: absolute;
    left: -29px;
    top: 0.4em;
    width: 10px;
    height: 10px;
    background: #3b82f6;
    border-radius: 50%;
    border: 2px solid #fff;
    box-shadow: 0 0 0 2px #3b82f6;
  }
  .timeline-item strong {
    color: #1f2937;
  }
  .timeline-item span {
    color: #4b5563;
    margin-left: 0.3rem;
  }

  /* --- Highlight Box --- */
  .highlight-box {
    background: linear-gradient(135deg, #3b82f6 0%, #10b981 100%);
    color: #fff;
    border-radius: 16px;
    padding: 2rem;
    text-align: center;
    margin: 1.5rem auto;
    max-width: 80%;
  }
  .highlight-box h3 {
    margin-top: 0;
    font-size: 1.3em;
    color: #fff;
  }
  .highlight-box p {
    margin-bottom: 0;
    font-size: 1.05em;
    color: rgba(255,255,255,0.95);
  }

  /* --- Statistics --- */
  .stat-box {
    display: flex;
    justify-content: center;
    gap: 2rem;
    margin-top: 1rem;
    flex-wrap: wrap;
  }
  .stat-box > div {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 1.2rem 2rem;
    text-align: center;
    min-width: 140px;
    flex: 1 1 calc(33.333% - 1.5rem);
    max-width: calc(50% - 1rem);
  }
  .stat-number {
    font-size: 2.2em;
    font-weight: 700;
    color: #3b82f6;
    line-height: 1.1;
  }
  .stat-label {
    font-size: 0.85em;
    color: #4b5563;
    margin-top: 0.3rem;
  }
  .stat-trend {
    font-size: 0.75em;
    color: #10b981;
    margin-top: 0.2rem;
  }

  /* --- Section Backgrounds --- */
  section.bg-gradient {
    background: linear-gradient(135deg, #3b82f6 0%, #60a5fa 50%, #10b981 100%);
    color: #fff;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
  }
  section.bg-gradient h1,
  section.bg-gradient h2 {
    color: #fff;
    text-shadow: 0 2px 8px rgba(0,0,0,0.15);
  }
  section.bg-gradient p {
    color: rgba(255,255,255,0.9);
    font-size: 1.2em;
  }

  /* --- Title Hero --- */
  section.title-hero {
    background: linear-gradient(135deg, #1f2937 0%, #374151 50%, #1f2937 100%);
    color: #fff;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
  }
  section.title-hero h1 {
    color: #fff;
    font-size: 2.5em;
    text-shadow: 0 2px 12px rgba(0,0,0,0.3);
    margin-bottom: 0.3em;
  }
  section.title-hero p {
    color: rgba(255,255,255,0.8);
    font-size: 1.3em;
  }

  /* --- Rich Table --- */
  section.rich-table {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
  section.rich-table h2 {
    text-align: center;
  }
  section.rich-table table {
    border-collapse: collapse;
    width: auto;
    margin: 1rem auto;
    font-size: 0.85em;
  }
  section.rich-table thead th {
    background: #3b82f6;
    color: #fff;
    padding: 0.7rem 1rem;
    font-weight: 600;
    border: 1px solid #3b82f6;
  }
  section.rich-table tbody tr:nth-child(even) {
    background: #f0f7ff;
  }
  section.rich-table tbody td {
    padding: 0.6rem 1rem;
    color: #374151;
    border: 1px solid #e5e7eb;
  }

  /* --- Rich Image Right --- */
  section.rich-image-right .image-split {
    grid-template-columns: 3fr 2fr;
  }

  /* --- Rich Image Center --- */
  section.rich-image-center {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
  }
  .image-center-wrap {
    text-align: center;
    margin-top: 1rem;
  }
  .image-center-wrap img {
    max-height: 400px;
    border-radius: 12px;
    box-shadow: 0 4px 16px rgba(0,0,0,0.12);
    object-fit: contain;
  }

  /* --- Image Comparison --- */
  .image-comparison {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1.5rem;
    margin-top: 1rem;
  }
  .comparison-item {
    text-align: center;
  }
  .comparison-item img {
    width: 100%;
    border-radius: 12px;
    object-fit: contain;
  }
  .comparison-label {
    margin-top: 0.5rem;
    font-weight: 600;
    color: #374151;
    font-size: 0.95em;
  }

  /* --- Process Steps --- */
  .process-steps {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 0;
    margin-top: 1.5rem;
    flex-wrap: nowrap;
  }
  .process-step {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 1rem 1.2rem;
    text-align: center;
    flex: 1;
    min-width: 0;
  }
  .process-step-number {
    font-size: 1.4em;
    font-weight: 700;
    color: #3b82f6;
    line-height: 1.2;
  }
  .process-step-label {
    font-size: 0.85em;
    color: #374151;
    margin-top: 0.3rem;
  }
  .process-arrow {
    font-size: 1.5em;
    color: #3b82f6;
    padding: 0 0.4rem;
    flex-shrink: 0;
  }

  /* --- Rich Quote --- */
  section.rich-quote {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
  }
  section.rich-quote blockquote {
    border-left: 4px solid #3b82f6;
    padding: 1.5rem 2rem;
    margin: 1rem auto;
    max-width: 80%;
    background: #f9fafb;
    border-radius: 0 12px 12px 0;
    font-size: 1.2em;
    font-style: italic;
    color: #374151;
  }
  section.rich-quote blockquote p:last-child {
    font-style: normal;
    font-size: 0.85em;
    color: #6b7280;
    margin-top: 0.5rem;
  }

  /* --- Rich Two Column (Simple) --- */
  .rich-2col {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2rem;
    margin-top: 1rem;
  }
  .rich-2col-left {
    border-right: 2px solid #e5e7eb;
    padding-right: 1.5rem;
  }
  .rich-2col-right {
    padding-left: 0.5rem;
  }
  .rich-2col h3 {
    color: #1f2937;
    font-size: 1.05em;
    margin-top: 0;
    margin-bottom: 0.5rem;
    border-bottom: 2px solid #e5e7eb;
    padding-bottom: 0.3rem;
  }
  .rich-2col ul {
    padding-left: 1.2rem;
    margin: 0;
  }
  .rich-2col li {
    margin-bottom: 0.3rem;
    color: #374151;
  }

  /* --- Rich Big Statement --- */
  section.rich-statement {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    background: linear-gradient(135deg, #1f2937 0%, #374151 100%);
  }
  section.rich-statement h1 {
    color: #ffffff;
    font-size: 2.8em;
    font-weight: 800;
    line-height: 1.1;
    max-width: 80%;
    text-shadow: 0 2px 8px rgba(0,0,0,0.2);
  }
  section.rich-statement p {
    color: rgba(255,255,255,0.75);
    font-size: 1.1em;
    margin-top: 0.5rem;
  }

  /* --- Rich Sidebar --- */
  .rich-sidebar-layout {
    display: grid;
    grid-template-columns: 3fr 1fr;
    gap: 1.5rem;
    margin-top: 1rem;
  }
  .rich-sidebar {
    border-left: 3px solid #3b82f6;
    padding-left: 1rem;
    font-size: 0.85em;
  }
  .rich-sidebar h4 {
    margin-top: 0;
    color: #3b82f6;
    font-size: 0.95em;
    margin-bottom: 0.5rem;
  }
  .rich-sidebar ul {
    padding-left: 1rem;
    margin: 0;
  }
  .rich-sidebar li {
    margin-bottom: 0.3rem;
    color: #4b5563;
  }

  /* --- Rich Progress Bar --- */
  .progress-container {
    display: flex;
    flex-direction: column;
    gap: 1rem;
    margin-top: 1rem;
  }
  .progress-item {
    display: flex;
    flex-direction: column;
    gap: 0.3rem;
  }
  .progress-header {
    display: flex;
    justify-content: space-between;
    font-size: 0.9em;
  }
  .progress-label {
    font-weight: 600;
    color: #1f2937;
  }
  .progress-value {
    color: #6b7280;
  }
  .progress-track {
    height: 12px;
    background: #e5e7eb;
    border-radius: 6px;
    overflow: hidden;
  }
  .progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #3b82f6, #10b981);
    border-radius: 6px;
  }

  /* --- Rich Chart Bar --- */
  .chart-bar-container {
    display: flex;
    flex-direction: column;
    gap: 0.8rem;
    margin-top: 1rem;
  }
  .chart-bar-row {
    display: flex;
    align-items: center;
    gap: 0.8rem;
  }
  .chart-bar-label {
    width: 120px;
    flex-shrink: 0;
    font-size: 0.85em;
    font-weight: 600;
    color: #1f2937;
    text-align: right;
  }
  .chart-bar-track {
    flex: 1;
    height: 24px;
    background: #f3f4f6;
    border-radius: 4px;
    overflow: hidden;
  }
  .chart-bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #3b82f6, #60a5fa);
    border-radius: 4px;
  }
  .chart-bar-value {
    width: 50px;
    flex-shrink: 0;
    font-size: 0.85em;
    font-weight: 600;
    color: #3b82f6;
  }

  /* --- Rich Horizontal Timeline --- */
  .h-timeline {
    position: relative;
    margin-top: 2rem;
  }
  .h-timeline-line {
    position: absolute;
    top: 9px;
    left: 5%;
    right: 5%;
    height: 3px;
    background: linear-gradient(90deg, #3b82f6, #10b981);
    border-radius: 2px;
  }
  .h-timeline-items {
    display: flex;
    justify-content: space-around;
    position: relative;
  }
  .h-timeline-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    text-align: center;
    max-width: 150px;
  }
  .h-timeline-dot {
    width: 14px;
    height: 14px;
    background: #3b82f6;
    border: 3px solid #fff;
    border-radius: 50%;
    box-shadow: 0 0 0 2px #3b82f6;
    margin-bottom: 0.8rem;
    flex-shrink: 0;
  }
  .h-timeline-item strong {
    font-size: 0.85em;
    color: #1f2937;
  }
  .h-timeline-item span {
    font-size: 0.75em;
    color: #6b7280;
    margin-top: 0.2rem;
  }

  /* --- Rich Pull Quote --- */
  section.rich-pull-quote {
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
  }
  .pull-quote-wrap {
    max-width: 80%;
    position: relative;
    padding: 2rem 1rem;
  }
  .pull-quote-wrap::before {
    content: '\201C';
    font-size: 6em;
    color: rgba(59,130,246,0.15);
    position: absolute;
    top: -0.3em;
    left: -0.1em;
    line-height: 1;
    font-family: Georgia, serif;
  }
  .pull-quote-text {
    font-size: 1.6em;
    font-weight: 600;
    color: #1f2937;
    line-height: 1.3;
    font-style: italic;
  }
  .pull-quote-attr {
    font-size: 1em;
    color: #3b82f6;
    margin-top: 0.8rem;
    font-weight: 600;
  }
  .pull-quote-ctx {
    font-size: 0.85em;
    color: #6b7280;
    margin-top: 0.3rem;
  }

  /* --- Rich Bento Grid --- */
  .bento-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    grid-auto-flow: dense;
    gap: 0.8rem;
    margin-top: 1rem;
  }
  .bento-cell {
    background: #f9fafb;
    border: 1px solid #e5e7eb;
    border-radius: 12px;
    padding: 1rem;
  }
  .bento-cell h4 {
    margin: 0 0 0.3rem 0;
    color: #1f2937;
    font-size: 0.95em;
  }
  .bento-cell p {
    margin: 0;
    color: #4b5563;
    font-size: 0.8em;
  }
  .bento-sm {
    grid-column: span 1;
    grid-row: span 1;
  }
  .bento-md {
    grid-column: span 2;
    grid-row: span 1;
  }
  .bento-lg {
    grid-column: span 2;
    grid-row: span 2;
  }

  /* --- Card Grid Improvement --- */
  .card-icon {
    width: 2.5em;
    height: 2.5em;
    line-height: 2.5em;
    border-radius: 50%;
    background: rgba(59,130,246,0.1);
    display: inline-block;
    margin-bottom: 0.5rem;
  }
  .card {
    box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  }

  /* --- Image Caption --- */
  .image-caption {
    font-size: 0.8em;
    color: #6b7280;
    text-align: center;
    margin-top: 0.5rem;
  }
---

<!-- slide-id: dd396cf3-3fb1-4a98-9cfe-239962172c15 -->

<style>
:root {
	--background: #0b1f3a;
	--foreground: #f8fafc;
	--muted: #173356;
	--muted-foreground: #cbd5e1;
	--primary: #38bdf8;
	--accent: #a78bfa;
	--accent-foreground: #0b1020;
	--success: #22c55e;
	--warning: #f59e0b;
	--danger: #fb7185;
	--border: #2f5278;
}

section {
	background: var(--background);
	color: var(--foreground);
	font-family: Aptos, Segoe UI, Arial, sans-serif;
	line-height: 1.18;
	padding: 56px 72px;
}

h1, h2, h3 {
	color: var(--primary);
	letter-spacing: 0;
	text-wrap: balance;
}

h1 { font-size: 2.25rem; }
h2 { font-size: 1.75rem; }
h3 { font-size: 1.2rem; }
p, li { font-size: 1.04rem; }
strong { color: var(--accent); }
small { color: var(--muted-foreground); }

section.lead h1 {
	font-size: 2.75rem;
	max-width: 860px;
}

section.section h1 {
	font-size: 2.55rem;
}

.subtle { color: var(--muted-foreground); }
.big { font-size: 1.35rem; }
.tight li { margin: 0.24rem 0; }
.center { display: grid; place-content: center; text-align: center; }
.quote { font-size: 1.58rem; color: var(--foreground); max-width: 900px; }

.badge {
	display: inline-block;
	padding: 0.22rem 0.62rem;
	border-radius: 999px;
	background: var(--accent);
	color: var(--accent-foreground);
	font-weight: 700;
	font-size: 0.72rem;
	text-transform: uppercase;
}

.grid { display: grid; gap: 0.82rem; align-items: stretch; }
.grid-2 { grid-template-columns: repeat(2, minmax(0, 1fr)); }
.grid-3 { grid-template-columns: repeat(3, minmax(0, 1fr)); }

.card {
	background: color-mix(in srgb, var(--muted) 72%, transparent);
	border: 1px solid color-mix(in srgb, var(--primary) 34%, #ffffff22);
	border-radius: 8px;
	padding: 0.76rem 0.9rem;
	min-height: 92px;
}

.card h3 { margin: 0 0 0.35rem; }
.card p { margin: 0; color: var(--muted-foreground); }
.num { white-space: nowrap; font-variant-numeric: tabular-nums; }

.flow {
	display: grid;
	grid-template-columns: repeat(4, minmax(0, 1fr));
	gap: 0.55rem;
	margin-top: 1rem;
}

.step {
	border: 1px solid var(--border);
	border-radius: 8px;
	padding: 0.72rem;
	background: color-mix(in srgb, var(--muted) 64%, transparent);
	min-height: 82px;
}

.step b { color: var(--primary); display: block; margin-bottom: 0.25rem; }
.step span { color: var(--muted-foreground); font-size: 0.82rem; }

.table {
	width: 100%;
	border-collapse: collapse;
	font-size: 0.84rem;
}

.table th, .table td {
	border-bottom: 1px solid var(--border);
	padding: 0.48rem 0.58rem;
	text-align: left;
	vertical-align: top;
}

.table th {
	color: var(--primary);
	background: color-mix(in srgb, var(--primary) 16%, var(--background));
}

.callout {
	margin-top: 1rem;
	border-left: 5px solid var(--primary);
	padding: 0.85rem 1rem;
	background: color-mix(in srgb, var(--muted) 70%, transparent);
	font-size: 1.28rem;
}

.demo {
	border: 1px solid color-mix(in srgb, var(--warning) 45%, #ffffff22);
	background: color-mix(in srgb, var(--warning) 14%, var(--background));
}

.danger { color: var(--danger); }
.success { color: var(--success); }
.warning { color: var(--warning); }
</style>

<!-- _class: lead -->

# Beyond Naive RAG

## Advanced Retrieval, Chunking, and Intelligent Search Pipelines

**Internal brainstorming session**  
2 hours including demos

<!-- Presenter intent: Set the tone. This is a working internal deck for shaping the story, not a final customer-facing version. -->

---

# What This Session Is

- Deep dive into RAG system design
- Shared language for advanced patterns
- Demo ideas we can later sharpen

<!-- Presenter intent: Explain that the goal is understanding and alignment first, polish second. -->

---

# What This Session Is Not

- Not a customer-ready pitch
- Not a product comparison
- Not a single reference architecture

<!-- Presenter intent: Keep expectations clear. We are exploring how to explain the topic well internally. -->

---

# Two-Hour Flow

<div class="grid grid-2">
	<div class="card"><h3>Part 1</h3><p>Naive RAG and why it breaks</p></div>
	<div class="card"><h3>Part 2</h3><p>Advanced retrieval techniques</p></div>
	<div class="card"><h3>Part 3</h3><p>GraphRAG and RAFT</p></div>
	<div class="card"><h3>Part 4</h3><p>Demos, discussion, next version</p></div>
</div>

<!-- Presenter intent: Give the audience a map. The session moves from simple to advanced, then into demos and discussion. -->

---

<!-- _class: section center -->

# Theme 1

## Naive RAG Explained With Challenges

<!-- Presenter intent: Start with the baseline everyone can understand before adding sophistication. -->

---

# Why RAG Matters

- Grounds answers in enterprise data
- Reduces hallucination risk
- Adds citations and explainability

<!-- Presenter intent: Explain the business reason first: customers need answers they can trust. -->

---

# RAG In One Sentence

<div class="callout">
Retrieve relevant context before asking the model to answer.
</div>

<!-- Presenter intent: Keep the definition simple. Retrieval gives the model fresh, private, or domain-specific context. -->

---

# RAG In One Flow

<div class="flow">
	<div class="step"><b>Ask</b><span>User question</span></div>
	<div class="step"><b>Retrieve</b><span>Find context</span></div>
	<div class="step"><b>Generate</b><span>Answer with model</span></div>
	<div class="step"><b>Cite</b><span>Show sources</span></div>
</div>

<!-- Presenter intent: Present the mental model. RAG is not magic; it is a sequence of decisions. -->

---

# The Important Shift

- The model is not the knowledge base
- Retrieval chooses what the model sees
- Better context usually beats bigger prompts

<!-- Presenter intent: Explain why retrieval quality is often the real bottleneck. -->

---

# Naive RAG Pattern

<div class="flow">
	<div class="step"><b>Chunk</b><span>Split documents</span></div>
	<div class="step"><b>Embed</b><span>Create vectors</span></div>
	<div class="step"><b>Search</b><span>Top-k similarity</span></div>
	<div class="step"><b>Prompt</b><span>Send to LLM</span></div>
</div>

<!-- Presenter intent: Show the demo-friendly pattern most people start with. -->

---

# Why Naive RAG Demos Well

- Small document sets
- Simple questions
- Clean source material

<!-- Presenter intent: Explain that naive RAG is not useless. It is a good starting point, but controlled demos hide production complexity. -->

---

# Why Naive RAG Breaks

- Real content is messy
- Questions are ambiguous
- Relevance is not just similarity

<!-- Presenter intent: Make the production gap visible. The hard part is not calling a vector database. -->

---

# Challenge: Context Loss

- Chunks cut across meaning
- Headers get separated from details
- Tables lose surrounding explanation

<!-- Presenter intent: Explain that the answer can be present in the corpus but absent from the retrieved chunk. -->

---

# Challenge: Irrelevant Chunks

- Similar words are not always relevant
- Generic text can score highly
- Duplicates crowd out better context

<!-- Presenter intent: Show that vector similarity is a signal, not a decision. -->

---

# Challenge: Poor Ranking

- Best answer may be rank 12
- Top-k can miss critical evidence
- Ranking needs business signals

<!-- Presenter intent: Explain why retrieval quality depends on candidate generation and ranking, not only embeddings. -->

---

# Challenge: No Intent Awareness

- Lookup questions need precision
- Reasoning questions need context
- Troubleshooting needs sequence

<!-- Presenter intent: Make the point that one retrieval pipeline cannot serve every question equally well. -->

---

# Challenge: Static Pipelines

- Same chunking for every document
- Same retrieval for every query
- Same ranking despite feedback

<!-- Presenter intent: Explain why production RAG should adapt over time. -->

---

# Key Message

<div class="quote">
Naive RAG works in demos, fails when retrieval quality becomes the product experience.
</div>

<!-- Presenter intent: Land the first major thesis. The user experiences retrieval failures as bad AI. -->

---

# Demo Placeholder

## Naive Retrieval Failure

- Ask one realistic question
- Show misleading top-k chunks
- Explain why the model cannot recover

<!-- Presenter intent: Prepare a simple before-state demo. The goal is not to embarrass the model; it is to show bad context creates bad answers. -->

---

<!-- _class: section center -->

# Theme 2

## Advanced RAG Techniques

<!-- Presenter intent: Move from diagnosis to system design. Advanced RAG means better decisions at each stage. -->

---

# RAG Is A System

- Data preparation
- Retrieval strategy
- Generation guardrails

<!-- Presenter intent: Explain that RAG is an architecture pattern, not a single API call. -->

---

# Production Pipeline

<div class="flow">
	<div class="step"><b>Ingest</b><span>Sources and metadata</span></div>
	<div class="step"><b>Prepare</b><span>Structure and chunks</span></div>
	<div class="step"><b>Retrieve</b><span>Search and rank</span></div>
	<div class="step"><b>Improve</b><span>Measure and tune</span></div>
</div>

<!-- Presenter intent: Provide the higher-level production view before drilling into each component. -->

---

# Pipeline: Ingestion

- Connect to source systems
- Preserve metadata
- Track freshness and ownership

<!-- Presenter intent: Use SharePoint or knowledge-base content as the example. Good retrieval starts before indexing. -->

---

# Pipeline: Document Processing

- Extract text and layout
- Preserve tables and sections
- Handle OCR where needed

<!-- Presenter intent: Mention Document Intelligence as a production option for complex documents. -->

---

# Pipeline: Chunking

- Decide boundaries
- Preserve meaning
- Balance size and precision

<!-- Presenter intent: Introduce chunking as the first major design lever. -->

---

# Pipeline: Embeddings

- Convert text into vectors
- Capture semantic similarity
- Example: `text-embedding-3-large`

<!-- Presenter intent: Explain embeddings simply: text becomes a searchable representation of meaning. -->

---

# Pipeline: Indexing

- Store content fields
- Store vector fields
- Store metadata fields

<!-- Presenter intent: Explain that the index is more than vectors. Metadata enables filtering, boosting, and governance. -->

---

# Pipeline: Retrieval

- Generate candidates
- Combine multiple signals
- Return evidence, not answers

<!-- Presenter intent: Separate retrieval from generation. Retrieval finds evidence; generation writes the answer. -->

---

# MCP For Data Retrieval

- Connect tools and data sources
- Retrieve live enterprise context
- Keep orchestration explicit

<!-- Presenter intent: Explain MCP as a standard way to let an AI system call approved tools or data sources for retrieval, instead of only searching a prebuilt vector index. -->

---

# Where MCP Fits

<div class="flow">
  <div class="step"><b>User asks</b><span>Intent</span></div>
  <div class="step"><b>Route</b><span>Choose source</span></div>
  <div class="step"><b>MCP tool</b><span>Fetch data</span></div>
  <div class="step"><b>Answer</b><span>Grounded response</span></div>
</div>

<!-- Presenter intent: Position MCP next to retrieval and routing. It is useful when the answer depends on live systems, APIs, databases, or governed tools. -->

---

# Pipeline: Re-Ranking

- Start broad
- Rank with stronger models
- Send only the best context

<!-- Presenter intent: Introduce the idea of top 50 candidates, re-ranked down to a smaller grounded context set. -->

---

# Pipeline: Generation

- Answer from retrieved context
- Cite sources
- Flag gaps clearly

<!-- Presenter intent: Explain that generation should be constrained by retrieved evidence, especially for enterprise use. -->

---

# Pipeline: Feedback

- Capture quality signals
- Find failure patterns
- Tune retrieval over time

<!-- Presenter intent: Position feedback as part of the architecture, not an afterthought. -->

---

# Chunking Spectrum

<div class="grid grid-3">
	<div class="card"><h3>Simple</h3><p>Fixed and recursive</p></div>
	<div class="card"><h3>Structured</h3><p>Document-aware</p></div>
	<div class="card"><h3>Adaptive</h3><p>Semantic and agentic</p></div>
</div>

<!-- Presenter intent: Show that chunking is a spectrum, not a binary choice. -->

---

# Fixed-Size Chunking

- Simple to implement
- Fast to run
- Breaks meaning often

<!-- Presenter intent: Explain the trade-off: easiest option, weakest context preservation. -->

---

# Recursive Chunking

- Respects separators
- Keeps paragraphs together
- Better default baseline

<!-- Presenter intent: Explain why recursive splitting is often the first upgrade from fixed-size chunking. -->

---

# Document-Based Chunking

- Uses headings and sections
- Keeps tables with context
- Fits structured documents

<!-- Presenter intent: Explain that document structure is a retrieval signal. A section heading can change the meaning of a paragraph. -->

---

# Semantic Chunking

- Splits by meaning
- Uses embedding similarity
- Preserves topical coherence

<!-- Presenter intent: Explain semantic chunking as content-aware boundaries rather than fixed token windows. -->

---

# Agentic Chunking

- LLM chooses boundaries
- Adapts per document
- Higher cost and complexity

<!-- Presenter intent: Present agentic chunking as powerful but not automatically necessary. Use where documents vary heavily. -->

---

# Chunking Trade-Off

<div class="grid grid-2">
	<div class="card"><h3>Granularity</h3><p>Precise retrieval, less context</p></div>
	<div class="card"><h3>Context</h3><p>Better meaning, more noise</p></div>
</div>

<!-- Presenter intent: Land the core chunking tension. Smaller is not always better; larger is not always safer. -->

---

# Late Chunking

- Retrieve larger blocks first
- Split later for prompting
- Keeps broader context available

<!-- Presenter intent: Explain late chunking as a way to avoid losing document-level meaning too early. -->

---

# Hierarchical Chunking

- Document -> section -> paragraph
- Search across levels
- Return the right level of context

<!-- Presenter intent: Explain how hierarchy helps the system retrieve both overview and detail. -->

---

# Graph-Based Chunking

- Link related chunks
- Preserve relationships
- Supports reasoning paths

<!-- Presenter intent: Bridge into GraphRAG later. Chunk relationships can matter as much as chunk text. -->

---

# Chunking Comparison

<table class="table">
	<thead><tr><th>Approach</th><th>Best for</th><th>Main risk</th></tr></thead>
	<tbody>
		<tr><td>Fixed</td><td>Fast baseline</td><td>Meaning breaks</td></tr>
		<tr><td>Document</td><td>Structured docs</td><td>Messy source formats</td></tr>
		<tr><td>Semantic</td><td>Coherent topics</td><td>More processing</td></tr>
		<tr><td>Agentic</td><td>High-value corpora</td><td>Cost and latency</td></tr>
	</tbody>
</table>

<!-- Presenter intent: Use this as a recap, not as a dense teaching slide. -->

---

# Demo Placeholder

## Chunking Impact

- Same document
- Fixed versus semantic chunks
- Compare retrieved context

<!-- Presenter intent: Show that chunking changes the evidence the model receives, even when the source document is unchanged. -->

---

# Dense Embeddings

- Neural representation
- Captures meaning
- Good for paraphrases

<!-- Presenter intent: Explain dense vectors with a simple example like car and vehicle being semantically close. -->

---

# Fine-Tuning Embedding Models

- Adapt similarity to the domain
- Improve retrieval for specialist terms
- Requires strong evaluation data

<!-- Presenter intent: Explain that embedding fine-tuning targets retrieval quality, not answer style. It is useful when generic embeddings miss domain-specific similarity. -->

---

# When To Fine-Tune Embeddings

- Domain language is specialized
- Similar concepts look unrelated
- Baseline retrieval fails repeatedly

<!-- Presenter intent: Make this practical. Fine-tune embeddings only when retrieval evaluation shows a consistent gap that prompting or ranking cannot fix. -->

---

# Sparse Search

- Keyword and term matching
- BM25-style precision
- Strong for exact phrases

<!-- Presenter intent: Explain why old-school keyword search remains valuable. Exact names, IDs, and error codes matter. -->

---

# Vector Search

- Similarity-based retrieval
- Finds semantic matches
- Can miss exact constraints

<!-- Presenter intent: Explain the strength and weakness of vector search without over-mathematizing it. -->

---

# Semantic Search

- Understands context better
- Improves ranking quality
- Often used after candidate retrieval

<!-- Presenter intent: Position semantic ranking as a stronger relevance layer, not just another name for vectors. -->

---

# Hybrid Search

- Dense vectors
- Sparse keyword signals
- Re-ranking on top

<!-- Presenter intent: Explain why hybrid search is often the production default: recall plus precision. -->

---

# Why Hybrid Wins

<div class="grid grid-2">
	<div class="card"><h3>Vectors</h3><p>Find meaning</p></div>
	<div class="card"><h3>Keywords</h3><p>Respect exact terms</p></div>
</div>

<!-- Presenter intent: Keep this simple. The best systems combine signals because users mix vague language with precise constraints. -->

---

# Production Retrieval Flow

<div class="flow">
	<div class="step"><b>Top 50</b><span>Broad candidates</span></div>
	<div class="step"><b>Top 10</b><span>Semantic re-rank</span></div>
	<div class="step"><b>Top 3-5</b><span>Grounding context</span></div>
	<div class="step"><b>Answer</b><span>Citations</span></div>
</div>

<!-- Presenter intent: Use this as the production contrast to naive top-k similarity search. -->

---

# Azure Example

- Azure AI Search index
- HNSW vector search
- Semantic ranker

<!-- Presenter intent: Ground the concept in a concrete Azure implementation without turning the slide into architecture documentation. -->

---

# Signal Boosting

- Trusted source boost
- Freshness boost
- Business priority boost

<!-- Presenter intent: Explain boosting as relevance tuning based on what the business already knows. -->

---

# Signal Suppression

- Outdated content
- Low-quality sources
- Deprecated policies

<!-- Presenter intent: Explain that ranking is also about pushing bad context down, not only pulling good context up. -->

---

# Metadata Filtering

- Time range
- Region
- Document type

<!-- Presenter intent: Explain that filters constrain the search space before the model answers. -->

---

# Security Filtering

- Entra ID permissions
- Confidentiality labels
- User-specific access

<!-- Presenter intent: Emphasize enterprise retrieval security. The model must not see documents the user cannot access. -->

---

# Retrieval Is Constrained Search

<div class="quote">
The best match is only useful if it is relevant, current, allowed, and trusted.
</div>

<!-- Presenter intent: Land the retrieval governance message. Similarity is not enough. -->

---

# Query Expansion

- Add synonyms
- Create subqueries
- Recover missing terms

<!-- Presenter intent: Explain that users rarely ask perfect search queries. The system can help complete the intent. -->

---

# Query Expansion Example

<div class="card">
	<h3>User asks: pricing</h3>
	<p>Expand to: cost, subscription, license fees, commercial terms</p>
</div>

<!-- Presenter intent: Use a tiny example to make query expansion obvious. -->

---

# Query Intent Detection

- Factual lookup
- Troubleshooting
- Reasoning or synthesis

<!-- Presenter intent: Explain that intent determines which retrieval path should run. -->

---

# Query Routing

- Pick the right index
- Pick the right retriever
- Pick the right answer strategy

<!-- Presenter intent: Introduce routing as orchestration. Different questions deserve different pipelines. -->

---

# Multi-Pipeline RAG

<div class="grid grid-2">
	<div class="card"><h3>FAQ</h3><p>Precise, short answers</p></div>
	<div class="card"><h3>Knowledge Base</h3><p>Grounded document answers</p></div>
	<div class="card"><h3>Structured Data</h3><p>Database or API lookup</p></div>
	<div class="card"><h3>GraphRAG</h3><p>Relationship reasoning</p></div>
</div>

<!-- Presenter intent: Show that advanced RAG is often a routing layer over multiple specialized retrieval strategies. -->

---

# Demo Placeholder

## Hybrid Search

- Use a query with one exact term
- Use a query with one semantic match
- Compare vector-only and hybrid results

<!-- Presenter intent: Show why hybrid search handles mixed real-world queries better than vector-only retrieval. -->

---

# Demo Placeholder

## Query Routing

- Ask a factual question
- Ask a reasoning question
- Show different pipelines triggered

<!-- Presenter intent: Make routing visible. The audience should see that advanced RAG can choose a retrieval strategy. -->

---

<!-- _class: section center -->

# Theme 3

## GraphRAG

<!-- Presenter intent: Move into relationship-aware retrieval after the audience understands chunk-based retrieval. -->

---

# Why Chunks Are Not Enough

- Some answers span documents
- Relationships carry meaning
- Similarity misses paths

<!-- Presenter intent: Explain the gap GraphRAG addresses: not all knowledge is contained in one nearby passage. -->

---

# What GraphRAG Adds

- Entities
- Relationships
- Communities or clusters

<!-- Presenter intent: Explain the graph ingredients simply: things, links, and groups of related things. -->

---

# Knowledge Graph From Documents

<div class="flow">
	<div class="step"><b>Extract</b><span>Entities</span></div>
	<div class="step"><b>Link</b><span>Relationships</span></div>
	<div class="step"><b>Summarize</b><span>Communities</span></div>
	<div class="step"><b>Retrieve</b><span>Graph context</span></div>
</div>

<!-- Presenter intent: Show the graph construction process at a high level. -->

---

# Knowledge Graph Versus GraphRAG

<div class="grid grid-2">
  <div class="card"><h3>Knowledge Graph</h3><p>Structured facts and relationships</p></div>
  <div class="card"><h3>GraphRAG</h3><p>Retrieval strategy using graph context</p></div>
</div>

<!-- Presenter intent: Separate the asset from the retrieval pattern. A knowledge graph is the structured representation; GraphRAG uses graph structure to retrieve better context for generation. -->

---

# Choose Knowledge Graph

- Need governed relationship data
- Need analytics or exploration
- Need reusable structured facts

<!-- Presenter intent: Explain that a knowledge graph is valuable even without generation. Choose it when the graph itself is a durable data product. -->

---

# Choose GraphRAG

- Need generated answers
- Need multi-hop context
- Need narrative synthesis

<!-- Presenter intent: Explain that GraphRAG is the right framing when the user experience is an answer, not just graph navigation. -->

---

# Retrieve Relationships

- Not only matching chunks
- Also connected facts
- Includes neighborhood context

<!-- Presenter intent: Contrast GraphRAG retrieval with top-k passage retrieval. -->

---

# Multi-Hop Example

<div class="callout">
X is related to Y, and Y is impacted by Z.
</div>

<!-- Presenter intent: Use a plain relationship chain before showing any graph visual. -->

---

# When GraphRAG Helps

- Complex policy landscapes
- Root-cause questions
- Cross-document synthesis

<!-- Presenter intent: Give practical use cases where graph retrieval earns its complexity. -->

---

# When GraphRAG Is Overkill

- Simple FAQ lookup
- Small clean corpora
- Low relationship density

<!-- Presenter intent: Be balanced. GraphRAG is powerful, but not the answer to every RAG problem. -->

---

# GraphRAG Design Choices

- What entities matter?
- Which relationships matter?
- How often does the graph refresh?

<!-- Presenter intent: Make GraphRAG feel like a design space, not a single product feature. -->

---

# Demo Placeholder

## GraphRAG Multi-Hop Answer

- Ask a relationship-heavy question
- Show graph path or cluster
- Compare against chunk-only retrieval

<!-- Presenter intent: Make the graph benefit concrete: the answer depends on linked context, not one isolated chunk. -->

---

<!-- _class: section center -->

# Theme 4

## Retrieval-Augmented Fine-Tuning

<!-- Presenter intent: Explain RAFT after RAG, so the audience can compare runtime grounding with model adaptation. -->

---

# What RAFT Is

- Fine-tuning with retrieved context
- Teaches domain answer patterns
- Improves use-case behavior

<!-- Presenter intent: Define RAFT without implying it replaces retrieval. -->

---

# RAG Versus RAFT

<div class="grid grid-2">
	<div class="card"><h3>RAG</h3><p>Grounds at runtime</p></div>
	<div class="card"><h3>RAFT</h3><p>Adapts the model</p></div>
</div>

<!-- Presenter intent: Establish the clean distinction. RAG changes context; RAFT changes model behavior. -->

---

# Runtime Grounding

- Uses latest indexed content
- Keeps sources external
- Easier to update

<!-- Presenter intent: Explain why RAG is strong for changing enterprise knowledge. -->

---

# Model Adaptation

- Learns answer style
- Learns domain patterns
- Can improve consistency

<!-- Presenter intent: Explain why fine-tuning can help when the model repeatedly needs a specialized response pattern. -->

---

# When RAFT Helps

- Repeated domain tasks
- Stable answer formats
- Specialized terminology

<!-- Presenter intent: Give examples where adaptation is useful: support patterns, legal style, engineering triage, compliance responses. -->

---

# RAFT Risks

- Training data quality
- Evaluation burden
- Drift from current knowledge

<!-- Presenter intent: Explain why RAFT needs strong evaluation. It can encode mistakes if the dataset is weak. -->

---

# RAG And RAFT Together

- RAFT improves behavior
- RAG supplies current facts
- Evaluation keeps both honest

<!-- Presenter intent: Land the point that RAFT complements RAG. It should not become a hidden stale knowledge base. -->

---

<!-- _class: section center -->

# Closing

## Feedback, Measurement, and Next Version

<!-- Presenter intent: Return to production operations and internal next steps. -->

---

# Feedback Signals

- Thumbs up or down
- User corrections
- Negative sentiment

<!-- Presenter intent: Explain that user behavior becomes retrieval training data if captured thoughtfully. -->

---

# Hidden Feedback Signals

- Repeated follow-up questions
- Copying citations
- Abandoned sessions

<!-- Presenter intent: Explain that explicit feedback is useful but incomplete. Product telemetry can reveal retrieval failures. -->

---

# Continuous Learning Loop

<div class="flow">
	<div class="step"><b>Retrieve</b><span>Evidence</span></div>
	<div class="step"><b>Generate</b><span>Answer</span></div>
	<div class="step"><b>Measure</b><span>Quality</span></div>
	<div class="step"><b>Improve</b><span>Pipeline</span></div>
</div>

<!-- Presenter intent: Show the closed loop. Production RAG should improve based on observed failures. -->

---

# Feedback Cycle With Evaluations

<div class="flow">
  <div class="step"><b>Capture</b><span>Signals</span></div>
  <div class="step"><b>Evaluate</b><span>Quality</span></div>
  <div class="step"><b>Diagnose</b><span>Root cause</span></div>
  <div class="step"><b>Tune</b><span>Pipeline</span></div>
</div>

<!-- Presenter intent: Explain that feedback becomes useful only when it is paired with evaluations. The goal is to learn whether the failure was retrieval, ranking, chunking, prompting, or missing data. -->

---

# What Evaluations Close

- Retrieval regressions
- Answer quality drift
- Demo-to-production gaps

<!-- Presenter intent: Connect evaluations to operating the system. They turn subjective feedback into repeatable tests before changing the pipeline. -->

---

# Measure Retrieval Quality

- Precision@5
- Recall@10
- Mean Reciprocal Rank

<!-- Presenter intent: Explain that retrieval needs its own metrics before answer quality can be trusted. -->

---

# Measure Answer Quality

- Groundedness
- Relevance
- Citation accuracy

<!-- Presenter intent: Explain that a fluent answer can still be wrong if it is not grounded. -->

---

# Measure User Satisfaction

- Helpful answer rate
- Query refinement rate
- Time to resolution

<!-- Presenter intent: Connect technical quality with the user experience. -->

---

# Internal Brainstorm

## What Should Become Customer-Facing?

- Which concepts resonate most?
- Which examples are strongest?
- Which claims need evidence?

<!-- Presenter intent: Invite critique. This deck is a way to discover the customer-facing story. -->

---

# Internal Brainstorm

## Which Demos Are Credible?

- What can we show live?
- What needs screenshots?
- What needs a backup path?

<!-- Presenter intent: Decide which demos can survive a real delivery environment. -->

---

# Key Takeaways

- RAG is a system, not a feature
- Chunking is foundational
- Hybrid retrieval usually wins

<!-- Presenter intent: Recap the first three messages clearly. -->

---

# Key Takeaways

- Query routing adds intelligence
- GraphRAG adds relationships
- RAFT adapts model behavior

<!-- Presenter intent: Recap the advanced themes without overloading one closing slide. -->

---

# Final Message

<div class="quote">
The difference between a demo and production RAG is not the model. It is the retrieval strategy.
</div>

<!-- Presenter intent: End with the memorable thesis from the outline. -->

---

<!-- _class: section center -->

# Appendix

## Demo Preparation

<!-- Presenter intent: Keep preparation details out of the main flow but available for planning. -->

---

# Demo Checklist

- Define success and failure examples
- Prepare fallback screenshots
- Capture retrieved chunks visibly

<!-- Presenter intent: Make demos about retrieval evidence, not just final answers. -->

---

# Demo 1: Naive Vs Advanced RAG

- Same query
- Same corpus
- Different retrieval pipeline

<!-- Presenter intent: Keep the variables controlled so the improvement is credible. -->

---

# Demo 2: Chunking Impact

- Same source document
- Fixed chunks versus semantic chunks
- Compare context quality

<!-- Presenter intent: Show that data preparation changes answer quality upstream. -->

---

# Demo 3: Hybrid Search

- Keyword-sensitive query
- Semantic paraphrase query
- Show combined ranking

<!-- Presenter intent: Demonstrate that hybrid search handles both precision and meaning. -->

---

# Demo 4: Query Routing

- FAQ-style question
- Structured lookup question
- Reasoning question

<!-- Presenter intent: Show that the orchestrator can choose the right retrieval strategy. -->

---

# Demo 5: GraphRAG

- Multi-hop question
- Graph path or cluster
- Relationship-based answer

<!-- Presenter intent: Show a case where chunk retrieval alone is weaker than graph context. -->

---

# Terminology

<table class="table">
	<thead><tr><th>Term</th><th>Simple meaning</th></tr></thead>
	<tbody>
		<tr><td>Chunk</td><td>A retrievable piece of content</td></tr>
		<tr><td>Embedding</td><td>A vector representation of meaning</td></tr>
		<tr><td>Re-ranker</td><td>A stronger model that sorts candidates</td></tr>
		<tr><td>Grounding</td><td>Answering from retrieved evidence</td></tr>
	</tbody>
</table>

<!-- Presenter intent: Keep this as a quick reference for mixed audiences. -->

---

# Azure Reference Pattern

- Azure AI Search for hybrid retrieval
- Azure OpenAI for embeddings and generation
- Azure AI Foundry for orchestration and evaluation

<!-- Presenter intent: Connect the abstract pattern to a practical Azure implementation. -->

---

# Azure Retrieval Details

- `text-embedding-3-large`
- BM25 plus vector search
- Semantic ranker and scoring profiles

<!-- Presenter intent: Use this appendix slide if the audience wants implementation specifics. -->

---

# Azure Processing Details

- Document Intelligence for layout and OCR
- Semantic chunking around 800-1200 tokens
- Overlap around 200 tokens

<!-- Presenter intent: Mention these as starting points, not universal defaults. Corpus evaluation should tune them. -->

---

# Open Questions

- Which customer scenarios should anchor the story?
- Which demo data can we safely use?
- Which claims need internal validation?

<!-- Presenter intent: End the appendix with practical next steps for turning this into a customer-ready deck. -->