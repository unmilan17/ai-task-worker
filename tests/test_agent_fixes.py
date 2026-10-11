"""Tests for the reliability/safety additions: circuit breaker, summary contract, unclear input,
follow-up turns, and the no_action_needed status."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta

import pytest

from worker.config import get_settings
from worker.core.agent import Agent, is_unclear
from worker.core.verifier import rule_check
from worker.llm.base import ToolCall
from worker.llm.fake import ScriptedLLM, call, turn
from worker.runtime.context import RunContext
from worker.runtime.events import EventBus
from worker.runtime.executor import ToolExecutor
from worker.runtime.human import ScriptedHumanChannel
from worker.safety.policy import Guard, Policy
from worker.safety.secrets import SecretVault
from worker.tools.base import ErrorKind, Tool, ToolError, ToolResult
from worker.tools.registry import ToolRegistry

from . import scripts as s
from .test_agent_loop import headline_executor_script


# --------------------------------------------------------------------------- circuit breaker
def _ctx(tmp_path, **settings_kw):
    settings = get_settings(runs_dir=tmp_path, **settings_kw)
    return RunContext(run_id="t", run_dir=tmp_path, settings=settings, events=EventBus("t"),
                      human=ScriptedHumanChannel(), vault=SecretVault({}), guard=Guard(Policy([], [], [])))


def test_circuit_breaker_stops_calls_to_a_down_system(tmp_path):
    attempts = {"n": 0}

    class Down(Tool):
        name = "fetch"
        description = "GET a URL"
        input_schema = {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}

        async def run(self, args, ctx) -> ToolResult:
            attempts["n"] += 1
            raise ToolError(ErrorKind.TRANSIENT, "HTTP 503")

    ctx = _ctx(tmp_path, max_tool_retries=2)
    ex = ToolExecutor(ToolRegistry([Down()]), ctx)

    async def scenario():
        import worker.runtime.executor as mod
        orig, mod.asyncio.sleep = mod.asyncio.sleep, (lambda s: orig(0))   # no real waiting in the test
        try:
            r1 = await ex.execute(ToolCall("1", "fetch", {"url": "http://erp.local/x"}))
            r2 = await ex.execute(ToolCall("2", "fetch", {"url": "http://erp.local/y"}))
            r3 = await ex.execute(ToolCall("3", "fetch", {"url": "http://erp.local/z"}))
            other = await ex.execute(ToolCall("4", "fetch", {"url": "http://portal.local/"}))
        finally:
            mod.asyncio.sleep = orig
        return r1, r2, r3, other

    r1, r2, r3, other = asyncio.run(scenario())
    assert r1.error_kind == ErrorKind.TRANSIENT            # first call: 1 attempt + 2 spaced retries
    assert r2.error_kind == ErrorKind.UNAVAILABLE          # second call: single probe, then the circuit opens
    assert r3.error_kind == ErrorKind.UNAVAILABLE          # third call: refused without contacting the system
    assert other.error_kind == ErrorKind.TRANSIENT         # other systems are unaffected
    assert attempts["n"] == 3 + 1 + 0 + 3
    assert any(e.type == "circuit_open" for e in ctx.events.history)


# --------------------------------------------------------------------------- input sanity
@pytest.mark.parametrize("text, unclear", [("", True), ("   ", True), ("asdf", True), ("pay", True),
                                           ("Log Hooli's latest invoice", False), ("INV-1057?", False)])
def test_unclear_input_detection(text, unclear):
    assert is_unclear(text) is unclear


@pytest.mark.asyncio
async def test_gibberish_input_asks_and_does_nothing(fresh_sandbox, settings):
    llm = ScriptedLLM(scripts={}, structured_responses={})
    human = ScriptedHumanChannel(default_answer="")        # the user gives no clearer answer
    report = await Agent(llm, settings, human).run("asdf")
    assert report["status"] == "blocked" and report["metrics"]["tool_steps"] == 0
    assert human.log and human.log[0][0].kind == "clarification"


# --------------------------------------------------------------------------- finish contract
@pytest.mark.asyncio
async def test_overlong_summary_is_sent_back_once(fresh_sandbox, settings):
    long = turn(call("finish", status="failed", summary="word " * 150))
    short = turn(call("finish", status="failed", summary="Could not do it: the ERP was down. Nothing was changed."))
    llm = ScriptedLLM(scripts={"executor": [long, short]}, structured_responses={"TaskSpec": s.spec(changes_data=False)})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Check something")
    assert report["summary"].startswith("Could not do it")
    received = llm.conversations[0].state.received
    assert any("limit is 100" in o.content for r in received for o in r["tool_outcomes"])


def test_rule_check_for_no_action_needed():
    spec = s.spec(changes_data=True)
    assert rule_check(spec, {"status": "no_action_needed"}, []) is None
    assert rule_check(spec, {"status": "no_action_needed"}, [{"step": 1, "description": "POST"}])["passed"] is False
    assert rule_check(spec, {"status": "completed"}, [])["passed"] is False


# --------------------------------------------------------------------------- follow-up turns
@pytest.mark.asyncio
async def test_follow_up_question_is_answered_from_memory(fresh_sandbox, settings):
    due = (date.today() + timedelta(days=24)).isoformat()

    def spec_for(prompt: str):
        return s.spec(changes_data="New message from the user" not in prompt)

    answer = turn(call("finish", status="completed", summary=f"The due date was {due}."))
    llm = ScriptedLLM(
        scripts={"executor": [*headline_executor_script(), answer],
                 "verifier": [turn(call("browser_navigate", url=f"{s.ERP}/bills?q=INV-ACME-1102")), s.verdict(True)]},
        structured_responses={"TaskSpec": spec_for})
    agent = Agent(llm, settings, ScriptedHumanChannel(), keep_open=True)
    try:
        first = await agent.run("Enter the latest Acme invoice into the ERP")
        steps_after_first = first["metrics"]["tool_steps"]
        second = await agent.follow_up("What was the due date again?")
    finally:
        await agent.close()
    assert first["status"] == "succeeded"
    assert second["status"] == "answered" and due in second["summary"]
    assert second["metrics"]["tool_steps"] == steps_after_first        # no new tool calls, nothing redone
    assert [t["turn"] for t in second["turns"]] == [1, 2]
    # the follow-up continued the SAME executor conversation
    assert len([c for c in llm.conversations if c.purpose == "executor"]) == 1


# --------------------------------------------------------------------------- fixes from the live suite run
def test_repeated_write_signature_matches_form_and_api(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.record_action("form", fields={"vendor_id": "V-0017", "invoice_number": "INV-1057", "amount": "112100.00",
                                      "due_date": "2026-10-22", "description": "Pumps 5 units"})
    assert ctx.repeated_writes() == 1
    ctx.record_action("api", request={"vendor_id": "V-0017", "invoice_number": "INV-1057", "amount": 112100,
                                      "due_date": "2026-10-22", "currency": "INR"})
    assert ctx.repeated_writes() == 2                    # same record via another route counts as a repeat
    ctx.record_action("other", request={"vendor_id": "V-0018", "invoice_number": "INV-A-77"})
    assert ctx.repeated_writes() == 1


def test_budget_stop_summary_is_informative(fresh_sandbox, settings):
    settings.max_steps = 2
    nav = turn(call("browser_navigate", url=f"{s.SH}/login"))
    llm = ScriptedLLM(scripts={"executor": [turn(call("remember", key="invoice.total", value="1234.50", source="x")),
                                            nav, nav, nav]},
                      structured_responses={"TaskSpec": s.spec(changes_data=False)})
    report = asyncio.run(Agent(llm, settings, ScriptedHumanChannel()).run("Do a long task"))
    assert report["status"] == "failed"
    assert "step budget" in report["summary"] and "invoice.total = 1234.50" in report["summary"]
    assert "No data was changed" in report["summary"]


def test_reviewer_evidence_includes_early_candidates():
    from worker.safety.review import evidence_lines, key_terms
    assert key_terms("Log the latest Acme invoice.") == ["acme"]
    assert key_terms("can u log acme corp's newest bill") == ["acme", "corp"]
    early = "| V-0017 | Acme Corp |\n| V-0018 | Acme Corporation Pvt Ltd |\n| V-0042 | Wayne Traders |"
    late = ["URL: http://localhost:8002/bills/new"] * 20
    lines = evidence_lines("Log the latest Acme invoice.", [early, *late])
    assert lines == ["| V-0017 | Acme Corp |", "| V-0018 | Acme Corporation Pvt Ltd |"]


def test_unverified_completion_is_never_shown_as_done(fresh_sandbox, settings):
    """The worker keeps claiming 'completed' without writing anything: after the verification rounds the status
    is needs_review and the user-facing summary says NOT VERIFIED, not the worker's claim."""
    settings.max_verify_rounds = 1
    fake_done = turn(call("finish", status="completed", summary="Recorded the invoice as bill B-9999."))
    llm = ScriptedLLM(scripts={"executor": [fake_done, fake_done]},
                      structured_responses={"TaskSpec": s.spec(changes_data=True)})
    report = asyncio.run(Agent(llm, settings, ScriptedHumanChannel()).run("Enter the latest invoice"))
    assert report["status"] == "needs_review"
    assert report["summary"].startswith("NOT VERIFIED.")
    assert "independent check found" in report["summary"]


def test_typed_value_echo_is_not_a_source():
    """D7: the browser's "Typed '2026-10-05'" echo must not count as provenance for that same value."""
    from types import SimpleNamespace

    from worker.safety.review import _corpus, unsourced_values

    def ev(name, output):
        return SimpleNamespace(type="tool_result", data={"name": name, "output": output})

    history = [ev("browser_navigate", "Invoice INV-G-203  Date 2026-09-05  Total USD 2,400.00"),
               ev("browser_type", "Typed '2026-10-05' into element 13.")]
    ctx = SimpleNamespace(task_text="Log Globex Ltd's latest invoice.", events=SimpleNamespace(history=history))
    fields = {"invoice_date": "2026-09-05", "due_date": "2026-10-05", "amount": "2400.00"}
    assert unsourced_values(fields, _corpus(ctx)) == [("due_date", "2026-10-05")]


@pytest.mark.asyncio
async def test_fill_form_fills_and_submits_in_one_call(fresh_sandbox, settings):
    """browser_fill_form: every field plus the submit click in one tool call (data entry in ~2 steps, not ~9)."""
    from .test_agent_loop import DUE, ISSUED, bills_for

    def fill_and_save(st):
        snap = st.last_output
        return turn(call("browser_fill_form", submit_ref=s.find_ref(snap, r'button "Save bill"'), fields=[
            {"ref": s.find_ref(snap, r'select "Vendor"'), "value": "Acme Corp (V-001)"},
            {"ref": s.find_ref(snap, r'"Supplier invoice number"'), "value": "INV-ACME-1102"},
            {"ref": s.find_ref(snap, r'"Invoice date"'), "value": ISSUED},
            {"ref": s.find_ref(snap, r'"Due date"'), "value": DUE},
            {"ref": s.find_ref(snap, r'"Total amount"'), "value": "4812.75"}]))

    script = headline_executor_script()
    i = script.index(s.click_save)
    script[i - 1:i + 2] = [fill_and_save]                     # replace fill (5 calls) + snapshot + click
    llm = ScriptedLLM(scripts={"executor": script,
                               "verifier": [turn(call("browser_navigate", url=f"{s.ERP}/bills?q=INV-ACME-1102")),
                                            s.verdict(True, "Bill found")]},
                      structured_responses={"TaskSpec": s.spec()})
    report = await Agent(llm, settings, ScriptedHumanChannel()).run("Enter the latest ACME invoice into the ERP")
    assert report["status"] == "succeeded", report
    rows = bills_for("INV-ACME-1102")
    assert len(rows) == 1 and rows[0]["amount_cents"] == 481275 and rows[0]["due_date"] == DUE
    assert report["actions"] and report["actions"][0]["fields"]["invoice_number"] == "INV-ACME-1102"


def test_name_collision_check_is_deterministic():
    """F1 on a weaker model: the LLM reviewer missed a second matching vendor. The deterministic check does not."""
    from worker.safety.review import name_collisions, name_phrases

    assert name_phrases("Log Northwind Traders' September invoices.") == ["Northwind Traders"]
    rows = ("| V-0101 | Contoso Ltd | billing@contoso.example |\n"
            "| V-0102 | Contoso Holdings Pvt Ltd | ar@contoso-holdings.example |\n"
            "[7:select \"Vendor\" options: Contoso Ltd (V-0101), Contoso Holdings Pvt Ltd (V-0102), Fabrikam (V-0103)]\n"
            "| V-0103 | Fabrikam | ap@fabrikam.example |")
    both = name_collisions("Log the latest Contoso invoice.", {"vendor_id": "V-0102"}, [rows])
    assert both == ["Contoso Ltd (V-0101)", "Contoso Holdings Pvt Ltd (V-0102)"]
    # whole words only, and a one-line dropdown listing every vendor never counts
    assert name_collisions("Log the latest invoice from Contoso Ltd.", {"vendor_id": "V-0101"}, [rows]) == []
    assert name_collisions("Log Fabrikam's invoice.", {"vendor_id": "V-0103"}, [rows]) == []
    # no entity reference in the write -> nothing to check
    assert name_collisions("Log the latest Contoso invoice.", {"invoice_number": "X-1"}, [rows]) == []


def test_derived_values_and_remembered_facts_need_a_real_source():
    """D7 re-run: blocked once, the model called due_date_from_terms with terms it made up ("Net 30" was only in
    the unreadable PDF) and remembered the result. Neither may count as a source."""
    from types import SimpleNamespace

    from worker.safety.review import _corpus, unsourced_values

    def ev(type_, **data):
        return SimpleNamespace(type=type_, data=data)

    page = ev("tool_result", name="browser_navigate", call_id="1", output="Invoice X-9  Date 2026-09-05  Total 2,400.00")
    calc = [ev("tool_call", call_id="2", name="due_date_from_terms", args={"issue_date": "2026-09-05", "terms": "Net 30"}),
            ev("tool_result", name="due_date_from_terms", call_id="2", output="Due date = 2026-10-05 (Net 30 from 2026-09-05)")]
    fact = ev("memory_fact", fact={"key": "due", "value": "2026-10-05", "source": "computed Net 30"})
    fields = {"due_date": "2026-10-05"}

    def corpus(*events):
        return _corpus(SimpleNamespace(task_text="Log the invoice.", events=SimpleNamespace(history=list(events))))

    assert unsourced_values(fields, corpus(page, *calc, fact)) == [("due_date", "2026-10-05")]
    terms_page = ev("tool_result", name="browser_navigate", call_id="0", output="Payment terms: Net 30")
    assert unsourced_values(fields, corpus(terms_page, page, *calc)) == []           # real terms -> fine
    scan = ev("tool_result", name="file_read", call_id="3", output="(scanned page image attached)", image=True)
    assert unsourced_values(fields, corpus(page, scan, fact)) == []                  # read off an image -> fine


def test_scope_check_blocks_look_alike_records_the_user_did_not_name():
    """Live B1: 'Make sure <exact vendor name> is up to date' -> the worker also wrote a bill for a look-alike
    vendor after a vague 'use your best judgement'. The exact name decides; look-alikes are other records."""
    from worker.safety.review import exactly_named, unnamed_entity

    rows = ("| V-0101 | Contoso Ltd | billing@contoso.example |\n"
            "| V-0102 | Contoso Holdings Pvt Ltd | ar@contoso-holdings.example |\n"
            "| V-0103 | Fabrikam | ap@fabrikam.example |")
    task = "Make sure Contoso Ltd is up to date in the finance system."
    assert unnamed_entity(task, {"vendor_id": "V-0102"}, [rows]) == ("Contoso Holdings Pvt Ltd (V-0102)",
                                                                      ["Contoso Ltd (V-0101)"])
    assert unnamed_entity(task, {"vendor_id": "V-0101"}, [rows]) is None
    assert unnamed_entity("Log the latest Contoso invoice.", {"vendor_id": "V-0102"}, [rows]) is None  # both match
    assert unnamed_entity(task, {"vendor_id": "V-0103"}, [rows]) is None      # not a look-alike: other checks apply
    cands = ["Contoso Ltd (V-0101)", "Contoso Holdings Pvt Ltd (V-0102)"]
    assert exactly_named(task, cands) == "Contoso Ltd (V-0101)"
    assert exactly_named("Log the latest Contoso invoice.", cands) is None


def test_verifier_running_out_of_steps_keeps_the_answer_unverified(fresh_sandbox, settings, monkeypatch):
    """Live B5: the worker answered correctly ("no invoice from that supplier"), then the checker used all its
    steps re-reading pages. The answer must survive, marked NOT VERIFIED - not be replaced by 'step budget'."""
    from worker.core import verifier
    monkeypatch.setattr(verifier, "VERIFIER_MAX_STEPS", 2)
    llm = ScriptedLLM(
        scripts={"executor": [turn(call("finish", status="no_action_needed", summary="There is no invoice from X."))],
                 "verifier": [turn(call("browser_navigate", url=f"{s.ERP}/bills"))] * 5},
        structured_responses={"TaskSpec": s.spec(changes_data=True)})
    report = asyncio.run(Agent(llm, settings, ScriptedHumanChannel()).run("Log X's latest invoice"))
    assert report["status"] == "needs_review"
    assert "There is no invoice from X." in report["summary"] and "could not confirm" in report["summary"]


def test_scope_check_reads_records_from_json_api_responses():
    """Live B1 re-run: the vendor list came from the API as pretty-printed JSON (ID and name on separate lines)."""
    import json as _json

    from worker.safety.review import unnamed_entity

    api = "GET http://erp/api/vendors -> HTTP 200\n" + _json.dumps({"vendors": [
        {"vendor_id": "V-0101", "name": "Contoso Ltd"},
        {"vendor_id": "V-0102", "name": "Contoso Holdings Pvt Ltd"}]}, indent=2)
    task = "Make sure Contoso Ltd is up to date in the finance system."
    assert unnamed_entity(task, {"vendor_id": "V-0102"}, [api]) == ("Contoso Holdings Pvt Ltd (V-0102)",
                                                                     ["Contoso Ltd (V-0101)"])
    assert unnamed_entity(task, {"vendor_id": "V-0101"}, [api]) is None
