"""Runs one tool call safely: validate -> execute -> classify -> (auto-retry) -> redact -> log.

Retry policy (the part interviewers usually ask about):

* Only TRANSIENT failures are retried automatically (timeouts, 5xx, refused
  connections), with exponential backoff (0.5s, 1s, 2s ...).
* Only if the call is *idempotent*. Opening a page or GETting an API is safe
  to repeat; submitting a form or POSTing a bill is not - repeating it after a
  timeout could create a duplicate. Those failures go back to the model with
  a hint to *check whether the action took effect* before trying again.
* Everything else (validation errors, missing elements, policy blocks...) is
  never retried blindly: the model gets the classified error + a recovery hint
  and decides what to do. A retry with the same input would fail the same way.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from ..llm.base import ToolCall
from ..tools.base import ErrorKind, Tool, ToolError, ToolResult
from ..tools.registry import ToolRegistry
from .context import RunContext

_TYPES = {"string": str, "integer": int, "boolean": bool, "array": list, "object": dict, "number": (int, float)}


def _decode_json(v: Any) -> Any:
    """Some providers send objects/arrays as JSON-encoded strings (see llm/openai_compat.sanitize_schema)."""
    if isinstance(v, str) and v.strip()[:1] in ("{", "["):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def validate_args(tool: Tool, args: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Light JSON-schema check with friendly coercions ("12" -> 12, '{"a":1}' -> {"a": 1}).
    Returns (args, error)."""
    if not isinstance(args, dict):
        return args, "arguments must be a JSON object"
    if "__invalid_json__" in args:
        return args, f"the arguments were not valid JSON ({args['__invalid_json__'][:200]!r})"
    sch = tool.input_schema
    args = dict(args)
    for k, spec in sch.get("properties", {}).items():
        if k in args and spec.get("type") in ("object", "array"):
            if args[k] in ("", "null") and k not in sch.get("required", []):
                del args[k]          # an empty optional JSON-string field means "not provided"
                continue
            args[k] = _decode_json(args[k])
            if spec.get("type") == "array" and isinstance(args[k], list) and spec.get("items", {}).get("type") == "object":
                args[k] = [_decode_json(x) for x in args[k]]
    props = sch.get("properties", {})
    missing = [k for k in sch.get("required", []) if k not in args]
    if missing:
        return args, f"missing required argument(s): {', '.join(missing)}"
    unknown = [k for k in args if k not in props]
    if unknown and sch.get("additionalProperties") is False:
        return args, f"unknown argument(s): {', '.join(unknown)}; expected {', '.join(props)}"
    out = dict(args)
    for k, v in args.items():
        expected = props.get(k, {}).get("type")
        if not expected or v is None:
            continue
        if expected == "integer" and isinstance(v, str) and v.strip().lstrip("-").isdigit():
            out[k] = int(v)
        elif expected == "boolean" and isinstance(v, str) and v.lower() in {"true", "false"}:
            out[k] = v.lower() == "true"
        elif not isinstance(v, _TYPES.get(expected, object)) or (expected == "integer" and isinstance(v, bool)):
            return args, f"argument '{k}' must be of type {expected}"
        enum = props.get(k, {}).get("enum")
        if enum and out[k] not in enum:
            return args, f"argument '{k}' must be one of {enum}"
    return out, None


def _classify_exception(e: Exception) -> ErrorKind:
    name = type(e).__name__.lower()
    msg = str(e).lower()
    if "timeout" in name or "timeout" in msg:
        return ErrorKind.TRANSIENT
    if "net::err" in msg or "connection" in msg:
        return ErrorKind.TRANSIENT
    if "target closed" in msg or "has been closed" in msg:
        return ErrorKind.TRANSIENT
    return ErrorKind.FATAL


def _target_host(call: ToolCall, ctx: RunContext) -> str | None:
    """Which system does this call talk to? (for the circuit breaker)"""
    url = call.args.get("url") if isinstance(call.args, dict) else None
    if isinstance(url, str) and url.startswith("http"):
        return ctx.breaker.host_of(url)
    if call.name.startswith("browser_") and ctx.browser is not None and ctx.browser.page is not None:
        return ctx.breaker.host_of(ctx.browser.page.url)
    return None


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, ctx: RunContext) -> None:
        self.registry = registry
        self.ctx = ctx

    async def execute(self, call: ToolCall) -> ToolResult:
        ctx = self.ctx
        tool = self.registry.get(call.name)
        safe_args = ctx.vault.redact_obj(call.args)
        if tool is None:
            res = ToolResult.failure(ErrorKind.INVALID_INPUT, f"Unknown tool '{call.name}'.",
                                     f"Available tools: {', '.join(self.registry.names())}")
            ctx.events.emit("tool_result", step=ctx.step, call_id=call.id, name=call.name, ok=False,
                            error_kind=res.error_kind.value, output=res.output)
            return res
        args, err = validate_args(tool, call.args)
        if err:
            res = ToolResult.failure(ErrorKind.INVALID_INPUT, f"Invalid arguments for {call.name}: {err}.")
            ctx.events.emit("tool_result", step=ctx.step, call_id=call.id, name=call.name, ok=False,
                            error_kind=res.error_kind.value, output=res.output)
            return res

        host = _target_host(call, ctx)
        if ctx.breaker.is_open(host):
            res = ToolResult.failure(ErrorKind.UNAVAILABLE,
                                     f"{host} is unavailable: its circuit breaker is open after repeated failures. "
                                     f"The call was not made.")
            ctx.events.emit("tool_result", step=ctx.step, call_id=call.id, name=call.name, ok=False,
                            error_kind=res.error_kind.value, output=res.output, hint=res.hint)
            return res
        # A host that just failed gets a single probe instead of the usual spaced retries.
        max_retries = 0 if ctx.breaker.suspect(host) else ctx.settings.max_tool_retries
        attempt = 0
        while True:
            attempt += 1
            ctx.events.emit("tool_call", step=ctx.step, call_id=call.id, name=call.name, args=safe_args,
                            attempt=attempt)
            t0 = time.time()
            try:
                res = await tool.run(args, ctx)
            except ToolError as e:
                res = ToolResult.failure(e.kind, e.message, e.hint)
            except Exception as e:  # noqa: BLE001 - classify anything unexpected
                kind = _classify_exception(e)
                res = ToolResult.failure(kind, f"{type(e).__name__}: {str(e)[:500]}")
            res.output = ctx.vault.redact(res.output)
            retry_safe = res.retry_safe if res.retry_safe is not None else tool.is_idempotent(args)

            if (not res.ok and res.error_kind == ErrorKind.TRANSIENT and retry_safe and attempt <= max_retries):
                delay = 1.0 * (2 ** (attempt - 1))      # spaced: 1 s, 2 s, ...
                ctx.events.emit("retry", step=ctx.step, call_id=call.id, name=call.name, attempt=attempt,
                                delay_s=delay, error=res.output[:300])
                await asyncio.sleep(delay)
                continue

            if attempt > 1:
                res.output = (f"(succeeded on attempt {attempt} after transient errors)\n" if res.ok else
                              f"(still failing after {attempt} attempts)\n") + res.output
            transient = not res.ok and res.error_kind == ErrorKind.TRANSIENT
            if ctx.breaker.record(host, transient):
                ctx.events.emit("circuit_open", step=ctx.step, host=host)
                res = ToolResult.failure(ErrorKind.UNAVAILABLE,
                                         f"{host} appears to be DOWN: repeated calls failed even after spaced "
                                         f"retries. Last error: {res.output[:300]}", screenshot=res.screenshot)
            ctx.events.emit(
                "tool_result", step=ctx.step, call_id=call.id, name=call.name, ok=res.ok,
                error_kind=res.error_kind.value if res.error_kind else None,
                output=res.output[:4000], hint=res.hint, duration_s=round(time.time() - t0, 2),
                screenshot=res.screenshot, side_effect=res.side_effect, attempts=attempt,
                data=ctx.vault.redact_obj(json.loads(json.dumps(res.data, default=str))))
            return res
