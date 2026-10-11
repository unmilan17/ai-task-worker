# Code walkthrough

A guided tour of the code base: what each file does, the important functions, and then the
**exact code path of one request**. Read [ARCHITECTURE.md](ARCHITECTURE.md) first for the big picture.

```
ai-task-worker/
├── worker/                    the AI worker
│   ├── cli.py                 command-line entry point (run / serve / sandbox / reset / eval / suite)
│   ├── config.py              all settings (from .env)
│   ├── core/                  the "brain" - phases of a run
│   │   ├── agent.py           ★ orchestrator: sanity → understand → execute → verify → report; follow-up turns
│   │   ├── loop.py            ★ act/observe/reflect loop + budgets
│   │   ├── task_spec.py       UNDERSTAND: request → TaskSpec
│   │   ├── verifier.py        VERIFY: rule check + read-only auditor
│   │   ├── report.py          REPORT: report.json / report.html
│   │   ├── report_template.html
│   │   └── prompts.py         every prompt in one place
│   ├── llm/                   model providers behind one interface
│   │   ├── base.py            LLMClient / Conversation / ToolCall / Usage / prices
│   │   ├── openai_compat.py   ★ Gemini (free, default) / Groq / OpenRouter / Ollama / OpenAI
│   │   ├── anthropic_client.py  Claude (optional, paid)
│   │   └── fake.py            ScriptedLLM for tests
│   ├── tools/                 the agent's capabilities
│   │   ├── base.py            ★ Tool, ToolResult, ErrorKind (+ recovery hints)
│   │   ├── browser.py         Playwright browser tools
│   │   ├── dom_snapshot.js    page → text with [ref:kind "label"] tokens
│   │   ├── files.py           sandboxed workspace file tools (PDF reading)
│   │   ├── http_api.py        REST API tool
│   │   ├── parsing.py         ★ exact parsing: amounts (₹/Rs./Indian grouping), dates (+ambiguity), Net-N, arithmetic
│   │   ├── data_tools.py      convert_amount / convert_date / due_date_from_terms / calculate
│   │   ├── agent_tools.py     remember / recall / update_plan / ask_user / finish / submit_verdict
│   │   └── registry.py        which tools the executor and verifier get
│   ├── runtime/               machinery that runs tools safely
│   │   ├── executor.py        ★ validate → run → classify → retry → redact → log
│   │   ├── loop_detector.py   stuck detection → reflection notes
│   │   ├── breaker.py         circuit breaker per system (host)
│   │   ├── context.py         RunContext (everything a tool needs)
│   │   ├── human.py           human-in-the-loop channels (CLI / web / scripted)
│   │   └── events.py          event bus + trace writer
│   ├── safety/
│   │   ├── policy.py          ★ Guard: allow-list, read-only mode, pre-write review, approval rules
│   │   ├── review.py          ★ pre-write review: provenance check + ambiguity review
│   │   └── secrets.py         credential vault (placeholders + redaction)
│   ├── memory/working_memory.py   facts with provenance + the plan
│   └── server/                web dashboard (FastAPI + WebSocket + static UI)
├── sandbox/                   the simulated company
│   ├── data.py                seed data model + the default "northwind" seed (with the deliberate traps)
│   ├── seed_centralign.py     the 60-case test suite's fixtures (second seed)
│   ├── supplierhub.py / erp.py / mail.py / helpdesk.py   the four apps
│   ├── chaos.py               fault injection: modes + 15 named switches
│   ├── state.py / pdfgen.py / launcher.py / common.py / config.py
│   └── templates/             HTML for the apps
├── config/environment.yaml    what the worker knows about its workplace
├── config/policy.yaml         hard guardrails
├── config/centralign/         the same for the second workplace (+ dashboard samples)
├── evals/                     8-scenario eval (tasks, checks, runner)
├── evals/suite/               the 60-case test suite: cases.yaml, grader.py (verdicts from DB state), run_suite.py
└── tests/                     pytest suite (no API key needed)
```
★ = the files to know best.

---

## 1. `worker/core/agent.py`: the orchestrator

`Agent.__init__` builds the per-run objects: run directory, `EventBus` (+ `TraceWriter`), `RunContext`
(vault, guard, memory, plan), and a `Budget`.

`Agent.run(task)` is the whole lifecycle in one function. Read it top to bottom:
1. `understand(...)` → `TaskSpec`; emit `task_spec` and the initial plan.
2. `spec.out_of_scope` → stop as `blocked` (no actions taken).
3. `_clarify(spec)` → ask blocking questions (only info the systems can't provide).
4. Build the executor conversation (`EXECUTOR_SYSTEM` + all tool specs) and a `ToolLoop` with
   terminal tool `finish`; `loop.start(kickoff)` returns the `finish` call (the *claim*) and its pending tool outcomes.
5. Verification loop: while the claim is `completed`, call `verify(...)`. If it fails and rounds
   remain, `loop.resume(pending, feedback)` continues **the same conversation** with the verifier's findings.
6. `_final_status(claim, verdicts)` → `succeeded | needs_review | blocked | failed`.
7. `except` clauses map `Cancelled`, `BudgetExceeded`, `LLMError` and unexpected errors to statuses. The
   `finally` block always closes the browser. **A report is always written**, even on crashes.

## 2. `worker/core/loop.py`: act → observe → reflect

`ToolLoop._run` is the heart:
```text
while True:
    budget.check()                                  # steps / time / cost / cancelled
    turn = conv.send(tool_outcomes, notes, user_text)
    emit llm_turn (thought summary, chosen tools, usage)
    if no tool calls: nudge (max 3) and continue
    for call in turn.tool_calls:
        if call is the terminal tool: remember it, acknowledge, skip the rest
        result = executor.execute(call)             # policy, retries, redaction inside
        outcomes.append(result rendered for the model)
        note = loop_detector.observe(call, result)  # reflection if stuck
    if terminal: return (terminal call, outcomes)   # outcomes are sent on resume()
    maybe add the 75% budget notice
```
`Budget` holds the counters and raises `BudgetExceeded` / `Cancelled`.

## 3. `worker/core/task_spec.py` + `prompts.py`
* `TaskSpec` is a pydantic model. `understand()` calls `llm.structured(system, prompt, TaskSpec)`, which
  uses the API's structured-output mode, so the result is always a valid object.
* `prompts.py` holds `UNDERSTAND_SYSTEM`, `EXECUTOR_SYSTEM` (working principles + security rules + tool
  tips), `VERIFIER_SYSTEM`, the kickoff templates and the verification-failure feedback.
  `environment_block()` inserts today's date and `config/environment.yaml`. `fill()` substitutes
  `{name}` placeholders without `str.format`, so the literal `{{secret:...}}` text survives.

## 4. `worker/core/verifier.py`
* `rule_check()` is a deterministic, free check: a data-changing task with zero recorded writes fails.
* `user_decisions()` lists what the user decided, as recorded by the system rather than claimed by the worker:
  approval-gate decisions (`guard.decisions`) and answers to `ask_user` questions (from the event history).
* `verify()` builds the auditor kickoff (task, criteria, claim, facts, actions, user decisions), starts a **new**
  conversation with `verifier_registry()` (no `finish`/`ask_user`/`file_write`/`remember`), sets
  `ctx.read_only = True` (the Guard then blocks all writes except logins), runs a `ToolLoop` until
  `submit_verdict`, and forces `passed=False` if any criterion is unmet.

## 5. `worker/runtime/executor.py`
`ToolExecutor.execute(call)`:
1. Unknown tool → `invalid_input` with the list of valid tools.
2. `validate_args` → required keys, unknown keys, types (with friendly coercions like `"12"` → `12`), enums.
3. Loop: emit `tool_call` → `tool.run()` → `ToolError` becomes a classified failure; other exceptions go through `_classify_exception` (timeouts/connection → transient, else fatal).
4. `res.output = vault.redact(...)`.
5. **Retry rule:** `not ok and kind == TRANSIENT and retry_safe and attempt <= max_retries` →
   backoff `0.5s, 1s, 2s…`. `retry_safe` comes from the result (e.g. a failed form submit sets it to
   `False`) or from `tool.is_idempotent(args)`.
6. Emit `tool_result` (ok, error kind, output, hint, duration, screenshot, side_effect, attempts).

## 6. `worker/tools/base.py`
* `ErrorKind` enum + `DEFAULT_HINTS` (what the model is told to do for each kind of failure).
* `ToolResult.success / failure`, `render_for_model()` → `ERROR [validation]: ...\nHint: ...`.
* `ToolError`: raise inside a tool to return a classified failure.
* `Tool`: `name`, `description`, `input_schema`, `idempotent`, `verifier_allowed`, `is_idempotent(args)`, `run()`.

## 7. `worker/tools/browser.py` + `dom_snapshot.js`
* `BrowserSession`: lazy Playwright start (headless or headed, optional slow-mo and video), one page,
  tracks the **HTTP status of the last main-frame navigation** (to detect 4xx/5xx after clicks), and
  takes the screenshots.
* `_observe()`: snapshot + screenshot → text: `URL / Title / HTTP status / [login-form note] --- page text`.
* `_ACTION_INFO_JS`: before clicking, find out what the element does: a link (href)? A submit button
  (collect the form's method, action and **field values**, with passwords masked)? This is what the Guard evaluates.
* `_perform_page_action()`: do the click/Enter, wait, observe, classify the status. For a failed
  *write* it adds the "check before retrying" hint and `retry_safe=False`. For a successful write it
  calls `ctx.record_action` (the side-effect log used by verification and the report).
* Tools: `BrowserNavigate` (handles "this URL is a download"), `BrowserClick`, `BrowserType` (resolves
  secrets, `submit=true` presses Enter, **not idempotent when submitting**), `BrowserSelect` (label or value, lists options on failure), `BrowserDownload` (uses the browser's cookies via `context.request`, saves to `workspace/downloads/`), etc.
* `dom_snapshot.js`: walks the DOM in reading order, skips hidden elements, renders headings and table
  rows, and replaces interactive elements with `[ref:kind "label" ...]` tokens. Labels come from
  `aria-label`, `<label for>`, placeholder or name. Selects list their options. Password values are masked.

## 8. `worker/tools/files.py`, `http_api.py`, `agent_tools.py`
* `safe_path()` resolves a path inside `workspace/` or raises `permission`. `read_any()` extracts PDF text with `pypdf`.
* `FileWrite` accepts raw `content` or `rows` (→ CSV), asks the Guard (`file_write` is blocked for the verifier), and records the side effect.
* `HttpRequest`: Guard check (`http_read` vs `http_write`, body fields checked by approval rules),
  secret substitution in headers/body, status classification (5xx transient, 401/403 auth, 404,
  409/422 validation), side-effect recording for writes. Only GET/PUT/DELETE are idempotent.
* `Remember` stores a `Fact` and takes an **evidence screenshot** of the page the value came from. `AskUser`
  routes through the human channel. `Finish` / `SubmitVerdict` are schemas that the loop intercepts.

## 9. `worker/safety/policy.py` and `secrets.py`
* `ProposedAction(kind, description, url, method, fields, text, is_login)` with `fingerprint(rule_id)`.
* `Rule.matches()`: numeric threshold on fields whose *name* matches a regex (tolerates `"$18,250.00"`),
  regex on the visible text, or method match.
* `Guard.authorize()`: (1) URL allow-list / blocked paths, (2) read-only mode, (3) approval rules → one
  `HumanRequest(kind="approval", details=all fields)` per action, listing every rule it triggers → approved: remember the fingerprints; rejected or
  timeout: raise `ToolError(REJECTED_BY_USER)`. Every decision is kept in `guard.decisions` for the report.
* `SecretVault.resolve()` / `redact()` (and the `_obj` variants for dicts/lists).

## 10. `worker/llm/openai_compat.py` (default adapter)
* `PRESETS`: per provider, the base URL, API-key variable, default requests/min and tokens/min,
  and capability flags (`reasoning_effort`, `vision`). Gemini's preset points at Google's
  OpenAI-compatible endpoint.
* `RateLimiter`: a sliding 60-second window. `acquire(estimated_tokens)` sleeps until one more request
  fits under both the RPM and TPM limits (and reports the wait through `on_wait`, which the Agent turns
  into a `rate_limit_wait` event). `record()` replaces the estimate with the real token usage.
* `sanitize_schema` / `to_openai_tool`: drop `additionalProperties`, omit `parameters` for no-arg tools,
  and turn free-form objects (HTTP headers, JSON bodies, CSV rows) into JSON-encoded strings. The
  executor's `validate_args` decodes them back (`_decode_json`).
* `OpenAICompatClient.chat()`: rate limit → `chat.completions.create` (the SDK retries 5xx briefly) →
  on a 429, `parse_quota_error` reads which quota was hit: a per-minute limit is waited out for the
  provider's `retryDelay` (up to `rate_retries` times) and the limiter adopts the reported limit; a
  per-day quota raises at once → other errors become clear `LLMError`s (missing key with the sign-up
  URL, rejected request, unreachable server).
* `structured()`: asks for a single JSON object matching the pydantic schema, extracts it
  (`extract_json` tolerates code fences and prose), validates it, and on failure sends the validation error back once for a repair.
* `_CompatConversation.send()`: appends tool results as `role: tool` messages, then notes and user text as one
  user message (images only if the provider has vision). It stores the assistant turn **append-only and
  with provider extras on tool calls kept** (Gemini returns "thought signatures" there, and they must be
  echoed back on the next request), and invents ids when a provider omits them.

## 10b. `worker/llm/anthropic_client.py` (optional, paid)
* `AnthropicClient.create()` wraps `messages.create` and adds the refusal-fallback beta (auto-disabled
  if the API rejects it), mapping SDK errors to `LLMError`. The SDK itself retries 429/5xx (`max_retries=4`).
* `_AnthropicConversation.send()` builds one user message: `tool_result` blocks (text + optional
  screenshot image), then note text blocks, then user text. Calls the API with
  `thinking={"type": "adaptive", "display": "summarized"}`, `output_config={"effort": ...}` and
  `cache_control={"type": "ephemeral"}`. **Appends `resp.content` verbatim**, then extracts text,
  thought summaries and `tool_use` blocks into an `AssistantTurn`.
* `structured()` uses `messages.parse(output_format=Schema)`.

## 11. `worker/server/` (dashboard)
* `create_app()`: `POST /api/runs` starts `Agent.run` in a new thread (`asyncio.run`) with a
  `WebHumanChannel`. `RunHandle.publish` stores events and pushes them to WebSocket subscribers with
  `loop.call_soon_threadsafe`. `/respond` resolves the pending human future. `/cancel` sets the budget's cancel event.
* `static/app.js`: renders events (timeline, plan, memory, changes, live screenshot), shows
  approval/question cards, and shows the final result with the verifier's per-criterion table and a link to `report.html`.

## 12. `sandbox/`
* `data.py`: `build_seed(today)` creates documents, vendors, bills and emails with dates relative to
  today. The module docstring lists every trap.
* `erp.py`: `validate_bill()` is shared by the form and the API (format, duplicates, dates, amount).
  The chaos hooks are in `new_bill_submit`. `_require_login` handles chaos session expiry.
* `chaos.py`: modes and the middleware. `common.py`: sessions, templates, `/__admin` routes.
* `launcher.py`: three uvicorn servers in threads, bound to IPv4 and IPv6 loopback.

---

## Trace one request through the code

`python -m worker run "Find the latest invoice from Acme ... tell me once it is done."`

| # | Where | What happens |
|---|---|---|
| 1 | `cli.cmd_run` | builds `Settings`, makes sure the sandbox runs, `make_llm()`, `Agent(..., CLIHumanChannel, on_event=print_event)` |
| 2 | `Agent.run` → `task_spec.understand` | structured call → `TaskSpec(goal=..., success_criteria=[...], changes_data=True, plan=[...])` |
| 3 | `Agent.run` | creates the executor conversation and `ToolLoop`, sends the kickoff |
| 4 | `ToolLoop._run` → `conv.send` | The model replies with `update_plan` + `browser_navigate(http://localhost:8001/login)` |
| 5 | `ToolExecutor.execute` → `BrowserNavigate.run` | `Guard.authorize(navigate)` → host allowed → `page.goto` → `_observe` → snapshot text with `[1:input "Username"] [2:input type=password ...] [3:button "Sign in"]` |
| 6 | next turn | `browser_type(1, "{{secret:supplierhub.username}}")`, `browser_type(2, "{{secret:supplierhub.password}}", submit=true)` → vault substitutes → Guard sees a *login* form → allowed → redirected to `/invoices` |
| 7 | several turns | the model filters to ACME, notices the sort order, skips the credit note, opens `INV-ACME-1102`, reads total and due date |
| 8 | `Remember.run` | stores facts + evidence screenshot; event `memory_fact` (UI "Working memory") |
| 9 | ERP | navigate `/bills/new` → redirected to login → sign in → form snapshot |
| 10 | `BrowserSelect`/`BrowserType` | vendor "Acme Corp (V-001)", invoice number, dates as `YYYY-MM-DD`, amount `4812.75` |
| 11 | `BrowserClick.run` | `_ACTION_INFO_JS` says: submit, POST `/bills/new`, fields {...} → `Guard.authorize(form_submit)` → `high_value_amount` doesn't match (≤ 10,000) → click → 303 → bill page → `record_action` + evidence screenshot |
| 12 | `finish(...)` | `ToolLoop` returns the claim to `Agent.run` |
| 13 | `verifier.verify` | `rule_check` passes (one write recorded) → fresh auditor conversation, `ctx.read_only=True` → it searches the bill list → `submit_verdict(passed=True, criteria=[...])` |
| 14 | `report.build_report` | writes `report.json` + `report.html`; `run_finished` event; CLI prints the result panel |

If the ERP form had failed (CHAOS=mild), step 11 would return `ERROR [transient] ... HTTP 500` with the
hint *"may or may not have been saved: check the target system BEFORE submitting again"*, with
`retry_safe=False`, so the executor would not retry. The model would then search the bills list, see
nothing, and resubmit.


---

## Additions for the 60-case test suite

### `worker/tools/parsing.py` + `data_tools.py`: exact conversions
* `parse_amount(text) -> Money(amount: Decimal, currency)`: removes the currency token (₹, Rs., INR, $, USD...),
  `/-`, and all grouping commas (so Indian `1,12,100` and western `112,100` both work). Handles negatives and `(150.00)`.
* `parse_date(text, day_first=None)`: ISO, `22 Oct 2026`, `October 22, 2026`, `22/10/2026`, `22-10-2026`. Numeric
  dates with two valid readings raise **`AmbiguousDate`** listing both. `find_dates(text)` / `find_amounts(text)`
  extract every date/number from a block of text (used by the provenance check).
* `due_from_terms(issue, "Net 30")` and `calculate("48,500 + 1,12,100")`. The calculator walks the Python AST and
  allows only numbers and `+ - * / ( )`, so it cannot run code.
* The four tools wrap these. `convert_date` returns an `AMBIGUOUS` validation error with a hint to find evidence
  or ask, instead of guessing. `policy._to_number` now uses `parse_amount` (bug fix: `Rs. 48500` used to be 0.485).

### `worker/safety/review.py`: the pre-write review
* Called from `Guard.authorize` for `form_submit` (not logins) and `http_write`, before the approval rules.
* `_corpus(ctx)` = task text + every tool output of the run + user answers + recorded facts.
* `unsourced_values(fields, corpus)`: for each field value containing a digit: date-like → any reading in
  `find_dates(corpus)`; amount-like → value in `find_amounts(corpus)`; otherwise → substring of the corpus.
  Missing → `ToolError(UNSOURCED)`, and nothing is written.
* `WriteReviewer.review()`: then (if enabled, max 8 reviews / 2 clarifications per run) a structured LLM call
  with `REVIEW_SYSTEM` and the request, payload, facts and the last 8 observations. `WriteReview(ambiguous,
  kind, candidates, question, explanation)`. A yes counts only with ≥ 2 candidates (or a missing value):
  the system asks the user (`HumanRequest`), then raises `NEEDS_INPUT` with the answer so the worker retries.
  Each decision is logged (`write_review` event, `report.write_reviews`).
* `WriteReview.scripted_default()` makes `ScriptedLLM` return "not ambiguous" unless a test scripts a review.

### `worker/runtime/breaker.py` + executor changes
* `CircuitBreaker.record(host, transient_failure)` counts consecutive failed *calls* per host; the 2nd opens the circuit.
  `suspect(host)` turns off automatic retries for a host that just failed. `is_open(host)` makes the executor
  refuse the call with `system_unavailable` (half-open after 5 minutes).
* `executor._target_host(call, ctx)`: the URL argument's host, or the current page's host for browser actions.
* Retry backoff is now 1 s, 2 s ("spaced").

### `worker/core/agent.py`: turns, follow-ups, sanity check
* `Agent(..., keep_open=True)` keeps the browser and executor conversation after `run()`;
  `follow_up(message)` runs another `_turn`; `close()` ends the session.
* `_turn()`: fresh `Budget` per turn; `is_unclear()` sanity check on the first turn; the spec input for a follow-up
  includes the previous turns and the recorded facts; turn 1 uses `EXECUTOR_KICKOFF`, later turns
  `loop.resume(pending, FOLLOWUP_KICKOFF)`. A follow-up that changes nothing and calls no tools is `answered`
  without verification. If a turn dies mid-loop, the conversation is dropped so a follow-up starts cleanly (facts kept).
* `verify(..., actions_from=...)` and `build_report(..., actions_from=..., turns=...)` only consider this turn's writes.
* `finish` gained the `no_action_needed` status. `rule_check` rejects "completed" with no writes and
  "no_action_needed" with writes. `ToolLoop._check_terminal` sends back summaries over 100 words (at most twice).

### `worker/tools/files.py`: scanned and corrupt PDFs
* `_open_pdf` / `read_any` turn any pypdf error into a `validation` error with "do not guess".
* A PDF without a text layer → `_scanned_pdf()` extracts the page image to `runs/<id>/pages/` and returns it as
  `image_path` (the adapters attach it as an image if the model has vision).

### Sandbox: second seed, switches, new features
* `data.SeedSettings`: per-seed behaviour (portal sort, vendor field, labels, currency, vendor creation, policies).
  `build_seed(today, name)` dispatches to `seed_centralign.build_centralign_seed`.
* `state.reset(today, seed_name)` rebuilds the ERP and HelpDesk SQLite DBs and all PDFs. `POST /__admin/reset?seed=`.
* `chaos.Chaos.set_switch / on / fire_once`; `POST /__admin/switch/<name>?on=true`; reset clears all switches.
* `erp.py`: `validate_bill(via_form=...)` (date format switch, reject-one), `submit_bill` (500-once, silent-drop,
  mangle-amount), `update_bill`, `delete_bills`, `create_vendor`, `create_po`, and matching UI + API routes.
* `pdfgen.py`: INR formatting with Indian grouping, `_render_scanned` (a Pillow image in a PDF), hidden white text.
* `helpdesk.py`: tickets list/detail/update.

### `evals/suite/`: the harness
* `cases.yaml`: 46 live cases + offline/aggregate/manual entries; `extra_checks` with `tag` judge other suite
  cases on the same run (e.g. H1 on A1).
* `grader.py`: `snapshot()` reads the DBs; `grade(case, before, after, reports, run_dirs)` builds `Check`s
  (state / damage / behaviour) and `_verdict()` maps them to pass / honest_failure / false_success /
  silent_damage (+ inconsistent, fail_behaviour). It also checks every run for leaked secrets (I4).
* `run_suite.py`: `_prepare` (reset seed, switches) → `_run_live` (normal, follow-ups, kill-and-restart,
  sequence) → grade → `suite-latest.md` with per-case x/N and the criteria scorecard. `_offline` runs the G7
  code audit and the J6 unit tests.

### Fixes from the first live suite run
* `review.key_terms(task)` / `evidence_lines(task, outputs)`: the ambiguity reviewer also sees every line of the
  whole run that mentions the request's key terms (F1: a second matching vendor seen many steps earlier).
* `RunContext.repeated_writes()` + the `REPEATED WRITE` note in `ToolLoop._run`: stop re-submitting data that
  "succeeds" but does not persist (E1).
* `Agent._stopped_summary()`: when a budget stops the run, the summary lists changes made, facts found and what to check.
* `Agent._turn`: a `needs_review` summary is rewritten to "NOT VERIFIED … an independent check found …"; a
  budget stop replaces any earlier claim. `ToolLoop.counts_steps` is False for the verifier (own 25-step cap).
