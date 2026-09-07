"""Provider contract.

Domain code depends only on this module. No vendor SDK, base URL, header, wire format or error
shape may appear outside `app/generation/providers/`; ADR-005 makes that boundary the reason the
rest of the system can stay provider-independent.
"""

from typing import Protocol

from pydantic import BaseModel

from app.generation.grounding.model import ProviderSpec


class LLMProvider(Protocol):
    @property
    def specification(self) -> ProviderSpec: ...

    async def generate(self, *, system_policy: str, question: str, evidence: str) -> str: ...

    async def generate_structured[T: BaseModel](
        self, *, system_policy: str, question: str, evidence: str, schema: type[T]
    ) -> T: ...

    async def analyze_image(
        self, *, system_policy: str, question: str, image: bytes, media_type: str
    ) -> str: ...
