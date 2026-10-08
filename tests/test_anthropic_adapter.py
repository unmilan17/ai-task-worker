"""Offline contract tests for the Claude adapter.

A mock Messages API endpoint (httpx2 MockTransport) records the exact HTTP requests
the adapter sends and replies with canned API-shaped responses. This verifies,
without an API key, that:

* requests carry the system prompt, tools, adaptive thinking, effort and caching,
* thinking / text / tool_use blocks are parsed into an AssistantTurn,
* the transcript is append-only (request N+1 starts with request N's messages
  plus the assistant turn exactly as returned, thinking signature included),
* tool results reference the right tool_use ids and carry is_error,
* the refusal-fallback beta is dropped automatically if the API rejects it,
* structured() returns a validated pydantic object.
"""

from __future__ import annotations

import json

import anthropic
import httpx2
import pytest

from worker.core.task_spec import TaskSpec
from worker.llm.anthropic_client import FALLBACK_BETA, AnthropicClient
from worker.llm.base import LLMError, ToolOutcome


def _message(content, stop_reason="tool_use"):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
            "content": content, "stop_reason": stop_reason, "stop_sequence": None,
            "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 800,
                      "cache_creation_input_tokens": 100}}


TURN_1 = _message([
    {"type": "thinking", "thinking": "I should open the supplier portal first.", "signature": "sig-abc"},
    {"type": "text", "text": "Opening SupplierHub."},
    {"type": "tool_use", "id": "toolu_01", "name": "browser_navigate", "input": {"url": "http://localhost:8001/login"}},
])
TURN_2 = _message([{"type": "tool_use", "id": "toolu_02", "name": "finish",
                    "input": {"status": "completed", "summary": "done"}}])


class MockAPI:
    def __init__(self, responses: list[tuple[int, dict]]):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content)
        self.requests.append({"body": body, "headers": dict(request.headers), "path": request.url.path})
        status, payload = self.responses.pop(0)
        return httpx2.Response(status, json=payload)


def make_client(api: MockAPI, fallback: bool = True) -> AnthropicClient:
    c = AnthropicClient("claude-opus-5-5", effort="medium", refusal_fallback=fallback)
    c.client = anthropic.AsyncAnthropic(
        api_key="test-key", base_url="http://mock.anthropic", max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(api.handler)))
    return c


TOOLS = [{"name": "browser_navigate", "description": "Open a URL",
          "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
         {"name": "finish", "description": "Finish",
          "input_schema": {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}}]


@pytest.mark.asyncio
async def test_request_shape_parsing_and_append_only_history():
    api = MockAPI([(200, TURN_1), (200, TURN_2)])
    llm = make_client(api)
    conv = llm.conversation("SYSTEM PROMPT", TOOLS)

    turn = await conv.send(user_text="Do the task")
    first = api.requests[0]["body"]
    assert api.requests[0]["path"] == "/v1/messages"
    assert first["model"] == "claude-opus-5-5"
    assert first["system"][0]["text"] == "SYSTEM PROMPT"
    assert [t["name"] for t in first["tools"]] == ["browser_navigate", "finish"]
    assert first["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert first["output_config"] == {"effort": "medium"}
    assert first["cache_control"] == {"type": "ephemeral"}
    assert first["fallbacks"] == "default"
    assert FALLBACK_BETA in api.requests[0]["headers"].get("anthropic-beta", "")
    assert first["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Do the task"}]}]

    assert turn.thoughts == "I should open the supplier portal first."
    assert turn.text == "Opening SupplierHub."
    assert [(c.id, c.name, c.args) for c in turn.tool_calls] == [
        ("toolu_01", "browser_navigate", {"url": "http://localhost:8001/login"})]
    assert llm.usage.cache_read_tokens == 800 and llm.usage.cost_usd > 0

    await conv.send(tool_outcomes=[ToolOutcome("toolu_01", "page text", is_error=False)],
                    notes=["REFLECT: note"])
    second = api.requests[1]["body"]["messages"]
    # Append-only: the earlier prefix is byte-identical, and the assistant turn is replayed verbatim.
    assert second[0] == first["messages"][0]
    assert second[1]["role"] == "assistant"
    assert second[1]["content"][0] == {"type": "thinking", "thinking": TURN_1["content"][0]["thinking"],
                                       "signature": "sig-abc"}
    assert second[1]["content"][2]["id"] == "toolu_01"
    results = second[2]["content"]
    assert results[0]["type"] == "tool_result" and results[0]["tool_use_id"] == "toolu_01"
    assert results[0]["is_error"] is False
    assert results[1] == {"type": "text", "text": "REFLECT: note"}   # notes come after tool results


@pytest.mark.asyncio
async def test_fallback_parameter_is_dropped_if_rejected():
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks: Extra inputs are not permitted"}}
    api = MockAPI([(400, err), (200, TURN_2)])
    llm = make_client(api)
    turn = await llm.conversation("S", TOOLS).send(user_text="x")
    assert turn.tool_calls[0].name == "finish"
    assert "fallbacks" not in api.requests[1]["body"]
    assert llm.refusal_fallback is False


@pytest.mark.asyncio
async def test_other_bad_requests_raise_llm_error():
    err = {"type": "error", "error": {"type": "invalid_request_error", "message": "messages: bad"}}
    llm = make_client(MockAPI([(400, err)]), fallback=False)
    with pytest.raises(LLMError):
        await llm.conversation("S", TOOLS).send(user_text="x")


@pytest.mark.asyncio
async def test_refusal_stop_reason_raises():
    refusal = _message([{"type": "text", "text": ""}], stop_reason="refusal")
    llm = make_client(MockAPI([(200, refusal)]), fallback=False)
    with pytest.raises(LLMError):
        await llm.conversation("S", TOOLS).send(user_text="x")


@pytest.mark.asyncio
async def test_structured_returns_validated_task_spec():
    spec = {"goal": "g", "deliverable": "d", "systems_involved": ["ERP"], "success_criteria": ["c"],
            "changes_data": True, "constraints_and_risks": [], "plan": ["p"], "blocking_questions": [],
            "out_of_scope": False, "out_of_scope_reason": ""}
    api = MockAPI([(200, _message([{"type": "text", "text": json.dumps(spec)}], stop_reason="end_turn"))])
    llm = make_client(api)
    result = await llm.structured("S", "request", TaskSpec)
    assert isinstance(result, TaskSpec) and result.changes_data is True
    sent = api.requests[0]["body"]
    assert sent["output_config"]["format"]["type"] == "json_schema"
