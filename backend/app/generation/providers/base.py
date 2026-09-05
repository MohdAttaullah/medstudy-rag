"""Provider contract only. No vendor adapter or medical generation is implemented in M0."""

from typing import Protocol

from pydantic import BaseModel


class LLMProvider(Protocol):
    async def generate(self, *, system_policy: str, question: str, evidence: str) -> str: ...

    async def generate_structured[T: BaseModel](
        self, *, system_policy: str, question: str, evidence: str, schema: type[T]
    ) -> T: ...

    async def analyze_image(
        self, *, system_policy: str, question: str, image: bytes, media_type: str
    ) -> str: ...
