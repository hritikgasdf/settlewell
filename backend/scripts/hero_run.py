"""Hero run and owned-only reset.

  --mock (default)             offline replay through execute(); beats labelled MOCK
  --sandbox --confirm-sandbox  REAL sandbox calls (exact sandbox URL, sandbox credentials only):
                               original SENT + colleague SCHEDULED duplicate, real Claude claim,
                               STOP_DUPLICATE, simulated bank return, bank change, locked FX quote,
                               conversion, approved replacement, reconcile. Resumable by stage.
  --reset [--sandbox ...]      touch ONLY ids in this run's manifest; verify readback; refuse
                               to remove the DB/manifest while any owned transfer is unresolved

Statuses: PASS, PARTIAL, FAILED(sanitized code), UNVERIFIED (not run / not configured).
The manifest holds sanitized ids and statuses only. The sandbox path is not yet exercised
against the live sandbox (PC pending): treat every sandbox PASS as unverified until that run.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

from backend.api_mode import make_client_factory, resolve_mode
from backend.hero_live import ALLOWED_KEYS, BEAT_OF, ID_RE, Hero, load_manifest, save_manifest  # noqa: F401

MANIFEST_DEFAULT = "var/hero-run-manifest.json"
DB_DEFAULT = "var/hero-sandbox.db"
BEATS = tuple(BEAT_OF.values())
UNRESOLVED = {"SENT", "PROCESSING", "SCHEDULED", "UNKNOWN", ""}


def run_mock() -> dict[str, str]:
    """Offline canned replay. It does NOT drive the live Hero, so only what the replay really
    exercises can PASS; everything else is PARTIAL (not exercised), never a blanket PASS."""
    from backend.mock_replay import play_hero

    replay = play_hero()
    out = {beat: "PARTIAL(MOCK: canned replay; this beat is not exercised by --mock)" for beat in BEATS}
    if replay.created_count == 1 and replay.proposer != replay.approver and replay.second_replacement != "":
        out["approved_replacement"] = "PASS(MOCK replay: one replacement, different approver, second refused)"
    if replay.paid_once:
        out["reconcile_both"] = "PASS(MOCK replay: obligation paid once)"
    return out


def run_sandbox(manifest_path: str, env=None, transport=None, db: str = DB_DEFAULT, complete=None,
                sleep=time.sleep) -> dict[str, str]:
    from backend.sandbox import SandboxDriver

    config = resolve_mode("sandbox", env)
    if config.mode != "SANDBOX":
        return {beat: f"UNVERIFIED({config.reason})" for beat in BEATS}
    if complete is None:  # real Claude parse only: never default_complete(), which can fall back to mock
        from backend.claims import anthropic_complete

        complete = anthropic_complete
    client = make_client_factory(config, env, transport)(None)
    driver = SandboxDriver(client, sleep=sleep, confirm_sandbox=True)
    return Hero(client, driver, manifest_path, db, complete=complete, sleep=sleep).run()


def _bound_db(manifest: dict, manifest_path: str) -> Path | None:
    """The run's own SQLite file, or None. Never a path the manifest merely names.

    Must sit beside the manifest or in ./var, be a regular (non-symlink) .db file, be a real SQLite
    database, and hold only obligations for THIS run's invoice reference."""
    raw, run = manifest.get("db"), str(manifest.get("run") or "")
    if not raw or not re.fullmatch(r"\d{8}", run):
        return None
    path = Path(raw)
    if path.is_symlink() or not path.is_file() or path.suffix != ".db":
        return None
    parent = path.resolve().parent
    if parent not in {Path(manifest_path).resolve().parent, (Path.cwd() / "var").resolve()}:
        return None
    try:
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        try:
            refs = [r[0] for r in conn.execute("SELECT invoice_ref FROM obligation")]
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    return path if all(r == f"INV-{run}" for r in refs) else None


def _ledger_owned_requests(manifest: dict, manifest_path: str) -> set[str]:
    """Request ids of this run's genuine executed replacements, from its BOUND ledger only.

    No bound DB (or any read error) means no extra ownership: the remote then stays foreign."""
    db = _bound_db(manifest, manifest_path)
    if db is None:
        return set()
    try:
        conn = sqlite3.connect(f"file:{db.resolve()}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT e.request_id FROM execution_intent e JOIN obligation o ON o.id = e.obligation_id "
                "WHERE e.kind = 'replacement' AND e.state = 'executed' AND e.remote_id IS NOT NULL "
                "AND e.request_id IS NOT NULL AND o.invoice_ref = ?", (f"INV-{manifest['run']}",)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return set()
    return {r[0] for r in rows}


def _cancel_via_execute(manifest: dict, manifest_path: str, client, transfer_id: str) -> None:
    """Settlewell's own cancellation: a bound single-use approved STOP_DUPLICATE through execute().

    Needs the run's bound ledger holding a transfer_record and incident for this id; otherwise it
    raises and reset fails closed. Never a raw remote cancel."""
    from datetime import UTC, datetime, timedelta

    from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
    from backend.execute import execute, intent_terms, open_intent
    from backend.ledger import connect

    db = _bound_db(manifest, manifest_path)
    if db is None:
        raise RuntimeError("cancel_needs_bound_ledger")
    conn = connect(str(db))
    try:
        row = conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = ?", (transfer_id,)).fetchone()
        inc = conn.execute("SELECT id FROM incident WHERE obligation_id = ? ORDER BY id DESC LIMIT 1",
                           (row["obligation_id"],)).fetchone() if row is not None else None
        if row is None or inc is None:
            raise RuntimeError("cancel_needs_recorded_transfer")
        now = datetime.now(UTC)
        intent = open_intent(
            conn, kind="cancel", incident_id=int(inc["id"]), obligation_id=row["obligation_id"],
            action="STOP_DUPLICATE", amount=str(row["amount"]), currency=str(row["currency"]),
            beneficiary_id=client.get_transfer(transfer_id).beneficiary_id or "",
            details_hash=row["details_hash"] or "", target_id=str(row["id"]), proposer=AP_CLERK,
            expires_at=(now + timedelta(hours=2)).isoformat(), created_at=now.isoformat())
        approval_id = conn.execute("SELECT approval_id FROM execution_intent WHERE id = ?",
                                   (intent.id,)).fetchone()["approval_id"]
        approve(conn, approval_id, intent_terms(conn, intent.id), approver=FINANCE_APPROVER,
                used_at=now.isoformat())
        execute(conn, client, intent.id, as_of=now.isoformat())
    finally:
        conn.close()


def reset_owned(manifest_path: str, client, driver) -> tuple[bool, list[str]]:
    """Release/cancel only transfers owned by this run's request ids; refuse on anything unresolved,
    foreign or unbound. Transfers are found by manifest id AND exact request-id lookup, so a crash
    before an id was saved cannot leave an orphan behind."""
    manifest = load_manifest(manifest_path)
    owned_requests = set((manifest.get("requests") or {}).values()) | _ledger_owned_requests(manifest, manifest_path)
    problems: list[str] = []
    transfer_ids = set(manifest.get("transfers") or {})
    for req in sorted(owned_requests):
        try:
            transfer_ids.update(t.id for t in client.list_transfers(request_id=req).items
                                if t.request_id == req)
        except Exception as error:  # noqa: BLE001 - unknown remote state fails closed
            problems.append(f"{req}:lookup:{type(error).__name__}")
    for transfer_id in sorted(transfer_ids):
        try:
            current = client.get_transfer(transfer_id)
            if getattr(current, "request_id", None) not in owned_requests:
                problems.append(f"{transfer_id}:foreign")  # never act on an id this run did not create
                continue
            if current.status == "SENT":
                driver.release_sent(transfer_id)
            elif current.status == "SCHEDULED":
                _cancel_via_execute(manifest, manifest_path, client, transfer_id)
            current = client.get_transfer(transfer_id)  # readback
            if (current.status or "") in UNRESOLVED:
                problems.append(f"{transfer_id}:{current.status}")
        except Exception as error:  # noqa: BLE001 - unknown remote state fails closed
            problems.append(f"{transfer_id}:{type(error).__name__}")
    if problems:
        return False, problems
    db = _bound_db(manifest, manifest_path)
    if manifest.get("db") and db is None and Path(manifest["db"]).exists():
        return False, ["db:unbound_not_deleted"]  # keep manifest; a human removes it
    if db is not None:
        db.unlink()  # the run's own dedicated DB only
    Path(manifest_path).unlink(missing_ok=True)
    return True, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--mock", action="store_true")
    group.add_argument("--sandbox", action="store_true")
    parser.add_argument("--confirm-sandbox", action="store_true")
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--manifest", default=MANIFEST_DEFAULT)
    parser.add_argument("--db", default=DB_DEFAULT)
    args = parser.parse_args(argv)
    if args.sandbox and not args.confirm_sandbox:
        print("refused: --sandbox needs --confirm-sandbox")
        return 2
    if args.reset:
        if not args.sandbox:
            owned = load_manifest(args.manifest)
            if owned.get("transfers") or owned.get("requests"):
                print("reset refused: manifest owns sandbox transfers; use --sandbox --confirm-sandbox")
                return 1
            Path(args.manifest).unlink(missing_ok=True)
            print("reset mock: manifest removed")
            return 0
        config = resolve_mode("sandbox")
        if config.mode != "SANDBOX":
            print("reset refused:", config.reason)
            return 1
        from backend.sandbox import SandboxDriver

        client = make_client_factory(config)(None)
        ok, problems = reset_owned(args.manifest, client, SandboxDriver(client, confirm_sandbox=True))
        print("reset", "ok" if ok else "refused " + ",".join(problems))
        return 0 if ok else 1
    result = run_sandbox(args.manifest, db=args.db) if args.sandbox else run_mock()
    for beat, status in result.items():
        print(f"{beat}: {status}")
    return 0 if all(v.startswith(("PASS", "PARTIAL")) for v in result.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
