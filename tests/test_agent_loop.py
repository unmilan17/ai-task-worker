"""End-to-end agent-loop tests: real sandbox + real browser + real tools/policy,
with the LLM replaced by a deterministic script (no API key needed)."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from sandbox import state
from worker.core.agent import Agent
from worker.llm.fake import ScriptedLLM, call, turn
from worker.runtime.human import ScriptedHumanChannel

from . import scripts as s

TODAY = date.today()
DUE = (TODAY + timedelta(days=24)).isoformat()
ISSUED = (TODAY - timedelta(days=6)).isoformat()


def bills_for(invoice_number: str) -> list[dict]:
    with state.erp_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM bills WHERE invoice_number=?", (invoice_number,))]


def headline_executor_script(extra_before_finish=None):
    return [
        turn(call("update_plan", steps=[{"id": 1, "title": "Find invoice", "status": "in_progress"}]),
             call("browser_navigate", url=f"{s.SH}/login")),
        *s.login_steps(f"{s.SH}/login", "supplierhub.username", "supplierhub.password")[1:],
        turn(call("browser_navigate", url=f"{s.SH}/invoices/INV-ACME-1102")),
        turn(call("remember", key="invoice.total", value="4812.75", source="SupplierHub INV-ACME-1102"),
             call("remember", key="invoice.due_date", value=DUE, source="SupplierHub INV-ACME-1102")),
        *s.login_steps(f"{s.ERP}/bills/new", "erp.username", "erp.password"),
        s.fill_bill_form("4812.75", DUE, ISSUED),
        s.click_save,
        s.click_button("Save bill"),
        *(extra_before_finish or []),
        s.finish(),
    ]


@pytest.mark.asyncio
async def test_headline_task_happy_path(fresh_sandbox, settings):
    llm = ScriptedLLM(
        scripts={"executor": headline_executor_script(),
                 "verifier": [turn(call("browser_navigate", url=f"{s.ERP}/bills?q=INV-ACME-1102")),
                              s.verdict(True, "Bill found with 4,812.75 USD")]},
        structured_responses={"TaskSpec": s.spec()})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Enter the latest ACME invoice into the ERP")

    assert report["status"] == "succeeded", report
    assert report["reliability"]["tool_failures_by_kind"] == {}, "happy path must have no tool errors"
    rows = bills_for("INV-ACME-1102")
    assert len(rows) == 1 and rows[0]["amount_cents"] == 481275 and rows[0]["due_date"] == DUE
    assert report["actions"], "the form submission should be recorded as a side effect"
    assert {f["key"] for f in report["facts"]} == {"invoice.total", "invoice.due_date"}

    # The real credentials must never appear in anything the model or the logs saw.
    trace = (settings.runs_dir / report["run_id"] / "trace.jsonl").read_text(encoding="utf-8")
    for secret in ("sandbox-Sh-7731", "sandbox-Erp-4410"):
        assert secret not in trace
        assert secret not in json.dumps(report)


@pytest.mark.asyncio
async def test_high_value_write_requires_approval_and_rejection_is_respected(fresh_sandbox, settings):
    body = {"vendor_id": "V-002", "invoice_number": "GLX-2231", "invoice_date": ISSUED,
            "due_date": DUE, "amount": "18250.00", "currency": "USD"}
    llm = ScriptedLLM(
        scripts={"executor": [
            turn(call("http_request", method="POST", url=f"{s.ERP}/api/bills", headers=s.API_KEY_HEADER, json_body=body)),
            s.finish("blocked", "The user declined the 18,250.00 USD bill, so nothing was entered."),
        ]},
        structured_responses={"TaskSpec": s.spec()})
    human = ScriptedHumanChannel(approve=False)
    report = await Agent(llm, settings, human).run("Enter Globex invoice GLX-2231 into the ERP")

    assert report["status"] == "blocked"
    assert bills_for("GLX-2231") == []                      # nothing was written
    assert human.log and human.log[0][0].kind == "approval"  # the human was asked
    assert report["approvals"][0]["approved"] is False
    assert report["approvals"][0]["rule"] == "high_value_amount"


@pytest.mark.asyncio
async def test_approved_high_value_write_goes_through(fresh_sandbox, settings):
    body = {"vendor_id": "V-002", "invoice_number": "GLX-2231", "invoice_date": ISSUED,
            "due_date": DUE, "amount": "18250.00", "currency": "USD"}
    llm = ScriptedLLM(
        scripts={"executor": [
            turn(call("http_request", method="POST", url=f"{s.ERP}/api/bills", headers=s.API_KEY_HEADER, json_body=body)),
            s.finish(),
        ], "verifier": [s.verdict(True)]},
        structured_responses={"TaskSpec": s.spec()})
    report = await Agent(llm, settings, ScriptedHumanChannel(approve=True)).run("Enter GLX-2231")
    assert report["status"] == "succeeded"
    assert len(bills_for("GLX-2231")) == 1


@pytest.mark.asyncio
async def test_failed_form_submit_is_not_blindly_retried(fresh_sandbox, settings):
    """CHAOS=mild: the first ERP form submission returns 500 without saving.
    The executor must NOT auto-retry a non-idempotent submit; the (scripted) agent
    checks the bill list, sees nothing was saved, and submits again -> exactly one bill."""
    import httpx
    httpx.post(f"{s.ERP}/__admin/chaos/mild")
    from sandbox.chaos import chaos
    chaos.set_mode("mild", seed=12345)
    chaos._rng.random = lambda: 0.99   # no random 503s in this test - only the deterministic submit failure

    script = headline_executor_script()
    save_idx = len(script) - 2      # index of the "click Save bill" step
    retry_steps = [
        turn(call("browser_navigate", url=f"{s.ERP}/bills?q=INV-ACME-1102")),   # check: was it saved?
        turn(call("browser_navigate", url=f"{s.ERP}/bills/new")),
        s.fill_bill_form("4812.75", DUE, ISSUED),
        s.click_save,
        s.click_button("Save bill"),
    ]
    script = script[:save_idx + 1] + retry_steps + script[save_idx + 1:]
    llm = ScriptedLLM(scripts={"executor": script, "verifier": [s.verdict(True)]},
                      structured_responses={"TaskSpec": s.spec()})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Enter the latest ACME invoice")

    assert len(bills_for("INV-ACME-1102")) == 1
    assert report["reliability"]["tool_failures_by_kind"].get("transient") == 1
    trace = [json.loads(line) for line in
             (settings.runs_dir / report["run_id"] / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
    submit_calls = [e for e in trace if e["type"] == "tool_call" and e["data"]["name"] == "browser_click"]
    assert all(e["data"]["attempt"] == 1 for e in submit_calls), "submits must never be auto-retried"


@pytest.mark.asyncio
async def test_claiming_success_without_doing_anything_fails_verification(fresh_sandbox, settings):
    """The executor claims 'completed' immediately. The deterministic rule check rejects it
    (data-changing task, no write recorded) and the executor is sent back to do the work."""
    llm = ScriptedLLM(
        scripts={"executor": [s.finish(summary="All done (not really)."), *headline_executor_script()],
                 "verifier": [s.verdict(True)]},
        structured_responses={"TaskSpec": s.spec()})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Enter the latest ACME invoice")
    assert report["verification"][0]["method"] == "rule" and not report["verification"][0]["passed"]
    assert report["verification"][-1]["passed"]
    assert report["status"] == "succeeded"
    assert len(bills_for("INV-ACME-1102")) == 1


@pytest.mark.asyncio
async def test_policy_blocks_external_hosts_and_admin_endpoints(fresh_sandbox, settings):
    llm = ScriptedLLM(
        scripts={"executor": [
            turn(call("browser_navigate", url="https://example.com/")),
            turn(call("http_request", method="POST", url=f"{s.ERP}/__admin/reset")),
            s.finish("failed", "Could not do it."),
        ]},
        structured_responses={"TaskSpec": s.spec(changes_data=False)})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Do something forbidden")
    assert report["reliability"]["policy_blocks"] == 2
    assert report["status"] == "failed"


@pytest.mark.asyncio
async def test_loop_detector_triggers_reflection(fresh_sandbox, settings):
    bad = turn(call("browser_click", ref=999))
    llm = ScriptedLLM(scripts={"executor": [turn(call("browser_navigate", url=f"{s.SH}/login")), bad, bad, bad,
                                            s.finish("failed", "stuck")]},
                      structured_responses={"TaskSpec": s.spec(changes_data=False)})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("x")
    assert report["reliability"]["reflections_triggered"] >= 1
    executor_conv = llm.conversations[0]
    assert any("REFLECT" in n or "LOOP DETECTED" in n for r in executor_conv.state.received for n in r["notes"])


@pytest.mark.asyncio
async def test_out_of_scope_request_is_refused_without_acting(fresh_sandbox, settings):
    llm = ScriptedLLM(scripts={}, structured_responses={
        "TaskSpec": s.spec(out_of_scope=True, out_of_scope_reason="Booking flights is not possible with these systems.")})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Book me a flight to Paris")
    assert report["status"] == "blocked"
    assert report["metrics"]["tool_steps"] == 0
