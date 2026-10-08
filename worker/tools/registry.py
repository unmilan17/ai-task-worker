"""Tool registry: the single list of capabilities given to the LLM.

Adding a capability = write a Tool subclass and add it here. Nothing in the
agent loop changes - this is the main extension point (see docs/INTERVIEW_GUIDE.md
"How to add a new tool").
"""

from __future__ import annotations

from .agent_tools import AGENT_TOOLS, SubmitVerdict
from .base import Tool
from .browser import BROWSER_TOOLS
from .files import FILE_TOOLS
from .http_api import HTTP_TOOLS


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools)

    def specs(self) -> list[dict]:
        return [t.spec() for t in self._tools.values()]


def executor_registry() -> ToolRegistry:
    return ToolRegistry([cls() for cls in [*BROWSER_TOOLS, *FILE_TOOLS, *HTTP_TOOLS, *AGENT_TOOLS]])


def verifier_registry() -> ToolRegistry:
    """Read-oriented subset for the verifier (writes are additionally blocked by the Guard)."""
    tools = [cls() for cls in [*BROWSER_TOOLS, *FILE_TOOLS, *HTTP_TOOLS, *AGENT_TOOLS]]
    tools = [t for t in tools if t.verifier_allowed and t.name not in {"file_write", "update_plan", "remember"}]
    return ToolRegistry([*tools, SubmitVerdict()])
