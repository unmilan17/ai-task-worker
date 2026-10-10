"""Circuit breaker: stop hammering a system that is down.

Per-call retries handle blips. But when a whole system is down, an agent can keep calling it from
different angles (reload, another page, the API...) until its budget runs out. The breaker counts
*consecutive failed calls* (each already retried with backoff) per host:

* 1st failed call  -> normal (it already made 1 + MAX_TOOL_RETRIES spaced attempts);
* while a host has a failed call on record, later calls to it get NO automatic retries
  (a single probe), so an outage costs few requests;
* 2nd consecutive failed call -> the circuit OPENS: further calls to that host fail immediately
  with ``system_unavailable`` and a hint to stop and report. It half-opens again after a cooldown.
* Any success resets the host's counter.

Counting whole calls (not single attempts) keeps it from tripping on random 503s in a merely flaky system.
"""

from __future__ import annotations

import time
from urllib.parse import urlparse


class CircuitBreaker:
    def __init__(self, threshold: int = 2, cooldown_s: float = 300.0) -> None:
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.failures: dict[str, int] = {}
        self.opened_at: dict[str, float] = {}

    @staticmethod
    def host_of(url: str | None) -> str | None:
        if not url:
            return None
        netloc = urlparse(url).netloc.lower()
        return netloc or None

    def is_open(self, host: str | None) -> bool:
        if not host or host not in self.opened_at:
            return False
        if time.monotonic() - self.opened_at[host] > self.cooldown_s:
            del self.opened_at[host]          # half-open: allow one probe
            return False
        return True

    def suspect(self, host: str | None) -> bool:
        """A host with a recent failed call: don't spend automatic retries on it."""
        return bool(host) and self.failures.get(host, 0) > 0

    def record(self, host: str | None, transient_failure: bool) -> bool:
        """Record a finished call. Returns True if this call opened the circuit."""
        if not host:
            return False
        if not transient_failure:
            self.failures.pop(host, None)
            self.opened_at.pop(host, None)
            return False
        self.failures[host] = self.failures.get(host, 0) + 1
        if self.failures[host] >= self.threshold and host not in self.opened_at:
            self.opened_at[host] = time.monotonic()
            return True
        return False
