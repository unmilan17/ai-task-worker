"""Guardrails: decide whether a proposed side-effecting action may run.

Tools describe what they are about to do as a ProposedAction (navigate to a
URL, submit a form with these fields, send an HTTP POST with this body,
click a button with this label). ``Guard.authorize`` evaluates it against
config/policy.yaml and returns normally (allowed) or raises a ToolError
(blocked / rejected by the human).

Three outcomes:

* ALLOW            - run it.
* DENY             - never allowed (unknown host, admin path, any write while
                     the read-only verifier is running).
* REQUIRE_APPROVAL - pause, ask the human through the HumanChannel, and run
                     only on an explicit yes. Approvals are remembered by a
                     fingerprint of the action, so a retry of the *same*
                     approved action (e.g. after a transient error) does not
                     ask twice, but any change to the payload asks again.

This is enforced in code, *outside* the LLM: the model is told the rules, but
even a confused or prompt-injected model cannot get past them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import yaml

from ..runtime.human import HumanRequest
from ..tools.base import ErrorKind, ToolError

if TYPE_CHECKING:
    from ..runtime.context import RunContext


@dataclass
class ProposedAction:
    kind: str                        # navigate | form_submit | http_write | http_read | click | file_write
    description: str                 # human-readable summary, shown in approval prompts
    url: str | None = None
    method: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)   # form fields / JSON body
    text: str | None = None          # visible label of the clicked element
    is_login: bool = False           # form contains a password field

    def fingerprint(self, rule_id: str) -> str:
        blob = json.dumps({"r": rule_id, "k": self.kind, "u": self.url, "m": self.method,
                           "f": self.fields, "t": self.text}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]


@dataclass
class Rule:
    id: str
    description: str
    applies_to: list[str]
    field_pattern: str | None = None
    greater_than: float | None = None
    text_pattern: str | None = None
    methods: list[str] | None = None

    def matches(self, a: ProposedAction) -> str | None:
        """Returns a reason string if the rule triggers for this action."""
        if a.kind not in self.applies_to:
            return None
        if self.methods and (a.method or "").upper() not in [m.upper() for m in self.methods]:
            return None
        if self.methods and not (self.field_pattern or self.text_pattern):
            return f"{a.method} request"
        if self.field_pattern and self.greater_than is not None:
            for k, v in _flatten(a.fields):
                if re.search(self.field_pattern, k.split(".")[-1]):
                    num = _to_number(v)
                    if num is not None and num > self.greater_than:
                        return f"field '{k}' = {v} exceeds {self.greater_than:,.2f}"
        if self.text_pattern:
            hay = " ".join(filter(None, [a.text or "", a.description]))
            m = re.search(self.text_pattern, hay)
            if m:
                return f"action text contains '{m.group(0)}'"
        return None


def _flatten(d: Any, prefix: str = ""):
    if isinstance(d, dict):
        for k, v in d.items():
            yield from _flatten(v, f"{prefix}{k}.")
    elif isinstance(d, list):
        for i, v in enumerate(d):
            yield from _flatten(v, f"{prefix}{i}.")
    else:
        yield prefix.rstrip("."), d


def _to_number(v: Any) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    if isinstance(v, str):
        cleaned = re.sub(r"[^\d.\-]", "", v)   # tolerate "$18,250.00"
        try:
            return float(cleaned) if cleaned not in {"", ".", "-"} else None
        except ValueError:
            return None
    return None


class Policy:
    def __init__(self, allowed_hosts: list[str], blocked_path_prefixes: list[str], rules: list[Rule]) -> None:
        self.allowed_hosts = {h.lower() for h in allowed_hosts}
        self.blocked_path_prefixes = blocked_path_prefixes
        self.rules = rules

    @classmethod
    def from_file(cls, path: Path) -> "Policy":
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
        rules = [Rule(**r) for r in cfg.get("approval_rules", [])]
        return cls(cfg.get("allowed_hosts", []), cfg.get("blocked_path_prefixes", []), rules)

    def check_url(self, url: str) -> str | None:
        """Returns a denial reason, or None if the URL is allowed."""
        p = urlparse(url)
        if p.scheme not in {"http", "https"}:
            return f"scheme '{p.scheme}' is not allowed"
        if p.netloc.lower() not in self.allowed_hosts:
            return f"host '{p.netloc}' is not on the allow-list ({', '.join(sorted(self.allowed_hosts))})"
        if any(p.path.startswith(prefix) for prefix in self.blocked_path_prefixes):
            return f"path '{p.path}' is blocked"
        return None


class Guard:
    """Applies the Policy for one run and talks to the human when approval is needed."""

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._approved: set[str] = set()
        self.decisions: list[dict[str, Any]] = []   # every approval decision, for the report

    async def authorize(self, action: ProposedAction, ctx: "RunContext") -> None:
        # 1. Hard denials ------------------------------------------------------
        if action.url:
            reason = self.policy.check_url(action.url)
            if reason:
                self._block(ctx, action, reason)
        writes = {"form_submit", "http_write", "file_write"}
        if ctx.read_only and action.kind in writes and not action.is_login:
            self._block(ctx, action, "the verifier runs in read-only mode and may not change anything")

        # 2. Approval rules: one question per action, listing every rule it triggers
        if action.kind == "form_submit" and action.is_login:
            return
        triggered = [(rule, why, action.fingerprint(rule.id)) for rule in self.policy.rules
                     if (why := rule.matches(action))]
        triggered = [t for t in triggered if t[2] not in self._approved]
        if not triggered:
            return
        policy_text = " ".join(rule.description for rule, _, _ in triggered)
        req = HumanRequest(
            kind="approval",
            question=f"{action.description}",
            details={"policy": policy_text, "reason": "; ".join(why for _, why, _ in triggered),
                     **({"target": action.url} if action.url else {}),
                     **{f"field:{k}": v for k, v in _flatten(action.fields) if v not in ("", None)}},
        )
        ctx.events.emit("human_request", step=ctx.step, request=req.to_dict())
        resp = await ctx.human.request(req, ctx.settings.human_timeout_s)
        ctx.events.emit("human_response", step=ctx.step, request_id=req.id, response=resp.to_dict())
        for rule, why, _ in triggered:
            self.decisions.append({"rule": rule.id, "action": action.description, "reason": why,
                                   "approved": bool(resp.approved), "comment": resp.answer,
                                   "timed_out": resp.timed_out})
        if not resp.approved:
            msg = ("No response from the user within the time limit, so the action was NOT performed."
                   if resp.timed_out else
                   f"The user REJECTED this action ({policy_text}). It was NOT performed."
                   + (f" User comment: {resp.answer}" if resp.answer else ""))
            raise ToolError(ErrorKind.REJECTED_BY_USER, msg)
        self._approved.update(fp for _, _, fp in triggered)

    def _block(self, ctx: "RunContext", action: ProposedAction, reason: str) -> None:
        ctx.events.emit("policy_block", step=ctx.step, action=action.description, reason=reason)
        raise ToolError(ErrorKind.PERMISSION, f"Blocked by policy: {reason}.")
