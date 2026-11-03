"""Reset the demo ledger and move sandbox transfers off SENT. No secrets are printed."""

from __future__ import annotations

import urllib.parse

from backend.ledger import MODEL_TABLES, connect, migrate, seed_hero

DEMO_HOST = "api-demo.airwallex.com"


class ResetError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def demo_base(base_url: str) -> bool:
    parsed = urllib.parse.urlparse(base_url)
    return parsed.scheme == "https" and parsed.hostname == DEMO_HOST


def reset_demo(conn, *, base_url: str, driver=None) -> int:
    """Wipe the local ledger, reseed the hero, and release sandbox SENT transfers."""
    if not demo_base(base_url):
        raise ResetError("base")
    if driver is not None:
        for transfer_id in sent_sandbox_ids(conn):
            driver.release_sent(transfer_id)
    clear_ledger(conn)
    return seed_hero(conn)


def clear_ledger(conn) -> None:
    """Delete every ledger row. Table names come from the model, not from input."""
    migrate(conn)
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in sorted(MODEL_TABLES):
        conn.execute(f"DELETE FROM {table}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()


def sent_sandbox_ids(conn) -> list[str]:
    rows = conn.execute(
        """
        SELECT airwallex_id FROM transfer_record
        WHERE status = 'SENT' AND airwallex_id IS NOT NULL AND airwallex_id NOT LIKE 'synthetic%'
        ORDER BY id
        """
    ).fetchall()
    return [row["airwallex_id"] for row in rows]


def open_ledger(path: str):
    from pathlib import Path

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    return connect(target)
