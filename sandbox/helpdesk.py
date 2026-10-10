"""HelpDesk - a small, unrelated IT ticketing app (port 8004).

It exists to test generalization: nothing in the agent was built for it. Sign in, list tickets,
open one, change its priority/status, save. Changes are visible on the ticket page afterwards.
"""

from __future__ import annotations

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from . import config, state
from .common import SessionStore, admin_router, install_chaos, templates

PRIORITIES = ["Low", "Medium", "High", "Urgent"]
STATUSES = ["Open", "In progress", "Resolved", "Closed"]
sessions = SessionStore("hd_session")

app = FastAPI(title="HelpDesk", docs_url=None, redoc_url=None, openapi_url=None)
install_chaos(app, "HelpDesk")
app.include_router(admin_router())


def _require_login(request: Request):
    sess = sessions.get(request)
    if not sess:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return sess, None


def _tickets() -> list[dict]:
    with state.helpdesk_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM tickets ORDER BY ticket_id").fetchall()]


@app.get("/", response_class=HTMLResponse)
def home():
    return RedirectResponse("/tickets", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/tickets", error: str = ""):
    return templates.TemplateResponse(request, "helpdesk/login.html", {"next": next, "error": error})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/tickets")):
    creds = config.load_credentials()
    if username != creds["helpdesk.username"] or password != creds["helpdesk.password"]:
        return templates.TemplateResponse(request, "helpdesk/login.html",
                                          {"next": next, "error": "Wrong username or password."}, status_code=401)
    resp = RedirectResponse(next if next.startswith("/") else "/tickets", status_code=303)
    resp.set_cookie(sessions.cookie_name, sessions.create(username), httponly=True)
    return resp


@app.get("/tickets", response_class=HTMLResponse)
def tickets(request: Request, q: str = ""):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = _tickets()
    if q:
        rows = [t for t in rows if q.lower() in (t["title"] + " " + t["description"]).lower()]
    return templates.TemplateResponse(request, "helpdesk/tickets.html", {"user": sess.username, "rows": rows, "q": q})


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket(request: Request, ticket_id: str, saved: int = 0):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    t = next((t for t in _tickets() if t["ticket_id"] == ticket_id), None)
    if not t:
        return HTMLResponse("<h1>404 - Ticket not found</h1>", status_code=404)
    return templates.TemplateResponse(request, "helpdesk/ticket.html", {
        "user": sess.username, "t": t, "priorities": PRIORITIES, "statuses": STATUSES, "saved": saved})


@app.post("/tickets/{ticket_id}")
def update_ticket(request: Request, ticket_id: str, priority: str = Form(""), status: str = Form("")):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    if priority not in PRIORITIES or status not in STATUSES:
        return HTMLResponse("<h1>400 - Invalid priority or status</h1>", status_code=400)
    with state.helpdesk_db() as db:
        n = db.execute("UPDATE tickets SET priority=?, status=?, updated_at=? WHERE ticket_id=?",
                       (priority, status, state.now_iso(), ticket_id)).rowcount
    if not n:
        return HTMLResponse("<h1>404 - Ticket not found</h1>", status_code=404)
    return RedirectResponse(f"/tickets/{ticket_id}?saved=1", status_code=303)
