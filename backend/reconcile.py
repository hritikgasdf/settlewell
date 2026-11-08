"""Build an incident timeline from transfer reads. No webhooks."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from backend.airwallex.client import AirwallexError
from backend.airwallex.types import Transfer
from backend.ledger import (
    append_audit,
    append_status_snapshot,
    hero_bundle,
    insert_external_transfer,
    remember_failure_reason,
    set_obligation_status,
    set_transfer_status,
)
from backend.outcomes import (
    NOT_PAID_STATUSES,
    HistoryError,
    Outcome,
    decide_set,
    full_refund,
    outcome_kind,
    refund_currency,
    refund_principal,
    reversals_for,
)
from backend.paid_once import find_matches
from backend.replace_payment import ReplaceError, _lookup_optional, _require_owner, _store

_LOOKUP_ERRORS = (HistoryError, AirwallexError, TimeoutError, OSError, ReplaceError)


def poll_incident(conn, client, incident_id: int, *, seen_at: str, settled: bool = False,
                  report: dict | None = None) -> list[dict]:
    """Recover our own attempts by stored request id, read every payment, then decide once.

    Reads only: nothing here creates, retries or posts a payment. While a webhook settle
    window is pending, only webhooks.settle (settled=True) may decide."""
    from backend.webhooks import incident_pending

    if not settled and incident_pending(conn, incident_id):
        _audit_once(conn, incident_id, "poll_held_pending_webhook", str(incident_id), seen_at)
        conn.commit()
        return timeline(conn, incident_id)
    obligation = hero_bundle(conn, incident_id)["obligation"]
    recovered = _recover_attempts(conn, client, incident_id, obligation, seen_at)
    bundle = hero_bundle(conn, incident_id)
    obligation, rows = bundle["obligation"], bundle["transfers"]
    original_id = _original_id(conn, incident_id, obligation, rows)
    if original_id is None:  # ambiguous role evidence: hold, never guess
        _audit_once(conn, incident_id, "original_ambiguous", str(obligation["id"]), seen_at)
    versions = {row["beneficiary_id"] for row in bundle["versions"] if row["beneficiary_id"]}
    unresolved = [
        row for row in rows if not row["airwallex_id"] and (row["status"] or "") != "REFUSED"
    ]
    incomplete = not recovered or bool(unresolved)
    outcomes: list[Outcome] = []
    for row in rows:
        if not row["airwallex_id"]:
            continue
        outcome, readable = _read_outcome(
            conn, client, incident_id, obligation, row, versions, row["id"] == original_id, seen_at
        )
        incomplete = incomplete or not readable
        outcomes.append(outcome)
    known_ids = {row["airwallex_id"] for row in rows if row["airwallex_id"]}
    known_requests = {row["request_id"] for row in rows if row["request_id"]}
    try:
        matches = find_matches(conn, client, obligation["id"])
    except _LOOKUP_ERRORS:
        _audit_once(conn, incident_id, "discovery_incomplete", str(obligation["id"]), seen_at)
        incomplete = True
        matches = []
    for kind, remote in matches:
        if remote.id in known_ids or (remote.request_id and remote.request_id in known_requests):
            continue  # a known id is refreshed above; our own request id is never external
        known_ids.add(remote.id)
        _tag_external(conn, obligation, remote, incident_id, seen_at)
        outcomes.append(Outcome(remote.id, "replacement", remote.status, credited=kind == "duplicate"))
    if unresolved:
        _set_status(conn, obligation["id"], "IN_FLIGHT")
    if report is not None:
        report["incomplete"] = incomplete or original_id is None
    _decide(conn, incident_id, obligation, original_id, outcomes, incomplete, bool(unresolved), seen_at)
    conn.commit()
    return timeline(conn, incident_id)


def _original_id(conn, incident_id: int, obligation, rows) -> int | None:
    """Durable original identity from evidence, never from row order.

    Order: a recorded original_disposition, then the live pointer, then the one unowned settlewell row.
    Our own replacement request ids (from executed intents) and colleague payments (colleague_tracked /
    colleague_repaid) are never the original, even when a legacy close moved the pointer onto them."""
    owned = {
        row["request_id"]
        for row in conn.execute(
            "SELECT request_id FROM execution_intent WHERE kind = 'replacement' AND obligation_id = ?",
            (obligation["id"],),
        )
        if row["request_id"]
    }
    colleagues = {
        row["detail"]
        for row in conn.execute(
            "SELECT detail FROM audit_event WHERE incident_id = ? AND kind IN ('colleague_tracked', 'colleague_repaid')",
            (incident_id,),
        )
    }
    usable = [
        row
        for row in rows
        if row["airwallex_id"] and row["request_id"] not in owned and row["airwallex_id"] not in colleagues
    ]
    recorded = {
        row["detail"].split(":", 1)[0]
        for row in conn.execute(
            "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'original_disposition'",
            (incident_id,),
        )
    }
    if recorded:
        match = [row for row in usable if row["airwallex_id"] in recorded]
        return match[0]["id"] if len(recorded) == 1 and len(match) == 1 else None
    if obligation["live_transfer_id"] is not None:
        pointed = [row for row in usable if row["id"] == obligation["live_transfer_id"]]
        if pointed:
            return pointed[0]["id"]
    # pointer missing or on a replacement/colleague: only one unowned settlewell row may stand in
    candidates = [row for row in usable if row["origin"] == "settlewell"]
    return candidates[0]["id"] if len(candidates) == 1 else None


def timeline(conn, incident_id: int) -> list[dict]:
    incident = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (incident_id,)).fetchone()
    snapshots = conn.execute(
        """
        SELECT 'status' AS kind, snapshot.seen_at AS at, snapshot.status AS detail
        FROM status_snapshot AS snapshot
        JOIN transfer_record ON transfer_record.id = snapshot.transfer_record_id
        WHERE transfer_record.obligation_id = ?
        """,
        (incident["obligation_id"],),
    ).fetchall()
    audits = conn.execute(
        "SELECT kind, created_at AS at, detail FROM audit_event WHERE incident_id = ?",
        (incident_id,),
    ).fetchall()
    rows = [dict(row) for row in snapshots] + [dict(row) for row in audits]
    return sorted(rows, key=lambda row: (row["at"], row["kind"]))


def _record_status(conn, record, remote: Transfer, seen_at: str) -> None:
    previous = conn.execute(
        """
        SELECT status, failure_message FROM status_snapshot
        WHERE transfer_record_id = ?
        ORDER BY id DESC LIMIT 1
        """,
        (record["id"],),
    ).fetchone()
    reason = remote.failure_reason or None
    changed = previous is None or previous["status"] != remote.status
    if reason and (previous is None or (previous["failure_message"] or "") != reason):
        changed = True
    set_transfer_status(
        conn,
        record["id"],
        status=remote.status,
        dispatch_date=remote.dispatch_date,
    )
    if reason:
        remember_failure_reason(conn, record["id"], reason)
    if not changed:
        return
    stored = conn.execute(
        "SELECT failure_code, failure_message FROM transfer_record WHERE id = ?",
        (record["id"],),
    ).fetchone()
    append_status_snapshot(
        conn,
        transfer_record_id=record["id"],
        status=remote.status,
        failure_code=stored["failure_code"],
        failure_message=stored["failure_message"],
        source="poll",
        seen_at=seen_at,
    )


def _recover_attempts(conn, client, incident_id: int, obligation, seen_at: str) -> bool:
    """Bind each unresolved durable attempt by its STORED request id. Read-only lookup, never a create.

    A missing, failed or mismatched lookup leaves the attempt and obligation IN_FLIGHT."""
    pending = conn.execute(
        """
        SELECT * FROM transfer_record
        WHERE obligation_id = ? AND origin = 'settlewell' AND airwallex_id IS NULL
          AND request_id IS NOT NULL AND COALESCE(status, '') != 'REFUSED'
        ORDER BY id
        """,
        (obligation["id"],),
    ).fetchall()
    clear = True
    for row in pending:
        if not _recover_one(conn, client, incident_id, obligation, row, seen_at):
            clear = False
            _audit_once(conn, incident_id, "attempt_unresolved", row["request_id"], seen_at)
            _set_status(conn, obligation["id"], "IN_FLIGHT")
        conn.commit()
    return clear


def _recover_one(conn, client, incident_id: int, obligation, row, seen_at: str) -> bool:
    from backend.execute import remember_remote

    request_id = row["request_id"]
    intent = conn.execute(
        """
        SELECT * FROM execution_intent
        WHERE request_id = ? AND kind = 'replacement' AND state = 'executed' AND obligation_id = ?
        """,
        (request_id, obligation["id"]),
    ).fetchone()
    if intent is None:
        return False
    # Ownership is the intent's own incident and grant, never the polling caller's incident.
    approval = conn.execute("SELECT incident_id FROM approval WHERE id = ?", (intent["approval_id"],)).fetchone()
    owner = conn.execute("SELECT obligation_id FROM incident WHERE id = ?", (intent["incident_id"],)).fetchone()
    if (
        approval is None
        or intent["incident_id"] is None
        or approval["incident_id"] != intent["incident_id"]
        or owner is None
        or owner["obligation_id"] != obligation["id"]
    ):
        return _mismatch(conn, incident_id, request_id, "approval", seen_at)
    try:
        remote = _lookup_optional(client, request_id)
    except (ReplaceError, OSError):
        return False
    if remote is None:
        return False
    # Terms come from the immutable approved attempt and intent, not the local obligation amount.
    amount, currency = row["amount"] or intent["amount"], row["currency"] or intent["currency"]
    details_hash = row["details_hash"] or intent["details_hash"]
    same_terms = (
        remote.reference == obligation["invoice_ref"]
        and remote.transfer_currency == currency
        and remote.transfer_amount is not None
        and remote.transfer_amount == Decimal(amount)
        and remote.beneficiary_id == intent["beneficiary_id"]
        and remote.request_id == request_id
    )
    if not same_terms:
        return _mismatch(conn, incident_id, request_id, "terms", seen_at)
    try:
        _require_owner(
            conn,
            intent_id=intent["id"],
            approval_id=intent["approval_id"],
            obligation_id=obligation["id"],
            as_of=seen_at,
            amount=amount,
            currency=currency,
            beneficiary_id=intent["beneficiary_id"],
            details_hash=details_hash,
            quote_id=intent["quote_id"],
            request_id=request_id,
            live=False,
        )
    except ReplaceError as error:
        return _mismatch(conn, incident_id, request_id, error.code, seen_at)
    _store(conn, obligation, remote, request_id=request_id, details_hash=details_hash, amount=amount, currency=currency)
    remember_remote(conn, intent["id"], str(remote.id), remote.status)
    conn.commit()  # the accepted id is durable before the funding hooks run
    from backend.funding import record_recovered

    try:
        record_recovered(conn, client, int(intent["id"]), remote, seen_at)
    except ReplaceError:
        return _mismatch(conn, incident_id, request_id, "funding", seen_at)
    return True


def _mismatch(conn, incident_id: int, request_id: str, why: str, seen_at: str) -> bool:
    _audit_once(conn, incident_id, "recovery_mismatch", request_id, seen_at)
    return False


def _read_outcome(conn, client, incident_id, obligation, row, versions, is_original, seen_at):
    """Refresh one known payment by id. Returns (Outcome, readable). Unreadable falls back to stored facts."""
    transfer_id = row["airwallex_id"]
    stored = row["status"]
    remote = reversals = refund = None
    readable = True
    try:
        remote = client.get_transfer(transfer_id)
        reversals = reversals_for(client, transfer_id, refund_currency(remote, row["currency"]))
        refund = full_refund(remote, reversals)
    except _LOOKUP_ERRORS:
        readable = False
        _audit_once(conn, incident_id, "outcome_lookup_failed", transfer_id, seen_at)
    for reversal in reversals or ():
        _note_reversal(conn, incident_id, reversal, seen_at)
    principal = refund_principal(remote) if remote is not None else None
    if refund is not None:
        detail = _refund_detail(transfer_id, principal)
        if _refund_state(conn, incident_id, transfer_id, detail) != "refund_verified":
            append_audit(conn, incident_id=incident_id, kind="refund_verified", detail=detail, created_at=seen_at)
        refunded = True
    elif reversals:
        refunded = False  # fresh data contradicts: partial, wrong currency, pending or ambiguous
        if principal is not None:
            detail = _refund_detail(transfer_id, principal)
            if _refund_state(conn, incident_id, transfer_id, detail) == "refund_verified":
                append_audit(conn, incident_id=incident_id, kind="refund_withdrawn", detail=detail, created_at=seen_at)
    else:  # no fresh evidence (transport failure or not in history): keep only a bound earlier verification
        refunded = _refund_remembered(conn, incident_id, transfer_id, remote, principal)
    seen, _amount = _remembered_reversal(conn, incident_id, transfer_id)
    status = stored
    if remote is not None:
        if stored in NOT_PAID_STATUSES and remote.status not in NOT_PAID_STATUSES:
            # A terminal failure is final: no stale live status or PAID may overwrite it.
            _audit_once(conn, incident_id, "stale_status_ignored", f"{transfer_id}:{remote.status}", seen_at)
        else:
            _record_status(conn, row, remote, seen_at)
            status = remote.status
    if row["origin"] == "settlewell":
        credited = True
    elif remote is None:  # unreadable: keep only a credit that was verified earlier
        credited = _audit_exists(conn, incident_id, "external_credited", transfer_id)
    else:
        credited = bool(
            remote.beneficiary_id in versions
            and remote.reference == obligation["invoice_ref"]
            and remote.transfer_currency == obligation["currency"]
            and remote.transfer_amount == Decimal(obligation["amount"])
        )
        if credited:
            _audit_once(conn, incident_id, "external_credited", transfer_id, seen_at)
    if row["origin"] == "external" and status in NOT_PAID_STATUSES:
        tracked = conn.execute(
            "SELECT 1 FROM audit_event WHERE incident_id = ? AND kind = 'colleague_tracked' AND detail = ?",
            (incident_id, transfer_id),
        ).fetchone()
        if tracked:
            _audit_once(conn, incident_id, "colleague_reopened", f"{transfer_id}:{status}", seen_at)
    outcome = Outcome(
        transfer_id,
        "original" if is_original else "replacement",
        status,
        reversed=seen,
        returned=is_original and status == "CANCELLED" and refunded,
        credited=credited,
        fresh=remote is not None,
    )
    return outcome, readable


def _refund_detail(transfer_id: str, principal) -> str:
    amount, currency = principal
    return f"source={transfer_id} amount={format(amount.normalize(), 'f')} currency={currency}"


def _refund_state(conn, incident_id: int, transfer_id: str, detail: str | None = None) -> str | None:
    """The LATEST refund transition (by row id, never by as_of) for this transfer: verified or withdrawn."""
    if detail is None:
        where, arg = "detail LIKE ?", f"source={transfer_id} amount=%"
    else:
        where, arg = "detail = ?", detail
    row = conn.execute(
        "SELECT kind FROM audit_event WHERE incident_id = ? AND kind IN ('refund_verified', 'refund_withdrawn') "
        f"AND {where} ORDER BY id DESC LIMIT 1",
        (incident_id, arg),
    ).fetchone()
    return row["kind"] if row else None


def _refund_remembered(conn, incident_id: int, transfer_id: str, remote, principal) -> bool:
    """An earlier full-refund verification, still bound to this principal and not withdrawn since."""
    if remote is not None:
        if principal is None:
            return False
        return _refund_state(conn, incident_id, transfer_id, _refund_detail(transfer_id, principal)) == "refund_verified"
    return _refund_state(conn, incident_id, transfer_id) == "refund_verified"


def _remembered_reversal(conn, incident_id: int, transfer_id: str) -> tuple[bool, Decimal | None]:
    """A reversal once seen stays known even after it leaves the balance-history window."""
    row = conn.execute(
        "SELECT detail FROM audit_event WHERE incident_id = ? AND kind = 'payout_reversal' AND detail LIKE ?",
        (incident_id, f"PAYOUT_REVERSAL source={transfer_id} amount=%"),
    ).fetchone()
    if row is None:
        return False, None
    try:
        return True, Decimal(row["detail"].rsplit("amount=", 1)[1])
    except (InvalidOperation, IndexError):
        return True, None


def _tag_external(conn, obligation, remote, incident_id: int, seen_at: str) -> None:
    local_id = insert_external_transfer(
        conn,
        airwallex_id=remote.id,
        request_id=remote.request_id,
        obligation_id=obligation["id"],
        amount=format(remote.transfer_amount, "f") if remote.transfer_amount is not None else obligation["amount"],
        currency=remote.transfer_currency or obligation["currency"],
        status=remote.status,
        reference=remote.reference or obligation["invoice_ref"],
    )
    if local_id is None:
        return
    append_status_snapshot(
        conn,
        transfer_record_id=local_id,
        status=remote.status,
        failure_code=None,
        failure_message=None,
        source="poll",
        seen_at=seen_at,
    )
    _audit_once(conn, incident_id, "external_lookalike", f"external airwallex_id={remote.id}", seen_at)


def _fee_escalated(conn, obligation_id: int) -> bool:
    """A payout for this obligation was charged over its fee budget: a person must review, no automatic clear."""
    for row in conn.execute(
        "SELECT fee_budget, fee_currency, actual_fee, actual_fee_currency FROM payout_funding "
        "WHERE obligation_id = ? AND actual_fee IS NOT NULL",
        (obligation_id,),
    ):
        if row["actual_fee_currency"] != row["fee_currency"] or Decimal(row["actual_fee"]) > Decimal(row["fee_budget"]):
            return True
    return False


def _set_status(conn, obligation_id: int, status: str) -> None:
    if status != "ESCALATED" and _fee_escalated(conn, obligation_id):
        status = "ESCALATED"
    set_obligation_status(conn, obligation_id, status)


def _decide(conn, incident_id, obligation, original_id, outcomes, incomplete, pending, seen_at) -> None:
    """Close or reopen ONCE from the complete set. Audits are idempotent; state moves are transitions."""
    current = conn.execute("SELECT status, live_transfer_id FROM obligation WHERE id = ?", (obligation["id"],)).fetchone()
    state = conn.execute("SELECT state FROM incident WHERE id = ?", (incident_id,)).fetchone()["state"]
    was_closed = state == "CLOSED" or current["status"] == "PAID_ONCE"
    verdict = decide_set(outcomes, incomplete=incomplete, was_closed=was_closed, pending=pending)
    for item in outcomes if original_id is not None else ():
        if item.role == "original":
            label = "returned" if item.returned else item.status
            _audit_once(conn, incident_id, "original_disposition", f"{item.airwallex_id}:{label}", seen_at)
        else:
            _audit_once(conn, incident_id, "replacement_disposition", f"{item.airwallex_id}:{item.status}", seen_at)
    escalated = _fee_escalated(conn, obligation["id"])
    if escalated:
        set_obligation_status(conn, obligation["id"], "ESCALATED")
    if verdict == "close":
        if escalated:
            return  # fee over budget: never closed automatically; outcome audits above are still recorded
        set_obligation_status(conn, obligation["id"], "PAID_ONCE")
        if state == "OPEN":
            conn.execute("UPDATE incident SET state = 'CLOSED' WHERE id = ?", (incident_id,))
            _audit_once(conn, incident_id, "incident_closed", f"{incident_id}:{_serial(conn, incident_id)}", seen_at)
        return
    if verdict == "conflict":
        _audit_once(conn, incident_id, "outcome_conflict", ",".join(sorted(i.airwallex_id for i in outcomes)), seen_at)
    if verdict in ("reopen", "conflict"):
        if current["status"] == "PAID_ONCE":
            _set_status(conn, obligation["id"], "IN_FLIGHT")
        if state == "CLOSED":
            conn.execute("UPDATE incident SET state = 'OPEN' WHERE id = ?", (incident_id,))
            _audit_once(conn, incident_id, "incident_reopened", f"{incident_id}:{_serial(conn, incident_id)}", seen_at)
        no_paid = all(outcome_kind(item) != "paid" for item in outcomes)
        if verdict == "reopen" and no_paid and original_id is not None and current["live_transfer_id"] != original_id:
            conn.execute("UPDATE obligation SET live_transfer_id = ? WHERE id = ?", (original_id, obligation["id"]))


def _serial(conn, incident_id: int) -> int:
    """Number of earlier close/reopen events, so each real transition gets its own stable audit."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM audit_event WHERE incident_id = ? AND kind IN ('incident_closed', 'incident_reopened')",
        (incident_id,),
    ).fetchone()["n"]


def _audit_once(conn, incident_id: int, kind: str, detail: str, seen_at: str) -> None:
    if not _audit_exists(conn, incident_id, kind, detail):
        append_audit(conn, incident_id=incident_id, kind=kind, detail=detail, created_at=seen_at)


def _note_reversal(conn, incident_id: int, item, seen_at: str) -> None:
    detail = f"PAYOUT_REVERSAL source={item.source} amount={item.amount}"
    if _audit_exists(conn, incident_id, "payout_reversal", detail):
        return
    append_audit(conn, incident_id=incident_id, kind="payout_reversal", detail=detail, created_at=seen_at)


def _audit_exists(conn, incident_id: int, kind: str, detail: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM audit_event WHERE incident_id = ? AND kind = ? AND detail = ?",
        (incident_id, kind, detail),
    ).fetchone()
    return row is not None
