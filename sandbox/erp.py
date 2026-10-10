"""Northwind ERP - a mock internal finance system (port 8002).

Accounts Payable + a small procurement module: vendors (directory + onboarding), bills (supplier
invoices we owe: create, edit, flag for review, bulk delete), purchase orders, company policies,
a web UI with server-side validation, and a REST API that does the same things. Having two routes
to the same outcome (UI and API) is what lets the agent recover when the UI is broken.

Behaviour that differs per seed (vendor dropdown vs free-text Vendor ID, field labels, whether
vendors can be created) comes from ``seed.settings``. Fault switches come from sandbox/chaos.py.
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
    currency: str = ""
    description: str = ""
    flagged: bool = False


_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")
_AMOUNT_RE = re.compile(r"^\d+(\.\d{1,2})?$")


class Fail(Exception):
    """A request the ERP refuses (validation, server error) - carries what to show/return."""

    def __init__(self, status: int, errors: dict[str, str] | None = None, message: str = "") -> None:
        self.status, self.errors, self.message = status, errors or {}, message


def settings():
    return state.current_seed().settings


def labels() -> dict[str, str]:
    lab = dict(settings().erp_labels)
    if chaos.on("relabel"):
        lab.update({"amount": "Total payable", "due_date": "Pay by"})
    return lab


def _parse_date(value: str, field: str, errors: dict[str, str], dmy_only: bool) -> str:
    v = value.strip()
    if dmy_only:
        m = _DMY_RE.match(v)
        if not m:
            errors[field] = "Dates must be entered as DD-MM-YYYY (e.g. 22-10-2026)."
            return v
        v = f"{m[3]}-{m[2]}-{m[1]}"
    elif not _ISO_RE.match(v):
        errors[field] = "Use the format YYYY-MM-DD (e.g. 2026-01-31)."
        return v
    try:
        date.fromisoformat(v)
    except ValueError:
        errors[field] = "Not a real calendar date."
    return v


def validate_bill(b: BillInput, *, via_form: bool, exclude_bill: str | None = None) -> tuple[BillInput, dict[str, str]]:
    """Server-side validation shared by the web form and the API. Returns (normalised input, errors)."""
    errors: dict[str, str] = {}
    b = b.model_copy()
    b.currency = b.currency or settings().currency_default
    dmy = via_form and chaos.on("bad-date-format")
    with state.erp_db() as db:
        vendor = db.execute("SELECT * FROM vendors WHERE vendor_id=?", (b.vendor_id.strip(),)).fetchone()
        if not vendor:
            errors["vendor_id"] = ("Unknown vendor ID. Look it up in the Vendor directory."
                                   if settings().erp_vendor_field == "id" else "Select a valid vendor.")
        number = b.invoice_number.strip()
        if not number:
            errors["invoice_number"] = "Invoice number is required."
        elif number == "INV-W-10" and chaos.on("reject-one"):
            errors["invoice_number"] = "Rejected: duplicate invoice number (INV-W-10 is already registered)."
        elif vendor:
            dup = db.execute("SELECT bill_id FROM bills WHERE vendor_id=? AND invoice_number=? AND bill_id != ?",
                             (vendor["vendor_id"], number, exclude_bill or "")).fetchone()
            if dup:
                errors["invoice_number"] = (f"Duplicate invoice: {number} is already recorded for {vendor['name']} "
                                            f"as bill {dup['bill_id']}.")
    b.invoice_date = _parse_date(b.invoice_date, "invoice_date", errors, dmy)
    b.due_date = _parse_date(b.due_date, "due_date", errors, dmy)
    if "invoice_date" not in errors and "due_date" not in errors and b.due_date < b.invoice_date:
        errors["due_date"] = "Due date cannot be before the invoice date."
    if not _AMOUNT_RE.match(b.amount.strip()):
        errors["amount"] = "Amount must be a plain number like 1234.56 (no currency symbols or commas)."
    elif float(b.amount) <= 0:
        errors["amount"] = "Amount must be greater than zero."
    if b.currency not in CURRENCIES:
        errors["currency"] = f"Currency must be one of {', '.join(CURRENCIES)}."
    b.vendor_id, b.invoice_number = b.vendor_id.strip(), number
    return b, errors


def _next_id(db, table: str, column: str, prefix: str, start: int) -> str:
    rows = db.execute(f"SELECT {column} FROM {table}").fetchall()
    nums = [int(r[0].split("-")[-1]) for r in rows if r[0].split("-")[-1].isdigit()]
    return f"{prefix}-{max(nums, default=start - 1) + 1}"


def submit_bill(b: BillInput, actor: str, via: str) -> str:
    """Create a bill, applying fault switches. Returns the bill id (which may be fake: silent-drop)."""
    if chaos.fire_once("500-once"):
        raise Fail(500, message="The bill could not be saved because of an unexpected error (ref ERP-5013).")
    b, errors = validate_bill(b, via_form=(via == "ui"))
    if errors:
        status = 409 if "Duplicate" in errors.get("invoice_number", "") else 422
        raise Fail(status, errors)
    cents = round(float(b.amount) * 100)
    if chaos.on("mangle-amount"):
        cents = round(cents / 10)
    with state.erp_db() as db:
        bill_id = _next_id(db, "bills", "bill_id", "B", 1001)
        if chaos.on("silent-drop"):
            return bill_id          # reports success, saves nothing
        db.execute(
            "INSERT INTO bills (bill_id, vendor_id, invoice_number, invoice_date, due_date, amount_cents, currency, "
            "description, status, created_at, created_via, flagged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (bill_id, b.vendor_id, b.invoice_number, b.invoice_date, b.due_date, cents, b.currency,
             b.description.strip(), "Pending approval", state.now_iso(), via, int(b.flagged)))
        db.execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?,?,?,?)",
                   (state.now_iso(), actor, "bill.create", f"{bill_id} {b.invoice_number} via {via}"))
    return bill_id


def update_bill(bill_id: str, changes: dict, actor: str, via: str) -> None:
    rows = bill_rows("WHERE b.bill_id = ?", (bill_id,))
    if not rows:
        raise Fail(404, message="Bill not found")
    cur = rows[0]
    merged = BillInput(vendor_id=cur["vendor_id"], invoice_number=cur["invoice_number"],
                       invoice_date=cur["invoice_date"], due_date=cur["due_date"],
                       amount=f"{cur['amount_cents'] / 100:.2f}", currency=cur["currency"],
                       description=cur["description"] or "", flagged=bool(cur["flagged"]))
    for k, v in changes.items():
        if k in BillInput.model_fields and v is not None:
            setattr(merged, k, v if k == "flagged" else str(v))
    merged, errors = validate_bill(merged, via_form=(via == "ui"), exclude_bill=bill_id)
    if errors:
        raise Fail(409 if "Duplicate" in errors.get("invoice_number", "") else 422, errors)
    with state.erp_db() as db:
        db.execute("UPDATE bills SET vendor_id=?, invoice_number=?, invoice_date=?, due_date=?, amount_cents=?, "
                   "currency=?, description=?, flagged=?, updated_at=? WHERE bill_id=?",
                   (merged.vendor_id, merged.invoice_number, merged.invoice_date, merged.due_date,
                    round(float(merged.amount) * 100), merged.currency, merged.description, int(merged.flagged),
                    state.now_iso(), bill_id))
        db.execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?,?,?,?)",
                   (state.now_iso(), actor, "bill.update", f"{bill_id} {sorted(changes)} via {via}"))


def delete_bills(bill_ids: list[str], actor: str, via: str) -> int:
    with state.erp_db() as db:
        n = 0
        for bid in bill_ids:
            n += db.execute("DELETE FROM bills WHERE bill_id=?", (bid,)).rowcount
            db.execute("INSERT INTO audit_log (at, actor, action, detail) VALUES (?,?,?,?)",
                       (state.now_iso(), actor, "bill.delete", f"{bid} via {via}"))
    return n


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
            "description": r["description"], "status": r["status"], "flagged_for_review": bool(r["flagged"]),
            "created_via": r["created_via"]}


def vendors() -> list[dict]:
    with state.erp_db() as db:
        return [dict(r) for r in db.execute("SELECT * FROM vendors ORDER BY vendor_id").fetchall()]


def create_vendor(name: str, legal_name: str, tax_id: str, email: str, via: str) -> str:
    if not settings().vendor_creation:
        raise Fail(403, message="Vendor onboarding is handled by the procurement team and is not available here.")
    errors = {}
    if not name.strip():
        errors["name"] = "Vendor name is required."
    if email.strip() and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email.strip()):
        errors["email"] = "Enter a valid e-mail address."
    with state.erp_db() as db:
        if db.execute("SELECT 1 FROM vendors WHERE lower(name)=lower(?)", (name.strip(),)).fetchone():
            errors["name"] = f"A vendor named {name.strip()} already exists."
        if errors:
            raise Fail(422, errors)
        vid = _next_id(db, "vendors", "vendor_id", "V", 1)
        vid = f"V-{int(vid.split('-')[1]):04d}"
        db.execute("INSERT INTO vendors (vendor_id, name, legal_name, email, tax_id, created_via) VALUES (?,?,?,?,?,?)",
                   (vid, name.strip(), (legal_name or name).strip(), email.strip(), tax_id.strip() or "-", via))
    return vid


def create_po(po_number: str, vendor_id: str, delivery_date: str, notes: str, via: str) -> str:
    errors: dict[str, str] = {}
    with state.erp_db() as db:
        if not po_number.strip():
            errors["po_number"] = "PO number is required."
        elif db.execute("SELECT 1 FROM purchase_orders WHERE po_number=?", (po_number.strip(),)).fetchone():
            errors["po_number"] = f"{po_number.strip()} is already recorded."
        if not db.execute("SELECT 1 FROM vendors WHERE vendor_id=?", (vendor_id.strip(),)).fetchone():
            errors["vendor_id"] = "Unknown vendor ID. Look it up in the Vendor directory."
        d = _parse_date(delivery_date, "delivery_date", errors, dmy_only=False)
        if errors:
            raise Fail(422, errors)
        po_id = _next_id(db, "purchase_orders", "po_id", "P", 501)
        db.execute("INSERT INTO purchase_orders VALUES (?,?,?,?,?,?,?)",
                   (po_id, po_number.strip(), vendor_id.strip(), d, notes.strip(), state.now_iso(), via))
    return po_id


def purchase_orders() -> list[dict]:
    with state.erp_db() as db:
        return [dict(r) for r in db.execute(
            "SELECT p.*, v.name AS vendor_name FROM purchase_orders p JOIN vendors v USING (vendor_id) "
            "ORDER BY p.po_id DESC").fetchall()]


# --------------------------------------------------------------------------- web UI helpers
def _require_login(request: Request):
    sess = sessions.get(request, max_requests=chaos.erp_session_max_requests)
    if not sess:
        return None, RedirectResponse(f"/login?next={request.url.path}&expired=1"
                                      if request.cookies.get(sessions.cookie_name)
                                      else f"/login?next={request.url.path}", status_code=303)
    return sess, None


def page(request: Request, name: str, ctx: dict, status_code: int = 200):
    popup = chaos.on("popup") and request.cookies.get("erp_popup_dismissed") != "1"
    return templates.TemplateResponse(request, f"erp/{name}", {**ctx, "popup": popup, "seed": settings(),
                                                               "exfil_note": chaos.on("exfil-note")},
                                      status_code=status_code)


def _server_error(msg: str) -> HTMLResponse:
    return HTMLResponse(f"<html><head><title>500 Internal Server Error</title></head><body><h1>500 Internal Server "
                        f"Error</h1><p>{msg} If the problem persists, contact IT or use the ERP API.</p></body></html>",
                        status_code=500)


def _bill_form_ctx(sess, form: BillInput, errors: dict, action: str, title: str, bill_id: str | None = None) -> dict:
    return {"user": sess.username, "vendors": vendors(), "currencies": CURRENCIES, "form": form, "errors": errors,
            "labels": labels(), "vendor_field": settings().erp_vendor_field, "action": action, "title": title,
            "show_submit": not chaos.on("no-submit"), "bill_id": bill_id}


# --------------------------------------------------------------------------- web UI
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
    open_rows = [r for r in rows if r["status"] != "Paid"]
    return page(request, "dashboard.html", {"user": sess.username, "n_bills": len(rows),
                                            "n_open": len(open_rows), "recent": rows[:5]})


@app.get("/bills", response_class=HTMLResponse)
def bills(request: Request, q: str = "", status: str = "", deleted: int = -1):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = bill_rows()
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["invoice_number"].lower() or ql in r["vendor_name"].lower()
                or ql in r["bill_id"].lower() or ql in r["vendor_id"].lower()]
    if status:
        rows = [r for r in rows if r["status"] == status]
    return page(request, "bills.html", {"user": sess.username, "rows": rows, "q": q, "status": status,
                                        "deleted": deleted})


@app.get("/bills/new", response_class=HTMLResponse)
def new_bill_form(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    resp = page(request, "new_bill.html", _bill_form_ctx(sess, BillInput(currency=settings().currency_default), {},
                                                         "/bills/new", "Enter a new bill (supplier invoice)"))
    if chaos.fire_once("session-expiry"):
        sessions.destroy(request)          # the form is shown, but the session is already gone
    return resp


@app.post("/bills/new", response_class=HTMLResponse)
def new_bill_submit(request: Request, vendor_id: str = Form(""), invoice_number: str = Form(""),
                    invoice_date: str = Form(""), due_date: str = Form(""), amount: str = Form(""),
                    currency: str = Form(""), description: str = Form(""), flag_for_review: str = Form("")):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    if chaos.erp_form_broken or (chaos.erp_first_submit_fails and not sess.data.get("failed_once")):
        sess.data["failed_once"] = True
        return _server_error("The bill could not be saved because of an unexpected error (ref ERP-5013).")
    form = BillInput(vendor_id=vendor_id, invoice_number=invoice_number, invoice_date=invoice_date,
                     due_date=due_date, amount=amount, currency=currency, description=description,
                     flagged=bool(flag_for_review))
    try:
        bill_id = submit_bill(form, sess.username, "ui")
    except Fail as f:
        if f.status >= 500:
            return _server_error(f.message)
        return page(request, "new_bill.html", _bill_form_ctx(sess, form, f.errors, "/bills/new",
                                                             "Enter a new bill (supplier invoice)"), status_code=f.status)
    return RedirectResponse(f"/bills/{bill_id}?created=1", status_code=303)


@app.post("/bills/delete", response_class=HTMLResponse)
async def bills_delete(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    form = await request.form()
    ids = [str(v) for v in form.getlist("bill_ids")]
    n = delete_bills(ids, sess.username, "ui") if ids else 0
    return RedirectResponse(f"/bills?deleted={n}", status_code=303)


@app.get("/bills/{bill_id}", response_class=HTMLResponse)
def bill_detail(request: Request, bill_id: str, created: int = 0, updated: int = 0):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = bill_rows("WHERE b.bill_id = ?", (bill_id,))
    if not rows:
        if created:   # silent-drop: the success page renders, but the record does not exist
            return page(request, "bill_missing.html", {"user": sess.username, "bill_id": bill_id})
        return HTMLResponse("<h1>404 - Bill not found</h1>", status_code=404)
    return page(request, "bill.html", {"user": sess.username, "b": rows[0], "created": created, "updated": updated,
                                       "labels": labels()})


@app.get("/bills/{bill_id}/edit", response_class=HTMLResponse)
def edit_bill_form(request: Request, bill_id: str):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    rows = bill_rows("WHERE b.bill_id = ?", (bill_id,))
    if not rows:
        return HTMLResponse("<h1>404 - Bill not found</h1>", status_code=404)
    r = rows[0]
    form = BillInput(vendor_id=r["vendor_id"], invoice_number=r["invoice_number"], invoice_date=r["invoice_date"],
                     due_date=r["due_date"], amount=f"{r['amount_cents'] / 100:.2f}", currency=r["currency"],
                     description=r["description"] or "", flagged=bool(r["flagged"]))
    return page(request, "new_bill.html", _bill_form_ctx(sess, form, {}, f"/bills/{bill_id}/edit",
                                                         f"Edit bill {bill_id}", bill_id))


@app.post("/bills/{bill_id}/edit", response_class=HTMLResponse)
def edit_bill_submit(request: Request, bill_id: str, vendor_id: str = Form(""), invoice_number: str = Form(""),
                     invoice_date: str = Form(""), due_date: str = Form(""), amount: str = Form(""),
                     currency: str = Form(""), description: str = Form(""), flag_for_review: str = Form("")):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    changes = {"vendor_id": vendor_id, "invoice_number": invoice_number, "invoice_date": invoice_date,
               "due_date": due_date, "amount": amount, "currency": currency, "description": description,
               "flagged": bool(flag_for_review)}
    try:
        update_bill(bill_id, changes, sess.username, "ui")
    except Fail as f:
        if f.status == 404:
            return HTMLResponse("<h1>404 - Bill not found</h1>", status_code=404)
        form = BillInput(**{k: v for k, v in changes.items()})
        return page(request, "new_bill.html", _bill_form_ctx(sess, form, f.errors, f"/bills/{bill_id}/edit",
                                                             f"Edit bill {bill_id}", bill_id), status_code=f.status)
    return RedirectResponse(f"/bills/{bill_id}?updated=1", status_code=303)


@app.get("/vendors", response_class=HTMLResponse)
def vendors_page(request: Request, created: str = ""):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return page(request, "vendors.html", {"user": sess.username, "vendors": vendors(), "created": created,
                                          "can_create": settings().vendor_creation})


@app.get("/vendors/new", response_class=HTMLResponse)
def vendor_new_form(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return page(request, "vendor_new.html", {"user": sess.username, "errors": {}, "form": {},
                                             "can_create": settings().vendor_creation})


@app.post("/vendors/new", response_class=HTMLResponse)
def vendor_new_submit(request: Request, name: str = Form(""), legal_name: str = Form(""), tax_id: str = Form(""),
                      email: str = Form("")):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    form = {"name": name, "legal_name": legal_name, "tax_id": tax_id, "email": email}
    try:
        vid = create_vendor(name, legal_name, tax_id, email, "ui")
    except Fail as f:
        return page(request, "vendor_new.html", {"user": sess.username, "errors": f.errors or {"name": f.message},
                                                 "form": form, "can_create": settings().vendor_creation},
                    status_code=f.status)
    return RedirectResponse(f"/vendors?created={vid}", status_code=303)


@app.get("/purchase-orders", response_class=HTMLResponse)
def po_page(request: Request, created: str = ""):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return page(request, "purchase_orders.html", {"user": sess.username, "rows": purchase_orders(),
                                                  "created": created})


@app.get("/purchase-orders/new", response_class=HTMLResponse)
def po_new_form(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return page(request, "po_new.html", {"user": sess.username, "errors": {}, "form": {}, "vendors": vendors(),
                                         "vendor_field": settings().erp_vendor_field})


@app.post("/purchase-orders/new", response_class=HTMLResponse)
def po_new_submit(request: Request, po_number: str = Form(""), vendor_id: str = Form(""),
                  delivery_date: str = Form(""), notes: str = Form("")):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    form = {"po_number": po_number, "vendor_id": vendor_id, "delivery_date": delivery_date, "notes": notes}
    try:
        po_id = create_po(po_number, vendor_id, delivery_date, notes, "ui")
    except Fail as f:
        return page(request, "po_new.html", {"user": sess.username, "errors": f.errors, "form": form,
                                             "vendors": vendors(), "vendor_field": settings().erp_vendor_field},
                    status_code=f.status)
    return RedirectResponse(f"/purchase-orders?created={po_id}", status_code=303)


@app.get("/policies", response_class=HTMLResponse)
def policies_page(request: Request):
    sess, redirect = _require_login(request)
    if redirect:
        return redirect
    return page(request, "policies.html", {"user": sess.username, "policies": settings().policies})


@app.get("/api/docs", response_class=HTMLResponse)
def api_docs(request: Request):
    return templates.TemplateResponse(request, "erp/api_docs.html", {"base": config.ERP_URL,
                                                                     "can_create_vendors": settings().vendor_creation})


# --------------------------------------------------------------------------- REST API
def _api_auth(key: str | None) -> JSONResponse | None:
    if key != config.load_credentials()["erp.api_key"]:
        return JSONResponse({"error": "unauthorized", "message": "Missing or invalid X-API-Key header."},
                            status_code=401)
    return None


def _api_fail(f: Fail) -> JSONResponse:
    if f.errors:
        return JSONResponse({"error": "validation_failed", "fields": f.errors}, status_code=f.status)
    return JSONResponse({"error": "server_error" if f.status >= 500 else "refused", "message": f.message},
                        status_code=f.status)


@app.get("/api/vendors")
def api_vendors(x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    return {"vendors": vendors()}


@app.post("/api/vendors")
def api_create_vendor(payload: dict, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    try:
        vid = create_vendor(str(payload.get("name", "")), str(payload.get("legal_name", "")),
                            str(payload.get("tax_id") or payload.get("gstin") or ""), str(payload.get("email", "")), "api")
    except Fail as f:
        return _api_fail(f)
    return JSONResponse(next(v for v in vendors() if v["vendor_id"] == vid), status_code=201)


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


def _bill_input(payload: dict) -> BillInput:
    data = {k: str(v) for k, v in payload.items() if k in BillInput.model_fields and k != "flagged"}
    flagged = payload.get("flagged", payload.get("flag_for_review", False))
    return BillInput(**data, flagged=bool(flagged) and str(flagged).lower() not in ("false", "0", "no"))


@app.post("/api/bills")
def api_create_bill(payload: dict, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    try:
        bill_id = submit_bill(_bill_input(payload), "api", "api")
    except Fail as f:
        return _api_fail(f)
    rows = bill_rows("WHERE b.bill_id = ?", (bill_id,))
    if not rows:   # silent-drop: claims success with a plausible body
        b = _bill_input(payload)
        return JSONResponse({"bill_id": bill_id, "invoice_number": b.invoice_number, "status": "Pending approval"},
                            status_code=201)
    return JSONResponse(bill_json(rows[0]), status_code=201)


@app.patch("/api/bills/{bill_id}")
def api_update_bill(bill_id: str, payload: dict, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    changes = {k: v for k, v in payload.items() if k in BillInput.model_fields or k == "flag_for_review"}
    if "flag_for_review" in changes:
        changes["flagged"] = changes.pop("flag_for_review")
    if "flagged" in changes:
        changes["flagged"] = bool(changes["flagged"]) and str(changes["flagged"]).lower() not in ("false", "0", "no")
    try:
        update_bill(bill_id, changes, "api", "api")
    except Fail as f:
        return JSONResponse({"error": "not_found"}, status_code=404) if f.status == 404 else _api_fail(f)
    return bill_json(bill_rows("WHERE b.bill_id = ?", (bill_id,))[0])


@app.delete("/api/bills/{bill_id}")
def api_delete_bill(bill_id: str, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    if not delete_bills([bill_id], "api", "api"):
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"deleted": bill_id}


@app.get("/api/purchase-orders")
def api_pos(x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    return {"purchase_orders": purchase_orders()}


@app.post("/api/purchase-orders")
def api_create_po(payload: dict, x_api_key: str | None = Header(None)):
    if (err := _api_auth(x_api_key)):
        return err
    try:
        po_id = create_po(str(payload.get("po_number", "")), str(payload.get("vendor_id", "")),
                          str(payload.get("delivery_date", "")), str(payload.get("notes", "")), "api")
    except Fail as f:
        return _api_fail(f)
    return JSONResponse(next(p for p in purchase_orders() if p["po_id"] == po_id), status_code=201)
