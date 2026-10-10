"""REPORT phase: the run's final, user-facing result plus the evidence behind it.

Two files per run in runs/<run_id>/:
* report.json - machine-readable (used by the dashboard and the eval harness)
* report.html - a self-contained page you can open or send to someone:
  summary, verification table, facts with sources, actions taken, approvals,
  recoveries, screenshots and the session video.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from jinja2 import Template

_TEMPLATE = Template((Path(__file__).parent / "report_template.html").read_text(encoding="utf-8"))


def _rel(path: str | None, base: Path) -> str | None:
    if not path:
        return None
    try:
        return Path(os.path.relpath(path, base)).as_posix()
    except ValueError:
        return path


def build_report(*, run_id: str, run_dir: Path, task: str, status: str, status_reason: str,
                 claim: dict[str, Any] | None, spec: dict[str, Any] | None, verdicts: list[dict[str, Any]],
                 ctx, events: list, llm, started: float, finished: float, model: str,
                 turns: list[dict[str, Any]] | None = None, actions_from: int = 0) -> dict[str, Any]:
    ev_types = [e.type for e in events]
    tool_failures: dict[str, int] = {}
    for e in events:
        if e.type == "tool_result" and not e.data.get("ok"):
            k = e.data.get("error_kind") or "unknown"
            tool_failures[k] = tool_failures.get(k, 0) + 1
    clarifications = []
    pending: dict[str, dict] = {}
    for e in events:
        if e.type == "human_request":
            pending[e.data["request"]["id"]] = e.data["request"]
        elif e.type == "human_response":
            req = pending.get(e.data["request_id"], {})
            clarifications.append({"kind": req.get("kind"), "question": req.get("question"),
                                   "details": req.get("details"), "response": e.data["response"]})
    videos = sorted((run_dir / "video").glob("*.webm")) if (run_dir / "video").exists() else []
    seen, evidence = set(), []
    for item in ctx.evidence:
        if item["path"] not in seen:
            seen.add(item["path"])
            evidence.append({**item, "path": _rel(item["path"], run_dir)})

    report = {
        "run_id": run_id,
        "task": task,
        "status": status,
        "status_reason": status_reason,
        "summary": (claim or {}).get("summary") or status_reason,
        "notes_for_user": (claim or {}).get("notes_for_user") or [],
        "claimed_evidence": (claim or {}).get("evidence") or [],
        "verification": verdicts,
        "verified": bool(verdicts and verdicts[-1].get("passed")),
        "spec": spec,
        "plan": ctx.plan.as_list(),
        "facts": [{**f, "screenshot": _rel(f.get("screenshot"), run_dir)} for f in ctx.memory.as_list()],
        "actions": [{**a, "screenshot": _rel(a.get("screenshot"), run_dir)} for a in ctx.actions[actions_from:]],
        "turns": turns or [],
        "write_reviews": list(getattr(ctx.reviewer, "log", []) or []),
        "human_interactions": clarifications,
        "approvals": ctx.guard.decisions,
        "reliability": {
            "auto_retries": ev_types.count("retry"),
            "tool_failures_by_kind": tool_failures,
            "reflections_triggered": ev_types.count("reflection"),
            "policy_blocks": ev_types.count("policy_block"),
            "verification_rounds": len(verdicts),
        },
        "evidence": evidence,
        "video": _rel(str(videos[-1]), run_dir) if videos else None,
        "metrics": {
            "model": model,
            "tool_steps": ctx.step,
            "llm_calls": llm.usage.calls,
            "input_tokens": llm.usage.input_tokens,
            "output_tokens": llm.usage.output_tokens,
            "cache_read_tokens": llm.usage.cache_read_tokens,
            "cost_usd": round(llm.usage.cost_usd, 4),
            "duration_s": round(finished - started, 1),
        },
    }
    report = ctx.vault.redact_obj(json.loads(json.dumps(report, default=str)))
    (run_dir / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (run_dir / "report.html").write_text(_TEMPLATE.render(r=report), encoding="utf-8")
    return report
