"""A chain of OpenAI-compatible models: when one model's free quota runs out, use the next.

Free tiers have small *daily* quotas per model (e.g. Gemini: each model has its own). Instead of a run
failing at 3 pm because the quota is gone, the FallbackClient tries the models of the chain in order:

* it always starts from the first (preferred) model, so it returns to it when its quota is back;
* a model whose daily quota is exhausted is skipped for an hour (remembered process-wide, so the next
  run does not waste a request on it);
* a model that cannot take a request this large (e.g. Groq's free tokens-per-minute cap), or that replies
  with nothing at all (twice), or that is still rate-limited after a short wait, is skipped for that
  request only;
* every switch is reported (``on_switch``) so the trace and the UI show which model did what.

Mid-run switches are safe for the history: each stored reply is tagged with the model that wrote it,
and model-bound extras (Gemini thought signatures) are only sent back to that model (``prepare_messages``).

Configure with LLM_FALLBACKS, e.g. ``gemini:gemini-3.1-flash-lite,gemini:gemini-2.5-flash,mistral``.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .base import Conversation, LLMClient, T
from .openai_compat import (EmptyResponse, OpenAICompatClient, ProviderUnreachable, QuotaExhausted, RateLimited,
                            RequestTooLarge, _CompatConversation)

EXHAUSTED_RECHECK_S = 3600
_EXHAUSTED: dict[tuple[str, str], float] = {}     # (provider, model) -> when its quota was found used up


class FallbackClient(LLMClient):
    def __init__(self, clients: list[OpenAICompatClient]) -> None:
        if not clients:
            raise ValueError("FallbackClient needs at least one client")
        super().__init__(clients[0].model)
        self.clients = clients
        for c in clients[:-1]:          # with a next model to try, do not sit out a busy free tier for minutes
            c.rate_retries = min(c.rate_retries, 1)
        self.active = 0
        self.provider = clients[0].provider
        self.preset = clients[0].preset
        self.last_model = clients[0].model
        self.models_used: list[str] = []
        self.on_switch: Callable[[str, str, str], None] | None = None
        self._on_wait: Callable[[float], None] | None = None

    # The Agent sets on_wait once; every client in the chain should report its waits.
    @property
    def on_wait(self) -> Callable[[float], None] | None:
        return self._on_wait

    @on_wait.setter
    def on_wait(self, fn: Callable[[float], None] | None) -> None:
        self._on_wait = fn
        for c in self.clients:
            c.on_wait = fn

    async def aclose(self) -> None:
        for c in self.clients:
            await c.aclose()

    def chain(self) -> list[str]:
        return [f"{c.provider}:{c.model}" for c in self.clients]

    def conversation(self, system: str, tools: list[dict[str, Any]], purpose: str = "executor") -> Conversation:
        return _CompatConversation(self, system, tools)    # type: ignore[arg-type]

    # structured() only needs self.chat(), so the single-model implementation works for the chain too.
    structured = OpenAICompatClient.structured

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None):
        problems: list[str] = []
        for i, c in enumerate(self.clients):
            key = (c.provider, c.model)
            since = _EXHAUSTED.get(key)
            if since is not None and time.time() - since < EXHAUSTED_RECHECK_S:
                problems.append(f"{c.provider}:{c.model} (quota used up)")
                continue
            try:
                resp, usage = await c.chat(messages, tools)
            except QuotaExhausted:
                _EXHAUSTED[key] = time.time()
                problems.append(f"{c.provider}:{c.model} (quota used up)")
                continue
            except RequestTooLarge:
                problems.append(f"{c.provider}:{c.model} (request too large)")
                continue
            except EmptyResponse:
                problems.append(f"{c.provider}:{c.model} (empty reply)")
                continue
            except RateLimited:
                problems.append(f"{c.provider}:{c.model} (rate-limited)")
                continue
            except ProviderUnreachable:
                problems.append(f"{c.provider}:{c.model} (unreachable)")
                continue
            _EXHAUSTED.pop(key, None)
            if i != self.active and self.on_switch:
                self.on_switch(f"{self.clients[self.active].provider}:{self.clients[self.active].model}",
                               f"{c.provider}:{c.model}", "; ".join(problems) or "preferred model available again")
            self.active, self.model, self.provider, self.preset, self.last_model = i, c.model, c.provider, c.preset, c.model
            if c.model not in self.models_used:
                self.models_used.append(c.model)
            self.usage.add(usage)
            return resp, usage
        raise QuotaExhausted("Every model in the fallback chain is unavailable right now: " + "; ".join(problems) +
                             ". Free quotas reset daily (Gemini: midnight Pacific). Add another provider to "
                             "LLM_FALLBACKS (e.g. mistral or openrouter) or wait.")


def parse_chain(spec: str) -> list[tuple[str, str]]:
    """'gemini:gemini-3.1-flash-lite, mistral, openrouter:meta/x:free' -> [(provider, model or '')]"""
    out = []
    for part in (spec or "").split(","):
        part = part.strip()
        if not part or part.lower() == "none":
            continue
        provider, _, model = part.partition(":")
        out.append((provider.strip().lower(), model.strip()))
    return out
