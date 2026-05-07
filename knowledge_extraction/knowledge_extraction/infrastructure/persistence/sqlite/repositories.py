"""SQLite engine + repositories."""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

import orjson
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from knowledge_extraction.domain import (
    Chunk,
    Document,
    DriftEvent,
    DriftKind,
    ExtractionResult,
    OntologyProposal,
    OntologyProposalSource,
    OntologyStatus,
    OntologyVersion,
)
from knowledge_extraction.infrastructure.persistence.sqlite.models import (
    Base,
    ChunkRow,
    ClaimRow,
    DocumentRow,
    DriftEventRow,
    EntityAliasRow,
    EntityMergeRow,
    EntityRow,
    OntologyProposalRow,
    OntologyRejectionRow,
    OntologyVersionRow,
    PromptCallRow,
    RelationshipRow,
)


def make_engine(sqlite_path: Path):
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{sqlite_path}", future=True)
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class RelationalRepository:
    """Repository for documents/chunks/extractions."""

    def __init__(self, sf: sessionmaker[Session]) -> None:
        self._sf = sf

    def save_document(self, document: Document) -> None:
        with self._sf() as s, s.begin():
            row = s.get(DocumentRow, document.id)
            if row is None:
                s.add(DocumentRow(
                    id=document.id, title=document.title,
                    source_path=str(document.source_path), page_count=document.page_count,
                ))

    def save_chunks(self, chunks: Iterable[Chunk]) -> None:
        with self._sf() as s, s.begin():
            for c in chunks:
                if s.get(ChunkRow, c.id) is None:
                    s.add(ChunkRow(
                        id=c.id, document_id=c.document_id, section_id=c.section_id,
                        text=c.text, page_start=c.page_start, page_end=c.page_end,
                        token_estimate=c.token_estimate,
                    ))

    def save_extraction(self, chunk: Chunk, result: ExtractionResult) -> None:
        with self._sf() as s, s.begin():
            for e in result.entities:
                if s.get(EntityRow, e.id) is None:
                    s.add(EntityRow(
                        id=e.id, name=e.name, type=e.type, confidence=e.confidence,
                        aliases_json=orjson.dumps(e.aliases).decode("utf-8"),
                    ))
            for r in result.relationships:
                if s.get(RelationshipRow, r.id) is None:
                    s.add(RelationshipRow(
                        id=r.id, source_id=r.source_id, target_id=r.target_id,
                        type=r.type, confidence=r.confidence, chunk_id=chunk.id,
                    ))
            for cl in result.claims:
                if s.get(ClaimRow, cl.id) is None:
                    s.add(ClaimRow(id=cl.id, text=cl.text, chunk_id=chunk.id, confidence=cl.confidence))

    def log_prompt_call(
        self, *, prompt_version: str, model: str, input_hash: str,
        response_text: str, input_tokens: int, output_tokens: int,
        latency_ms: int, retries: int = 0,
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
                prompt_version=prompt_version, model=model, input_hash=input_hash,
                response_text=response_text, input_tokens=input_tokens,
                output_tokens=output_tokens, latency_ms=latency_ms, retries=retries,
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
                "chunks": s.query(ChunkRow).count(),
                "entities": s.query(EntityRow).count(),
                "relationships": s.query(RelationshipRow).count(),
                "claims": s.query(ClaimRow).count(),
            }

    def list_chunks(self) -> list[Chunk]:
        with self._sf() as s:
            rows = s.execute(select(ChunkRow)).scalars().all()
            return [
                Chunk(
                    id=r.id, document_id=r.document_id, section_id=r.section_id,
                    text=r.text, page_start=r.page_start, page_end=r.page_end,
                    token_estimate=r.token_estimate,
                )
                for r in rows
            ]


class GovernanceRepository:
    """Ontology versioning, proposals, aliases, drift."""

    def __init__(self, sf: sessionmaker[Session]) -> None:
        self._sf = sf

    # --- versions ---

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

    # --- proposals ---

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

    # --- aliases ---

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

    # --- drift ---

    def record_drift(self, event: DriftEvent) -> None:
        with self._sf() as s, s.begin():
            s.add(DriftEventRow(
                version=event.version, kind=event.kind.value, detail_json=event.detail,
                observed_at=event.observed_at,
            ))

    def drift_summary(self, version: str) -> dict[str, int]:
        with self._sf() as s:
            rows = s.execute(select(DriftEventRow).where(DriftEventRow.version == version)).scalars().all()
            summary: dict[str, int] = {}
            for r in rows:
                summary[r.kind] = summary.get(r.kind, 0) + 1
            return summary

    # --- helpers ---

    @staticmethod
    def _to_version(row: OntologyVersionRow | None) -> OntologyVersion | None:
        if row is None:
            return None
        return OntologyVersion(
            id=row.id, version=row.version, status=OntologyStatus(row.status),
            schema_yaml=row.schema_yaml, created_at=row.created_at,
            approved_at=row.approved_at, approved_by=row.approved_by,
        )

    @staticmethod
    def _to_proposal(row: OntologyProposalRow | None) -> OntologyProposal | None:
        if row is None:
            return None
        return OntologyProposal(
            id=row.id, base_version=row.base_version,
            source_mode=OntologyProposalSource(row.source_mode),
            schema_yaml=row.schema_yaml, diff_json=row.diff_json,
            confidence=row.confidence, status=OntologyStatus(row.status),
            created_at=row.created_at,
        )


__all__ = [
    "DriftEvent",
    "DriftKind",
    "GovernanceRepository",
    "RelationalRepository",
    "datetime",
    "make_engine",
    "make_session_factory",
]
