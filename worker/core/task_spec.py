"""UNDERSTAND phase: turn a natural-language request into a structured TaskSpec.

Why a separate step before acting?

* It forces the system to state the user's *end goal* and *checkable success
  criteria* up front. The verifier later checks exactly these criteria, so
  "done" has a concrete meaning that is fixed before any action is taken.
* It is the cheapest moment to catch requests that are missing essential
  information (ask first, act later) or that are out of scope.
* The initial plan gives the UI a checklist from the first second.
"""

from __future__ import annotations

import json

from pydantic import BaseModel

from ..llm.base import LLMClient
from .prompts import UNDERSTAND_SYSTEM, fill


class Clarification(BaseModel):
    question: str
    options: list[str]


class TaskSpec(BaseModel):
    goal: str
    deliverable: str
    systems_involved: list[str]
    success_criteria: list[str]
    changes_data: bool
    constraints_and_risks: list[str]
    plan: list[str]
    blocking_questions: list[Clarification]
    out_of_scope: bool
    out_of_scope_reason: str

    def render(self) -> str:
        return json.dumps(self.model_dump(exclude={"blocking_questions", "out_of_scope", "out_of_scope_reason"}),
                          indent=2)


async def understand(llm: LLMClient, task: str, environment: str) -> TaskSpec:
    system = fill(UNDERSTAND_SYSTEM, environment=environment)
    return await llm.structured(system, f"User request:\n{task}", TaskSpec)
