"""ScriptedLLM - a deterministic stand-in for a real model, used in tests.

Agent-loop tests should not depend on a paid API, network access or model
randomness. A ScriptedLLM plays back a script per conversation purpose
("executor", "verifier"). Each script step is either a fixed AssistantTurn or
a function that receives the conversation so far (so it can, for example,
find the ref of the "Sign in" button in the latest page snapshot) and returns
one.

This lets the tests exercise the *real* loop, tools, browser, policy and
sandbox - only the decision-making is scripted.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Union

from .base import AssistantTurn, Conversation, LLMClient, T, ToolCall, ToolOutcome

Step = Union[AssistantTurn, Callable[["ScriptState"], AssistantTurn]]
_ids = itertools.count(1)


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, args=args)


def turn(*calls: ToolCall, text: str = "", thoughts: str = "") -> AssistantTurn:
    return AssistantTurn(text=text, thoughts=thoughts, tool_calls=list(calls), stop_reason="tool_use" if calls else "end_turn")


def find_ref(snapshot: str, pattern: str) -> int:
    """Find the ref number of the first element whose token matches `pattern` (regex, case-insensitive)."""
    for m in re.finditer(r"\[(\d+):([^\]]*)\]", snapshot):
        if re.search(pattern, m.group(2), re.I):
            return int(m.group(1))
    raise AssertionError(f"No element matching {pattern!r} in snapshot:\n{snapshot[:2000]}")


@dataclass
class ScriptState:
    received: list[dict[str, Any]] = field(default_factory=list)   # everything the agent sent

    @property
    def last_outputs(self) -> list[str]:
        return [o.content for o in self.received[-1].get("tool_outcomes", [])] if self.received else []

    @property
    def last_output(self) -> str:
        outs = self.last_outputs
        return outs[-1] if outs else ""

    def all_text(self) -> str:
        chunks = []
        for r in self.received:
            chunks += [o.content for o in r.get("tool_outcomes", [])] + list(r.get("notes", [])) + [r.get("user_text") or ""]
        return "\n".join(chunks)


class ScriptedLLM(LLMClient):
    provider = "scripted"

    def __init__(self, scripts: dict[str, list[Step]], structured_responses: dict[str, Any] | None = None) -> None:
        super().__init__("scripted")
        self.scripts = {k: list(v) for k, v in scripts.items()}
        self.structured_responses = structured_responses or {}
        self.conversations: list[_ScriptedConversation] = []

    def conversation(self, system: str, tools: list[dict[str, Any]], purpose: str = "executor") -> Conversation:
        conv = _ScriptedConversation(self, purpose, system, tools)
        self.conversations.append(conv)
        return conv

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        resp = self.structured_responses[schema.__name__]
        return resp(prompt) if callable(resp) else resp


class _ScriptedConversation(Conversation):
    def __init__(self, llm: ScriptedLLM, purpose: str, system: str, tools: list[dict[str, Any]]) -> None:
        self.llm, self.purpose, self.system, self.tools = llm, purpose, system, tools
        self.state = ScriptState()

    async def send(self, *, user_text: str | None = None, tool_outcomes: list[ToolOutcome] | None = None,
                   notes: list[str] | None = None) -> AssistantTurn:
        self.state.received.append({"user_text": user_text, "tool_outcomes": tool_outcomes or [], "notes": notes or []})
        script = self.llm.scripts.get(self.purpose, [])
        if not script:
            return turn(text="(script exhausted)")
        step = script.pop(0)
        return step(self.state) if callable(step) else step
