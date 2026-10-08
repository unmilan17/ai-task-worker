"""OpenAI (Chat Completions) implementation of the LLM interface.

Included to show that the agent is not tied to one vendor: set
LLM_PROVIDER=openai and LLM_MODEL=<model> to use it. It is a thin adapter and
has NOT been tested against the live OpenAI API as part of this submission
(the demo and evals use Claude) - see docs/LIMITATIONS_AND_ROADMAP.md.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from .base import AssistantTurn, Conversation, LLMClient, LLMError, T, ToolCall, ToolOutcome, Usage


class OpenAIClient(LLMClient):
    provider = "openai"

    def __init__(self, model: str, effort: str = "medium") -> None:
        super().__init__(model)
        import openai  # imported lazily so the dependency is optional
        self._openai = openai
        self.client = openai.AsyncOpenAI(max_retries=4)
        self.effort = effort

    def conversation(self, system: str, tools: list[dict[str, Any]], purpose: str = "executor") -> Conversation:
        return _OpenAIConversation(self, system, tools)

    def _account(self, resp) -> Usage:
        u = getattr(resp, "usage", None)
        usage = Usage(input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                      output_tokens=getattr(u, "completion_tokens", 0) or 0, calls=1)
        self.usage.add(usage)
        return usage

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        msgs = [{"role": "system", "content": system + "\n\nRespond ONLY with a JSON object matching this JSON "
                 f"schema:\n{json.dumps(schema.model_json_schema())}"},
                {"role": "user", "content": prompt}]
        try:
            resp = await self.client.chat.completions.create(model=self.model, messages=msgs,
                                                             response_format={"type": "json_object"})
        except self._openai.OpenAIError as e:
            raise LLMError(f"OpenAI structured call failed: {e}") from e
        self._account(resp)
        try:
            return schema.model_validate_json(resp.choices[0].message.content or "{}")
        except Exception as e:  # noqa: BLE001
            raise LLMError(f"OpenAI returned invalid {schema.__name__}: {e}") from e


class _OpenAIConversation(Conversation):
    def __init__(self, llm: OpenAIClient, system: str, tools: list[dict[str, Any]]) -> None:
        self.llm = llm
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self.tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                        "parameters": t["input_schema"]}} for t in tools]

    async def send(self, *, user_text: str | None = None, tool_outcomes: list[ToolOutcome] | None = None,
                   notes: list[str] | None = None) -> AssistantTurn:
        images: list[str] = []
        for o in tool_outcomes or []:
            prefix = "ERROR: " if o.is_error else ""
            self.messages.append({"role": "tool", "tool_call_id": o.call_id, "content": prefix + o.content})
            if o.image_path and Path(o.image_path).exists():
                images.append(base64.standard_b64encode(Path(o.image_path).read_bytes()).decode())
        parts: list[dict[str, Any]] = [{"type": "text", "text": t} for t in [*(notes or []), user_text] if t]
        parts += [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{d}"}} for d in images]
        if parts:
            self.messages.append({"role": "user", "content": parts})
        try:
            resp = await self.llm.client.chat.completions.create(model=self.llm.model, messages=self.messages,
                                                                 tools=self.tools)
        except self.llm._openai.OpenAIError as e:
            raise LLMError(f"OpenAI call failed: {e}") from e
        usage = self.llm._account(resp)
        msg = resp.choices[0].message
        self.messages.append(msg.model_dump(exclude_none=True))
        turn = AssistantTurn(text=msg.content or "", stop_reason=resp.choices[0].finish_reason or "", usage=usage)
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            turn.tool_calls.append(ToolCall(id=tc.id, name=tc.function.name, args=args))
        return turn
