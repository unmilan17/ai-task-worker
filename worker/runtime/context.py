"""RunContext: everything a tool needs while it runs, in one object.

Passing a single context object (instead of globals) keeps tools testable:
a test can build a RunContext with a fake human, a temp workspace and a
real browser, and call any tool directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from ..config import Settings
from ..memory.working_memory import Plan, WorkingMemory
from ..safety.policy import Guard
from ..safety.secrets import SecretVault
from .breaker import CircuitBreaker
from .events import EventBus
from .human import HumanChannel

if TYPE_CHECKING:
    from ..safety.review import WriteReviewer
    from ..tools.browser import BrowserSession


@dataclass
class RunContext:
    run_id: str
    run_dir: Path
    settings: Settings
    events: EventBus
    human: HumanChannel
    vault: SecretVault
    guard: Guard
    memory: WorkingMemory = field(default_factory=WorkingMemory)
    plan: Plan = field(default_factory=Plan)
    browser: "BrowserSession | None" = None
    step: int = 0
    read_only: bool = False                    # True while the verifier runs
    actions: list[dict[str, Any]] = field(default_factory=list)   # successful side effects
    evidence: list[dict[str, Any]] = field(default_factory=list)  # screenshots worth showing
    task_text: str = ""                        # the user's request (+ follow-ups), for provenance checks
    reviewer: "WriteReviewer | None" = None     # pre-write review (safety/review.py)
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    _http: httpx.AsyncClient | None = None

    def http(self) -> httpx.AsyncClient:
        """One HTTP client per run (creating a client costs ~0.5 s on Windows: SSL context setup)."""
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=20)
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def workspace(self) -> Path:
        return self.settings.workspace_dir

    def record_action(self, description: str, **details: Any) -> None:
        """Log a successful side effect (form submitted, record created, file written)."""
        self.actions.append({"step": self.step, "description": description, **details})
        self.events.emit("side_effect", step=self.step, description=description, **details)

    def repeated_writes(self) -> int:
        """How many successful writes so far carried the same key values as the latest one.

        The signature is the set of values containing a digit (IDs, amounts, dates), so a form submission
        and an API call with the same data count as the same write."""
        def norm(v: Any) -> str:
            t = str(v).strip().lower()
            try:
                return f"{float(t.replace(',', '')):.2f}"
            except ValueError:
                return t

        def sig(a: dict[str, Any]) -> frozenset:
            payload = a.get("fields") or a.get("request") or {}
            vals = payload.values() if isinstance(payload, dict) else []
            return frozenset(norm(v) for v in vals          # IDs, amounts, dates - not free text
                             if isinstance(v, (str, int, float)) and any(ch.isdigit() for ch in str(v))
                             and " " not in str(v).strip())
        if not self.actions:
            return 0
        last = sig(self.actions[-1])
        return sum(1 for a in self.actions if last and sig(a) == last)

    def add_evidence(self, path: str, caption: str) -> None:
        self.evidence.append({"step": self.step, "path": path, "caption": caption})
