"""SupplierHub - a mock supplier billing portal (port 8001).

Northwind's AP team logs in here to see invoices that suppliers have published.
Read-only from our side: list (paginated, filterable), detail page, PDF download.
"""

from __future__ import annotations

from math import ceil

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from . import config, state
from .common import SessionStore, admin_router, install_chaos, templates

PAGE_SIZE = 5
sessions = SessionStore("sh_session")

app = FastAPI(title="SupplierHub", docs_url=None, redoc_url=None, openapi_url=None)
install_chaos(app, "SupplierHub")
app.include_router(admin_router())


def _require_login(request: Request):
    sess = sessions.get(request)
    if not sess:
        return None, RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return sess, None


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return RedirectResponse("/invoices", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/invoices", error: str = ""):
    return templates.TemplateResponse(request, "supplierhub/login.html", {"next": next, "error": error})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/invoices")):
    creds = config.load_credentials()
    if username != creds["supplierhub.username"] or password != creds["supplierhub.password"]:
        return templates.TemplateResponse(
            request, "supplierhub/login.html",
            {"next": next, "error": "Invalid username or password."}, status_code=401)
    resp = RedirectResponse(next if next.startswith("/") else "/invoices", status_code=303)
    resp.set_cookie(sessions.cookie_name, sessions.create(username), httponly=True)
    return resp


@app.get("/logout")
def logout(request: Request):
    sessions.destroy(request)
    return RedirectResponse("/login", status_code=303)


@app.get("/invoices", response_class=HTMLResponse)
def invoices(request: Request, supplier: str = "", page: int = 1, sort: str = "updated"):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    seed = state.current_seed()
    docs = seed.documents
    suppliers = sorted({d.supplier for d in docs})
    if supplier:
        docs = [d for d in docs if d.supplier == supplier]
    # Default sort is by "last updated" (like many real portals) - NOT by issue date.
    key = {"updated": lambda d: d.last_updated, "issued": lambda d: d.issue_date,
           "number": lambda d: d.doc_id}.get(sort, lambda d: d.last_updated)
    docs = sorted(docs, key=key, reverse=(sort != "number"))
    pages = max(1, ceil(len(docs) / PAGE_SIZE))
    page = min(max(1, page), pages)
    rows = docs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return templates.TemplateResponse(request, "supplierhub/invoices.html", {
        "user": sess.username, "rows": rows, "suppliers": suppliers, "supplier": supplier,
        "page": page, "pages": pages, "sort": sort, "total": len(docs)})


@app.get("/invoices/{doc_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, doc_id: str):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    doc = next((d for d in state.current_seed().documents if d.doc_id == doc_id), None)
    if not doc:
        return HTMLResponse("<h1>404 - Document not found</h1>", status_code=404)
    return templates.TemplateResponse(request, "supplierhub/invoice.html", {"user": sess.username, "d": doc})


@app.get("/invoices/{doc_id}/pdf")
def invoice_pdf(request: Request, doc_id: str):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    path = config.PDF_DIR / f"{doc_id}.pdf"
    if not path.exists() or doc_id not in {d.doc_id for d in state.current_seed().documents}:
        return HTMLResponse("<h1>404 - File not found</h1>", status_code=404)
    return FileResponse(path, media_type="application/pdf", filename=f"{doc_id}.pdf")
