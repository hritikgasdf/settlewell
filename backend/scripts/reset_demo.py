"""Reset the local hero ledger. Sandbox SENT transfers are released only on the demo host.

Reads credentials from the environment when SETTLEWELL_SANDBOX=1. Never prints them.
"""

from __future__ import annotations

import argparse
import os
import sys

from backend.airwallex.client import DEFAULT_BASE_URL, AirwallexClient
from backend.ledger import HERO_INVOICE, HERO_SUPPLIER, hero_bundle
from backend.reset import ResetError, open_ledger, reset_demo
from backend.sandbox import SandboxDriver
from settlewell.flags import mock_mode, sandbox_enabled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="var/settlewell.db")
    args = parser.parse_args(argv)
    base_url = os.environ.get("AIRWALLEX_BASE_URL", DEFAULT_BASE_URL)
    conn = open_ledger(args.db)
    driver = None
    if sandbox_enabled() and not mock_mode():
        client = AirwallexClient.from_env()
        driver = SandboxDriver(client)
    try:
        incident_id = reset_demo(conn, base_url=base_url, driver=driver)
    except ResetError as error:
        print("reset_base", error.code)
        return 1
    bundle = hero_bundle(conn, incident_id)
    supplier_ok = bundle["supplier"]["name"] == HERO_SUPPLIER
    invoice_ok = bundle["obligation"]["invoice_ref"] == HERO_INVOICE
    sent_left = conn.execute(
        """
        SELECT id FROM transfer_record
        WHERE status = 'SENT' AND airwallex_id NOT LIKE 'synthetic%'
        """
    ).fetchone()
    print("reset_supplier", "pass" if supplier_ok else "fail")
    print("reset_invoice", "pass" if invoice_ok else "fail")
    print("reset_sent", "pass" if sent_left is None else "fail")
    return 0 if supplier_ok and invoice_ok and sent_left is None else 1


if __name__ == "__main__":
    sys.exit(main())
