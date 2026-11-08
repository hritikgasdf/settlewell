"""Stage coverage for the 30 incident rows and the non-money outreach handler. Offline only."""

import json

import pytest

from backend import incidents
from backend.claims import parse_email
from backend.incident_world import make_world

WHERE = "Where is the payment for INV-2291? EUR 4200.00 never arrived."


def _m(**f):
    base = {"invoice_ref": "INV-2291", "amount": "4200.00", "currency": "EUR",
            "asks_where": True, "asks_proof": False, "asks_new_account": False}
    return lambda _e: json.dumps({**base, **f})


@pytest.fixture(autouse=True)
def _mock(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")


def test_every_pass_row_has_real_evidence_of_its_stages():
    rows = {r.number: r for r in incidents.run_all()}
    for n, r in rows.items():
        assert r.status == "PASS" and all(ok for _, ok in r.evidence["checks"]), n
        assert len(r.evidence["checks"]) >= 3, n
    # Claim-stage rows end in a genuine execute() escalation, not a zero-outbound boolean.
    for n in (15, 17, 19):
        text = " ".join(label for label, _ in rows[n].evidence["checks"])
        assert "escalation ran through execute()" in text, n
    for n in (1, 2, 4, 20, 22):
        text = " ".join(label for label, _ in rows[n].evidence["checks"])
        assert "queued" in text and "unapproved" in text, n


@pytest.mark.parametrize("conf,rule", [(0.3, "R-LOW-CONFIDENCE"), (0.79, "R-LOW-CONFIDENCE"),
                                       (True, "R-BAD-JSON"), ("0.9", "R-BAD-JSON"),
                                       (1.01, "R-BAD-JSON"), (-0.1, "R-BAD-JSON"),
                                       (0.8, "R-MATCHED"), (1, "R-MATCHED")])
def test_confidence_rules(conf, rule):
    w = make_world()
    assert parse_email(WHERE, _m(confidence=conf), w.conn, supplier_id=w.supplier_id).rule_id == rule


def test_confidence_nan_and_missing():
    w = make_world()
    nan = ('{"invoice_ref":"INV-2291","amount":"4200.00","currency":"EUR","asks_where":true,'
           '"asks_proof":false,"asks_new_account":false,"confidence":NaN}')
    assert parse_email(WHERE, lambda _e: nan, w.conn, supplier_id=w.supplier_id).rule_id == "R-BAD-JSON"
    legacy = parse_email(WHERE, _m(), w.conn, supplier_id=w.supplier_id)
    assert legacy.rule_id == "R-MATCHED" and legacy.claim.confidence is None


def test_outreach_needs_approval_and_exact_body():
    w = make_world(status="SENT")
    _, result, error = w.outreach("ESCALATE", "[ESCALATION R-X] hold", approver=None)
    assert result is None and error == "unapproved" and w.outbox_bodies() == []
    _, result, error = w.outreach("ESCALATE", "no prefix")
    assert result is None and error == "body"
    _, result, error = w.outreach("SEND_MONEY", "[ESCALATION R-X]")
    assert result is None and error is not None and w.remote.posts == []


def _approved(w, action, body):
    from backend.execute import approve_intent
    from backend.incident_outreach import propose_outreach
    from backend.incident_world import AP_CLERK, EXPIRES, FINANCE_APPROVER, SEEN

    ref = propose_outreach(
        w.conn, incident_id=w.incident_id, obligation_id=w.obligation_id, supplier_id=w.supplier_id,
        transfer_record_id=w.transfer_record_id(), action=action, body=body,
        currency="EUR", proposer=AP_CLERK, expires_at=EXPIRES, created_at=SEEN)
    approve_intent(w.conn, ref.id, approver=FINANCE_APPROVER, as_of=SEEN)
    return ref


def test_outreach_replay_returns_same_row_and_queues_once():
    w = make_world(status="SENT")
    ref, first, error = w.outreach("ESCALATE", "[ESCALATION R-X] hold")
    assert error is None and first is not None
    again, err2 = w.run(ref.id)
    assert err2 is None and again.remote_id == first.remote_id
    assert len(w.outbox_bodies()) == 1
    assert w.audit_kinds().count("incident_escalated") == 1
    assert w.remote.posts == []


def test_modified_body_after_approval_is_void_and_nothing_is_queued():
    w = make_world(status="SENT")
    ref = _approved(w, "ESCALATE", "[ESCALATION R-X] original")
    w.conn.execute("UPDATE execution_intent SET body_json = ? WHERE id = ?",
                   (json.dumps({"body": "[ESCALATION R-X] tampered"}), ref.id))
    w.conn.commit()
    result, error = w.run(ref.id)
    assert result is None and error is not None
    assert w.outbox_bodies() == [] and w.intent_state(ref.id) != "executed"


def test_changed_recipient_after_approval_is_refused():
    w = make_world(status="SENT")
    ref = _approved(w, "ESCALATE", "[ESCALATION R-X] hold")
    w.conn.execute("UPDATE execution_intent SET beneficiary_id = 'supplier:999' WHERE id = ?", (ref.id,))
    w.conn.commit()
    result, error = w.run(ref.id)
    assert result is None and error is not None and w.outbox_bodies() == []


def test_stale_status_after_approval_refuses_reply():
    w = make_world(status="SENT")
    ref = _approved(w, "REPLY_WAIT", "It is on its way.")
    from backend.incident_world import ORIGINAL

    w.remote.set_status(ORIGINAL, "CANCELLED")  # PAID is still a policy WAIT; CANCELLED is not
    result, error = w.run(ref.id)
    assert result is None and error is not None and w.outbox_bodies() == []


def test_crash_after_claim_resumes_and_queues_once(monkeypatch):
    from backend import incident_outreach

    w = make_world(status="SENT")
    real = incident_outreach.draft_reply

    def boom(*a, **k):
        raise RuntimeError("crash")

    monkeypatch.setattr(incident_outreach, "draft_reply", boom)
    ref, result, error = w.outreach("ESCALATE", "[ESCALATION R-X] hold")
    assert result is None and error == "RuntimeError" and w.outbox_bodies() == []
    monkeypatch.setattr(incident_outreach, "draft_reply", real)
    resumed, err = w.run(ref.id)
    assert err is None and resumed is not None
    assert len(w.outbox_bodies()) == 1 and w.audit_kinds().count("incident_escalated") == 1
    _, err = w.run(ref.id)
    assert err is None and len(w.outbox_bodies()) == 1
