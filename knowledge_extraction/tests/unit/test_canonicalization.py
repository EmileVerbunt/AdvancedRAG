from knowledge_extraction.application.services.canonicalization_service import (
    CanonicalizationService,
    canonical_id,
    normalize,
)


class _FakeGov:
    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    def find_canonical(self, alias: str) -> str | None:
        return self.aliases.get(alias)

    def add_alias(self, canonical_id, alias, source="x", confidence=1.0):
        self.aliases[alias] = canonical_id


def test_normalize_strips_diacritics() -> None:
    assert normalize("OpenAI") == "openai"
    assert normalize("DeepMind  ") == "deepmind"


def test_canonicalization_reuses_known_entity() -> None:
    from knowledge_extraction.domain import Entity

    gov = _FakeGov()
    svc = CanonicalizationService(gov)  # type: ignore[arg-type]
    e1 = Entity(id="x", name="OpenAI", type="Organization")
    e2 = Entity(id="y", name="open ai", type="Organization")
    svc.canonicalize(e1)
    svc.canonicalize(e2)
    assert e1.id == e2.id
    assert e1.id == canonical_id("Organization", "openai")
