"""Azure OpenAI / Foundry chat client supporting key + DefaultAzureCredential."""
from __future__ import annotations

import time
from dataclasses import dataclass

from openai import AsyncAzureOpenAI
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from knowledge_extraction.config.settings import AzureAuthMode, Settings


@dataclass(slots=True)
class LLMResponse:
    text: str
    input_tokens: int
    output_tokens: int
    latency_ms: int


class AzureFoundryLLM:
    """Async chat client. Uses API key or DefaultAzureCredential per settings."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        if settings.azure_auth_mode is AzureAuthMode.CREDENTIAL:
            from azure.identity import DefaultAzureCredential, get_bearer_token_provider

            credential = DefaultAzureCredential()
            token_provider = get_bearer_token_provider(
                credential, "https://cognitiveservices.azure.com/.default"
            )
            self._client = AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                azure_ad_token_provider=token_provider,
            )
        else:
            self._client = AsyncAzureOpenAI(
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
                api_key=settings.azure_openai_api_key,
            )

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(min=1, max=20),
        retry=retry_if_exception_type(Exception),
    )
    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ) -> LLMResponse:
        from openai import BadRequestError

        from knowledge_extraction.infrastructure.telemetry.observability import wide_event

        is_new_gen = self._is_new_gen_model(model)
        with wide_event(
            "llm.complete_json",
            model=model,
            system_chars=len(system),
            user_chars=len(user),
            max_tokens=max_tokens,
            new_gen=is_new_gen,
        ) as ev:
            t0 = time.perf_counter()
            kwargs: dict[str, object] = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system + "\n\nRespond ONLY with a single valid JSON object."},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
            }
            if is_new_gen:
                kwargs["max_completion_tokens"] = max_tokens
            else:
                kwargs["max_tokens"] = max_tokens
                kwargs["temperature"] = temperature
            try:
                resp = await self._client.chat.completions.create(**kwargs)
                ev["response_format_used"] = "json_object"
            except BadRequestError as e:
                msg = str(e).lower()
                if "unsupported" in msg or "response_format" in msg:
                    kwargs.pop("response_format", None)
                    ev["response_format_fallback"] = True
                    resp = await self._client.chat.completions.create(**kwargs)
                else:
                    raise
            latency_ms = int((time.perf_counter() - t0) * 1000)
            choice = resp.choices[0].message.content or "{}"
            usage = resp.usage
            input_tokens = getattr(usage, "prompt_tokens", 0) if usage else 0
            output_tokens = getattr(usage, "completion_tokens", 0) if usage else 0
            ev["input_tokens"] = input_tokens
            ev["output_tokens"] = output_tokens
            ev["latency_ms"] = latency_ms
            ev["response_chars"] = len(choice)
            return LLMResponse(
                text=_extract_json(choice),
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
            )

    @staticmethod
    def _is_new_gen_model(model: str) -> bool:
        m = model.lower()
        return (
            m.startswith(("gpt-5", "o1", "o3", "o4"))
            or m.startswith("phi-4")
        )


def _extract_json(text: str) -> str:
    """Strip code fences / leading prose so json.loads succeeds even when response_format was rejected."""
    s = text.strip()
    if s.startswith("```"):
        s = s.strip("`")
        # remove optional language tag
        if s.lower().startswith("json"):
            s = s[4:]
        s = s.strip()
    start = s.find("{")
    end = s.rfind("}")
    if start != -1 and end != -1 and end > start:
        return s[start : end + 1]
    return s or "{}"
