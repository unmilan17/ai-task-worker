"""Human-in-the-loop channel.

The agent sometimes has to stop and ask a person:

* ``clarification`` - "Two Initech invoices were issued on the same day; which one?"
* ``approval``      - "Create a bill for 18,250.00 USD? (policy: > 10,000 needs approval)"

The agent awaits ``channel.request(req)``. *How* the question reaches a human
is pluggable:

* CLIHumanChannel      - prints the question in the terminal and reads stdin,
* WebHumanChannel      - the dashboard shows a card with buttons (see server/app.py),
* ScriptedHumanChannel - canned answers for automated tests and evals.

If nobody answers within the timeout, the request resolves as "no answer",
which the agent treats as *not approved* - the safe default.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import re
import threading
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol


@dataclass
class HumanRequest:
    kind: str                      # "clarification" | "approval"
    question: str
    details: dict[str, Any] = field(default_factory=dict)
    options: list[str] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HumanResponse:
    approved: bool | None = None   # for approvals
    answer: str = ""               # for clarifications (or an approval comment)
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HumanChannel(Protocol):
    async def request(self, req: HumanRequest, timeout_s: float) -> HumanResponse: ...


class CLIHumanChannel:
    """Asks in the terminal. Used by `worker run`."""

    def __init__(self, console=None) -> None:
        self.console = console

    async def request(self, req: HumanRequest, timeout_s: float) -> HumanResponse:
        def ask() -> HumanResponse:
            p = self.console.print if self.console else print
            p("")
            if req.kind == "approval":
                p(f"[bold yellow]APPROVAL NEEDED[/]: {req.question}" if self.console else f"APPROVAL NEEDED: {req.question}")
                for k, v in req.details.items():
                    p(f"    {k}: {v}")
                ans = input("Approve? [y/N] (optional comment after a space): ").strip()
                approved = ans.lower().startswith("y")
                return HumanResponse(approved=approved, answer=ans[1:].strip() if approved else ans)
            p(f"[bold cyan]QUESTION[/]: {req.question}" if self.console else f"QUESTION: {req.question}")
            for i, o in enumerate(req.options, 1):
                p(f"    {i}. {o}")
            ans = input("Your answer: ").strip()
            if ans.isdigit() and req.options and 1 <= int(ans) <= len(req.options):
                ans = req.options[int(ans) - 1]
            return HumanResponse(answer=ans)

        try:
            return await asyncio.wait_for(asyncio.to_thread(ask), timeout=timeout_s)
        except asyncio.TimeoutError:
            return HumanResponse(approved=False, answer="", timed_out=True)


class WebHumanChannel:
    """Bridges the agent (running in its own thread/event loop) and the web UI.

    The agent registers a pending request and waits on a thread-safe Future;
    the web server resolves it when the user clicks a button.
    """

    def __init__(self, on_request=None) -> None:
        self._pending: dict[str, tuple[HumanRequest, concurrent.futures.Future]] = {}
        self._lock = threading.Lock()
        self.on_request = on_request

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r, _ in self._pending.values()]

    def resolve(self, request_id: str, response: HumanResponse) -> bool:
        with self._lock:
            item = self._pending.pop(request_id, None)
        if not item:
            return False
        item[1].set_result(response)
        return True

    async def request(self, req: HumanRequest, timeout_s: float) -> HumanResponse:
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            self._pending[req.id] = (req, fut)
        if self.on_request:
            self.on_request(req)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(fut), timeout=timeout_s)
        except asyncio.TimeoutError:
            with self._lock:
                self._pending.pop(req.id, None)
            return HumanResponse(approved=False, answer="", timed_out=True)


@dataclass
class ScriptedHumanChannel:
    """Deterministic human for tests/evals.

    approve: default decision for approval requests.
    answers: list of (regex, answer) pairs matched against clarification questions.
    default_answer: used when no pattern matches.
    """

    approve: bool = True
    answers: list[tuple[str, str]] = field(default_factory=list)
    default_answer: str = "I don't know - use your best judgement and tell me what you assumed."
    log: list[tuple[HumanRequest, HumanResponse]] = field(default_factory=list)

    async def request(self, req: HumanRequest, timeout_s: float) -> HumanResponse:
        if req.kind == "approval":
            resp = HumanResponse(approved=self.approve, answer="approved" if self.approve else "rejected")
        else:
            text = req.question + " " + " ".join(req.options)
            ans = next((a for pat, a in self.answers if re.search(pat, text, re.I)), self.default_answer)
            resp = HumanResponse(answer=ans)
        self.log.append((req, resp))
        return resp
