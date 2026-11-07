# P0-3 funding ledger: what is implemented and what remains

New payouts pass the exact-intent claim checks, a full original principal refund, a fresh cash-only balance check including the fee budget and SETTLEWELL_RESERVE_FLOOR, and (for quote-bound payouts) settled conversion verification. Existing unresolved requests recover by their stored request_id, not a new create. The payout_funding ledger records the preflight/hold and the independently observed after balance, with audit events. An unreadable after balance stays explicitly missing and is backfilled on execute/reconciliation recovery.

Reversal history traverses every cursor up to a safety ceiling of 5000 pages; a missing/repeated cursor or ceiling exhaustion raises HistoryError and never authorizes funding. Duplicate/split credits hold instead of summing. Explicit pending/unknown status holds.

## Fee budget (conservative estimate, not a bank cap)
- The Airwallex adapter has NO verified pre-send fee endpoint (public docs return fee_amount/fee_currency only on create). None is invented.
- Fallback budget, source `conservative_upper_bound_estimate`: `max(2 x live original fee, 25.00)` in the payout currency, for EUR/USD/GBP only. It is a coded operational estimate, not a bank-enforced cap, and cannot guarantee future bank pricing. The original fee must be known, finite, non-negative and in the payout currency; otherwise (and for unsupported currencies) the payout holds. There is no silent zero.
- Optional seam: a client MAY expose a read-only `quote_payout_fee(beneficiary_id, transfer_amount, transfer_currency, transfer_method)`. The gate uses it (source `adapter_fee_quote`) only if the quote matches the exact new beneficiary/amount/currency/method, has a fee in the payout currency, a provenance `source`, and an unexpired `valid_to_at`. A quote that is present but errors, is malformed, stale or for other terms HOLDS the payout (`fee` code); it never falls back. The seam is tested offline only; the real adapter does not implement it, so real runs use the fallback.
- Planning (`fund_replacement`) and the payout gate use the same fallback. A fresh balance read must still cover amount + budget + floor: for 4200.00 that is 4200.00 + 26.94 + 1000.00 = 5226.94.
- The budget and policy are audited in `payout_funding_preflight` and `ExecuteResult.funding_note`. Only the ACTUAL fee is charged: with an actual 13.47 fee the balance after the payout is floor + unused buffer (1013.47 in the replay), never a fictional debit.
- An actual fee above the budget is audited (`payout_fee_over_budget`) and the obligation is ESCALATED with no resend.

## Durable flags (reconcile)
- Refund verification is a latest-transition log (`refund_verified` / `refund_withdrawn`, ordered by row id, not as_of). Fresh contradicting history appends a bound withdrawal; outage or empty history cannot resurrect it; only a fresh full CASH credit re-verifies.
- A fee-over-budget escalation is sticky: reconcile keeps the obligation ESCALATED, never closes the incident, across restart and sibling incidents on the same obligation; outcome audits are still written. There is no automatic clear, acknowledgement or repayout.

## Resolved since the earlier WIP
Legacy fixtures now carry live original and explicit CASH data; the initial-send timeout/duplicate lookup binds exact request_id/beneficiary/amount/currency/reference and holds on an ambiguous cursor; refund verification requires a known CASH account type and rejects CREDIT/YIELD/missing.

## Remaining limits
- Accepted/unknown sends keep their hold (amount + fee budget) until a definitive first-send refusal. Debit-attribution release is deferred, so balances can be conservatively double-reserved.
- A conversion with an unknown remote id must be resumed through its own execute intent (recover by request_id); the payout gate never re-creates it.
- No live Airwallex, real money movement, webhook runtime or periodic scheduling was exercised. Tests use typed offline bank fakes.
- The immutable first-submitted timestamp is unchanged.
