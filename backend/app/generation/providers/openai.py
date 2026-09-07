"""OpenAI adapter.

Talks the Chat Completions wire format over the httpx client the repository already depends on,
rather than pulling the vendor SDK into an image that otherwise carries no model libraries. The
isolation ADR-005 asks for is about where vendor detail may live, and it all lives here.
"""

import json
from typing import Any

import httpx
from pydantic import BaseModel, SecretStr, ValidationError

from app.core.config import ModelSelection
from app.core.generation_config import GroundingConfig, ProviderConfig
from app.generation.errors import GenerationError
from app.generation.grounding.model import ProviderSpec
from app.generation.prompts.grounded import user_message

API_BASE = "https://api.openai.com/v1"


class OpenAIProvider:
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
        self.base_url = (config.openai_base_url or API_BASE).rstrip("/")

    @property
    def specification(self) -> ProviderSpec:
        return ProviderSpec(
            provider="openai",
            model_id=self.selection.model_id,
            endpoint=self.base_url,
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            schema_version=self.grounding.schema_version,
            prompt_version=self.grounding.prompt_version,
        )

    async def generate(self, *, system_policy: str, question: str, evidence: str) -> str:
        body = await self._post(system_policy, question, evidence, None)
        return str(_content(body))

    async def generate_structured[T: BaseModel](
        self, *, system_policy: str, question: str, evidence: str, schema: type[T]
    ) -> T:
        body = await self._post(system_policy, question, evidence, schema)
        raw = _content(body)
        try:
            return schema.model_validate_json(raw)
        except ValidationError as exc:
            raise GenerationError("GENERATION_SCHEMA_VIOLATION", _summary(exc)) from None
        except ValueError:
            raise GenerationError("GENERATION_MALFORMED_RESPONSE") from None

    async def analyze_image(
        self, *, system_policy: str, question: str, image: bytes, media_type: str
    ) -> str:
        # M7 declares no approved vision-analysis path. The gate abstains before reaching here;
        # this exists so the capability cannot be enabled by accident.
        raise GenerationError(
            "GENERATION_NOT_PERMITTED", "Vision analysis is not approved in this milestone."
        )

    async def _post(
        self, system_policy: str, question: str, evidence: str, schema: type[BaseModel] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.selection.model_id,
            "max_completion_tokens": self.config.max_output_tokens,
            "messages": [
                {"role": "system", "content": system_policy},
                {"role": "user", "content": user_message(question, evidence)},
            ],
        }
        if self.config.temperature is not None:
            payload["temperature"] = self.config.temperature
        if schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "grounded_draft",
                    "strict": False,
                    "schema": schema.model_json_schema(),
                },
            }
        return await _request(
            self._client,
            self.config,
            self.base_url + "/chat/completions",
            {"Authorization": "Bearer " + self._key.get_secret_value()},
            payload,
        )


def _content(body: dict[str, Any]) -> str:
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise GenerationError("GENERATION_MALFORMED_RESPONSE") from None
    if not isinstance(text, str) or not text.strip():
        raise GenerationError("GENERATION_EMPTY")
    return text


def _rejection(response: httpx.Response) -> str:
    """Field names only, never the provider's prose, which can echo the prompt."""
    try:
        body = response.json()
    except ValueError:
        return "no machine-readable detail"
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return "no machine-readable detail"
    parts = [str(error[key]) for key in ("code", "param") if error.get(key)]
    return " ".join(parts) or str(error.get("type") or "no machine-readable detail")


def _summary(exc: ValidationError) -> str:
    """Field paths only. Provider output can contain evidence text and must not reach a log."""
    return "Structured output rejected at: " + ", ".join(
        ".".join(str(part) for part in error["loc"]) or "root" for error in exc.errors()[:5]
    )


async def _request(
    client: httpx.AsyncClient | None,
    config: ProviderConfig,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    owned = client is None
    http = client or httpx.AsyncClient(timeout=config.request_timeout_seconds)
    try:
        response = await http.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise GenerationError("GENERATION_PROVIDER_TIMEOUT") from None
    except httpx.HTTPError:
        raise GenerationError("GENERATION_PROVIDER_UNAVAILABLE") from None
    finally:
        if owned:
            await http.aclose()
    if response.status_code in (401, 403):
        raise GenerationError("GENERATION_PROVIDER_AUTH_FAILED")
    if response.status_code == 429:
        raise GenerationError("GENERATION_RATE_LIMITED")
    if 400 <= response.status_code < 500:
        # A 4xx is a rejected request, not an outage, and calling it unavailability sends whoever
        # reads the log looking for a down provider instead of the malformed field. The provider's
        # own message can quote the prompt back, so only the machine-readable field names are
        # carried through: a code and a parameter path name nothing the user asked.
        raise GenerationError(
            "GENERATION_PROVIDER_REJECTED_REQUEST",
            f"Provider rejected the request ({response.status_code}): {_rejection(response)}.",
        )
    if response.status_code >= 500:
        raise GenerationError(
            "GENERATION_PROVIDER_UNAVAILABLE", f"Provider returned {response.status_code}."
        )
    try:
        body = response.json()
    except json.JSONDecodeError:
        raise GenerationError("GENERATION_MALFORMED_RESPONSE") from None
    if not isinstance(body, dict):
        raise GenerationError("GENERATION_MALFORMED_RESPONSE")
    return body
