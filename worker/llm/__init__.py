"""LLM providers behind one interface (see base.py).

* gemini / groq / openrouter / ollama / openai -> openai_compat.OpenAICompatClient (free tiers supported)
* anthropic                                    -> anthropic_client.AnthropicClient
"""

from __future__ import annotations

import os

from ..config import Settings
from .base import LLMClient


def key_status(settings: Settings) -> tuple[bool, str]:
    """Is an API key configured for the selected provider? Returns (ok, message for the user)."""
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        ok = bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"))
        return ok, "" if ok else "ANTHROPIC_API_KEY is not set in .env."
    from .openai_compat import PRESETS
    preset = PRESETS.get(provider)
    if preset is None:
        return False, f"Unknown LLM_PROVIDER '{provider}'."
    if preset.key_env is None or os.getenv(preset.key_env):
        return True, ""
    return False, f"{preset.key_env} is not set in .env - get a free key at {preset.signup}"


def make_llm(settings: Settings) -> LLMClient:
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(settings.model, effort=settings.effort, refusal_fallback=settings.refusal_fallback)
    from .openai_compat import OpenAICompatClient
    return OpenAICompatClient(provider, settings.model, effort=settings.effort, rpm=settings.llm_rpm,
                              tpm=settings.llm_tpm, base_url=settings.llm_base_url or None)
