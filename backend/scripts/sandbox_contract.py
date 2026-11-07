"""Prove create → SENT → FAILED(90701) → CANCELLED on the sandbox.

Prints pass/fail and status codes only. Set SETTLEWELL_SANDBOX=1 to call Airwallex.

The beneficiary and the transfer created here are not Settlewell payments.
They exist so this script can watch a sandbox payout fail with 90701. The
driver refuses those creates unless this module is on the call stack. The
execution service is not involved, and nothing here is an approval.
"""

from backend.sandbox import SandboxDriver, run_contract_cli


def run_closed_return(api, **kwargs):
    """Contract-script entry for the offline driver test. Not a payment path."""
    return SandboxDriver(api).prove_account_closed_return(**kwargs)

if __name__ == "__main__":
    raise SystemExit(run_contract_cli())
