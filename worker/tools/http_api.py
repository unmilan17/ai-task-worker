"""HTTP tool - lets the agent call documented REST APIs (e.g. the ERP API).

Why give a "computer-using" agent an API tool at all? Because a good human
worker uses the most reliable route available. When the ERP web form is
broken (CHAOS=hard), the agent can read the API docs and finish the job via
the API instead of giving up. Hosts are restricted by the policy allow-list,
and write requests (POST/PUT/PATCH/DELETE) go through the same approval rules
as form submissions.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..runtime.context import RunContext
from ..safety.policy import ProposedAction
from .base import ErrorKind, Tool, ToolError, ToolResult, schema

MAX_BODY_CHARS = 8000


class HttpRequest(Tool):
    name = "http_request"
    description = ("Call an HTTP/REST API on an allowed company system. Headers and body may contain "
                   "{{secret:name}} placeholders (e.g. an API key), which are filled in securely. Returns the "
                   "status code and response body.")
    input_schema = schema({
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"]},
        "url": {"type": "string"},
        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
        "query": {"type": "object", "additionalProperties": {"type": "string"}},
        "json_body": {"type": "object", "description": "JSON request body for POST/PUT/PATCH"},
    }, ["method", "url"])

    def is_idempotent(self, args: dict[str, Any]) -> bool:
        return args.get("method", "GET").upper() in {"GET", "PUT", "DELETE"}

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        method = args["method"].upper()
        url = args["url"]
        body = args.get("json_body")
        kind = "http_read" if method == "GET" else "http_write"
        await ctx.guard.authorize(ProposedAction(kind, f"{method} {url}", url=url, method=method,
                                                 fields=body or {}), ctx)
        try:
            headers = ctx.vault.resolve_obj(args.get("headers") or {})
            body_resolved = ctx.vault.resolve_obj(body) if body is not None else None
        except KeyError as e:
            raise ToolError(ErrorKind.INVALID_INPUT, str(e))
        try:
            resp = await ctx.http().request(method, url, headers=headers, params=args.get("query"),
                                            json=body_resolved)
        except httpx.TimeoutException:
            raise ToolError(ErrorKind.TRANSIENT, f"{method} {url} timed out.")
        except httpx.HTTPError as e:
            raise ToolError(ErrorKind.TRANSIENT, f"{method} {url} failed: {e}")

        try:
            text = json.dumps(resp.json(), indent=1)
        except ValueError:
            text = resp.text
        if len(text) > MAX_BODY_CHARS:
            text = text[:MAX_BODY_CHARS] + "\n[... truncated ...]"
        out = f"{method} {url} -> HTTP {resp.status_code}\n{text}"

        status = resp.status_code
        if status >= 500:
            hint = None
            if method != "GET":
                hint = "Server error on a write request: check whether the record was created before retrying."
            return ToolResult.failure(ErrorKind.TRANSIENT, out, hint, retry_safe=method == "GET")
        if status in (401, 403):
            return ToolResult.failure(ErrorKind.AUTH, out, "Check the authentication header described in the API docs.")
        if status == 404:
            return ToolResult.failure(ErrorKind.NOT_FOUND, out)
        if status in (400, 409, 422):
            return ToolResult.failure(ErrorKind.VALIDATION, out)
        if method != "GET":
            ctx.record_action(f"{method} {url} -> HTTP {status}", url=url, request=body, response=text[:1000])
        return ToolResult.success(out, side_effect=method != "GET", data={"status": status})


HTTP_TOOLS: list[type[Tool]] = [HttpRequest]
