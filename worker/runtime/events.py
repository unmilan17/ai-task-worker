"""A tiny synchronous publish/subscribe event bus.

Every interesting thing the agent does (phase change, LLM thought, tool call,
retry, approval request, verification result...) is published as an Event.
Subscribers decide what to do with it:

* the TraceWriter appends it to runs/<id>/trace.jsonl (audit log + replay),
* the CLI prints it to the terminal,
* the web server forwards it to the dashboard over a WebSocket.

Keeping observability behind one bus means the agent code never needs to know
which interface is watching.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Event:
    type: str
    run_id: str
    data: dict[str, Any] = field(default_factory=dict)
    step: int | None = None
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Subscriber = Callable[[Event], None]


class EventBus:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._subs: list[Subscriber] = []
        self.history: list[Event] = []

    def subscribe(self, fn: Subscriber) -> None:
        self._subs.append(fn)

    def emit(self, type: str, step: int | None = None, **data: Any) -> Event:
        ev = Event(type=type, run_id=self.run_id, data=data, step=step)
        self.history.append(ev)
        for fn in list(self._subs):
            try:
                fn(ev)
            except Exception:  # noqa: BLE001 - an observer must never crash the agent
                pass
        return ev


class TraceWriter:
    """Appends every event as one JSON line - the run's permanent audit trail."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, ev: Event) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), default=str) + "\n")
