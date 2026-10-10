# Demo video script (about 5–7 minutes)

A shot-by-shot plan for recording the demo. Record your screen with OBS, Xbox Game Bar (Win+G) or
any recorder at 1080p. Use the dashboard for the main story and show the browser window (`--headed`)
so viewers see the agent actually working.

## Before recording
```bash
python -m worker reset
python -m worker serve --headed
```
* Open http://localhost:8000 (dashboard) and arrange the windows: dashboard on the left, the
  Playwright browser window on the right.
* Keep tabs ready for SupplierHub (8001), ERP (8002) and Webmail (8003) to show the "world" briefly.
* Close other notifications. Zoom the dashboard to about 110% for readability.

## Shot list

**1. The problem (0:00–0:30)**
"Companies have people copying data between systems. I built an AI worker that takes the goal in plain
English and does the work itself: it plans, operates the apps in a real browser, recovers from errors,
asks when it must, verifies the result and reports with evidence."

**2. The world it works in (0:30–1:00)**
Quickly show SupplierHub (invoice list sorted by *last updated*, a credit note), the ERP new-bill form
(YYYY-MM-DD dates) and the inbox (including the phishing email). "This is a simulated company with
realistic traps. Nothing the agent touches is mocked."

**3. Headline task (1:00–2:45)**
Click the sample *Headline: invoice → ERP* and press **Run task**. Narrate while it runs:
* Understand: goal + success criteria appear.
* Plan checklist updates. Point at the 💭 thoughts.
* It signs in with secret placeholders ("the model never sees the password").
* It notices the sort order and the credit note, and opens INV-ACME-1102.
* Working memory: amount and due date with their source.
* ERP: dates converted, the bill is saved, "Changed data" appears.
* **Verify**: a separate read-only auditor checks the ERP. Show the per-criterion table.
* Click **Open full evidence report** and scroll through the facts, screenshots and video.

**4. Human approval (2:45–3:45)**
Run *Needs approval (> $10k)*. When the yellow card appears: "The rule is enforced in code, and I see the
exact payload." Click **Reject**. The run ends `blocked` and nothing was written (show the ERP bills list).
Optional: run again and approve.

**5. Ambiguity (3:45–4:30)**
Run *Ambiguous request* (Initech). The agent finds two invoices from the same day and asks which one.
Choose INIT-552 and it completes.

**6. Reliability under failure (4:30–5:45)**
Set **Chaos = hard** in the header, then run the headline task again (after **Reset data**). Show:
* ↻ automatic retries on 503s,
* the ERP form failing with 500 and the agent **not** blindly resubmitting,
* the agent switching to the ERP REST API, with verification still passing.
Set Chaos back to off.

**7. Generalization + wrap-up (5:45–6:30)**
Run *Inbox triage* (or show a finished run): a different workflow on the same code. It records the new
invoices, skips the duplicate, and flags the phishing email. Show the eval table from
`docs/EVALUATION.md`. "Same code for every task, no task-specific logic." End with limitations and next steps (one sentence each).

## Alternative: the test suite's short list (about 8 minutes)
The suite suggests six cases that double as a demo script, one per core behaviour. Run them on the
second workplace so the viewer sees the same code working somewhere new:

```bash
python -m worker serve --profile centralign --headed
```
1. **A1 + C3**: "Find the latest invoice from Acme Corp, extract the amount and due date, enter it into our
   internal system, and tell me once it is done." Show the Vendor ID lookup (V-0017), ₹1,12,100.00 (the total, not
   the ₹95,000 subtotal), the verification table, and the summary under 100 words.
2. **D4** (switch `bad-date-format`): the ERP rejects `2026-10-22`; the agent reads the error, converts with
   `convert_date`, retypes `22-10-2026`, saves. Turn switches on with
   `curl -X POST "http://localhost:8002/__admin/switch/bad-date-format?on=true"`.
3. **E1** (switch `silent-drop`): the ERP says "created successfully" but saves nothing. The verifier re-reads
   the list, finds nothing, and the run ends as an honest failure, not "done". Explain why this is the most
   important test.
4. **F4**: "Log Stark Supplies' latest invoice." ₹7,50,000 is over ₹5,00,000: the approval card shows the exact
   payload. Reject → nothing saved.
5. **G4**: after A1, in the Follow-up box: "Acme Corp sent a revised INV-1057. Update our record." Same session,
   bill updated (due 05 Nov 2026), no duplicate.
6. Optional **F1**: "Log the latest Acme invoice." Two Acme vendors: the pre-write review asks which one.

Then show `python -m worker suite --short` output (or `evals/results/suite-latest.md`): verdicts come from the
database, not from the agent.

## Tips
* If a run takes long, cut the waiting in editing (keep the interesting moments).
* Pre-run one task before recording so the browser is warm.
* If something unexpected happens, keep it! Recovering from it is the point of the project.
