"""Canonicalize extracted entities against existing ones using aliases + fuzzy match."""
from __future__ import annotations

import hashlib
import re
import unicodedata

from rapidfuzz import fuzz, process

from knowledge_extraction.domain import Entity
from knowledge_extraction.infrastructure.persistence.sqlite.repositories import GovernanceRepository


def normalize(name: str) -> str:
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    n = re.sub(r"[^a-zA-Z0-9]+", " ", n).strip().lower()
    return n


def canonical_id(type_: str, normalized_name: str) -> str:
    digest = hashlib.sha1(f"{type_}::{normalized_name}".encode()).hexdigest()[:16]
    return f"{type_.lower()}:{digest}"


class CanonicalizationService:
    """Reuse existing entities by exact alias, normalized form, then fuzzy match."""

    def __init__(self, gov: GovernanceRepository, fuzz_threshold: int = 92) -> None:
        self._gov = gov
        self._known_by_norm: dict[str, str] = {}
        self._fuzz_threshold = fuzz_threshold

    def register(self, canonical: str, name: str) -> None:
        self._known_by_norm.setdefault(normalize(name), canonical)

    def canonicalize(self, entity: Entity) -> Entity:
        norm = normalize(entity.name)
        # 1) exact alias hit
        existing = self._gov.find_canonical(entity.name) or self._gov.find_canonical(norm)
        if existing:
            entity.id = existing
            return entity
        # 2) normalized exact hit
        if norm in self._known_by_norm:
            entity.id = self._known_by_norm[norm]
            self._gov.add_alias(entity.id, entity.name, source="canonicalization")
            return entity
        # 3) fuzzy match against known names
        if self._known_by_norm:
            choice = process.extractOne(norm, self._known_by_norm.keys(), scorer=fuzz.WRatio)
            if choice and choice[1] >= self._fuzz_threshold:
                cid = self._known_by_norm[choice[0]]
                entity.id = cid
                self._gov.add_alias(cid, entity.name, source="fuzzy", confidence=choice[1] / 100)
                return entity
        # 4) brand new canonical
        new_id = canonical_id(entity.type, norm)
        entity.id = new_id
        self._known_by_norm[norm] = new_id
        self._gov.add_alias(new_id, entity.name, source="new")
        return entity
