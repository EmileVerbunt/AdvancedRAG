"""Multimodal vision adapter for figure interpretation."""
from __future__ import annotations

import base64
import json

import orjson
from openai import AsyncAzureOpenAI

from knowledge_extraction.config.settings import AzureAuthMode, Settings
from knowledge_extraction.domain import (
    ChartAxis,
    ChartInterpretation,
    ChartMetric,
    ChartTrend,
    Figure,
)


class AzureVisionAdapter:
    def __init__(self, settings: Settings, model: str) -> None:
        self._model = model
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

    async def interpret_figure(self, figure: Figure, prompt: str) -> ChartInterpretation:
        from knowledge_extraction.infrastructure.telemetry.observability import wide_event

        if figure.image_path is None or not figure.image_path.exists():
            return ChartInterpretation(figure_id=figure.id)
        with wide_event(
            "vision.interpret_figure",
            model=self._model,
            figure_id=figure.id,
            page=figure.page,
            prompt_chars=len(prompt),
        ) as ev:
            b64 = base64.b64encode(figure.image_path.read_bytes()).decode("ascii")
            kwargs: dict[str, object] = {
                "model": self._model,
                "messages": [
                    {"role": "system", "content": prompt.split("USER:", 1)[0].replace("SYSTEM:", "").strip()},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt.split("USER:", 1)[1].strip() if "USER:" in prompt else ""},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                        ],
                    },
                ],
                "response_format": {"type": "json_object"},
            }
            if self._is_new_gen_model(self._model):
                kwargs["max_completion_tokens"] = 1024
            else:
                kwargs["max_tokens"] = 1024
            resp = await self._client.chat.completions.create(**kwargs)
            text = resp.choices[0].message.content or "{}"
            usage = getattr(resp, "usage", None)
            if usage is not None:
                ev["input_tokens"] = getattr(usage, "prompt_tokens", 0) or 0
                ev["output_tokens"] = getattr(usage, "completion_tokens", 0) or 0
            ev["response_chars"] = len(text)
            try:
                data = orjson.loads(text)
            except Exception:
                data = {}
            return ChartInterpretation(
                figure_id=figure.id,
                title=data.get("title", ""),
                chart_type=data.get("chart_type", ""),
                axes=[ChartAxis(**a) for a in data.get("axes", [])],
                legends=list(data.get("legends", [])),
                metrics=[ChartMetric(**m) for m in data.get("metrics", [])],
                trends=[ChartTrend(**t) for t in data.get("trends", [])],
                interpretation=data.get("interpretation", ""),
                confidence=float(data.get("confidence", 0.0)),
            )

    @staticmethod
    def _safe_json(text: str) -> dict[str, object]:
        try:
            return json.loads(text)
        except Exception:
            return {}

    @staticmethod
    def _is_new_gen_model(model: str) -> bool:
        m = model.lower()
        return (
            m.startswith(("gpt-5", "o1", "o3", "o4"))
            or m.startswith("phi-4")
        )
