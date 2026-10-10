"""Shared test fixtures.

The sandbox apps are started in-process once per test session on their
normal ports (8001-8003). In-process matters: some tests tune the chaos
injector directly, so please stop `worker serve` / `worker sandbox` before
running the tests.
"""

from __future__ import annotations

import httpx
import pytest

from sandbox import config as sandbox_config
from sandbox import launcher
from sandbox.chaos import chaos


def _running() -> bool:
    try:
        return httpx.get(f"{sandbox_config.ERP_URL}/__admin/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.fixture(scope="session")
def sandbox():
    if _running():
        pytest.exit("The sandbox is already running in another process (worker serve/sandbox). "
                    "Stop it before running the tests.", returncode=2)
    servers = launcher.start(reset=True)
    yield
    launcher.stop(servers)


@pytest.fixture
def fresh_sandbox(sandbox):
    """Reset ERP state and chaos before a test."""
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/chaos/off")
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/reset", params={"seed": "northwind"}, timeout=30)
    yield
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/chaos/off")
    chaos.set_mode("off")
    chaos.clear_switches()


@pytest.fixture
def centralign_sandbox(sandbox):
    """The 60-case test-suite fixtures (second seed), no faults."""
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/chaos/off")
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/reset", params={"seed": "centralign"}, timeout=30)
    yield
    chaos.clear_switches()
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/reset", params={"seed": "northwind"}, timeout=30)


@pytest.fixture
def settings(tmp_path):
    from worker.config import get_settings
    ws = tmp_path / "workspace"
    for d in ("inbox", "outbox", "downloads"):
        (ws / d).mkdir(parents=True)
    return get_settings(runs_dir=tmp_path / "runs", workspace_dir=ws, record_video=False, headless=True,
                        max_steps=40, human_timeout_s=5)
