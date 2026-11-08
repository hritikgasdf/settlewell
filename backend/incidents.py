"""P1-7 runner: score the 30 catalog incidents. Offline MOCK only, no Airwallex, no secrets.

PASS needs a scenario in backend.incident_scenarios whose assertions all held, run through
claim -> pinned match -> decision -> intent -> approval -> execute -> reconcile against the
stateful mock remote. Rows still on a legacy fixture are PARTIAL. Exit code is nonzero unless
every row is PASS.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from backend.incident_catalog import INCIDENTS
from settlewell.flags import mock_mode

TABLE_PATH = Path("/workspace/settlewell-ctx/incidents-table.md")
MIN_CHECKS = 3


@dataclass(frozen=True)
class Scenario:
    number: int
    title: str
    status: str  # PASS / FAIL / PARTIAL / STUB
    expected: str
    observed: str
    money_out: int
    notes: str = ""
    evidence: dict = field(default_factory=dict)


def _real(inc: dict, run) -> Scenario:
    n, expected = inc["number"], inc["variants"][0]["expected_action"]
    try:
        out = run()
    except Exception as error:  # noqa: BLE001 - a crash is a FAIL row, never a pass
        return Scenario(n, inc["title"], "FAIL", expected, f"crash {type(error).__name__}: {error}", 0)
    checks = out["checks"]
    failed = [label for label, ok in checks if not ok]
    ok = not failed and len(checks) >= MIN_CHECKS
    note = f"{len(checks)} checks" + (f"; {out['note']}" if out.get("note") else "") + (f"; failed: {'; '.join(failed)}" if failed else "")
    return Scenario(n, inc["title"], "PASS" if ok else "FAIL", expected, out["observed"],
                    out["money_out"], note, {"checks": checks})


def run_all() -> tuple[Scenario, ...]:
    if not mock_mode():
        raise RuntimeError("SETTLEWELL_MOCK=1 required")
    from backend.eval_harness import run_first_15
    from backend.eval_rest import run_remaining_15
    from backend.incident_scenarios import SCENARIOS

    legacy = {r.number: r for r in (*run_first_15(), *run_remaining_15())}
    rows = []
    for inc in INCIDENTS:
        n = inc["number"]
        if n in SCENARIOS:
            rows.append(_real(inc, SCENARIOS[n]))
            continue
        leg = legacy[inc["legacy"]["number"]]
        gaps = "; ".join(inc["gaps"]) or "none listed"
        status = "FAIL" if not leg.passed or not leg.invariants_passed else "PARTIAL"
        rows.append(Scenario(
            n, inc["title"], status, inc["variants"][0]["expected_action"],
            f"legacy {leg.action}/{leg.rule_id}", 0, f"legacy fixture only; gaps: {gaps}",
        ))
    return tuple(rows)


def render(rows: tuple[Scenario, ...]) -> str:
    out = ["| # | Title | Status | Expected | Observed | Transfers out | Notes |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        note = r.notes.replace("|", "/")
        out.append(f"| {r.number} | {r.title} | {r.status} | {r.expected} | {r.observed} | {r.money_out} | {note} |")
    counts = {s: sum(r.status == s for r in rows) for s in ("PASS", "PARTIAL", "STUB", "FAIL")}
    out += ["", "Counts: " + ", ".join(f"{k}={v}" for k, v in counts.items())]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if "--all" not in args:
        print("usage: python -m backend.incidents --all")
        return 2
    rows = run_all()
    table = render(rows)
    print(table)
    if TABLE_PATH.parent.exists():
        TABLE_PATH.write_text(table)
    return 0 if all(r.status == "PASS" for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
