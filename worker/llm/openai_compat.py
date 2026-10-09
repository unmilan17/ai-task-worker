"""OpenAI-compatible LLM adapter - one implementation for many (free-tier) providers.

Google Gemini, Groq, OpenRouter, Ollama and OpenAI all expose the same
"Chat Completions" HTTP API, so a single adapter covers them all. Only a
preset differs: base URL, API-key variable, default rate limits, and a few
capability flags.

Default: **Google Gemini free tier** (an API key from Google AI Studio; no credit card).

Things this adapter handles so the agent loop does not have to:

* **Free-tier rate limits.** A client-side limiter spaces requests to stay
  under the provider's requests-per-minute and tokens-per-minute quotas
  (agents send many large requests in a row, which is exactly what free
  tiers throttle). A 429 that still happens is read: per-minute limits are waited
  out for the delay the provider asks for (and the limiter adopts the reported
  limit); an exhausted *daily* quota stops the run at once with a clear message.
* **Append-only history with provider extras preserved.** Assistant turns are
  stored as returned, including provider-specific fields on tool calls (Gemini
  attaches reasoning "thought signatures" there, which must be echoed back
  unchanged on the next request).
* **Portable tool schemas.** Some providers reject JSON-schema features such as
  ``additionalProperties`` or free-form objects. Schemas are sanitised and
  free-form objects become JSON-encoded strings (decoded again by the executor).
* **Portable structured output.** Instead of provider-specific "JSON mode", the
  model is asked for JSON, the reply is extracted and validated with pydantic,
  and there is one automatic repair attempt on invalid output.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .base import AssistantTurn, Conversation, LLMClient, LLMError, T, ToolCall, ToolOutcome, Usage


@dataclass(frozen=True)
class Preset:
    base_url: str | None
    key_env: str | None          # environment variable holding the API key (None = no key needed)
    rpm: int                     # default requests per minute (0 = unlimited)
    tpm: int                     # default tokens per minute (0 = unlimited)
    reasoning_effort: bool       # accepts the `reasoning_effort` parameter
    vision: bool                 # accepts images in user messages
    signup: str                  # where to get a key (shown in error messages)


PRESETS: dict[str, Preset] = {
    # Free tier: Flash / Flash-Lite models, no credit card. Limits are per project and model and change
    # over time; check them in Google AI Studio. Measured 2026-10: gemini-3.5-flash-lite (the default)
    # took 12+ requests/minute and 500/day; gemini-3.8-flash allowed only 5/minute and 20/day.
    # The limiter starts at 15/minute and lowers itself to whatever limit a 429 response reports.
    "gemini": Preset("https://generativelanguage.googleapis.com/v1beta/openai/", "GEMINI_API_KEY",
                     rpm=15, tpm=200_000, reasoning_effort=True, vision=True,
                     signup="https://aistudio.google.com/apikey"),
    # Free tier, no card, very fast - but small tokens-per-minute limits (see docs).
    "groq": Preset("https://api.groq.com/openai/v1", "GROQ_API_KEY",
                   rpm=25, tpm=8_000, reasoning_effort=True, vision=False,
                   signup="https://console.groq.com/keys"),
    # Many ":free" models; low daily limits on free accounts.
    "openrouter": Preset("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY",
                         rpm=15, tpm=0, reasoning_effort=False, vision=False,
                         signup="https://openrouter.ai/keys"),
    # Local models via Ollama: free and unlimited, quality depends on your hardware/model.
    "ollama": Preset("http://localhost:11434/v1", None, rpm=0, tpm=0, reasoning_effort=False,
                     vision=False, signup="https://ollama.com/download"),
    "openai": Preset(None, "OPENAI_API_KEY", rpm=0, tpm=0, reasoning_effort=True, vision=True,
                     signup="https://platform.openai.com/api-keys"),
}


# ----------------------------------------------------------------------------- rate limiter
class RateLimiter:
    """Sliding-window limiter on requests/minute and tokens/minute.

    ``acquire(estimated_tokens)`` waits until sending one more request of that size keeps us
    within both limits; ``record(tokens)`` replaces the estimate with the real usage.
    """

    def __init__(self, rpm: int, tpm: int, window_s: float = 60.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.rpm, self.tpm, self.window = rpm, tpm, window_s
        self.clock = clock
        self._events: deque[list[float]] = deque()   # [timestamp, tokens]
        self._lock = asyncio.Lock()

    def _prune(self, now: float) -> None:
        while self._events and now - self._events[0][0] >= self.window:
            self._events.popleft()

    def _wait_needed(self, now: float, est: int) -> float:
        self._prune(now)
        waits = [0.0]
        if self.rpm and len(self._events) >= self.rpm:
            waits.append(self._events[len(self._events) - self.rpm][0] + self.window - now)
        if self.tpm and self._events:
            used = sum(e[1] for e in self._events)
            if used + est > self.tpm:
                # wait until enough old requests leave the window
                acc = used
                for ts, tok in self._events:
                    acc -= tok
                    if acc + est <= self.tpm:
                        waits.append(ts + self.window - now)
                        break
                else:
                    waits.append(self._events[-1][0] + self.window - now)
        return max(waits)

    async def acquire(self, est_tokens: int, on_wait: Callable[[float], None] | None = None) -> list[float]:
        async with self._lock:
            while True:
                wait = self._wait_needed(self.clock(), est_tokens)
                if wait <= 0:
                    entry = [self.clock(), float(est_tokens)]
                    self._events.append(entry)
                    return entry
                if on_wait:
                    on_wait(wait)
                await asyncio.sleep(wait + 0.05)

    @staticmethod
    def record(entry: list[float], tokens: int) -> None:
        entry[1] = float(tokens)


# ----------------------------------------------------------------------------- schema sanitising
def sanitize_schema(s: dict[str, Any]) -> dict[str, Any]:
    """Make a JSON schema portable across OpenAI-compatible providers."""
    s = {k: v for k, v in s.items() if k not in ("additionalProperties", "$schema")}
    t = s.get("type")
    if t == "object":
        props = s.get("properties")
        if not props:   # free-form object -> JSON string (decoded by the executor)
            desc = (s.get("description", "") + " (a JSON object encoded as a string)").strip()
            return {"type": "string", "description": desc}
        s["properties"] = {k: sanitize_schema(v) for k, v in props.items()}
    elif t == "array" and isinstance(s.get("items"), dict):
        s["items"] = sanitize_schema(s["items"])
    return s


def to_openai_tool(spec: dict[str, Any]) -> dict[str, Any]:
    fn: dict[str, Any] = {"name": spec["name"], "description": spec["description"]}
    params = spec.get("input_schema") or {}
    if params.get("properties"):
        fn["parameters"] = sanitize_schema(params)
    return {"type": "function", "function": fn}


def extract_json(text: str) -> str:
    """Pull a JSON object out of a model reply (tolerates ```json fences and surrounding prose)."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I | re.M)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in the reply")
    return text[start:end + 1]


@dataclass
class QuotaError:
    """What a 429 response says about the quota that was hit."""
    daily: bool                  # a per-day quota: retrying today is pointless
    retry_after: float | None    # seconds the provider asks us to wait
    limit: int | None            # the quota value, e.g. 5 (requests per minute)
    metric: str                  # quota id / metric name, for the error message


def parse_quota_error(body: Any, message: str = "") -> QuotaError:
    """Read a 429 body. Gemini sends google.rpc QuotaFailure + RetryInfo details; other providers
    usually send only a message, so fall back to scanning the text ("retry in 32.6s", "per day")."""
    if isinstance(body, list) and body:
        body = body[0]
    err = body.get("error", body) if isinstance(body, dict) else {}
    if not isinstance(err, dict):
        err = {}
    details = err.get("details") or []
    text = f"{message} {err.get('message', '')}"
    retry_after = limit = None
    metric = ""
    for d in details if isinstance(details, list) else []:
        if not isinstance(d, dict):
            continue
        if "retryDelay" in d:
            m = re.match(r"([\d.]+)s", str(d["retryDelay"]))
            retry_after = float(m.group(1)) if m else None
        for v in d.get("violations") or []:
            metric = v.get("quotaId") or v.get("quotaMetric") or metric
            try:
                limit = int(v.get("quotaValue"))
            except (TypeError, ValueError):
                pass
    if retry_after is None:
        m = re.search(r"retry in ([\d.]+)\s*s", text, re.I)
        retry_after = float(m.group(1)) if m else None
    daily = bool(re.search(r"per ?day|daily", metric + " " + text, re.I))
    return QuotaError(daily=daily, retry_after=retry_after, limit=limit, metric=metric or "rate limit")


def _estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None) -> int:
    return int(len(json.dumps(messages, default=str)) / 3.5 + len(json.dumps(tools or [])) / 3.5) + 500


# ----------------------------------------------------------------------------- client
class OpenAICompatClient(LLMClient):
    def __init__(self, provider: str, model: str, effort: str = "medium", rpm: int = 0, tpm: int = 0,
                 base_url: str | None = None, max_tokens: int = 8192) -> None:
        super().__init__(model)
        if provider not in PRESETS:
            raise ValueError(f"Unknown provider '{provider}'. Options: {', '.join(PRESETS)} or 'anthropic'.")
        if not model:
            raise ValueError(f"No model configured for provider '{provider}' - set LLM_MODEL in .env.")
        import openai  # imported lazily so the dependency stays optional for Anthropic-only setups
        self._openai = openai
        self.provider = provider
        self.preset = PRESETS[provider]
        key = os.getenv(self.preset.key_env, "") if self.preset.key_env else "not-needed"
        self.key_missing = not key
        # The SDK retries transient errors with a short backoff (seconds). Free-tier 429s ask for longer
        # waits (often 30-60 s), so chat() handles those itself using the provider's retry delay.
        self.client = openai.AsyncOpenAI(api_key=key or "missing", base_url=base_url or self.preset.base_url,
                                         max_retries=2, timeout=180)
        self.rate_retries = 6
        self.effort = {"xhigh": "high", "max": "high"}.get(effort, effort)
        self.limiter = RateLimiter(rpm or self.preset.rpm, tpm or self.preset.tpm)
        self.max_tokens = max_tokens
        self.on_wait: Callable[[float], None] | None = None   # set by the Agent to surface waits in the UI

    def conversation(self, system: str, tools: list[dict[str, Any]], purpose: str = "executor") -> Conversation:
        return _CompatConversation(self, system, tools)

    async def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None):
        if self.key_missing:
            raise LLMError(f"No API key for '{self.provider}': set {self.preset.key_env} in .env "
                           f"(get a free key at {self.preset.signup}).")
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "max_tokens": self.max_tokens}
        if tools:
            kwargs["tools"] = tools
        if self.preset.reasoning_effort and self.effort in {"low", "medium", "high"}:
            kwargs["reasoning_effort"] = self.effort
        est = _estimate_tokens(messages, tools)
        for attempt in range(self.rate_retries + 1):
            entry = await self.limiter.acquire(est, self.on_wait)
            try:
                resp = await self.client.chat.completions.create(**kwargs)
                break
            except self._openai.RateLimitError as e:
                q = parse_quota_error(e.body, str(e))
                if q.daily:
                    raise LLMError(
                        f"{self.provider}: the daily free-tier quota for {self.model} is used up ({q.metric}"
                        f"{f', limit {q.limit}' if q.limit else ''}). Daily quotas reset after 24 h (Gemini: "
                        f"midnight Pacific time). Each model has its own quota, so you can set LLM_MODEL to "
                        f"another model meanwhile.") from e
                if attempt == self.rate_retries:
                    raise LLMError(f"{self.provider} rate limit still exceeded after {attempt} waits: {e}. "
                                   f"Lower LLM_RPM or switch model/provider.") from e
                if q.limit and re.search(r"request", q.metric, re.I) and re.search(r"minute", q.metric, re.I):
                    self.limiter.rpm = min(self.limiter.rpm or q.limit, q.limit)   # learn the real limit
                wait = q.retry_after if q.retry_after is not None else 30.0
                if self.on_wait:
                    self.on_wait(wait)
                await asyncio.sleep(wait + 1)
            except self._openai.AuthenticationError as e:
                raise LLMError(f"{self.provider} rejected the API key ({self.preset.key_env}): {e}") from e
            except self._openai.BadRequestError as e:
                raise LLMError(f"{self.provider} rejected the request: {e}") from e
            except self._openai.APIConnectionError as e:
                raise LLMError(f"Could not reach {self.provider} at {self.client.base_url}: {e}") from e
            except self._openai.APIStatusError as e:
                raise LLMError(f"{self.provider} API error {e.status_code}: {e}") from e
        usage = self._account(resp)
        RateLimiter.record(entry, usage.input_tokens + usage.output_tokens)
        if not resp.choices:
            raise LLMError(f"{self.provider} returned no choices (possibly blocked by a safety filter).")
        return resp, usage

    def _account(self, resp) -> Usage:
        u = getattr(resp, "usage", None)
        usage = Usage(input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                      output_tokens=getattr(u, "completion_tokens", 0) or 0, calls=1)
        self.usage.add(usage)   # free tiers: cost stays 0
        return usage

    async def structured(self, system: str, prompt: str, schema: type[T]) -> T:
        instructions = (
            "\n\nRespond with ONLY a single JSON object (no prose, no code fences) that validates against "
            f"this JSON schema:\n{json.dumps(schema.model_json_schema())}")
        messages: list[dict[str, Any]] = [{"role": "system", "content": system + instructions},
                                          {"role": "user", "content": prompt}]
        last_error = ""
        for _ in range(2):   # one automatic repair attempt
            resp, _usage = await self.chat(messages)
            text = resp.choices[0].message.content or ""
            try:
                return schema.model_validate_json(extract_json(text))
            except (ValidationError, ValueError) as e:
                last_error = str(e)[:1500]
                messages += [{"role": "assistant", "content": text},
                             {"role": "user", "content": f"That reply was not valid: {last_error}\n"
                                                         "Reply again with ONLY the corrected JSON object."}]
        raise LLMError(f"Model did not return a valid {schema.__name__}: {last_error}")


class _CompatConversation(Conversation):
    def __init__(self, llm: OpenAICompatClient, system: str, tools: list[dict[str, Any]]) -> None:
        self.llm = llm
        self.tools = [to_openai_tool(t) for t in tools]   # frozen for the whole conversation
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        self._ids = 0

    async def send(self, *, user_text: str | None = None, tool_outcomes: list[ToolOutcome] | None = None,
                   notes: list[str] | None = None) -> AssistantTurn:
        images: list[str] = []
        for o in tool_outcomes or []:
            self.messages.append({"role": "tool", "tool_call_id": o.call_id,
                                  "content": ("ERROR: " if o.is_error else "") + (o.content or "(empty)")})
            if o.image_path and Path(o.image_path).exists():
                images.append(o.image_path)
        texts = [t for t in [*(notes or []), user_text] if t]
        if images and not self.llm.preset.vision:
            texts.append("(A screenshot was requested but this model cannot view images; rely on the text snapshot.)")
            images = []
        if texts or images:
            parts: list[dict[str, Any]] = [{"type": "text", "text": t} for t in texts]
            for p in images:
                data = base64.standard_b64encode(Path(p).read_bytes()).decode()
                parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}})
            self.messages.append({"role": "user", "content": parts if images else "\n\n".join(texts)})

        resp, usage = await self.llm.chat(self.messages, self.tools)
        choice = resp.choices[0]
        msg = choice.message

        # Store the assistant turn as returned (append-only), keeping provider extras on tool calls.
        stored: dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        turn = AssistantTurn(text=(msg.content or "").strip(), stop_reason=choice.finish_reason or "", usage=usage)
        reasoning = getattr(msg, "reasoning_content", None) or getattr(msg, "reasoning", None)
        if isinstance(reasoning, str):
            turn.thoughts = reasoning.strip()
        raw_calls = []
        for tc in msg.tool_calls or []:
            d = tc.model_dump(exclude_none=True)
            if not d.get("id"):
                self._ids += 1
                d["id"] = f"call_{self._ids}"
            d.setdefault("type", "function")
            raw_calls.append(d)
            fn = d.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
                if not isinstance(args, dict):
                    raise ValueError
            except (ValueError, TypeError):
                args = {"__invalid_json__": str(raw_args)[:500]}
            turn.tool_calls.append(ToolCall(id=d["id"], name=fn.get("name", ""), args=args))
        if raw_calls:
            stored["tool_calls"] = raw_calls
        extra = getattr(msg, "model_extra", None) or {}
        if "extra_content" in extra:
            stored["extra_content"] = extra["extra_content"]
        self.messages.append(stored)
        return turn
