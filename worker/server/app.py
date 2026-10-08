"""Web dashboard backend.

* POST /api/runs                 start a task (the agent runs in its own thread + event loop)
* WS   /ws/runs/{id}             live event stream (history is replayed first, then live events)
* POST /api/runs/{id}/respond    answer a pending approval / clarification
* POST /api/runs/{id}/cancel     stop a run
* GET  /api/runs[/{id}]          run history / one run's events + report
* GET  /runs/...                 static evidence (screenshots, report.html, video)

Why a thread per run? The agent is a long-running async program that drives
a browser; isolating it in its own thread and event loop keeps the web
server responsive and avoids event-loop-policy issues with Playwright on
Windows. Events cross the thread boundary through ``loop.call_soon_threadsafe``;
human answers cross back through a thread-safe Future (WebHumanChannel).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sandbox import config as sandbox_config

from ..config import get_settings
from ..core.agent import Agent, new_run_id
from ..llm import make_llm
from ..runtime.events import Event
from ..runtime.human import HumanResponse, WebHumanChannel

STATIC = Path(__file__).parent / "static"

SAMPLE_TASKS = [
    {"label": "Headline: invoice → ERP",
     "task": "Find the latest invoice from Acme, extract the amount and due date, enter it into our ERP, "
             "and tell me once it is done."},
    {"label": "Needs approval (> $10k)",
     "task": "Record Globex's most recent invoice in the ERP."},
    {"label": "Ambiguous request",
     "task": "Put the latest Initech invoice into the ERP."},
    {"label": "Inbox triage",
     "task": "Go through the AP inbox and make sure every supplier invoice we received by email is recorded "
             "in the ERP. Tell me what you did with each email."},
    {"label": "Read-only report",
     "task": "Which bills in the ERP are due in the next 14 days and not yet paid? Save them as a CSV in the "
             "outbox and give me the total."},
]


@dataclass
class RunHandle:
    run_id: str
    task: str
    human: WebHumanChannel
    cancel: threading.Event = field(default_factory=threading.Event)
    events: list[dict[str, Any]] = field(default_factory=list)
    subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = field(default_factory=list)
    status: str = "running"
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, ev: Event) -> None:
        d = ev.to_dict()
        with self.lock:
            self.events.append(d)
            subs = list(self.subscribers)
        if ev.type == "run_finished":
            self.status = ev.data.get("status", "finished")
        for loop, q in subs:
            loop.call_soon_threadsafe(q.put_nowait, d)


class StartRun(BaseModel):
    task: str
    chaos: str | None = None


class Respond(BaseModel):
    request_id: str
    approved: bool | None = None
    answer: str = ""


def create_app(headed: bool = False) -> FastAPI:
    settings = get_settings(headless=not headed, slow_mo_ms=250 if headed else None)
    settings.runs_dir.mkdir(parents=True, exist_ok=True)
    runs: dict[str, RunHandle] = {}
    app = FastAPI(title="AI Task Worker", docs_url=None, redoc_url=None)

    # ---------------------------------------------------------------- runs
    @app.post("/api/runs")
    def start_run(body: StartRun) -> dict[str, Any]:
        if any(h.status == "running" for h in runs.values()):
            raise HTTPException(409, "A run is already in progress. Wait for it to finish or cancel it.")
        if not body.task.strip():
            raise HTTPException(400, "Task is empty.")
        if body.chaos:
            _sandbox_admin(f"chaos/{body.chaos}")
        run_id = new_run_id()
        handle = RunHandle(run_id=run_id, task=body.task, human=WebHumanChannel())
        runs[run_id] = handle

        def worker() -> None:
            try:
                agent = Agent(make_llm(settings), settings, handle.human, run_id=run_id,
                              on_event=handle.publish, cancel=handle.cancel)
                asyncio.run(agent.run(body.task))
            except Exception as e:  # noqa: BLE001 - e.g. missing API key at client construction
                handle.publish(Event("error", run_id, {"message": f"{type(e).__name__}: {e}"}))
                handle.publish(Event("run_finished", run_id, {"status": "error", "reason": str(e),
                                                               "summary": str(e)}))
            finally:
                if handle.status == "running":
                    handle.status = "finished"

        threading.Thread(target=worker, name=f"run-{run_id}", daemon=True).start()
        return {"run_id": run_id}

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        out = []
        for d in sorted(settings.runs_dir.iterdir(), reverse=True) if settings.runs_dir.exists() else []:
            rep = d / "report.json"
            if rep.exists():
                r = json.loads(rep.read_text(encoding="utf-8"))
                out.append({"run_id": r["run_id"], "task": r["task"], "status": r["status"],
                            "verified": r.get("verified"), "cost_usd": r["metrics"]["cost_usd"]})
            elif d.name in runs:
                out.append({"run_id": d.name, "task": runs[d.name].task, "status": runs[d.name].status})
        return out[:50]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run_dir = settings.runs_dir / run_id
        if run_id in runs:
            h = runs[run_id]
            events = list(h.events)
            pending = h.human.pending()
            status = h.status
        elif (run_dir / "trace.jsonl").exists():
            events = [json.loads(line) for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()]
            pending, status = [], "finished"
        else:
            raise HTTPException(404, "Unknown run")
        report = json.loads((run_dir / "report.json").read_text(encoding="utf-8")) \
            if (run_dir / "report.json").exists() else None
        return {"run_id": run_id, "status": status, "events": events, "pending": pending, "report": report}

    @app.post("/api/runs/{run_id}/respond")
    def respond(run_id: str, body: Respond) -> dict[str, Any]:
        h = runs.get(run_id)
        if not h or not h.human.resolve(body.request_id, HumanResponse(approved=body.approved, answer=body.answer)):
            raise HTTPException(404, "No such pending request")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        h = runs.get(run_id)
        if not h:
            raise HTTPException(404, "Unknown run")
        h.cancel.set()
        for p in h.human.pending():       # unblock a run waiting for a human
            h.human.resolve(p["id"], HumanResponse(approved=False, answer="cancelled"))
        return {"ok": True}

    @app.websocket("/ws/runs/{run_id}")
    async def ws_run(ws: WebSocket, run_id: str) -> None:
        await ws.accept()
        h = runs.get(run_id)
        if not h:
            await ws.close(code=4404)
            return
        q: asyncio.Queue = asyncio.Queue()
        with h.lock:
            backlog = list(h.events)
            h.subscribers.append((asyncio.get_running_loop(), q))
        try:
            for d in backlog:
                await ws.send_json(d)
            while True:
                d = await q.get()
                await ws.send_json(d)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            with h.lock:
                h.subscribers = [s for s in h.subscribers if s[1] is not q]

    # ---------------------------------------------------------------- config / sandbox
    @app.get("/api/config")
    def get_config() -> dict[str, Any]:
        return {"model": settings.model, "provider": settings.llm_provider, "samples": SAMPLE_TASKS,
                "api_key_present": bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY")
                                        or os.getenv("ANTHROPIC_AUTH_TOKEN")),
                "systems": {"SupplierHub": sandbox_config.SUPPLIERHUB_URL, "ERP": sandbox_config.ERP_URL,
                            "Webmail": sandbox_config.MAIL_URL},
                "chaos": _sandbox_admin("health", method="GET").get("chaos")}

    @app.post("/api/sandbox/reset")
    def sandbox_reset() -> dict[str, Any]:
        return _sandbox_admin("reset")

    @app.post("/api/sandbox/chaos/{mode}")
    def sandbox_chaos(mode: str) -> dict[str, Any]:
        return _sandbox_admin(f"chaos/{mode}")

    # ---------------------------------------------------------------- static
    app.mount("/runs", StaticFiles(directory=str(settings.runs_dir)), name="runs")
    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


_admin_client = httpx.Client(timeout=5)


def _sandbox_admin(path: str, method: str = "POST") -> dict[str, Any]:
    try:
        r = _admin_client.request(method, f"{sandbox_config.ERP_URL}/__admin/{path}")
        return r.json()
    except httpx.HTTPError as e:
        return {"ok": False, "error": f"sandbox not reachable: {e}"}
