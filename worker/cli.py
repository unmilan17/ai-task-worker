"""Command-line interface.

    python -m worker run "<task in plain English>"                 # run one task in the terminal
    python -m worker serve                                         # web dashboard (+ sandbox) on :8000
    python -m worker sandbox [--chaos mild]                        # only the simulated company apps
    python -m worker reset                                         # reset sandbox data
    python -m worker eval [--repeat 3] [--only id1,id2]            # run the evaluation suite
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from sandbox import config as sandbox_config

console = Console(highlight=False)


# --------------------------------------------------------------------------- sandbox helpers
def sandbox_running() -> bool:
    try:
        return httpx.get(f"{sandbox_config.ERP_URL}/__admin/health", timeout=1).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_sandbox(chaos: str | None = None, reset: bool = False, seed: str | None = None):
    """Start the sandbox in-process unless it is already running elsewhere; make sure it runs `seed`."""
    servers = None
    if not sandbox_running():
        from sandbox import launcher
        servers = launcher.start(reset=reset, seed=seed)
    else:
        current = httpx.get(f"{sandbox_config.ERP_URL}/__admin/health", timeout=5).json().get("seed")
        if reset or (seed and seed != current):
            httpx.post(f"{sandbox_config.ERP_URL}/__admin/reset", params={"seed": seed} if seed else None, timeout=30)
    if chaos:
        httpx.post(f"{sandbox_config.ERP_URL}/__admin/chaos/{chaos}")
    return servers


# --------------------------------------------------------------------------- live event printer
def print_event(ev) -> None:
    t, d = ev.type, ev.data
    if t == "phase":
        console.rule(f"[bold]{d['phase'].upper()}[/]" + (f" [dim]({d['reason']})[/]" if d.get("reason") else ""))
    elif t == "task_spec":
        s = d["spec"]
        console.print(f"[bold]Goal:[/] {s['goal']}")
        for c in s["success_criteria"]:
            console.print(f"  [green]✓ criterion:[/] {c}")
    elif t == "plan_updated":
        console.print("[bold]Plan:[/] " + " | ".join(f"{p['id']}.{p['title']} [{p['status']}]" for p in d["plan"]))
    elif t == "llm_turn" and d.get("thoughts"):
        console.print(f"[dim italic]💭 {d['thoughts'][:400]}[/]")
    elif t == "tool_call":
        args = json.dumps(d["args"], ensure_ascii=False)
        retry = f" [yellow](attempt {d['attempt']})[/]" if d["attempt"] > 1 else ""
        console.print(f"[cyan]→ {d['name']}[/]({args[:160]}){retry}")
    elif t == "tool_result":
        if d["ok"]:
            console.print(f"  [green]✓[/] [dim]{d['output'].splitlines()[0][:150] if d['output'] else ''}[/]")
        else:
            console.print(f"  [red]✗ {d.get('error_kind')}[/]: {d['output'].splitlines()[0][:200]}")
    elif t == "retry":
        console.print(f"  [yellow]↻ transient error, retrying in {d['delay_s']}s[/]")
    elif t == "reflection":
        console.print(Panel(d["note"], title="reflection", border_style="yellow"))
    elif t == "policy_block":
        console.print(f"  [red]⛔ policy blocked:[/] {d['action']} - {d['reason']}")
    elif t == "memory_fact":
        console.print(f"  [magenta]📌 {d['fact']['key']} = {d['fact']['value']}[/]")
    elif t == "side_effect":
        console.print(f"  [bold blue]✎ {d['description']}[/]")
    elif t == "verification":
        v = d["verdict"]
        console.print(f"[bold]Verification ({v.get('method')}):[/] "
                      + ("[green]PASSED[/]" if v.get("passed") else "[red]FAILED[/]") + f" - {v.get('summary')}")
        for c in v.get("criteria") or []:
            console.print(f"   {'[green]✓' if c.get('met') else '[red]✗'}[/] {c.get('criterion')} [dim]({c.get('evidence')})[/]")
    elif t == "write_review" and d.get("result") != "ok":
        console.print(f"  [yellow]🛡 pre-write review ({d['result']}): {d['detail']}[/]")
    elif t == "circuit_open":
        console.print(f"  [red]⚡ circuit breaker opened for {d['host']} - the system looks down[/]")
    elif t == "turn_started":
        console.rule(f"[bold]FOLLOW-UP (turn {d.get('turn')})[/]")
    elif t == "rate_limit_wait":
        console.print(f"  [dim]⏳ waiting {d['seconds']}s for the free-tier rate limit[/]")
    elif t == "error":
        console.print(f"[red]{d['message']}[/]")


def print_report(report: dict) -> None:
    color = {"succeeded": "green", "answered": "green", "blocked": "yellow", "needs_review": "yellow"}.get(report["status"], "red")
    body = f"[bold {color}]{report['status'].upper()}[/] - {report['status_reason']}\n\n{report['summary']}"
    if report.get("notes_for_user"):
        body += "\n\nNotes:\n" + "\n".join(f"• {n}" for n in report["notes_for_user"])
    console.print(Panel(body, title="Result", border_style=color))
    m = report["metrics"]
    t = Table(show_header=False, box=None)
    t.add_row("Steps", str(m["tool_steps"]), "LLM calls", str(m["llm_calls"]))
    t.add_row("Duration", f"{m['duration_s']} s", "Est. cost", f"${m['cost_usd']}")
    t.add_row("Retries", str(report["reliability"]["auto_retries"]), "Verified", str(report["verified"]))
    console.print(t)


# --------------------------------------------------------------------------- commands
def warn_if_no_key(settings) -> None:
    from worker.llm import key_status
    ok, msg = key_status(settings)
    if not ok:
        console.print(f"[yellow]Warning: {msg} (copy .env.example to .env first). Runs will fail without it.[/]")


def cmd_run(args) -> int:
    from worker.config import get_settings
    from worker.core.agent import Agent
    from worker.llm import make_llm
    from worker.runtime.human import CLIHumanChannel

    settings = get_settings(headless=not args.headed, model=args.model, effort=args.effort,
                            slow_mo_ms=300 if args.headed else None, profile=args.profile)
    warn_if_no_key(settings)
    ensure_sandbox(chaos=args.chaos, reset=args.reset, seed=settings.profile)
    llm = make_llm(settings)
    interactive = sys.stdin.isatty() and not args.no_follow_up
    agent = Agent(llm, settings, CLIHumanChannel(console), on_event=print_event, keep_open=interactive)
    console.print(Panel(args.task, title=f"Task  [dim](run {agent.run_id}, {settings.profile}, "
                                         f"{settings.llm_provider} · {settings.model})[/]"))

    async def session() -> dict:
        report = await agent.run(args.task)
        print_report(report)
        while interactive and not agent.closed:
            msg = (await asyncio.to_thread(input, "\nFollow-up (Enter to finish): ")).strip()
            if not msg:
                break
            report = await agent.follow_up(msg)
            print_report(report)
        await agent.close()
        return report

    report = asyncio.run(session())
    console.print(f"Evidence report: [link]{agent.run_dir / 'report.html'}[/]")
    return 0 if report["status"] in ("succeeded", "answered") else 1


def cmd_serve(args) -> int:
    import uvicorn

    from sandbox.launcher import loopback_sockets
    from worker.server.app import create_app
    from worker.config import get_settings
    settings = get_settings(profile=args.profile)
    warn_if_no_key(settings)
    ensure_sandbox(chaos=args.chaos, seed=settings.profile)
    console.print(f"Dashboard: http://localhost:{args.port}  (profile: {settings.profile})")
    server = uvicorn.Server(uvicorn.Config(create_app(headed=args.headed, profile=args.profile), log_level="warning"))
    server.run(sockets=loopback_sockets(args.port))
    return 0


def cmd_sandbox(args) -> int:
    sys.argv = [sys.argv[0]] + (["--chaos", args.chaos] if args.chaos else []) + (
        ["--seed", args.seed] if args.seed else [])
    from sandbox import launcher
    launcher.main()
    return 0


def cmd_reset(args) -> int:
    if sandbox_running():
        httpx.post(f"{sandbox_config.ERP_URL}/__admin/reset")
    else:
        from sandbox import state
        state.reset()
    console.print("Sandbox data reset.")
    return 0


def cmd_eval(args) -> int:
    from evals.run_evals import main as eval_main
    return eval_main(repeat=args.repeat, only=args.only.split(",") if args.only else None, headed=args.headed)


def cmd_suite(args) -> int:
    from evals.suite.run_suite import main as suite_main
    return suite_main(only=args.only.split(",") if args.only else None, repeat=args.repeat, short=args.short,
                      offline=args.offline, headed=args.headed)


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # Windows consoles default to cp1252
    except AttributeError:
        pass
    p = argparse.ArgumentParser(prog="worker", description="Autonomous AI Task Worker")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run one task in the terminal")
    r.add_argument("task")
    r.add_argument("--headed", action="store_true", help="show the browser window")
    r.add_argument("--chaos", choices=["off", "mild", "hard"])
    r.add_argument("--reset", action="store_true", help="reset sandbox data first")
    r.add_argument("--model")
    r.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"])
    r.add_argument("--profile", choices=["northwind", "centralign"],
                   help="workplace profile + sandbox seed (default: PROFILE env or northwind)")
    r.add_argument("--no-follow-up", action="store_true", help="exit after the first answer")
    r.set_defaults(fn=cmd_run)

    s = sub.add_parser("serve", help="start the web dashboard (and the sandbox)")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--headed", action="store_true")
    s.add_argument("--chaos", choices=["off", "mild", "hard"])
    s.add_argument("--profile", choices=["northwind", "centralign"])
    s.set_defaults(fn=cmd_serve)

    sb = sub.add_parser("sandbox", help="run only the simulated company apps")
    sb.add_argument("--chaos", choices=["off", "mild", "hard"])
    sb.add_argument("--seed", choices=["northwind", "centralign"])
    sb.set_defaults(fn=cmd_sandbox)

    sub.add_parser("reset", help="reset sandbox data").set_defaults(fn=cmd_reset)

    e = sub.add_parser("eval", help="run the evaluation suite")
    e.add_argument("--repeat", type=int, default=1)
    e.add_argument("--only", help="comma-separated task ids")
    e.add_argument("--headed", action="store_true")
    e.set_defaults(fn=cmd_eval)

    st = sub.add_parser("suite", help="run the 60-case test suite (centralign seed)")
    st.add_argument("--only", help="comma-separated case ids, e.g. A1,E1,F4")
    st.add_argument("--repeat", type=int, default=1)
    st.add_argument("--short", action="store_true", help="A1, D4, E1, F4, G4 only")
    st.add_argument("--offline", action="store_true", help="only cases that need no LLM (G7, J6)")
    st.add_argument("--headed", action="store_true")
    st.set_defaults(fn=cmd_suite)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
