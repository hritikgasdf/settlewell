"""Supplier replies stay here until a person approves them. Nothing is sent."""

from __future__ import annotations


def draft_reply(conn, *, incident_id: int, approval_id: int, body: str, created_at: str) -> int:
    cursor = conn.execute(
        """
        INSERT INTO outbox_message (incident_id, approval_id, body, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (incident_id, approval_id, body, created_at),
    )
    return int(cursor.lastrowid)


def outbox_rows(conn) -> list[dict]:
    rows = conn.execute(
        """
        SELECT outbox_message.body, approval.used_at
        FROM outbox_message
        JOIN approval ON approval.id = outbox_message.approval_id
        ORDER BY outbox_message.id
        """
    ).fetchall()
    return [
        {"body": row["body"], "state": "approved" if row["used_at"] else "waiting"}
        for row in rows
    ]
