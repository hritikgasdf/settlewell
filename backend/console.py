"""Mock console for the first four hero beats. No Airwallex calls."""

from __future__ import annotations

import hashlib
from html import escape

from backend.approvals import AP_CLERK, ApprovalTerms, propose
from backend.ledger import (
    append_audit,
    append_bank_detail_version,
    append_status_snapshot,
    connect,
    insert_external_transfer,
    lock_fx_quote,
    migrate,
)
from backend.policy import decide
from backend.reconcile import timeline
from settlewell.flags import mock_mode

SEEN = "2026-11-03T12:00:00+00:00"
_LABELS = {
    "console:1": "Late SENT",
    "console:2": "Still processing",
    "console:3": "Scheduled duplicate",
    "console:4": "Already paid",
}


class ConsoleError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def render(path: str, conn=None) -> str:
    """Return one console page. Refuses to run unless SETTLEWELL_MOCK=1."""
    if not mock_mode():
        raise ConsoleError("mock")
    own = conn is None
    if conn is None:
        conn = connect()
    seed_console(conn)
    if path in ("/", "/inbox"):
        page = _inbox(conn)
    elif path == "/ledger":
        page = _ledger(conn)
    elif path.startswith("/incidents/"):
        page = _incident(conn, path.removeprefix("/incidents/"))
    else:
        page = _layout("Not found", "<p>That page is not in the console.</p>")
    if own:
        conn.close()
    return page


def seed_console(conn) -> None:
    """Create the four clickable beats. A second call leaves them as they are."""
    migrate(conn)
    if conn.execute("SELECT id FROM incident WHERE claim_json = 'console:1'").fetchone():
        return
    supplier = conn.execute(
        "INSERT INTO supplier (name, contact, phone_on_file) VALUES (?, ?, ?)",
        ("Lumen Textiles GmbH", "ap@lumen.example", "+49-30-000000"),
    )
    supplier_id = int(supplier.lastrowid)
    append_bank_detail_version(
        conn,
        supplier_id=supplier_id,
        beneficiary_id="ben_onboarding",
        details_hash="onboarding-v1",
        bank_name="Commerzbank",
        account_last4="4400",
        source="onboarding",
    )
    late = _obligation(conn, supplier_id, "INV-2291", "awx-late", "SENT", "2026-11-04")
    _beat(
        conn,
        late,
        claim="console:1",
        state="OPEN",
        status="SENT",
        evidence="dispatch_date 2026-11-04 is after transfer_date 2026-11-03",
        action="SEND_REPLY",
    )
    duplicate = insert_external_transfer(
        conn,
        airwallex_id="awx-dup",
        request_id="colleague-friday",
        obligation_id=late,
        amount="4200.00",
        currency="EUR",
        status="SCHEDULED",
        reference="INV-2291",
    )
    append_status_snapshot(
        conn,
        transfer_record_id=int(duplicate),
        status="SCHEDULED",
        failure_code=None,
        failure_message=None,
        source="poll",
        seen_at=SEEN,
    )
    _beat(
        conn,
        late,
        claim="console:3",
        state="DUPLICATE",
        status="SCHEDULED",
        evidence="untagged SCHEDULED transfer awx-dup",
        action="STOP_DUPLICATE",
        duplicate=True,
    )
    processing = _obligation(conn, supplier_id, "INV-2302", "awx-proc", "PROCESSING", None)
    _beat(
        conn,
        processing,
        claim="console:2",
        state="OPEN",
        status="PROCESSING",
        evidence="status PROCESSING, still in flight",
        action="SEND_REPLY",
    )
    paid = _obligation(conn, supplier_id, "INV-2404", "awx-paid", "PAID", "2026-11-03")
    _beat(
        conn,
        paid,
        claim="console:4",
        state="OPEN",
        status="PAID",
        evidence="status PAID, no replace",
        action="SEND_REPLY",
    )
    _seed_result(conn)
    conn.commit()


def _obligation(conn, supplier_id: int, invoice: str, airwallex_id: str, status: str, dispatch: str | None) -> int:
    obligation = conn.execute(
        """
        INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status)
        VALUES (?, '4200.00', 'EUR', ?, 'OPEN')
        """,
        (invoice, supplier_id),
    )
    transfer = conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, details_hash,
            amount, currency, status, dispatch_date, reference
        ) VALUES (?, ?, ?, 'settlewell', 'onboarding-v1', '4200.00', 'EUR', ?, ?, ?)
        """,
        (airwallex_id, f"stl-{invoice.lower()}-1", obligation.lastrowid, status, dispatch, invoice),
    )
    conn.execute(
        "UPDATE obligation SET live_transfer_id = ? WHERE id = ?",
        (transfer.lastrowid, obligation.lastrowid),
    )
    append_status_snapshot(
        conn,
        transfer_record_id=int(transfer.lastrowid),
        status=status,
        failure_code=None,
        failure_message=None,
        source="poll",
        seen_at=SEEN,
    )
    return int(obligation.lastrowid)


def _beat(
    conn,
    obligation_id: int,
    *,
    claim: str,
    state: str,
    status: str,
    evidence: str,
    action: str,
    duplicate: bool = False,
) -> None:
    incident = conn.execute(
        "INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, ?, ?)",
        (obligation_id, claim, state),
    )
    incident_id = int(incident.lastrowid)
    decision = decide(
        status,
        claim="duplicate" if duplicate else "where",
        duplicate_status=status if duplicate else None,
    )
    digest = hashlib.sha256(evidence.encode()).hexdigest()
    conn.execute(
        """
        INSERT INTO decision (incident_id, rule_id, inputs_hash, proposed_action, policy_verdict)
        VALUES (?, ?, ?, ?, ?)
        """,
        (incident_id, decision.rule_id, digest, decision.action.value, decision.action.value),
    )
    append_audit(conn, incident_id=incident_id, kind="evidence", detail=evidence, created_at=SEEN)
    append_audit(conn, incident_id=incident_id, kind="opened", detail=claim, created_at=SEEN)
    propose(
        conn,
        incident_id,
        ApprovalTerms(
            action=action,
            amount="4200.00",
            currency="EUR",
            beneficiary_id="ben_onboarding",
            details_hash="onboarding-v1",
        ),
        proposer=AP_CLERK,
    )


def _seed_result(conn) -> None:
    """Closing books for the hero: paid once, the lost fee, balances, and the FX quote."""
    conn.execute("UPDATE transfer_record SET fee = '13.47' WHERE airwallex_id = 'awx-late'")
    conn.execute("UPDATE transfer_record SET fee = '0.00' WHERE airwallex_id = 'awx-dup'")
    conn.execute("UPDATE obligation SET status = 'PAID_ONCE' WHERE invoice_ref = 'INV-2291'")
    for currency, available, label in (
        ("EUR", "800.00", "before"),
        ("USD", "100000.00", "before"),
        ("EUR", "5200.00", "after"),
        ("USD", "95160.00", "after"),
    ):
        conn.execute(
            """
            INSERT INTO balance_snapshot (currency, available, taken_at, label)
            VALUES (?, ?, ?, ?)
            """,
            (currency, available, SEEN, label),
        )
    lock_fx_quote(
        conn,
        quote_id="quo_hero",
        pair="USD/EUR",
        rate="1.10",
        amount="4400.00",
        valid_to="2026-11-03T12:15:00+00:00",
    )
    case = conn.execute(
        """
        INSERT INTO eval_case (name, fixture_json, expected_decision)
        VALUES ('hero-result', '{}', 'paid once')
        """
    )
    conn.execute(
        """
        INSERT INTO eval_run (case_id, invariant_results, created_at)
        VALUES (?, 'paid once', ?)
        """,
        (case.lastrowid, SEEN),
    )


def _inbox(conn) -> str:
    rows = conn.execute(
        """
        SELECT incident.id, incident.claim_json, obligation.invoice_ref, decision.proposed_action
        FROM incident
        JOIN obligation ON obligation.id = incident.obligation_id
        JOIN decision ON decision.incident_id = incident.id
        WHERE incident.claim_json LIKE 'console:%'
        ORDER BY incident.id
        """
    ).fetchall()
    items = []
    for row in rows:
        label = _LABELS.get(row["claim_json"], row["claim_json"])
        href = f"/incidents/{row['id']}"
        text = f"{label} · {row['invoice_ref']} · {row['proposed_action']}"
        items.append(f'<li><a href="{escape(href)}">{escape(text)}</a></li>')
    body = (
        '<p><a href="/ledger">Money ledger</a></p>'
        f'<ul class="inbox">{"".join(items)}</ul>'
    )
    return _layout("Inbox", body)


def _ledger(conn) -> str:
    paid = conn.execute(
        "SELECT invoice_ref, status FROM obligation WHERE status = 'PAID_ONCE' ORDER BY id"
    ).fetchall()
    fees = conn.execute(
        """
        SELECT airwallex_id, currency, fee FROM transfer_record
        WHERE fee IS NOT NULL ORDER BY id
        """
    ).fetchall()
    balances = conn.execute(
        "SELECT currency, available, label FROM balance_snapshot ORDER BY id"
    ).fetchall()
    quote = conn.execute(
        "SELECT quote_id, pair, rate, amount, state FROM fx_quote ORDER BY id DESC LIMIT 1"
    ).fetchone()
    runs = conn.execute(
        "SELECT invariant_results FROM eval_run ORDER BY id"
    ).fetchall()
    paid_html = "".join(
        f"<li>{escape(row['invoice_ref'])} {escape(row['status'].replace('_', ' ').lower())}</li>"
        for row in paid
    )
    fee_html = "".join(
        f"<li>{escape(row['airwallex_id'])} · {_money(row['currency'], row['fee'])}</li>"
        for row in fees
    )
    balance_html = "".join(
        "<tr><td>{}</td><td>{}</td><td>{}</td></tr>".format(
            escape(row["label"]),
            escape(row["currency"]),
            _money(row["currency"], row["available"]),
        )
        for row in balances
    )
    if quote is None:
        fx_html = "<p>No quote.</p>"
    else:
        fx_html = (
            f"<p>{escape(quote['pair'])} · rate {escape(quote['rate'])} · "
            f"buy {escape(quote['amount'])} · {escape(quote['quote_id'])} · {escape(quote['state'])}</p>"
        )
    hero_fee = next((row["fee"] for row in fees if row["fee"] == "13.47"), None)
    cost = _money("EUR", hero_fee) if hero_fee else ""
    strip = "".join(f"<li>{escape(row['invariant_results'])}</li>" for row in runs)
    body = f"""
    <p><a href="/">Inbox</a></p>
    <h1>Money ledger</h1>
    <section id="result">
      <h2>Result</h2>
      <ul class="paid-once">{paid_html}</ul>
      <p class="incident-cost">Incident cost {cost}</p>
    </section>
    <section id="balances">
      <h2>Balances</h2>
      <table><tbody>{balance_html}</tbody></table>
    </section>
    <section id="fees"><h2>Fees</h2><ul>{fee_html}</ul></section>
    <section id="fx"><h2>FX</h2>{fx_html}</section>
    <section id="eval-strip"><h2>Eval</h2><ul>{strip}</ul></section>
    """
    return _layout("Money ledger", body)


def _money(currency: str, amount: str) -> str:
    if currency == "EUR":
        shown = f"€{amount}"
    elif currency == "USD":
        shown = f"${amount}"
    else:
        shown = f"{currency} {amount}"
    return escape(shown)


def _incident(conn, raw_id: str) -> str:
    try:
        incident_id = int(raw_id)
    except ValueError:
        return _layout("Not found", "<p>That incident is not in the inbox.</p>")
    incident = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    if incident is None or not str(incident["claim_json"] or "").startswith("console:"):
        return _layout("Not found", "<p>That incident is not in the inbox.</p>")
    decision = conn.execute(
        "SELECT * FROM decision WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
        (incident_id,),
    ).fetchone()
    approval = conn.execute(
        "SELECT * FROM approval WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
        (incident_id,),
    ).fetchone()
    evidence = conn.execute(
        "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'evidence'",
        (incident_id,),
    ).fetchall()
    label = _LABELS.get(incident["claim_json"], incident["claim_json"])
    steps = "".join(
        f"<li>{escape(row['at'])} · {escape(row['kind'])} · {escape(str(row['detail']))}</li>"
        for row in timeline(conn, incident_id)
    )
    evidence_html = "".join(f"<li>{escape(row['detail'])}</li>" for row in evidence)
    used = approval["used_at"] if approval and approval["used_at"] else "not used"
    proposer = approval["proposer"] if approval else ""
    action = approval["action"] if approval else ""
    audits = conn.execute(
        "SELECT kind, detail, created_at FROM audit_event WHERE incident_id = ? ORDER BY id",
        (incident_id,),
    ).fetchall()
    audit_html = "".join(
        f"<li>{escape(row['created_at'])} · {escape(row['kind'])} · {escape(row['detail'])}</li>"
        for row in audits
    )
    body = f"""
    <p><a href="/">Inbox</a></p>
    <h1>{escape(label)}</h1>
    <section id="timeline"><h2>Timeline</h2><ul>{steps}</ul></section>
    <section id="decision"><h2>Decision</h2>
      <p class="rule-id">{escape(decision['rule_id'])}</p>
      <ul class="evidence">{evidence_html}</ul>
    </section>
    <section id="approval"><h2>Approval</h2>
      <p class="action">{escape(action)}</p>
      <p class="proposer">{escape(proposer)}</p>
      <p class="used">{escape(used)}</p>
    </section>
    <section id="audit"><h2>Audit</h2><ul>{audit_html}</ul></section>
    """
    return _layout(label, body)


def _layout(title: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{escape(title)} · Settlewell</title>
<style>
body {{ font-family: Georgia, serif; margin: 2rem auto; max-width: 40rem; color: #1c1917; }}
a {{ color: #9a3412; }}
section {{ border-top: 1px solid #e7e5e4; margin-top: 1.5rem; padding-top: 0.5rem; }}
table {{ border-collapse: collapse; width: 100%; }}
td {{ padding: 0.2rem 0.75rem 0.2rem 0; font-variant-numeric: tabular-nums; }}
.rule-id, .incident-cost {{ font-family: ui-monospace, monospace; }}
</style>
</head>
<body>
{body}
</body>
</html>
"""
