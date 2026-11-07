# P0-3 funding ledger: implementation and explicit gaps

New payouts pass the exact-intent claim checks, a full original principal refund, a fresh cash-only balance check including fee budget and SETTLEWELL_RESERVE_FLOOR, and (for quote-bound payouts) settled conversion verification. Existing unresolved requests recover by their stored request_id, not a new create. The payout_funding ledger records the preflight/hold and independently observed after balance, with audit events. An unreadable after balance remains explicitly missing and can be backfilled on execute/reconciliation recovery.

Reversal history traverses every cursor up to a safety ceiling of5000pages; missing/repeated cursor or ceiling exhaustion raises HistoryError and never authorizes funding. Duplicate/split credits conservatively hold instead of summing. Posted balance history is treated as posted by the endpoint contract; explicit pending/unknown status holds.

## Not complete / do not treat as production acceptance
- Fee is a labelled original_transfer_estimate from the live original, NOT a guaranteed price for the new payout. No verified pre-send Airwallex fee endpoint is available in this adapter. Fee drift can breach the intended reserve; a known actual breach escalates/audits but cannot undo a send.
- Holds for accepted/unknown sends persist indefinitely until definitive first-send refusal. Debit-attribution release is not implemented, so current available balances can be conservatively double-reserved.
- Initial-send timeout/duplicate-request lookup still lacks the strict remote-term validation applied on later restart recovery.
- A later reconcile can overwrite the ESCALATED obligation status from a fee breach; immutable audit events remain.
- A conversion with unknown remote id must first be resumed through its own execute intent to recover by request_id. Payout gate itself never re-creates it.
- Refund verification does not currently reject an explicit non-cash history account type. Its source/amount/currency/status checks and cash-only current balance checks are separate.
- Previously verified full-refund evidence is bound to principal and fresh contradictory history withdraws its use, but withdrawal itself is not a durable tombstone. If contradictory history later disappears, older verification can be reused during reconciliation (not during a fresh payout funding gate).
- No live Airwallex, real money movement, bank fee quote, webhook runtime or periodic scheduling was exercised/built in this round. Tests use typed offline bank fakes.

Work stopped after the requested maximum two Claude calls per item; remaining gaps are reported rather than hidden by more iterations.

## Final local verification
One final full mock suite:366passed,7failed. Ruff passes. Failures are legacy bank-change/eval/replay fixtures lacking live original/cash funding data; no gate bypasses or assertions were deleted to hide them. Independent execute repros confirm the initial timeout lookup can bind a foreign transfer and non-cash reversal entries are not excluded. This branch is WIP, not ready to merge.
