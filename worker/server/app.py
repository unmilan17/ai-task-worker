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
import queue
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from sandbox import config as sandbox_config
from sandbox.chaos import SWITCHES

from ..config import get_settings
from ..core.agent import Agent, new_run_id
from ..llm import key_status, make_llm
from ..runtime.events import Event
from ..runtime.human import HumanResponse, WebHumanChannel

STATIC = Path(__file__).parent / "static"

def load_samples(path: Path) -> list[dict[str, str]]:
    """Dashboard sample tasks come from config (per profile) - never from agent code."""
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or []
    except OSError:
        return []


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
    followups: "queue.Queue[str | None]" = field(default_factory=queue.Queue)
    session_open: bool = True       # the agent keeps its browser/memory for follow-up messages

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


class FollowUp(BaseModel):
    message: str


FOLLOWUP_IDLE_S = 30 * 60      # an idle session (waiting for follow-ups) is closed after 30 minutes


class Respond(BaseModel):
    request_id: str
    approved: bool | None = None
    answer: str = ""


def create_app(headed: bool = False, profile: str | None = None) -> FastAPI:
    settings = get_settings(headless=not headed, slow_mo_ms=250 if headed else None, profile=profile)
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
        for other in runs.values():          # a new run closes idle sessions (the sandbox is shared)
            if other.session_open:
                other.followups.put(None)
        run_id = new_run_id()
        handle = RunHandle(run_id=run_id, task=body.task, human=WebHumanChannel())
        runs[run_id] = handle

        async def session() -> None:
            agent = Agent(make_llm(settings), settings, handle.human, run_id=run_id,
                          on_event=handle.publish, cancel=handle.cancel, keep_open=True)
            try:
                await agent.run(body.task)
                while not handle.cancel.is_set():
                    try:
                        msg = await asyncio.to_thread(handle.followups.get, True, FOLLOWUP_IDLE_S)
                    except queue.Empty:
                        break
                    if msg is None:
                        break
                    handle.status = "running"
                    await agent.follow_up(msg)
            finally:
                handle.session_open = False
                await agent.close()
                handle.publish(Event("session_closed", run_id, {}))

        def worker() -> None:
            try:
                asyncio.run(session())
            except Exception as e:  # noqa: BLE001 - e.g. missing API key at client construction
                handle.session_open = False
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
        open_ = runs[run_id].session_open if run_id in runs else False
        return {"run_id": run_id, "status": status, "events": events, "pending": pending, "report": report,
                "session_open": open_}

    @app.post("/api/runs/{run_id}/respond")
    def respond(run_id: str, body: Respond) -> dict[str, Any]:
        h = runs.get(run_id)
        if not h or not h.human.resolve(body.request_id, HumanResponse(approved=body.approved, answer=body.answer)):
            raise HTTPException(404, "No such pending request")
        return {"ok": True}

    @app.post("/api/runs/{run_id}/followup")
    def follow_up(run_id: str, body: FollowUp) -> dict[str, Any]:
        h = runs.get(run_id)
        if not h or not h.session_open:
            raise HTTPException(409, "This session is closed. Start a new task instead.")
        if h.status == "running":
            raise HTTPException(409, "The worker is still busy with the previous message.")
        if not body.message.strip():
            raise HTTPException(400, "Message is empty.")
        h.status = "running"
        h.followups.put(body.message.strip())
        return {"ok": True}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str) -> dict[str, Any]:
        h = runs.get(run_id)
        if not h:
            raise HTTPException(404, "Unknown run")
        h.cancel.set()
        h.followups.put(None)
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
        return {"model": settings.model, "provider": settings.llm_provider, "samples": load_samples(settings.samples_file), "profile": settings.profile,
                "api_key_present": key_status(settings)[0], "api_key_message": key_status(settings)[1],
                "systems": {"SupplierHub": sandbox_config.SUPPLIERHUB_URL, "ERP": sandbox_config.ERP_URL,
                            "Webmail": sandbox_config.MAIL_URL},
                "chaos": (health := _sandbox_admin("health", method="GET")).get("chaos"),
                "switches": health.get("switches", []), "seed": health.get("seed"),
                "all_switches": list(SWITCHES)}

    @app.post("/api/sandbox/reset")
    def sandbox_reset() -> dict[str, Any]:
        try:   # reset to this server's profile seed (northwind / centralign); also clears fault switches
            return _admin_client.post(f"{sandbox_config.ERP_URL}/__admin/reset", params={"seed": settings.profile},
                                      timeout=60).json()
        except httpx.HTTPError as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/sandbox/switch/{name}")
    def sandbox_switch(name: str, on: bool = True) -> dict[str, Any]:
        """Turn one named fault switch on/off (see sandbox/chaos.py), e.g. silent-drop for the demo."""
        try:
            r = _admin_client.post(f"{sandbox_config.ERP_URL}/__admin/switch/{name}", params={"on": str(on).lower()})
            return r.json()
        except httpx.HTTPError as e:
            return {"ok": False, "error": str(e)}

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
