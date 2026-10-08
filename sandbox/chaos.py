"""Fault injection ("chaos") for the sandbox apps.

Real systems fail in boring ways: a request times out, a server returns 503,
a session expires, a form breaks after a deployment. The agent's reliability
features (retries, re-login, alternative routes, verification) are only
believable if we can trigger those failures on demand.

Modes (set with the CHAOS env var, or at runtime via POST /__admin/chaos):

* ``off``  - everything works (default).
* ``mild`` - transient trouble. ~15% of page loads return 503, pages are a bit
             slow, and the *first* "create bill" form submission in each ERP
             session fails with a 500 *without* saving anything. A good agent
             checks whether the bill was created before retrying.
* ``hard`` - persistent trouble. ~30% of page loads return 503, ERP sessions
             expire after a few requests, and the ERP "create bill" web form is
             broken (always 500). The only way to finish the task is to notice
             the outage and switch to the ERP REST API.

The randomness is seeded so that runs are reproducible for a given seed.
"""

from __future__ import annotations

import os
import random
import threading
import asyncio

from fastapi import Request
from fastapi.responses import HTMLResponse

_VALID = ("off", "mild", "hard")


class Chaos:
    def __init__(self, mode: str | None = None, seed: int = 7) -> None:
        self._lock = threading.Lock()
        self.mode = "off"
        self.set_mode(mode or os.getenv("CHAOS", "off"), seed)

    def set_mode(self, mode: str, seed: int = 7) -> None:
        mode = (mode or "off").lower()
        if mode not in _VALID:
            raise ValueError(f"chaos mode must be one of {_VALID}")
        with self._lock:
            self.mode = mode
            self._rng = random.Random(seed)

    # -- knobs ------------------------------------------------------------
    @property
    def error_rate(self) -> float:
        return {"off": 0.0, "mild": 0.15, "hard": 0.30}[self.mode]

    @property
    def latency_s(self) -> float:
        return {"off": 0.0, "mild": 0.4, "hard": 0.8}[self.mode]

    @property
    def erp_form_broken(self) -> bool:
        return self.mode == "hard"

    @property
    def erp_first_submit_fails(self) -> bool:
        return self.mode == "mild"

    @property
    def erp_session_max_requests(self) -> int | None:
        return 12 if self.mode == "hard" else None

    def roll(self) -> bool:
        with self._lock:
            return self._rng.random() < self.error_rate

    # -- middleware -------------------------------------------------------
    def middleware(self, app_name: str):
        """Returns a FastAPI HTTP middleware that injects latency and random 503s
        into ordinary GET page loads (never into admin endpoints or static files)."""

        async def _mw(request: Request, call_next):
            path = request.url.path
            exempt = path.startswith("/__admin") or path.startswith("/static") or path == "/favicon.ico"
            if not exempt and self.mode != "off":
                if self.latency_s:
                    await asyncio.sleep(self.latency_s)
                if request.method == "GET" and self.roll():
                    return HTMLResponse(
                        f"<html><head><title>503 Service Unavailable</title></head><body>"
                        f"<h1>503 Service Unavailable</h1><p>{app_name} is temporarily unable to "
                        f"handle the request. Please try again.</p></body></html>",
                        status_code=503,
                    )
            return await call_next(request)

        return _mw


chaos = Chaos()
