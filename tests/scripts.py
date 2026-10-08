"""Reusable scripted-LLM building blocks for agent-loop tests."""

from __future__ import annotations

from worker.core.task_spec import TaskSpec
from worker.llm.fake import call, find_ref, turn

SH = "http://localhost:8001"
ERP = "http://localhost:8002"
API_KEY_HEADER = {"X-API-Key": "{{secret:erp.api_key}}"}


def spec(changes_data: bool = True, **kw) -> TaskSpec:
    base = dict(
        goal="Record the latest ACME invoice as a bill in the ERP.",
        deliverable="Confirmation with the new bill ID.",
        systems_involved=["SupplierHub", "Northwind ERP"],
        success_criteria=["The ERP has exactly one bill for the latest ACME invoice with the correct amount and due date."],
        changes_data=changes_data,
        constraints_and_risks=["Do not enter credit notes.", "No duplicates."],
        plan=["Find latest ACME invoice", "Extract amount and due date", "Enter bill in ERP", "Confirm"],
        blocking_questions=[],
        out_of_scope=False,
        out_of_scope_reason="",
    )
    base.update(kw)
    return TaskSpec(**base)


def login_steps(url: str, user_secret: str, pass_secret: str):
    """Two script steps: open a login page, then fill both fields and submit."""
    return [
        turn(call("browser_navigate", url=url)),
        lambda st: turn(
            call("browser_type", ref=find_ref(st.last_output, r"input type=text"), text="{{secret:%s}}" % user_secret),
            call("browser_type", ref=find_ref(st.last_output, r"input type=password"),
                 text="{{secret:%s}}" % pass_secret, submit=True),
        ),
    ]


def fill_bill_form(amount: str, due: str, invoice_date: str, invoice_number: str = "INV-ACME-1102",
                   vendor: str = "Acme Corp (V-001)"):
    def step(st):
        snap = st.last_output
        return turn(
            call("browser_select", ref=find_ref(snap, r'select "Vendor"'), option=vendor),
            call("browser_type", ref=find_ref(snap, r'"Supplier invoice number"'), text=invoice_number),
            call("browser_type", ref=find_ref(snap, r'"Invoice date"'), text=invoice_date),
            call("browser_type", ref=find_ref(snap, r'"Due date"'), text=due),
            call("browser_type", ref=find_ref(snap, r'"Total amount"'), text=amount),
        )
    return step


def click_save(st):
    # The previous turn only typed; take a snapshot first to get the button ref.
    return turn(call("browser_snapshot"))


def click_button(label: str):
    return lambda st: turn(call("browser_click", ref=find_ref(st.last_output, r'button "%s"' % label)))


def finish(status: str = "completed", summary: str = "Done."):
    return turn(call("finish", status=status, summary=summary,
                     evidence=[{"claim": "bill created", "proof": "ERP bill page"}]))


def verdict(passed: bool, summary: str = "checked"):
    return turn(call("submit_verdict", passed=passed, summary=summary,
                     criteria=[{"criterion": "bill exists", "met": passed, "evidence": summary}]))
