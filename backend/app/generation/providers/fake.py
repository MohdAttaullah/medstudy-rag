"""Deterministic provider double.

Adapter conformance and the whole draft path are testable without a paid call or a secret, which is
what keeps the safety behaviour under test in ordinary CI rather than only when keys happen to be
configured.
"""

import json
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from app.core.generation_config import GroundingConfig, ProviderConfig
from app.generation.errors import GenerationError
from app.generation.grounding.model import ProviderSpec


class FakeProvider:
    """Returns whatever `respond` produces for the rendered request.

    `respond` receives the evidence rendering so a test can bind claims to the real evidence ids it
    just built, and can equally return an unknown id to exercise the rejection path.
    """

    def __init__(
        self,
        respond: Callable[[str, str], object],
        config: ProviderConfig | None = None,
        grounding: GroundingConfig | None = None,
        model_id: str = "fake-deterministic-1",
    ) -> None:
        self.respond = respond
        self.config = config or ProviderConfig()
        self.grounding = grounding or GroundingConfig()
        self.model_id = model_id
        self.calls: list[tuple[str, str, str]] = []

    @property
    def specification(self) -> ProviderSpec:
        return ProviderSpec(
            provider="fake",
            model_id=self.model_id,
            endpoint="memory://fake",
            temperature=self.config.temperature,
            max_output_tokens=self.config.max_output_tokens,
            schema_version=self.grounding.schema_version,
            prompt_version=self.grounding.prompt_version,
        )

    async def generate(self, *, system_policy: str, question: str, evidence: str) -> str:
        self.calls.append((system_policy, question, evidence))
        result = self.respond(question, evidence)
        return result if isinstance(result, str) else json.dumps(result)

    async def generate_structured[T: BaseModel](
        self, *, system_policy: str, question: str, evidence: str, schema: type[T]
    ) -> T:
        self.calls.append((system_policy, question, evidence))
        result = self.respond(question, evidence)
        if isinstance(result, BaseException):
            raise result
        try:
            return schema.model_validate(result)
        except ValidationError:
            raise GenerationError(
                "GENERATION_SCHEMA_VIOLATION", "Structured output rejected."
            ) from None

    async def analyze_image(
        self, *, system_policy: str, question: str, image: bytes, media_type: str
    ) -> str:
        raise GenerationError(
            "GENERATION_NOT_PERMITTED", "Vision analysis is not approved in this milestone."
        )
