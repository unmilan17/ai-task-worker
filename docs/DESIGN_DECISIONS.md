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
**Choice:** the Anthropic adapter appends `response.content` verbatim (including thinking blocks) and
never edits earlier turns. Notes (reflection, budget warnings, verifier feedback) are *appended* as text
blocks after tool results.

**Why:** current Claude models bind reasoning ("thinking") blocks to the exact conversation prefix that
produced them. Editing history invalidates them (an error on newer accounts) and also breaks prompt
caching. Append-only keeps the model's reasoning intact across steps and keeps cost down via caching.
Trade-off: long tasks grow the context. Snapshots are compact (D3) and capped. For very long tasks the
roadmap has server-side context editing/compaction.

---

### D12. Model and settings: Claude Opus 5.5, adaptive thinking, effort = medium, prompt caching
**Choice:** `claude-opus-5-5` by default (configurable, e.g. `claude-sonnet-5-5` for cheaper runs),
adaptive thinking with summarised display (the summary is shown to the user as the agent's thoughts),
`effort=medium`, top-level prompt caching, and the server-side refusal fallback.

**Why:** multi-step tool use with recovery is where model capability matters most. Effort is the cost
lever, and caching makes each step pay only for new tokens. Forced `tool_choice` isn't supported on
current models, so the loop nudges the model if it replies without a tool call.

---

### D13. Provider-agnostic LLM interface + a scripted fake
**Choice:** `LLMClient` / `Conversation` abstractions with Anthropic, OpenAI and `ScriptedLLM` implementations.

**Why:** the agent logic is independent of the vendor. More importantly, `ScriptedLLM` lets the test
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
**Choice:** max tool steps (60), wall-clock minutes (20), and estimated cost (USD 5) per run, checked
before every tool call, plus a 75%-budget notice to the model.

**Why:** an autonomous system must have bounded cost and runtime even when everything else goes
wrong. When a budget stops the run, it still produces an honest `failed` report.
