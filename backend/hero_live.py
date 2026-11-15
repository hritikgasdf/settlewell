"""Live (sandbox) hero: a resumable state machine over the real gates.

Every money step goes through execute() with a bound, different-person approval. Original and
colleague-duplicate fixtures are created by the confirmed SandboxDriver for THIS run only.
Request ids and object ids are persisted to the manifest BEFORE/AS each remote object exists, so
a restart recovers by request id and never re-POSTs. Failures return a sanitized beat and code.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from backend.approvals import AP_CLERK, FINANCE_APPROVER, approve
from backend.bank_change import verify_bank_change
from backend.execute import CallbackConfirmation, execute, intent_terms, open_intent
from backend.fx_funding import fund_replacement
from backend.ledger import append_bank_detail_version, connect, migrate
from backend.paid_once import scan_paid_once
from backend.policy import code_from_reason, decide
from backend.proof import issue_proof
from backend.reconcile import poll_incident
from backend.replacement_tool import refund_card

ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}$")
ID_MAPS = ("transfers", "conversions", "quotes", "beneficiaries")
ALLOWED_KEYS = {"mode", "db", "run", "stage", "requests", "intents", "ids", "beats", *ID_MAPS}
STAGES = ("bootstrap", "claim", "duplicate", "proof_sent", "bank_return", "bank_change", "fx",
          "replacement", "close")
BEAT_OF = {
    "bootstrap": "original_sent", "claim": "claude_claim_pinned", "duplicate": "stop_duplicate",
    "proof_sent": "proof_outbox_status", "bank_return": "bank_return_refund",
    "bank_change": "bank_change_validated", "fx": "fx_quote_conversion_settled",
    "replacement": "approved_replacement", "close": "reconcile_both",
}
OLD_BANK = {
    "beneficiary": {
        "entity_type": "COMPANY", "company_name": "Lumen Textiles GmbH",
        "address": {"country_code": "DE", "city": "Berlin", "postcode": "10115",
                    "street_address": "1 Synthetic Strasse"},
        "bank_details": {"account_name": "Lumen Textiles GmbH", "account_currency": "EUR",
                         "bank_country_code": "DE", "bank_name": "Commerzbank",
                         "iban": "DE89370400440532013000", "swift_code": "COBADEFF"},
    },
    "nickname": "settlewell-hero-old", "transfer_methods": ["SWIFT"],
}
NEW_BANK = {
    "beneficiary": {
        "entity_type": "COMPANY", "company_name": "Lumen Textiles GmbH",
        "address": OLD_BANK["beneficiary"]["address"],
        "bank_details": {"account_name": "Lumen Textiles GmbH", "account_currency": "EUR",
                         "bank_country_code": "DE", "bank_name": "Deutsche Kreditbank",
                         "iban": "DE02120300000000202051", "swift_code": "BYLADEM1001"},
    },
    "nickname": "settlewell-hero-new", "transfer_methods": ["SWIFT"],
}
AMOUNT = Decimal("4200.00")


class HeroError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def load_manifest(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    return {k: v for k, v in json.loads(p.read_text()).items() if k in ALLOWED_KEYS}


def save_manifest(path: str, data: dict) -> None:
    """Atomic, allow-listed, id-checked. Never raw responses or credentials."""
    clean = {k: v for k, v in data.items() if k in ALLOWED_KEYS}
    for key in ID_MAPS:
        for value in clean.get(key) or {}:
            if not ID_RE.match(str(value)):
                raise ValueError(f"unsafe id in {key}")
    for key in ("requests", "ids"):
        for value in (clean.get(key) or {}).values():
            if not ID_RE.match(str(value)):
                raise ValueError(f"unsafe id in {key}")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent)
    with os.fdopen(fd, "w") as handle:
        json.dump(clean, handle, sort_keys=True)
    os.replace(tmp, p)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def sanitized(error: Exception) -> str:
    code = getattr(error, "code", None) or getattr(error, "status_code", None)
    return f"{type(error).__name__}:{code}" if code else type(error).__name__


class Hero:
    def __init__(self, client, driver, manifest_path: str, db_path: str, *, complete=None,
                 sleep=time.sleep, now=_now, polls: int = 20) -> None:
        self.client, self.driver, self.path = client, driver, manifest_path
        self.complete, self.sleep, self.now, self.polls = complete, sleep, now, polls
        self.m = load_manifest(manifest_path)
        if not self.m:
            self.m = {"mode": "SANDBOX", "run": f"{uuid.uuid4().int % 10**8:08d}"}  # numeric: INV-<run>
        self.m.setdefault("db", db_path)
        for key in ("requests", "intents", "ids", "beats", *ID_MAPS):
            self.m.setdefault(key, {})
        self.m["mode"] = "SANDBOX"
        if self.m["db"] != ":memory:":  # owned DB only: create the missing parent, never touch existing files
            Path(self.m["db"]).parent.mkdir(parents=True, exist_ok=True)
        self.conn = connect(self.m["db"])
        migrate(self.conn)
        self.save()

    def save(self) -> None:
        save_manifest(self.path, self.m)

    # ---- driver loop ----
    def run(self) -> dict[str, str]:
        done = self.m.get("stage")
        start = STAGES.index(done) + 1 if done in STAGES else 0
        for name in STAGES[start:]:
            beat = BEAT_OF[name]
            try:
                status = getattr(self, "_" + name)()
            except Exception as error:  # noqa: BLE001 - sanitized beat + code; stage not advanced
                self.m["beats"][beat] = "FAILED"
                self.save()
                return {**self.report(), beat: f"FAILED({sanitized(error)})"}
            self.m["beats"][beat] = status
            self.m["stage"] = name
            self.save()
        return self.report()

    def report(self) -> dict[str, str]:
        out = {b: self.m["beats"].get(b, "NOT_RUN") for b in BEAT_OF.values()}
        return out

    # ---- helpers ----
    def _req(self, name: str) -> str:
        req = self.m["requests"].setdefault(name, f"stl-h-{self.m['run']}-{name}")
        self.save()  # durable BEFORE the remote send
        return req

    def _seed_ben(self, name: str, body: dict) -> str:
        """SCENARIO WORLD SETUP ONLY (prerequisite original beneficiary). Not a Settlewell action."""
        self.driver.require_sandbox()
        req = self._req(name)
        found = self.client.find_beneficiary(req)
        if found is not None:
            bid = found.id
        else:
            if not self.client.validate_beneficiary(body).ok:
                raise HeroError("beneficiary_invalid")
            bid = self.driver.seed_world_beneficiary({**body, "request_id": req}).id
        self.m["beneficiaries"][bid] = name
        self.m["ids"][name + "_ben"] = bid
        self.save()
        return bid

    def _seed_transfer(self, name: str, ben: str, *, future: bool):
        """SCENARIO WORLD SETUP ONLY (original payment, colleague duplicate). Not a Settlewell action."""
        self.driver.require_sandbox()
        req = self._req(name)
        items = self.client.list_transfers(request_id=req).items
        found = next((t for t in items if t.request_id == req), None)  # exact ownership only
        if found is None:
            kwargs = {
                "transfer_amount": AMOUNT, "transfer_currency": "EUR", "source_currency": "EUR",
                "beneficiary_id": ben, "transfer_method": "SWIFT", "reference": self.m["ids"]["ref"],
                "reason": "other_services", "request_id": req,
            }
            if future:
                kwargs["transfer_date"] = (datetime.now(UTC) + timedelta(days=3)).date().isoformat()
            found = self.driver.seed_world_transfer(**kwargs)
        self.m["transfers"][found.id] = found.status
        self.m["ids"][name] = found.id
        self.save()
        return found

    def _intent(self, name: str, **kw) -> int:
        """Open once (id persisted), then approve once with a different person than the proposer."""
        if name not in self.m["intents"]:
            now = self.now()
            exp = (datetime.fromisoformat(now) + timedelta(hours=2)).isoformat()
            intent = open_intent(self.conn, proposer=AP_CLERK, expires_at=exp, created_at=now, **kw)
            self.m["intents"][name] = intent.id
            self.save()
        intent_id = self.m["intents"][name]
        row = self.conn.execute(
            "SELECT e.approval_id, a.approver FROM execution_intent e JOIN approval a ON a.id = e.approval_id "
            "WHERE e.id = ?", (intent_id,)).fetchone()
        if not row["approver"]:
            approve(self.conn, row["approval_id"], intent_terms(self.conn, intent_id),
                    approver=FINANCE_APPROVER, used_at=self.now())
        return intent_id

    def _ids(self) -> tuple[int, int, int]:
        i = self.m["ids"]
        return int(i["obligation_row"]), int(i["incident_row"]), int(i["transfer_row"])

    def _remote_id(self, intent_id: int) -> str:
        return execute(self.conn, self.client, intent_id, as_of=self.now()).remote_id

    # ---- stages ----
    def seed_scenario_world(self):
        """Sandbox-only pre-existing world: old beneficiary, original SENT payment, colleague duplicate.

        Refuses outside the confirmed sandbox BEFORE any remote write. Settlewell actions (duplicate
        cancel, new-bank beneficiary, replacement) are never created here; they go through execute()."""
        self.driver.require_sandbox()
        self.m["ids"].setdefault("ref", f"INV-{self.m['run']}")
        old = self._seed_ben("old", OLD_BANK)
        orig = self._seed_transfer("orig", old, future=False)
        if orig.status != "SENT":
            orig = self.driver.reach(orig.id, "SENT")
        if orig.status != "SENT":
            raise HeroError("original_not_sent")
        dup = self._seed_transfer("dup", old, future=True)
        if dup.status != "SCHEDULED":
            raise HeroError("duplicate_not_scheduled")
        return orig, dup, old

    def _bootstrap(self) -> str:
        orig, _dup, old = self.seed_scenario_world()
        if "obligation_row" not in self.m["ids"]:
            self._seed(orig, old)
        self.m["transfers"][orig.id] = "SENT"
        return "PASS(sandbox)"

    def _seed(self, orig, old_ben: str) -> None:
        c = self.conn
        sup = c.execute("INSERT INTO supplier (name, contact, phone_on_file) VALUES (?, ?, ?)",
                        ("Lumen Textiles GmbH", "ap@lumen.example", "+49-30-000000"))
        append_bank_detail_version(c, supplier_id=int(sup.lastrowid), beneficiary_id=old_ben,
                                   details_hash="onboarding-v1", bank_name="Commerzbank",
                                   account_last4="3000", source="onboarding")
        ob = c.execute("INSERT INTO obligation (invoice_ref, amount, currency, supplier_id, status) "
                       "VALUES (?, ?, 'EUR', ?, 'OPEN')", (self.m["ids"]["ref"], str(AMOUNT), sup.lastrowid))
        fee = format(orig.fee_amount, "f") if orig.fee_amount is not None else None  # remote fee or unknown
        tr = c.execute(
            "INSERT INTO transfer_record (airwallex_id, request_id, obligation_id, origin, details_hash, "
            "amount, currency, fee, status, reference) VALUES (?, ?, ?, 'settlewell', 'onboarding-v1', "
            "?, 'EUR', ?, 'SENT', ?)",
            (orig.id, self.m["requests"]["orig"], ob.lastrowid, str(AMOUNT), fee, self.m["ids"]["ref"]))
        c.execute("UPDATE obligation SET live_transfer_id = ? WHERE id = ?", (tr.lastrowid, ob.lastrowid))
        inc = c.execute("INSERT INTO incident (obligation_id, claim_json, state) VALUES (?, NULL, 'OPEN')",
                        (ob.lastrowid,))
        c.commit()
        self.m["ids"].update(supplier_row=str(sup.lastrowid), obligation_row=str(ob.lastrowid),
                             transfer_row=str(tr.lastrowid), incident_row=str(inc.lastrowid))
        self.save()

    def _claim(self) -> str:
        from backend.claims import anthropic_complete, complete_with_meta, parse_email

        ob, inc, _ = self._ids()
        email = (f"Lumen Textiles GmbH: EUR 4,200 for {self.m['ids']['ref']} never arrived. "
                 "Pay to our NEW account today.")
        meta: dict = {}

        def complete(text: str) -> str:
            if self.complete is anthropic_complete:  # the real Claude call: keep its safe metadata
                out, info = complete_with_meta(text)
                meta.update(info)
                return out
            return self.complete(text)

        result = parse_email(email, complete, self.conn, supplier_id=int(self.m["ids"]["supplier_row"]))
        if result.escalated or result.obligation_id != ob or result.claim is None:
            raise HeroError("claim_" + result.rule_id)
        # Safe parse provenance only: source and model name, never the prompt or raw reply.
        parse = {"source": "claude" if meta.get("model") else "injected_test_parser",
                 "model": meta.get("model"), "rule_id": result.rule_id,
                 "summary": "Model proposed JSON only; code matched invoice, amount and currency "
                            "to the pinned obligation and supplier."}
        self.conn.execute("UPDATE incident SET claim_json = ? WHERE id = ?",
                          (json.dumps({**dataclasses.asdict(result.claim), "matched": True,
                                       "parse": parse}), inc))
        self.conn.commit()
        return "PASS(sandbox, model parse; pinned supplier match)"

    def _duplicate(self) -> str:
        ob, _, _ = self._ids()
        dup_id = self.m["ids"]["dup"]
        if "dup_incident" not in self.m["ids"]:
            opened = scan_paid_once(self.conn, self.client, ob)
            if not opened:
                raise HeroError("duplicate_not_detected")
            self.m["ids"]["dup_incident"] = str(opened[0])
            self.save()
        row = self.conn.execute("SELECT * FROM transfer_record WHERE airwallex_id = ?", (dup_id,)).fetchone()
        if row is None:
            raise HeroError("duplicate_not_recorded")
        intent = self._intent(
            "cancel", kind="cancel", incident_id=int(self.m["ids"]["dup_incident"]), obligation_id=ob,
            action="STOP_DUPLICATE", amount=str(row["amount"]), currency=str(row["currency"]),
            beneficiary_id=self.client.get_transfer(dup_id).beneficiary_id or "",
            details_hash=row["details_hash"] or "", target_id=str(row["id"]))
        execute(self.conn, self.client, intent, as_of=self.now())
        status = self.client.get_transfer(dup_id).status
        self.m["transfers"][dup_id] = status
        if status != "CANCELLED":
            raise HeroError("duplicate_not_cancelled")
        return "PASS(sandbox)"

    def _proof_sent(self) -> str:
        _, inc, tr = self._ids()
        proof = issue_proof(self.conn, self.client, tr, incident_id=inc, seen_at=self.now())
        if not proof.attached:
            raise HeroError("proof_refused_while_sent")
        return "PARTIAL(sandbox: status-gated proof attached; outbox send is a human step)"

    def _bank_return(self) -> str:
        _, inc, tr = self._ids()
        orig = self.m["ids"]["orig"]
        failed = self.driver.fail_account_closed(orig)
        if failed.status != "CANCELLED" or not (failed.failure_reason or "").startswith("90701"):
            raise HeroError("return_not_observed")
        if not self.driver.reversal_joined(orig, "EUR"):
            raise HeroError("reversal_not_observed")
        self.m["transfers"][orig] = "CANCELLED"
        poll_incident(self.conn, self.client, inc, seen_at=self.now())
        if issue_proof(self.conn, self.client, tr, incident_id=inc, seen_at=self.now()).attached:
            raise HeroError("proof_not_refused_after_return")
        return "PASS(sandbox: simulated bank return, reversal joined)"

    def _bank_change(self) -> str:
        ob, inc, _ = self._ids()
        if "bank" not in self.m["intents"]:  # crash between verify and save: reuse our own intent
            prior = self.conn.execute(
                "SELECT id, details_hash, approval_id FROM execution_intent WHERE kind = 'beneficiary' "
                "AND obligation_id = ? ORDER BY id DESC LIMIT 1", (ob,)).fetchone()
            if prior is not None:
                self.m["intents"]["bank"] = prior["id"]
                self.m["ids"]["details_hash"] = prior["details_hash"]
                self.m["ids"]["bank_approval"] = str(prior["approval_id"])
                self.save()
        if "bank" not in self.m["intents"]:
            now = self.now()
            card = verify_bank_change(
                self.conn, self.client, obligation_id=ob, incident_ref=str(inc),
                bank_name="Deutsche Kreditbank", account_last4=NEW_BANK["beneficiary"]["bank_details"]["iban"][-4:],
                beneficiary=NEW_BANK,
                confirmation=CallbackConfirmation(phone="+49-30-000000", confirmed=True,
                                                  attested_by=FINANCE_APPROVER, attested_at=now))
            row = self.conn.execute(
                "SELECT id FROM execution_intent WHERE kind = 'beneficiary' AND obligation_id = ? "
                "ORDER BY id DESC LIMIT 1", (ob,)).fetchone()
            self.m["intents"]["bank"] = row["id"]
            self.m["ids"]["details_hash"] = card.details_hash
            self.m["ids"]["bank_approval"] = str(card.approval_id)
            self.save()
        intent = self.m["intents"]["bank"]
        appr = self.conn.execute("SELECT approver FROM approval WHERE id = ?",
                                 (int(self.m["ids"]["bank_approval"]),)).fetchone()
        if not appr["approver"]:
            approve(self.conn, int(self.m["ids"]["bank_approval"]), intent_terms(self.conn, intent),
                    approver=FINANCE_APPROVER, used_at=self.now())
        bid = self._remote_id(intent)
        self.m["beneficiaries"][bid] = "new"
        self.m["ids"]["new_ben"] = bid
        return "PARTIAL(sandbox: validated + executed; callback attestation is scripted, not a human call)"

    def _fx(self) -> str:
        ob, inc, _ = self._ids()
        if not refund_card(self.conn, self.client, obligation_id=ob).unlocked:
            raise HeroError("refund_not_unlocked")
        if "quote" not in self.m["ids"]:
            funding = fund_replacement(self.conn, self.client, obligation_id=ob, as_of=self.now())
            self.m["ids"]["quote"] = funding.quote_id or "none"
            self.save()
        quote_id = self.m["ids"]["quote"]
        if quote_id == "none":
            return "PARTIAL(sandbox: no FX needed; EUR cash covered the replacement)"
        self.m["quotes"][quote_id] = "LOCKED"
        quote = self.conn.execute("SELECT amount FROM fx_quote WHERE quote_id = ?", (quote_id,)).fetchone()
        intent = self._intent(
            "fx", kind="fx_conversion", incident_id=inc, obligation_id=ob, action="FX_FUND",
            amount=quote["amount"], currency="EUR", beneficiary_id=self.m["ids"]["new_ben"],
            details_hash=self.m["ids"]["details_hash"], quote_id=quote_id)
        execute(self.conn, self.client, intent, as_of=self.now())
        row = self.conn.execute("SELECT request_id FROM execution_intent WHERE id = ?", (intent,)).fetchone()
        for _ in range(self.polls):
            found = self.client.find_conversion(row["request_id"]) if row["request_id"] else None
            if found is not None:
                self.m["conversions"][found.conversion_id] = found.status
                self.save()
                if (found.status or "").upper() == "SETTLED":
                    return "PASS(sandbox: locked quote consumed once, conversion SETTLED)"
            self.sleep(1.0)
        raise HeroError("conversion_not_settled")

    def _replacement(self) -> str:
        ob, inc, _ = self._ids()
        failed = self.client.get_transfer(self.m["ids"]["orig"])
        failure = decide(failed.status, failure_code=code_from_reason(failed.failure_reason),
                         claim="new_details")
        intent = self._intent(
            "replace", kind="replacement", incident_id=inc, obligation_id=ob, action=failure.action.value,
            amount=format(failed.transfer_amount, "f"), currency=failed.transfer_currency or "EUR",
            beneficiary_id=self.m["ids"]["new_ben"], details_hash=self.m["ids"]["details_hash"],
            failure_code="90701")
        new_id = self._remote_id(intent)
        req = self.conn.execute("SELECT request_id FROM execution_intent WHERE id = ? AND state = 'executed' "
                                "AND remote_id = ?", (intent, new_id)).fetchone()
        if req is not None and req["request_id"]:
            self.m["requests"]["replacement"] = req["request_id"]
        self.m["transfers"][new_id] = "SENT"
        self.m["ids"]["replacement"] = new_id
        self.save()
        current = self.client.get_transfer(new_id)
        if current.status != "PAID":
            current = self.driver.reach(new_id, "SENT")
            self.driver.reach(new_id, "PAID")
        self.m["transfers"][new_id] = self.client.get_transfer(new_id).status
        return "PASS(sandbox: approved replacement executed once)"

    def _close(self) -> str:
        ob, inc, _ = self._ids()
        poll_incident(self.conn, self.client, inc, seen_at=self.now())
        status = self.conn.execute("SELECT status FROM obligation WHERE id = ?", (ob,)).fetchone()["status"]
        if status != "PAID_ONCE":
            raise HeroError("not_closed_" + str(status))
        return "PASS(sandbox: reconciled, paid once)"
