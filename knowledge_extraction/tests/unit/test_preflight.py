from pathlib import Path

from knowledge_extraction.cli.main import _collect_preflight_results
from knowledge_extraction.config.settings import AzureAuthMode, Settings


def _make_settings(tmp_path: Path, *, api_key: str) -> Settings:
    config_dir = tmp_path / "config" / "prompts"
    config_dir.mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "ontology.yaml").write_text("version: v1.0.0\n", encoding="utf-8")
    (config_dir / "governed_extraction.v1.j2").write_text("{{ text }}", encoding="utf-8")
    return Settings(
        project_root=tmp_path,
        azure_auth_mode=AzureAuthMode.KEY,
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_key=api_key,
        azure_document_intelligence_endpoint="",
        azure_openai_reasoning_model="o4-mini",
        azure_openai_extraction_model="gpt-4.1-mini",
        azure_openai_vision_model="gpt-4.1",
        azure_openai_embedding_model="text-embedding-3-large",
    )


def test_preflight_non_live_passes_with_valid_key_config(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, api_key="test-key")
    results = _collect_preflight_results(settings, live=False, include_graphrag=False)
    by_check = {result.check: result for result in results}
    assert by_check["azure openai endpoint"].status == "PASS"
    assert by_check["azure auth"].status == "PASS"
    assert by_check["ontology config"].status == "PASS"
    assert by_check["prompt templates"].status == "PASS"
    assert by_check["document intelligence"].status == "WARN"


def test_preflight_non_live_fails_when_key_mode_missing_api_key(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path, api_key="")
    results = _collect_preflight_results(settings, live=False, include_graphrag=False)
    by_check = {result.check: result for result in results}
    assert by_check["azure auth"].status == "FAIL"
