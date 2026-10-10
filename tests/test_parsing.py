"""J6: the deterministic parts - amount/date/terms parsing, arithmetic, approval-rule parsing, retry cap."""

from __future__ import annotations

import asyncio
from datetime import date
from decimal import Decimal

import pytest

from worker.safety.policy import _to_number
from worker.tools import parsing as p


@pytest.mark.parametrize("text, amount, currency", [
    ("₹1,12,100.00", "112100.00", "INR"),
    ("Rs. 48500", "48500", "INR"),
    ("1,12,100/-", "112100", None),
    ("USD 2,400.00", "2400.00", "USD"),
    ("$18,250.00", "18250.00", "USD"),
    ("INR 7,50,000", "750000", "INR"),
    ("-$150.00", "-150.00", "USD"),
    ("(150.00)", "-150.00", None),
    ("Total due: ₹ 9,900.00 only", "9900.00", "INR"),
])
def test_parse_amount(text, amount, currency):
    m = p.parse_amount(text)
    assert m is not None and m.amount == Decimal(amount) and m.currency == currency


def test_parse_amount_none_and_plain_format():
    assert p.parse_amount("no number here") is None
    assert p.parse_amount("₹1,12,100").plain() == "112100.00"


def test_approval_rule_parser_handles_indian_formats():
    """Regression: 'Rs. 48500' used to parse as 0.485 and '1,12,100/-' as None, so a high-value amount
    written that way could slip past the approval rule."""
    assert _to_number("Rs. 7,50,000") == 750000.0
    assert _to_number("7,50,000/-") == 750000.0
    assert _to_number("Rs. 48500") == 48500.0


@pytest.mark.parametrize("text", ["22 Oct 2026", "22/10/2026", "2026-10-22", "October 22, 2026", "22-10-2026",
                                  "22nd October 2026", "Oct 22, 2026"])
def test_parse_date_formats(text):
    assert p.parse_date(text) == date(2026, 10, 22)


def test_ambiguous_numeric_date_is_not_guessed():
    with pytest.raises(p.AmbiguousDate) as e:
        p.parse_date("03/04/2026")
    assert set(e.value.readings) == {date(2026, 4, 3), date(2026, 3, 4)}
    assert p.parse_date("03/04/2026", day_first=True) == date(2026, 4, 3)
    assert p.parse_date("25/03/2026") == date(2026, 3, 25)        # only one valid reading


def test_net_terms():
    assert p.due_from_terms("05 Sep 2026", "Net 30") == date(2026, 10, 5)
    assert p.due_from_terms(date(2026, 9, 5), "net15") == date(2026, 9, 20)
    with pytest.raises(ValueError):
        p.due_from_terms("05 Sep 2026", "due on receipt")


def test_calculate_is_exact_and_safe():
    assert p.calculate("48,500 + 1,12,100.00") == Decimal("160600.00")
    assert p.calculate("₹95,000 * 18 / 100") == Decimal("17100")
    for bad in ("__import__('os')", "open('x')", "a + 1"):
        with pytest.raises(ValueError):
            p.calculate(bad)


def test_find_dates_and_amounts_in_text():
    text = "Invoice INV-1057 issued 22 Sep 2026, due 22/10/2026. Total ₹1,12,100.00 (03/04/2026 ref)"
    assert {date(2026, 9, 22), date(2026, 10, 22), date(2026, 4, 3), date(2026, 3, 4)} <= p.find_dates(text)
    assert Decimal("112100.00") in p.find_amounts(text)


def test_retry_wrapper_stops_at_its_cap(tmp_path):
    """The executor retries a transient failure at most max_tool_retries times, then gives up."""
    from worker.config import get_settings
    from worker.llm.base import ToolCall
    from worker.runtime.context import RunContext
    from worker.runtime.events import EventBus
    from worker.runtime.executor import ToolExecutor
    from worker.runtime.human import ScriptedHumanChannel
    from worker.safety.policy import Guard, Policy
    from worker.safety.secrets import SecretVault
    from worker.tools.base import ErrorKind, Tool, ToolError, ToolResult
    from worker.tools.registry import ToolRegistry

    calls = {"n": 0}

    class Flaky(Tool):
        name = "flaky"
        description = "always times out"
        input_schema = {"type": "object", "properties": {}, "required": []}

        async def run(self, args, ctx) -> ToolResult:
            calls["n"] += 1
            raise ToolError(ErrorKind.TRANSIENT, "timeout")

    settings = get_settings(runs_dir=tmp_path, max_tool_retries=2)
    ctx = RunContext(run_id="t", run_dir=tmp_path, settings=settings, events=EventBus("t"),
                     human=ScriptedHumanChannel(), vault=SecretVault({}),
                     guard=Guard(Policy([], [], [])))
    res = asyncio.run(ToolExecutor(ToolRegistry([Flaky()]), ctx).execute(ToolCall("1", "flaky", {})))
    assert not res.ok and calls["n"] == 3          # 1 attempt + 2 retries, then stop
