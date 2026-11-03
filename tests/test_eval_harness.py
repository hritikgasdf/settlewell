"""The first 15 incidents pass offline. No network."""

from backend.eval_cases import CASES
from backend.eval_harness import EvalError, _score, main, run_first_15
from backend.eval_rest import run_remaining_15


def test_the_harness_refuses_to_run_outside_mock(monkeypatch):
    monkeypatch.delenv("SETTLEWELL_MOCK", raising=False)
    try:
        run_first_15()
    except EvalError as error:
        assert error.code == "mock"
    else:
        raise AssertionError("mock")


def test_the_first_15_match_their_rule_and_action(monkeypatch, capsys):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    results = run_first_15()
    assert [result.number for result in results] == list(range(1, 16))
    for result in results:
        assert result.passed, (result.number, result.name, result.action, result.rule_id)
        assert result.invariants_passed, result.number
    assert main() == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert lines[-1] == "30/30"
    assert all(line.split()[-3] == "pass" for line in lines[:-1])
    remaining = run_remaining_15()
    assert [result.number for result in remaining] == list(range(16, 31))
    for result in remaining:
        assert result.passed, (result.number, result.name, result.action, result.rule_id)
        assert result.invariants_passed, result.number


def test_a_wrong_expected_rule_is_reported_as_fail(monkeypatch):
    monkeypatch.setenv("SETTLEWELL_MOCK", "1")
    case = dict(CASES[0])
    case["expected_rule"] = "R-NOT-A-RULE"
    result = _score(case)
    assert result.passed is False
    assert result.rule_id == "R-NO-REPLACE-IN-FLIGHT"
