# Sandbox rehearsal

Two runs on `api-demo.airwallex.com` with the adapter's pinned `x-api-version` `2025-11-11`. `reset_demo` ran before each one. Each payment was 20.00 EUR over SWIFT. No credentials are recorded here.

The rehearsal sets `SETTLEWELL_RESERVE_FLOOR` for that process only, so the EUR shortfall is 100.00. That is under the 10,000 limit. The default floor stays 1,000.00 when the variable is unset. Both runs locked one USD→EUR quote, sent one conversion, and rejected a second claim on that quote.

Both finished runs passed. Run 1 took 33886 ms. Run 2 took 37876 ms.

## Attempts before these two runs

- `original_sent` failed twice with HTTP 400, code `fee_unknown`. A later create passed. No code change.
- The first FX attempt's second run failed at `fx_floor` in 1425 ms. HTTP 400, code `invalid_argument`. Both fresh databases reused `stl-fx-` plus the same local obligation id, so the second conversion was not a new request. The conversion request id now comes from the quote, and a repeat of that quote stays the same id.

## Run 1

Total 33886 ms. Pass.

| Step | Result | ms |
|---|---|---|
| base | pass | 0 |
| reset | pass | 3443 |
| beneficiary | pass | 1818 |
| original_sent | pass | 1936 |
| ledger | pass | 2 |
| proof_sent | pass | 2407 |
| duplicate_scheduled | pass | 640 |
| stop_duplicate | pass | 3042 |
| failure_90701 | pass | 2724 |
| proof_refused | pass | 342 |
| bank_change | pass | 893 |
| refund | pass | 720 |
| fx_floor | pass | 2121 |
| approval | pass | 571 |
| replacement | pass | 7603 |
| ledger_paid | pass | 4584 |

`fx_floor` locked one quote, recorded one conversion, and a second claim on that quote was rejected. The quote ended `CONSUMED`. Balance snapshots are before and after. The duplicate ended CANCELLED with no EUR balance-history movement. Proof attached while SENT and was refused after cancel. `90701` decided `R-REPLACE_NEW_DETAILS-90701`. Self-approval was rejected. The ledger obligation ended `PAID_ONCE`. No non-synthetic transfer was left `SENT`.

## Run 2

Total 37876 ms. Pass.

| Step | Result | ms |
|---|---|---|
| base | pass | 0 |
| reset | pass | 3578 |
| beneficiary | pass | 1717 |
| original_sent | pass | 1671 |
| ledger | pass | 2 |
| proof_sent | pass | 2799 |
| duplicate_scheduled | pass | 957 |
| stop_duplicate | pass | 3816 |
| failure_90701 | pass | 3791 |
| proof_refused | pass | 368 |
| bank_change | pass | 852 |
| refund | pass | 694 |
| fx_floor | pass | 2354 |
| approval | pass | 570 |
| replacement | pass | 9091 |
| ledger_paid | pass | 4592 |

Same end state as run 1: one consumed quote, one conversion, reuse rejected, obligation `PAID_ONCE`, and no non-synthetic `SENT` transfer left.

## Live proof run, 2026-11-04

One more run, same host and pinned version, `reset_demo` first, `SETTLEWELL_RESERVE_FLOOR` set so the shortfall was 100.00 EUR. Payment 20.00 EUR SWIFT. Total 37088 ms. Pass. Ids and final statuses:

| What | Id | Final status |
|---|---|---|
| Original transfer | `856ef116-4754-4bb4-870c-2fd461751da4` | Failed 90701 (account closed); Airwallex status CANCELLED |
| Cancelled duplicate | `78e1501e-0b9b-4544-984a-5111da33e5cb` | CANCELLED |
| Replacement transfer | `a9ec4848-8322-4c3b-b006-d5ac730459e9` | PAID |
| FX quote | `c4bd4ef1-afaf-3314-9269-0937af396e19` | CONSUMED |
| Conversion | `62f19891-3aca-46f2-9640-8d76ad349126` | SETTLED |

The quote status is Settlewell's row. The conversion status is the Airwallex conversion status. The duplicate had no EUR balance-history movement. A second claim on the quote was rejected. The obligation ended `PAID_ONCE`.
