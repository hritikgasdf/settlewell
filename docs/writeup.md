# Settlewell (formerly Where's My Money?)

Airwallex Agentic Banking Hackathon, Starter Kit 03: Payment Ops Incident Commander.

- **Live sandbox** (real Airwallex sandbox + Claude): https://settlewell-sandbox.onrender.com
- **Mock demo** (resettable): https://settlewell-demo.onrender.com
- **Passcode:** shared with judges privately. First load may take ~1 minute (free-tier cold start).
- **Demo video:** [VIDEO LINK — add before submitting]
- **Code:** https://github.com/hritikgasdf/settlewell (setup in the README)

### Problem

A supplier writes that a payment never arrived, and sometimes asks to be paid again to a new account. An agent that only replaces one failed transfer still misses that incident. It does not see a second payment a colleague scheduled, it does not refuse a proof PDF once the transfer is cancelled, and it does not keep its own history of the supplier's bank details behind a call-back.

Settlewell handles the incident from that email through to closure. It matches the email to an invoice, reads the transfer, and proposes one next step: wait, prove, trace, stop a duplicate, replace, or escalate. Amounts are copied from Airwallex's record. A second person approves anything that moves money; low-risk, cancel-only and read-only steps are handled by the agent under a fixed policy.

The safety core follows Airwallex's documented reference pattern: amounts copied from Airwallex's record, one replacement per failure, approver ≠ proposer, and closure with both dispositions, plus its documented single-use quote lock for FX conversions. The reserve floor and the locked USD→EUR quote are part of that safety. They are not the headline. The headline is the supplier incident: cancel a still-scheduled duplicate at €0, gate the proof PDF on status, decide from Airwallex failure codes, and version the bank details.

### Agentic flow in one line

Supplier email → Claude parses the claim → deterministic rules diagnose (duplicate, 90701 bank return, changed details) → the agent handles low-risk steps itself → a human approves only money-risky steps → everything is audited.

### AI for understanding, rules for money

Claude (Haiku, `claude-haiku-4-5` by default) has four touchpoints. None of them can move money.

1. **Claim parser** (`backend/claims.py`): reads the supplier's email and proposes JSON. A strict validator and the pinned supplier record accept it or escalate.
2. **Incident explainer** (`backend/advisory.py`): writes a plain-English summary of the incident and why the deterministic rules chose the recorded action.
3. **Supplier reply draft**: an editable draft in the console. It is never sent; replies stay queued in the outbox until a person approves them.
4. **Unknown-incident triage**: when no playbook matched (an escalation or unknown rule), Claude suggests one playbook name from a fixed list with a confidence. It is a suggestion only; nothing executes from it.

The three advisory outputs are plain strings served by a read-only GET (`/api/incidents/{id}/advisory`). No money module imports `backend/advisory.py` (a test enforces this), the endpoint leaves the ledger byte-for-byte unchanged, and every money step still goes through `execute()` with a bound, single-use human approval. In MOCK mode, without `ANTHROPIC_API_KEY`, or on any error or timeout, the console shows a deterministic template, and each section is labelled with its source (Claude, rules engine or template). Results are cached per incident so page loads do not re-call the model.

### Risk-tiered autonomy: the agent acts on its own when risk is low and asks a human only when money is at risk

A fixed, code-defined policy table (`backend/autonomy.py`, a read-only mapping) labels every action type
"auto" or "human". Nothing at runtime can change it: not the agent, not the worker, not Claude.
Claude's advisory text is never read by this module.

| Action | Who | Rule |
|---|---|---|
| Cancel a proven duplicate that is still SCHEDULED (cancel-only, never moves money) | Agent | A-CANCEL-SCHEDULED-DUPLICATE |
| Parse the supplier email (code validates against the pinned record) | Agent | A-PARSE-CLAIM |
| Re-read transfer status, retry status checks | Agent | A-READ-STATUS |
| Fetch the bank return code (e.g. 90701) | Agent | A-READ-RETURN-CODE |
| Attach status-gated proof | Agent | A-ATTACH-PROOF |
| WAIT rule: schedule a recheck instead of asking | Agent | A-WAIT-RECHECK |
| Draft and queue the supplier update into the approval-gated outbox (never delivered) | Agent | A-QUEUE-SUPPLIER-UPDATE |
| New bank details / new beneficiary (fraud vector: call-back + approval) | Human | H-NEW-BANK-DETAILS |
| Replacement payout | Human | H-REPLACEMENT-PAYOUT |
| Any FX conversion | Human | H-FX-CONVERSION |
| Any amount above `SETTLEWELL_AUTO_MAX_AMOUNT` (code default EUR 1,000; non-EUR amounts always go to a human) | Human | H-OVER-THRESHOLD |
| Anything the rules do not recognise (unknown kind, ESCALATE, duplicate not SCHEDULED) | Human | H-UNRECOGNISED |

Auto steps still go through `execute()`: the same single-use, terms-bound approval is consumed with approver
`policy:<rule id>`, the audit log says `auto-approved by policy (rule X)`, and `execute()` re-checks the table
before any remote call, so a forged `policy:` approval on a payout, FX or beneficiary intent is refused.
`SETTLEWELL_AUTONOMY=0` (the code default) restores human approval for every step. The hosted demos set
autonomy on and the auto limit to EUR 5,000 (so the EUR 4,200 scheduled duplicate qualifies) unless the host
env says otherwise. The incident page shows an "Agent handled it" timeline and a counter, "N of M steps
handled automatically, K needed a human (reasons)". In the hosted sandbox autorun, the human-gated steps
(bank change, FX, replacement) are approved by a scripted `finance-approver`, and the console labels them
"hosted autorun simulates this human approval".

### Benefits

- **Fewer human steps.** On the live hosted sandbox hero run the agent handled **5 of 7 steps** by itself (read the claim, check status, fetch the bank return code, attach proof, cancel the scheduled duplicate before any money left). Only the 2 money-risky steps, the new bank details and the replacement payment, waited for a human. (No FX top-up was needed on that run; when the reserve floor requires one, the FX conversion is a further human-approved step.) The whole run took about **43 seconds**.
- **Time saved (estimate).** At roughly 3 minutes per manual step, five automated steps save an **estimated ~15 minutes per incident**. This is an estimate, not a measured study.
- **No double payment.** One replacement per failure; the obligation closes only when the invoice is paid once.
- **Fraud-safe bank changes.** A call-back attestation plus a second-person approval before any new beneficiary is paid.
- **Explainable.** Every automatic step is recorded as "auto-approved by policy (rule X)", and Claude explains each decision in plain English.

### Who it is for

The two demo identities are `ap-clerk` and `finance-approver`. The clerk proposes. The finance approver approves. The same person cannot approve their own proposal. The supplier in the seeded hero is Lumen Textiles GmbH, invoice INV-2291, 4200.00 EUR. That supplier and those emails are synthetic.

### Hero flow

The seeded story is INV-2291. The offline replay at `/replay`, labelled MOCK, walks it with no network: a colleague's scheduled duplicate is cancelled with no balance movement, proof attaches while the original is SENT and is refused after cancel, `90701` decides `R-REPLACE_NEW_DETAILS-90701`, the bank change is Commerzbank to a new DKB beneficiary, the refund card unlocks before replacement, USD→EUR uses a locked quote, a second claim on that quote is rejected, and the ledger shows INV-2291 paid once.

### Live evidence (current code)

The hosted sandbox service (`SETTLEWELL_MODE=sandbox`, autonomy on, EUR 5,000 auto limit) runs the full hero against `api-demo.airwallex.com` with `x-api-version` `2025-11-11` on boot and on every **Reset demo**: a scheduled duplicate is auto-cancelled by policy, proof attaches while SENT and is refused after cancel, the simulated `90701` return decides `R-REPLACE_NEW_DETAILS-90701`, a new beneficiary is created after a (scripted) call-back attestation and approval, a locked USD→EUR quote funds the replacement when the reserve floor requires it, the replacement is PAID, and the obligation ends `PAID_ONCE`. Claude Haiku parses the claim and writes the advisory card live. Transfer ids change on each rerun, so none are pinned here; the console shows the current run. A fully recorded live run with ids (2026-11-08) and the earlier 2026-11-04 rehearsal are in [`docs/operator.md`](operator.md) and [`docs/rehearsal.md`](rehearsal.md). Sandbox funds are virtual.

### What is live vs mock

| Surface | Which |
|---|---|
| Hosted sandbox service: hero money path, Claude parse and advisory | Live Airwallex sandbox + Claude Haiku |
| Hosted mock service, `/replay`, seeded INV-2291 (4200.00 EUR, synthetic ids) | MOCK |
| Eval harness and 30-incident runner (`SETTLEWELL_MOCK=1`) | Offline; no Airwallex call |
| Supplier replies | Queued in the outbox; never delivered |
| Webhooks | Simulated locally; hosted delivery not verified |

### What a transfer-only agent does not do

**Cancel a colleague's still-scheduled duplicate at €0.** The scan reads every transfer page. A second live payment counts when the invoice matches and the beneficiary is one of that supplier's bank-detail versions. The same invoice paid to another beneficiary is ambiguous, not a confirmed duplicate. While that transfer is `SCHEDULED`, the action is `STOP_DUPLICATE` (`R-STOP-DUPLICATE-SCHEDULED`). With autonomy on, the policy auto-approves this cancel (rule A-CANCEL-SCHEDULED-DUPLICATE); with autonomy off, the clerk proposes and the finance approver approves. Cancel does not post a ledger fee. On the live sandbox runs the duplicate ends CANCELLED with no EUR balance-history movement.

**A status-gated proof PDF.** `issue_proof` asks for a `NO_FEE_DISPLAY` confirmation letter only while the live status is SENT or PAID. The rule while SENT is `R-PROOF-SENT`. After cancel, the same request is refused and the reply says the transfer is cancelled. The mock page's ETA and cut-off are labels, not Airwallex API fields. The live sandbox run attaches proof while SENT and refuses it after cancel.

**Decisions from Airwallex failure codes.** Policy is a table of 29 published codes. The numeric code comes from `failure_reason`. `90701` (account closed) becomes `REPLACE_NEW_DETAILS` and `R-REPLACE_NEW_DETAILS-90701`. `91001` (recall) escalates (`R-ESCALATE-91001`). The adapter pins `x-api-version` `2025-11-11` so the reason is still on a later GET after CANCELLED. The live sandbox run takes that `90701` path.

**The incident, from the supplier's email through to closure.** The email is matched to an invoice. The incident stays open while the transfer is in flight, while proof is issued or refused, while a duplicate is stopped, and while a replacement waits on a refund and an approval. It closes when the ledger says the invoice was paid once. On the live sandbox run the obligation ends `PAID_ONCE`.

**Supplier bank-detail version history, with a call-back.** New details are validated. A call-back to the phone on file is required. Settlewell then creates a new beneficiary and leaves the previous one as it was. The card shows the bank-name and last-four diff. No call-back is `R-BANK-CALLBACK-REQUIRED` in the harness. Unverified details are not paid. The live sandbox run creates that new beneficiary before the replacement.

### Locked quote, as supporting safety

Before a EUR replacement, Settlewell compares the EUR balance with the payment plus a reserve floor. The default floor is 1,000.00. A top-up above 10,000.00 is not sent; the harness reports that as `R-FX-LIMIT-ESCALATE`. Under the limit, it creates a USD→EUR quote with validity `MIN_15` and locks the quote. Conversion runs only after a second person approves that locked quote. The quote id is part of the approval hash. Sandbox quotes can be multi-use; Settlewell marks its own row `CONSUMED` with one `UPDATE` where `state = 'LOCKED'` before `create_conversion`, and a second claim is rejected. An expired quote is replaced and must be approved again (`R-FX-QUOTE-EXPIRED`).

The default floor does not fire on the pre-funded sandbox, so the recorded live run sets a stricter `SETTLEWELL_RESERVE_FLOOR` for that process only. The 2026-11-08 live run locked quote `1006e5ba-1813-38c5-ade0-7e5c0b4e7c3a` and settled conversion `103b9ab7-a7bc-468d-a146-ab62cb6843df` (bought EUR4340.50, sold USD4904.04); a second claim on a consumed quote is rejected. If the variable is not a positive number, `reserve_floor` raises `FundingError` with the message `SETTLEWELL_RESERVE_FLOOR must be a positive number`.

A replacement is not proposed until a `PAYOUT_REVERSAL` whose source is the original transfer is visible. The synthetic hero card reads `principal back: €4,200 · fee lost €13.47`. In the 2026-11-08 live run the full EUR4200 reversal was joined before the replacement, and the actual replacement fee was EUR13.50 from cash history.

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

Mock mode is the default. The eval harness refuses to run unless that flag is set. Page handlers do not call Airwallex. Webhook delivery checks an HMAC over the timestamp and the raw body, drops stale timestamps, and inserts the event id first so a replay hits the unique constraint and is not applied twice.

### Honest limits

The mock replay uses canned transfer statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL`, and a canned quote rate (fidelity table on `/replay` and in `backend/mock_replay.py`). A trace is a drafted support request; Settlewell does not retrieve an MT103 or a UETR. Cancelling a `PROCESSING` transfer is not claimed. The call-back attestation is entered by a person and not verified by software; in the hosted autorun the human approvals are scripted and labelled as simulated. Supplier replies are never sent. Hosted webhook delivery is not verified. Sandbox money is virtual and production use is unsupported (no per-user auth, shared demo passcode). Sandbox ids change on every rerun, and a reset can stall if a run fails partway.

### Eval and tests

`uv run pytest` passes 727 tests with no network and no secrets; `uv run ruff check .` is clean. `SETTLEWELL_MOCK=1 uv run python -m backend.scripts.eval_harness` prints one line per incident and then `30/30` (offline MOCK, not live). `SETTLEWELL_MOCK=1 uv run python -m backend.incidents --all` runs the 30-incident catalog through claim, match, decision, intent, approval, `execute` and reconcile against a stateful mock remote.

The no-LLM baseline is a static table in [`docs/operator.md`](operator.md#no-llm-baseline-static-table), not a second model. Without the policy engine, every incident is `ESCALATE`. Settlewell's column is the harness expectation: for example late SENT waits (`R-NO-REPLACE-IN-FLIGHT`), a scheduled duplicate stops (`R-STOP-DUPLICATE-SCHEDULED`), account-closed new details replace (`R-REPLACE_NEW_DETAILS-90701`), a non-JSON or smuggled payment escalates (`R-BAD-JSON`), and the hero composite passes (`R-HERO`).
