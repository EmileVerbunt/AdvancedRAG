"""Ontology candidate generation from Discovery findings + clusters."""
from __future__ import annotations

import contextlib
import hashlib
from datetime import UTC, datetime
from pathlib import Path

import orjson
import yaml

from knowledge_extraction.application.pipelines.stage_2b_extraction_discovery import DiscoveryFindings
from knowledge_extraction.application.pipelines.stage_3_semantic_clustering import ClusterSummary
from knowledge_extraction.application.ports import LLMPort
from knowledge_extraction.application.services.prompt_registry import PromptRegistry
from knowledge_extraction.domain import (
    OntologyProposal,
    OntologyProposalSource,
    OntologyStatus,
)
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import GovernanceRepository


class OntologyProposalPipeline:
    PROMPT_NAME = "ontology_proposal"
    PROMPT_VERSION = "v1"

    def __init__(
        self,
        *,
        llm: LLMPort,
        prompts: PromptRegistry,
        gov: GovernanceRepository,
        model: str,
        artifact_dir: Path,
    ) -> None:
        self._llm = llm
        self._prompts = prompts
        self._gov = gov
        self._model = model
        self._artifact_dir = artifact_dir
        self._artifact_dir.mkdir(parents=True, exist_ok=True)

    async def propose(
        self,
        findings: DiscoveryFindings,
        clusters: list[ClusterSummary],
        base_version: str | None,
    ) -> OntologyProposal:
        entity_stats = [
            {"type": t, "count": c, "samples": findings.entity_examples.get(t, [])[:5]}
            for t, c in findings.entity_type_counter.most_common(40)
        ]
        rel_stats = [
            {"type": t, "count": c, "samples": [list(p) for p in findings.relationship_examples.get(t, [])[:5]]}
            for t, c in findings.relationship_type_counter.most_common(40)
        ]
        cluster_payload = [
            {"id": c.id, "members": c.members[:12]} for c in clusters[:30]
        ]
        prompt = self._prompts.render(
            self.PROMPT_NAME, self.PROMPT_VERSION,
            entity_type_stats_json=orjson.dumps(entity_stats).decode("utf-8"),
            relationship_type_stats_json=orjson.dumps(rel_stats).decode("utf-8"),
            clusters_json=orjson.dumps(cluster_payload).decode("utf-8"),
        )
        resp = await self._llm.complete_json(
            model=self._model, system=prompt.system, user=prompt.user, max_tokens=4096,
        )
        try:
            data = orjson.loads(resp.text)
        except Exception:
            data = {}

        version_suggestion = str(data.get("version_suggestion") or self._next_candidate_version(base_version))
        yaml_doc = {
            "version": version_suggestion,
            "description": data.get("rationale", "Discovery-mode ontology candidate."),
            "entity_types": [
                {"name": e["name"], "description": e.get("description", "")}
                for e in data.get("entity_types", []) if isinstance(e, dict) and e.get("name")
            ],
            "relationship_types": [
                {
                    "name": r["name"],
                    "description": r.get("description", ""),
                    "allowed_source": list(r.get("allowed_source", [])),
                    "allowed_target": list(r.get("allowed_target", [])),
                }
                for r in data.get("relationship_types", []) if isinstance(r, dict) and r.get("name")
            ],
        }
        yaml_text = yaml.safe_dump(yaml_doc, sort_keys=False)
        candidate_index = len(list(self._artifact_dir.glob("ontology_candidate_v*.yaml"))) + 1
        out_path = self._artifact_dir / f"ontology_candidate_v{candidate_index}.yaml"
        out_path.write_text(yaml_text, encoding="utf-8")

        confidence = float(_avg_confidence(data))
        proposal = OntologyProposal(
            base_version=base_version,
            source_mode=OntologyProposalSource.DISCOVERY,
            schema_yaml=yaml_text,
            diff_json=hashlib.sha1(yaml_text.encode("utf-8")).hexdigest(),
            confidence=confidence,
            status=OntologyStatus.PROPOSED,
            created_at=datetime.now(UTC),
        )
        return self._gov.add_proposal(proposal)

    @staticmethod
    def _next_candidate_version(base: str | None) -> str:
        try:
            major, minor, _ = (int(x) for x in (base or "1.0.0").split("."))
            return f"{major}.{minor + 1}.0"
        except Exception:
            return "1.1.0"


def _avg_confidence(data: dict) -> float:
    confidences: list[float] = []
    for k in ("entity_types", "relationship_types"):
        for item in data.get(k, []) or []:
            if isinstance(item, dict) and "confidence" in item:
                with contextlib.suppress(Exception):
                    confidences.append(float(item["confidence"]))
    return sum(confidences) / len(confidences) if confidences else 0.6
