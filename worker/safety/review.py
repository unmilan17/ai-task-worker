"""Pre-write review: a last check before any data-changing action runs.

Two failure modes that verification afterwards cannot undo:

1. **Invented values.** The model writes a due date it never saw ("assume 30 days"), or a
   mis-copied amount. -> *Provenance check* (deterministic): every value about to be written that
   contains a digit (amounts, dates, IDs, invoice numbers) must appear in something the worker
   actually read (tool outputs: pages, files, API responses), was told (task text, user answers)
   or computed with an exact tool (convert_date, due_date_from_terms, calculate - their outputs
   are tool outputs too). Amounts and dates are compared by *value*, not text, so "$1,234.50"
   on the page matches "1234.50" in the form, and "November 1, 2026" matches "01-11-2026".

2. **Guessing between candidates.** Two vendors match the name the user gave; two invoices share the latest
   date; "03/04/2026" could be March or April. Models often pick one silently. -> *Ambiguity
   review* (LLM, fresh context, narrow question): "did the worker resolve a genuine ambiguity by
   guessing?" To avoid needless questions, a 'yes' only counts if the reviewer names at least two
   concrete candidates (or a value that is missing from every source). Then the *system* asks the
   user - the worker does not have to remember to - and the write is held until the worker retries
   with the answer in hand.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from ..llm.base import LLMClient, LLMError
from ..runtime.human import HumanRequest
from ..tools.base import ErrorKind, ToolError
from ..tools.parsing import date_candidates, find_amounts, find_dates, parse_amount

if TYPE_CHECKING:
    from ..runtime.context import RunContext
    from .policy import ProposedAction

_SKIP_FIELDS = {"next", "csrf", "csrf_token", "token", "_token", "password", "page", "sort"}
_AMOUNT_LIKE = re.compile(r"^\s*(?:₹|\$|€|£|rs\.?|inr|usd|eur|gbp)?\s*-?[\d,]+(?:\.\d+)?\s*(?:/-)?\s*$", re.I)
MAX_REVIEWS_PER_RUN = 8
MAX_EVIDENCE_LINES = 40
_GENERIC = {"latest", "newest", "invoice", "invoices", "bill", "bills", "find", "enter", "into", "from", "system",
            "internal", "extract", "amount", "date", "dates", "tell", "once", "done", "with", "that", "this", "what",
            "have", "their", "your", "please", "record", "records", "update", "make", "sure", "check", "whether",
            "matches", "match", "finance", "tool", "most", "recent", "last", "every", "each", "them", "they", "will",
            "would", "could", "should", "log", "logs", "add", "new", "set", "the", "and", "for", "due", "our"}
MAX_CLARIFICATIONS_PER_RUN = 2


class WriteReview(BaseModel):
    ambiguous: bool
    kind: str            # none | multiple_candidates | ambiguous_value | missing_value
    candidates: list[str]
    question: str
    explanation: str

    @classmethod
    def scripted_default(cls) -> "WriteReview":
        """What the ScriptedLLM returns in tests unless a test scripts a review."""
        return cls(ambiguous=False, kind="none", candidates=[], question="", explanation="")


REVIEW_SYSTEM = """\
You review ONE proposed data-changing action of an AI worker before it runs. You do not do the task.

Decide only this: did the worker resolve a genuine ambiguity in the user's request by guessing?
It IS ambiguous when, given what the worker observed and the user's answers so far:
- multiple_candidates: two or more different records/entities fit the request equally well (e.g. the user
  gave a company name and two different vendors match it; "the latest invoice" but two invoices share the
  latest date), and nothing in the request picks one;
- ambiguous_value: a value being written can be read in more than one way and the source does not settle
  it (e.g. a numeric date like 03/04/2026 with no evidence whether it is day-first);
- missing_value: a required value being written does not appear in any source and was assumed
  (e.g. a due date when the document has neither a due date nor payment terms).
Look carefully at the list of lines that mention the request's key terms: if it shows two or more DIFFERENT
entities (e.g. two vendor names or vendor IDs) that both contain the user's words, and the user's words do not
say which one, that is multiple_candidates - even if the worker never mentioned the other one.
It is NOT ambiguous when the request plus observations determine the choice (one exact match, a clear
latest date, an explicit document number, a value computed from stated terms), or when the user has
already answered the question. Ordinary work (formatting dates, converting currency symbols, looking up
an ID in a directory) is not ambiguity. Be strict: when in doubt, it is NOT ambiguous.

If ambiguous, list the concrete candidates (at least two for multiple_candidates/ambiguous_value; the
missing field for missing_value) and write ONE short question for the user."""


def _corpus(ctx: "RunContext") -> str:
    """Everything the worker read, was told, or computed in this run."""
    parts = [ctx.task_text]
    for e in ctx.events.history:
        if e.type == "tool_result" and e.data.get("output"):
            parts.append(str(e.data["output"]))
        elif e.type == "human_response":
            parts.append(str(e.data.get("response", {}).get("answer", "")))
        elif e.type == "memory_fact":
            f = e.data.get("fact", {})
            parts.append(f"{f.get('value', '')} {f.get('source', '')}")
    return "\n".join(parts)


def key_terms(task: str) -> list[str]:
    """The words of the request that identify *what* it is about (names first, e.g. a company name).
    Capitalised words (not the first word) are preferred; otherwise non-generic words of 4+ letters."""
    words = re.findall(r"[A-Za-z][A-Za-z0-9&-]*", task or "")
    names = [w for i, w in enumerate(words) if i > 0 and w[0].isupper()]
    names = [n for n in names if n.lower() not in _GENERIC and len(n) >= 3]
    if names:
        return list(dict.fromkeys(n.lower() for n in names))
    plain = [w.lower() for w in words if len(w) >= 4 and w.lower() not in _GENERIC]
    return list(dict.fromkeys(plain))[:6]


def evidence_lines(task: str, outputs: list[str], limit: int = MAX_EVIDENCE_LINES) -> list[str]:
    """Every distinct line the worker saw (whole run, not just recently) that mentions a key term of the request.
    This is what lets the reviewer notice a second matching vendor/document seen many steps ago."""
    terms = key_terms(task)
    if not terms:
        return []
    seen: dict[str, None] = {}
    for out in outputs:
        for line in out.splitlines():
            ln = line.strip()[:220]
            if ln and ln not in seen and any(t in ln.lower() for t in terms):
                seen[ln] = None
    return list(seen)[:limit]


def _flatten(d: Any, prefix: str = ""):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _flatten(v, f"{prefix}{k}.")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield from _flatten(v, f"{prefix}{i}.")
    else:
        yield prefix.rstrip("."), d


def unsourced_values(fields: dict[str, Any], corpus: str) -> list[tuple[str, str]]:
    """Return (field, value) pairs whose value cannot be traced to the corpus."""
    amounts = find_amounts(corpus)
    dates = find_dates(corpus)
    norm_corpus = re.sub(r"\s+", " ", corpus).lower()
    missing = []
    for key, value in _flatten(fields):
        if value is None or isinstance(value, bool):
            continue
        name = key.split(".")[-1].lower()
        text = str(value).strip()
        if not text or name in _SKIP_FIELDS or "*" in text or not re.search(r"\d", text):
            continue
        cands = date_candidates(text)
        if cands:
            if not any(c in dates for c in cands):
                missing.append((key, text))
            continue
        if _AMOUNT_LIKE.match(text):
            m = parse_amount(text)
            if m is not None and abs(m.amount) not in amounts:
                missing.append((key, text))
            continue
        if re.sub(r"\s+", " ", text).lower() not in norm_corpus:
            missing.append((key, text))
    return missing


class WriteReviewer:
    def __init__(self, llm: LLMClient | None, provenance: bool = True, ambiguity: bool = True) -> None:
        self.llm = llm
        self.provenance = provenance
        self.ambiguity = ambiguity and llm is not None
        self.reviews = 0
        self.clarifications = 0
        self.log: list[dict[str, Any]] = []

    async def review(self, action: "ProposedAction", ctx: "RunContext") -> None:
        """Raises ToolError if the write must not run (yet)."""
        corpus = _corpus(ctx)
        if self.provenance and action.fields:
            missing = unsourced_values(action.fields, corpus)
            if missing:
                listing = ", ".join(f"{k}={v!r}" for k, v in missing)
                self._record(ctx, action, "unsourced", listing)
                raise ToolError(ErrorKind.UNSOURCED,
                                f"Write blocked before it ran: these values do not appear in anything you read, "
                                f"were told, or computed with a tool: {listing}.")
        if not self.ambiguity or self.reviews >= MAX_REVIEWS_PER_RUN or self.clarifications >= MAX_CLARIFICATIONS_PER_RUN:
            return
        self.reviews += 1
        verdict = await self._ask_reviewer(action, ctx, corpus)
        if verdict is None or not verdict.ambiguous:
            self._record(ctx, action, "ok", verdict.explanation if verdict else "review skipped (model error)")
            return
        concrete = len(verdict.candidates) >= 2 or (verdict.kind == "missing_value" and verdict.candidates)
        if not concrete or not verdict.question.strip():
            self._record(ctx, action, "ok", f"vague concern ignored: {verdict.explanation}")
            return
        self.clarifications += 1
        self._record(ctx, action, "ambiguous", verdict.explanation)
        req = HumanRequest(kind="clarification", question=verdict.question, options=verdict.candidates[:4],
                           details={"context": f"Before writing: {verdict.explanation}"})
        ctx.events.emit("human_request", step=ctx.step, request=req.to_dict())
        resp = await ctx.human.request(req, ctx.settings.human_timeout_s)
        ctx.events.emit("human_response", step=ctx.step, request_id=req.id, response=resp.to_dict())
        if resp.timed_out or not resp.answer.strip():
            raise ToolError(ErrorKind.REJECTED_BY_USER,
                            f"Not written: the request is ambiguous ({verdict.explanation}) and the user did not "
                            f"answer the question '{verdict.question}'.",
                            "Do not guess. Finish with status 'blocked' and explain what you need.")
        raise ToolError(ErrorKind.NEEDS_INPUT,
                        f"Not written yet: a pre-write review found the choice ambiguous ({verdict.explanation}). "
                        f"The user was asked: {verdict.question!r} and answered: {resp.answer!r}.")

    async def _ask_reviewer(self, action: "ProposedAction", ctx: "RunContext", corpus: str) -> WriteReview | None:
        outputs = [str(e.data["output"]) for e in ctx.events.history if e.type == "tool_result" and e.data.get("output")]
        recent = [o[:2500] for o in outputs[-8:]]
        evidence = evidence_lines(ctx.task_text, outputs)
        prompt = (f"# User request\n{ctx.task_text}\n\n# Proposed action\n{action.description}\n"
                  f"Fields: {json.dumps(action.fields, default=str)[:2000]}\n\n"
                  f"# Facts the worker recorded\n{ctx.memory.render()}\n\n"
                  f"# Every line the worker saw during the whole task that mentions the request's key terms "
                  f"({', '.join(key_terms(ctx.task_text))})\n" + ("\n".join(evidence) or "(none)") +
                  "\n\n# What the worker observed most recently (oldest first)\n" + "\n---\n".join(recent))
        try:
            return await self.llm.structured(REVIEW_SYSTEM, ctx.vault.redact(prompt), WriteReview)  # type: ignore[union-attr]
        except LLMError:
            return None

    def _record(self, ctx: "RunContext", action: "ProposedAction", result: str, detail: str) -> None:
        entry = {"step": ctx.step, "action": action.description, "result": result, "detail": detail}
        self.log.append(entry)
        ctx.events.emit("write_review", **entry)
