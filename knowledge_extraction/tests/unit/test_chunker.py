from knowledge_extraction.application.pipelines.stage_1_chunking import SemanticChunker
from knowledge_extraction.domain import Document, Page


def _doc(doc_id: str = "d1") -> Document:
    return Document(id=doc_id, title="t", source_path="x.pdf", pages=[], sections=[])  # type: ignore[arg-type]


def test_chunker_splits_by_headings() -> None:
    md = """# Introduction

Paragraph one.

Paragraph two.

## Methods

Methods text.

## Results

Results text.
"""
    chunker = SemanticChunker(target_chars=20, max_chars=80)
    sections, chunks = chunker.chunk(_doc(), md)
    assert len(sections) == 3
    assert chunks
    assert all(c.document_id == "d1" for c in chunks)
    assert all(c.section_id for c in chunks)


def test_repeated_section_titles_produce_distinct_chunks() -> None:
    """Regression: sections sharing a title must yield their own slice, not the first one."""
    md = """# Introduction

Alpha body.

# Notes

First notes body.

# Introduction

Beta body.

# Notes

Second notes body.
"""
    chunker = SemanticChunker(target_chars=20, max_chars=200)
    sections, chunks = chunker.chunk(_doc(), md)

    assert len(sections) == 4
    texts = [c.text for c in chunks]

    # Every section's body must appear in some chunk.
    for needle in ("Alpha body.", "Beta body.", "First notes body.", "Second notes body."):
        assert any(needle in t for t in texts), f"missing slice for: {needle}"

    # And no chunk text should be duplicated.
    assert len(texts) == len(set(texts)), f"duplicate chunk texts: {texts}"


def test_chunks_match_their_section_slice() -> None:
    """Each chunk's text must come from its declared section, not a homonymous earlier section."""
    md = """# Topic

Original topic content.

# Topic

Different topic content.
"""
    chunker = SemanticChunker(target_chars=20, max_chars=200)
    sections, chunks = chunker.chunk(_doc(), md)
    assert len(sections) == 2

    by_section: dict[str, list[str]] = {s.id: [] for s in sections}
    for c in chunks:
        assert c.section_id is not None
        by_section[c.section_id].append(c.text)

    first_id, second_id = sections[0].id, sections[1].id
    assert any("Original topic content." in t for t in by_section[first_id])
    assert any("Different topic content." in t for t in by_section[second_id])
    assert not any("Different topic content." in t for t in by_section[first_id])
    assert not any("Original topic content." in t for t in by_section[second_id])


def test_chunker_uses_pagebreak_markers_for_page_mapping() -> None:
    doc = Document(
        id="d2",
        title="t",
        source_path="x.pdf",  # type: ignore[arg-type]
        pages=[Page(number=i, text="") for i in range(1, 6)],
        sections=[],
    )
    md = """# Intro

Page one content.

<!-- PageBreak -->

# Benchmarks

English language understanding (SuperGLUE) appears on this page.
"""
    chunker = SemanticChunker(target_chars=200, max_chars=400)
    sections, chunks = chunker.chunk(doc, md)
    benchmark_section = next(s for s in sections if s.title == "Benchmarks")
    benchmark_chunks = [c for c in chunks if c.section_id == benchmark_section.id]

    assert benchmark_section.page_start == 2
    assert benchmark_section.page_end == 2
    assert benchmark_chunks
    assert all(c.page_start == 2 and c.page_end == 2 for c in benchmark_chunks)
