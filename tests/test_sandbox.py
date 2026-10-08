"""The simulated company apps behave like the real systems they stand in for."""

from __future__ import annotations

import httpx

from sandbox import config, state

KEY = {"X-API-Key": config.load_credentials()["erp.api_key"]}


def test_seed_contains_the_intended_traps(fresh_sandbox):
    seed = state.current_seed()
    acme = sorted([d for d in seed.documents if d.supplier == "ACME Corporation"], key=lambda d: d.issue_date)
    assert acme[-1].doc_type == "Credit Note"           # newest ACME document is NOT an invoice
    latest_invoice = max((d for d in acme if d.doc_type == "Invoice"), key=lambda d: d.issue_date)
    assert latest_invoice.doc_id == "INV-ACME-1102" and latest_invoice.total == 4812.75
    initech_dates = [d.issue_date for d in seed.documents if d.supplier == "Initech LLC"]
    assert initech_dates.count(max(initech_dates)) == 2  # ambiguous "latest" Initech invoice


def test_supplierhub_requires_login_and_sorts_by_last_updated(fresh_sandbox):
    c = httpx.Client(base_url=config.SUPPLIERHUB_URL, follow_redirects=True)
    assert "/login" in str(c.get("/invoices").url)
    creds = config.load_credentials()
    c.post("/login", data={"username": creds["supplierhub.username"], "password": creds["supplierhub.password"]})
    page1 = c.get("/invoices").text
    # Default sort is "last updated": an old, recently-paid invoice comes first and the
    # genuinely latest ACME invoice is not even on page 1.
    assert page1.index("INV-ACME-1077") < page1.index("GLX-2231")
    assert "INV-ACME-1102" not in page1
    assert "INV-ACME-1102" in c.get("/invoices?page=2").text


def test_erp_validation_rules(fresh_sandbox):
    base = {"vendor_id": "V-001", "invoice_number": "NEW-1", "invoice_date": "2026-01-01",
            "due_date": "2026-01-31", "amount": "100.00", "currency": "USD"}
    bad_amount = httpx.post(f"{config.ERP_URL}/api/bills", headers=KEY, json={**base, "amount": "$1,000.00"})
    assert bad_amount.status_code == 422 and "amount" in bad_amount.json()["fields"]
    bad_date = httpx.post(f"{config.ERP_URL}/api/bills", headers=KEY, json={**base, "due_date": "Jan 31, 2026"})
    assert bad_date.status_code == 422 and "due_date" in bad_date.json()["fields"]
    dup = httpx.post(f"{config.ERP_URL}/api/bills", headers=KEY, json={**base, "invoice_number": "INV-ACME-1077"})
    assert dup.status_code == 409
    ok = httpx.post(f"{config.ERP_URL}/api/bills", headers=KEY, json=base)
    assert ok.status_code == 201 and ok.json()["status"] == "Pending approval"
    assert httpx.get(f"{config.ERP_URL}/api/bills", headers={"X-API-Key": "wrong"}).status_code == 401


def test_hard_chaos_breaks_the_erp_form_but_not_the_api(fresh_sandbox):
    httpx.post(f"{config.ERP_URL}/__admin/chaos/hard")
    from sandbox.chaos import chaos
    chaos._rng.random = lambda: 0.99  # disable random 503s for determinism
    c = httpx.Client(base_url=config.ERP_URL, follow_redirects=True)
    creds = config.load_credentials()
    c.post("/login", data={"username": creds["erp.username"], "password": creds["erp.password"]})
    form = {"vendor_id": "V-001", "invoice_number": "NEW-2", "invoice_date": "2026-01-01",
            "due_date": "2026-01-31", "amount": "5.00", "currency": "USD"}
    assert c.post("/bills/new", data=form).status_code == 500
    api = httpx.post(f"{config.ERP_URL}/api/bills", headers=KEY, json=form)
    assert api.status_code == 201
