"""Anthropic adapter.

Structured output goes through a single forced tool, which is the reliable way to make the Messages
API return an object matching a schema instead of prose that merely looks like JSON.
"""

from typing import Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.core.config import ModelSelection
from app.core.generation_config import GroundingConfig, ProviderConfig
from app.generation.errors import GenerationError
from app.generation.grounding.model import ProviderSpec
from app.generation.prompts.grounded import user_message
from app.generation.providers.openai import _request, _summary

API_BASE = "https://api.anthropic.com/v1"
API_VERSION = "2023-06-01"
TOOL = "record_grounded_draft"


class AnthropicProvider:
    def __init__(
        self,
        selection: ModelSelection,
        config: ProviderConfig,
        grounding: GroundingConfig,
        api_key: SecretStr,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.selection, self.config, self.grounding = selection, config, grounding
        self._key, self._client = api_key, client
        self.base_url = (config.anthropic_base_url or API_BASE).rstrip("/")

    @property
    def specification(self) -> ProviderSpec:
        return ProviderSpec(
            provider="anthropic",
            model_id=self.selection.model_id,
            endpoint=self.base_url,
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            schema_version=self.grounding.schema_version,
            prompt_version=self.grounding.prompt_version,
        )

    async def generate(self, *, system_policy: str, question: str, evidence: str) -> str:
        body = await self._post(system_policy, question, evidence, None)
        for block in body.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                return str(block["text"])
        raise GenerationError("GENERATION_EMPTY")

    async def generate_structured[T: BaseModel](
        self, *, system_policy: str, question: str, evidence: str, schema: type[T]
    ) -> T:
        body = await self._post(system_policy, question, evidence, schema)
        for block in body.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") == TOOL:
                try:
                    return schema.model_validate(block.get("input"))
                except ValidationError as exc:
                    raise GenerationError("GENERATION_SCHEMA_VIOLATION", _summary(exc)) from None
        if body.get("stop_reason") == "max_tokens":
            raise GenerationError(
                "GENERATION_MALFORMED_RESPONSE", "Provider stopped before completing the object."
            )
        raise GenerationError("GENERATION_EMPTY")

    async def analyze_image(
        self, *, system_policy: str, question: str, image: bytes, media_type: str
    ) -> str:
        raise GenerationError(
            "GENERATION_NOT_PERMITTED", "Vision analysis is not approved in this milestone."
        )

    async def _post(
        self, system_policy: str, question: str, evidence: str, schema: type[BaseModel] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.selection.model_id,
            "max_tokens": self.config.max_output_tokens,
            "temperature": self.config.temperature,
            "system": system_policy,
            "messages": [{"role": "user", "content": user_message(question, evidence)}],
        }
        if schema is not None:
            payload["tools"] = [
                {
                    "name": TOOL,
                    "description": "Record the grounded draft and its evidence bindings.",
                    "input_schema": schema.model_json_schema(),
                }
            ]
            payload["tool_choice"] = {"type": "tool", "name": TOOL}
        return await _request(
            self._client,
            self.config,
            self.base_url + "/messages",
            {
                "x-api-key": self._key.get_secret_value(),
                "anthropic-version": API_VERSION,
            },
            payload,
        )
