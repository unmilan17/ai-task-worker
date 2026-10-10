"""Builds and resets the sandbox state: ERP + helpdesk SQLite DBs, document PDFs, seed manifest.

Two seeds exist (see data.py and seed_centralign.py). The active seed is chosen with
``reset(seed_name=...)``, the ``SANDBOX_SEED`` environment variable, or ``POST /__admin/reset?seed=``.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from typing import Iterator

from . import config
from .data import Seed, build_seed
from .pdfgen import render_invoice_pdf

_current_seed: Seed | None = None
SEEDS = ("northwind", "centralign")


def current_seed() -> Seed:
    global _current_seed
    if _current_seed is None:
        if config.SEED_MANIFEST_PATH.exists() and config.ERP_DB_PATH.exists():
            manifest = json.loads(config.SEED_MANIFEST_PATH.read_text(encoding="utf-8"))
            _current_seed = build_seed(date.fromisoformat(manifest["today"]), manifest.get("seed", "northwind"))
        else:
            reset()
    assert _current_seed is not None
    return _current_seed


def reset(today: date | None = None, seed_name: str | None = None) -> Seed:
    """Wipe and recreate all sandbox state. Called by the launcher, tests and evals."""
    global _current_seed
    name = seed_name or os.getenv("SANDBOX_SEED") or (_current_seed.name if _current_seed else "northwind")
    seed = build_seed(today, name)
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)

    for doc in seed.all_documents():
        render_invoice_pdf(doc, config.PDF_DIR / f"{doc.doc_id}.pdf")

    for path in (config.ERP_DB_PATH, config.HELPDESK_DB_PATH):
        if path.exists():
            path.unlink()
    with erp_db() as db:
        db.executescript(
            """
            CREATE TABLE vendors (
                vendor_id TEXT PRIMARY KEY, name TEXT NOT NULL, legal_name TEXT NOT NULL,
                email TEXT, tax_id TEXT, created_via TEXT NOT NULL DEFAULT 'seed'
            );
            CREATE TABLE bills (
                bill_id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendors(vendor_id),
                invoice_number TEXT NOT NULL, invoice_date TEXT NOT NULL, due_date TEXT NOT NULL,
                amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, description TEXT,
                status TEXT NOT NULL, created_at TEXT NOT NULL, created_via TEXT NOT NULL,
                flagged INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
                UNIQUE (vendor_id, invoice_number)
            );
            CREATE TABLE purchase_orders (
                po_id TEXT PRIMARY KEY, po_number TEXT NOT NULL UNIQUE, vendor_id TEXT NOT NULL,
                delivery_date TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL, created_via TEXT NOT NULL
            );
            CREATE TABLE audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT
            );
            """
        )
        for v in seed.vendors:
            db.execute("INSERT INTO vendors (vendor_id, name, legal_name, email, tax_id) VALUES (?,?,?,?,?)",
                       (v.vendor_id, v.name, v.legal_name, v.email, v.tax_id))
        for b in seed.bills:
            db.execute(
                "INSERT INTO bills (bill_id, vendor_id, invoice_number, invoice_date, due_date, amount_cents, "
                "currency, description, status, created_at, created_via, flagged) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (b.bill_id, b.vendor_id, b.invoice_number, b.invoice_date, b.due_date, b.amount_cents,
                 b.currency, b.description, b.status, f"{b.invoice_date}T09:00:00", "seed", int(b.flagged)),
            )
    with helpdesk_db() as db:
        db.executescript(
            """
            CREATE TABLE tickets (
                ticket_id TEXT PRIMARY KEY, title TEXT NOT NULL, requester TEXT NOT NULL, status TEXT NOT NULL,
                priority TEXT NOT NULL, description TEXT, updated_at TEXT
            );
            """
        )
        for tk in seed.tickets:
            db.execute("INSERT INTO tickets VALUES (?,?,?,?,?,?,?)",
                       (tk.ticket_id, tk.title, tk.requester, tk.status, tk.priority, tk.description, None))

    config.SEED_MANIFEST_PATH.write_text(json.dumps(seed.to_manifest(), indent=2), encoding="utf-8")
    _current_seed = seed
    return seed


@contextmanager
def _db(path) -> Iterator[sqlite3.Connection]:
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def erp_db():
    return _db(config.ERP_DB_PATH)


def helpdesk_db():
    return _db(config.HELPDESK_DB_PATH)


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()
