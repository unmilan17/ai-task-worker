"""LLM providers behind one interface (see base.py)."""

from __future__ import annotations

from ..config import Settings
from .base import LLMClient


def make_llm(settings: Settings) -> LLMClient:
    provider = settings.llm_provider.lower()
    if provider == "anthropic":
        from .anthropic_client import AnthropicClient
        return AnthropicClient(settings.model, effort=settings.effort, refusal_fallback=settings.refusal_fallback)
    if provider == "openai":
        from .openai_client import OpenAIClient
        return OpenAIClient(settings.model, effort=settings.effort)
    raise ValueError(f"Unknown LLM_PROVIDER '{settings.llm_provider}' (expected 'anthropic' or 'openai')")
