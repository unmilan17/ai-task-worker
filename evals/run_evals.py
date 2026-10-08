"""Evaluation harness: run every task in evals/tasks.yaml against a freshly reset
sandbox, score the real end state with deterministic checks, and write a
results table.

    python -m worker eval                 # all tasks once
    python -m worker eval --repeat 3      # 3 runs per task (agents are stochastic)
    python -m worker eval --only inbox_triage,initech_ambiguous

Results: evals/results/<timestamp>.json and evals/results/latest.md
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from sandbox import config as sandbox_config
from worker.config import get_settings
from worker.core.agent import Agent
from worker.llm import make_llm
from worker.runtime.human import ScriptedHumanChannel

from .checks import CHECKS

HERE = Path(__file__).parent
RESULTS = HERE / "results"


def load_tasks(only: list[str] | None = None) -> list[dict[str, Any]]:
    tasks = yaml.safe_load((HERE / "tasks.yaml").read_text(encoding="utf-8"))
    return [t for t in tasks if not only or t["id"] in only]


def _admin(path: str) -> None:
    httpx.post(f"{sandbox_config.ERP_URL}/__admin/{path}", timeout=10).raise_for_status()


async def run_one(task: dict[str, Any], headed: bool) -> dict[str, Any]:
    settings = get_settings(headless=not headed)
    _admin("chaos/off")
    _admin("reset")
    outbox = settings.workspace_dir / "outbox"
    if outbox.exists():
        shutil.rmtree(outbox)
    outbox.mkdir(parents=True, exist_ok=True)
    (outbox / ".gitkeep").touch()
    _admin(f"chaos/{task.get('chaos', 'off')}")

    h = task.get("human") or {}
    human = ScriptedHumanChannel(approve=h.get("approve", True), answers=[tuple(a) for a in h.get("answers", [])])
    agent = Agent(make_llm(settings), settings, human)
    t0 = time.time()
    report = await agent.run(task["task"])
    _admin("chaos/off")

    manifest = json.loads(sandbox_config.SEED_MANIFEST_PATH.read_text(encoding="utf-8"))
    checks = []
    for name in task["checks"]:
        passed, detail = CHECKS[name](report, manifest, settings.workspace_dir)
        checks.append({"check": name, "passed": passed, "detail": detail})
    status_ok = report["status"] == task.get("expect_status", "succeeded")
    return {
        "id": task["id"], "title": task.get("title", task["id"]), "run_id": report["run_id"],
        "status": report["status"], "expected_status": task.get("expect_status"), "status_ok": status_ok,
        "checks": checks, "passed": status_ok and all(c["passed"] for c in checks),
        "steps": report["metrics"]["tool_steps"], "cost_usd": report["metrics"]["cost_usd"],
        "duration_s": round(time.time() - t0, 1), "retries": report["reliability"]["auto_retries"],
        "verified": report["verified"],
    }


def to_markdown(results: list[dict[str, Any]], model: str) -> str:
    lines = [f"Model: `{model}` · {datetime.now():%Y-%m-%d %H:%M}", "",
             "| Task | Expected | Got | Outcome checks | Pass | Steps | Retries | Time (s) | Cost ($) |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        checks = "<br>".join(f"{'✅' if c['passed'] else '❌'} {c['check']}" for c in r["checks"])
        lines.append(f"| {r['title']} | {r['expected_status']} | {r['status']} | {checks} | "
                     f"{'✅' if r['passed'] else '❌'} | {r['steps']} | {r['retries']} | {r['duration_s']} | {r['cost_usd']} |")
    n = len(results)
    ok = sum(r["passed"] for r in results)
    lines += ["", f"**{ok}/{n} runs passed** · total cost ${sum(r['cost_usd'] for r in results):.2f}"]
    return "\n".join(lines)


def main(repeat: int = 1, only: list[str] | None = None, headed: bool = False) -> int:
    from worker.cli import console, ensure_sandbox
    ensure_sandbox()
    tasks = load_tasks(only)
    results = []
    for task in tasks:
        for i in range(repeat):
            console.rule(f"[bold]{task['id']}[/] ({i + 1}/{repeat})")
            r = asyncio.run(run_one(task, headed))
            results.append(r)
            mark = "[green]PASS[/]" if r["passed"] else "[red]FAIL[/]"
            console.print(f"{mark} status={r['status']} steps={r['steps']} cost=${r['cost_usd']}")
            for c in r["checks"]:
                console.print(f"   {'✓' if c['passed'] else '✗'} {c['check']}: {c['detail']}")
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    model = get_settings().model
    (RESULTS / f"{stamp}.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    md = to_markdown(results, model)
    (RESULTS / "latest.md").write_text(md, encoding="utf-8")
    console.print(md)
    return 0 if all(r["passed"] for r in results) else 1
