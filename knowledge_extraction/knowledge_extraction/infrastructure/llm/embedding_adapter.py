"""Embedding adapter for Azure OpenAI."""
from __future__ import annotations

from collections.abc import Sequence

from openai import AsyncAzureOpenAI

from knowledge_extraction.config.settings import AzureAuthMode, Settings


class AzureEmbeddingAdapter:
    def __init__(self, settings: Settings) -> None:
        if settings.azure_auth_mode is AzureAuthMode.CREDENTIAL:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            credential = DefaultAzureCredential()
            tp = get_bearer_token_provider(credential, "https://cognitiveservices.azure.com/.default")
            self._client = AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                azure_ad_token_provider=tp,
            )
        else:
            self._client = AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                api_key=settings.azure_openai_api_key,
            )

    async def embed(self, texts: Sequence[str], *, model: str) -> list[list[float]]:
        if not texts:
            return []
        from knowledge_extraction.infrastructure.telemetry.observability import wide_event

        with wide_event("embedding.embed", model=model, batch_size=len(texts)) as ev:
            ev["input_chars"] = sum(len(t) for t in texts)
            resp = await self._client.embeddings.create(model=model, input=list(texts))
            vectors = [list(d.embedding) for d in resp.data]
            ev["dims"] = len(vectors[0]) if vectors else 0
            usage = getattr(resp, "usage", None)
            if usage is not None:
                ev["input_tokens"] = getattr(usage, "prompt_tokens", 0) or 0
            return vectors
