"""Small helpers shared by the three sandbox apps (sessions, templates, admin routes)."""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, FastAPI, Request
from fastapi.templating import Jinja2Templates

from . import config, state
from .chaos import chaos
from .pdfgen import human_date, money

templates = Jinja2Templates(directory=str(config.TEMPLATES_DIR))
templates.env.filters["human_date"] = human_date
templates.env.filters["money"] = money
templates.env.filters["cents"] = lambda c: f"{c / 100:,.2f}"


@dataclass
class Session:
    username: str
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    requests: int = 0
    data: dict = field(default_factory=dict)


ALL_SESSION_STORES: list["SessionStore"] = []


class SessionStore:
    """In-memory cookie sessions with idle timeout (and chaos-driven expiry)."""

    def __init__(self, cookie_name: str, idle_timeout_s: int = 15 * 60) -> None:
        self.cookie_name = cookie_name
        self.idle_timeout_s = idle_timeout_s
        self._sessions: dict[str, Session] = {}
        ALL_SESSION_STORES.append(self)

    def create(self, username: str) -> str:
        token = secrets.token_urlsafe(16)
        self._sessions[token] = Session(username)
        return token

    def get(self, request: Request, max_requests: int | None = None) -> Session | None:
        token = request.cookies.get(self.cookie_name)
        sess = self._sessions.get(token or "")
        if not sess:
            return None
        now = time.time()
        expired = now - sess.last_seen > self.idle_timeout_s
        if max_requests is not None and sess.requests >= max_requests:
            expired = True
        if expired:
            self._sessions.pop(token, None)
            return None
        sess.last_seen = now
        sess.requests += 1
        return sess

    def destroy(self, request: Request) -> None:
        self._sessions.pop(request.cookies.get(self.cookie_name) or "", None)

    def clear(self) -> None:
        self._sessions.clear()


def admin_router() -> APIRouter:
    """Endpoints used by tests/evals to reset the world. The agent's safety
    policy blocks /__admin paths, so the agent can never call these itself."""
    router = APIRouter(prefix="/__admin")

    @router.post("/reset")
    def reset() -> dict:
        state.reset()
        for s in ALL_SESSION_STORES:
            s.clear()
        return {"ok": True, "today": state.current_seed().today}

    @router.post("/chaos/{mode}")
    def set_chaos(mode: str) -> dict:
        chaos.set_mode(mode)
        for s in ALL_SESSION_STORES:
            s.clear()
        return {"ok": True, "mode": chaos.mode}

    @router.get("/health")
    def health() -> dict:
        return {"ok": True, "chaos": chaos.mode}

    return router


def install_chaos(app: FastAPI, name: str) -> None:
    app.middleware("http")(chaos.middleware(name))
