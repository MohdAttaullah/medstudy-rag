"""Provider selection.

Config-driven and explicit. No configured generator means generation is unavailable, which is not
the same as falling back to some default model — an invented model identity would make the
recorded provenance of a medical draft a fiction.
"""

from typing import TYPE_CHECKING

from app.generation.errors import GenerationError
from app.generation.providers.anthropic import AnthropicProvider
from app.generation.providers.base import LLMProvider
from app.generation.providers.openai import OpenAIProvider

if TYPE_CHECKING:
    from app.core.config import Settings


def build_provider(settings: "Settings") -> LLMProvider:
    selection = settings.generator
    if selection is None:
        raise GenerationError(
            "GENERATION_PROVIDER_UNCONFIGURED", "No generator model is configured."
        )
    key = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }[selection.provider]
    if not key.get_secret_value():
        raise GenerationError(
            "GENERATION_PROVIDER_UNCONFIGURED",
            f"No API key is configured for the {selection.provider} generator.",
        )
    if selection.provider == "openai":
        return OpenAIProvider(selection, settings.provider, settings.grounding, key)
    return AnthropicProvider(selection, settings.provider, settings.grounding, key)
