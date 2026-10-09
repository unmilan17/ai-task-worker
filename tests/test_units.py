"""Fast unit tests for the safety and runtime building blocks (no browser, no LLM)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from worker.config import PROJECT_ROOT
from worker.llm.base import ToolCall
from worker.runtime.executor import validate_args
from worker.runtime.loop_detector import LoopDetector
from worker.safety.policy import Policy, ProposedAction, Rule
from worker.safety.secrets import SecretVault
from worker.tools.base import ErrorKind, ToolError, ToolResult
from worker.tools.browser import BrowserClick
from worker.tools.files import safe_path

POLICY = Policy.from_file(PROJECT_ROOT / "config" / "policy.yaml")


# --------------------------------------------------------------------------- secrets
def test_vault_resolves_and_redacts():
    v = SecretVault({"erp.password": "hunter2-secret"})
    assert v.resolve("pw={{secret:erp.password}}") == "pw=hunter2-secret"
    assert v.redact("the password is hunter2-secret!") == "the password is {{secret:erp.password}}!"
    assert v.redact_obj({"a": ["hunter2-secret"]}) == {"a": ["{{secret:erp.password}}"]}
    with pytest.raises(KeyError):
        v.resolve("{{secret:nope}}")


# --------------------------------------------------------------------------- policy
def test_url_allow_list_and_blocked_paths():
    assert POLICY.check_url("http://localhost:8002/bills") is None
    assert "not on the allow-list" in POLICY.check_url("https://evil.example.com/")
    assert "blocked" in POLICY.check_url("http://localhost:8002/__admin/reset")
    assert "scheme" in POLICY.check_url("file:///etc/passwd")


def _rule(rule_id: str) -> Rule:
    return next(r for r in POLICY.rules if r.id == rule_id)


@pytest.mark.parametrize("amount, triggers", [("18250.00", True), ("$18,250.00", True), ("10000", False),
                                              ("4812.75", False), (12000, True)])
def test_high_value_rule(amount, triggers):
    a = ProposedAction("http_write", "POST /api/bills", fields={"amount": amount, "invoice_number": "X"})
    assert bool(_rule("high_value_amount").matches(a)) is triggers


def test_high_value_rule_ignores_non_amount_fields():
    a = ProposedAction("form_submit", "submit", fields={"invoice_number": "99999", "po": "123456"})
    assert _rule("high_value_amount").matches(a) is None


def test_destructive_rule_matches_button_text():
    assert _rule("destructive_or_payment_action").matches(ProposedAction("click", "Click 'Delete bill'", text="Delete bill"))
    assert _rule("destructive_or_payment_action").matches(ProposedAction("click", "Click 'Save bill'", text="Save bill")) is None


def test_http_delete_always_needs_approval():
    assert _rule("http_delete").matches(ProposedAction("http_write", "DELETE x", method="DELETE"))
    assert _rule("http_delete").matches(ProposedAction("http_write", "POST x", method="POST")) is None


def test_guard_asks_once_per_action_even_when_several_rules_match():
    from types import SimpleNamespace
    from worker.runtime.human import ScriptedHumanChannel
    from worker.safety.policy import Guard
    human = ScriptedHumanChannel(approve=True)
    ctx = SimpleNamespace(step=1, read_only=False, human=human, settings=SimpleNamespace(human_timeout_s=1),
                          events=SimpleNamespace(emit=lambda *a, **k: None))
    guard = Guard(Policy.from_file(PROJECT_ROOT / "config" / "policy.yaml"))
    delete = ProposedAction("http_write", "DELETE http://localhost:8002/api/bills/B-1", method="DELETE",
                            url="http://localhost:8002/api/bills/B-1")
    asyncio.run(guard.authorize(delete, ctx))
    assert len(human.log) == 1                               # one question, not one per matching rule
    assert {d["rule"] for d in guard.decisions} == {"destructive_or_payment_action", "http_delete"}
    asyncio.run(guard.authorize(delete, ctx))                # same approved action again: no new question
    assert len(human.log) == 1


def test_verifier_sees_what_the_user_decided():
    from types import SimpleNamespace
    from worker.core.verifier import user_decisions
    from worker.runtime.events import EventBus
    bus = EventBus("r")
    bus.emit("human_request", request={"id": "q1", "kind": "clarification", "question": "Which invoice?"})
    bus.emit("human_response", request_id="q1", response={"answer": "INIT-552"})
    guard = SimpleNamespace(decisions=[{"action": "POST /api/bills", "reason": "amount > 10,000", "approved": True}])
    text = user_decisions(SimpleNamespace(guard=guard, events=bus))
    assert "POST /api/bills (amount > 10,000) -> APPROVED by the user" in text
    assert "Which invoice? -> user answered: INIT-552" in text


def test_approval_fingerprint_changes_with_payload():
    a = ProposedAction("http_write", "POST", fields={"amount": "18250"})
    b = ProposedAction("http_write", "POST", fields={"amount": "18251"})
    assert a.fingerprint("r") != b.fingerprint("r")
    assert a.fingerprint("r") == ProposedAction("http_write", "POST", fields={"amount": "18250"}).fingerprint("r")


# --------------------------------------------------------------------------- runtime
def test_validate_args_coerces_and_rejects():
    tool = BrowserClick()
    args, err = validate_args(tool, {"ref": "12"})
    assert err is None and args["ref"] == 12
    _, err = validate_args(tool, {})
    assert "missing" in err
    _, err = validate_args(tool, {"ref": 1, "bogus": 2})
    assert "unknown" in err


def test_loop_detector_flags_repeats_and_failure_streaks():
    d = LoopDetector()
    c = ToolCall("1", "browser_click", {"ref": 3})
    fail = ToolResult.failure(ErrorKind.NOT_FOUND, "no element")
    assert d.observe(c, fail) is None
    assert d.observe(c, fail) is None
    note = d.observe(c, fail)
    assert note and "LOOP DETECTED" in note

    d2 = LoopDetector()
    for i in range(2):
        assert d2.observe(ToolCall(str(i), "browser_click", {"ref": i}), fail) is None
    assert "REFLECT" in d2.observe(ToolCall("9", "browser_click", {"ref": 9}), fail)


def test_loop_detector_ignores_bookkeeping():
    d = LoopDetector()
    ok = ToolResult.success("ok")
    for _ in range(5):
        assert d.observe(ToolCall("1", "remember", {"key": "a"}), ok) is None


def test_workspace_path_guard(tmp_path: Path):
    assert safe_path(tmp_path, "outbox/a.csv") == (tmp_path / "outbox" / "a.csv").resolve()
    for bad in ("../secret.txt", "../../etc/passwd", "outbox/../../x"):
        with pytest.raises(ToolError) as e:
            safe_path(tmp_path, bad)
        assert e.value.kind == ErrorKind.PERMISSION


def test_tool_result_rendering_includes_classification_and_hint():
    r = ToolResult.failure(ErrorKind.VALIDATION, "Amount must be a plain number")
    text = r.render_for_model()
    assert text.startswith("ERROR [validation]") and "Hint:" in text


# --------------------------------------------------------------------------- human channel
def test_scripted_human_answers_by_pattern():
    from worker.runtime.human import HumanRequest, ScriptedHumanChannel
    h = ScriptedHumanChannel(approve=False, answers=[(r"initech", "INIT-552")])
    resp = asyncio.run(h.request(HumanRequest("clarification", "Which Initech invoice?"), 1))
    assert resp.answer == "INIT-552"
    resp = asyncio.run(h.request(HumanRequest("approval", "Create bill?"), 1))
    assert resp.approved is False
