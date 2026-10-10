"""Tools that act on the agent itself rather than on the outside world:
memory, planning, talking to the user, and ending the run.
"""

from __future__ import annotations

from typing import Any

from ..memory.working_memory import Fact
from ..runtime.context import RunContext
from ..runtime.human import HumanRequest
from .base import ErrorKind, Tool, ToolError, ToolResult, schema


class Remember(Tool):
    name = "remember"
    description = ("Save an important fact you discovered (e.g. an invoice amount or due date) together with "
                   "where you found it. Saved facts are used for verification and the final report, so record "
                   "every value you will rely on.")
    input_schema = schema({
        "key": {"type": "string", "description": "Short identifier, e.g. 'invoice.due_date'"},
        "value": {"type": "string"},
        "source": {"type": "string", "description": "Where it came from, e.g. 'invoice detail page in the supplier portal'"},
    }, ["key", "value", "source"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        url = ctx.browser.page.url if ctx.browser and ctx.browser.page else None
        shot = None
        if ctx.browser and ctx.browser.page:
            shot = await ctx.browser.screenshot(f"evidence {args['key']}")
            if shot:
                ctx.add_evidence(shot, f"Source of '{args['key']}' = {args['value']}")
        fact = Fact(key=args["key"], value=str(args["value"]), source=args["source"], step=ctx.step,
                    url=url, screenshot=shot)
        prev = ctx.memory.remember(fact)
        ctx.events.emit("memory_fact", step=ctx.step, fact=fact.__dict__)
        note = f" (previous value was {prev.value!r})" if prev and prev.value != fact.value else ""
        return ToolResult.success(f"Remembered {fact.key} = {fact.value!r}{note}.")


class Recall(Tool):
    name = "recall"
    description = "List the facts saved so far (optionally filtered by a search term)."
    input_schema = schema({"query": {"type": "string"}})

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        facts = ctx.memory.recall(args.get("query"))
        if not facts:
            return ToolResult.success("(no matching facts)")
        return ToolResult.success("\n".join(f"- {f.key} = {f.value} (source: {f.source})" for f in facts))


class UpdatePlan(Tool):
    name = "update_plan"
    description = ("Replace your working plan (the checklist shown to the user). Use it to mark progress and to "
                   "revise the plan when something unexpected happens.")
    input_schema = schema({
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["pending", "in_progress", "done", "skipped", "blocked"]},
                "note": {"type": "string"},
            },
            "required": ["id", "title", "status"],
        }},
    }, ["steps"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        if not args.get("steps"):
            raise ToolError(ErrorKind.INVALID_INPUT, "Provide at least one plan step.")
        ctx.plan.replace(args["steps"])
        ctx.events.emit("plan_updated", step=ctx.step, plan=ctx.plan.as_list())
        return ToolResult.success("Plan updated.")


class AskUser(Tool):
    name = "ask_user"
    description = ("Ask the user a question and wait for the answer. Use ONLY when you cannot proceed safely "
                   "on your own: the request is genuinely ambiguous (e.g. several records match), information is "
                   "missing, or something looks wrong/suspicious. Offer concrete options when possible.")
    input_schema = schema({
        "question": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "context": {"type": "string", "description": "What you found that makes this necessary"},
    }, ["question"])
    verifier_allowed = False

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        req = HumanRequest(kind="clarification", question=args["question"], options=args.get("options") or [],
                           details={"context": args.get("context", "")})
        ctx.events.emit("human_request", step=ctx.step, request=req.to_dict())
        resp = await ctx.human.request(req, ctx.settings.human_timeout_s)
        ctx.events.emit("human_response", step=ctx.step, request_id=req.id, response=resp.to_dict())
        ctx.memory.remember(Fact(key=f"user_answer.{req.id}", value=resp.answer or "(no answer)",
                                 source=f"User answer to: {args['question']}", step=ctx.step))
        if resp.timed_out or not resp.answer.strip():
            return ToolResult.failure(ErrorKind.REJECTED_BY_USER, "The user did not answer.",
                                      "Do not guess on anything risky. Finish with status 'blocked' and explain "
                                      "what you need.")
        return ToolResult.success(f"User answered: {resp.answer}")


class Finish(Tool):
    """Handled by the agent loop (ends the execution phase); run() is never called."""

    name = "finish"
    description = ("Call when you are done (or cannot continue). Your claim is then independently verified "
                   "against the real systems before the user sees it. 'summary' is the message for the user: at "
                   "most 100 words, stating the key facts (record IDs, amounts, dates, where it was entered). If "
                   "you did not finish, the summary must say what failed, what you tried, the current state of the "
                   "systems (what was and was not changed) and what the user should do next. 'evidence' lists "
                   "concrete proof (record IDs, values, where you confirmed them).")
    input_schema = schema({
        "status": {"type": "string", "enum": ["completed", "no_action_needed", "blocked", "failed"],
                   "description": "completed = goal achieved by changes you made; no_action_needed = the goal was "
                                  "already true or there was nothing to do (e.g. the record already exists, nothing "
                                  "found to process), so nothing was changed; blocked = needs the user (declined/"
                                  "ambiguous/missing information); failed = could not achieve it"},
        "summary": {"type": "string", "description": "At most 100 words."},
        "evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim": {"type": "string"}, "proof": {"type": "string"}},
            "required": ["claim", "proof"],
        }},
        "notes_for_user": {"type": "array", "items": {"type": "string"},
                           "description": "Assumptions (e.g. 'due date computed from Net 30 terms'), anomalies, "
                                          "suspicious content, values extracted but not entered, follow-ups"},
    }, ["status", "summary"])
    verifier_allowed = False

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:  # pragma: no cover
        return ToolResult.success("finish acknowledged")


class SubmitVerdict(Tool):
    """Verifier-only. Handled by the verifier loop."""

    name = "submit_verdict"
    description = "Submit your verification verdict after checking every success criterion against the real systems."
    input_schema = schema({
        "criteria": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "criterion": {"type": "string"},
                "met": {"type": "boolean"},
                "evidence": {"type": "string", "description": "What you observed (values, record IDs, page)"},
            },
            "required": ["criterion", "met", "evidence"],
        }},
        "passed": {"type": "boolean", "description": "True only if every criterion is met"},
        "summary": {"type": "string"},
        "issues": {"type": "array", "items": {"type": "string"}},
    }, ["criteria", "passed", "summary"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:  # pragma: no cover
        return ToolResult.success("verdict acknowledged")


AGENT_TOOLS: list[type[Tool]] = [Remember, Recall, UpdatePlan, AskUser, Finish]
