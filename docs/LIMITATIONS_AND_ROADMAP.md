# Known limitations, assumptions, and what I would build next

## Known limitations

**Scope of the environment**
* The worker operates three purpose-built web apps, a REST API and local files. It is not tested on
  arbitrary public websites (CAPTCHAs, heavy JavaScript apps, iframes, shadow DOM, canvas UIs).
* No desktop-application control. "Computer use" here means a real browser, HTTP and the file system.
* Webmail is read-only (no sending), and new vendors can't be created in the ERP (by design: that is
  procurement's job, so the agent must report instead of inventing a workaround).

**Agent behaviour**
* LLMs are non-deterministic. The same task can take a different path, or occasionally fail, from run to
  run. That is why the eval suite repeats tasks and reports pass rates rather than a single demo.
* The verifier is also an LLM. It runs in a fresh, read-only context, which catches many errors, but it
  can still be fooled the same way as the executor (e.g. both misread the same page). Deterministic
  outcome checks are used only in the eval harness.
* Context growth: the transcript is append-only (required for reasoning continuity and caching). Very
  long tasks (hundreds of steps) would need server-side context editing or compaction (not enabled).
* Tool calls run sequentially within a run, and there is one browser tab per run. No parallel sub-agents.
* The understand step can only ask questions *before* acting if the ambiguity is visible in the request.
  Ambiguity discovered in the data (e.g. two Initech invoices) relies on the model choosing to `ask_user`.

**Reliability**
* No checkpoint/resume: if the process dies mid-run, the run is lost (the trace remains).
* Error classification for browser actions relies on HTTP status codes and Playwright exceptions. A
  web app that returns "200 OK" with an error message in the page is only detected by the model reading the page.
* Budgets are estimates (cost uses list prices and token counts reported by the API; free tiers report $0).
* Free tiers are rate limited per minute and per day. Runs are slower, and a full repeated eval may need
  more than one day of quota. Free-tier model names and limits change, so check the provider console.

**Safety**
* Approval rules match on field *names* (`amount`, `total`) and button text. A system with unusual field
  names needs its rules adjusted in `config/policy.yaml`.
* Secret values shorter than 4 characters are not redacted (to avoid redacting common substrings).
* The dashboard has no authentication. It is meant to run locally.

**Providers**
* Both LLM adapters are verified offline against mock API endpoints (request shape, history handling,
  error mapping). Provider-specific behaviour (e.g. how strictly Gemini validates thought signatures, schema
  quirks) can only be confirmed with live runs. Live eval results are recorded in EVALUATION.md.

## Assumptions

* The user is an employee of the fictional company *Northwind Traders* (Accounts Payable), and the worker
  acts with that employee's access.
* "Latest invoice" means the most recent **issue date** among documents of type *Invoice* (not credit notes,
  and not the "last updated" timestamp).
* Amounts are USD unless the document says otherwise. The ERP total is the invoice's *total due* (incl. tax).
* Bills entered by the worker land in "Pending approval" in the ERP (the company's own approval workflow
  still applies after data entry).
* Company rule: bills above 10,000.00 need explicit approval from the requesting user before entry.
* The sandbox's test credentials are fake values that live in the repository on purpose. A real deployment
  would load secrets from a secret manager.
* One run at a time per dashboard instance (the sandbox is shared state).

## What I would build next (in priority order)

1. **Deterministic verification adapters per system.** Alongside the LLM auditor, small verifiers that
   query the system of record directly (e.g. ERP API: "does a bill with these exact fields exist?").
   Combine them: deterministic where possible, LLM for the fuzzy parts.
2. **Checkpoint and resume.** Persist the run state (transcript, facts, plan, pending approval) so a run
   survives restarts and can wait days for an approval. Also allow resuming a `needs_review` run with feedback.
3. **Learning from runs (procedural memory).** After a verified success, store a short "playbook" note
   per system ("ERP dates must be YYYY-MM-DD; SupplierHub sorts by last-updated") and retrieve it next
   time. This cuts steps and cost without hard-coding procedures.
4. **Context management for long tasks.** Server-side tool-result clearing / compaction, plus
   summarising old page snapshots, so 200-step tasks stay cheap.
5. **Vision fallback.** Automatically attach a screenshot when the DOM snapshot is poor (canvas, images,
   custom widgets), and use the provider's computer-use toolset for non-browser desktop apps.
6. **Parallelism.** Sub-agents for independent sub-tasks (e.g. process 10 emails in parallel, each with its own browser context), with a coordinator merging results.
7. **Richer policies.** Per-system/per-role permissions, spend limits per day, two-person approval,
   approvals via Slack/email, and an audit export.
8. **Production hardening.** Auth on the dashboard, a job queue (one worker process per run), a real
   secret manager, structured metrics (success rate, cost per task, human-intervention rate) and alerting.
9. **Bigger eval suite.** More tasks, adversarial variants (more injection styles, UI changes), and
   tracking pass rate per model/effort setting to choose the cheapest configuration that meets the quality bar.
