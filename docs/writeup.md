# Settlewell (formerly "Where's My Money?")

Outline and draft for the HackerEarth submission. Facts are from this repo and from `docs/rehearsal.md`.

## Outline

1. Problem and who it is for
2. What a transfer-only agent does not do
3. Hero flow, and what is live versus mock
4. Live sandbox proof
5. Locked quote as supporting safety
6. Airwallex APIs
7. Safety
8. Honest limits
9. Eval: 30/30 and the no-LLM baseline
10. What Hritik does in person

## Draft

### Problem

A supplier writes that a payment never arrived, and sometimes asks to be paid again to a new account. An agent that only replaces one failed transfer still misses that incident. It does not see a second payment a colleague scheduled, it does not refuse a proof PDF once the transfer is cancelled, and it does not keep its own history of the supplier's bank details behind a call-back.

Settlewell (formerly "Where's My Money?") handles the incident from that email through to closure. It matches the email to an invoice, reads the transfer, and proposes one next step: wait, prove, trace, stop a duplicate, replace, or escalate. Amounts are copied from Airwallex's record. A second person approves anything that moves money.

The safety core follows Airwallex's kit 3 reference pattern: amounts copied from Airwallex's record, one replacement per failure, approver ≠ proposer, and closure with both dispositions, plus kit 1's single-use quote lock. The reserve floor and the locked USD→EUR quote are part of that safety. They are not the headline. The headline is the supplier incident: cancel a still-scheduled duplicate at €0, gate the proof PDF on status, decide from Airwallex failure codes, and version the bank details.

### Who it is for

The two demo identities are `ap-clerk` and `finance-approver`. The clerk proposes. The finance approver approves. The same person cannot approve their own proposal. The supplier in the seeded hero is Lumen Textiles GmbH, invoice INV-2291, 4200.00 EUR. That supplier and those emails are synthetic.

### Hero flow

The seeded story is INV-2291. The offline replay at `/replay`, labelled MOCK, walks it with no network: a colleague's scheduled duplicate is cancelled with no balance movement, proof attaches while the original is SENT and is refused after cancel, `90701` decides `R-REPLACE_NEW_DETAILS-90701`, the bank change is Commerzbank to a new DKB beneficiary, the refund card unlocks before replacement, USD→EUR uses a locked quote, a second claim on that quote is rejected, and the ledger shows INV-2291 paid once.

The live rehearsal on `api-demo.airwallex.com` used the adapter's pinned `x-api-version` `2025-11-11`. `reset_demo` ran before each run. Each live payment was 20.00 EUR over SWIFT, not the 4200.00 seed. Run 1 took 33886 ms. Run 2 took 37876 ms. Both passed. Each duplicate ended CANCELLED with no EUR balance-history movement. Proof attached while SENT and was refused after cancel. `90701` decided `R-REPLACE_NEW_DETAILS-90701`. Self-approval was rejected. One replacement was created and the second was refused. The obligation ended `PAID_ONCE`. No non-synthetic transfer was left `SENT`.

`uv run python -m settlewell` serves `http://127.0.0.1:8765/` in mock mode with no credentials. Sandbox mode is `SETTLEWELL_SANDBOX=1`. That console reads `SETTLEWELL_DB` (default `var/settlewell.db`) and does not call Airwallex from page handlers.

### What is live vs mock

| Surface | Which |
|---|---|
| `/replay`, and any screen that uses canned statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL`, or a canned quote rate | MOCK |
| The seeded INV-2291 console beats (4200.00 EUR, synthetic ids) | MOCK |
| Eval harness (`SETTLEWELL_MOCK=1`) | Offline. It does not call Airwallex |
| The 2026-11-04 sandbox run: create, SENT, proof, scheduled duplicate cancel, `90701`, proof refused after cancel, new beneficiary, reversal, locked USD→EUR quote, one conversion, replacement PAID | Live on `api-demo.airwallex.com` |
| Sandbox console pages | Read the local ledger only. They do not call Airwallex |

### Live sandbox proof

On 2026-11-04 one hero run on `api-demo.airwallex.com` used `x-api-version` `2025-11-11` and `SETTLEWELL_RESERVE_FLOOR` so the EUR shortfall was 100.00. The payment was 20.00 EUR over SWIFT. `reset_demo` ran first. The run took 37088 ms and passed. The quote status is Settlewell's row. The conversion status is Airwallex's.

| What | Id | Final status |
|---|---|---|
| Original transfer | `856ef116-4754-4bb4-870c-2fd461751da4` | CANCELLED |
| Cancelled duplicate | `78e1501e-0b9b-4544-984a-5111da33e5cb` | CANCELLED |
| Replacement transfer | `a9ec4848-8322-4c3b-b006-d5ac730459e9` | PAID |
| FX quote | `c4bd4ef1-afaf-3314-9269-0937af396e19` | CONSUMED |
| Conversion | `62f19891-3aca-46f2-9640-8d76ad349126` | SETTLED |

A second claim on quote `c4bd4ef1-afaf-3314-9269-0937af396e19` was rejected. The same table is in the README and in `docs/rehearsal.md`.

### What a transfer-only agent does not do

**Cancel a colleague's still-scheduled duplicate at €0.** The scan is account-wide: another live transfer with the same invoice, amount, and currency. While that transfer is `SCHEDULED`, the action is `STOP_DUPLICATE` (`R-STOP-DUPLICATE-SCHEDULED`). The clerk proposes and the finance approver approves. Cancel does not post a ledger fee. On the 2026-11-04 sandbox run the duplicate `78e1501e-0b9b-4544-984a-5111da33e5cb` ended CANCELLED, and the EUR balance history had no movement for it.

**A status-gated proof PDF.** `issue_proof` asks for a `NO_FEE_DISPLAY` confirmation letter only while the live status is SENT or PAID. The rule while SENT is `R-PROOF-SENT`. After cancel, the same request is refused and the reply says the transfer is cancelled. The mock page's ETA and cut-off are labels, not Airwallex API fields. The live run attached proof while SENT and refused it after cancel.

**Decisions from Airwallex failure codes.** Policy is a table of 29 published codes. The numeric code comes from `failure_reason`. `90701` (account closed) becomes `REPLACE_NEW_DETAILS` and `R-REPLACE_NEW_DETAILS-90701`. `91001` (recall) escalates (`R-ESCALATE-91001`). The adapter pins `x-api-version` `2025-11-11` so the reason is still on a later GET after CANCELLED. The live run took that `90701` path.

**The incident, from the supplier's email through to closure.** The email is matched to an invoice. The incident stays open while the transfer is in flight, while proof is issued or refused, while a duplicate is stopped, and while a replacement waits on a refund and an approval. It closes when the ledger says the invoice was paid once. The live obligation ended `PAID_ONCE`.

**Supplier bank-detail version history, with a call-back.** New details are validated. A call-back to the phone on file is required. Settlewell then creates a new beneficiary and leaves the previous one as it was. The card shows the bank-name and last-four diff. No call-back is `R-BANK-CALLBACK-REQUIRED` in the harness. Unverified details are not paid. The live run created that new beneficiary before the replacement.

### Locked quote, as supporting safety

Before a EUR replacement, Settlewell compares the EUR balance with the payment plus a reserve floor. The default floor is 1,000.00. A top-up above 10,000.00 is not sent; the harness reports that as `R-FX-LIMIT-ESCALATE`. Under the limit, it creates a USD→EUR quote with validity `MIN_15`, locks the quote, and converts once. The quote id is part of the approval hash. Sandbox quotes can be multi-use; Settlewell marks its own row `CONSUMED` with one `UPDATE` where `state = 'LOCKED'` before `create_conversion`, and a second claim is rejected. An expired quote is replaced and must be approved again (`R-FX-QUOTE-EXPIRED`).

The default floor does not fire on the pre-funded sandbox, so the rehearsal sets `SETTLEWELL_RESERVE_FLOOR` for that process only. The shortfall was 100.00 EUR, under the 10,000 limit. The 2026-11-04 run locked quote `c4bd4ef1-afaf-3314-9269-0937af396e19`, sent conversion `62f19891-3aca-46f2-9640-8d76ad349126` (`SETTLED`), and rejected a second claim. Earlier recorded runs did the same without these ids in the note. If the variable is not a positive number, `reserve_floor` raises `FundingError` with the message `SETTLEWELL_RESERVE_FLOOR must be a positive number`.

A replacement is not proposed until a `PAYOUT_REVERSAL` whose source is the original transfer is visible. The synthetic hero card reads `principal back: €4,200 · fee lost €13.47`. The live rehearsal unlocked the refund card; the fee on that 20.00 EUR transfer is not copied here because `docs/rehearsal.md` does not record it.

### Airwallex APIs

Calls go to `https://api-demo.airwallex.com` with `x-api-version: 2025-11-11`. The adapter uses:

- `POST /api/v1/authentication/login`
- `GET /api/v1/balances/current` and `GET /api/v1/balances/history`
- `GET /api/v1/transfers`, `GET /api/v1/transfers/{id}`, `POST /api/v1/transfers/create`, and `POST /api/v1/transfers/{id}/cancel`
- `POST /api/v1/beneficiaries/validate` and `POST /api/v1/beneficiaries/create`
- `POST /api/v1/confirmation_letters/create`
- `POST /api/v1/fx/quotes/create`, `GET /api/v1/fx/quotes/{id}`, and `POST /api/v1/fx/conversions/create`
- `POST /api/v1/webhooks/create` for the payout events, including failed and funding events

Sandbox-only simulation lives in `backend/sandbox.py`: `POST /api/v1/simulation/transfers/{id}/transition`. `reset_demo` refuses any host other than `api-demo.airwallex.com`. It releases local non-synthetic `SENT` transfers and also lists remote `SENT` transfers, at most five pages, then moves those off `SENT` through that simulation path.

### Safety

The outbox holds drafted replies until the finance approver uses the approval. Nothing in the outbox is delivered to a supplier. There is no `sent_at`.

Approvals bind a hash of the action, amount, currency, beneficiary, details hash, quote id, and evidence. A changed detail voids the grant. A second use is rejected. The approver must not be the proposer.

One replacement per failure: the request id is `stl-<invoice>-<attempt>`, the next attempt waits until the previous one is a definitive refusal, and an unknown timeout stays in flight instead of being sent again under a new id.

Mock mode is the default (`SETTLEWELL_MOCK=1` when you start `python -m settlewell` without sandbox mode). The eval harness refuses to run unless that flag is set. Page handlers do not call Airwallex. Webhook delivery checks an HMAC over the timestamp and the raw body, drops stale timestamps, and inserts the event id first so a replay hits the unique constraint and is not applied twice.

### Honest limits

The mock replay uses canned transfer statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL`, and a canned quote rate. The fidelity table is on `/replay` and in `backend/mock_replay.py`. ETA and the cut-off are labels, not API fields. A trace is a drafted support request. Settlewell does not retrieve an MT103 or a UETR. Cancelling a `PROCESSING` transfer is not a behavior this demo claims.

The live EUR rail in the rehearsal was SWIFT. The payment behaviour is frozen: no new actions, incidents, or console pages. A broken demo run can still be fixed.

### Eval

`SETTLEWELL_MOCK=1 uv run python -m backend.scripts.eval_harness` prints one line per incident and then `30/30`. Without the flag it prints `eval skipped`.

The no-LLM baseline is a static table in the README, not a second model. Without the policy engine, every incident is `ESCALATE`. Settlewell's column is the harness expectation: for example late SENT waits (`R-NO-REPLACE-IN-FLIGHT`), a scheduled duplicate stops (`R-STOP-DUPLICATE-SCHEDULED`), account-closed new details replace (`R-REPLACE_NEW_DETAILS-90701`), a non-JSON or smuggled payment escalates (`R-BAD-JSON`), and the hero composite passes (`R-HERO`). Case 24 reports `R-FX-FLOOR-CONVERT` under the limit and `R-FX-LIMIT-ESCALATE` over it.

### What Hritik does in person

- Item 41. Record the rough-cut video and the side clips.
- Item 43. Record the final video, including voice-over and captions, on the real sandbox, and keep the backup.
- Item 44. Grant judges access to the repo. The final writeup pass can follow this draft; access cannot.
- Item 45. Dry-run the submission form and submit.
