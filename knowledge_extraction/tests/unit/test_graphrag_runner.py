import yaml

from knowledge_extraction.config.settings import AzureAuthMode, Settings
from knowledge_extraction.infrastructure.graphrag.graphrag_runner import GraphRagRunner


def _runner_with(settings: Settings) -> GraphRagRunner:
    runner = GraphRagRunner.__new__(GraphRagRunner)
    runner._settings = settings
    return runner


def _base_settings(auth: AzureAuthMode = AzureAuthMode.KEY) -> Settings:
    return Settings(
        azure_openai_endpoint="https://example.openai.azure.com",
        azure_openai_api_version="2024-10-21",
        azure_openai_extraction_model="gpt-5.4",
        azure_openai_embedding_model="text-embedding-3-small",
        azure_openai_api_key="placeholder-key",
        azure_auth_mode=auth,
    )


def test_azure_settings_yaml_matches_graphrag_2x_schema() -> None:
    runner = _runner_with(_base_settings())
    text = runner._azure_settings_yaml()
    data = yaml.safe_load(text)

    # graphrag 2.x: single top-level `models` dict, nested input.storage.
    assert "models" in data
    assert "completion_models" not in data
    assert "embedding_models" not in data
    assert isinstance(data["input"]["storage"], dict)
    assert "chunks" in data

    chat = data["models"]["default_chat_model"]
    assert chat["model"] == "gpt-5.4"
    assert chat["deployment_name"] == "gpt-5.4"
    assert chat["api_base"] == "https://example.openai.azure.com"

    # Workflow refs use 2.x model_id field (not completion_model_id).
    assert data["extract_graph"]["model_id"] == "default_chat_model"
    assert data["community_reports"]["model_id"] == "default_chat_model"
    assert data["embed_text"]["model_id"] == "default_embedding_model"

    # Explicit workflow list still includes create_community_reports so the
    # query CLI gets the required parquets.
    assert "create_community_reports" in data["workflows"]

    # Vector store schema preserved for lancedb.
    vs = data["vector_store"]["default_vector_store"]
    assert vs["type"] == "lancedb"
    assert vs["embeddings_schema"]["entity.description"]["vector_size"] == 1536


def test_azure_settings_yaml_key_mode_uses_api_key_auth() -> None:
    runner = _runner_with(_base_settings(AzureAuthMode.KEY))
    text = runner._azure_settings_yaml()
    data = yaml.safe_load(text)

    for model in data["models"].values():
        assert model["auth_type"] == "api_key"
        # Templated env var (kept literal in YAML; resolved by graphrag at runtime).
        assert model["api_key"] == "${GRAPHRAG_API_KEY}"


def test_azure_settings_yaml_credential_mode_uses_managed_identity() -> None:
    runner = _runner_with(_base_settings(AzureAuthMode.CREDENTIAL))
    text = runner._azure_settings_yaml()
    data = yaml.safe_load(text)

    # graphrag rejects api_key when auth_type == azure_managed_identity,
    # so the field must be entirely absent.
    for model in data["models"].values():
        assert model["auth_type"] == "azure_managed_identity"
        assert "api_key" not in model
