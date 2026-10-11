"""Offline contract tests for the OpenAI-compatible adapter (Gemini / Groq / OpenRouter / Ollama).

A mock Chat Completions endpoint (httpx MockTransport) records the exact requests the adapter
sends and replies with canned, API-shaped responses - no API key or network needed.
"""

from __future__ import annotations

import asyncio
import json

import httpx2
import openai
import pytest

from worker.core.task_spec import TaskSpec
from worker.llm.base import LLMError, ToolOutcome
from worker.llm.openai_compat import (OpenAICompatClient, RateLimiter, extract_json, parse_quota_error,
                                      sanitize_schema, to_openai_tool)
from worker.runtime.executor import validate_args
from worker.tools.files import FileWrite
from worker.tools.http_api import HttpRequest
from worker.tools.registry import executor_registry


def _completion(message: dict, finish="tool_calls", prompt_tokens=1200, completion_tokens=80) -> dict:
    return {"id": "c1", "object": "chat.completion", "created": 1, "model": "gemini-test",
            "choices": [{"index": 0, "message": {"role": "assistant", **message}, "finish_reason": finish}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                      "total_tokens": prompt_tokens + completion_tokens}}


# A Gemini-style tool call carrying a provider extra (thought signature) that must be echoed back.
TURN_1 = _completion({"content": "Opening the portal.", "tool_calls": [{
    "id": "call_a", "type": "function",
    "function": {"name": "browser_navigate", "arguments": json.dumps({"url": "http://localhost:8001/login"})},
    "extra_content": {"google": {"thought_signature": "SIG123"}}}]})
TURN_2 = _completion({"content": None, "tool_calls": [{
    "id": "", "type": "function",   # some providers omit ids - the adapter must invent one
    "function": {"name": "finish", "arguments": "{\"status\": \"completed\", \"summary\": \"ok\"}"}}]})


class MockAPI:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[dict] = []

    def handler(self, request: httpx2.Request) -> httpx.Response:
        self.requests.append({"body": json.loads(request.content), "path": request.url.path,
                              "auth": request.headers.get("authorization")})
        status, payload = self.responses.pop(0)
        return httpx2.Response(status, json=payload)


def make_client(api: MockAPI, monkeypatch, provider="gemini", **kw) -> OpenAICompatClient:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    c = OpenAICompatClient(provider, "gemini-test", effort="xhigh", **kw)
    c.client = openai.AsyncOpenAI(api_key="test-key", base_url="http://mock/v1beta/openai/", max_retries=0,
                                  http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(api.handler)))
    return c


@pytest.mark.asyncio
async def test_request_shape_and_append_only_history_with_provider_extras(monkeypatch):
    api = MockAPI([(200, TURN_1), (200, TURN_2)])
    llm = make_client(api, monkeypatch)
    tools = executor_registry().specs()
    conv = llm.conversation("SYSTEM", tools)

    turn = await conv.send(user_text="Do the task")
    req = api.requests[0]["body"]
    assert api.requests[0]["path"].endswith("/chat/completions")
    assert api.requests[0]["auth"] == "Bearer test-key"
    assert req["model"] == "gemini-test"
    assert req["reasoning_effort"] == "high"                       # xhigh mapped to a portable value
    assert req["messages"][:2] == [{"role": "system", "content": "SYSTEM"}, {"role": "user", "content": "Do the task"}]
    names = [t["function"]["name"] for t in req["tools"]]
    assert "browser_navigate" in names and "finish" in names
    assert "additionalProperties" not in json.dumps(req["tools"])   # sanitised for strict providers
    snapshot_tool = next(t for t in req["tools"] if t["function"]["name"] == "browser_snapshot")
    assert "parameters" not in snapshot_tool["function"]            # no-arg tool: parameters omitted

    assert turn.text == "Opening the portal."
    assert [(c.id, c.name, c.args) for c in turn.tool_calls] == [
        ("call_a", "browser_navigate", {"url": "http://localhost:8001/login"})]

    turn2 = await conv.send(tool_outcomes=[ToolOutcome("call_a", "login page", is_error=False)], notes=["NOTE"])
    msgs = api.requests[1]["body"]["messages"]
    assert msgs[:2] == req["messages"][:2]                           # prefix unchanged (append-only)
    assistant = msgs[2]
    assert assistant["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG123"}}
    assert msgs[3] == {"role": "tool", "tool_call_id": "call_a", "content": "login page"}
    assert msgs[4] == {"role": "user", "content": "NOTE"}
    assert turn2.tool_calls[0].name == "finish" and turn2.tool_calls[0].id   # id invented when missing
    assert llm.usage.calls == 2 and llm.usage.cost_usd == 0


@pytest.mark.asyncio
async def test_structured_output_extracts_and_repairs_json(monkeypatch):
    good = {"goal": "g", "deliverable": "d", "systems_involved": ["ERP"], "success_criteria": ["c"],
            "changes_data": True, "constraints_and_risks": [], "plan": ["p"], "blocking_questions": [],
            "out_of_scope": False, "out_of_scope_reason": ""}
    api = MockAPI([(200, _completion({"content": "Sure! here it is: {\"goal\": 1}"}, finish="stop")),
                   (200, _completion({"content": "```json\n" + json.dumps(good) + "\n```"}, finish="stop"))])
    llm = make_client(api, monkeypatch)
    spec = await llm.structured("SYS", "request", TaskSpec)
    assert isinstance(spec, TaskSpec) and spec.changes_data
    assert "JSON schema" in api.requests[0]["body"]["messages"][0]["content"]
    assert "not valid" in api.requests[1]["body"]["messages"][-1]["content"]   # repair round


@pytest.mark.asyncio
async def test_missing_key_and_quota_errors_are_clear(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    llm = OpenAICompatClient("gemini", "gemini-test")
    with pytest.raises(LLMError, match="GEMINI_API_KEY"):
        await llm.conversation("S", []).send(user_text="x")

    llm = make_client(MockAPI([(429, _quota_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier", 20))]),
                      monkeypatch)
    with pytest.raises(LLMError, match="daily free-tier quota.*limit 20"):
        await llm.conversation("S", []).send(user_text="x")


def _quota_429(quota_id: str, value: int, delay: str = "32s") -> list:
    """A free-tier 429 body exactly as Gemini's OpenAI-compatible endpoint returns it (a JSON list)."""
    return [{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                       "message": f"You exceeded your current quota. Please retry in {delay[:-1]}.65s.",
                       "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                    "violations": [{"quotaId": quota_id, "quotaValue": str(value)}]},
                                   {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}]}}]


@pytest.mark.asyncio
async def test_per_minute_429_waits_the_requested_delay_and_learns_the_limit(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("worker.llm.openai_compat.asyncio.sleep", fake_sleep)
    api = MockAPI([(429, _quota_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 3)), (200, TURN_2)])
    llm = make_client(api, monkeypatch, rpm=10)
    waits: list[float] = []
    llm.on_wait = waits.append
    turn = await llm.conversation("S", []).send(user_text="x")
    assert turn.tool_calls[0].name == "finish" and len(api.requests) == 2
    assert waits == [32.0] and slept[-1] >= 32          # the provider's RetryInfo delay, surfaced to the UI
    assert llm.limiter.rpm == 3                          # adopted the real per-minute limit

    # a limit that never clears ends with a clear error after a bounded number of waits
    api = MockAPI([(429, _quota_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", 3))] * 3)
    llm = make_client(api, monkeypatch)
    llm.rate_retries = 2
    with pytest.raises(LLMError, match="after 2 waits"):
        await llm.conversation("S", []).send(user_text="x")
    assert len(api.requests) == 3


def test_parse_quota_error_for_other_provider_formats():
    q = parse_quota_error({"error": {"message": "Rate limit reached for model on tokens per day (TPD). "
                                                "Please try again in 7m12.5s."}})
    assert q.daily and q.limit is None
    q = parse_quota_error({"message": "Too many requests. Please retry in 4.5s"})
    assert not q.daily and q.retry_after == 4.5
    q = parse_quota_error(None, "429 Too Many Requests")
    assert not q.daily and q.retry_after is None


def test_unknown_provider_and_missing_model():
    with pytest.raises(ValueError):
        OpenAICompatClient("nope", "m")
    with pytest.raises(ValueError):
        OpenAICompatClient("openrouter", "")


# --------------------------------------------------------------------------- helpers
def test_sanitize_schema_turns_free_form_objects_into_json_strings():
    http = to_openai_tool(HttpRequest().spec())["function"]["parameters"]
    assert http["properties"]["headers"]["type"] == "string"
    assert http["properties"]["json_body"]["type"] == "string"
    rows = to_openai_tool(FileWrite().spec())["function"]["parameters"]["properties"]["rows"]
    assert rows == {"type": "array", "items": {"type": "string", "description": "(a JSON object encoded as a string)"}}
    nested = sanitize_schema({"type": "object", "additionalProperties": False,
                              "properties": {"a": {"type": "object", "properties": {"b": {"type": "string"}},
                                                   "additionalProperties": False}}})
    assert "additionalProperties" not in json.dumps(nested)


def test_executor_decodes_json_string_arguments():
    args, err = validate_args(HttpRequest(), {"method": "POST", "url": "http://localhost:8002/api/bills",
                                              "headers": '{"X-API-Key": "k"}', "json_body": '{"amount": "1.00"}',
                                              "query": ""})
    assert err is None and args["headers"] == {"X-API-Key": "k"} and args["json_body"] == {"amount": "1.00"}
    assert "query" not in args
    args, err = validate_args(FileWrite(), {"path": "outbox/a.csv", "rows": ['{"a": 1}', '{"a": 2}']})
    assert err is None and args["rows"] == [{"a": 1}, {"a": 2}]
    _, err = validate_args(HttpRequest(), {"__invalid_json__": "{bad"})
    assert "not valid JSON" in err


def test_extract_json():
    assert json.loads(extract_json('noise {"a": {"b": 1}} trailing')) == {"a": {"b": 1}}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_rate_limiter_enforces_requests_and_tokens_per_window():
    now = [0.0]
    waits: list[float] = []

    async def fake_sleep(s):
        waits.append(s)
        now[0] += s

    async def scenario():
        rl = RateLimiter(rpm=2, tpm=1000, window_s=60, clock=lambda: now[0])
        await rl.acquire(100)
        await rl.acquire(100)
        assert not waits
        await rl.acquire(100)            # 3rd request in the same minute -> must wait ~60 s
        assert 59 < sum(waits) < 61
        waits.clear()
        tl = RateLimiter(rpm=0, tpm=1000, window_s=60, clock=lambda: now[0])
        e = await tl.acquire(100)
        RateLimiter.record(e, 950)       # the real usage turned out to be large
        await tl.acquire(100)            # 950 + 100 > 1000 tokens/min -> must wait for the window
        assert 59 < sum(waits) < 61

    import worker.llm.openai_compat as mod
    orig = mod.asyncio.sleep
    mod.asyncio.sleep = fake_sleep
    try:
        asyncio.run(scenario())
    finally:
        mod.asyncio.sleep = orig


# --------------------------------------------------------------------------- fallback chain
DAILY_429 = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded",
                       "details": [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                                    "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                                                    "quotaValue": "500"}]}]}}


def _client(api, monkeypatch, model):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    c = OpenAICompatClient("gemini", model)
    c.client = openai.AsyncOpenAI(api_key="k", base_url="http://mock/", max_retries=0,
                                  http_client=openai.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(api.handler)))
    return c


@pytest.mark.asyncio
async def test_fallback_switches_model_when_daily_quota_is_used_up(monkeypatch):
    from worker.llm import fallback
    from worker.llm.fallback import FallbackClient
    monkeypatch.setattr(fallback, "_EXHAUSTED", {})
    a = MockAPI([(429, DAILY_429)])
    b = MockAPI([(200, TURN_1), (200, TURN_2)])
    llm = FallbackClient([_client(a, monkeypatch, "model-a"), _client(b, monkeypatch, "model-b")])
    switches = []
    llm.on_switch = lambda old, new, why: switches.append((old, new))
    conv = llm.conversation("S", executor_registry().specs())
    t1 = await conv.send(user_text="go")
    assert t1.tool_calls[0].name == "browser_navigate" and llm.model == "model-b"
    assert switches == [("gemini:model-a", "gemini:model-b")]
    await conv.send(tool_outcomes=[ToolOutcome("call_a", "page")])
    assert len(a.requests) == 1                      # the exhausted model is not asked again
    sent = b.requests[1]["body"]["messages"]
    assert all("_by" not in m for m in sent)         # internal tag never sent
    assert sent[2]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "SIG123"}}  # same model
    assert llm.models_used == ["model-b"] and llm.usage.calls == 2


def test_extras_are_dropped_for_a_different_model():
    from worker.llm.openai_compat import prepare_messages
    hist = [{"role": "assistant", "content": "", "_by": "model-a",
             "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"},
                             "extra_content": {"google": {"thought_signature": "S"}}}]}]
    same = prepare_messages(hist, "model-a")[0]
    other = prepare_messages(hist, "model-b")[0]
    dummy = prepare_messages(hist, "model-b", dummy_signatures=True)[0]
    assert same["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "S" and "_by" not in same
    assert "extra_content" not in other["tool_calls"][0]
    assert dummy["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == "skip_thought_signature_validator"


@pytest.mark.asyncio
async def test_fallback_skips_models_that_cannot_take_the_request_and_reports_when_all_fail(monkeypatch):
    from worker.llm import fallback
    from worker.llm.fallback import FallbackClient
    from worker.llm.openai_compat import QuotaExhausted
    monkeypatch.setattr(fallback, "_EXHAUSTED", {})
    too_big = {"error": {"message": "Request too large for model: tokens per minute (TPM): Limit 8000, Requested 15000",
                         "type": "tokens", "code": "rate_limit_exceeded"}}
    a = MockAPI([(413, too_big)])
    b = MockAPI([(429, DAILY_429)])
    llm = FallbackClient([_client(a, monkeypatch, "small"), _client(b, monkeypatch, "spent")])
    with pytest.raises(QuotaExhausted, match="request too large.*quota used up"):
        await llm.conversation("S", []).send(user_text="x")


def test_default_chain_uses_other_gemini_models(monkeypatch):
    from worker.config import get_settings
    from worker.llm import make_llm
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    llm = make_llm(get_settings(llm_provider="gemini", model="gemini-3.5-flash-lite", llm_fallbacks=""))
    assert llm.chain()[0] == "gemini:gemini-3.5-flash-lite" and len(llm.chain()) >= 2
    llm = make_llm(get_settings(llm_provider="gemini", model="m", llm_fallbacks="mistral,none"))
    assert type(llm).__name__ == "OpenAICompatClient"       # mistral skipped (no key) -> single model
    monkeypatch.setenv("MISTRAL_API_KEY", "k2")
    llm = make_llm(get_settings(llm_provider="gemini", model="m", llm_fallbacks="mistral"))
    assert llm.chain() == ["gemini:m", "mistral:mistral-small-latest"]


@pytest.mark.asyncio
async def test_empty_replies_move_the_request_to_the_next_model(monkeypatch):
    """Seen live: gemini-2.5-flash answered with no text and no tool calls (0 output tokens), four times.
    One empty reply is retried; a second one moves this request to the next model in the chain."""
    from worker.llm import fallback
    from worker.llm.fallback import FallbackClient
    monkeypatch.setattr(fallback, "_EXHAUSTED", {})
    empty = _completion({"content": None}, finish="stop", completion_tokens=0)
    a = MockAPI([(200, empty), (200, empty)])
    b = MockAPI([(200, TURN_1)])
    llm = FallbackClient([_client(a, monkeypatch, "mute"), _client(b, monkeypatch, "ok")])
    turn = await llm.conversation("S", executor_registry().specs()).send(user_text="go")
    assert turn.tool_calls[0].name == "browser_navigate" and llm.model == "ok"
    assert len(a.requests) == 2 and fallback._EXHAUSTED == {}       # not marked as out of quota


@pytest.mark.asyncio
async def test_a_busy_model_is_skipped_after_one_short_wait(monkeypatch):
    """Seen live: a free tier answering 429 "Rate limit exceeded" for minutes. With another model in the chain,
    wait once, then send this request to the next model instead of failing the run."""
    from worker.llm import fallback
    from worker.llm.fallback import FallbackClient
    monkeypatch.setattr(fallback, "_EXHAUSTED", {})
    busy = {"error": {"code": 429, "message": "Rate limit exceeded",
                      "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0s"}]}}
    a = MockAPI([(429, busy), (429, busy)])
    b = MockAPI([(200, TURN_1)])
    llm = FallbackClient([_client(a, monkeypatch, "busy"), _client(b, monkeypatch, "free")])
    turn = await llm.conversation("S", executor_registry().specs()).send(user_text="go")
    assert turn.tool_calls[0].name == "browser_navigate" and llm.model == "free"
    assert len(a.requests) == 2 and fallback._EXHAUSTED == {}


def test_providers_that_need_parameters_get_an_empty_schema():
    from worker.llm.openai_compat import to_openai_tool, tools_for
    bare = [to_openai_tool({"name": "snap", "description": "d", "input_schema": {"type": "object", "properties": {}}})]
    assert "parameters" not in tools_for("gemini", bare)[0]["function"]          # Gemini rejects empty objects
    assert tools_for("mistral", bare)[0]["function"]["parameters"] == {"type": "object", "properties": {}}


@pytest.mark.asyncio
async def test_a_dropped_connection_is_retried(monkeypatch):
    """Live: the network was down for a minute at the start of a batch and every case failed instantly."""
    import asyncio as aio
    sleeps = []

    async def fast_sleep(s):
        sleeps.append(s)
    monkeypatch.setattr(aio, "sleep", fast_sleep)
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise httpx2.ConnectError("network down")
        return httpx2.Response(200, json=TURN_1)
    api = MockAPI([])
    api.handler = handler
    llm = _client(api, monkeypatch, "m")
    turn = await llm.conversation("S", executor_registry().specs()).send(user_text="go")
    assert turn.tool_calls[0].name == "browser_navigate" and calls["n"] == 3 and sleeps[:2] == [5, 10]


@pytest.mark.asyncio
async def test_a_provider_overload_is_waited_out(monkeypatch):
    """Live J4: Gemini answered 503 "This model is currently experiencing high demand" and the run ended."""
    import asyncio as aio

    async def fast_sleep(s):
        pass
    monkeypatch.setattr(aio, "sleep", fast_sleep)
    overloaded = {"error": {"code": 503, "message": "This model is currently experiencing high demand.",
                            "status": "UNAVAILABLE"}}
    api = MockAPI([(503, overloaded), (200, TURN_1)])
    turn = await _client(api, monkeypatch, "m").conversation("S", executor_registry().specs()).send(user_text="go")
    assert turn.tool_calls[0].name == "browser_navigate" and len(api.requests) == 2
