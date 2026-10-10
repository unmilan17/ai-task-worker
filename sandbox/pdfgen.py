"""Generates realistic-looking document PDFs for the sandbox (fpdf2 + Pillow).

* normal invoices / credit notes / purchase orders: text PDFs (pypdf can extract the text);
* ``scanned=True``: the page is a picture of the invoice (no text layer), as from a scanner,
  so reading it needs vision (or OCR);
* ``hidden_text``: white-on-white text that a human never sees but text extraction does
  (a prompt-injection test).
"""

from __future__ import annotations

import io
from datetime import date
from pathlib import Path

from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

from .data import PortalDocument

_SYMBOL = {"USD": "$", "INR": "₹", "EUR": "€", "GBP": "£"}


def human_date(iso: str | None) -> str:
    if not iso:
        return "-"
    return date.fromisoformat(iso).strftime("%B %d, %Y").replace(" 0", " ")


def short_date(iso: str | None) -> str:
    """'22 Oct 2026' style (used by the centralign seed)."""
    if not iso:
        return "-"
    return date.fromisoformat(iso).strftime("%d %b %Y")


def _indian_group(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def money(x: float, currency: str = "USD", symbol: bool = True) -> str:
    """$18,250.00 / ₹1,12,100.00 (Indian digit grouping for INR) / -$150.00"""
    sign = "-" if x < 0 else ""
    cents = round(abs(x) * 100)
    whole, frac = divmod(cents, 100)
    grouped = _indian_group(whole) if currency == "INR" else f"{whole:,}"
    prefix = _SYMBOL.get(currency, currency + " ") if symbol else ""
    return f"{sign}{prefix}{grouped}.{frac:02d}"


def pdf_money(x: float, currency: str) -> str:
    """PDF core fonts have no ₹ glyph, so PDFs print 'INR 1,12,100.00' / 'USD 2,400.00' / '$...'."""
    if currency == "USD":
        return money(x, "USD")
    return f"{currency} {money(x, currency, symbol=False)}"


def _rows(doc: PortalDocument) -> list[tuple[str, str]]:
    issue = doc.display_issue or (short_date(doc.issue_date) if doc.currency == "INR" else human_date(doc.issue_date))
    rows = [("Document number", doc.doc_id), ("Issue date", issue)]
    if doc.doc_type == "Purchase Order":
        rows.append(("Delivery date", short_date(doc.delivery_date)))
    else:
        due = doc.display_due or (short_date(doc.due_date) if doc.currency == "INR" and doc.due_date
                                  else human_date(doc.due_date) if doc.due_date else None)
        if due:
            rows.append(("Due date", due))
        if doc.terms:
            rows.append(("Payment terms", doc.terms))
    rows += [("Customer PO", doc.po_number), ("Bill to", "Northwind Traders, Accounts Payable"),
             ("Currency", doc.currency)]
    return rows


def render_invoice_pdf(doc: PortalDocument, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if doc.scanned:
        return _render_scanned(doc, out_path)
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
    for k, v in _rows(doc):
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
        pdf.cell(35, 8, pdf_money(item.unit_price, doc.currency), border=1, align="R")
        pdf.cell(35, 8, pdf_money(item.total, doc.currency), border=1, align="R", new_x="LMARGIN", new_y="NEXT")

    pdf.ln(3)
    total_label = "ORDER TOTAL" if doc.doc_type == "Purchase Order" else f"TOTAL DUE ({doc.currency})"
    for label, val, bold in (("Subtotal", doc.subtotal, False), (doc.tax_label, doc.tax, False),
                             (total_label, doc.total, True)):
        pdf.set_font("Helvetica", "B" if bold else "", 11)
        pdf.cell(150, 8, label, align="R")
        pdf.cell(35, 8, pdf_money(val, doc.currency), align="R", new_x="LMARGIN", new_y="NEXT")

    if doc.memo:
        pdf.ln(6)
        pdf.set_font("Helvetica", "I", 10)
        pdf.multi_cell(0, 6, doc.memo)
    if doc.hidden_text:
        pdf.ln(4)
        pdf.set_text_color(255, 255, 255)          # invisible on a white page, still in the text layer
        pdf.set_font("Helvetica", "", 6)
        pdf.multi_cell(0, 4, doc.hidden_text)
        pdf.set_text_color(0, 0, 0)

    pdf.output(str(out_path))
    return out_path


def _render_scanned(doc: PortalDocument, out_path: Path) -> Path:
    """An image-only PDF: draw the invoice onto a picture and embed just the picture."""
    img = Image.new("L", (1240, 1754), 248)                    # A4 at 150 dpi, slightly grey "paper"
    draw = ImageDraw.Draw(img)
    big, mid, small = (ImageFont.load_default(size=s) for s in (52, 30, 26))
    y = 90
    draw.text((90, y), doc.supplier, font=big, fill=20)
    y += 110
    draw.text((90, y), doc.doc_type.upper(), font=mid, fill=20)
    y += 70
    for k, v in _rows(doc):
        draw.text((90, y), f"{k}:", font=small, fill=30)
        draw.text((470, y), v, font=small, fill=30)
        y += 44
    y += 40
    draw.line((90, y, 1150, y), fill=60, width=2)
    y += 20
    for item in doc.items:
        draw.text((90, y), f"{item.description}  x{item.quantity}", font=small, fill=30)
        draw.text((880, y), pdf_money(item.total, doc.currency), font=small, fill=30)
        y += 44
    y += 30
    draw.text((560, y), f"TOTAL DUE ({doc.currency}):", font=mid, fill=10)
    draw.text((900, y), pdf_money(doc.total, doc.currency), font=mid, fill=10)
    img = img.rotate(0.4, fillcolor=248)                     # scans are never perfectly straight
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    pdf = FPDF(format="A4")
    pdf.add_page()
    pdf.image(buf, x=0, y=0, w=210, h=297)
    pdf.output(str(out_path))
    return out_path
