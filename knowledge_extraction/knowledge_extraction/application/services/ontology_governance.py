"""Ontology governance facade: approve/reject/diff/migrate."""
from __future__ import annotations

import difflib
from datetime import UTC, datetime

import yaml

from knowledge_extraction.domain import OntologyStatus, OntologyVersion
from knowledge_extraction.domain.ontology import OntologyProposalSource
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import GovernanceRepository


class OntologyGovernance:
    def __init__(self, gov: GovernanceRepository) -> None:
        self._gov = gov

    # versions

    def list_versions(self) -> list[OntologyVersion]:
        return self._gov.list_versions()

    def show(self, version: str) -> OntologyVersion | None:
        return self._gov.get_version(version)

    def diff(self, a: str, b: str) -> str:
        ra = self._gov.get_version(a)
        rb = self._gov.get_version(b)
        if ra is None or rb is None:
            raise ValueError("version(s) not found")
        return "\n".join(difflib.unified_diff(
            ra.schema_yaml.splitlines(), rb.schema_yaml.splitlines(),
            fromfile=a, tofile=b, lineterm="",
        ))

    # proposals

    def list_proposals(self, status: OntologyStatus | None = None):
        return self._gov.list_proposals(status)

    def reject(self, proposal_id: int, reason: str) -> None:
        self._gov.reject_proposal(proposal_id, reason)

    def approve(self, proposal_id: int, approved_by: str = "cli") -> OntologyVersion:
        proposal = self._gov.get_proposal(proposal_id)
        if proposal is None:
            raise ValueError(f"Proposal {proposal_id} not found")
        # Decide the new version number: bump from the highest existing approved
        # version when the proposal's baked-in version would collide (or is missing).
        raw = yaml.safe_load(proposal.schema_yaml) or {}
        baked = str(raw.get("version") or "").strip()
        existing = {v.version for v in self._gov.list_versions()}
        if baked and baked not in existing:
            new_version = baked
        else:
            highest = max(existing, key=_version_key, default=proposal.base_version or "1.0.0")
            new_version = self._next_version(highest)
        # Patch the YAML so its `version:` matches the assigned version.
        raw["version"] = new_version
        new_yaml = yaml.safe_dump(raw, sort_keys=False)
        version = OntologyVersion(
            version=new_version,
            status=OntologyStatus.APPROVED,
            schema_yaml=new_yaml,
            created_at=datetime.now(UTC),
            approved_at=datetime.now(UTC),
            approved_by=approved_by,
        )
        result = self._gov.upsert_version(version)
        self._gov.mark_proposal_approved(proposal_id)
        return result

    def propose_from_yaml(self, yaml_text: str, base_version: str | None,
                          source: OntologyProposalSource, confidence: float = 0.7):
        load_ontology_text(yaml_text)  # validate parses
        from knowledge_extraction.domain import OntologyProposal
        proposal = OntologyProposal(
            base_version=base_version,
            source_mode=source,
            schema_yaml=yaml_text,
            confidence=confidence,
            created_at=datetime.now(UTC),
        )
        return self._gov.add_proposal(proposal)

    @staticmethod
    def _next_version(base: str | None) -> str:
        if not base:
            return "1.1.0"
        try:
            major, minor, _patch = (int(x) for x in base.split("."))
            return f"{major}.{minor + 1}.0"
        except Exception:
            return "1.1.0"


def load_ontology_text(yaml_text: str) -> None:
    """Validate that an ontology YAML string parses into a schema."""
    raw = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict):
        raise ValueError("ontology YAML must be a mapping")
    if "entity_types" not in raw or "relationship_types" not in raw:
        raise ValueError("ontology YAML must include entity_types and relationship_types")


def _version_key(v: str) -> tuple[int, int, int]:
    try:
        major, minor, patch = (int(x) for x in v.split("."))
        return (major, minor, patch)
    except Exception:
        return (0, 0, 0)
