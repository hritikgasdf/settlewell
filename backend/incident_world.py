"""One books-plus-remote fixture family for the P1-7 incident scenarios. Offline only.

Every payment goes claim -> pinned match -> decision -> intent -> approval -> execute, then the
shared reconciler reads the remote. Nothing here posts money except through execute().
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from backend.approvals import AP_CLERK, FINANCE_APPROVER
from backend.claims import ParseResult, mock_complete, parse_email
from backend.execute import ExecuteError, approve_intent, execute, open_intent
from backend.incident_remote import StatefulRemote
from backend.ledger import connect, seed_hero
from backend.policy import Decision, code_from_reason, decide
from backend.reconcile import poll_incident
from backend.replace_payment import ReplaceError

SEEN = "2026-11-03T12:00:00+00:00"
EXPIRES = "2026-11-03T13:00:00+00:00"
AMOUNT = Decimal("4200.00")
NEW_FEE = Decimal("13.47")
ORIGINAL = "awx-hero"


@dataclass
class World:
    conn: object
    remote: StatefulRemote
    incident_id: int
    obligation_id: int
    supplier_id: int
    path: str = ":memory:"
    checks: list[tuple[str, bool]] = field(default_factory=list)

    def check(self, label: str, condition: bool) -> None:
        self.checks.append((label, bool(condition)))

    def claim(self, text: str) -> ParseResult:
        """Offline model, pinned supplier. The match must pick this obligation."""
        parsed = parse_email(text, mock_complete, self.conn, supplier_id=self.supplier_id)
        self.check("claim matched pinned obligation", not parsed.escalated and parsed.obligation_id == self.obligation_id)
        return parsed

    def decision(self) -> Decision:
        remote = self.remote.get_transfer(ORIGINAL)
        return decide(remote.status, failure_code=code_from_reason(remote.failure_reason), claim="where")

    def propose(self, action: str, code: str | None, beneficiary_id: str, details_hash: str, *,
                approver: str | None = FINANCE_APPROVER, conn=None, amount: Decimal = AMOUNT):
        conn = conn or self.conn
        ref = open_intent(
            conn, kind="replacement", incident_id=self.incident_id, obligation_id=self.obligation_id,
            action=action, amount=str(amount), currency="EUR", beneficiary_id=beneficiary_id,
            details_hash=details_hash, proposer=AP_CLERK, failure_code=code, expires_at=EXPIRES, created_at=SEEN,
        )
        if approver:
            approve_intent(conn, ref.id, approver=approver, as_of=SEEN)
        return ref

    def run(self, intent_id: int, *, conn=None):
        """execute() once. Returns (result, error). Any failure is data, never swallowed silently."""
        try:
            return execute(conn or self.conn, self.remote, intent_id, as_of=SEEN), None
        except (ExecuteError, ReplaceError) as error:
            return None, error.code
        except Exception as error:  # noqa: BLE001 - e.g. a lost response after the bank accepted
            return None, type(error).__name__

    def transfer_record_id(self) -> int:
        return int(self.conn.execute(
            "SELECT live_transfer_id FROM obligation WHERE id = ?", (self.obligation_id,)).fetchone()[0])

    def outreach(self, action: str, body: str, *, approver: str | None = FINANCE_APPROVER):
        """A non-money action: propose, approve, then execute(). Returns (ref, result, error)."""
        from backend.incident_outreach import propose_outreach

        ref = propose_outreach(
            self.conn, incident_id=self.incident_id, obligation_id=self.obligation_id,
            supplier_id=self.supplier_id, transfer_record_id=self.transfer_record_id(), action=action,
            body=body, currency="EUR", proposer=AP_CLERK, expires_at=EXPIRES, created_at=SEEN,
        )
        if approver:
            approve_intent(self.conn, ref.id, approver=approver, as_of=SEEN)
        result, error = self.run(ref.id)
        return ref, result, error

    def escalate(self, rule_id: str):
        """A held claim goes to a person through execute(): outbox + audit, no transfer."""
        body = f"[ESCALATION {rule_id}] Held for a person. Nothing was taken from the email."
        return self.outreach("ESCALATE", body)

    def outbox_bodies(self) -> list[str]:
        return [r["body"] for r in self.conn.execute("SELECT body FROM outbox_message ORDER BY id")]

    def audit_kinds(self) -> list[str]:
        return [r["kind"] for r in self.conn.execute("SELECT kind FROM audit_event ORDER BY id")]

    def poll(self, *, conn=None):
        return poll_incident(conn or self.conn, self.remote, self.incident_id, seen_at=SEEN)

    def intent_state(self, intent_id: int) -> str:
        return self.conn.execute("SELECT state FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()["state"]

    def money_out(self) -> int:
        """Outbound payments our code POSTed (colleague transfers are not ours)."""
        return len(self.remote.posts)


def make_world(path: str = ":memory:", *, status: str = "SENT", fee: str = "13.47", eur: str = "100000.00") -> World:
    conn = connect(path)
    incident_id = seed_hero(conn, airwallex_id=ORIGINAL)
    row = conn.execute(
        "SELECT o.id AS oid, o.supplier_id AS sid FROM incident i JOIN obligation o ON o.id = i.obligation_id WHERE i.id = ?",
        (incident_id,),
    ).fetchone()
    remote = StatefulRemote(fee=fee, eur=eur)
    remote.add_transfer(ORIGINAL, status=status, amount=str(AMOUNT), reference="INV-2291",
                        beneficiary_id="ben_onboard", request_id="hero-inv2291")
    return World(conn, remote, incident_id, int(row["oid"]), int(row["sid"]), path)


def bounce(world: World, code: str = "90802: Bank rejected") -> None:
    """The bank returns the original. Reconcile records it from the remote, not from the books."""
    world.remote.bounce(ORIGINAL, code)
    world.poll()


NOW_MS = int(datetime.fromisoformat(SEEN).timestamp() * 1000)  # the same clock as SEEN
SECRET = "incident-runner-secret"


def deliver(world: World, event_id: str, offset_ms: int, status: str, name: str, **data) -> str:
    """One signed synthetic webhook through receive_webhook. Occurrence time is NOW_MS + offset."""
    import hashlib
    import hmac
    import json
    from datetime import datetime

    from backend.webhooks import receive_webhook

    payload = {"id": ORIGINAL, "status": status, **data}
    created = datetime.fromtimestamp((NOW_MS + offset_ms) / 1000, UTC).isoformat()
    raw = json.dumps({"id": event_id, "name": name, "data": payload, "created_at": created}, separators=(",", ":")).encode()
    stamp = str(NOW_MS + offset_ms)
    signature = hmac.new(SECRET.encode(), stamp.encode() + raw, hashlib.sha256).hexdigest()
    return receive_webhook(
        world.conn, secret=SECRET, raw_body=raw, x_timestamp=stamp, x_signature=signature, now_ms=NOW_MS + 1000
    ).outcome


def settle_all(world: World, *, after_ms: int = 20_000) -> list[str]:
    from backend.webhooks import settle

    return settle(world.conn, world.remote, NOW_MS + after_ms)


def ledger_status(world: World) -> tuple[str, str]:
    row = world.conn.execute(
        "SELECT status, COALESCE(failure_message, '') AS reason FROM transfer_record WHERE airwallex_id = ?",
        (ORIGINAL,),
    ).fetchone()
    obligation = world.conn.execute("SELECT status FROM obligation WHERE id = ?", (world.obligation_id,)).fetchone()
    return f"{row['status']}/{row['reason'][:5]}", obligation["status"]
