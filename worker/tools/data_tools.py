"""Exact data-conversion tools for the agent (thin wrappers over tools/parsing.py).

The model decides which values to use; these tools make the conversions exact:
amounts to plain numbers, dates to the format a system needs, payment terms to
due dates, and arithmetic.
"""

from __future__ import annotations

from typing import Any

from ..runtime.context import RunContext
from . import parsing
from .base import ErrorKind, Tool, ToolError, ToolResult, schema

_FORMATS = {"YYYY-MM-DD": "%Y-%m-%d", "DD-MM-YYYY": "%d-%m-%Y", "DD/MM/YYYY": "%d/%m/%Y",
            "MM/DD/YYYY": "%m/%d/%Y", "D MON YYYY": "%d %b %Y"}


class ConvertAmount(Tool):
    name = "convert_amount"
    description = ("Convert a written amount (e.g. '₹1,12,100.00', 'Rs. 48500', '1,12,100/-', 'USD 2,400.00') "
                   "into a plain number with 2 decimals plus the detected currency. Use it before typing "
                   "amounts into forms.")
    input_schema = schema({"text": {"type": "string"}}, ["text"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        m = parsing.parse_amount(args["text"])
        if m is None:
            raise ToolError(ErrorKind.INVALID_INPUT, f"No amount found in {args['text']!r}.")
        cur = m.currency or "unknown (not stated in the text)"
        return ToolResult.success(f"amount={m.plain()} currency={cur}", data={"amount": m.plain(), "currency": m.currency})


class ConvertDate(Tool):
    name = "convert_date"
    description = ("Convert a written date (e.g. '22 Oct 2026', 'October 22, 2026', '22/10/2026') into a target "
                   f"format ({', '.join(_FORMATS)}). Numeric dates such as 03/04/2026 are reported as AMBIGUOUS "
                   "unless you pass day_first (true = DD/MM, false = MM/DD); do not guess - find evidence in the "
                   "document or ask the user.")
    input_schema = schema({
        "text": {"type": "string"},
        "target_format": {"type": "string", "enum": list(_FORMATS)},
        "day_first": {"type": "boolean", "description": "Only if the document establishes the convention"},
    }, ["text", "target_format"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        try:
            d = parsing.parse_date(args["text"], day_first=args.get("day_first"))
        except parsing.AmbiguousDate as e:
            raise ToolError(ErrorKind.VALIDATION, f"AMBIGUOUS: {e}.",
                            "Look for evidence of the date convention in the document (another unambiguous "
                            "date, the issuer's country). If there is none, ask the user which reading is right.")
        except ValueError as e:
            raise ToolError(ErrorKind.INVALID_INPUT, str(e))
        out = d.strftime(_FORMATS[args["target_format"]])
        return ToolResult.success(f"{args['text']!r} -> {out} (ISO {d.isoformat()})", data={"date": d.isoformat()})


class DueDateFromTerms(Tool):
    name = "due_date_from_terms"
    description = ("Compute a due date from an issue date and payment terms such as 'Net 30'. Use when an invoice "
                   "states terms but no due date, and say in your summary that the date was computed.")
    input_schema = schema({"issue_date": {"type": "string"}, "terms": {"type": "string"}}, ["issue_date", "terms"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        try:
            due = parsing.due_from_terms(args["issue_date"], args["terms"])
        except parsing.AmbiguousDate as e:
            raise ToolError(ErrorKind.VALIDATION, f"AMBIGUOUS issue date: {e}.")
        except ValueError as e:
            raise ToolError(ErrorKind.INVALID_INPUT, str(e))
        return ToolResult.success(f"Due date = {due.isoformat()} ({args['terms']} from {args['issue_date']})",
                                  data={"due_date": due.isoformat()})


class Calculate(Tool):
    name = "calculate"
    description = ("Exact arithmetic on amounts: + - * / and parentheses, e.g. '48,500 + 1,12,100.00'. Use it for "
                   "totals instead of mental arithmetic.")
    input_schema = schema({"expression": {"type": "string"}}, ["expression"])

    async def run(self, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        try:
            v = parsing.calculate(args["expression"])
        except ValueError as e:
            raise ToolError(ErrorKind.INVALID_INPUT, str(e))
        return ToolResult.success(f"{args['expression']} = {v.quantize(parsing.Decimal('0.01'))}",
                                  data={"result": str(v)})


DATA_TOOLS: list[type[Tool]] = [ConvertAmount, ConvertDate, DueDateFromTerms, Calculate]
