"""Shared configuration for the simulated company environment ("the sandbox").

The sandbox stands in for the real systems an Accounts Payable clerk at the
fictional company *Northwind Traders* would use:

* SupplierHub   - a supplier billing portal where vendors publish invoices to us
* Northwind ERP - our internal finance system (web UI + REST API)
* Webmail       - the AP team's shared inbox
* HelpDesk      - an unrelated IT ticketing app (used to test generalization)

Each one runs as a separate web server on its own port so that, from the
agent's point of view, they behave like independent systems.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
STATE_DIR = ROOT / "state"          # runtime state (SQLite DB, generated PDFs, seed manifest)
TEMPLATES_DIR = ROOT / "templates"

HOST = os.getenv("SANDBOX_HOST", "127.0.0.1")
SUPPLIERHUB_PORT = int(os.getenv("SUPPLIERHUB_PORT", "8001"))
ERP_PORT = int(os.getenv("ERP_PORT", "8002"))
MAIL_PORT = int(os.getenv("MAIL_PORT", "8003"))
HELPDESK_PORT = int(os.getenv("HELPDESK_PORT", "8004"))

SUPPLIERHUB_URL = f"http://localhost:{SUPPLIERHUB_PORT}"
ERP_URL = f"http://localhost:{ERP_PORT}"
MAIL_URL = f"http://localhost:{MAIL_PORT}"
HELPDESK_URL = f"http://localhost:{HELPDESK_PORT}"

ERP_DB_PATH = STATE_DIR / "erp.db"
HELPDESK_DB_PATH = STATE_DIR / "helpdesk.db"
SEED_MANIFEST_PATH = STATE_DIR / "seed_manifest.json"
PDF_DIR = STATE_DIR / "pdfs"

# Test-only credentials for the sandbox apps. They are fake values that exist
# only inside this project. The agent never sees them: it refers to them as
# {{secret:<name>}} placeholders and the secret vault substitutes them at the
# moment a tool runs (see worker/safety/secrets.py).
CREDENTIALS_PATH = ROOT / "test_credentials.json"


def load_credentials() -> dict[str, str]:
    return json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
