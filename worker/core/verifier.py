"""VERIFY phase: an independent, read-only check of the real world.

The executor's own "I'm done" is not trusted. Verification has two layers:

1. **Rule check (deterministic, free).** If the task spec says the task changes
   data but the run recorded no successful data-changing action, the claim is
   rejected immediately - no LLM needed.

2. **Auditor agent (LLM).** A *fresh* conversation (it does not inherit the
   executor's reasoning, so it does not inherit its mistakes) with read-only
   tools. The Guard blocks every write while it runs. It checks each success
   criterion from the TaskSpec directly in the systems - e.g. opens the ERP,
   finds the bill, compares the amount and due date with the invoice - and
   returns a per-criterion verdict with evidence.

If verification fails, the executor gets the verifier's findings and a chance
to fix things (bounded by settings.max_verify_rounds).
"""

from __future__ import annotations

import json
from typing import Any

from ..llm.base import LLMClient
from ..runtime.context import RunContext
from ..runtime.executor import ToolExecutor
from ..tools.registry import verifier_registry
from .loop import Budget, ToolLoop
from .prompts import VERIFIER_KICKOFF, VERIFIER_SYSTEM, fill
from .task_spec import TaskSpec

VERIFIER_MAX_STEPS = 25


def user_decisions(ctx: RunContext) -> str:
    """What the user decided during the run, as recorded by the system (not as claimed by the worker):
    approval-gate decisions and answers to the worker's questions."""
    lines = [f"- Approval gate: {d['action']} ({d['reason']}) -> "
             f"{'APPROVED' if d['approved'] else 'NOT approved'} by the user" for d in ctx.guard.decisions]
    questions: dict[str, str] = {}
    for e in ctx.events.history:
        if e.type == "human_request" and e.data["request"].get("kind") != "approval":
            questions[e.data["request"]["id"]] = e.data["request"]["question"]
        elif e.type == "human_response" and e.data["request_id"] in questions:
            answer = e.data["response"].get("answer") or "(no answer)"
            lines.append(f"- Worker asked: {questions[e.data['request_id']]} -> user answered: {answer}")
    return "\n".join(lines) or "(none)"


def rule_check(spec: TaskSpec, claim: dict[str, Any], ctx: RunContext) -> dict[str, Any] | None:
    """Cheap deterministic checks. Returns a failed verdict, or None if nothing is wrong."""
    if spec.changes_data and not ctx.actions:
        return {"passed": False, "method": "rule",
                "summary": "The task requires changing data, but no successful data-changing action was recorded "
                           "during the run.",
                "criteria": [], "issues": ["No form submission / API write / file write succeeded."]}
    return None


async def verify(llm: LLMClient, ctx: RunContext, budget: Budget, task: str, spec: TaskSpec,
                 claim: dict[str, Any], environment: str) -> dict[str, Any]:
    ctx.events.emit("phase", step=ctx.step, phase="verify")
    failed = rule_check(spec, claim, ctx)
    if failed:
        ctx.events.emit("verification", step=ctx.step, verdict=failed)
        return failed

    evidence = "\n".join(f"- {e.get('claim')}: {e.get('proof')}" for e in claim.get("evidence") or []) or "(none)"
    actions = "\n".join(f"- step {a['step']}: {a['description']}" + (f" fields={json.dumps(a.get('fields'))}"
                                                                     if a.get("fields") else "")
                        for a in ctx.actions) or "(none)"
    kickoff = fill(VERIFIER_KICKOFF, task=task,
                   criteria="\n".join(f"{i + 1}. {c}" for i, c in enumerate(spec.success_criteria)),
                   status=claim.get("status"), summary=claim.get("summary"), evidence=evidence,
                   facts=ctx.memory.render(), actions=ctx.vault.redact(actions),
                   decisions=ctx.vault.redact(user_decisions(ctx)))

    registry = verifier_registry()
    conv = llm.conversation(fill(VERIFIER_SYSTEM, environment=environment), registry.specs(), purpose="verifier")
    loop = ToolLoop(conv, ToolExecutor(registry, ctx), ctx, budget, terminal_tool="submit_verdict",
                    label="verifier", max_steps=VERIFIER_MAX_STEPS)
    ctx.read_only = True
    try:
        call, _ = await loop.start(kickoff)
    finally:
        ctx.read_only = False
    verdict = {"method": "auditor", **call.args}
    # A verdict claiming success while listing an unmet criterion is not a pass.
    if any(not c.get("met") for c in verdict.get("criteria") or []):
        verdict["passed"] = False
    ctx.events.emit("verification", step=ctx.step, verdict=verdict)
    return verdict
