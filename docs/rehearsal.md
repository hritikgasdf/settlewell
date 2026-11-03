# Sandbox rehearsal

Two runs on `api-demo.airwallex.com` with the adapter's pinned `x-api-version` `2025-11-11`. `reset_demo` ran before each one. Each payment was 20.00 EUR over SWIFT. No credentials are recorded here.

Both finished runs passed, each in about 32 seconds.

The floor check returned `ready` on both runs. The sandbox EUR balance already covered the 20.00 payment plus the 1,000.00 reserve floor, so no quote was created and no conversion was sent.

## Before the two clean runs

- `reset` on a fresh database failed in 3029 ms. HTTP status: none. Error: sqlite `OperationalError` (`no such table: transfer_record`). `reset_demo` listed transfers before migrating. It now migrates first.
- One later `reset` failed in 31622 ms. HTTP status: none. Error: `TimeoutError`. The next reset passed. No code change.

## Run 1

Total 32102 ms. Pass.

| Step | Result | ms |
|---|---|---|
| base | pass | 0 |
| reset | pass | 2600 |
| beneficiary | pass | 1620 |
| original_sent | pass | 1856 |
| ledger | pass | 2 |
| proof_sent | pass | 2638 |
| duplicate_scheduled | pass | 749 |
| stop_duplicate | pass | 3561 |
| failure_90701 | pass | 2485 |
| proof_refused | pass | 351 |
| bank_change | pass | 910 |
| refund | pass | 889 |
| fx_floor | pass | 815 |
| approval | pass | 571 |
| replacement | pass | 7726 |
| ledger_paid | pass | 4021 |

`stop_duplicate` ended CANCELLED with no EUR balance-history movement. Proof attached while SENT (`R-PROOF-SENT`) and was refused after cancel. `90701` decided `R-REPLACE_NEW_DETAILS-90701`. The bank change created a new beneficiary. The refund card unlocked. Self-approval was rejected and the finance approver approved. The second replacement was refused. The ledger obligation ended `PAID_ONCE`. No non-synthetic transfer was left `SENT`.

## Run 2

Total 31880 ms. Pass.

| Step | Result | ms |
|---|---|---|
| base | pass | 0 |
| reset | pass | 3567 |
| beneficiary | pass | 1823 |
| original_sent | pass | 1964 |
| ledger | pass | 2 |
| proof_sent | pass | 2436 |
| duplicate_scheduled | pass | 1103 |
| stop_duplicate | pass | 2990 |
| failure_90701 | pass | 2478 |
| proof_refused | pass | 542 |
| bank_change | pass | 1145 |
| refund | pass | 693 |
| fx_floor | pass | 821 |
| approval | pass | 350 |
| replacement | pass | 7442 |
| ledger_paid | pass | 3492 |

Same end state as run 1: duplicate CANCELLED with no balance-history movement, proof refused after cancel, one replacement, obligation `PAID_ONCE`, and no non-synthetic `SENT` transfer left.
