"""Provider-agnostic LLM interface.

The agent talks to models only through two abstractions:

* ``Conversation`` - a multi-turn, tool-using chat. The agent calls
  ``send(...)`` with tool results / notes / user text and gets back an
  ``AssistantTurn`` (text, a short thought summary, tool calls, usage).
  Each provider stores its *own native* transcript internally and only ever
  appends to it. That matters for modern reasoning models: Claude returns
  thinking blocks that must be passed back verbatim and in an unchanged
  history, so the transcript must never be rewritten.

* ``LLMClient.structured(...)`` - a single call that returns a validated
  Pydantic object (used to turn the request into a TaskSpec).

Swapping providers (Anthropic <-> OpenAI <-> a scripted fake for tests)
therefore changes nothing in the agent code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


@dataclass
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass
class ToolOutcome:
    call_id: str
    content: str
    is_error: bool = False
    image_path: str | None = None


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0
    cost_usd: float = 0.0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls
        self.cost_usd += other.cost_usd


@dataclass
class AssistantTurn:
    text: str = ""
    thoughts: str = ""                       # readable reasoning summary, if the provider gives one
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: Usage = field(default_factory=Usage)


class LLMError(Exception):
    """Unrecoverable model/provider failure (after the SDK's own retries)."""


class Conversation(ABC):
    @abstractmethod
    async def send(self, *, user_text: str | None = None, tool_outcomes: list[ToolOutcome] | None = None,
                   notes: list[str] | None = None) -> AssistantTurn:
        """Append one user turn (tool results first, then notes, then text) and get the model's reply."""


class LLMClient(ABC):
    provider: str = "base"

    def __init__(self, model: str) -> None:
        self.model = model
        self.usage = Usage()

    @abstractmethod
    def conversation(self, system: str, tools: list[dict[str, Any]], purpose: str = "executor") -> Conversation:
        """Start a new tool-using conversation. ``purpose`` is a label (executor/verifier) used by fakes/logs."""

    @abstractmethod
    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        """One-shot call returning a validated instance of ``schema``."""


# USD per million tokens: (input, output, cache_read, cache_write). Approximate list prices.
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-sonnet-5-5": (2.0, 10.0, 0.20, 2.5),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
}


def estimate_cost(model: str, u: Usage) -> float:
    p = PRICES.get(model)
    if not p:
        return 0.0
    return (u.input_tokens * p[0] + u.output_tokens * p[1] + u.cache_read_tokens * p[2]
            + u.cache_write_tokens * p[3]) / 1_000_000
