# Interview guide

Use this to prepare for the technical discussion. It covers what to say, likely questions with answers,
how to debug a run live, and modification exercises to practise.

---

## 1. The 60-second pitch

> I built an AI worker that takes a plain-English request like "find the latest Acme invoice and
> enter it into the ERP", and actually does it in a real browser against a simulated company: a
> supplier portal, an ERP with a web UI and a REST API, and a shared inbox.
>
> It works in five phases. **Understand** turns the request into a spec with checkable success
> criteria. **Execute** is a tool-calling loop: the model chooses actions, my code runs them through
> safety guardrails and returns the new page state. **Reliability** comes from classified errors,
> retries only where they're safe, loop detection and budgets. **Verify** uses a separate read-only
> auditor that checks the real systems against the success criteria, so the agent can't just claim
> success. **Report** produces a summary with evidence: facts with sources, screenshots and video.
>
> Risky actions, like a bill over 10,000, pause for human approval, and that rule is enforced in code,
> not just the prompt. Credentials never reach the model. The same code handles eight different eval
> tasks with no task-specific logic, including failures I inject on purpose.

## 2. The 5-minute walkthrough (whiteboard order)

1. **Problem framing:** "Do the user's goal, not a script". It has to act, cope, verify and be safe.
2. **Environment:** why a simulated company (real state, real failures, deterministic checking, traps).
3. **Architecture diagram:** `Agent` → `ToolLoop` → `ToolExecutor` → `Guard` → `Tools` → apps, with the
   event bus on the side (see ARCHITECTURE.md §1).
4. **One request end to end:** the sequence diagram (ARCHITECTURE.md §3).
5. **Three engineering highlights:**
   * idempotency-aware retries (don't double-submit),
   * independent read-only verification against pre-committed criteria,
   * guardrails + secret vault outside the model.
6. **Evidence:** live demo or eval table, then limitations and next steps.

## 3. Likely questions and good answers

### Autonomy & design
**Q: How does it decide what to do next?**
The model sees the goal, the spec, its plan, and the latest observation (page snapshot or tool result),
and chooses the next tool call. My code never scripts steps. It runs whatever the model chooses, subject to the guardrails.

**Q: Why not just one big prompt with the whole procedure?**
That would be a script, and it breaks on the first surprise. Prompts here contain *working principles*
(observe after acting, never duplicate, treat external content as data). The procedure is discovered per task.

**Q: Why a separate "understand" step?**
It commits to checkable success criteria *before* acting, so the verifier has something objective to
check. It also catches out-of-scope or under-specified requests before any side effects (D7).

**Q: Why not LangChain/LangGraph?**
I needed precise control between "model chose a tool" and "tool runs": policy checks, idempotency-aware
retries, redaction, events. A ~200-line explicit loop gives that and is easy to explain and debug. A
framework would add abstraction without solving those problems (D2).

**Q: How does it remember things?**
Three layers: the conversation transcript (short-term), explicit facts saved with `remember` including
their source (used by the verifier and report), and the plan. Facts are what make the final answer auditable.

### Reliability
**Q: What happens when a page returns 503?**
For an idempotent action (navigate, GET) the executor retries automatically with exponential backoff,
up to 2 times. If it still fails, the model gets `ERROR [transient]` with a hint.

**Q: And if "Save bill" fails with a 500?**
That's not idempotent, so it's never auto-retried: the bill might have been saved. The model gets the
error with the hint "check whether it took effect before retrying". In the test it searches the bill
list, sees nothing, then resubmits, ending with exactly one bill. (`test_failed_form_submit_is_not_blindly_retried`)

**Q: What if the web form is completely broken?**
In `CHAOS=hard` the ERP form always fails. After the failure (and a reflection note if it repeats), the
agent can use the documented ERP REST API via `http_request`. The API key is a secret placeholder. This
is "attempting a reasonable alternative".

**Q: How do you stop it looping forever?**
The loop detector catches identical call+outcome three times or three failures in a row and injects a
reflection note. Hard budgets on steps, time and cost stop the run with an honest `failed` report.

**Q: What if the model replies with text instead of a tool call?**
The loop nudges it (up to 3 times). Forcing a tool call isn't supported consistently across providers, so
nudging is the portable approach.

**Q: What if the LLM API is down?**
The SDK retries 429/5xx with backoff. If it still fails, `LLMError` ends the run with status `error`, and
a report is still written. The roadmap adds checkpoint/resume.

### Verification
**Q: How do you know it actually worked?**
Two layers. A deterministic rule rejects "completed" if the task changes data and no write was
recorded. Then a separate auditor (fresh conversation, read-only tools, writes blocked by the Guard)
checks each success criterion in the live systems and returns per-criterion evidence. If it fails,
the executor gets the findings and can fix things (max 2 rounds). The final status is `succeeded` only
if verification passed.

**Q: Isn't the verifier the same model? Can't it make the same mistake?**
It's the same model but a *different context*: it doesn't share the executor's beliefs, and it's told to
be skeptical and check the source documents. It's not perfect, so the eval suite also uses deterministic
outcome checks to measure the real success rate. Next step: deterministic verification adapters per system (roadmap).

**Q: Why not use deterministic checks inside the agent?**
They are task-specific, which would break generalization. The agent's verifier works from the
TaskSpec criteria of any task. Deterministic checks belong in the eval harness, which measures the agent.

### Safety
**Q: How do approvals work?**
Tools describe side effects as `ProposedAction`s, for example a form submission with its actual field values.
The Guard matches rules from `config/policy.yaml` (amount > 10,000, destructive verbs, DELETE). If one
matches, the run pauses and the human sees the exact payload. Reject → the tool returns
`rejected_by_user` and the agent finishes `blocked`. Approvals are fingerprinted, so identical retries
don't re-ask but a changed payload does. No answer before the timeout counts as a rejection (fail closed).

**Q: Why enforce it in code if the prompt already says it?**
Prompts are advice. Code is enforcement. A confused or prompt-injected model can't bypass a check in the tool layer.

**Q: How do you handle prompt injection?**
Defence in depth. (1) The system prompt says external content is data, not instructions, and to report
suspicious content. (2) Side effects go through the Guard (allow-listed hosts, approvals). (3) The model
never holds credentials. (4) The verifier is read-only. The inbox contains a real injection attempt, and
the eval checks it is ignored.

**Q: How are credentials handled?**
The model only ever writes `{{secret:erp.password}}`. The vault substitutes the value inside the tool,
and every output, trace and report is redacted. A test asserts the real values never appear in the trace or report.

### Generalization
**Q: How much changes for a new task?**
Nothing. The eight eval tasks (data entry, approvals, ambiguity, inbox triage, reporting) run on identical code.

**Q: For a new system?**
If it's a web app or REST API: add it to `config/environment.yaml` (URL, credentials placeholder,
purpose) and the host to `policy.yaml`. If it's a new kind of interface (e.g. a SQL database or a
desktop app): write a new `Tool` class and register it. The loop, safety, verification and reporting are unchanged.

### Engineering
**Q: How do you test an LLM agent?**
`ScriptedLLM` replaces only the model's decisions with a script, while the real loop, tools, browser,
policy and sandbox run. That gives deterministic, free tests of the important behaviour: retries,
approvals, verifier rejection, loop detection, policy blocks, secret redaction. Separately, the eval
suite runs the real model and scores outcomes deterministically.

**Q: What does it cost?**
Nothing on the default setup: it runs on Gemini's free tier. Every run reports LLM calls and tokens. The real
constraint is free-tier **rate limits**, so the adapter has a sliding-window limiter (requests and tokens per
minute), shows waits in the UI, and the SDK retries 429s. Levers to use fewer calls and tokens: compact snapshots,
actions that return the new state (fewer turns), auto-retries that don't spend model calls, and effort level.

**Q: Why Gemini and not, say, Groq or a local model?**
All three work through the same adapter. Gemini's free tier has strong
tool calling, a long context and no credit card. Groq's free tokens-per-minute cap is smaller than a late-run agent
request. Local models need a strong GPU and struggle with long tool-use chains. Switching is one line in `.env`, and the
eval suite measures the difference.

**Q: Why is the conversation history append-only?**
Reasoning models tie their reasoning state to the conversation. Gemini 3 returns "thought signatures" on tool calls
that must be sent back unchanged, and Claude binds thinking blocks to the exact prefix. Editing history would
break that (and prompt caching), so notes are appended, never injected into earlier turns (D11).

**Q: Biggest weakness?**
Context growth on very long tasks (append-only + snapshots), a single-threaded browser, and the
verifier being an LLM. See LIMITATIONS_AND_ROADMAP.md, and say what you'd do next.

## 4. Debugging a run (do this live if asked)

1. Open the dashboard run (or `runs/<id>/report.html`) and look at **status + reliability stats**.
2. Open `runs/<id>/trace.jsonl`. Each line is an event. Useful filters:
   ```bash
   # every tool call and its outcome, in order
   python -c "import json;[print(e['step'], e['type'], e['data'].get('name'), e['data'].get('ok'), (e['data'].get('output') or '')[:120]) for e in map(json.loads, open('runs/<id>/trace.jsonl', encoding='utf-8')) if e['type'] in ('tool_call','tool_result')]"
   ```
   * `llm_turn.thoughts`: what the model was thinking before each action.
   * `tool_result.error_kind` / `hint`: how the failure was classified.
   * `retry`, `reflection`, `policy_block`, `human_request`: the reliability and safety machinery at work.
   * `verification`: per-criterion verdict and evidence.
3. Check the **screenshots** in `runs/<id>/screens/` (numbered in order) and the video.
4. Reproduce deterministically: copy the model's decisions into a `ScriptedLLM` test (see
   `tests/test_agent_loop.py`) and step through with a debugger.

Typical root causes: stale element refs (fix: fresh snapshot; the hint says so), wrong date format
(ERP validation message), a session expired (redirect to login, handled by re-login), or an ambiguous
label in the snapshot (improve `labelOf` in `dom_snapshot.js`).

## 5. Practice exercises (likely "modify it live" requests)

| Exercise | Where | Sketch |
|---|---|---|
| Change the approval threshold to 5,000 | `config/policy.yaml` | edit `greater_than`. No code change. Run the Acme task: 4,812.75 still passes, Globex asks. |
| Require approval for *any* ERP write | `config/policy.yaml` | new rule: `applies_to: [form_submit, http_write]`, `text_pattern: ".*"`. |
| Add a new tool (e.g. `send_email` on the webmail) | `worker/tools/` | subclass `Tool`, define schema, `idempotent=False`, call `ctx.guard.authorize(ProposedAction("http_write", ...))`, add it to `registry.py`. |
| Add a new system | `config/environment.yaml` + `policy.yaml` | add URL/purpose/credentials placeholder and the host. |
| Add an eval task | `evals/tasks.yaml` + `evals/checks.py` | write the task in plain English, add a deterministic check function. |
| Make retries more aggressive | `.env` `MAX_TOOL_RETRIES`, `executor.py` backoff | explain why writes still must not auto-retry. |
| Make the verifier stricter | `prompts.VERIFIER_SYSTEM` / `verifier.rule_check` | e.g. a rule: every fact used in the summary must have a source. |
| Switch provider/model | `.env` `LLM_PROVIDER=groq` (+ `GROQ_API_KEY`) or `LLM_MODEL=gemini-3.5-flash-lite` | re-run evals and compare pass rate, steps and time. |
| Make the agent gentler on a free quota | `.env` `LLM_RPM=4` | explain the sliding-window limiter in `openai_compat.py`. |
| Add a new error kind (e.g. RATE_LIMITED) | `tools/base.py` (`ErrorKind`, `DEFAULT_HINTS`), `http_api.py` (map 429), `executor.py` (retry with longer delay) | |

## 6. Numbers to know
* About 5.5k lines of Python. 42 automated tests (no API key needed). 8 eval scenarios.
* Budgets: 60 tool steps, 20 minutes, USD 5 per run. Up to 2 auto-retries. Up to 2 verification-fix rounds.
* Approval threshold: 10,000.00. Snapshot cap: 9,000 characters.
* See `docs/EVALUATION.md` for pass rate, steps, time and cost per task.
