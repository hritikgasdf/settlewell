"""Create Lumen Textiles, INV-2291, and its transfer in SQLite."""

import argparse
from pathlib import Path

from backend.ledger import HERO_INVOICE, HERO_SUPPLIER, connect, hero_bundle, seed_hero


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="var/settlewell.db")
    args = parser.parse_args()
    path = Path(args.db)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    incident_id = seed_hero(conn)
    bundle = hero_bundle(conn, incident_id)
    supplier_ok = bundle["supplier"]["name"] == HERO_SUPPLIER
    invoice_ok = bundle["obligation"]["invoice_ref"] == HERO_INVOICE
    transfer_ok = bool(bundle["transfers"]) and bundle["obligation"]["live_transfer_id"] == bundle["transfers"][0]["id"]
    print("seed_supplier", "pass" if supplier_ok else "fail")
    print("seed_invoice", "pass" if invoice_ok else "fail")
    print("seed_transfer", "pass" if transfer_ok else "fail")


if __name__ == "__main__":
    main()
