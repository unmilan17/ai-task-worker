# Autonomous AI Task Worker

**Give it a goal in plain English. It does the work on a computer, then proves it did.**

> *"Find the latest invoice from Acme, extract the amount and due date, enter it into our ERP, and tell me once it is done."*

The worker turns that sentence into checkable success criteria, signs in to a supplier portal in a
**real browser**, finds the right document (the portal's default sort order and a look-alike credit
note try to mislead it), records the facts it relies on with their sources, enters the bill in the ERP
(converting formats to what the ERP accepts), and recovers from errors along the way. It asks you
before risky actions, has an **independent read-only auditor verify** the result in the live
systems, and returns a summary with evidence: facts with provenance, screenshots and a video.

![Dashboard](docs/img/dashboard.png)
<sub>The dashboard (here replaying a scripted test run): phases, live timeline of thoughts → actions → results, live browser view, plan, working memory, changes made, and the verifier's per-criterion verdict.</sub>

**Demo video:** _add link here after recording ([script](docs/DEMO_SCRIPT.md))_

---

## Contents
1. [What it can do](#1-what-it-can-do-mapped-to-the-brief)
2. [Quick start](#2-quick-start)
3. [Using it](#3-using-it)
4. [How it works (architecture)](#4-how-it-works)
5. [Key technical decisions](#5-key-technical-decisions)
6. [Evaluation & tests](#6-evaluation--tests)
7. [Models, APIs, frameworks and components used](#7-models-apis-frameworks-and-components-used)
8. [Assumptions](#8-assumptions)
9. [Known limitations](#9-known-limitations)
10. [What I would build next](#10-what-i-would-build-next)
11. [Project layout & documentation index](#11-project-layout--documentation-index)

---

## 1. What it can do (mapped to the brief)

| The brief asks for... | How this prototype does it | Where |
|---|---|---|
| Understand the **end goal** | An *Understand* step turns the request into a `TaskSpec`: goal, deliverable, **objectively checkable success criteria**, risks, initial plan, blocking questions, out-of-scope flag. | `worker/core/task_spec.py` |
| Break it into actions | A living plan (`update_plan`) plus a tool-calling loop. The model chooses each next action from the current observation. No scripted steps. | `worker/core/loop.py` |
| Use tools (browser, files, APIs, apps) | Real Chromium via Playwright (navigate/click/type/select/download…), sandboxed workspace files (PDF reading, CSV writing), REST API calls. Three simulated company apps. | `worker/tools/`, `sandbox/` |
| Observe each result | Every page-changing action **returns the new page state** (text snapshot with element refs + HTTP status) and a screenshot. | `worker/tools/browser.py`, `dom_snapshot.js` |
| Decide what's next | The model gets goal + plan + latest observation + classified errors with recovery hints. | `worker/core/prompts.py` |
| Remember what it discovered | `remember` stores facts **with provenance** (source, step, URL, evidence screenshot). They feed verification and the report. | `worker/memory/working_memory.py` |
| Detect failures | Every tool result is classified (`transient`, `not_found`, `auth`, `validation`, `permission`, `rejected_by_user`…), including HTTP 4xx/5xx after form submissions. | `worker/tools/base.py` |
| Retry / try alternatives | Auto-retry **only transient errors on idempotent actions** (never re-submit a form blindly). Loop and failure-streak detection forces reflection. Alternative routes (e.g. ERP REST API when the web form is broken). Hard budgets. | `worker/runtime/executor.py`, `loop_detector.py` |
| **Verify** the outcome | Deterministic rule check + a **fresh, read-only auditor agent** that checks each success criterion in the live systems. Failures go back to the executor to fix. | `worker/core/verifier.py` |
| Ask for clarification / approval | `ask_user` for genuine ambiguity. **Code-enforced approval rules** (e.g. amount > 10,000) pause the run and show the exact payload. Fails closed on timeout. | `worker/safety/policy.py`, `runtime/human.py` |
| Concise summary + evidence | `report.html` / `report.json`: status, verification table, facts with sources, changes made, approvals, reliability stats, screenshots, session video, full JSONL trace. | `worker/core/report.py` |

Plus: credentials never reach the model (secret placeholders + redaction), prompt-injection resistance,
a live web dashboard and a CLI, fault injection (`CHAOS=mild|hard`), an eval harness, and 42 automated tests.
It runs on **free-tier LLM APIs** (Google Gemini by default), with built-in rate limiting for free quotas.

## 2. Quick start

**Requirements:** Python 3.11+ (built on 3.12), Windows/macOS/Linux, and a **free** Google Gemini API key
(get one in a minute at https://aistudio.google.com/apikey, no credit card). Other free options: see [LLM providers](#llm-providers-free-tiers).

```bash
git clone <this repo> ai-task-worker
cd ai-task-worker
python -m venv .venv
.venv\Scripts\activate             # Windows  (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
python -m playwright install chromium
copy .env.example .env             # macOS/Linux: cp .env.example .env
# then edit .env and set GEMINI_API_KEY=<your free key>
```

Start everything (simulated company apps + dashboard):
```bash
python -m worker serve             # add --headed to watch the browser work
```
Open **http://localhost:8000**, click a sample task, press **Run task**.

The simulated company runs on http://localhost:8001 (SupplierHub), http://localhost:8002 (ERP, API docs
at `/api/docs`) and http://localhost:8003 (Webmail). Test credentials are in `sandbox/test_credentials.json`
if you want to click around yourself.

## 3. Using it

### Dashboard
`python -m worker serve [--headed] [--chaos mild|hard]`, then go to http://localhost:8000.
* **Sample tasks**: headline task, approval (> $10k), ambiguous request, inbox triage, read-only report.
* **Chaos** dropdown injects failures into the apps. **Reset data** restores the seed state.
* Approval/question cards appear when the worker needs you. **Open full evidence report** for the audit view.

### Command line
```bash
python -m worker run "Find the latest invoice from Acme, extract the amount and due date, enter it into our ERP, and tell me once it is done."
python -m worker run "Record Globex's most recent invoice in the ERP."        # asks for approval in the terminal
python -m worker run "Put the latest Initech invoice into the ERP." --headed   # asks which invoice
python -m worker run "<task>" --chaos hard --reset                            # with failures, fresh data
python -m worker sandbox [--chaos mild]     # only the company apps
python -m worker reset                      # reset sandbox data
python -m worker eval [--repeat 3]          # evaluation suite
pytest -q                                   # automated tests (no API key needed)
```
Every run writes `runs/<run_id>/` with `report.html`, `report.json`, `trace.jsonl`, `screens/` and `video/`.

### Configuration
* `.env`: provider and model (`LLM_PROVIDER`, `LLM_MODEL`; default Gemini free tier), `LLM_EFFORT`, rate limits, budgets, headless/video.
* `config/environment.yaml`: what the worker knows about its workplace (systems, URLs, sign-in placeholders, company rules).
* `config/policy.yaml`: hard guardrails (allowed hosts, approval rules and thresholds).

### LLM providers (free tiers)
All providers except Anthropic go through one OpenAI-compatible adapter (`worker/llm/openai_compat.py`).
Switch with two lines in `.env`:

| `LLM_PROVIDER` | Key variable (where to get it) | Default model | Notes |
|---|---|---|---|
| `gemini` **(default, recommended)** | `GEMINI_API_KEY` ([AI Studio](https://aistudio.google.com/apikey), free, no card) | `gemini-3.8-flash` | Strong tool calling, long context, vision. Free quota is per model; if one is used up, set `LLM_MODEL=gemini-3.5-flash-lite`. Free-tier prompts may be used by Google to improve products (fine here: all data is fake). |
| `groq` | `GROQ_API_KEY` ([console](https://console.groq.com/keys), free) | `openai/gpt-oss-120b` | Very fast, but small tokens-per-minute limits on the free tier, so long agent runs are throttled. |
| `openrouter` | `OPENROUTER_API_KEY` ([openrouter.ai](https://openrouter.ai/keys)) | set `LLM_MODEL` to a `:free` model | Low daily request caps on free accounts. |
| `ollama` | none | `qwen3:14b` | Fully local and unlimited, but needs a capable GPU. Small models are weaker at long multi-step tool use. |
| `openai` / `anthropic` | `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | `gpt-5-mini` / `claude-opus-5-5` | Paid. |

Free tiers limit requests and tokens per minute. The adapter spaces requests to stay under them
(`LLM_RPM` / `LLM_TPM` override the presets), shows "waiting for rate limit" in the UI, and retries 429s
with backoff. Model names and quotas change over time, so check your provider's console.

## 4. How it works

```
 request ─▶ UNDERSTAND ─▶ (CLARIFY) ─▶ EXECUTE ───────────────────────▶ VERIFY ─▶ REPORT
            TaskSpec:       only if     act → observe → reflect loop     fresh,      status +
            goal, success   info is     model picks tools; executor      read-only   evidence
            criteria, plan  missing     validates, guards, retries,      auditor
                                        redacts; detector catches loops    │ fail → feedback to executor (≤2×)
```

```mermaid
flowchart LR
    U[User] --> A[Agent] --> L[Loop] --> X[Executor] --> G[Guard] --> T[Tools] --> S[(Browser / API / Files)]
    G -. approval .-> U
    A --> V[Verifier read-only] --> S
    A --> R[Report + evidence]
    LLM[(LLM: Gemini / Groq / Ollama / ...)] <--> A
    LLM <--> L
    LLM <--> V
```

* **The agent** (`worker/core/agent.py`) runs the phases. Read it top to bottom. It is the whole lifecycle in one function.
* **The loop** sends observations to the model, runs its tool calls through the **executor**
  (validate → guard → run → classify → retry if safe → redact → log), and adds reflection notes when stuck.
* **Tools** are uniform (`Tool` → `ToolResult` with error kind + hint). The browser sees pages as compact
  text with element refs (`[12:button "Save bill"]`) and acts by ref.
* **The Guard** enforces `policy.yaml` *in code*: host allow-list, read-only verifier, approval rules on the
  real payload. **The vault** keeps credentials out of the model.
* **The verifier** is a separate conversation with read-only tools that checks every success criterion
  against the live systems.
* **Everything is an event** → `trace.jsonl`, the CLI and the WebSocket dashboard.

Full details: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** (component diagram, state machine, end-to-end
sequence diagram) and **[docs/CODE_WALKTHROUGH.md](docs/CODE_WALKTHROUGH.md)** (file by file, plus one request traced through the code).

### The simulated company (why it's not "mocked")
Three real web apps with real state. The traps are deliberate. They test understanding rather than clicking:

| Trap | Where |
|---|---|
| Portal sorts by *last updated*: the first Acme row is an old, recently-paid invoice | SupplierHub |
| The newest Acme document is a **credit note**, not an invoice | SupplierHub |
| Supplier names differ between systems ("ACME Corporation" vs "Acme Corp") | Portal vs ERP |
| Human dates ("November 1, 2026") vs ERP's strict `YYYY-MM-DD`, and `$4,812.75` vs `4812.75` | Portal vs ERP |
| Duplicate-invoice detection, server-side validation, session expiry | ERP |
| Two Initech invoices issued the same day → "latest" is ambiguous | SupplierHub |
| An 18,250 USD invoice above the 10,000 approval threshold | SupplierHub |
| A phishing email instructing "AI agents" to create a vendor and a bill | Webmail |
| `CHAOS=mild`: random 503s, slow pages, first form submit fails *without saving*. `CHAOS=hard`: ERP form broken (API still works), sessions expire | all |

![SupplierHub](docs/img/supplierhub.png)

## 5. Key technical decisions

Summary below. Each one is explained with the alternatives in **[docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md)**.

| Decision | Why |
|---|---|
| Purpose-built simulated company, not public websites or mocked tools | Real state and failures, deterministic ground truth for evals, traps designed in |
| Hand-written ~200-line orchestrator, no agent framework | Full control between "model chose" and "tool runs" (policy, safe retries, redaction); easy to explain and debug |
| DOM → text with element refs; screenshots for evidence/vision fallback | Exact values, ~10× cheaper than vision, unambiguous actions |
| Actions return the new state | Observation is structural, not optional; fewer model turns |
| Error taxonomy + **idempotency-aware retries** | Retrying a form submit can create duplicates; retrying a validation error is pointless |
| Loop detector → reflection notes; hard budgets | Cheap fix for the most common agent failure (repetition); bounded cost |
| Understand step fixes success criteria **before** acting | "Done" is defined before the agent can rationalise; enables independent verification |
| Fresh, read-only LLM auditor + deterministic rule check | Self-checks inherit the executor's mistakes; read-only can't cause harm |
| Guardrails & approvals in code, on the real payload | Prompts are advice, code is enforcement; robust to prompt injection |
| Secret placeholders + redaction | The model can't leak what it never sees |
| One OpenAI-compatible adapter + presets (Gemini default), client-side rate limiter | Runs on free tiers; switching provider is one line in `.env` |
| Append-only transcript that preserves provider extras (e.g. Gemini thought signatures) | Keeps the model's reasoning valid across steps; required for multi-turn tool calling on reasoning models |
| Provider-agnostic LLM interface + `ScriptedLLM` | Vendor independence; deterministic, free tests of the real loop |
| Environment knowledge as data, **zero task-specific code** | New tasks need no code; new systems need config or one Tool class |

## 6. Evaluation & tests

* **42 automated tests** (`pytest -q`, no API key): the real loop, browser, tools, guardrails and sandbox
  driven by a scripted LLM (approval rejection respected, failed submits never auto-retried, fake "done"
  claims rejected, secrets never in traces...), unit tests for the safety/runtime pieces, sandbox
  behaviour, and offline contract tests of both LLM adapters (mock API endpoints) and the free-tier rate limiter.
* **8-scenario eval suite** (`python -m worker eval --repeat 3`) with the live model and
  **deterministic outcome checks on the real ERP database / files**: headline task, mild chaos, hard
  chaos (API fallback), approval rejected, approval granted, ambiguity, inbox triage with
  duplicate + phishing email, and a read-only CSV report.

Details and results: **[docs/EVALUATION.md](docs/EVALUATION.md)**.

## 7. Models, APIs, frameworks and components used

| Component | Used for |
|---|---|
| **Google Gemini API, free tier** (default), model `gemini-3.8-flash` via its OpenAI-compatible endpoint | reasoning, tool calling, JSON task spec |
| OpenAI Python SDK (`openai`) | client for every OpenAI-compatible provider: Gemini, Groq, OpenRouter, Ollama, OpenAI |
| Optional: Groq / OpenRouter free tiers, local models via Ollama, Anthropic Claude API (paid, `anthropic` SDK) | alternative providers, one line in `.env` |
| **Playwright** (Chromium) | real browser automation, screenshots, video recording |
| **FastAPI** + Uvicorn + WebSockets | dashboard backend; the three simulated company apps |
| Jinja2 | HTML templates for the sandbox apps and the evidence report |
| pydantic | TaskSpec schema / validation |
| httpx | HTTP tool, sandbox admin calls |
| pypdf / fpdf2 | reading invoice PDFs / generating them for the sandbox |
| SQLite | the ERP's database |
| PyYAML, python-dotenv, rich | config, `.env`, terminal UI |
| pytest + pytest-asyncio | tests |

No external services other than the LLM API. No real company systems or credentials. Everything else runs locally.
AI coding assistants were used during development, as the brief permits.

## 8. Assumptions
* The worker acts for an Accounts Payable employee at the fictional *Northwind Traders*, with that employee's access.
* "Latest invoice" = most recent **issue date** among documents of type *Invoice* (credit notes excluded).
* The ERP amount is the invoice's **total due incl. tax**. Currency USD unless stated.
* Bills above **10,000.00** need explicit approval from the requesting user (company rule, enforced in code).
* Bills land in the ERP as "Pending approval". The company's own approval workflow continues after data entry.
* Sandbox credentials are fake test values committed on purpose. Production would use a secret manager.
* One run at a time per dashboard instance (the sandbox is shared state).

## 9. Known limitations
* Operates purpose-built web apps + REST + files, not arbitrary public websites or desktop apps.
* LLMs are stochastic, so measure pass rates over repeated eval runs. The verifier is also an LLM (deterministic checks are used in the evals).
* Append-only context grows on very long tasks (no compaction yet). One browser tab and sequential tool calls.
* No checkpoint/resume if the process dies mid-run. The dashboard has no authentication (local use).
* Approval rules match field names/button text and need tuning for systems with unusual naming.
* Free tiers have per-minute and per-day quotas, so runs are slower and a full eval may need more than one day of quota.
* The LLM adapters are verified offline against mock API endpoints. Live results depend on your provider and model (see [EVALUATION.md](docs/EVALUATION.md)).

More: **[docs/LIMITATIONS_AND_ROADMAP.md](docs/LIMITATIONS_AND_ROADMAP.md)**.

## 10. What I would build next
1. Deterministic per-system verification adapters alongside the LLM auditor.
2. Checkpoint/resume (survive restarts; wait days for an approval).
3. Procedural memory: learn short "playbook" notes per system from verified runs.
4. Context management (tool-result clearing / compaction) for long tasks.
5. Vision fallback and desktop computer-use for non-web apps.
6. Parallel sub-agents (e.g. one per email) with a coordinator.
7. Richer policies (roles, spend limits, two-person approval, Slack approvals) and production hardening.

## 11. Project layout & documentation index

```
worker/            the AI worker (core/ llm/ tools/ runtime/ safety/ memory/ server/ cli.py)
sandbox/           the simulated company (SupplierHub, ERP + API, Webmail, chaos, seed data)
config/            environment.yaml (workplace knowledge), policy.yaml (guardrails)
evals/             eval scenarios, deterministic checks, runner
tests/             pytest suite
docs/              documentation (below)
runs/              per-run evidence (created at runtime)
workspace/         the worker's file area (inbox/, downloads/, outbox/)
```

| Document | Read it for |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | components, run state machine, end-to-end sequence diagram |
| [docs/DESIGN_DECISIONS.md](docs/DESIGN_DECISIONS.md) | every important decision, alternatives, and why |
| [docs/CODE_WALKTHROUGH.md](docs/CODE_WALKTHROUGH.md) | file-by-file tour + one request traced through the code |
| [docs/CONCEPTS.md](docs/CONCEPTS.md) | plain-language primer: agents, tool calling, idempotency, prompt injection... |
| [docs/INTERVIEW_GUIDE.md](docs/INTERVIEW_GUIDE.md) | pitch, likely questions with answers, live debugging, modification exercises |
| [docs/EVALUATION.md](docs/EVALUATION.md) | test suite, eval scenarios, results |
| [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md) | shot list for the demo video |
| [docs/LIMITATIONS_AND_ROADMAP.md](docs/LIMITATIONS_AND_ROADMAP.md) | limitations, assumptions, next steps |
