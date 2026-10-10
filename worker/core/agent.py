"""The orchestrator: runs a task from request to verified report, and handles follow-up turns.

    UNDERSTAND -> (CLARIFY) -> EXECUTE [act/observe/reflect loop] -> VERIFY -> REPORT
                                   ^                                    |
                                   +---- verification failed (bounded) -+

A run can continue with follow-up messages ("What was the due date again?", "Do the same for
the other supplier"): the same browser, working memory and executor conversation are kept, so the worker
answers from what it already knows or continues the work without starting over. Each turn gets its
own task spec, budget, verification and report.

Read `_turn` top to bottom to understand the whole system; every phase delegates to a small module
(task_spec, loop, verifier, report).
"""

from __future__ import annotations

import re
import threading
import time
import traceback
import uuid
from datetime import datetime
from typing import Any, Callable

from ..config import Settings
from ..llm.base import LLMClient, LLMError
from ..memory.working_memory import Plan
from ..runtime.context import RunContext
from ..runtime.events import Event, EventBus, TraceWriter
from ..runtime.executor import ToolExecutor
from ..runtime.human import HumanChannel, HumanRequest
from ..safety.policy import Guard, Policy
from ..safety.review import WriteReviewer
from ..safety.secrets import SecretVault
from ..tools.browser import BrowserSession
from ..tools.registry import executor_registry
from .loop import Budget, BudgetExceeded, Cancelled, ToolLoop
from .prompts import (EXECUTOR_KICKOFF, EXECUTOR_SYSTEM, FOLLOWUP_KICKOFF, VERIFY_FAILED_FEEDBACK,
                      environment_block, fill)
from .report import build_report
from .task_spec import TaskSpec, understand
from .verifier import verify

UNCLEAR_QUESTION = "I'm not sure what you'd like me to do. What task should I work on?"


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def is_unclear(text: str) -> bool:
    """Deterministic sanity check: empty input or a single word with no digits ('asdf')."""
    words = re.findall(r"[A-Za-z]{2,}", text or "")
    return not (text or "").strip() or (len(words) <= 1 and not re.search(r"\d", text or ""))


class Agent:
    def __init__(self, llm: LLMClient, settings: Settings, human: HumanChannel, run_id: str | None = None,
                 on_event: Callable[[Event], None] | None = None, cancel: threading.Event | None = None,
                 keep_open: bool = False) -> None:
        self.llm = llm
        self.settings = settings
        self.keep_open = keep_open          # keep browser + conversation alive for follow-up turns
        self.run_id = run_id or new_run_id()
        self.run_dir = settings.runs_dir / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.events = EventBus(self.run_id)
        self.events.subscribe(TraceWriter(self.run_dir / "trace.jsonl"))
        if on_event:
            self.events.subscribe(on_event)
        self.ctx = RunContext(
            run_id=self.run_id, run_dir=self.run_dir, settings=settings, events=self.events, human=human,
            vault=SecretVault.from_file(settings.secrets_file),
            guard=Guard(Policy.from_file(settings.policy_file)),
        )
        self.ctx.reviewer = WriteReviewer(llm, provenance=settings.provenance_check,
                                          ambiguity=settings.ambiguity_review)
        self.cancel = cancel or threading.Event()
        self.budget = Budget(settings, llm, cancel=self.cancel)
        self.turns: list[dict[str, Any]] = []
        self.closed = False
        self._loop: ToolLoop | None = None
        self._pending: list = []
        self._environment: str | None = None
        if hasattr(llm, "on_switch"):  # fallback chain: record which model took over and why
            llm.on_switch = lambda old, new, why: self.events.emit("model_switch", step=self.ctx.step,
                                                                    from_model=old, to_model=new, reason=why[:300])
        if hasattr(llm, "on_wait"):   # free-tier rate limiting: show waits in the CLI/UI instead of silence
            llm.on_wait = lambda s: self.events.emit("rate_limit_wait", step=self.ctx.step, seconds=round(s, 1))

    # ------------------------------------------------------------------ public
    async def run(self, task: str) -> dict[str, Any]:
        """First turn. Closes the session afterwards unless keep_open=True."""
        try:
            return await self._turn(task, follow_up=False)
        finally:
            if not self.keep_open:
                await self.close()

    async def follow_up(self, message: str) -> dict[str, Any]:
        """Another turn in the same session (same browser, memory and conversation)."""
        if self.closed:
            raise RuntimeError("This session is closed; start a new run.")
        return await self._turn(message, follow_up=True)

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.ctx.browser is not None:
            await self.ctx.browser.close()
        await self.ctx.aclose()

    # ------------------------------------------------------------------ one turn
    async def _turn(self, text: str, follow_up: bool) -> dict[str, Any]:
        ctx, ev = self.ctx, self.events
        turn_no = len(self.turns) + 1
        started = time.time()
        spec: TaskSpec | None = None
        claim: dict[str, Any] | None = None
        verdicts: list[dict[str, Any]] = []
        status, reason = "error", ""
        actions_before, steps_before = len(ctx.actions), ctx.step
        self.budget = Budget(self.settings, self.llm, cancel=self.cancel)     # fresh budget per turn
        if self._loop is not None:
            self._loop.budget = self.budget
        ev.emit("turn_started" if follow_up else "run_started", task=text, turn=turn_no, model=self.llm.model,
                provider=self.llm.provider, run_dir=str(self.run_dir))
        if ctx.browser is None:
            ctx.browser = BrowserSession(self.settings, self.run_dir)
        ctx.task_text = f"{ctx.task_text}\n\nFollow-up from the user: {text}" if follow_up else text
        task_for_spec = text
        try:
            environment = self._environment = self._environment or environment_block(self.settings.environment_file)

            # 0. SANITY: empty or meaningless input -> ask, never act on it ----------
            if not follow_up and is_unclear(text):
                answer = await self._ask(UNCLEAR_QUESTION)
                if not answer or is_unclear(answer):
                    claim = {"status": "blocked", "summary": "The request was empty or unclear, so I did nothing. "
                                                             "Tell me what you'd like done."}
                    raise _Stop("blocked", "the request was empty or unclear")
                task_for_spec = text = answer
                ctx.task_text = answer

            # 1. UNDERSTAND --------------------------------------------------
            ev.emit("phase", phase="understand")
            spec = await understand(self.llm, self._spec_input(task_for_spec, follow_up), environment)
            ev.emit("task_spec", spec=spec.model_dump())
            if not follow_up or spec.changes_data:
                ctx.plan = Plan(spec.plan)
                ev.emit("plan_updated", plan=ctx.plan.as_list())

            if spec.out_of_scope:
                claim = {"status": "blocked", "summary": f"I can't do this task: {spec.out_of_scope_reason}"}
                raise _Stop("blocked", spec.out_of_scope_reason)

            # 2. CLARIFY (only for information the systems cannot provide) -----
            clarifications = await self._clarify(spec)
            if clarifications is None:
                claim = {"status": "blocked", "summary": "I need more information before I can start, "
                                                         "but my question was not answered."}
                raise _Stop("blocked", "clarification not answered")

            # 3. EXECUTE (new conversation on turn 1, the same one for follow-ups) ---
            ev.emit("phase", phase="execute")
            if self._loop is None:
                registry = executor_registry()
                conv = self.llm.conversation(fill(EXECUTOR_SYSTEM, environment=environment), registry.specs(),
                                             purpose="executor")
                self._loop = ToolLoop(conv, ToolExecutor(registry, ctx), ctx, self.budget, terminal_tool="finish",
                                      label="executor")
                kickoff = fill(EXECUTOR_KICKOFF, task=text, spec=spec.render(), clarifications=clarifications)
                finish_call, pending = await self._loop.start(kickoff)
            else:
                kickoff = fill(FOLLOWUP_KICKOFF, message=text, spec=spec.render(), clarifications=clarifications)
                finish_call, pending = await self._loop.resume(self._pending, kickoff)
            claim = finish_call.args
            ev.emit("claim", claim=claim)

            # 4. VERIFY (and let the executor fix problems, a bounded number of times)
            answered_from_memory = follow_up and not spec.changes_data and ctx.step == steps_before
            for round_no in range(self.settings.max_verify_rounds + 1):
                if claim.get("status") not in ("completed", "no_action_needed") or answered_from_memory:
                    break
                verdict = await verify(self.llm, ctx, self.budget, self._task_context(text, follow_up), spec,
                                       claim, environment, actions_from=actions_before)
                verdicts.append(verdict)
                if verdict.get("passed") or round_no == self.settings.max_verify_rounds:
                    break
                ev.emit("phase", phase="execute", reason="fixing verification failures")
                feedback = fill(VERIFY_FAILED_FEEDBACK, report=_format_verdict(verdict))
                finish_call, pending = await self._loop.resume(pending, feedback)
                claim = finish_call.args
                ev.emit("claim", claim=claim)
            self._pending = pending
            status, reason = _final_status(claim, verdicts, answered_from_memory)
            if status == "needs_review" and verdicts:
                # Never show the user an unverified "done" as if it were true.
                claim = {**claim, "summary": f"NOT VERIFIED. The worker reported: \"{claim.get('summary', '')}\" "
                                             f"But an independent check found: {verdicts[-1].get('summary', '')}"}
        except _Stop as s:
            status, reason = s.status, s.reason
        except (Cancelled, BudgetExceeded, LLMError, Exception) as e:  # noqa: BLE001 - always produce a report
            if isinstance(e, Cancelled):
                status, reason = "cancelled", str(e)
            elif isinstance(e, BudgetExceeded):
                status, reason = "failed", f"Stopped: {e}."
                summary = self._stopped_summary(str(e), actions_before)
                if verdicts and not verdicts[-1].get("passed"):
                    summary += f" An independent check found: {verdicts[-1].get('summary', '')}"
                # Replace any earlier (unverified) claim: the run did not finish.
                claim = {**(claim or {}), "status": "failed", "summary": summary}
            elif isinstance(e, LLMError):
                status, reason = "error", f"Model/provider error: {e}"
            else:
                status, reason = "error", f"Unexpected error: {type(e).__name__}: {e}"
                ev.emit("error", message=reason, traceback=traceback.format_exc())
            # The conversation may be mid-turn (tool calls without results): follow-ups start a fresh
            # conversation; the working memory (facts) and the browser are kept.
            self._loop, self._pending = None, []

        ev.emit("phase", phase="report")
        self.turns.append({"turn": turn_no, "message": text, "status": status,
                           "summary": (claim or {}).get("summary") or reason})
        report = build_report(run_id=self.run_id, run_dir=self.run_dir, task=text, status=status,
                              status_reason=reason, claim=claim, spec=spec.model_dump() if spec else None,
                              verdicts=verdicts, ctx=ctx, events=ev.history, llm=self.llm, started=started,
                              finished=time.time(), model=self.llm.model, turns=self.turns,
                              actions_from=actions_before)
        ev.emit("run_finished", status=status, reason=reason, summary=report["summary"], turn=turn_no,
                verified=report["verified"], report=str(self.run_dir / "report.html"),
                metrics=report["metrics"], can_follow_up=self.keep_open and not self.closed)
        return report

    # ------------------------------------------------------------------ helpers
    def _stopped_summary(self, why: str, actions_from: int) -> str:
        """A useful message when a budget stops the run before the worker could report (no extra LLM call)."""
        changes = [a["description"] for a in self.ctx.actions[actions_from:]]
        facts = self.ctx.memory.recall()
        parts = [f"I stopped before finishing: {why}."]
        parts.append(f"Changes made: {'; '.join(changes)}." if changes else "No data was changed.")
        if facts:
            parts.append("What I found: " + "; ".join(f"{f.key} = {f.value}" for f in facts[-6:]) + ".")
        parts.append("Please check the systems (see the evidence report) before retrying.")
        return " ".join(parts)

    def _spec_input(self, text: str, follow_up: bool) -> str:
        if not follow_up:
            return f"User request:\n{text}"
        history = "\n".join(f"- Turn {t['turn']}: user asked: {t['message']!r} -> {t['status']}: {t['summary']}"
                            for t in self.turns)
        return (f"Conversation so far in this session:\n{history}\n\nFacts recorded so far:\n{self.ctx.memory.render()}"
                f"\n\nNew message from the user (interpret it in the context above):\n{text}")

    def _task_context(self, text: str, follow_up: bool) -> str:
        if not follow_up:
            return text
        prev = "; ".join(f"{t['message']!r} ({t['status']})" for t in self.turns)
        return f"Follow-up request: {text}\n(Earlier in this session the user asked: {prev})"

    async def _ask(self, question: str, options: list[str] | None = None) -> str | None:
        req = HumanRequest(kind="clarification", question=question, options=options or [])
        self.events.emit("human_request", request=req.to_dict())
        resp = await self.ctx.human.request(req, self.settings.human_timeout_s)
        self.events.emit("human_response", request_id=req.id, response=resp.to_dict())
        return None if resp.timed_out or not resp.answer.strip() else resp.answer.strip()

    async def _clarify(self, spec: TaskSpec) -> str | None:
        """Ask blocking questions before acting. Returns text for the kickoff, or None if unanswered."""
        if not spec.blocking_questions:
            return ""
        answers = []
        for q in spec.blocking_questions:
            answer = await self._ask(q.question, q.options)
            if answer is None:
                return None
            answers.append(f"- Q: {q.question}\n  A: {answer}")
        return "# Clarifications from the user (before starting)\n" + "\n".join(answers) + "\n\n"


class _Stop(Exception):
    def __init__(self, status: str, reason: str) -> None:
        super().__init__(reason)
        self.status, self.reason = status, reason


def _format_verdict(v: dict[str, Any]) -> str:
    lines = [f"Summary: {v.get('summary')}"]
    for c in v.get("criteria") or []:
        if not c.get("met"):
            lines.append(f"- NOT MET: {c.get('criterion')} | observed: {c.get('evidence')}")
    lines += [f"- Issue: {i}" for i in v.get("issues") or []]
    return "\n".join(lines)


def _final_status(claim: dict[str, Any], verdicts: list[dict[str, Any]],
                  answered_from_memory: bool = False) -> tuple[str, str]:
    st = claim.get("status")
    if answered_from_memory and st in ("completed", "no_action_needed"):
        return "answered", "Answered from what was already established in this session (nothing was changed)."
    if st in ("completed", "no_action_needed"):
        if verdicts and verdicts[-1].get("passed"):
            if st == "no_action_needed":
                return "succeeded", "Nothing needed to change - independently verified."
            return "succeeded", "Completed and independently verified."
        return "needs_review", "The worker reported completion but verification did not pass - please review."
    if st == "blocked":
        return "blocked", "Stopped safely: needs a decision or information from the user."
    return "failed", "The worker could not complete the task."
