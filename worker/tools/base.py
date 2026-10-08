"""The uniform tool interface.

Every capability the agent has (browser, files, HTTP, memory, asking the
user...) is a Tool with:

* a name + description + JSON schema  -> shown to the LLM,
* ``idempotent``                      -> safe to retry automatically?
* ``async run(args, ctx)``            -> does the work, returns a ToolResult.

A ToolResult is *structured*: besides the text the model reads, it carries an
error classification (ErrorKind) and a recovery hint. The executor uses the
classification to decide whether to retry automatically; the model uses the
hint to decide what to do next.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..runtime.context import RunContext


class ErrorKind(str, Enum):
    TRANSIENT = "transient"          # timeouts, 5xx, flaky network -> retry may help
    NOT_FOUND = "not_found"          # element/page/record does not exist (stale ref, wrong URL)
    AUTH = "auth"                    # not logged in, session expired, bad credentials
    VALIDATION = "validation"        # the target system rejected the input (fix the input)
    PERMISSION = "permission"        # blocked by the safety policy
    REJECTED_BY_USER = "rejected_by_user"  # a human said no
    INVALID_INPUT = "invalid_input"  # the tool call itself was malformed
    FATAL = "fatal"                  # unexpected failure, unlikely to be fixed by retrying


# What the model is told to do for each class of failure.
DEFAULT_HINTS: dict[ErrorKind, str] = {
    ErrorKind.TRANSIENT: "Temporary failure. If this action changes data, first check whether it took effect "
                         "before retrying; otherwise retry, or try an alternative route.",
    ErrorKind.NOT_FOUND: "The target was not found. Element refs change after every page update - take a fresh "
                         "browser_snapshot and use the new ref, or re-check the URL/identifier.",
    ErrorKind.AUTH: "You appear to be signed out or unauthorised. Sign in again (credentials are available as "
                    "{{secret:...}} placeholders) and repeat the step.",
    ErrorKind.VALIDATION: "The system rejected the input. Read the error message, correct the value/format and try again.",
    ErrorKind.PERMISSION: "This action is blocked by company policy. Do not try to work around it; choose a "
                          "permitted approach or report the limitation to the user.",
    ErrorKind.REJECTED_BY_USER: "The user declined. Do not attempt the same action again; finish and report the outcome.",
    ErrorKind.INVALID_INPUT: "The tool call was malformed. Check the tool's parameters and call it again correctly.",
    ErrorKind.FATAL: "Unexpected error. Try a different approach; if nothing works, finish with status 'failed'.",
}


@dataclass
class ToolResult:
    ok: bool
    output: str                                   # what the model reads
    data: dict[str, Any] = field(default_factory=dict)   # structured details for trace/UI
    error_kind: ErrorKind | None = None
    hint: str | None = None
    image_path: str | None = None                 # optional image the model should see (vision)
    screenshot: str | None = None                 # evidence screenshot path (shown in UI/report)
    side_effect: bool = False                     # True if this call changed data somewhere
    retry_safe: bool | None = None                # override tool.idempotent for this result

    @classmethod
    def success(cls, output: str, **kw: Any) -> "ToolResult":
        return cls(ok=True, output=output, **kw)

    @classmethod
    def failure(cls, kind: ErrorKind, message: str, hint: str | None = None, **kw: Any) -> "ToolResult":
        return cls(ok=False, output=message, error_kind=kind, hint=hint or DEFAULT_HINTS[kind], **kw)

    def render_for_model(self) -> str:
        if self.ok:
            return self.output
        return f"ERROR [{self.error_kind.value if self.error_kind else 'unknown'}]: {self.output}\nHint: {self.hint}"


class ToolError(Exception):
    """Raised inside tools to short-circuit into a classified failure."""

    def __init__(self, kind: ErrorKind, message: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint


class Tool(ABC):
    name: str = ""
    description: str = ""
    input_schema: dict[str, Any] = {"type": "object", "properties": {}, "required": []}
    idempotent: bool = True       # safe to retry automatically on transient errors
    verifier_allowed: bool = True  # may the read-only verifier use it?

    def is_idempotent(self, args: dict[str, Any]) -> bool:
        """Can this exact call be repeated safely if it fails transiently?"""
        return self.idempotent

    def spec(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}

    @abstractmethod
    async def run(self, args: dict[str, Any], ctx: "RunContext") -> ToolResult: ...


def schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    """Helper for writing tool JSON schemas compactly."""
    return {"type": "object", "properties": properties, "required": required or [],
            "additionalProperties": False}
