"""Webmail - the AP team's shared inbox (port 8003).

Single-sign-on is assumed (no login page) to keep the demo focused. Emails can
carry PDF attachments, and the inbox deliberately contains a phishing /
prompt-injection message that the agent must NOT obey.
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse

from . import config, state
from .common import admin_router, install_chaos, templates

app = FastAPI(title="Webmail", docs_url=None, redoc_url=None, openapi_url=None)
install_chaos(app, "Webmail")
app.include_router(admin_router())

MAILBOX = "ap-team@northwind.example"


@app.get("/", response_class=HTMLResponse)
def inbox(request: Request):
    emails = sorted(state.current_seed().emails, key=lambda e: e.received, reverse=True)
    return templates.TemplateResponse(request, "mail/inbox.html", {"emails": emails, "mailbox": MAILBOX})


@app.get("/message/{email_id}", response_class=HTMLResponse)
def message(request: Request, email_id: str):
    email = next((e for e in state.current_seed().emails if e.email_id == email_id), None)
    if not email:
        return HTMLResponse("<h1>404 - Message not found</h1>", status_code=404)
    return templates.TemplateResponse(request, "mail/message.html", {"e": email, "mailbox": MAILBOX})


@app.get("/attachments/{email_id}/{filename}")
def attachment(email_id: str, filename: str):
    email = next((e for e in state.current_seed().emails if e.email_id == email_id), None)
    if not email or not email.attachment or filename != f"{email.attachment}.pdf":
        return HTMLResponse("<h1>404 - Attachment not found</h1>", status_code=404)
    return FileResponse(config.PDF_DIR / filename, media_type="application/pdf", filename=filename)
