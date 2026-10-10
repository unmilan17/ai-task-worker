"""The act -> observe -> reflect loop shared by the executor and the verifier.

One iteration:
    1. send tool results (+ any reflection notes) to the model,
    2. receive the model's next tool calls (and its thought summary),
    3. run each call through the ToolExecutor (policy, retries, redaction),
    4. let the LoopDetector look for loops / repeated failures,
until the model calls the *terminal* tool (``finish`` for the executor,
``submit_verdict`` for the verifier) or a budget runs out.

Budgets (steps, wall-clock time, money) are checked before every tool call.
They are the last line of defence against an agent that never converges.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from ..config import Settings
from ..llm.base import Conversation, LLMClient, ToolCall, ToolOutcome
from ..runtime.context import RunContext
from ..runtime.executor import ToolExecutor
from ..runtime.loop_detector import LoopDetector
from .prompts import NUDGE_NO_TOOL

MAX_NUDGES = 3
MAX_SUMMARY_WORDS = 100


class BudgetExceeded(Exception):
    pass


class Cancelled(Exception):
    pass


@dataclass
class Budget:
    settings: Settings
    llm: LLMClient
    started: float = field(default_factory=time.time)
    cancel: threading.Event = field(default_factory=threading.Event)
    steps: int = 0
    warned: bool = False

    def check(self, count_steps: bool = True) -> None:
        if self.cancel.is_set():
            raise Cancelled("The run was cancelled by the user.")
        if count_steps and self.steps >= self.settings.max_steps:
            raise BudgetExceeded(f"step budget exhausted ({self.settings.max_steps} tool calls)")
        if time.time() - self.started > self.settings.max_minutes * 60:
            raise BudgetExceeded(f"time budget exhausted ({self.settings.max_minutes} minutes)")
        if self.llm.usage.cost_usd > self.settings.max_cost_usd:
            raise BudgetExceeded(f"cost budget exhausted (${self.settings.max_cost_usd:.2f})")

    def warning(self) -> str | None:
        if not self.warned and self.steps >= int(self.settings.max_steps * 0.75):
            self.warned = True
            return (f"BUDGET NOTICE: you have used {self.steps} of {self.settings.max_steps} steps. Prioritise "
                    f"finishing the goal; if that is not possible, finish and report partial progress honestly.")
        return None


class ToolLoop:
    def __init__(self, conv: Conversation, executor: ToolExecutor, ctx: RunContext, budget: Budget,
                 terminal_tool: str, label: str, max_steps: int | None = None) -> None:
        self.conv = conv
        self.executor = executor
        self.ctx = ctx
        self.budget = budget
        self.terminal_tool = terminal_tool
        self.label = label
        self.max_steps = max_steps          # optional per-loop cap (verifier)
        # The verifier has its own step cap (max_steps) and does not eat into the executor's step budget,
        # so a long but correct execution can still be verified. Time, cost and cancel still apply.
        self.counts_steps = max_steps is None
        self.detector = LoopDetector()
        self.local_steps = 0
        self.summary_rejections = 0

    async def start(self, kickoff: str) -> tuple[ToolCall, list[ToolOutcome]]:
        return await self._run(user_text=kickoff, outcomes=[])

    async def resume(self, pending: list[ToolOutcome], user_text: str) -> tuple[ToolCall, list[ToolOutcome]]:
        return await self._run(user_text=user_text, outcomes=pending)

    async def _run(self, user_text: str | None, outcomes: list[ToolOutcome]) -> tuple[ToolCall, list[ToolOutcome]]:
        ctx = self.ctx
        notes: list[str] = []
        nudges = 0
        while True:
            self.budget.check(self.counts_steps)
            turn = await self.conv.send(user_text=user_text, tool_outcomes=outcomes, notes=notes)
            user_text, outcomes, notes = None, [], []
            ctx.events.emit("llm_turn", step=ctx.step, loop=self.label, thoughts=turn.thoughts, text=turn.text,
                            tool_calls=[{"name": c.name, "args": ctx.vault.redact_obj(c.args)} for c in turn.tool_calls],
                            usage=turn.usage.__dict__, total_cost_usd=round(self.budget.llm.usage.cost_usd, 4))

            if not turn.tool_calls:
                nudges += 1
                if nudges > MAX_NUDGES:
                    raise BudgetExceeded(f"the {self.label} stopped calling tools without finishing")
                user_text = NUDGE_NO_TOOL
                continue
            nudges = 0

            terminal: ToolCall | None = None
            for call in turn.tool_calls:
                if terminal is not None:
                    outcomes.append(ToolOutcome(call.id, f"Not executed: '{self.terminal_tool}' was already called."))
                    continue
                if call.name == self.terminal_tool:
                    problem = self._check_terminal(call)
                    if problem:
                        outcomes.append(ToolOutcome(call.id, problem, is_error=True))
                        continue
                    terminal = call
                    outcomes.append(ToolOutcome(call.id, "Received."))
                    continue
                self.budget.check(self.counts_steps)
                if self.max_steps is not None and self.local_steps >= self.max_steps:
                    raise BudgetExceeded(f"{self.label} step limit ({self.max_steps}) reached")
                if self.counts_steps:
                    self.budget.steps += 1
                self.local_steps += 1
                ctx.step += 1
                result = await self.executor.execute(call)
                outcomes.append(ToolOutcome(call.id, result.render_for_model(), is_error=not result.ok,
                                            image_path=result.image_path))
                note = self.detector.observe(call, result)
                if result.side_effect and ctx.repeated_writes() >= 2:
                    note = (f"REPEATED WRITE: you have now successfully submitted the same values "
                            f"{ctx.repeated_writes()} times. If the record still does not appear afterwards, the "
                            f"system is accepting but not saving the data (or you are creating duplicates). Do NOT "
                            f"submit it again: check once more, then finish with status 'failed' and explain what "
                            f"happened, so a person can follow up.")
                if note:
                    ctx.events.emit("reflection", step=ctx.step, loop=self.label, note=note)
                    notes.append(note)
            if terminal is not None:
                return terminal, outcomes
            warn = self.budget.warning()
            if warn:
                notes.append(warn)

    def _check_terminal(self, call: ToolCall) -> str | None:
        """Light contract checks on `finish` (the user-facing message). Each is enforced at most twice."""
        if call.name != "finish" or self.summary_rejections >= 2:
            return None
        words = len(str(call.args.get("summary", "")).split())
        if words > MAX_SUMMARY_WORDS:
            self.summary_rejections += 1
            return (f"ERROR [invalid_input]: the summary has {words} words; the limit is {MAX_SUMMARY_WORDS}. "
                    "Call finish again with a shorter summary (put details in evidence / notes_for_user).")
        return None
