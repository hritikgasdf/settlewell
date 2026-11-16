"""Risk-tiered autonomy: the agent acts on its own when risk is low and asks a human only
when money is at risk.

The policy table below is static and code-defined. It is a read-only mapping built at import
time; nothing at runtime (agent, worker, Claude advisory output, API input) can change it.
Advisory model text is never read here. Every verdict is computed from stored ledger rows.

Auto actions still go through execute(): a single-use, terms-bound approval is consumed with
approver ``policy:<rule id>`` and execute() re-checks this module before any remote call.
SETTLEWELL_AUTONOMY=0 (the code default) restores the human-approves-everything behaviour.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from backend.policy import Action, decide

AUTO = "auto"
HUMAN = "human"
POLICY_PREFIX = "policy:"
DEFAULT_MAX_AMOUNT = Decimal(1000)
THRESHOLD_CURRENCY = "EUR"


@dataclass(frozen=True)
class PolicyRule:
    rule_id: str
    tier: str
    summary: str


# Action type -> tier. Frozen dataclasses inside a MappingProxyType: read-only at runtime.
POLICY_TABLE: MappingProxyType[str, PolicyRule] = MappingProxyType({
    "cancel": PolicyRule("A-CANCEL-SCHEDULED-DUPLICATE", AUTO,
                         "Cancel a proven duplicate that is still SCHEDULED (cancel-only, never moves money)"),
    "claim_parse": PolicyRule("A-PARSE-CLAIM", AUTO,
                              "Parse the supplier email; code validates it against the pinned record (no money)"),
    "status_check": PolicyRule("A-READ-STATUS", AUTO,
                               "Re-read transfer status and retry status checks (read-only)"),
    "return_code": PolicyRule("A-READ-RETURN-CODE", AUTO,
                              "Fetch the bank return code, e.g. 90701 (read-only)"),
    "proof": PolicyRule("A-ATTACH-PROOF", AUTO,
                        "Attach status-gated proof (read-only evidence)"),
    "wait": PolicyRule("A-WAIT-RECHECK", AUTO,
                       "WAIT rule: schedule a recheck instead of asking a person"),
    "outreach": PolicyRule("A-QUEUE-SUPPLIER-UPDATE", AUTO,
                           "Draft and queue the supplier update into the approval-gated outbox (never delivered)"),
    "beneficiary": PolicyRule("H-NEW-BANK-DETAILS", HUMAN,
                              "New bank details or beneficiary: call-back plus human approval (fraud vector)"),
    "replacement": PolicyRule("H-REPLACEMENT-PAYOUT", HUMAN,
                              "Replacement payout sends new money"),
    "fx_conversion": PolicyRule("H-FX-CONVERSION", HUMAN, "FX conversion spends money"),
})
# Kinds that move money or change who gets paid. These can never be auto-approved.
NEVER_AUTO = frozenset({"replacement", "fx_conversion", "beneficiary"})
UNKNOWN_RULE = PolicyRule("H-UNRECOGNISED", HUMAN,
                          "Not in the policy table, or the rules do not recognise it")


@dataclass(frozen=True)
class Verdict:
    tier: str
    rule_id: str
    reason: str

    @property
    def auto(self) -> bool:
        return self.tier == AUTO


def enabled(env=None) -> bool:
    source = os.environ if env is None else env
    return (source.get("SETTLEWELL_AUTONOMY") or "0").strip().lower() in ("1", "true", "on", "yes")


def max_amount(env=None) -> Decimal:
    source = os.environ if env is None else env
    raw = (source.get("SETTLEWELL_AUTO_MAX_AMOUNT") or "").strip()
    if not raw:
        return DEFAULT_MAX_AMOUNT
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return Decimal(0)  # unreadable setting: fail closed, nothing is auto
    return value if value.is_finite() and value >= 0 else Decimal(0)


def rule_for(kind: str) -> PolicyRule:
    return POLICY_TABLE.get(kind, UNKNOWN_RULE)


def _human(reason: str, rule_id: str) -> Verdict:
    return Verdict(HUMAN, rule_id, reason)


def classify_intent(conn, intent, *, env=None) -> Verdict:
    """Tier for one stored execution_intent row. Deterministic; reads ledger rows only."""
    kind = str(intent["kind"])
    rule = rule_for(kind)
    if kind in NEVER_AUTO or rule.tier != AUTO:
        return _human(rule.summary, rule.rule_id)
    if not enabled(env):
        return _human("Autonomy is off (SETTLEWELL_AUTONOMY): every step needs a person", rule.rule_id)
    if kind == "cancel":
        return _cancel_verdict(conn, intent, rule, env)
    if kind == "outreach":
        try:
            amount = Decimal(str(intent["amount"]))
        except InvalidOperation:
            return _human("Unreadable amount", UNKNOWN_RULE.rule_id)
        if amount != 0:
            return _human("Outreach must carry no money", UNKNOWN_RULE.rule_id)
        return Verdict(AUTO, rule.rule_id, rule.summary)
    return _human(UNKNOWN_RULE.summary, UNKNOWN_RULE.rule_id)


def _cancel_verdict(conn, intent, rule: PolicyRule, env) -> Verdict:
    if intent["action"] != Action.STOP_DUPLICATE.value:
        return _human("Cancel is not a STOP_DUPLICATE proposal", UNKNOWN_RULE.rule_id)
    record = None
    if intent["target_id"]:
        record = conn.execute(
            "SELECT status, amount, currency FROM transfer_record WHERE id = ?",
            (str(intent["target_id"]),),
        ).fetchone()
    if record is None:
        return _human("Duplicate transfer not on record", UNKNOWN_RULE.rule_id)
    live = conn.execute(
        "SELECT live_transfer_id FROM obligation WHERE id = ?", (intent["obligation_id"],)
    ).fetchone()
    if live is None or str(live["live_transfer_id"]) == str(intent["target_id"]):
        return _human("Target is the obligation's live payment, not a proven duplicate", UNKNOWN_RULE.rule_id)
    status = str(record["status"] or "").upper()
    if decide(status, claim="duplicate", duplicate_status=status).action is not Action.STOP_DUPLICATE:
        return _human(f"Duplicate is {status or 'unknown'}, not SCHEDULED: the rules do not allow a stop",
                      "R-NO-STOP-NOT-SCHEDULED")
    return _amount_verdict(str(intent["amount"]), str(intent["currency"]), rule, env)


def _amount_verdict(amount: str, currency: str, rule: PolicyRule, env) -> Verdict:
    limit = max_amount(env)
    try:
        value = Decimal(amount)
    except InvalidOperation:
        return _human("Unreadable amount", UNKNOWN_RULE.rule_id)
    if currency.upper() != THRESHOLD_CURRENCY:
        # No FX lookup here: a non-EUR amount has no verified EUR equivalent, so a human decides.
        return _human(f"{currency} amount has no verified {THRESHOLD_CURRENCY} equivalent", "H-OVER-THRESHOLD")
    if not value.is_finite() or value <= 0 or value > limit:
        return _human(f"Amount {currency} {value} is above the auto limit {THRESHOLD_CURRENCY} {limit}",
                      "H-OVER-THRESHOLD")
    return Verdict(AUTO, rule.rule_id, rule.summary)


def policy_approver(rule_id: str) -> str:
    return POLICY_PREFIX + rule_id


def is_policy_approver(approver: str | None) -> bool:
    return bool(approver) and str(approver).startswith(POLICY_PREFIX)


def table_view() -> list[dict]:
    return [{"action": kind, "tier": r.tier, "rule_id": r.rule_id, "summary": r.summary}
            for kind, r in POLICY_TABLE.items()]


_IN_FLIGHT = frozenset({"SCHEDULED", "PROCESSING", "SENT", "OVERDUE", "IN_APPROVAL"})


def _step(kind: str, title: str, tier: str, state: str, rule_id: str, *, at: str | None = None,
          detail: str = "", actor: str = "agent") -> dict:
    return {"kind": kind, "title": title, "tier": tier, "state": state, "rule_id": rule_id,
            "at": at, "detail": detail, "actor": actor}


def timeline(conn, obligation_id: int, *, sandbox: bool = False, env=None) -> dict:
    """Read-only 'Agent handled it' view for every incident of one obligation."""
    incidents = [int(r["id"]) for r in conn.execute(
        "SELECT id FROM incident WHERE obligation_id = ? ORDER BY id", (obligation_id,))]
    transfers = conn.execute(
        "SELECT id, status FROM transfer_record WHERE obligation_id = ? ORDER BY id", (obligation_id,)
    ).fetchall()
    tids = [int(t["id"]) for t in transfers]
    imarks = ",".join("?" * len(incidents)) or "NULL"
    tmarks = ",".join("?" * len(tids)) or "NULL"
    steps: list[dict] = []
    claimed = conn.execute(
        f"SELECT claim_json FROM incident WHERE id IN ({imarks}) AND claim_json LIKE '{{%'", incidents
    ).fetchone()
    if claimed is not None:
        r = POLICY_TABLE["claim_parse"]
        steps.append(_step("claim_parse", "Parsed the supplier claim and matched it to the pinned invoice",
                           AUTO, "done", r.rule_id))
    snaps = conn.execute(
        f"SELECT status, failure_code, seen_at FROM status_snapshot WHERE transfer_record_id IN ({tmarks}) "
        "ORDER BY id", tids).fetchall()
    if snaps:
        r = POLICY_TABLE["status_check"]
        steps.append(_step("status_check", f"Re-read transfer status ({len(snaps)} checks)", AUTO, "done",
                           r.rule_id, at=snaps[-1]["seen_at"]))
    codes = [s for s in snaps if s["failure_code"]]
    if codes:
        r = POLICY_TABLE["return_code"]
        steps.append(_step("return_code", f"Fetched bank return code {codes[-1]['failure_code']}", AUTO,
                           "done", r.rule_id, at=codes[-1]["seen_at"]))
    proofs = conn.execute(
        f"SELECT COUNT(*) AS n FROM proof_document WHERE transfer_record_id IN ({tmarks})", tids
    ).fetchone()["n"]
    if proofs:
        r = POLICY_TABLE["proof"]
        steps.append(_step("proof", f"Attached status-gated proof ({proofs})", AUTO, "done", r.rule_id))
    live = conn.execute(
        "SELECT t.status FROM obligation o JOIN transfer_record t ON t.id = o.live_transfer_id WHERE o.id = ?",
        (obligation_id,)).fetchone()
    waits = conn.execute(
        f"SELECT COUNT(*) AS n FROM decision WHERE incident_id IN ({imarks}) AND proposed_action = 'WAIT'",
        incidents).fetchone()["n"]
    live_status = str(live["status"]).upper() if live is not None else ""
    if waits or live_status in _IN_FLIGHT:
        r = POLICY_TABLE["wait"]
        steps.append(_step("wait", "WAIT rule: payment still in flight, recheck scheduled (worker polls)", AUTO,
                           "scheduled" if live_status in _IN_FLIGHT else "done", r.rule_id))
    escalations = conn.execute(
        f"SELECT rule_id FROM decision WHERE incident_id IN ({imarks}) AND proposed_action = 'ESCALATE'",
        incidents).fetchall()
    for e in escalations:
        steps.append(_step("escalate", "Rules did not recognise this case: escalated", HUMAN, "waiting",
                           str(e["rule_id"]), detail=UNKNOWN_RULE.summary, actor="human"))
    intents = conn.execute(
        f"""SELECT e.*, a.approver, a.used_at FROM execution_intent e LEFT JOIN approval a ON a.id = e.approval_id
            WHERE e.incident_id IN ({imarks}) ORDER BY e.id""", incidents).fetchall()
    for row in intents:
        steps.append(_intent_step(conn, row, sandbox=sandbox, env=env))
    auto_n = sum(1 for s in steps if s["tier"] == AUTO)
    human = [s for s in steps if s["tier"] == HUMAN]
    reasons = sorted({s["detail"] for s in human if s["detail"]})
    stamps = sorted(str(r["created_at"]) for r in conn.execute(
        f"SELECT created_at FROM audit_event WHERE incident_id IN ({imarks})", incidents))
    return {
        "enabled": enabled(env), "auto_max_amount": format(max_amount(env), "f"),
        "threshold_currency": THRESHOLD_CURRENCY, "steps": steps, "total": len(steps),
        "auto": auto_n, "human": len(human), "human_reasons": reasons,
        "first_at": stamps[0] if stamps else None, "last_at": stamps[-1] if stamps else None,
        "policy": table_view(),
    }


_TITLES = {
    "cancel": "Cancel the scheduled duplicate payment",
    "beneficiary": "Add the new bank details (beneficiary)",
    "replacement": "Send the replacement payment",
    "fx_conversion": "Convert currency to fund the replacement",
    "outreach": "Queue the supplier update in the outbox",
}


def _intent_step(conn, row, *, sandbox: bool, env) -> dict:
    kind = str(row["kind"])
    title = _TITLES.get(kind, kind)
    approver = row["approver"]
    if is_policy_approver(approver):
        rule = str(approver)[len(POLICY_PREFIX):]
        state = "done" if row["state"] == "executed" else str(row["state"])
        return _step(kind, title, AUTO, state, rule, at=row["used_at"], actor=str(approver),
                     detail=f"auto-approved by policy (rule {rule})")
    verdict = classify_intent(conn, row, env=env)
    if row["state"] == "refused":
        return _step(kind, title, HUMAN if not verdict.auto else AUTO, "refused", verdict.rule_id,
                     detail=verdict.reason, actor="human")
    if approver:
        note = verdict.reason
        if sandbox:
            note += " (hosted autorun simulates this human approval)"
        return _step(kind, title, HUMAN, "done" if row["state"] == "executed" else "approved",
                     verdict.rule_id, at=row["used_at"], detail=note, actor=str(approver))
    if verdict.auto:
        return _step(kind, title, AUTO, "auto_eligible", verdict.rule_id,
                     detail="Auto-eligible: the agent can do this on its own (or a person can approve)")
    return _step(kind, title, HUMAN, "waiting", verdict.rule_id, detail=verdict.reason, actor="human")
