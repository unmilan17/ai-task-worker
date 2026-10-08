"""Generates realistic-looking invoice PDFs for the sandbox (using fpdf2)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fpdf import FPDF

from .data import PortalDocument


def human_date(iso: str | None) -> str:
    if not iso:
        return "-"
    return date.fromisoformat(iso).strftime("%B %d, %Y").replace(" 0", " ")


def money(x: float) -> str:
    sign = "-" if x < 0 else ""
    return f"{sign}${abs(x):,.2f}"


def render_invoice_pdf(doc: PortalDocument, out_path: Path) -> Path:
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.set_auto_page_break(True, margin=15)

    pdf.set_font("Helvetica", "B", 20)
    pdf.cell(0, 12, doc.supplier, new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    pdf.cell(0, 6, "Accounts Receivable", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(6)

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, doc.doc_type.upper(), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 11)
    rows = [
        ("Document number", doc.doc_id),
        ("Issue date", human_date(doc.issue_date)),
        ("Due date", human_date(doc.due_date)),
        ("Customer PO", doc.po_number),
        ("Bill to", "Northwind Traders, Accounts Payable"),
    ]
    for k, v in rows:
        pdf.cell(45, 7, k + ":")
        pdf.cell(0, 7, v, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(5)

    pdf.set_font("Helvetica", "B", 11)
    pdf.cell(95, 8, "Description", border=1)
    pdf.cell(20, 8, "Qty", border=1, align="R")
    pdf.cell(35, 8, "Unit price", border=1, align="R")
    pdf.cell(35, 8, "Amount", border=1, align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for item in doc.items:
        pdf.cell(95, 8, item.description[:55], border=1)
        pdf.cell(20, 8, str(item.quantity), border=1, align="R")
        pdf.cell(35, 8, money(item.unit_price), border=1, align="R")
        pdf.cell(35, 8, money(item.total), border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)
    for label, val, bold in (("Subtotal", doc.subtotal, False), ("Sales tax", doc.tax, False),
                             ("TOTAL DUE (USD)", doc.total, True)):
        pdf.set_font("Helvetica", "B" if bold else "", 11)
        pdf.cell(150, 8, label, align="R")
        pdf.cell(35, 8, money(val), align="R", new_x="LMARGIN", new_y="NEXT")

    if doc.memo:
        pdf.ln(6)
        pdf.set_font("Helvetica", "I", 10)
        pdf.multi_cell(0, 6, doc.memo)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path
