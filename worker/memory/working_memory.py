"""Working memory: what the agent has learned during this run, with provenance.

Facts are written explicitly by the agent through the ``remember`` tool, e.g.

    remember(key="invoice.total", value="1234.50",
             source="supplier portal, invoice detail page (Total due)")

Why explicit facts instead of "the model will remember it from the chat"?

* The facts survive context growth: they are listed again in every
  verification prompt and in the final report.
* Each fact records *where it came from* (source + step + URL), which is what
  makes the final summary auditable ("1,234.50 - read from the invoice page at
  step 9, screenshot attached").
* The verifier can compare the claimed facts against the real systems.

The Plan is kept here too: the agent's own, revisable checklist.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Fact:
    key: str
    value: str
    source: str
    step: int
    url: str | None = None
    screenshot: str | None = None
    ts: float = field(default_factory=time.time)


class WorkingMemory:
    def __init__(self) -> None:
        self._facts: dict[str, Fact] = {}

    def remember(self, fact: Fact) -> Fact | None:
        """Stores a fact; returns the previous value if the key was overwritten."""
        prev = self._facts.get(fact.key)
        self._facts[fact.key] = fact
        return prev

    def recall(self, query: str | None = None) -> list[Fact]:
        facts = list(self._facts.values())
        if query:
            q = query.lower()
            facts = [f for f in facts if q in f.key.lower() or q in f.value.lower()]
        return facts

    def as_list(self) -> list[dict[str, Any]]:
        return [asdict(f) for f in self._facts.values()]

    def render(self) -> str:
        if not self._facts:
            return "(no facts recorded)"
        return "\n".join(f"- {f.key} = {f.value}  (source: {f.source}; step {f.step})" for f in self._facts.values())


@dataclass
class PlanStep:
    id: int
    title: str
    status: str = "pending"    # pending | in_progress | done | skipped | blocked
    note: str = ""


class Plan:
    def __init__(self, titles: list[str] | None = None) -> None:
        self.steps: list[PlanStep] = [PlanStep(i + 1, t) for i, t in enumerate(titles or [])]
        self.revisions = 0

    def replace(self, steps: list[dict[str, Any]]) -> None:
        self.steps = [PlanStep(int(s.get("id", i + 1)), str(s["title"]), str(s.get("status", "pending")),
                               str(s.get("note", ""))) for i, s in enumerate(steps)]
        self.revisions += 1

    def as_list(self) -> list[dict[str, Any]]:
        return [asdict(s) for s in self.steps]

    def render(self) -> str:
        return "\n".join(f"{s.id}. [{s.status}] {s.title}" + (f" - {s.note}" if s.note else "")
                         for s in self.steps) or "(empty plan)"
