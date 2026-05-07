"""Detect semantic drift signals from validation reports."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from knowledge_extraction.application.services.ontology_validator import ValidationReport
from knowledge_extraction.domain import DriftEvent, DriftKind
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import GovernanceRepository


class DriftDetector:
    def __init__(self, gov: GovernanceRepository, version: str, unknown_threshold: float = 0.15) -> None:
        self._gov = gov
        self._version = version
        self._unknown_threshold = unknown_threshold
        self._unknown_type_counts: Counter[str] = Counter()

    def observe(self, report: ValidationReport, refinement_types: list[str]) -> None:
        now = datetime.now(UTC)
        if report.unknown_rate >= self._unknown_threshold:
            self._gov.record_drift(DriftEvent(
                version=self._version, kind=DriftKind.UNKNOWN_RATE,
                detail={"rate": report.unknown_rate}, observed_at=now,
            ))
        for src, tgt, typ in report.off_schema_relationships:
            self._gov.record_drift(DriftEvent(
                version=self._version, kind=DriftKind.OFF_SCHEMA_RELATION,
                detail={"source": src, "target": tgt, "type": typ}, observed_at=now,
            ))
        for t in refinement_types:
            self._unknown_type_counts[t] += 1
            if self._unknown_type_counts[t] >= 3:
                self._gov.record_drift(DriftEvent(
                    version=self._version, kind=DriftKind.NEW_TYPE_PRESSURE,
                    detail={"candidate_type": t, "count": self._unknown_type_counts[t]},
                    observed_at=now,
                ))
