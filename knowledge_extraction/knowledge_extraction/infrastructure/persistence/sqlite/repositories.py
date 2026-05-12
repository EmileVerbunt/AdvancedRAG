"""SQLite engine + repositories."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

import orjson
from sqlalchemy import create_engine, delete, select, text
from sqlalchemy.orm import Session, sessionmaker

from knowledge_extraction.domain import (
    ChartInterpretation,
    Chunk,
    Claim,
    Document,
    DriftEvent,
    ExtractionResult,
    Figure,
    LayoutBoundingRegion,
    LayoutSpan,
    OntologyProposal,
    OntologyProposalSource,
    OntologyStatus,
    OntologyVersion,
    Relationship,
    Table,
    TableCell,
)
from knowledge_extraction.infrastructure.persistence.sqlite.models import (
    Base,
    ChunkExtractionRow,
    ChunkRow,
    ClaimRow,
    DocumentRow,
    DriftEventRow,
    EntityAliasRow,
    EntityMergeRow,
    EntityRow,
    FigureRow,
    OntologyProposalRow,
    OntologyRejectionRow,
    OntologyVersionRow,
    PromptCallRow,
    RelationshipRow,
    TableCellRow,
    TableRow,
)


def make_engine(sqlite_path: Path):
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
    Base.metadata.create_all(engine)
    _apply_schema_patches(engine)
    return engine


def _apply_schema_patches(engine) -> None:
    # Lightweight forward-compatibility patching for existing SQLite files.
    with engine.begin() as conn:
        claim_columns = {
            row[1]
            for row in conn.execute(text("PRAGMA table_info(claims)")).fetchall()
        }
        if "supporting_figure_id" not in claim_columns:
            conn.execute(text("ALTER TABLE claims ADD COLUMN supporting_figure_id VARCHAR(64)"))
        if "supporting_table_id" not in claim_columns:
            conn.execute(text("ALTER TABLE claims ADD COLUMN supporting_table_id VARCHAR(64)"))


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class RelationalRepository:
    """Repository for documents, tables, chunks, and extractions."""

    def __init__(self, sf: sessionmaker[Session]) -> None:
        self._sf = sf

    def save_document(self, document: Document) -> None:
        with self._sf() as s, s.begin():
            row = s.get(DocumentRow, document.id)
            if row is None:
                s.add(DocumentRow(
                    id=document.id,
                    title=document.title,
                    source_path=str(document.source_path),
                    page_count=document.page_count,
                ))
            else:
                row.title = document.title
                row.source_path = str(document.source_path)
                row.page_count = document.page_count
        if document.tables:
            self.save_tables(document.tables)
        if document.figures:
            self.save_figures(document.figures)

    def save_tables(self, tables: Iterable[Table]) -> None:
        with self._sf() as s, s.begin():
            for table in tables:
                row = s.get(TableRow, table.id)
                payload_regions = orjson.dumps([r.model_dump(mode="json") for r in table.bounding_regions]).decode("utf-8")
                payload_spans = orjson.dumps([sp.model_dump(mode="json") for sp in table.spans]).decode("utf-8")
                if row is None:
                    s.add(TableRow(
                        id=table.id,
                        document_id=table.document_id,
                        page=table.page,
                        page_end=table.page_end,
                        caption=table.caption,
                        caption_page=table.caption_page,
                        markdown=table.markdown,
                        bounding_regions_json=payload_regions,
                        spans_json=payload_spans,
                    ))
                else:
                    row.document_id = table.document_id
                    row.page = table.page
                    row.page_end = table.page_end
                    row.caption = table.caption
                    row.caption_page = table.caption_page
                    row.markdown = table.markdown
                    row.bounding_regions_json = payload_regions
                    row.spans_json = payload_spans
                    s.execute(delete(TableCellRow).where(TableCellRow.table_id == table.id))
                for cell in table.cells:
                    s.add(TableCellRow(
                        table_id=table.id,
                        row_index=cell.row,
                        column_index=cell.col,
                        text=cell.text,
                        kind=cell.kind,
                        row_span=cell.row_span,
                        col_span=cell.col_span,
                        page=cell.page,
                        span_start=cell.span_start,
                        span_end=cell.span_end,
                        bounding_regions_json=orjson.dumps([r.model_dump(mode="json") for r in cell.bounding_regions]).decode("utf-8"),
                        spans_json=orjson.dumps([sp.model_dump(mode="json") for sp in cell.spans]).decode("utf-8"),
                    ))

    def save_figures(
        self,
        figures: Iterable[Figure],
        interpretations: dict[str, ChartInterpretation] | None = None,
        *,
        model: str | None = None,
    ) -> None:
        with self._sf() as s, s.begin():
            for figure in figures:
                row = s.get(FigureRow, figure.id)
                interpretation = (interpretations or {}).get(figure.id)
                payload = (
                    orjson.dumps(interpretation.model_dump(mode="json")).decode("utf-8")
                    if interpretation is not None
                    else None
                )
                crop_box_json = orjson.dumps(list(figure.crop_box) if figure.crop_box else None).decode("utf-8")
                regions_json = orjson.dumps([r.model_dump(mode="json") for r in figure.bounding_regions]).decode("utf-8")
                spans_json = orjson.dumps([sp.model_dump(mode="json") for sp in figure.spans]).decode("utf-8")
                elements_json = orjson.dumps(figure.elements).decode("utf-8")
                if row is None:
                    s.add(FigureRow(
                        id=figure.id,
                        document_id=figure.document_id,
                        page=figure.page,
                        caption=figure.caption,
                        image_path=str(figure.image_path) if figure.image_path else None,
                        crop_box_json=crop_box_json,
                        bounding_regions_json=regions_json,
                        spans_json=spans_json,
                        elements_json=elements_json,
                        interpretation_json=payload,
                        interpretation_model=model,
                        interpretation_title=interpretation.title if interpretation else None,
                        interpretation_chart_type=interpretation.chart_type if interpretation else None,
                        interpretation_confidence=interpretation.confidence if interpretation else None,
                    ))
                else:
                    row.document_id = figure.document_id
                    row.page = figure.page
                    row.caption = figure.caption
                    row.image_path = str(figure.image_path) if figure.image_path else None
                    row.crop_box_json = crop_box_json
                    row.bounding_regions_json = regions_json
                    row.spans_json = spans_json
                    row.elements_json = elements_json
                    row.interpretation_json = payload
                    row.interpretation_model = model
                    row.interpretation_title = interpretation.title if interpretation else None
                    row.interpretation_chart_type = interpretation.chart_type if interpretation else None
                    row.interpretation_confidence = interpretation.confidence if interpretation else None

    def save_chunks(self, chunks: Iterable[Chunk]) -> None:
        with self._sf() as s, s.begin():
            for c in chunks:
                figure_refs_json = orjson.dumps(c.figure_refs).decode("utf-8")
                table_refs_json = orjson.dumps(c.table_refs).decode("utf-8")
                row = s.get(ChunkRow, c.id)
                if row is None:
                    s.add(ChunkRow(
                        id=c.id,
                        document_id=c.document_id,
                        section_id=c.section_id,
                        text=c.text,
                        page_start=c.page_start,
                        page_end=c.page_end,
                        figure_refs_json=figure_refs_json,
                        table_refs_json=table_refs_json,
                        token_estimate=c.token_estimate,
                    ))
                else:
                    row.document_id = c.document_id
                    row.section_id = c.section_id
                    row.text = c.text
                    row.page_start = c.page_start
                    row.page_end = c.page_end
                    row.figure_refs_json = figure_refs_json
                    row.table_refs_json = table_refs_json
                    row.token_estimate = c.token_estimate

    def update_chunk_figure_refs(self, chunk_id: str, figure_refs: list[str]) -> None:
        with self._sf() as s, s.begin():
            row = s.get(ChunkRow, chunk_id)
            if row is not None:
                row.figure_refs_json = orjson.dumps(figure_refs).decode("utf-8")

    def save_extraction(self, chunk: Chunk, result: ExtractionResult) -> None:
        with self._sf() as s, s.begin():
            for e in result.entities:
                if s.get(EntityRow, e.id) is None:
                    s.add(EntityRow(
                        id=e.id,
                        name=e.name,
                        type=e.type,
                        confidence=e.confidence,
                        aliases_json=orjson.dumps(e.aliases).decode("utf-8"),
                    ))
            for r in result.relationships:
                if s.get(RelationshipRow, r.id) is None:
                    s.add(RelationshipRow(
                        id=r.id,
                        source_id=r.source_id,
                        target_id=r.target_id,
                        type=r.type,
                        confidence=r.confidence,
                        chunk_id=chunk.id,
                    ))
            for cl in result.claims:
                row = s.get(ClaimRow, cl.id)
                if row is None:
                    s.add(ClaimRow(
                        id=cl.id,
                        text=cl.text,
                        chunk_id=chunk.id,
                        confidence=cl.confidence,
                        supporting_figure_id=cl.supporting_figure_id,
                        supporting_table_id=cl.supporting_table_id,
                    ))
                else:
                    row.text = cl.text
                    row.chunk_id = chunk.id
                    row.confidence = cl.confidence
                    row.supporting_figure_id = cl.supporting_figure_id
                    row.supporting_table_id = cl.supporting_table_id
            extraction = s.get(ChunkExtractionRow, chunk.id)
            relationship_count = len(result.relationships)
            claim_count = len(result.claims)
            if extraction is None:
                s.add(ChunkExtractionRow(
                    chunk_id=chunk.id,
                    relationship_count=relationship_count,
                    claim_count=claim_count,
                    updated_at=datetime.now(UTC),
                ))
            else:
                extraction.relationship_count = relationship_count
                extraction.claim_count = claim_count
                extraction.updated_at = datetime.now(UTC)

    def needs_chunk_extraction(self, chunk_id: str) -> bool:
        with self._sf() as s:
            extraction = s.get(ChunkExtractionRow, chunk_id)
            if extraction is None:
                return True
            relationship_rows = s.execute(
                select(RelationshipRow.id).where(RelationshipRow.chunk_id == chunk_id)
            ).scalars().all()
            claim_rows = s.execute(
                select(ClaimRow.id).where(ClaimRow.chunk_id == chunk_id)
            ).scalars().all()
            return (
                len(relationship_rows) < extraction.relationship_count
                or len(claim_rows) < extraction.claim_count
            )

    def load_extraction_for_chunk(self, chunk_id: str) -> ExtractionResult:
        with self._sf() as s:
            relationship_rows = s.execute(
                select(RelationshipRow).where(RelationshipRow.chunk_id == chunk_id)
            ).scalars().all()
            claim_rows = s.execute(
                select(ClaimRow).where(ClaimRow.chunk_id == chunk_id)
            ).scalars().all()
            return ExtractionResult(
                chunk_id=chunk_id,
                relationships=[
                    Relationship(
                        id=row.id,
                        source_id=row.source_id,
                        target_id=row.target_id,
                        type=row.type,
                        confidence=row.confidence,
                    )
                    for row in relationship_rows
                ],
                claims=[
                    Claim(
                        id=row.id,
                        text=row.text,
                        confidence=row.confidence,
                        supporting_figure_id=row.supporting_figure_id,
                        supporting_table_id=row.supporting_table_id,
                    )
                    for row in claim_rows
                ],
            )

    def log_prompt_call(
        self,
        *,
        prompt_version: str,
        model: str,
        input_hash: str,
        response_text: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        retries: int = 0,
    ) -> None:
        with self._sf() as s, s.begin():
            existing = s.execute(
                select(PromptCallRow).where(
                    PromptCallRow.prompt_version == prompt_version,
                    PromptCallRow.input_hash == input_hash,
                    PromptCallRow.model == model,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return
            s.add(PromptCallRow(
                prompt_version=prompt_version,
                model=model,
                input_hash=input_hash,
                response_text=response_text,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                retries=retries,
            ))

    def cached_response(self, *, prompt_version: str, model: str, input_hash: str) -> str | None:
        with self._sf() as s:
            row = s.execute(
                select(PromptCallRow).where(
                    PromptCallRow.prompt_version == prompt_version,
                    PromptCallRow.input_hash == input_hash,
                    PromptCallRow.model == model,
                )
            ).scalar_one_or_none()
            return row.response_text if row else None

    def stats(self) -> dict[str, int]:
        with self._sf() as s:
            return {
                "documents": s.query(DocumentRow).count(),
                "tables": s.query(TableRow).count(),
                "table_cells": s.query(TableCellRow).count(),
                "chunks": s.query(ChunkRow).count(),
                "figures": s.query(FigureRow).count(),
                "entities": s.query(EntityRow).count(),
                "relationships": s.query(RelationshipRow).count(),
                "claims": s.query(ClaimRow).count(),
            }

    def list_chunks(self) -> list[Chunk]:
        with self._sf() as s:
            rows = s.execute(select(ChunkRow)).scalars().all()
            return [
                Chunk(
                    id=r.id,
                    document_id=r.document_id,
                    section_id=r.section_id,
                    text=r.text,
                    page_start=r.page_start,
                    page_end=r.page_end,
                    figure_refs=orjson.loads(r.figure_refs_json) if r.figure_refs_json else [],
                    table_refs=orjson.loads(r.table_refs_json) if r.table_refs_json else [],
                    token_estimate=r.token_estimate,
                )
                for r in rows
            ]

    def list_tables(self, document_id: str | None = None) -> list[Table]:
        with self._sf() as s:
            stmt = select(TableRow)
            if document_id is not None:
                stmt = stmt.where(TableRow.document_id == document_id)
            rows = s.execute(stmt.order_by(TableRow.id)).scalars().all()
            tables: list[Table] = []
            for row in rows:
                cell_rows = s.execute(
                    select(TableCellRow).where(TableCellRow.table_id == row.id).order_by(
                        TableCellRow.row_index, TableCellRow.column_index
                    )
                ).scalars().all()
                tables.append(
                    Table(
                        id=row.id,
                        document_id=row.document_id,
                        page=row.page,
                        page_end=row.page_end,
                        caption=row.caption,
                        caption_page=row.caption_page,
                        markdown=row.markdown,
                        bounding_regions=[
                            LayoutBoundingRegion.model_validate(item)
                            for item in (orjson.loads(row.bounding_regions_json) if row.bounding_regions_json else [])
                        ],
                        spans=[
                            LayoutSpan.model_validate(item)
                            for item in (orjson.loads(row.spans_json) if row.spans_json else [])
                        ],
                        cells=[
                            TableCell(
                                row=cell.row_index,
                                col=cell.column_index,
                                text=cell.text,
                                kind=cell.kind,
                                row_span=cell.row_span,
                                col_span=cell.col_span,
                                page=cell.page,
                                span_start=cell.span_start,
                                span_end=cell.span_end,
                                bounding_regions=[
                                    LayoutBoundingRegion.model_validate(item)
                                    for item in (
                                        orjson.loads(cell.bounding_regions_json) if cell.bounding_regions_json else []
                                    )
                                ],
                                spans=[
                                    LayoutSpan.model_validate(item)
                                    for item in (orjson.loads(cell.spans_json) if cell.spans_json else [])
                                ],
                            )
                            for cell in cell_rows
                        ],
                    )
                )
            return tables

    def list_figures(self, document_id: str | None = None) -> list[Figure]:
        with self._sf() as s:
            stmt = select(FigureRow)
            if document_id is not None:
                stmt = stmt.where(FigureRow.document_id == document_id)
            rows = s.execute(stmt.order_by(FigureRow.id)).scalars().all()
            figures: list[Figure] = []
            for row in rows:
                crop_box = orjson.loads(row.crop_box_json) if row.crop_box_json else None
                figures.append(
                    Figure(
                        id=row.id,
                        document_id=row.document_id,
                        page=row.page,
                        caption=row.caption,
                        image_path=Path(row.image_path) if row.image_path else None,
                        crop_box=tuple(crop_box) if crop_box else None,
                        bounding_regions=[
                            LayoutBoundingRegion.model_validate(item)
                            for item in (orjson.loads(row.bounding_regions_json) if row.bounding_regions_json else [])
                        ],
                        spans=[
                            LayoutSpan.model_validate(item)
                            for item in (orjson.loads(row.spans_json) if row.spans_json else [])
                        ],
                        elements=orjson.loads(row.elements_json) if row.elements_json else [],
                    )
                )
            return figures

    def list_claims(self, chunk_id: str | None = None) -> list[Claim]:
        with self._sf() as s:
            stmt = select(ClaimRow)
            if chunk_id is not None:
                stmt = stmt.where(ClaimRow.chunk_id == chunk_id)
            rows = s.execute(stmt.order_by(ClaimRow.id)).scalars().all()
            return [
                Claim(
                    id=row.id,
                    text=row.text,
                    confidence=row.confidence,
                    supporting_figure_id=row.supporting_figure_id,
                    supporting_table_id=row.supporting_table_id,
                )
                for row in rows
            ]


class GovernanceRepository:
    """Ontology versioning, proposals, aliases, drift."""

    def __init__(self, sf: sessionmaker[Session]) -> None:
        self._sf = sf

    def list_versions(self) -> list[OntologyVersion]:
        with self._sf() as s:
            rows = s.execute(select(OntologyVersionRow).order_by(OntologyVersionRow.id)).scalars().all()
            return [self._to_version(r) for r in rows]

    def get_version(self, version: str) -> OntologyVersion | None:
        with self._sf() as s:
            row = s.execute(select(OntologyVersionRow).where(OntologyVersionRow.version == version)).scalar_one_or_none()
            return self._to_version(row) if row else None

    def latest_approved(self) -> OntologyVersion | None:
        with self._sf() as s:
            row = s.execute(
                select(OntologyVersionRow)
                .where(OntologyVersionRow.status == OntologyStatus.APPROVED.value)
                .order_by(OntologyVersionRow.id.desc())
            ).scalars().first()
            return self._to_version(row) if row else None

    def upsert_version(self, version: OntologyVersion) -> OntologyVersion:
        with self._sf() as s, s.begin():
            existing = s.execute(
                select(OntologyVersionRow).where(OntologyVersionRow.version == version.version)
            ).scalar_one_or_none()
            if existing is None:
                row = OntologyVersionRow(
                    version=version.version,
                    status=version.status.value,
                    schema_yaml=version.schema_yaml,
                    created_at=version.created_at,
                    approved_at=version.approved_at,
                    approved_by=version.approved_by,
                )
                s.add(row)
                s.flush()
                version.id = row.id
                return version
            existing.status = version.status.value
            existing.schema_yaml = version.schema_yaml
            existing.approved_at = version.approved_at
            existing.approved_by = version.approved_by
            version.id = existing.id
            return version

    def add_proposal(self, proposal: OntologyProposal) -> OntologyProposal:
        with self._sf() as s, s.begin():
            row = OntologyProposalRow(
                base_version=proposal.base_version,
                source_mode=proposal.source_mode.value,
                schema_yaml=proposal.schema_yaml,
                diff_json=proposal.diff_json,
                confidence=proposal.confidence,
                status=proposal.status.value,
                created_at=proposal.created_at,
            )
            s.add(row)
            s.flush()
            proposal.id = row.id
            return proposal

    def list_proposals(self, status: OntologyStatus | None = None) -> list[OntologyProposal]:
        with self._sf() as s:
            stmt = select(OntologyProposalRow)
            if status is not None:
                stmt = stmt.where(OntologyProposalRow.status == status.value)
            return [self._to_proposal(r) for r in s.execute(stmt).scalars().all()]

    def get_proposal(self, proposal_id: int) -> OntologyProposal | None:
        with self._sf() as s:
            row = s.get(OntologyProposalRow, proposal_id)
            return self._to_proposal(row) if row else None

    def reject_proposal(self, proposal_id: int, reason: str) -> None:
        with self._sf() as s, s.begin():
            row = s.get(OntologyProposalRow, proposal_id)
            if row is None:
                raise ValueError(f"Proposal {proposal_id} not found")
            row.status = OntologyStatus.REJECTED.value
            s.add(OntologyRejectionRow(proposal_id=proposal_id, reason=reason))

    def mark_proposal_approved(self, proposal_id: int) -> None:
        with self._sf() as s, s.begin():
            row = s.get(OntologyProposalRow, proposal_id)
            if row is None:
                raise ValueError(f"Proposal {proposal_id} not found")
            row.status = OntologyStatus.APPROVED.value

    def add_alias(self, canonical_id: str, alias: str, source: str = "governed", confidence: float = 1.0) -> None:
        with self._sf() as s, s.begin():
            s.add(EntityAliasRow(canonical_id=canonical_id, alias=alias, source=source, confidence=confidence))

    def find_canonical(self, alias: str) -> str | None:
        with self._sf() as s:
            row = s.execute(select(EntityAliasRow).where(EntityAliasRow.alias == alias)).scalars().first()
            return row.canonical_id if row else None

    def record_merge(self, surviving_id: str, merged_id: str, reason: str) -> None:
        with self._sf() as s, s.begin():
            s.add(EntityMergeRow(surviving_id=surviving_id, merged_id=merged_id, reason=reason))

    def record_drift(self, event: DriftEvent) -> None:
        with self._sf() as s, s.begin():
            s.add(DriftEventRow(
                version=event.version,
                kind=event.kind.value,
                detail_json=event.detail,
                observed_at=event.observed_at,
            ))

    def drift_summary(self, version: str) -> dict[str, int]:
        with self._sf() as s:
            rows = s.execute(select(DriftEventRow).where(DriftEventRow.version == version)).scalars().all()
            summary: dict[str, int] = {}
            for r in rows:
                summary[r.kind] = summary.get(r.kind, 0) + 1
            return summary

    @staticmethod
    def _to_version(row: OntologyVersionRow | None) -> OntologyVersion | None:
        if row is None:
            return None
        return OntologyVersion(
            id=row.id,
            version=row.version,
            status=OntologyStatus(row.status),
            schema_yaml=row.schema_yaml,
            created_at=row.created_at,
            approved_at=row.approved_at,
            approved_by=row.approved_by,
        )

    @staticmethod
    def _to_proposal(row: OntologyProposalRow | None) -> OntologyProposal | None:
        if row is None:
            return None
        return OntologyProposal(
            id=row.id,
            base_version=row.base_version,
            source_mode=OntologyProposalSource(row.source_mode),
            schema_yaml=row.schema_yaml,
            diff_json=row.diff_json,
            confidence=row.confidence,
            status=OntologyStatus(row.status),
            created_at=row.created_at,
        )


__all__ = [
    "DriftEvent",
    "GovernanceRepository",
    "RelationalRepository",
    "datetime",
    "make_engine",
    "make_session_factory",
]
