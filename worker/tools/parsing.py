"""Deterministic parsing of the values business documents are made of: money, dates, payment terms.

LLMs are good at deciding *which* value matters and bad at reliably converting it
("1,12,100/-" -> 112100.00, "Net 30 from 05 Sep 2026" -> 2026-10-05, 48,500 + 1,12,100).
These pure functions do the conversion exactly, and are exposed to the agent as tools
(``convert_amount``, ``convert_date``, ``due_date_from_terms``, ``calculate``). They are also used
by the safety layer: the approval rules and the pre-write provenance check parse amounts and dates
with the same code, so a value cannot slip past a rule just because it was written differently.
"""

from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

# --------------------------------------------------------------------------- money
_CURRENCY_TOKENS = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupees": "INR",
    "$": "USD", "usd": "USD", "us$": "USD",
    "€": "EUR", "eur": "EUR", "£": "GBP", "gbp": "GBP",
}
_CUR_RE = re.compile(r"(₹|\$|€|£|\b(?:rs\.?|inr|usd|us\$|eur|gbp|rupees)(?![a-z]))", re.I)
_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str | None

    def plain(self) -> str:
        """Format the ERP and most forms accept: 112100.00"""
        return f"{self.amount:.2f}"


def parse_amount(text: str | int | float | Decimal) -> Money | None:
    """Parse '₹1,12,100.00', 'Rs. 48500', '1,12,100/-', 'USD 2,400.00', '$18,250.00', '-150.00'.

    Indian digit grouping (1,12,100) and western grouping (112,100) both work because all commas are
    separators. Returns None if there is no number in the text.
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float, Decimal)):
        return Money(Decimal(str(text)), None)
    s = str(text).strip()
    if not s:
        return None
    cur = None
    m = _CUR_RE.search(s)
    if m:
        cur = _CURRENCY_TOKENS.get(m.group(1).lower().rstrip(), _CURRENCY_TOKENS.get(m.group(1).lower()))
        s = s[:m.start()] + " " + s[m.end():]
    s = s.replace("/-", " ").replace(" ", " ")
    s = re.sub(r"-\s+(?=\d)", "-", s)          # "-$150" -> "- 150" -> "-150"
    nums = _NUM_RE.findall(s)
    if not nums:
        return None
    raw = nums[0].replace(",", "")
    negative = raw.startswith("-") or bool(re.search(r"\(\s*" + re.escape(nums[0]) + r"\s*\)", s))
    try:
        value = Decimal(raw.lstrip("-"))
    except InvalidOperation:
        return None
    return Money(-value if negative else value, cur)


# --------------------------------------------------------------------------- dates
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}


class AmbiguousDate(ValueError):
    def __init__(self, text: str, readings: list[date]) -> None:
        super().__init__(f"'{text}' is ambiguous: could be " + " or ".join(d.isoformat() for d in readings))
        self.readings = readings


def _mk(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(text: str, day_first: bool | None = None) -> date:
    """Parse '22 Oct 2026', 'October 22, 2026', '22/10/2026', '2026-10-22', '22-10-2026', '22.10.2026'.

    Numeric dates like '03/04/2026' are ambiguous (3 April or 4 March). Unless ``day_first`` is given,
    they raise AmbiguousDate when both readings are valid, so the caller has to decide (or ask).
    """
    s = str(text).strip().rstrip(".")
    if not s:
        raise ValueError("empty date")
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)            # ISO
    if m:
        d = _mk(int(m[1]), int(m[2]), int(m[3]))
        if d:
            return d
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?[\s\-]+([A-Za-z]{3,9})\.?,?[\s\-]+(\d{4})", s)   # 22 Oct 2026
    if m and m[2][:3].lower() in _MONTHS:
        d = _mk(int(m[3]), _MONTHS[m[2][:3].lower()], int(m[1]))
        if d:
            return d
    m = re.fullmatch(r"([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)          # October 22, 2026
    if m and m[1][:3].lower() in _MONTHS:
        d = _mk(int(m[3]), _MONTHS[m[1][:3].lower()], int(m[2]))
        if d:
            return d
    m = re.fullmatch(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})", s)   # 22/10/2026, 03/04/2026
    if m:
        a, b, y = int(m[1]), int(m[2]), int(m[3])
        dmy, mdy = _mk(y, b, a), _mk(y, a, b)
        if day_first is True and dmy:
            return dmy
        if day_first is False and mdy:
            return mdy
        if dmy and mdy and dmy != mdy:
            raise AmbiguousDate(s, [dmy, mdy])
        if dmy or mdy:
            return dmy or mdy  # type: ignore[return-value]
    raise ValueError(f"unrecognised date '{s}'")


def date_candidates(text: str) -> list[date]:
    """All dates a string could mean (both readings for ambiguous numeric dates)."""
    try:
        return [parse_date(text)]
    except AmbiguousDate as e:
        return e.readings
    except ValueError:
        return []


_DATE_FINDER = re.compile(
    r"\b(\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/\-.]\d{1,2}[/\-.]\d{4}|"
    r"\d{1,2}(?:st|nd|rd|th)?[\s\-]+[A-Za-z]{3,9}\.?,?[\s\-]+\d{4}|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4})\b")


def find_dates(text: str) -> set[date]:
    """Every date mentioned in a block of text (both readings of ambiguous ones)."""
    out: set[date] = set()
    for m in _DATE_FINDER.finditer(text or ""):
        out.update(date_candidates(m.group(1)))
    return out


def find_amounts(text: str) -> set[Decimal]:
    """Every number in a block of text, with grouping commas removed (used for provenance checks)."""
    out: set[Decimal] = set()
    for raw in _NUM_RE.findall((text or "").replace("/-", " ")):
        try:
            out.add(abs(Decimal(raw.replace(",", ""))))
        except InvalidOperation:
            pass
    return out


def due_from_terms(issue: str | date, terms: str) -> date:
    """'Net 30' (or 'net30', '30 days') from the issue date -> due date."""
    start = issue if isinstance(issue, date) else parse_date(issue)
    m = re.search(r"(?:net\s*)?(\d{1,3})\s*(?:days?)?", terms, re.I)
    if not m or not re.search(r"net|day", terms, re.I):
        raise ValueError(f"unrecognised payment terms '{terms}' (expected e.g. 'Net 30')")
    return start + timedelta(days=int(m.group(1)))


# --------------------------------------------------------------------------- arithmetic
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def calculate(expression: str) -> Decimal:
    """Safely evaluate + - * / ( ) on numbers written like amounts ('48,500 + 1,12,100.00').
    No names, calls or attributes - so this cannot run arbitrary code."""
    expr = re.sub(r"(₹|\$|€|£|\b(?:rs\.?|inr|usd|eur|gbp)\b|/-)", " ", expression, flags=re.I)
    expr = re.sub(r"(?<=\d),(?=\d)", "", expr)

    def ev(node: ast.AST) -> Decimal:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return Decimal(str(node.value))
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
            return _OPS[type(node.op)](ev(node.operand))
        raise ValueError("only numbers and + - * / ( ) are allowed")

    try:
        return ev(ast.parse(expr.strip(), mode="eval"))
    except (SyntaxError, ZeroDivisionError) as e:
        raise ValueError(f"cannot evaluate '{expression}': {e}") from e
