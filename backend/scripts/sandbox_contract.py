"""Prove create → SENT → FAILED(90701) → CANCELLED on the sandbox.

Prints pass/fail and status codes only. Set SETTLEWELL_SANDBOX=1 to call Airwallex.
"""

from backend.sandbox import run_contract_cli

if __name__ == "__main__":
    raise SystemExit(run_contract_cli())
