"""Pydantic settings loaded from environment / .env."""
from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AzureAuthMode(StrEnum):
    KEY = "key"
    CREDENTIAL = "credential"


class ExtractionMode(StrEnum):
    DISCOVERY = "discovery"
    GOVERNED = "governed"


class Settings(BaseSettings):
    """Runtime settings, populated from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Azure OpenAI / Foundry
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = "2024-10-21"
    azure_auth_mode: AzureAuthMode = AzureAuthMode.KEY

    azure_openai_reasoning_model: str = "o4-mini"
    azure_openai_extraction_model: str = "gpt-4.1-mini"
    azure_openai_vision_model: str = "gpt-4.1"
    azure_openai_embedding_model: str = "text-embedding-3-large"

    # Document Intelligence
    azure_document_intelligence_endpoint: str = ""
    azure_document_intelligence_key: str = ""

    # Storage
    graph_storage_path: Path = Path("./work/graph")
    vector_db_path: Path = Path("./work/qdrant")
    checkpoint_path: Path = Path("./work/checkpoints")
    artifact_path: Path = Path("./work/artifacts")
    sqlite_path: Path = Path("./work/knowledge_extraction.db")

    # Qdrant
    qdrant_url: str = ""
    qdrant_api_key: str = ""

    # Telemetry
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""

    # Logging
    log_dir: Path = Path("./work/logs")
    log_console_format: Literal["console", "json"] = "console"

    # Pipeline defaults
    default_mode: ExtractionMode = ExtractionMode.GOVERNED
    active_ontology_version: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Project paths (computed)
    project_root: Path = Field(default_factory=lambda: Path(__file__).resolve().parents[2])

    @property
    def config_dir(self) -> Path:
        return self.project_root / "config"

    @property
    def prompts_dir(self) -> Path:
        return self.config_dir / "prompts"

    @property
    def ontology_yaml_path(self) -> Path:
        return self.config_dir / "ontology.yaml"

    def ensure_dirs(self) -> None:
        for p in (
            self.graph_storage_path,
            self.vector_db_path,
            self.checkpoint_path,
            self.artifact_path,
            self.log_dir,
        ):
            p.mkdir(parents=True, exist_ok=True)
        self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return process-wide Settings singleton."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
