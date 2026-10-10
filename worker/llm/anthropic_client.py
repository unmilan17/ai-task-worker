"""Claude (Anthropic API) implementation of the LLM interface.

Notable choices, all explainable in an interview:

* **Append-only transcript.** ``response.content`` (including thinking blocks)
  is appended verbatim and earlier turns are never edited. Claude's thinking
  blocks are bound to the exact conversation prefix that produced them; editing
  history would invalidate them (and the prompt cache). Notes such as loop
  warnings are therefore *appended* as text blocks after the tool results.
* **Adaptive thinking with summarized display.** The model decides how much to
  think per step; the readable summary is streamed to the UI as the agent's
  "thoughts" so a human can follow its reasoning.
* **Effort** (``output_config.effort``) trades cost for depth; default medium.
* **Prompt caching** (top-level ``cache_control``) caches the growing prefix,
  so each step only pays full price for the newest tokens.
* **Refusal fallback** (server-side, opt-in beta): if a safety classifier
  declines a request, the API re-runs it on a fallback model instead of the
  step failing. Disabled automatically if the API rejects the parameter.
* **No forced tool_choice** (not supported on current models): the prompt
  steers the model to call tools, and the loop nudges it if it doesn't.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import anthropic

from .base import (AssistantTurn, Conversation, LLMClient, LLMError, T, ToolCall, ToolOutcome, Usage,
                   estimate_cost)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
NO_CREDENTIALS = "No Anthropic credentials found - set ANTHROPIC_API_KEY in .env (see .env.example)."


def _is_missing_credentials(e: Exception) -> bool:
    return isinstance(e, TypeError) and "authentication method" in str(e)


class AnthropicClient(LLMClient):
    provider = "anthropic"

    def __init__(self, model: str, effort: str = "medium", refusal_fallback: bool = True,
                 max_tokens: int = 16000) -> None:
        super().__init__(model)
        self.client = anthropic.AsyncAnthropic(max_retries=4, timeout=300)
        self.effort = effort
        self.refusal_fallback = refusal_fallback
        self.max_tokens = max_tokens

    async def aclose(self) -> None:
        await self.client.close()

    def conversation(self, system: str, tools: list[dict[str, Any]], purpose: str = "executor") -> Conversation:
        return _AnthropicConversation(self, system, tools)

    def _account(self, resp) -> Usage:
        u = resp.usage
        usage = Usage(input_tokens=u.input_tokens or 0, output_tokens=u.output_tokens or 0,
                      cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                      cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0, calls=1)
        usage.cost_usd = estimate_cost(self.model, usage)
        self.usage.add(usage)
        return usage

    async def create(self, **kwargs: Any):
        """messages.create with the optional refusal-fallback beta and clean error mapping."""
        if self.refusal_fallback:
            kwargs = {**kwargs, "extra_headers": {"anthropic-beta": FALLBACK_BETA},
                      "extra_body": {"fallbacks": "default"}}
        try:
            return await self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as e:
            if self.refusal_fallback and "fallback" in str(e).lower():
                self.refusal_fallback = False      # API/account doesn't support it - continue without
                kwargs.pop("extra_headers", None)
                kwargs.pop("extra_body", None)
                return await self.client.messages.create(**kwargs)
            raise LLMError(f"Anthropic rejected the request: {e}") from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic authentication failed - set ANTHROPIC_API_KEY in .env") from e
        except (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.InternalServerError) as e:
            raise LLMError(f"Anthropic API unavailable after retries: {e}") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e}") from e
        except TypeError as e:
            if _is_missing_credentials(e):
                raise LLMError(NO_CREDENTIALS) from e
            raise

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        try:
            resp = await self.client.messages.parse(
                model=self.model, max_tokens=self.max_tokens, system=system,
                messages=[{"role": "user", "content": prompt}], output_format=schema)
        except anthropic.APIError as e:
            raise LLMError(f"Structured call failed: {e}") from e
        except TypeError as e:
            if _is_missing_credentials(e):
                raise LLMError(NO_CREDENTIALS) from e
            raise
        self._account(resp)
        if resp.stop_reason == "refusal" or resp.parsed_output is None:
            raise LLMError(f"Model did not return a valid {schema.__name__} (stop_reason={resp.stop_reason}).")
        return resp.parsed_output


class _AnthropicConversation(Conversation):
    def __init__(self, llm: AnthropicClient, system: str, tools: list[dict[str, Any]]) -> None:
        self.llm = llm
        # Frozen at the start of the conversation (changing them mid-run would break caching/thinking).
        self.system = [{"type": "text", "text": system}]
        self.tools = [{"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
                      for t in tools]
        self.messages: list[dict[str, Any]] = []

    async def send(self, *, user_text: str | None = None, tool_outcomes: list[ToolOutcome] | None = None,
                   notes: list[str] | None = None) -> AssistantTurn:
        content: list[dict[str, Any]] = []
        for o in tool_outcomes or []:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": o.content or "(empty)"}]
            if o.image_path and Path(o.image_path).exists():
                data = base64.standard_b64encode(Path(o.image_path).read_bytes()).decode()
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}})
            content.append({"type": "tool_result", "tool_use_id": o.call_id, "content": blocks,
                            "is_error": o.is_error})
        for n in notes or []:
            content.append({"type": "text", "text": n})
        if user_text:
            content.append({"type": "text", "text": user_text})
        if not content:
            content.append({"type": "text", "text": "Continue."})
        self.messages.append({"role": "user", "content": content})

        resp = await self.llm.create(
            model=self.llm.model, max_tokens=self.llm.max_tokens, system=self.system, tools=self.tools,
            messages=self.messages, thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.llm.effort}, cache_control={"type": "ephemeral"})
        usage = self.llm._account(resp)

        if resp.stop_reason == "refusal":
            details = getattr(resp, "stop_details", None)
            raise LLMError(f"The model declined to continue (refusal: {getattr(details, 'category', None)}).")

        # Append the assistant turn exactly as returned (thinking blocks included).
        self.messages.append({"role": "assistant", "content": resp.content or [{"type": "text", "text": "(no output)"}]})

        turn = AssistantTurn(stop_reason=resp.stop_reason or "", usage=usage)
        texts, thoughts = [], []
        for b in resp.content:
            if b.type == "text":
                texts.append(b.text)
            elif b.type == "thinking" and getattr(b, "thinking", ""):
                thoughts.append(b.thinking)
            elif b.type == "tool_use":
                turn.tool_calls.append(ToolCall(id=b.id, name=b.name, args=dict(b.input or {})))
        turn.text = "\n".join(texts).strip()
        turn.thoughts = "\n".join(thoughts).strip()
        return turn
