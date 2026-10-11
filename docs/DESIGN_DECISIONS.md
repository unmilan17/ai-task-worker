# Design decisions (and why)

Each decision lists **the choice**, **the alternatives considered**, and **the reasoning**. These are the
"why did you build it this way?" answers for the interview.

---

### D1. Build a realistic simulated company instead of automating real websites
**Choice:** three purpose-built web apps (supplier portal, ERP with API, webmail) with realistic traps
and injectable failures.

**Alternatives:** automate public sites (e.g. a demo shop); mock the tools entirely (return canned JSON).

**Why:**
* The brief says "*a narrow prototype that genuinely works is better than a broad system where most
  functionality is mocked*" and forbids real company systems. Purpose-built apps give **real**
  browsers, real HTTP, real validation and real state, with nothing mocked from the agent's point of view.
* I control the ground truth, so outcomes can be checked **deterministically** (did the right bill land
  in the DB?). On public websites you can only eyeball success.
* I can design the **hard parts** in on purpose: misleading sort order, a credit note that looks like an
  invoice, name mismatches between systems, date-format conversion, duplicates, an approval threshold,
  an ambiguous request, a prompt-injection email, and chaos modes. Those traps separate an agent that
  *understands goals* from one that follows clicks.
* Reproducible (seeded data relative to "today", reset endpoint) makes evals and tests possible.

---

### D2. A hand-written orchestrator, not LangChain / LangGraph / an agent framework
**Choice:** about 200 lines of explicit Python (`core/agent.py` + `core/loop.py`) on top of the provider SDK.

**Alternatives:** LangGraph state machine, CrewAI/AutoGen multi-agent frameworks, the Anthropic SDK
tool runner.

**Why:**
* **Explainability and control.** The interview asks me to explain, debug and modify the system. Every
  behaviour (when to retry, when to verify, what goes back to the model) is visible in plain code, not
  hidden in framework callbacks.
* The parts that matter (error taxonomy, idempotency-aware retries, guardrails between the model and
  the world, verification) need **hooks between "model chose a tool" and "tool runs"**. A thin custom
  loop gives those hooks directly.
* The loop is small and stable. The interesting engineering is in the tools, safety and verification,
  which a framework wouldn't provide anyway.
* Trade-off accepted: I re-implement things a framework gives for free (persistence/resume of runs). Listed in the roadmap.

---

### D3. See pages as text with element refs (DOM snapshot), not pixels
**Choice:** inject `dom_snapshot.js`, which renders the page as compact text with inline tokens like
`[12:button "Save bill"]`, and act by ref number. Screenshots are taken for evidence, and the model can
request a vision look (`browser_screenshot(look=true)`).

**Alternatives:** pure vision computer-use (screenshot → click x,y); raw HTML; CSS selectors chosen by the model.

**Why:**
* **Exactness:** amounts and dates are read as text, with no OCR errors on `4,812.75`.
* **Cost/speed:** a snapshot is ~1–3k tokens vs. a screenshot image per step, and it needs no coordinate guessing.
* **Unambiguous actions:** refs map to exactly one element. Stale refs fail loudly (`not_found` + "take a fresh snapshot") instead of clicking the wrong pixel.
* Raw HTML is too long and noisy. Model-written CSS selectors are brittle.
* Trade-off: canvas-heavy or visually encoded UIs need vision. The screenshot tool is the escape hatch, and the roadmap includes a vision fallback for such apps.

---

### D4. Every action returns the new state ("observe" is built into the tool)
**Choice:** `navigate`, `click` and `type(submit=true)` return a fresh snapshot plus HTTP status. Form
submissions with 4xx/5xx are returned as **classified failures** with the page content.

**Why:** "Observe the result of each action" should not depend on the model remembering to look. It
also halves the number of model turns (no separate "now snapshot" call) and makes failure detection
structural (status codes, validation messages on the page).

---

### D5. Classified errors + idempotency-aware retries
**Choice:** every failure has an `ErrorKind` (`transient`, `not_found`, `auth`, `validation`,
`permission`, `rejected_by_user`, `invalid_input`, `fatal`) and a recovery hint. The executor
**auto-retries only transient errors on idempotent calls**, with exponential backoff. Everything else goes back
to the model.

**Alternatives:** retry everything N times; never retry; let the model handle all errors.

**Why:**
* Blind retries are dangerous for writes. If "Save bill" times out, the bill may already be saved, and
  retrying creates a **duplicate**. So `browser_click`, `browser_type(submit=true)` and `POST` are
  never auto-retried. Their failure hint says "check whether it took effect first". The mild-chaos
  test proves this (`tests/test_agent_loop.py::test_failed_form_submit_is_not_blindly_retried`).
* Retrying a validation error is pointless: the same input fails the same way. The model must change the input.
* Auto-retrying cheap, safe things (a GET that hit a 503) saves model calls and tokens.

---

### D6. Loop / failure-streak detection that forces reflection
**Choice:** `LoopDetector` injects a note when the same call produces the same outcome 3 times, or
after 3 consecutive failures, asking the model to diagnose and change strategy (fresh snapshot, check
state, alternative route, ask the user, or give up honestly).

**Why:** the most common failure mode of LLM agents is repetition. A cheap deterministic detector
plus a nudge gets most of the benefit of a separate "reflection" LLM call at none of the cost. Hard
budgets (steps, time, money) are the final backstop.

---

### D7. A separate UNDERSTAND step that fixes the success criteria up front
**Choice:** before acting, one structured-output call produces a `TaskSpec` with the end goal and
**objectively checkable success criteria**.

**Why:**
* It makes "done" concrete *before* the agent can rationalise what it did. The verifier checks exactly
  these criteria.
* It catches out-of-scope requests and missing information before any side effects.
* It gives the UI an initial plan immediately (product feel).
* Structured outputs (pydantic schema) means no fragile JSON parsing.

---

### D8. Independent verification (fresh context, read-only) instead of trusting "I'm done"
**Choice:** two layers. (1) A deterministic rule: a data-changing task with no recorded successful write
is rejected immediately. (2) An **auditor agent** in a *new* conversation, with read-only tools
enforced by the Guard, checks every success criterion against the live systems and returns per-criterion
evidence. A failed verdict sends concrete findings back to the executor (at most 2 rounds).

**Alternatives:** self-verification in the same conversation; deterministic task-specific checks only.

**Why:**
* Self-checks inherit the executor's misconceptions (if it read the wrong invoice, it will "confirm" the wrong invoice). A fresh context does not.
* Read-only enforcement means verification can't "fix" things silently or cause damage.
* Task-specific deterministic checks don't generalise to new tasks. The auditor is task-agnostic because
  it works from the TaskSpec criteria. (The eval suite *does* use deterministic checks, but only to
  measure the agent, never inside the agent.)
* The rule check is free and catches the most blatant failure, a hallucinated completion.

---

### D9. Guardrails enforced in code, outside the model
**Choice:** tools describe side effects as `ProposedAction`s. The `Guard` applies `config/policy.yaml`:
host allow-list, blocked paths, verifier read-only mode, and approval rules (amount > 10,000, destructive
verbs, HTTP DELETE). The human is asked through the `HumanChannel`. A rejection becomes a
`rejected_by_user` error the agent must respect.

**Alternatives:** tell the model the rules and trust it; a "request_approval" tool the model calls voluntarily.

**Why:**
* Prompts are advice and code is enforcement. A prompt-injected or confused model can't bypass a check
  that runs in the tool layer. The phishing email in the inbox explicitly tries this.
* Approval at the **moment of the side effect, with the real payload** (the actual form fields being
  submitted) is more trustworthy than approval of the model's *description* of what it intends to do.
* Approvals are fingerprinted, so a retry of the identical action doesn't re-ask, but a changed payload does.
* Rules are configuration. A finance team can change the threshold without touching code.

---

### D10. Credentials never reach the model (secret placeholders + redaction)
**Choice:** the model types `{{secret:erp.password}}`. The vault substitutes it inside the tool, and
every tool output, trace entry and report is redacted back to placeholders. Tests assert that the real
values never appear in the trace or report.

**Why:** prompts, traces and screenshots are logged and shared. A model that never holds a secret
can't leak it or be tricked into revealing it. It also mirrors how production systems use secret managers.

---

### D11. Append-only conversation history
**Choice:** both adapters store each assistant turn exactly as returned and never edit earlier turns. The
OpenAI-compatible adapter keeps provider extras on tool calls (Gemini's thought signatures). The Anthropic
adapter keeps thinking blocks. Notes (reflection, budget warnings, verifier feedback) are *appended* as text
blocks after tool results.

**Why:** reasoning models bind their reasoning state to the conversation. Gemini 3 requires its thought
signatures to be sent back with function-call turns, and Claude binds thinking blocks to the exact
conversation prefix that produced them. Editing history invalidates them (an error on newer accounts) and also breaks prompt
caching. Append-only keeps the model's reasoning intact across steps and keeps cost down via caching.
Trade-off: long tasks grow the context. Snapshots are compact (D3) and capped. For very long tasks the
roadmap has server-side context editing/compaction.

---

### D12. Model choice: a free tier by default (Gemini Flash-Lite), paid models optional
**Choice:** Google Gemini's free tier (`gemini-3.5-flash-lite`) through its OpenAI-compatible endpoint, with
`reasoning_effort=medium` and a client-side rate limiter. Groq, OpenRouter, Ollama, OpenAI and Anthropic Claude are
one `.env` line away.

**Alternatives:** paid frontier models only; local models only; Groq's free tier as the default.

**Why free Gemini Flash-Lite:** no credit card, native tool calling, a long context window and vision. The larger
`gemini-3.8-flash` was the first choice, but its free tier allowed only 5 requests per minute and 20 per day when
tested (2026-10), while one task needs about 30-45 requests. Flash-Lite allowed 500 requests per day: about a dozen
tasks, or one full eval run. Groq's free
tier is faster but its tokens-per-minute cap is smaller than a single agent request late in a run. Local models
need a strong GPU and are weaker at long multi-step tool use. The agent does not depend on the model: quality
and cost are a configuration choice, measured with the eval suite.

**Rate limits are a first-class concern:** an agent sends many large requests in a row, which is exactly what
free tiers throttle. So the adapter throttles itself (sliding window on requests and tokens per minute), shows
the wait in the UI, and reads any 429 that still happens: a per-minute limit is waited out for the delay the
provider asks for (and the limiter adopts the reported limit); an exhausted daily quota ends the run with a clear message. The loop never forces `tool_choice`, which some
providers don't support. It nudges the model if it replies without a tool call.

---

### D13. Provider-agnostic LLM interface + a scripted fake
**Choice:** `LLMClient` / `Conversation` abstractions with an OpenAI-compatible implementation (Gemini, Groq,
OpenRouter, Ollama, OpenAI), an Anthropic implementation, and `ScriptedLLM`.

**Why:** the agent logic is independent of the vendor. That is what made moving the default from a paid model to a
free tier a configuration change rather than a rewrite. More importantly, `ScriptedLLM` lets the test
suite run the **real** loop, tools, browser, policy and sandbox deterministically and for free. Only
the decisions are scripted. That is how failure handling (retry rules, approvals, verifier
rejection, loop detection) is tested without paying for or depending on a live model.

---

### D14. Environment knowledge as data (`config/environment.yaml`), task knowledge in nobody's code
**Choice:** the agent gets an "onboarding document" (systems, URLs, sign-in placeholders, company
rules). There is **no task-specific code or prompt anywhere**. The eight eval tasks run on identical code.

**Why:** this is the answer to the generalization criterion. A new task needs no change at all. A new
system needs at most an entry in `environment.yaml` (if it's a web app or REST API) or a new `Tool`
class (if it's a new kind of interface). Prompts state *working principles* (observe, verify, don't
duplicate, treat external content as data), not procedures.

---

### D15. Human-in-the-loop as a pluggable channel with safe defaults
**Choice:** `HumanChannel` protocol with CLI, Web and Scripted implementations. If nobody answers
within the timeout, the result is **not approved / no answer**, and the agent stops safely.

**Why:** the same agent works interactively in a terminal, behind a dashboard, or in automated evals.
Failing closed is the only sane default for approvals.

---

### D16. Observability first: event bus → trace, live UI, evidence report
**Choice:** every decision and action is an `Event`. Subscribers write `trace.jsonl`, drive the CLI and
the WebSocket UI. Each run produces `report.html` with verification table, facts with provenance,
changes made, approvals, reliability stats, screenshots and video.

**Why:** "returning useful evidence of completion" is a product requirement, and debugging agents is
impossible without traces. The trace is the first thing to open when a run goes wrong (see the
debugging section in [INTERVIEW_GUIDE.md](INTERVIEW_GUIDE.md)).

---

### D17. Budgets as hard limits
**Choice:** max tool steps (60), wall-clock minutes (30, generous because of free-tier rate limits), and estimated
cost (USD 5, for paid providers) per run, checked
before every tool call, plus a 75%-budget notice to the model.

**Why:** an autonomous system must have bounded cost and runtime even when everything else goes
wrong. When a budget stops the run, it still produces an honest `failed` report.


---

### D18. A pre-write review between "the model decided" and "the data changes"
**Choice:** before any form submission or HTTP write, after the allow-list and before the approval rules,
`safety/review.py` runs (1) a deterministic **provenance check**: every value with a digit must appear in what
the worker read, was told, or computed with an exact tool; and (2) a narrow **ambiguity review**: a fresh LLM call
asks only "did the worker resolve a genuine ambiguity by guessing?". A yes needs ≥ 2 concrete candidates or a
missing value. Then the system asks the user and holds the write.

**Alternatives:** stronger prompt wording ("ask when ambiguous"); relying on verification afterwards; a full
second-opinion LLM on every step.

**Why:** the live eval showed the default model picking silently between two same-day invoices even with an
explicit instruction. Prompting it harder didn't help, so the check moved into the system, at the one moment
where a wrong guess becomes a record (verification afterwards cannot undo a wrong payment or entry). The provenance part is
free and deterministic and catches invented values (e.g. a due date the document doesn't have). The ambiguity part
is cheap (one call per write, capped), has a narrow question, and has a guard against needless "shall I proceed?"
questions (it must name candidates).
**Trade-off:** values read from an image must be recorded with `remember` to count as sourced, and the review is
still an LLM judgement. It is measured by suite cases F1–F3 and F6.

**Lesson from the first live run (F1):** the reviewer originally saw only the last 8 observations. The agent had seen
the second vendor ("Acme Corporation Pvt Ltd") eleven times, all early in the run, so the reviewer missed it.
The reviewer now also gets *every line from the whole run that mentions the request's key terms*
(`review.key_terms` / `evidence_lines`: capitalised names like "Acme" first). Context selection matters as much as the question.

### D19. Exact conversion tools instead of model arithmetic
**Choice:** `convert_amount`, `convert_date` (refuses to guess `03/04/2026`), `due_date_from_terms`, `calculate`,
all backed by `tools/parsing.py`, which is also used by the approval rules and the provenance check.

**Why:** models are good at deciding *which* value matters and unreliable at converting it (`1,12,100/-`, Net 30
from 05 Sep, 48,500 + 1,12,100). Pure functions are exact and unit-tested (suite J6). Sharing one parser between the
agent and the guardrails closed a real bug: the approval rule read `Rs. 48500` as 0.485, so a high-value amount
written that way could have slipped past it.

### D20. A circuit breaker per system
**Choice:** count consecutive failed *calls* (each already retried with spaced backoff) per host. A host with a
failure on record gets single probes instead of retries; the 2nd failed call opens the circuit and further
calls fail immediately with `system_unavailable` and the hint "stop and report what you extracted".

**Why:** retries handle blips, but during an outage an agent keeps trying new angles until its budget is gone.
Counting calls rather than attempts keeps it from tripping on random 503s in a merely flaky system (hard chaos
has 30 % errors and still passes). Outage cost: 3 spaced attempts + 1 probe, then a clean failure report (suite D5).

### D21. Follow-up turns in the same session
**Choice:** `Agent(keep_open=True)` keeps the browser, working memory and the executor conversation;
`follow_up(message)` runs another turn with its own task spec, budget, verification and report. A pure question
answered without tools is `answered` and skips verification.

**Why:** real users continue ("what was the due date?", "do the same for Globex", "they sent a revised invoice").
Starting over would redo work and lose context. Appending to the same conversation is cheap and keeps model
reasoning valid (D11). If a turn dies mid-loop, the conversation is dropped (facts kept) so follow-ups start cleanly.

### D22. A second workplace as data, plus a state-based suite harness
**Choice:** the 60-case test suite's fixtures are a second sandbox seed (`centralign`) with its own onboarding
document and policy (`config/centralign/`). A runner resets the seed, flips one named fault switch, runs the
task text only, and grades the **database state** with the suite's verdicts.

**Why:** it proves generalization honestly. The same agent code runs a second company (INR, other traps, new modules,
an unrelated HelpDesk app), and every edit needed would show up as a code change. Grading from state (not
the agent's words) is the only way to measure false success and silent damage, the two outcomes that
matter most.


### D23. Detect repeated "successful" writes
**Choice:** each successful write gets a signature: the set of its ID/amount/date values, normalised, so a form post
and an API call with the same data match. On the 2nd identical successful write, the loop tells the agent to
stop submitting and report that the system accepts but does not save (or that it is creating duplicates).
When a budget stops a run anyway, the summary is built from the run's own record (changes made, facts found,
what to check) instead of just "budget exhausted".

**Why (from the live E1 run, silent-drop switch):** the agent checked its own work and found the bill missing, then
tried the API, got "201 Created" twice with nothing saved, and kept trying until the 60-step budget ran out. The
outcome was safe (nothing written, honest failure) but slow and unhelpful. Repeated identical writes are a
generic signal: either the system is broken, or the agent is about to create duplicates.

### D24. Never show an unverified "done"; let verification have its own budget
**Choice:** if verification did not pass, the user-facing summary starts with "NOT VERIFIED" and quotes what the
independent check found, instead of the worker's claim. A budget stop replaces any earlier claim with a summary
built from the run's record. The verifier has its own step cap (25) that does not count against the executor's 60.

**Why (live E1 and F1 re-runs):** in E1 the status was correctly `failed`, but the summary still said "recorded as
B-1003" (the executor's false claim). Status and message must never disagree. In F1 a long but correct execution
used 55 of 60 shared steps, so verification ran out of budget and a correct result was reported as a failure.

### D25. The worker's own echoes are not a source; "use your judgement" does not supply a value
**Choice:** the provenance check's corpus leaves out the outputs of tools that only echo what the worker itself
entered (`browser_type`, `browser_select`: "Typed '2026-10-05' into element 13"). The pre-write reviewer is told
that a delegating answer ("use your best judgement", "proceed") can settle a choice between candidates, but
never a value that appears in no source. The executor prompt says the same, and the reviewer's "held" message
carries a hint for missing values: if the answer does not state the value, do not assume one.

**Why (live D7, corrupt-pdf switch):** the only document with Globex's payment terms was unreadable. The worker
typed an assumed due date (+30 days) into the form. The provenance check passed because the corpus contained the
worker's own "Typed '2026-10-05'" message. The reviewer then flagged the assumption and asked the user. The
scripted user replied "Please proceed with your best judgement", and the reviewer treated that as permission.
Result: a bill with an invented due date, reported as verified (silent damage). Both holes are generic: any
value the agent typed was "sourced" by its own typing, and any vague reply unlocked any guess.

### D26. Fill a whole form in one call
**Choice:** `browser_fill_form(fields=[{ref, value}...], submit_ref=?)` fills text fields and dropdowns and
optionally clicks the submit button. The submit goes through exactly the same policy/review path as
`browser_click`, and it is never auto-retried.

**Why (live E4: twelve similar bills, one rejected):** one tool call per field cost about 9 steps per bill, so
the 60-step budget ran out after 3 of 4 bills. That was an honest stop, but too slow. Batching the fields is the
generic fix (the budget still protects against loops). The budget-stop summary now also names the record each
change was about (`invoice_number=INV-W-08`), not just "Submitted form" three times.

### D27. A deterministic "which one did you mean?" check before the LLM reviewer
**Choice:** before the LLM ambiguity review, `name_collisions()` looks at every write that references an entity by
ID (`vendor_id=V-…`). It takes the names in the request (runs of capitalised words, e.g. "Acme" or "Acme Corp")
and finds the table rows the worker saw that contain that name as whole words and exactly one ID of the same
kind. If two or more different IDs match, the system asks the user ("Which one did you mean: Acme Corp (V-0017)
or Acme Corporation Pvt Ltd (V-0018)?") and holds the write. It does not ask if the user already answered with one
of the candidates.

**Why (live F1 on the fallback model):** with `gemini-3.1-flash-lite` the LLM reviewer was shown both Acme rows
and still called the choice "not ambiguous", so the bill went to the wrong vendor (silent damage). Name
collisions are a common, mechanical pattern, so they should not depend on how strong the model is. Two rules
keep false alarms away: whole-word matching ("Acme Corp" does not match "Acme Corporation") and one-ID-per-line
(a dropdown that lists every vendor on one line is ignored). The LLM reviewer still covers the cases that need
judgement (ambiguous dates, missing values, ties on "latest").

### D28. Close the laundering routes: computed and remembered values must have a real origin
**Choice:** the provenance corpus is built in order. A result of an exact computation tool (`due_date_from_terms`,
`convert_date`, `convert_amount`, `calculate`) counts as a source only if that call's own inputs were sourced: the
terms text, the issue date, and every sizeable number in an expression. `remember`ed facts count only after the
worker has looked at an image (a scanned page). A value read from an image can only be written down through
`remember`; anywhere else, a fact is just the worker's own claim.

**Why (live D7 re-run):** the D25 fix worked. The invented due date was blocked ("do not appear in anything you
read"). The model then called `due_date_from_terms(issue_date, "Net 30")` with terms it had made up (the real terms
were only in the unreadable PDF) and `remember`ed the result, and both counted as sources. Any check that trusts
the agent's own outputs can be gamed by the agent, so the corpus now only trusts what came from outside.

Two related fixes from the same night:
- **`find_dates` missed dates after a reference.** In `X-9  Date 2026-09-05` the pattern for "9 &lt;Month&gt; 2026"
  swallowed "9  Date 2026", so the real date was never seen and a correct value could be blocked. The finder now
  uses a lookahead, so candidates can overlap.
- **Empty model replies move to the next model.** `gemini-2.5-flash` once answered with no text and no tool
  calls (0 output tokens) four times, and the run ended "stopped calling tools". An empty reply is retried once.
  A second empty reply raises `EmptyResponse`, and the fallback chain tries the next model for that request
  (without marking the model's quota as used up).
- **A busy provider is skipped, not waited out.** Mistral's free plan answered `429 Rate limit exceeded` to every
  capable model for minutes at a time. A model that still has a next model behind it in the chain now waits once,
  then gives this request to the next model. Before, it retried six times at 30 s each and then failed the run.
  Providers that require `parameters` on every tool (Mistral) get an empty schema per request; Gemini rejects
  one, so it is added only where needed.

### D29. Stay in scope: exact names decide, look-alikes are other records
**Choice:** two deterministic rules in the pre-write review, plus matching prompt rules.
- **Scope check (`unnamed_entity`).** A write that references an entity the user did not name is blocked when the
  request's name matches a different entity of the same kind and the written one is a look-alike (it shares a word
  with that name). Only an answer that explicitly names the extra record unlocks it.
- **Exact name wins (`exactly_named`).** If the LLM reviewer reports "multiple candidates" but the request uses one
  candidate's full name, the verdict is overruled and no question is asked.
- **Prompt rules.** "Use your judgement" means the narrowest reading of the user's exact words. Questions (totals,
  status) must look in every system that can hold relevant records and say what was counted. Actions the rules allow
  with approval (deletes, payments) are never "out of scope", and the model must not invent rules.

**Why (live, 2026-10-11):**
- **B1.** "Make sure Acme Corp is up to date". The LLM reviewer called the exact name ambiguous and asked. The scripted
  "use your best judgement" reply then led the worker to also write a bill for the look-alike Acme Corporation Pvt
  Ltd (silent damage).
- **F5.** "Delete … old invoices" was refused up front with an invented "record retention" rule, instead of listing
  the invoices and asking.
- **A5.** "How much do we owe" counted only the bills already entered, not the invoice received but not yet entered.

The first two are code checks, because model judgement on scope proved unreliable. A5 is a prompt rule, because
deciding which records are relevant needs judgement.
