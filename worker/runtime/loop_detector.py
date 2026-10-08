"""Detects when the agent is stuck and tells it to step back and reflect.

LLM agents have a characteristic failure mode: repeating the same action
and hoping for a different result (clicking a stale ref again, re-submitting
a form that keeps failing). This detector watches the stream of
(action, outcome) pairs and produces a *reflection note* that is attached to
the next message to the model when it sees:

1. the same call with the same outcome N times in a row (a loop), or
2. K consecutive failures of any kind (a strategy that is not working).

The note does not choose the alternative - the model does - but it forces a
deliberate change of strategy instead of a blind retry. If the agent keeps
going in circles, the step budget eventually stops the run.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from dataclasses import dataclass

from ..llm.base import ToolCall
from ..tools.base import ToolResult


@dataclass
class _Entry:
    action: str
    outcome: str
    ok: bool
    summary: str


class LoopDetector:
    def __init__(self, repeat_threshold: int = 3, failure_threshold: int = 3) -> None:
        self.repeat_threshold = repeat_threshold
        self.failure_threshold = failure_threshold
        self.history: deque[_Entry] = deque(maxlen=20)
        self.consecutive_failures = 0
        self.reflections = 0

    @staticmethod
    def _sig(obj) -> str:
        return hashlib.md5(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()

    def observe(self, call: ToolCall, result: ToolResult) -> str | None:
        """Record one step; return a reflection note if the agent looks stuck."""
        if call.name in {"remember", "recall", "update_plan"}:
            return None   # bookkeeping calls never indicate a loop
        entry = _Entry(action=self._sig([call.name, call.args]),
                       outcome=self._sig([result.ok, result.error_kind, result.output[:3000]]),
                       ok=result.ok,
                       summary=f"{call.name}({json.dumps(call.args, default=str)[:120]}) -> "
                               f"{'ok' if result.ok else 'ERROR ' + (result.error_kind.value if result.error_kind else '')}")
        self.history.append(entry)
        self.consecutive_failures = 0 if result.ok else self.consecutive_failures + 1

        recent = list(self.history)[-self.repeat_threshold:]
        if (len(recent) == self.repeat_threshold and len({(e.action, e.outcome) for e in recent}) == 1):
            self.reflections += 1
            return (f"LOOP DETECTED: you have made the same call {self.repeat_threshold} times in a row with the "
                    f"same result ({recent[-1].summary}). Repeating it will not help. Stop and reflect: what do "
                    f"you actually know, what assumption might be wrong, and what DIFFERENT approach could work "
                    f"(fresh snapshot, another page, another system/API, or ask_user if you are truly blocked)?")
        if self.consecutive_failures >= self.failure_threshold and self.consecutive_failures % self.failure_threshold == 0:
            self.reflections += 1
            fails = "\n".join(f"  - {e.summary}" for e in list(self.history)[-self.consecutive_failures:])
            return (f"REFLECT BEFORE CONTINUING: your last {self.consecutive_failures} actions failed:\n{fails}\n"
                    f"Diagnose the root cause before the next action. Consider: Did an earlier action actually "
                    f"succeed (check the current state)? Are you signed in? Is there an alternative route (e.g. a "
                    f"documented API instead of a broken web form)? If nothing works, finish with status 'failed' "
                    f"and explain what happened.")
        return None
