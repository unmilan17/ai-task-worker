"""SupplierHub - a mock supplier billing portal (port 8001).

Northwind's AP team logs in here to see documents that suppliers have published (invoices, credit
notes, purchase-order confirmations). Read-only from our side: list (paginated, filterable),
detail page, PDF download. The default sort order depends on the seed ("last updated" for the
Northwind seed, oldest-first for the test-suite seed).
"""

from __future__ import annotations

from math import ceil

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import config, state
from .chaos import chaos
from .common import SessionStore, admin_router, install_chaos, templates
from .pdfgen import human_date, short_date

PAGE_SIZE = 5
sessions = SessionStore("sh_session")

app = FastAPI(title="SupplierHub", docs_url=None, redoc_url=None, openapi_url=None)
install_chaos(app, "SupplierHub")
app.include_router(admin_router())

SORTS = {
    "updated": ("Last updated", lambda d: d.last_updated, True),
    "issued": ("Issue date (newest first)", lambda d: d.issue_date, True),
    "issued_asc": ("Issue date (oldest first)", lambda d: d.issue_date, False),
    "number": ("Document number", lambda d: d.doc_id, False),
}


def _fmt_date():
    seed = state.current_seed()
    return short_date if seed.settings.currency_default == "INR" else human_date


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
def invoices(request: Request, supplier: str = "", page: int = 1, sort: str = "", doc_type: str = ""):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    seed = state.current_seed()
    sort = sort if sort in SORTS else seed.settings.portal_default_sort
    docs = seed.documents
    suppliers = sorted({d.supplier for d in docs})
    doc_types = sorted({d.doc_type for d in docs})
    if supplier:
        docs = [d for d in docs if d.supplier == supplier]
    if doc_type:
        docs = [d for d in docs if d.doc_type == doc_type]
    _, key, reverse = SORTS[sort]
    docs = sorted(docs, key=lambda d: (key(d), d.doc_id), reverse=reverse)
    pages = max(1, ceil(len(docs) / PAGE_SIZE))
    page = min(max(1, page), pages)
    rows = docs[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return templates.TemplateResponse(request, "supplierhub/invoices.html", {
        "user": sess.username, "rows": rows, "suppliers": suppliers, "supplier": supplier, "doc_types": doc_types,
        "doc_type": doc_type, "page": page, "pages": pages, "sort": sort, "sorts": SORTS, "total": len(docs),
        "fmt": _fmt_date()})


@app.get("/invoices/{doc_id}", response_class=HTMLResponse)
def invoice_detail(request: Request, doc_id: str):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    doc = next((d for d in state.current_seed().documents if d.doc_id == doc_id), None)
    if not doc:
        return HTMLResponse("<h1>404 - Document not found</h1>", status_code=404)
    shipped_note = "Shipped 25/03/2026" if doc_id == "INV-S-4" and chaos.on("s4-shipped-note") else ""
    return templates.TemplateResponse(request, "supplierhub/invoice.html",
                                      {"user": sess.username, "d": doc, "fmt": _fmt_date(), "shipped_note": shipped_note})


@app.get("/invoices/{doc_id}/pdf")
def invoice_pdf(request: Request, doc_id: str):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    path = config.PDF_DIR / f"{doc_id}.pdf"
    if not path.exists() or doc_id not in {d.doc_id for d in state.current_seed().documents}:
        return HTMLResponse("<h1>404 - File not found</h1>", status_code=404)
    if chaos.on("broken-pdf-link"):
        return HTMLResponse("<h1>404 - File not found</h1><p>The file storage link is broken.</p>", status_code=404)
    if chaos.on("corrupt-pdf"):
        junk = b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog" + bytes(range(256)) * 8
        return Response(junk, media_type="application/pdf",
                        headers={"Content-Disposition": f'attachment; filename="{doc_id}.pdf"'})
    return FileResponse(path, media_type="application/pdf", filename=f"{doc_id}.pdf")
