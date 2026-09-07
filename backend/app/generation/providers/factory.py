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
from app.observability.usage import UsageSink

if TYPE_CHECKING:
    from app.core.config import ModelSelection, Settings
    from app.core.generation_config import GroundingConfig


def build_provider(settings: "Settings", usage: "UsageSink | None" = None) -> LLMProvider:
    selection = settings.generator
    if selection is None:
        raise GenerationError(
            "GENERATION_PROVIDER_UNCONFIGURED", "No generator model is configured."
        )
    return build_named_provider(settings, selection, settings.grounding, usage)


def build_named_provider(
    settings: "Settings",
    selection: "ModelSelection",
    grounding: "GroundingConfig",
    usage: "UsageSink | None" = None,
) -> LLMProvider:
    """Build an adapter for one explicitly named selection.

    Taking the selection as an argument is what lets the M8 verifier run on a different provider or
    model from the generator without either stage reaching for the other's configuration.
    """
    key = {
        "openai": settings.openai_api_key,
        "anthropic": settings.anthropic_api_key,
    }[selection.provider]
    if not key.get_secret_value():
        raise GenerationError(
            "GENERATION_PROVIDER_UNCONFIGURED",
            f"No API key is configured for the {selection.provider} provider.",
        )
    if selection.provider == "openai":
        return OpenAIProvider(selection, settings.provider, grounding, key, usage=usage)
    return AnthropicProvider(selection, settings.provider, grounding, key, usage=usage)
