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
An answer that only delegates ("use your best judgement", "go ahead", "proceed") resolves a choice between
candidates the worker had listed, but it does NOT supply a missing_value: a value that appears in no source
is still missing unless the user stated it or explicitly confirmed that exact value.

If ambiguous, list the concrete candidates (at least two for multiple_candidates/ambiguous_value; the
missing field for missing_value) and write ONE short question for the user."""


# Tools whose output only echoes free text the worker itself typed - not a source ("Typed '<value>' into element 13").
_ECHO_TOOLS = {"browser_type"}          # a dropdown choice is page data: its value comes from the page


# Exact computation tools: their result is a source only if their inputs were (no "Net 30" made up and then
# turned into a due date). Maps tool -> the arguments that carry data.
_DERIVED_TOOLS = {"convert_amount": ("text",), "convert_date": ("text",), "due_date_from_terms": ("issue_date", "terms"),
                  "calculate": ("expression",)}
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _inputs_sourced(name: str, args: dict[str, Any], corpus: str) -> bool:
    for key in _DERIVED_TOOLS[name]:
        value = str(args.get(key) or "")
        if name == "calculate":         # every sizeable number in the expression must be an amount we saw
            amounts = find_amounts(corpus)
            for n in _NUMBER.findall(value):
                m = parse_amount(n)
                if m is not None and abs(m.amount) >= 100 and abs(m.amount) not in amounts:
                    return False
        elif unsourced_values({key: value}, corpus):
            return False
    return True


def _corpus(ctx: "RunContext") -> str:
    """Everything the worker read, was told, or computed (from sourced inputs) in this run.

    Not sources: echoes of what the worker typed, computations on values it made up, and `remember`ed facts -
    except after the worker has looked at an image (a scanned page), because a value read from an image can
    only be written down through `remember`."""
    parts = [ctx.task_text]
    args_by_call: dict[str, dict[str, Any]] = {}
    seen_image = False
    for e in ctx.events.history:
        if e.type == "tool_call":
            args_by_call[e.data.get("call_id", "")] = e.data.get("args") or {}
        elif e.type in ("tool_result", "observation") and e.data.get("output") and e.data.get("name") not in _ECHO_TOOLS:
            name = e.data.get("name")
            if name in _DERIVED_TOOLS and not _inputs_sourced(name, args_by_call.get(e.data.get("call_id", ""), {}),
                                                              "\n".join(parts)):
                continue
            seen_image = seen_image or bool(e.data.get("image"))
            parts.append(str(e.data["output"]))
        elif e.type == "human_response":
            parts.append(str(e.data.get("response", {}).get("answer", "")))
        elif e.type == "memory_fact" and seen_image:
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


_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"}
_ID_VALUE = re.compile(r"^([A-Za-z]{1,6}-?)\d{2,}$")


def name_phrases(task: str) -> list[str]:
    """Runs of capitalised words that name something: 'Find ... from Contoso Ltd, ...' -> ['Contoso Ltd'];
    "Log Fabrikam Inc's latest invoice" -> ['Fabrikam Inc']. A possessive ends a name."""
    phrases, cur = [], []
    for i, m in enumerate(re.finditer(r"[A-Za-z][A-Za-z0-9&.-]*('s|')?", task or "")):
        word = m.group(0)
        base = re.sub(r"('s|')$", "", word).rstrip(".")
        named = i > 0 and base[:1].isupper() and base.lower() not in _GENERIC | _MONTHS and len(base) >= 2
        if named:
            cur.append(base)
        if cur and (not named or word != base):
            phrases.append(" ".join(cur))
            cur = []
    if cur:
        phrases.append(" ".join(cur))
    return list(dict.fromkeys(phrases))


def name_collisions(task: str, fields: dict[str, Any], outputs: list[str]) -> list[str]:
    """Deterministic 'which one did you mean?' check for a write that references an entity by ID (vendor_id=...).

    If the name the user gave matches rows for two or more different IDs of the same kind (Contoso -> X-0101 and
    X-0102), return a label per candidate. Only lines with exactly one ID of that kind count (a table row), so a
    dropdown listing every vendor on one line cannot create a false alarm. Whole words only: 'Contoso Ltd' does not
    match 'Contoso Ltda'."""
    for value, family in _entity_refs(fields):
        for phrase in name_phrases(task):
            found = _ids_named(phrase, family, outputs)
            if len(found) >= 2:
                return [f"{name} ({eid})" for eid, name in sorted(found.items())]
    return []


def unnamed_entity(task: str, fields: dict[str, Any], outputs: list[str]) -> tuple[str, list[str]] | None:
    """Scope check: the write references an entity the user did not name, while the user's words do name another
    entity of the same kind (asked about 'Contoso Ltd' = X-0101, about to write for X-0102 'Contoso Holdings').
    Returns (the written ID, labels of the entities the request names), or None."""
    phrases = name_phrases(task)
    for value, family in _entity_refs(fields):
        named: dict[str, str] = {}
        for phrase in phrases:
            named.update(_ids_named(phrase, family, outputs))
        if not named or value in named:
            continue
        # Only look-alikes: the written entity's own rows share a word with the name the user gave.
        words = {w.lower() for p in phrases for w in re.findall(r"[A-Za-z]{3,}", p)}
        rows = [ln for ln in _rows(outputs) if set(family.findall(ln)) == {value}]
        if any(re.search(r"\b" + re.escape(w) + r"\b", ln, re.I) for w in words for ln in rows):
            return f"{_row_name(value, rows)} ({value})", [f"{name} ({eid})" for eid, name in sorted(named.items())]
    return None


def _row_name(eid: str, rows: list[str]) -> str:
    """The display name next to an ID in table rows: 'Name (ID)' cells first, else the first plain text cell."""
    cells = [c.strip() for ln in rows for c in re.split(r"\||\t", ln)]
    for c in cells:
        if c.endswith(f"({eid})"):
            return c[: -len(eid) - 2].strip()
    plain = [c for c in cells if re.search(r"[A-Za-z]{3,}", c) and eid not in c and "[" not in c]
    return plain[0][:80] if plain else eid


def exactly_named(task: str, candidates: list[str]) -> str | None:
    """If the request uses one candidate's full name ('Contoso Ltd' among 'Contoso Ltd (X-0101)' and 'Contoso
    Holdings (X-0102)'), that candidate is meant - a look-alike name is a different record, not an ambiguity."""
    phrases = {p.lower() for p in name_phrases(task)}
    exact = [c for c in candidates if re.sub(r"\s*\([^)]*\)\s*$", "", c).strip().lower() in phrases]
    return exact[0] if len(exact) == 1 else None


def _entity_refs(fields: dict[str, Any]) -> list[tuple[str, re.Pattern]]:
    """(value, pattern for IDs of the same kind) for every '..._id' field holding an ID-like value."""
    out = []
    for k, v in _flatten(fields):
        m = _ID_VALUE.match(str(v).strip())
        if m and re.search(r"(^|_)id$", k.split(".")[-1], re.I):
            out.append((str(v).strip(), re.compile(r"\b" + re.escape(m.group(1)) + r"\d{2,}\b")))
    return out


def _ids_named(phrase: str, family: re.Pattern, outputs: list[str]) -> dict[str, str]:
    """ID -> shortest name, for table rows (one ID of this kind per line) that contain `phrase` as whole words."""
    pat = re.compile(r"\b" + r"\s+".join(map(re.escape, phrase.split())) + r"\b", re.I)
    found: dict[str, str] = {}
    for line in _rows(outputs):
        ids = set(family.findall(line))
        if len(ids) != 1 or not pat.search(line):
            continue
        eid = ids.pop()
        cells = [c.strip() for c in re.split(r"\||\t", line) if pat.search(c)]
        name = re.sub(r"\s*\(" + re.escape(eid) + r"\)", "", cells[0] if cells else phrase).strip()
        if eid not in found or len(name) < len(found[eid]):
            found[eid] = name[:80]
    return found


def _rows(outputs: list[str]):
    """Every line the worker saw, plus one ' | '-joined row per JSON record (an API response spreads one record
    over many lines, so its ID and name would never share a line)."""
    for out in outputs:
        yield from out.splitlines()
        start = min((i for i in (out.find("{"), out.find("[")) if i >= 0), default=-1)
        if start < 0:
            continue
        try:
            data = json.loads(out[start:])
        except ValueError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                scalars = [str(v) for v in item.values() if isinstance(v, (str, int, float)) and not isinstance(v, bool)]
                if scalars:
                    yield " | ".join(scalars)
                stack.extend(v for v in item.values() if isinstance(v, (dict, list)))


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


def _answered(ctx: "RunContext", candidates: list[str]) -> bool:
    """True if the user already answered with one of the candidates (by name or ID) in this run."""
    answers = [str(e.data.get("response", {}).get("answer") or "") for e in ctx.events.history
               if e.type == "human_response"]
    for cand in candidates:
        m = re.match(r"(.*) \(([^)]*)\)$", cand)
        name, eid = (m.group(1), m.group(2)) if m else (cand, "")
        for a in answers:
            if (eid and eid.lower() in a.lower()) or re.search(r"\b" + re.escape(name) + r"\b", a, re.I):
                return True
    return False


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
        if action.fields:
            outputs = [str(e.data["output"]) for e in ctx.events.history
                       if e.type in ("tool_result", "observation") and e.data.get("output")
                       and e.data.get("name") not in _ECHO_TOOLS]
            stray = unnamed_entity(ctx.task_text, action.fields, outputs)
            if stray and not _answered(ctx, [stray[0]]):
                self._record(ctx, action, "out_of_scope", f"{stray[0]} is not what the request names "
                                                          f"({', '.join(stray[1])})")
                raise ToolError(ErrorKind.VALIDATION,
                                f"Write blocked before it ran: it is for {stray[0]}, but the request names "
                                f"{' / '.join(stray[1])}. A similar name is a different record.",
                                "Do only what the user asked for. Leave this record out, unless the user explicitly "
                                "names it; 'use your judgement' does not widen the task.")
        if self.clarifications >= MAX_CLARIFICATIONS_PER_RUN:
            return
        if action.fields:
            outputs = [str(e.data["output"]) for e in ctx.events.history
                       if e.type in ("tool_result", "observation") and e.data.get("output") and e.data.get("name") not in _ECHO_TOOLS]
            cands = name_collisions(ctx.task_text, action.fields, outputs)
            if cands and not _answered(ctx, cands):
                verdict = WriteReview(ambiguous=True, kind="multiple_candidates", candidates=cands,
                                      question=f"Which one did you mean: {' or '.join(cands)}?",
                                      explanation=f"the name in the request matches {len(cands)} different records: "
                                                  + ", ".join(cands))
                await self._hold(ctx, action, verdict)
        if not self.ambiguity or self.reviews >= MAX_REVIEWS_PER_RUN:
            return
        self.reviews += 1
        verdict = await self._ask_reviewer(action, ctx, corpus)
        if verdict is None or not verdict.ambiguous:
            self._record(ctx, action, "ok", verdict.explanation if verdict else "review skipped (model error)")
            return
        if verdict.kind == "multiple_candidates" and (exact := exactly_named(ctx.task_text, verdict.candidates)):
            self._record(ctx, action, "ok", f"the request uses the exact name of {exact}; look-alikes are other records")
            return
        concrete = len(verdict.candidates) >= 2 or (verdict.kind == "missing_value" and verdict.candidates)
        if not concrete or not verdict.question.strip():
            self._record(ctx, action, "ok", f"vague concern ignored: {verdict.explanation}")
            return
        await self._hold(ctx, action, verdict)

    async def _hold(self, ctx: "RunContext", action: "ProposedAction", verdict: WriteReview) -> None:
        """Ask the user the reviewer's question, then raise so the write does not run with a guess."""
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
                        f"The user was asked: {verdict.question!r} and answered: {resp.answer!r}.",
                        "If the answer does not state the missing value itself, do not assume one: finish and "
                        "report what is missing." if verdict.kind == "missing_value" else None)

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
