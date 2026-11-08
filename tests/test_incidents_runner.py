from backend import incident_scenarios, incidents

REAL = set(range(1, 31))


def test_runner_scores_all_30_honestly(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    rows = incidents.run_all()
    assert [r.number for r in rows] == list(range(1, 31))
    assert set(incident_scenarios.SCENARIOS) == REAL
    by = {r.number: r for r in rows}
    for r in rows:
        assert r.status != "FAIL", (r.number, r.notes)
        assert r.status == "PASS" and len(r.evidence["checks"]) >= incidents.MIN_CHECKS
    # Negative rows show a refusal and zero outbound transfers; paying rows show exactly one.
    for n in (3, 6, 7, 10, 12, 15, 17, 18, 19, 21, 26, 27, 28):
        assert by[n].money_out == 0
    for n in (5, 8, 9, 11, 13, 14, 23, 24, 25, 29, 30):
        assert by[n].money_out == 1


def test_a_failed_check_is_a_failed_row():
    inc = incidents.INCIDENTS[0]
    row = incidents._real(inc, lambda: {"observed": "x", "checks": [("a", True), ("b", False), ("c", True)], "money_out": 0})
    assert row.status == "FAIL"
    thin = incidents._real(inc, lambda: {"observed": "x", "checks": [("a", True)], "money_out": 0})
    assert thin.status == "FAIL"


def test_exit_zero_only_when_all_30_pass(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    assert incidents.main(["--all"]) == 0
    assert sum(r.status == "PASS" for r in incidents.run_all()) == 30
