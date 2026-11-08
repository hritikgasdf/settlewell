"""A grant voided by changed terms stays void even if the terms are put back."""

from backend.incident_scenarios import _replace_same
from backend.incident_world import bounce, make_world


def test_void_grant_is_permanent(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    w = make_world(status="SENT")
    bounce(w)
    ref = _replace_same(w)
    w.conn.execute("UPDATE execution_intent SET beneficiary_id = 'ben_swapped' WHERE id = ?", (ref.id,))
    assert w.run(ref.id) == (None, "void")
    w.conn.execute("UPDATE execution_intent SET beneficiary_id = 'ben_onboard' WHERE id = ?", (ref.id,))
    assert w.run(ref.id) == (None, "refused")
    assert w.remote.posts == []
