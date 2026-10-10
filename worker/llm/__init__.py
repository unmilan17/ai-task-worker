"""LLM providers behind one interface (see base.py).

* gemini / groq / openrouter / ollama / openai -> openai_compat.OpenAICompatClient (free tiers supported)
* anthropic                                    -> anthropic_client.AnthropicClient
"""

from __future__ import annotations

import os

from ..config import DEFAULT_MODELS, Settings
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
    from ..config import DEFAULT_FALLBACKS
    from .fallback import FallbackClient, parse_chain
    from .openai_compat import PRESETS, OpenAICompatClient

    primary = OpenAICompatClient(provider, settings.model, effort=settings.effort, rpm=settings.llm_rpm,
                                 tpm=settings.llm_tpm, base_url=settings.llm_base_url or None)
    spec = settings.llm_fallbacks or DEFAULT_FALLBACKS.get(provider, "")
    clients = [primary]
    for fb_provider, fb_model in parse_chain(spec):
        preset = PRESETS.get(fb_provider)
        if preset is None or (preset.key_env and not os.getenv(preset.key_env)):
            continue                                   # unknown provider or no key: skip silently
        model = fb_model or DEFAULT_MODELS.get(fb_provider, "")
        if not model or (fb_provider, model) in {(c.provider, c.model) for c in clients}:
            continue
        clients.append(OpenAICompatClient(fb_provider, model, effort=settings.effort))
    return clients[0] if len(clients) == 1 else FallbackClient(clients)
