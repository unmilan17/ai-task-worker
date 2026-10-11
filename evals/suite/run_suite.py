"""Runs the 60-case test suite (evals/suite/cases.yaml) against the "centralign" seed.

    python -m worker suite --short                  # A1, D4, E1, F4no, G4 (one case per core behaviour)
    python -m worker suite --only A1,E1 --repeat 3  # selected cases, 3 runs each (report x/3)
    python -m worker suite --offline                # only the cases that need no LLM (G7 code audit, J6 unit tests)
    python -m worker suite                          # everything (needs several days of a free daily quota)

Each run: reset the sandbox to the centralign seed -> turn on the case's fault switches -> run the agent with
ONLY the task text -> grade the real database state (grader.py). Results: evals/results/suite-<stamp>.json and
evals/results/suite-latest.md (per-case x/N, verdict counts, scorecard per evaluation criterion).
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

from sandbox import config as sandbox_config
from worker.config import PROJECT_ROOT, get_settings
from worker.core.agent import Agent
from worker.llm import make_llm
from worker.runtime.human import ScriptedHumanChannel

from .grader import Grade, grade, snapshot

HERE = Path(__file__).parent
RESULTS = HERE.parent / "results"
ERP = sandbox_config.ERP_URL


def load() -> dict[str, Any]:
    return yaml.safe_load((HERE / "cases.yaml").read_text(encoding="utf-8"))


def _admin(path: str, **params) -> None:
    httpx.post(f"{ERP}/__admin/{path}", params=params or None, timeout=60).raise_for_status()


def _prepare(case: dict, settings) -> None:
    _admin("chaos/off")
    _admin("reset", seed="centralign")
    for sw in case.get("switches", []):
        _admin(f"switch/{sw}", on="true")
    # Every case starts with an empty workspace: files downloaded by earlier runs (possibly from the other seed)
    # must not be found and trusted by this one.
    for sub in ("outbox", "downloads"):
        folder = settings.workspace_dir / sub
        folder.mkdir(parents=True, exist_ok=True)
        for f in folder.rglob("*"):
            if f.is_file() and f.name != ".gitkeep":
                f.unlink()


def _human(case: dict, defaults: dict) -> ScriptedHumanChannel:
    h = {**defaults.get("human", {}), **(case.get("human") or {})}
    return ScriptedHumanChannel(approve=h.get("approve", True), answers=[tuple(a) for a in h.get("answers", [])],
                                default_answer=h.get("default_answer", ""), approval_answer=h.get("approval_answer"))


async def _run_live(case: dict, defaults: dict, headed: bool) -> tuple[list[dict], list[Path]]:
    settings = get_settings(profile="centralign", headless=not headed)
    kind = case.get("kind", "run")
    human = _human(case, defaults)
    reports, dirs = [], []

    def agent(keep_open: bool = False) -> Agent:
        a = Agent(make_llm(settings), settings, human, keep_open=keep_open)
        dirs.append(a.run_dir)
        return a

    if kind == "followups":
        a = agent(keep_open=True)
        try:
            reports.append(await a.run(case["task"]))
            for sw in case.get("switches_before_followups", []):     # something happens between the turns
                _admin(f"switch/{sw}", on="true")
            for msg in case.get("followups", []):
                reports.append(await a.follow_up(msg))
        finally:
            await a.close()
    elif kind == "sequence":
        for task in case["tasks"]:
            reports.append(await agent().run(task))
    elif kind == "kill_restart":
        reports.append(await _kill_after(agent(), case))
        reports.append(await agent().run(case["task"]))            # "restart the process on the same task"
    else:
        reports.append(await agent().run(case["task"]))
    return reports, dirs


async def _kill_after(a: Agent, case: dict) -> dict:
    """Run the task and 'kill' it (cancel at the next step boundary) once N new bills exist."""
    target = int(case.get("kill_after_bills", 3))
    baseline = len(snapshot()["bills"])

    async def watch() -> None:
        while not a.cancel.is_set():
            if len(snapshot()["bills"]) - baseline >= target:
                a.cancel.set()
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch())
    try:
        return await a.run(case["task"])
    finally:
        watcher.cancel()


def _offline(case: dict) -> dict[str, Any]:
    if case.get("check") == "code_audit":
        pattern = re.compile(r"acme|globex|initech|umbrella|hooli|stark|wayne|soylent|pied piper|INV-[A-Z0-9]|GLX-|UMB-|V-00",
                             re.I)
        hits = []
        for p in (PROJECT_ROOT / "worker").rglob("*"):
            if p.suffix in {".py", ".js", ".html"} and p.is_file():
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    if pattern.search(line):
                        hits.append(f"{p.relative_to(PROJECT_ROOT).as_posix()}:{i}")
        return {"verdict": "pass" if not hits else "fail_behaviour",
                "checks": [{"name": "no fixture names/IDs in agent code or prompts", "passed": not hits,
                            "detail": ", ".join(hits[:10]) or "clean (fixtures appear only in sandbox/, config/, evals/, tests/)"}]}
    if case.get("check") == "unit_tests":
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/test_parsing.py"], cwd=PROJECT_ROOT,
                           capture_output=True, text=True)
        last = (r.stdout.strip().splitlines() or ["?"])[-1]
        return {"verdict": "pass" if r.returncode == 0 else "fail_behaviour",
                "checks": [{"name": "deterministic parsers + retry cap (tests/test_parsing.py)",
                            "passed": r.returncode == 0, "detail": last}]}
    raise ValueError(case)


def run_case(case: dict, defaults: dict, headed: bool) -> dict[str, Any]:
    kind = case.get("kind", "run")
    if kind == "offline":
        return {"id": case["id"], **_offline(case), "duration_s": 0, "steps": 0, "status": "-"}
    settings = get_settings(profile="centralign")
    _prepare(case, settings)
    before = snapshot()
    t0 = time.time()
    reports, dirs = asyncio.run(_run_live(case, defaults, headed))
    _admin("chaos/off")
    after = snapshot()
    g: Grade = grade(case, before, after, reports, dirs)
    final = reports[-1]
    return {"id": case["id"], "suite_id": case.get("suite_id", case["id"]), **g.to_dict(),
            "status": final["status"], "summary": final["summary"], "run_ids": [d.name for d in dirs],
            "steps": sum(r["metrics"]["tool_steps"] for r in reports[-1:]),
            "llm_calls": sum(r["metrics"]["llm_calls"] for r in reports),
            "models": sorted({m for r in reports for m in r["metrics"].get("models_used") or [r["metrics"]["model"]]}),
            "duration_s": round(time.time() - t0, 1)}


def _scorecard(results: list[dict], spec: dict) -> dict[str, tuple[int, int]]:
    by_id: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        sid = r.get("suite_id", r["id"])
        if r["verdict"] == "error":
            continue                      # cut off by the provider: no verdict on the agent
        by_id[sid].append(r["verdict"] == "pass")
        for c in r.get("checks", []):
            if c.get("tag"):
                by_id[c["tag"]].append(bool(c["passed"]))
    live = [r for r in results if "claim_done" in r]
    if live:
        by_id["E5"].append(not [r for r in live if r["verdict"] in ("false_success", "silent_damage")])
    out = {}
    for crit, ids in spec.items():
        vals = [v for i in ids for v in by_id.get(i, [])]
        out[crit] = (sum(vals), len(vals))
    return out


def to_markdown(results: list[dict], cases: dict, model: str) -> str:
    lines = [f"Primary model: `{model}` (fallback chain on quota exhaustion) · profile `centralign` · "
             f"{datetime.now():%Y-%m-%d %H:%M}", "",
             "| Case | Runs passed | Verdicts | Status (last) | Model(s) | Steps | Time (s) | Failed checks (last run) |",
             "|---|---|---|---|---|---|---|---|"]
    grouped: dict[str, list[dict]] = defaultdict(list)
    for r in results:
        grouped[r["id"]].append(r)
    for cid, runs in grouped.items():
        passed = sum(r["verdict"] == "pass" for r in runs)
        verdicts = ", ".join(f"{k}×{v}" for k, v in Counter(r["verdict"] for r in runs).items())
        last = runs[-1]
        failed = "; ".join(f"{c['name']}: {c['detail']}" for c in last.get("checks", [])
                           if not c["passed"] and not c.get("tag"))[:220]
        models = ", ".join(sorted({m for r in runs for m in r.get("models", [])})) or "-"
        lines.append(f"| {cid} | {passed}/{len(runs)} | {verdicts} | {last.get('status')} | {models} | {last.get('steps')} | "
                     f"{last.get('duration_s')} | {failed} |")
    tagged = defaultdict(list)
    for r in results:
        for c in r.get("checks", []):
            if c.get("tag"):
                tagged[c["tag"]].append(c)
    if tagged:
        lines += ["", "Checks judged on other runs:", "", "| Case | Passed | Detail (last) |", "|---|---|---|"]
        for tag, cs in sorted(tagged.items()):
            lines.append(f"| {tag} | {sum(c['passed'] for c in cs)}/{len(cs)} | {cs[-1]['name']}: {cs[-1]['detail'][:120]} |")
    lines += ["", "Scorecard (pass rate per evaluation criterion, over the cases that were run):", "",
              "| Criterion | Passed |", "|---|---|"]
    for crit, (ok, n) in _scorecard(results, cases["scorecard"]).items():
        lines.append(f"| {crit} | {ok}/{n} |" if n else f"| {crit} | not run |")
    bad = sum(r["verdict"] in ("false_success", "silent_damage") for r in results)
    lines += ["", f"**False success + silent damage: {bad}** (target 0) · total runs: {len(results)}"]
    return "\n".join(lines)


def main(only: list[str] | None = None, repeat: int = 1, short: bool = False, offline: bool = False,
         headed: bool = False) -> int:
    from worker.cli import console, ensure_sandbox
    spec = load()
    cases = [c for c in spec["cases"] if c.get("kind") not in ("manual", "aggregate")]
    if short:
        only = spec["short_list"]
    if offline:
        cases = [c for c in cases if c.get("kind") == "offline"]
    if only:
        cases = [c for c in cases if c["id"] in only or c.get("suite_id") in only]
    if any(c.get("kind") != "offline" for c in cases):
        ensure_sandbox(seed="centralign")
    results = []
    for case in cases:
        for i in range(1 if case.get("kind") == "offline" else repeat):
            console.rule(f"[bold]{case['id']}[/] ({i + 1}/{repeat})  {case.get('task', case.get('check', ''))[:80]}")
            r = run_case(case, spec.get("defaults", {}), headed)
            results.append(r)
            color = "green" if r["verdict"] == "pass" else "yellow" if r["verdict"] in ("honest_failure", "inconsistent", "fail_behaviour") else "red"
            console.print(f"[{color}]{r['verdict'].upper()}[/] status={r.get('status')} steps={r.get('steps')}")
            for c in r.get("checks", []):
                console.print(f"   {'✓' if c['passed'] else '✗'} {('[' + c['tag'] + '] ') if c.get('tag') else ''}{c['name']}: {c['detail']}")
    RESULTS.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    (RESULTS / f"suite-{stamp}.json").write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    md = to_markdown(results, spec, get_settings(profile="centralign").model)
    (RESULTS / "suite-latest.md").write_text(md, encoding="utf-8")
    console.print(md)
    return 0 if all(r["verdict"] == "pass" for r in results) else 1
