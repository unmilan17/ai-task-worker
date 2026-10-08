"""Northwind ERP - a mock internal finance system (port 8002).

Accounts Payable module only: vendors, bills (supplier invoices we owe), a web
UI with server-side validation, and a REST API that does the same thing.
Having two routes to the same outcome (UI and API) is what lets the agent
recover when the UI is broken (CHAOS=hard).
"""

from __future__ import annotations

import re
from datetime import date

from fastapi import FastAPI, Form, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from . import config, state
from .chaos import chaos
from .common import SessionStore, admin_router, install_chaos, templates

CURRENCIES = ["USD", "EUR", "GBP", "INR"]
sessions = SessionStore("erp_session")

app = FastAPI(title="Northwind ERP", docs_url=None, redoc_url=None, openapi_url=None)
install_chaos(app, "Northwind ERP")
app.include_router(admin_router())


# --------------------------------------------------------------------------- domain
class BillInput(BaseModel):
    vendor_id: str = ""
    invoice_number: str = ""
    invoice_date: str = ""
    due_date: str = ""
    amount: str = ""
    currency: str = "USD"
    description: str = ""


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


def validate_bill(b: BillInput) -> dict[str, str]:
    """Server-side validation shared by the web form and the API. Returns field -> error."""
    errors: dict[str, str] = {}
    with state.erp_db() as db:
        vendor = db.execute("SELECT * FROM vendors WHERE vendor_id=?", (b.vendor_id,)).fetchone()
        if not vendor:
            errors["vendor_id"] = "Select a valid vendor."
        if not b.invoice_number.strip():
            errors["invoice_number"] = "Invoice number is required."
        elif vendor:
            dup = db.execute("SELECT bill_id FROM bills WHERE vendor_id=? AND invoice_number=?",
                             (b.vendor_id, b.invoice_number.strip())).fetchone()
            if dup:
                errors["invoice_number"] = (f"Duplicate invoice: {b.invoice_number.strip()} is already recorded "
                                            f"for {vendor['name']} as bill {dup['bill_id']}.")
    for f in ("invoice_date", "due_date"):
        v = getattr(b, f).strip()
        if not _DATE_RE.match(v):
            errors[f] = "Use the format YYYY-MM-DD (e.g. 2026-01-31)."
        else:
            try:
                date.fromisoformat(v)
            except ValueError:
                errors[f] = "Not a real calendar date."
    if "invoice_date" not in errors and "due_date" not in errors and b.due_date < b.invoice_date:
        errors["due_date"] = "Due date cannot be before the invoice date."
    if not _AMOUNT_RE.match(b.amount.strip()):
        errors["amount"] = "Amount must be a plain number like 1234.56 (no currency symbols or commas)."
    elif float(b.amount) <= 0:
        errors["amount"] = "Amount must be greater than zero."
    if b.currency not in CURRENCIES:
        errors["currency"] = f"Currency must be one of {', '.join(CURRENCIES)}."
    return errors


def create_bill(b: BillInput, actor: str, via: str) -> str:
    with state.erp_db() as db:
        n = db.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
        bill_id = f"B-{1001 + n}"
        db.execute(
            "INSERT INTO bills VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (bill_id, b.vendor_id, b.invoice_number.strip(), b.invoice_date.strip(), b.due_date.strip(),
             round(float(b.amount) * 100), b.currency, b.description.strip(), "Pending approval",
             state.now_iso(), via))
        db.execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?,?,?,?)",
                   (state.now_iso(), actor, "bill.create", f"{bill_id} {b.invoice_number} via {via}"))
    return bill_id


def bill_rows(where: str = "", params: tuple = ()) -> list[dict]:
    with state.erp_db() as db:
        rows = db.execute(
            "SELECT b.*, v.name AS vendor_name FROM bills b JOIN vendors v USING (vendor_id) "
            f"{where} ORDER BY b.bill_id DESC", params).fetchall()
    return [dict(r) for r in rows]


def bill_json(r: dict) -> dict:
    return {"bill_id": r["bill_id"], "vendor_id": r["vendor_id"], "vendor_name": r["vendor_name"],
            "invoice_number": r["invoice_number"], "invoice_date": r["invoice_date"],
            "due_date": r["due_date"], "amount": r["amount_cents"] / 100, "currency": r["currency"],
            "description": r["description"], "status": r["status"], "created_via": r["created_via"]}


def vendors() -> list[dict]:
    with state.erp_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM vendors ORDER BY vendor_id").fetchall()]


# --------------------------------------------------------------------------- web UI
def _require_login(request: Request):
    sess = sessions.get(request, max_requests=chaos.erp_session_max_requests)
    if not sess:
        return None, RedirectResponse(f"/login?next={request.url.path}&expired=1"
                                      if request.cookies.get(sessions.cookie_name)
                                      else f"/login?next={request.url.path}", status_code=303)
    return sess, None


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/", expired: int = 0):
    msg = "Your session has expired. Please sign in again." if expired else ""
    return templates.TemplateResponse(request, "erp/login.html", {"next": next, "error": msg})


@app.post("/login")
def login(request: Request, username: str = Form(""), password: str = Form(""), next: str = Form("/")):
    creds = config.load_credentials()
    if username != creds["erp.username"] or password != creds["erp.password"]:
        return templates.TemplateResponse(request, "erp/login.html",
                                          {"next": next, "error": "Invalid credentials."}, status_code=401)
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("/login") else "/",
                            status_code=303)
    resp.set_cookie(sessions.cookie_name, sessions.create(username), httponly=True)
    return resp


@app.get("/logout")
def logout(request: Request):
    sessions.destroy(request)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(sessions.cookie_name)
    return resp


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = bill_rows()
    open_total = sum(r["amount_cents"] for r in rows if r["status"] != "Paid") / 100
    return templates.TemplateResponse(request, "erp/dashboard.html", {
        "user": sess.username, "n_bills": len(rows), "open_total": open_total, "recent": rows[:5]})


@app.get("/bills", response_class=HTMLResponse)
def bills(request: Request, q: str = "", status: str = ""):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = bill_rows()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["invoice_number"].lower() or ql in r["vendor_name"].lower()
                or ql in r["bill_id"].lower()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return templates.TemplateResponse(request, "erp/bills.html",
                                      {"user": sess.username, "rows": rows, "q": q, "status": status})


@app.get("/bills/new", response_class=HTMLResponse)
def new_bill_form(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "erp/new_bill.html", {
        "user": sess.username, "vendors": vendors(), "currencies": CURRENCIES,
        "form": BillInput(), "errors": {}})


@app.post("/bills/new", response_class=HTMLResponse)
def new_bill_submit(request: Request, vendor_id: str = Form(""), invoice_number: str = Form(""),
                    invoice_date: str = Form(""), due_date: str = Form(""), amount: str = Form(""),
                    currency: str = Form("USD"), description: str = Form("")):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    if chaos.erp_form_broken or (chaos.erp_first_submit_fails and not sess.data.get("failed_once")):
        sess.data["failed_once"] = True
        return HTMLResponse(
            "<html><head><title>500 Internal Server Error</title></head><body><h1>500 Internal Server Error</h1>"
            "<p>The bill could not be saved because of an unexpected error (ref ERP-5013). "
            "If the problem persists, contact IT or use the ERP API.</p></body></html>", status_code=500)
    form = BillInput(vendor_id=vendor_id, invoice_number=invoice_number, invoice_date=invoice_date,
                     due_date=due_date, amount=amount, currency=currency, description=description)
    errors = validate_bill(form)
    if errors:
        return templates.TemplateResponse(request, "erp/new_bill.html", {
            "user": sess.username, "vendors": vendors(), "currencies": CURRENCIES,
            "form": form, "errors": errors}, status_code=422)
    bill_id = create_bill(form, sess.username, "ui")
    return RedirectResponse(f"/bills/{bill_id}?created=1", status_code=303)


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: str, created: int = 0):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = bill_rows("WHERE b.bill_id = ?", (bill_id,))
    if not rows:
        return HTMLResponse("<h1>404 - Bill not found</h1>", status_code=404)
    return templates.TemplateResponse(request, "erp/bill.html",
                                      {"user": sess.username, "b": rows[0], "created": created})


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return templates.TemplateResponse(request, "erp/vendors.html", {"user": sess.username, "vendors": vendors()})


@app.get("/api/docs", response_class=HTMLResponse)
def api_docs(request: Request):
    return templates.TemplateResponse(request, "erp/api_docs.html", {"base": config.ERP_URL})


# --------------------------------------------------------------------------- REST API
def _api_auth(key: str | None) -> JSONResponse | None:
    if key != config.load_credentials()["erp.api_key"]:
        return JSONResponse({"error": "unauthorized", "message": "Missing or invalid X-API-Key header."},
                            status_code=401)
    return None


@app.get("/api/vendors")
def api_vendors(x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    return {"vendors": vendors()}


@app.get("/api/bills")
def api_bills(x_api_key: str | None = Header(None), vendor_id: str = "", invoice_number: str = "",
              status: str = "", due_after: str = "", due_before: str = ""):
    if (err := _api_auth(x_api_key)):
        return err
    rows = [bill_json(r) for r in bill_rows()]
    if vendor_id:
        rows = [r for r in rows if r["vendor_id"] == vendor_id]
    if invoice_number:
        rows = [r for r in rows if r["invoice_number"] == invoice_number]
    if status:
        rows = [r for r in rows if r["status"] == status]
    if due_after:
        rows = [r for r in rows if r["due_date"] >= due_after]
    if due_before:
        rows = [r for r in rows if r["due_date"] <= due_before]
    return {"bills": rows, "count": len(rows)}


@app.get("/api/bills/{bill_id}")
def api_bill(bill_id: str, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    rows = bill_rows("WHERE b.bill_id = ?", (bill_id,))
    if not rows:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return bill_json(rows[0])


@app.post("/api/bills")
def api_create_bill(payload: dict, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    try:
        b = BillInput(**{k: str(v) for k, v in payload.items() if k in BillInput.model_fields})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "bad_request", "message": str(e)}, status_code=400)
    errors = validate_bill(b)
    if errors:
        status = 409 if "Duplicate" in errors.get("invoice_number", "") else 422
        return JSONResponse({"error": "validation_failed", "fields": errors}, status_code=status)
    bill_id = create_bill(b, "api", "api")
    return JSONResponse(bill_json(bill_rows("WHERE b.bill_id = ?", (bill_id,))[0]), status_code=201)
