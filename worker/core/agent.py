"""The orchestrator: one class that runs a task from request to verified report.

    UNDERSTAND -> (CLARIFY) -> EXECUTE [act/observe/reflect loop] -> VERIFY -> REPORT
                                   ^                                    |
                                   +---- verification failed (bounded) -+

Read this file top to bottom to understand the whole system; every phase
delegates to a small module (task_spec, loop, verifier, report).
"""

from __future__ import annotations

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
from ..safety.secrets import SecretVault
from ..tools.browser import BrowserSession
from ..tools.registry import executor_registry
from .loop import Budget, BudgetExceeded, Cancelled, ToolLoop
from .prompts import EXECUTOR_KICKOFF, EXECUTOR_SYSTEM, VERIFY_FAILED_FEEDBACK, environment_block, fill
from .report import build_report
from .task_spec import TaskSpec, understand
from .verifier import verify


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


class Agent:
    def __init__(self, llm: LLMClient, settings: Settings, human: HumanChannel, run_id: str | None = None,
                 on_event: Callable[[Event], None] | None = None, cancel: threading.Event | None = None) -> None:
        self.llm = llm
        self.settings = settings
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
        self.budget = Budget(settings, llm, cancel=cancel or threading.Event())
        if hasattr(llm, "on_wait"):   # free-tier rate limiting: show waits in the CLI/UI instead of silence
            llm.on_wait = lambda s: self.events.emit("rate_limit_wait", step=self.ctx.step, seconds=round(s, 1))

    # ------------------------------------------------------------------ public
    async def run(self, task: str) -> dict[str, Any]:
        ctx, ev = self.ctx, self.events
        started = time.time()
        spec: TaskSpec | None = None
        claim: dict[str, Any] | None = None
        verdicts: list[dict[str, Any]] = []
        status, reason = "error", ""
        ev.emit("run_started", task=task, model=self.llm.model, provider=self.llm.provider,
                run_dir=str(self.run_dir))
        ctx.browser = BrowserSession(self.settings, self.run_dir)
        try:
            environment = environment_block(self.settings.environment_file)

            # 1. UNDERSTAND --------------------------------------------------
            ev.emit("phase", phase="understand")
            spec = await understand(self.llm, task, environment)
            ev.emit("task_spec", spec=spec.model_dump())
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

            # 3. EXECUTE -----------------------------------------------------
            ev.emit("phase", phase="execute")
            registry = executor_registry()
            conv = self.llm.conversation(fill(EXECUTOR_SYSTEM, environment=environment), registry.specs(),
                                         purpose="executor")
            loop = ToolLoop(conv, ToolExecutor(registry, ctx), ctx, self.budget, terminal_tool="finish",
                            label="executor")
            kickoff = fill(EXECUTOR_KICKOFF, task=task, spec=spec.render(), clarifications=clarifications)
            finish_call, pending = await loop.start(kickoff)
            claim = finish_call.args
            ev.emit("claim", claim=claim)

            # 4. VERIFY (and let the executor fix problems, a bounded number of times)
            for round_no in range(self.settings.max_verify_rounds + 1):
                if claim.get("status") != "completed":
                    break
                verdict = await verify(self.llm, ctx, self.budget, task, spec, claim, environment)
                verdicts.append(verdict)
                if verdict.get("passed") or round_no == self.settings.max_verify_rounds:
                    break
                ev.emit("phase", phase="execute", reason="fixing verification failures")
                feedback = fill(VERIFY_FAILED_FEEDBACK, report=_format_verdict(verdict))
                finish_call, pending = await loop.resume(pending, feedback)
                claim = finish_call.args
                ev.emit("claim", claim=claim)

            status, reason = _final_status(claim, verdicts)
        except _Stop as s:
            status, reason = s.status, s.reason
        except Cancelled as e:
            status, reason = "cancelled", str(e)
        except BudgetExceeded as e:
            status, reason = "failed", f"Stopped: {e}."
        except LLMError as e:
            status, reason = "error", f"Model/provider error: {e}"
        except Exception as e:  # noqa: BLE001 - always produce a report
            status, reason = "error", f"Unexpected error: {type(e).__name__}: {e}"
            ev.emit("error", message=reason, traceback=traceback.format_exc())
        finally:
            await ctx.browser.close()
            await ctx.aclose()

        ev.emit("phase", phase="report")
        report = build_report(run_id=self.run_id, run_dir=self.run_dir, task=task, status=status,
                              status_reason=reason, claim=claim, spec=spec.model_dump() if spec else None,
                              verdicts=verdicts, ctx=ctx, events=ev.history, llm=self.llm, started=started,
                              finished=time.time(), model=self.llm.model)
        ev.emit("run_finished", status=status, reason=reason, summary=report["summary"],
                verified=report["verified"], report=str(self.run_dir / "report.html"),
                metrics=report["metrics"])
        return report

    # ------------------------------------------------------------------ helpers
    async def _clarify(self, spec: TaskSpec) -> str | None:
        """Ask blocking questions before acting. Returns text for the kickoff, or None if unanswered."""
        if not spec.blocking_questions:
            return ""
        answers = []
        for q in spec.blocking_questions:
            req = HumanRequest(kind="clarification", question=q.question, options=q.options)
            self.events.emit("human_request", request=req.to_dict())
            resp = await self.ctx.human.request(req, self.settings.human_timeout_s)
            self.events.emit("human_response", request_id=req.id, response=resp.to_dict())
            if resp.timed_out or not resp.answer.strip():
                return None
            answers.append(f"- Q: {q.question}\n  A: {resp.answer}")
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


def _final_status(claim: dict[str, Any], verdicts: list[dict[str, Any]]) -> tuple[str, str]:
    st = claim.get("status")
    if st == "completed":
        if verdicts and verdicts[-1].get("passed"):
            return "succeeded", "Completed and independently verified."
        return "needs_review", "The worker reported completion but verification did not pass - please review."
    if st == "blocked":
        return "blocked", "Stopped safely: needs a decision or information from the user."
    return "failed", "The worker could not complete the task."
