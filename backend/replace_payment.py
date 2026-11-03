"""One replacement per attempt. The request id is shared by concurrent callers."""

from __future__ import annotations

from decimal import Decimal

from backend.airwallex.client import AirwallexError


class ReplaceError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def replacement_request_id(invoice_ref: str, attempt: int) -> str:
    slug = "".join(character for character in invoice_ref.lower() if character.isalnum())
    request_id = f"stl-{slug}-{attempt}"
    if not 10 <= len(request_id) <= 50:
        raise ReplaceError("request_id")
    return request_id


def replace_payment(
    conn,
    client,
    *,
    obligation_id: int,
    attempt: int,
    beneficiary_id: str,
    details_hash: str,
    transfer_method: str,
    reason: str,
) -> str:
    """Create at most one transfer for this attempt. Amount and currency come from the obligation."""
    obligation = conn.execute("SELECT * FROM obligation WHERE id = ?", (obligation_id,)).fetchone()
    if obligation is None:
        raise ReplaceError("missing")
    request_id = replacement_request_id(obligation["invoice_ref"], attempt)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = _by_request_id(conn, request_id)
        if existing is not None:
            conn.commit()
            return str(existing["airwallex_id"])
        remote = _create_or_find(
            client,
            obligation,
            request_id=request_id,
            beneficiary_id=beneficiary_id,
            transfer_method=transfer_method,
            reason=reason,
        )
        _store(conn, obligation, remote, request_id=request_id, details_hash=details_hash)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return str(remote.id)


def _create_or_find(client, obligation, *, request_id, beneficiary_id, transfer_method, reason):
    try:
        return client.create_transfer(
            transfer_amount=Decimal(obligation["amount"]),
            transfer_currency=obligation["currency"],
            source_currency=obligation["currency"],
            beneficiary_id=beneficiary_id,
            transfer_method=transfer_method,
            reference=obligation["invoice_ref"],
            reason=reason,
            request_id=request_id,
            metadata={"obligation_id": obligation["invoice_ref"]},
        )
    except AirwallexError as error:
        if error.code != "duplicate_request_id":
            raise
        return _lookup(client, request_id)
    except TimeoutError:
        return _lookup(client, request_id)


def _lookup(client, request_id: str):
    page = client.list_transfers(request_id=request_id)
    if len(page.items) != 1:
        raise ReplaceError("unknown")
    return page.items[0]


def _by_request_id(conn, request_id: str):
    return conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE request_id = ?",
        (request_id,),
    ).fetchone()


def _store(conn, obligation, remote, *, request_id, details_hash) -> None:
    if _by_request_id(conn, request_id) is not None:
        return
    conn.execute(
        """
        INSERT INTO transfer_record (
            airwallex_id, request_id, obligation_id, origin, details_hash,
            amount, currency, status, reference
        ) VALUES (?, ?, ?, 'settlewell', ?, ?, ?, ?, ?)
        """,
        (
            remote.id,
            request_id,
            obligation["id"],
            details_hash,
            obligation["amount"],
            obligation["currency"],
            remote.status,
            obligation["invoice_ref"],
        ),
    )
