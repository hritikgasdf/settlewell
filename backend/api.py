"""Local-demo HTTP API. Every write goes through backend.execute. MOCK only.

There is no production authentication here. The operator is server-controlled, writes
need loopback Host, an allowed Origin and a per-process CSRF token, and writes are
refused unless SETTLEWELL_MOCK=1. GET handlers never write.

Hosted demo (backend.hosted): SETTLEWELL_ALLOWED_HOSTS / SETTLEWELL_PUBLIC_ORIGIN add exact
hosts and origins, SETTLEWELL_DEMO_PASSCODE puts a passcode gate in front of every page and
route except /health, and SETTLEWELL_STATIC_DIR serves the built UI on the same origin. With
none of them set, behaviour is the loopback-only local demo.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from backend import api_proof
from backend import hosting as hosting_mod
from backend.airwallex.client import AirwallexError
from backend.airwallex.types import CancelResult, Transfer
from backend.approvals import AP_CLERK, FINANCE_APPROVER, binding_hash
from backend.execute import (
    ExecuteError,
    approve_intent,
    execute,
    fail_closed,
    intent_terms,
    open_intent,
)
from backend.fx_funding import FundingError
from backend.ledger import (
    append_audit,
    connect,
    insert_external_transfer,
    migrate,
    seed_hero,
)
from backend.replace_payment import ReplaceError
from backend.stop_duplicate import StopDuplicateError

BANNER = "MOCK / LOCAL DEMO - synthetic data, no live money"
OPERATOR = FINANCE_APPROVER
CSRF_HEADER = "X-Settlewell-CSRF"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}
DEFAULT_PORT = 8765
UI_PORT = 5173
CANCELLABLE = "SCHEDULED"

_STATUS = {
    "used": (409, "approval_used"),
    "refused": (409, "intent_refused"),
    "void": (409, "terms_void"),
    "expired": (409, "approval_expired"),
    "self": (403, "self_approval"),
    "approver": (403, "operator_not_allowed"),
    "in_flight": (409, "in_flight"),
    "missing": (404, "not_found"),
}

_write_lock = threading.Lock()


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **extra) -> None:
        self.status, self.code, self.message, self.extra = status, code, message or code, extra
        super().__init__(code)


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ApproveBody(_Body):
    binding_hash: str = Field(min_length=64, max_length=64)


class RejectBody(_Body):
    binding_hash: str = Field(min_length=64, max_length=64)
    reason: str = Field(min_length=1, max_length=200)


class CancelBody(_Body):
    expected_status: str = Field(min_length=1, max_length=40)


def _now() -> str:
    return datetime.now(UTC).isoformat()


_ACTIVE: list = [None]  # ModeConfig set by create_app; None keeps the env-only MOCK behaviour


def mock_enabled() -> bool:
    """True when writes are allowed: MOCK env, or an explicit writable mode config."""
    if _ACTIVE[0] is not None:
        return _ACTIVE[0].writes_enabled
    return os.environ.get("SETTLEWELL_MOCK") == "1"


def writes_authorized() -> bool:
    """MOCK (env or config) or an explicit resolved SANDBOX config. READ_ONLY never; a client factory cannot change this."""
    active = _ACTIVE[0]
    if active is None:
        return os.environ.get("SETTLEWELL_MOCK") == "1"
    return active.writes_enabled and active.mode in ("MOCK", "SANDBOX")


def mode_label() -> str:
    if _ACTIVE[0] is not None:
        return _ACTIVE[0].mode
    return "MOCK" if mock_enabled() else "READ_ONLY"


def _banner() -> str:
    return _ACTIVE[0].banner if _ACTIVE[0] is not None else BANNER


# ---- deterministic mock remote that keeps its state in the same SQLite file ----


class LedgerMockClient:
    """Fake Airwallex. Reads and honest updates live in mock_remote, so restarts agree."""

    def __init__(self, conn) -> None:
        self.conn = conn

    def get_transfer(self, transfer_id: str) -> Transfer:
        row = self.conn.execute(
            "SELECT * FROM mock_remote WHERE airwallex_id = ?", (transfer_id,)
        ).fetchone()
        if row is None:
            raise AirwallexError(404, "resource_not_found", "")
        return Transfer(
            id=row["airwallex_id"],
            status=row["status"],
            transfer_amount=Decimal(row["amount"]),
            transfer_currency=row["currency"],
            beneficiary_id=row["beneficiary_id"],
            reference=row["reference"],
        )

    def cancel_transfer(self, transfer_id: str) -> CancelResult:
        row = self.conn.execute(
            "SELECT status FROM mock_remote WHERE airwallex_id = ?", (transfer_id,)
        ).fetchone()
        if row is None or row["status"] != CANCELLABLE:
            raise AirwallexError(400, "update_unsupported", "")
        self.conn.execute(
            "UPDATE mock_remote SET status = 'CANCELLED' WHERE airwallex_id = ?", (transfer_id,)
        )
        self.conn.commit()
        return CancelResult(ok=True, transfer=None)


_MOCK_REMOTE = """
CREATE TABLE IF NOT EXISTS mock_remote (
    airwallex_id TEXT PRIMARY KEY, status TEXT NOT NULL, beneficiary_id TEXT,
    amount TEXT NOT NULL, currency TEXT NOT NULL, reference TEXT
);
"""


def init_db(path: str) -> None:
    """Create tables and seed the demo exactly once. A populated DB is never reseeded."""
    from pathlib import Path

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        migrate(conn)
        conn.executescript(_MOCK_REMOTE)
        conn.executescript(api_proof.PROOF_BLOB)  # additive; existing rows are untouched
        if conn.execute("SELECT COUNT(*) AS n FROM incident").fetchone()["n"] == 0:
            _seed_demo(conn)
    finally:
        conn.close()


def reset_mock_demo(path: str) -> None:
    """MOCK only: wipe every table of the synthetic demo DB and reseed INV-2291.

    Table names come from sqlite_master, never from input. No remote call is made."""
    conn = connect(path)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA foreign_keys = OFF")
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        for name in names:
            conn.execute(f'DELETE FROM "{name}"')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            conn.execute("DELETE FROM sqlite_sequence")
        conn.commit()
        conn.execute("PRAGMA foreign_keys = ON")
        _seed_demo(conn)
    finally:
        conn.close()


def _seed_demo(conn) -> None:
    now = _now()
    incident_id = seed_hero(conn)
    obligation = conn.execute(
        "SELECT * FROM obligation WHERE id = (SELECT obligation_id FROM incident WHERE id = ?)",
        (incident_id,),
    ).fetchone()
    live = conn.execute(
        "SELECT airwallex_id FROM transfer_record WHERE id = ?", (obligation["live_transfer_id"],)
    ).fetchone()
    dup = insert_external_transfer(
        conn,
        airwallex_id="synthetic-dup-2291",
        request_id="colleague-friday",
        obligation_id=obligation["id"],
        amount=obligation["amount"],
        currency=obligation["currency"],
        status=CANCELLABLE,
        reference=obligation["invoice_ref"],
    )
    conn.execute("UPDATE transfer_record SET details_hash = 'colleague-v1' WHERE id = ?", (dup,))
    api_proof.seed_claim(conn, incident_id, int(obligation["supplier_id"]))
    api_proof.seed_mock_proof(conn, obligation["live_transfer_id"], obligation["invoice_ref"], "SENT")
    for remote_id, status, ben in (
        (live["airwallex_id"], "SENT", "ben_lumen"),
        ("synthetic-dup-2291", CANCELLABLE, "ben_colleague"),
    ):
        conn.execute(
            "INSERT INTO mock_remote VALUES (?, ?, ?, ?, ?, ?)",
            (remote_id, status, ben, obligation["amount"], obligation["currency"],
             obligation["invoice_ref"]),
        )
    ref = open_intent(
        conn,
        kind="cancel",
        incident_id=incident_id,
        obligation_id=obligation["id"],
        action="STOP_DUPLICATE",
        amount=obligation["amount"],
        currency=obligation["currency"],
        beneficiary_id="ben_colleague",
        details_hash="colleague-v1",
        proposer=AP_CLERK,
        target_id=str(dup),
        expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
        created_at=now,
    )
    # Honest seed label: this is not a policy run. Still record the rules that justify the
    # seeded STOP_DUPLICATE proposal (R-MATCHED from the offline parser; scheduled duplicate).
    seeded_rationale = (
        "Seeded demo row, not a policy run. The offline parser matched the synthetic email "
        "under R-MATCHED (INV-2291, amount and currency agreed). The colleague duplicate is "
        "SCHEDULED, so the proposal is STOP_DUPLICATE (R-STOP-DUPLICATE-SCHEDULED). "
        "Prerequisites are listed on the proposal; they were not produced by a live policy execution."
    )
    conn.execute(
        """INSERT INTO decision (incident_id, rule_id, inputs_hash, proposed_action,
           policy_verdict, llm_rationale) VALUES (?, 'DUPLICATE_SCHEDULED_SEED', ?, 'STOP_DUPLICATE',
           'seeded_demo', ?)""",
        (incident_id, binding_hash(intent_terms(conn, ref.id)), seeded_rationale),
    )
    append_audit(conn, incident_id=incident_id, kind="demo_seeded",
                 detail=f"intent={ref.id} synthetic data", created_at=now)
    conn.commit()


# ---- read models (no writes) ----


def _rows(cursor) -> list[dict]:
    return [dict(row) for row in cursor.fetchall()]


def _intent_view(conn, row, operator: str, now: str) -> dict:
    approval = conn.execute("SELECT * FROM approval WHERE id = ?", (row["approval_id"],)).fetchone()
    terms_hash = binding_hash(intent_terms(conn, row["id"]))
    stored = approval["binding_hash"] if approval else None
    if row["state"] == "refused" or stored is None:
        approval_state = "voided"
    elif approval["used_at"] is not None:
        approval_state = "consumed"
    else:
        approval_state = "pending"
    checks = [
        ("intent_pending", row["state"] == "proposed", f"state={row['state']}"),
        ("approval_unused", approval_state == "pending", f"approval={approval_state}"),
        ("terms_bound", stored == terms_hash, "stored terms hash matches intent terms"),
        ("not_expired", bool(row["expires_at"]) and now < row["expires_at"],
         f"expires_at={row['expires_at']}"),
        ("separate_approver", bool(approval) and approval["proposer"] != operator,
         f"proposer={approval['proposer'] if approval else None} operator={operator}"),
        ("mock_writes_enabled", mock_enabled(), "writes need MOCK or an explicit SANDBOX mode"),
    ]
    if row["kind"] == "cancel":
        record = conn.execute(
            "SELECT status FROM transfer_record WHERE id = ?", (row["target_id"],)
        ).fetchone()
        checks.append(("transfer_cancellable", bool(record) and record["status"] == CANCELLABLE,
                       f"local_status={record['status'] if record else None}"))
    else:
        checks.append(("gate_checks_at_execute", None,
                       "policy, funding and quote are evaluated by execute()"))
    prerequisites = [{"name": n, "ok": ok, "detail": d} for n, ok, d in checks]
    blocked = [p["name"] for p in prerequisites if p["ok"] is False]
    return {
        "id": row["id"], "incident_id": row["incident_id"], "kind": row["kind"],
        "action": row["action"], "state": row["state"], "amount": row["amount"],
        "currency": row["currency"], "beneficiary_id": row["beneficiary_id"],
        "details_hash": row["details_hash"], "quote_id": row["quote_id"],
        "target_id": row["target_id"], "failure_code": row["failure_code"],
        "expires_at": row["expires_at"], "created_at": row["created_at"],
        "remote_id": row["remote_id"], "remote_status": row["remote_status"],
        "binding_hash": terms_hash if approval_state == "pending" else None,
        "approval": {
            "id": row["approval_id"], "state": approval_state,
            "proposer": approval["proposer"] if approval else None,
            "approver": approval["approver"] if approval else None,
            "used_at": approval["used_at"] if approval else None,
        },
        "prerequisites": prerequisites, "blocked_reasons": blocked,
        "can_approve": not blocked, "can_reject": row["state"] == "proposed"
        and approval_state == "pending",
    }


def _transfer_view(row, live_id) -> dict:
    out = {k: row[k] for k in (
        "id", "airwallex_id", "request_id", "origin", "amount", "currency", "fee", "status",
        "failure_code", "failure_message", "dispatch_date", "reference", "details_hash")}
    out["is_live"] = row["id"] == live_id
    out["cancellable"] = row["status"] == CANCELLABLE
    return out


def _claim_of(raw) -> dict | None:
    """A parsed claim object, or None. Marker rows (e.g. 'duplicate:<id>') are not claims."""
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _incident_detail(conn, incident_id: int, operator: str) -> dict | None:
    inc = conn.execute("SELECT * FROM incident WHERE id = ?", (incident_id,)).fetchone()
    if inc is None:
        return None
    now = _now()
    ob = conn.execute("SELECT * FROM obligation WHERE id = ?", (inc["obligation_id"],)).fetchone()
    sup = conn.execute("SELECT * FROM supplier WHERE id = ?", (ob["supplier_id"],)).fetchone()
    versions = _rows(conn.execute(
        """SELECT id, beneficiary_id, details_hash, bank_name, account_last4, source,
           verified_by, verified_at FROM bank_detail_version WHERE supplier_id = ? ORDER BY id""",
        (sup["id"],)))
    pinned = [v for v in versions if v["source"] in ("onboarding", "verified_callback")]
    transfers = conn.execute(
        "SELECT * FROM transfer_record WHERE obligation_id = ? ORDER BY id", (ob["id"],)
    ).fetchall()
    ids = [t["id"] for t in transfers]
    marks = ",".join("?" * len(ids)) or "NULL"
    intents = conn.execute(
        "SELECT * FROM execution_intent WHERE incident_id = ? ORDER BY id", (incident_id,)
    ).fetchall()
    claim = _claim_of(inc["claim_json"])
    return {
        "mode": mode_label(),
        "banner": _banner(),
        "incident": {"id": inc["id"], "state": inc["state"], "claim": claim,
                     "evidence": {"claim_recorded": claim is not None,
                                  "claim_matched": bool(claim and claim.get("matched") is True)}},
        "obligation": {k: ob[k] for k in (
            "id", "invoice_ref", "amount", "currency", "status", "live_transfer_id")},
        "supplier": {"id": sup["id"], "name": sup["name"], "contact": sup["contact"]},
        "pinned_bank": pinned[-1] if pinned else None,
        "bank_versions": versions,
        "transfers": [_transfer_view(t, ob["live_transfer_id"]) for t in transfers],
        "status_history": _rows(conn.execute(
            f"""SELECT transfer_record_id, status, failure_code, source, seen_at
            FROM status_snapshot WHERE transfer_record_id IN ({marks}) ORDER BY id""", ids)),
        "decisions": _rows(conn.execute(
            """SELECT id, rule_id, proposed_action, policy_verdict, llm_rationale, inputs_hash
            FROM decision WHERE incident_id = ? ORDER BY id""", (incident_id,))),
        "proposals": [_intent_view(conn, r, operator, now) for r in intents],
        "funding": {
            "payouts": _rows(conn.execute(
                """SELECT intent_id, currency, amount, fee_budget, reserve_floor, available_before,
                state, hold_state, after_state FROM payout_funding WHERE incident_id = ?
                ORDER BY id""", (incident_id,))),
            "balances": _rows(conn.execute(
                """SELECT currency, available, label, taken_at FROM balance_snapshot
                ORDER BY id DESC LIMIT 10""")),
        },
        "proof": {
            "pdf_link": None,
            "reason": "no PDF bytes are stored or served in this build",
            "documents": _rows(conn.execute(
                f"""SELECT id, transfer_record_id, format, status_at_issue, sha256, sent_at
                FROM proof_document WHERE transfer_record_id IN ({marks}) ORDER BY id""", ids)),
        },
        "ledger": {"obligation_amount": ob["amount"], "currency": ob["currency"],
                   "fees": [{"transfer_id": t["id"], "fee": t["fee"]} for t in transfers
                            if t["fee"] is not None]},
        "audit": _rows(conn.execute(
            "SELECT id, kind, detail, created_at FROM audit_event WHERE incident_id = ? ORDER BY id",
            (incident_id,))),
    }


# ---- app ----


def create_app(db_path: str | None = None, *, client_factory=None, clock=None,
               port: int = DEFAULT_PORT, worker: bool = False, worker_interval: float = 5.0,
               now_ms=None, mode_config=None, ui_port: int = UI_PORT, hosting=None,
               sandbox_demo=None) -> FastAPI:
    path = db_path or os.environ.get("SETTLEWELL_DB", "var/settlewell.db")
    host_cfg = hosting if hosting is not None else hosting_mod.Hosting.from_env()
    _ACTIVE[0] = mode_config
    sandbox = mode_config is not None and mode_config.mode == "SANDBOX"
    make_client = client_factory or LedgerMockClient
    now = clock or _now
    token = secrets.token_urlsafe(32)
    origins = {f"http://{h}:{p}" for h in ("127.0.0.1", "localhost") for p in (port, ui_port)}
    origins |= set(host_cfg.public_origins)
    allowed_hosts = LOOPBACK | set(host_cfg.allowed_hosts)
    hosted = bool(host_cfg.allowed_hosts or host_cfg.public_origins or host_cfg.gate_enabled
                  or host_cfg.static_dir)
    limiter = hosting_mod.LoginLimiter()
    host_message = "host not allowed" if host_cfg.allowed_hosts else "loopback only"
    if mock_enabled() and not sandbox:
        init_db(path)  # demo seed + mock_remote are MOCK only; sandbox DBs come from a run manifest
    clock_ms = now_ms or (lambda: int(time.time() * 1000))
    background = None

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        nonlocal background
        if worker and mock_enabled():
            from backend.worker import Worker

            def open_conn():
                conn = connect(path)
                conn.execute("PRAGMA busy_timeout = 5000")
                return conn

            background = Worker(open_conn, lambda conn: make_client(conn), interval=worker_interval,
                                clock_ms=clock_ms)
            background.start()
        try:
            yield
        finally:
            if background is not None:
                background.stop()
                background = None

    app = FastAPI(title="Settlewell local demo API", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.exception_handler(ApiError)
    async def _api_error(_request, error: ApiError):
        body = {"error": {"code": error.code, "message": error.message, **error.extra}}
        return JSONResponse(body, status_code=error.status)

    @app.exception_handler(PermissionError)
    async def _read_only(_request, _error):
        body = {"error": {"code": "read_only", "message": "READ_ONLY: no remote client is configured"}}
        return JSONResponse(body, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_request, _error):
        body = {"error": {"code": "invalid_request", "message": "invalid or unexpected fields"}}
        return JSONResponse(body, status_code=400)

    # Middleware added later runs first: host guard -> passcode gate -> routes.
    @app.middleware("http")
    async def _passcode_gate(request: Request, call_next):
        path_ = request.url.path
        if host_cfg.gate_enabled and path_ not in hosting_mod.GATE_EXEMPT:
            cookie = request.cookies.get(hosting_mod.COOKIE_NAME)
            if not hosting_mod.cookie_valid(host_cfg.session_secret, cookie):
                if hosting_mod.is_api_path(path_) or request.method != "GET":
                    return JSONResponse(
                        {"error": {"code": "passcode_required", "message": "demo passcode required"}},
                        status_code=401)
                return RedirectResponse(hosting_mod.LOGIN_PATH, status_code=303)
        response = await call_next(request)
        if hosted:
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            # Chrome's PDF viewer frames the document; DENY makes it a blank white page.
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
            if content_type != "application/pdf":
                response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.middleware("http")
    async def _host_guard(request: Request, call_next):
        if request.url.path == hosting_mod.HEALTH_PATH:  # platform health checks use their own Host
            return await call_next(request)
        host = hosting_mod.host_of(request.headers.get("host") or "")
        if host not in allowed_hosts:
            return JSONResponse({"error": {"code": "forbidden_host", "message": host_message}},
                                status_code=403)
        return await call_next(request)

    @app.get(hosting_mod.HEALTH_PATH)
    def platform_health():
        return {"status": "ok", "mode": mode_label()}

    if host_cfg.gate_enabled:

        @app.get(hosting_mod.LOGIN_PATH)
        def login_form():
            return HTMLResponse(hosting_mod.login_page(), headers={"Cache-Control": "no-store"})

        @app.post(hosting_mod.LOGIN_PATH)
        async def login_submit(request: Request):
            origin = request.headers.get("origin")
            if origin is not None and origin not in origins:
                raise ApiError(403, "origin_forbidden", "Origin not allowed")
            who = hosting_mod.client_key(request)
            if limiter.blocked(who):
                return HTMLResponse(hosting_mod.login_page("Too many attempts. Wait a few minutes."),
                                    status_code=429, headers={"Retry-After": "300"})
            raw = await request.body()
            if len(raw) > hosting_mod.MAX_LOGIN_BODY:
                raise ApiError(413, "body_too_large", "body too large")
            from urllib.parse import parse_qs

            supplied = (parse_qs(raw.decode("utf-8", "replace")).get("passcode") or [""])[0]
            if not hosting_mod.passcode_ok(host_cfg.passcode, supplied):
                limiter.fail(who)
                return HTMLResponse(hosting_mod.login_page("Wrong passcode."), status_code=401,
                                    headers={"Cache-Control": "no-store"})
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(hosting_mod.COOKIE_NAME, hosting_mod.make_cookie(host_cfg.session_secret),
                                max_age=hosting_mod.COOKIE_TTL_S, path="/", httponly=True,
                                secure=True, samesite="strict")
            return response

        @app.post(hosting_mod.LOGOUT_PATH)
        def logout():
            response = RedirectResponse(hosting_mod.LOGIN_PATH, status_code=303)
            response.delete_cookie(hosting_mod.COOKIE_NAME, path="/", secure=True, httponly=True,
                                   samesite="strict")
            return response

    def db():
        if not os.path.exists(path):
            raise ApiError(503, "db_missing", "start with SETTLEWELL_MOCK=1 to create the demo DB")
        conn = connect(path)
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    def write_guard(request: Request) -> None:
        if not writes_authorized():
            raise ApiError(503, "writes_disabled", "writes need MOCK or a validated SANDBOX mode")
        origin = request.headers.get("origin")
        if origin is None:
            raise ApiError(403, "origin_missing", "Origin header required")
        if origin not in origins:
            raise ApiError(403, "origin_forbidden", "Origin not allowed")
        sent = request.headers.get(CSRF_HEADER)
        if not sent:
            raise ApiError(403, "csrf_missing", f"{CSRF_HEADER} header required")
        if not hmac.compare_digest(sent, token):
            raise ApiError(403, "csrf_invalid", "bad CSRF token")

    @app.post("/api/webhooks/airwallex")
    async def airwallex_webhook(request: Request):
        # Authenticated by the HMAC signature over the raw body, not by operator CSRF.
        from backend.webhooks import MAX_BODY, WebhookError, receive_webhook

        secret = os.environ.get("AIRWALLEX_WEBHOOK_SECRET", "")
        if not secret.strip():
            raise ApiError(503, "webhook_unconfigured", "webhook secret is not configured")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY:
            raise ApiError(413, "body_too_large", "body too large")
        raw = await request.body()
        conn = db()
        try:
            result = receive_webhook(
                conn, secret=secret, raw_body=raw,
                x_timestamp=request.headers.get("x-timestamp", ""),
                x_signature=request.headers.get("x-signature", ""), now_ms=clock_ms())
        except WebhookError as error:
            status = {"signature": 401, "stale": 400, "body": 400, "unconfigured": 503}[error.code]
            raise ApiError(status, f"webhook_{error.code}", "webhook rejected") from None
        finally:
            conn.close()
        return {"outcome": result.outcome}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": mode_label(),
                "banner": _banner(), "writes_enabled": writes_authorized()}

    @app.get("/api/session")
    def session():
        return {"operator": OPERATOR, "csrf_token": token, "csrf_header": CSRF_HEADER,
                "mode": mode_label(), "banner": _banner(),
                "writes_enabled": writes_authorized(),
                "note": "local demo only; no production authentication"}

    @app.get("/api/incidents")
    def incidents():
        conn = db()
        try:
            rows = conn.execute(
                """SELECT incident.id, incident.state, obligation.invoice_ref, obligation.amount,
                   obligation.currency, obligation.status AS obligation_status, supplier.name AS supplier,
                   (SELECT COUNT(*) FROM execution_intent e WHERE e.incident_id = incident.id
                    AND e.state = 'proposed') AS pending_proposals
                   FROM incident JOIN obligation ON obligation.id = incident.obligation_id
                   JOIN supplier ON supplier.id = obligation.supplier_id ORDER BY incident.id"""
            ).fetchall()
            return {"mode": mode_label(), "banner": _banner(),
                    "incidents": [dict(r) for r in rows]}
        finally:
            conn.close()

    @app.get("/api/incidents/{incident_id}")
    def incident(incident_id: int):
        conn = db()
        try:
            detail = _incident_detail(conn, incident_id, OPERATOR)
            if detail is not None:
                try:
                    detail["proof"] = api_proof.proof_view(conn, make_client, incident_id)
                except PermissionError:
                    raise ApiError(503, "read_only", "READ_ONLY: proof status unavailable") from None
        finally:
            conn.close()
        if detail is None:
            raise ApiError(404, "not_found", "incident not found")
        return detail

    @app.get("/api/incidents/{incident_id}/proof/{document_id}/pdf")
    def proof_pdf(incident_id: int, document_id: int):
        conn = db()
        try:
            data, headers = api_proof.load_pdf(conn, make_client, incident_id, document_id)
        except PermissionError:
            raise ApiError(503, "read_only", "READ_ONLY: proof unavailable") from None
        except LookupError as error:
            code, status = error.args
            raise ApiError(status, code.split(":")[0], f"proof refused: {code}",
                           gate_code=code) from None
        finally:
            conn.close()
        return Response(data, media_type=api_proof.PDF_MEDIA_TYPE, headers=headers)

    def _intent_in_incident(conn, incident_id: int, intent_id: int):
        row = conn.execute("SELECT * FROM execution_intent WHERE id = ?", (intent_id,)).fetchone()
        if row is None:
            raise ApiError(404, "not_found", "intent not found")
        if row["incident_id"] != incident_id:
            raise ApiError(409, "cross_incident", "intent belongs to another incident")
        return row

    def _check_hash(conn, row, supplied: str) -> None:
        if row["state"] == "refused":
            raise ApiError(409, "intent_refused", "intent already refused")
        approval = conn.execute("SELECT * FROM approval WHERE id = ?", (row["approval_id"],)).fetchone()
        if row["state"] != "proposed" or approval is None or approval["used_at"] is not None:
            raise ApiError(409, "approval_used", "approval already consumed; replay refused")
        current = binding_hash(intent_terms(conn, row["id"]))
        if not hmac.compare_digest(supplied, current) or approval["binding_hash"] != current:
            raise ApiError(409, "stale_terms", "terms changed; reload the proposal")

    def _gate(conn, client, row) -> dict:
        """Approve as the server-side operator, then run the one execute() gate."""
        stamp = now()
        try:
            approve_intent(conn, row["id"], approver=OPERATOR, as_of=stamp)
            append_audit(conn, incident_id=row["incident_id"], kind="intent_approved",
                         detail=f"intent={row['id']} by={OPERATOR}", created_at=stamp)
            conn.commit()
            result = execute(conn, client, row["id"], as_of=stamp)
        except (ExecuteError, StopDuplicateError, AirwallexError, ReplaceError,
                FundingError) as error:
            code = getattr(error, "code", None) or "remote_error"
            closed = fail_closed(conn, row["id"], code=str(code), as_of=stamp)
            status, name = _STATUS.get(str(code), (409, "gate_refused"))
            if str(code).startswith("status_"):
                status, name = 409, "not_cancellable"
            raise ApiError(status, name, f"gate refused: {code}", gate_code=str(code),
                           intent_terminal=closed) from error
        append_audit(conn, incident_id=row["incident_id"], kind="intent_executed",
                     detail=f"intent={row['id']} kind={result.kind} remote={result.remote_id} "
                            f"status={result.status}", created_at=stamp)
        conn.commit()
        view = _intent_view(conn, _intent_in_incident(conn, row["incident_id"], row["id"]),
                            OPERATOR, stamp)
        return {"ok": True, "incident_id": row["incident_id"],
                "result": {"kind": result.kind, "remote_id": result.remote_id,
                           "status": result.status, "funding_note": result.funding_note},
                "intent": view}

    @app.post("/api/incidents/{incident_id}/proposals/{intent_id}/approve")
    def approve(incident_id: int, intent_id: int, body: ApproveBody, request: Request):
        write_guard(request)
        with _write_lock:
            conn = db()
            try:
                row = _intent_in_incident(conn, incident_id, intent_id)
                _check_hash(conn, row, body.binding_hash)
                return _gate(conn, make_client(conn), row)
            finally:
                conn.close()

    @app.post("/api/incidents/{incident_id}/proposals/{intent_id}/reject")
    def reject(incident_id: int, intent_id: int, body: RejectBody, request: Request):
        write_guard(request)
        from backend.execute import reject_intent

        with _write_lock:
            conn = db()
            try:
                row = _intent_in_incident(conn, incident_id, intent_id)
                _check_hash(conn, row, body.binding_hash)
                try:
                    reject_intent(conn, intent_id, rejector=OPERATOR, reason=body.reason,
                                  as_of=now())
                except ExecuteError as error:
                    status, name = _STATUS.get(error.code, (409, "gate_refused"))
                    raise ApiError(status, name, f"gate refused: {error.code}") from error
                view = _intent_view(conn, _intent_in_incident(conn, incident_id, intent_id),
                                    OPERATOR, now())
                return {"ok": True, "incident_id": incident_id, "intent": view}
            finally:
                conn.close()

    @app.post("/api/incidents/{incident_id}/transfers/{transfer_id}/cancel")
    def cancel(incident_id: int, transfer_id: int, body: CancelBody, request: Request):
        write_guard(request)
        with _write_lock:
            conn = db()
            try:
                inc = conn.execute("SELECT obligation_id FROM incident WHERE id = ?",
                                   (incident_id,)).fetchone()
                rec = conn.execute("SELECT * FROM transfer_record WHERE id = ?",
                                   (transfer_id,)).fetchone()
                if inc is None or rec is None:
                    raise ApiError(404, "not_found", "incident or transfer not found")
                if rec["obligation_id"] != inc["obligation_id"]:
                    raise ApiError(409, "cross_incident", "transfer belongs to another incident")
                if rec["status"] != body.expected_status:
                    raise ApiError(409, "stale_status", f"transfer is now {rec['status']}")
                if rec["status"] != CANCELLABLE:
                    raise ApiError(409, "not_cancellable", f"status {rec['status']} is not pending")
                client = make_client(conn)
                existing = conn.execute(
                    """SELECT * FROM execution_intent WHERE kind = 'cancel' AND incident_id = ?
                    AND target_id = ? AND state = 'proposed' ORDER BY id DESC LIMIT 1""",
                    (incident_id, str(transfer_id))).fetchone()
                if existing is None:
                    try:
                        remote = client.get_transfer(rec["airwallex_id"])
                    except AirwallexError as error:
                        raise ApiError(409, "remote_unknown", "remote status unreadable") from error
                    if remote.status != CANCELLABLE or not remote.beneficiary_id:
                        raise ApiError(409, "not_cancellable",
                                       f"remote status {remote.status} is not pending")
                    stamp = now()
                    ref = open_intent(
                        conn, kind="cancel", incident_id=incident_id,
                        obligation_id=inc["obligation_id"], action="STOP_DUPLICATE",
                        amount=rec["amount"], currency=rec["currency"],
                        beneficiary_id=remote.beneficiary_id,
                        details_hash=rec["details_hash"] or "", proposer=AP_CLERK,
                        target_id=str(transfer_id),
                        expires_at=(datetime.fromisoformat(stamp) + timedelta(minutes=10)).isoformat(),
                        created_at=stamp)
                    existing = _intent_in_incident(conn, incident_id, ref.id)
                return _gate(conn, client, existing)
            finally:
                conn.close()

    @app.get("/api/demo/status")
    def demo_status():
        if sandbox:
            return {"mode": mode_label(), "resettable": sandbox_demo is not None,
                    **(sandbox_demo.status() if sandbox_demo is not None else {})}
        return {"mode": mode_label(), "resettable": mock_enabled(), "state": "idle"}

    @app.post("/api/demo/reset")
    def demo_reset(request: Request):
        """Rerun the demo. MOCK: wipe and reseed the synthetic DB. SANDBOX: release/cancel this
        run's own transfers (cancels go through execute()) and run the hero again in the
        background. Same Origin + CSRF guard as every other write."""
        write_guard(request)
        if not sandbox:
            with _write_lock:
                reset_mock_demo(path)
            return {"ok": True, "state": "done", "mode": mode_label()}
        if sandbox_demo is None:
            raise ApiError(409, "reset_unavailable",
                           "sandbox rerun is disabled; set SETTLEWELL_SANDBOX_AUTORUN=1 on the host")
        started, reason = sandbox_demo.start(reset=True)
        if not started:
            raise ApiError(409, "reset_refused", reason)
        return JSONResponse({"ok": True, "state": "running", "mode": mode_label()}, status_code=202)

    static = hosting_mod.resolve_static_dir(host_cfg.static_dir)
    if static is not None:
        from fastapi.staticfiles import StaticFiles

        app.mount("/", StaticFiles(directory=str(static), html=True), name="ui")

    return app


def main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--ui-port", type=int, default=UI_PORT,
                        help="exact loopback UI origin port to allow (1024-65535, default 5173)")
    parser.add_argument("--db", default=os.environ.get("SETTLEWELL_DB", "var/settlewell.db"))
    parser.add_argument("--mode", choices=("mock", "sandbox"), default="mock",
                        help="mock (default) or sandbox (exact sandbox URL + sandbox credentials only)")
    parser.add_argument("--manifest", default="var/hero-run-manifest.json",
                        help="sandbox mode: hero run manifest that names the seeded DB")
    parser.add_argument("--no-worker", action="store_true", help="disable the periodic worker")
    args = parser.parse_args(argv)
    if not (1024 <= args.ui_port <= 65535) or not (1024 <= args.port <= 65535):
        print("refused: ports must be 1024-65535")
        return 2
    from backend.api_mode import make_client_factory, resolve_mode

    config = resolve_mode(args.mode)
    if args.mode == "sandbox":  # bind to the REAL seeded ledger recorded by this run's manifest
        from backend.hero_live import load_manifest

        owned_db = load_manifest(args.manifest).get("db")
        if not owned_db:
            print("refused: no hero run manifest; run backend.scripts.hero_run --sandbox first")
            return 1
        args.db = owned_db
    if config.mode == "MOCK":
        os.environ["SETTLEWELL_MOCK"] = "1"
    else:
        os.environ.pop("SETTLEWELL_MOCK", None)  # SANDBOX / READ_ONLY are never MOCK
    print("mode", config.mode, "writes", config.writes_enabled, config.reason, ",".join(config.missing))
    app = create_app(args.db, port=args.port, ui_port=args.ui_port, worker=not args.no_worker and config.writes_enabled,
                     mode_config=config, client_factory=make_client_factory(config))
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
