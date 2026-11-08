"""Periodic worker. Webhooks are the fast path; this is the backstop and the crash recovery.

Reads and same-intent resumes only: nothing here proposes a new intent, amount or request id."""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime

from backend.airwallex.client import AirwallexError
from backend.execute import ExecuteError, execute
from backend.fx_funding import conversion_request_id
from backend.ledger import append_audit, record_fx_conversion
from backend.reconcile import _LOOKUP_ERRORS, poll_incident
from backend.webhooks import incident_pending, settle

_ERRORS = (*_LOOKUP_ERRORS, ExecuteError, AirwallexError)


def tick(conn, client, now_ms: int) -> dict:
    """One pass: settle due webhooks, resume crashed intents, poll incidents and conversions."""
    seen = datetime.fromtimestamp(now_ms / 1000, UTC).isoformat()
    report = {"settled": [], "resumed": [], "polled": [], "held": [], "conversions": []}
    report["settled"] = settle(conn, client, now_ms)
    for row in conn.execute(
        """SELECT e.id, e.incident_id FROM execution_intent e JOIN approval a ON a.id = e.approval_id
           WHERE (e.state = 'proposed' OR (e.state = 'executed' AND e.remote_id IS NULL))
             AND a.used_at IS NOT NULL ORDER BY e.id"""
    ).fetchall():
        try:  # same intent, same approval; execute re-checks terms, expiry and ownership
            execute(conn, client, row["id"], as_of=seen)
            report["resumed"].append(row["id"])
        except _ERRORS as error:
            conn.rollback()
            append_audit(conn, incident_id=row["incident_id"], kind="worker_resume_held",
                         detail=f"{row['id']}:{getattr(error, 'code', type(error).__name__)}", created_at=seen)
            conn.commit()
            report["held"].append(row["id"])
    for row in conn.execute("SELECT id FROM incident ORDER BY id").fetchall():
        if incident_pending(conn, row["id"]):
            continue  # a settle window is open; only settle() may decide
        try:
            poll_incident(conn, client, row["id"], seen_at=seen)
            report["polled"].append(row["id"])
        except _ERRORS:
            conn.rollback()
            report["held"].append(row["id"])
    for quote in conn.execute(
        "SELECT quote_id FROM fx_quote WHERE state = 'CONSUMED' AND consumed_by = ''"
    ).fetchall():
        try:  # look up by stored request id; never create or release on an unknown answer
            found = client.find_conversion(conversion_request_id(quote["quote_id"]))
        except _ERRORS:
            conn.rollback()
            continue
        if found is not None and found.quote_id in (None, quote["quote_id"]):
            record_fx_conversion(conn, quote["quote_id"], found.conversion_id)
            report["conversions"].append(quote["quote_id"])
    conn.commit()
    return report


class Worker:
    """Background thread with a clean stop. Tests inject the clock and call tick() directly."""

    def __init__(self, open_conn, make_client, *, interval: float = 5.0, clock_ms=None) -> None:
        self._open, self._make = open_conn, make_client
        self.interval = interval
        self._clock = clock_ms or (lambda: int(time.time() * 1000))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> dict:
        conn = self._open()
        try:
            return tick(conn, self._make(conn), self._clock())
        finally:
            conn.close()

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="settlewell-worker", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as error:  # noqa: BLE001 - one bad tick must not kill the loop; state stays held
                logging.getLogger(__name__).warning("worker tick failed: %s", type(error).__name__)
            self._stop.wait(self.interval)
