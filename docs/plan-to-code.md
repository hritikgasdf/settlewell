# Plan to code: final mapping

This maps the build plan (`build-plan.md`, v4 checklist in §5) to the repository. It was rewritten after the P2-8 handoff. Statuses come from reading the current code and from the local verification logs of this round. **Updated after fresh live sandbox and Claude calls on this box, 2026-11-08 IST.** Final test and live results are in `/workspace/settlewell-ctx/final-test-report.md`; only specifically evidenced paths are PASS; mocked/unverified scopes remain PARTIAL.

## Status legend

| Label | Meaning |
|---|---|
| **PASS** | Specific live/actual evidence in the final report; not certification of all failure branches |
| **STATIC** | Code and unit tests exist in the repo. They run against fakes. Says nothing about bank behaviour |
| **PARTIAL** | Code exists; a named gap remains, or the path is not fully traced in current code |
| **MOCK-ONLY** | Runs only against fakes, canned data or the stateful mock remote |
| **LIVE-HIST** | The 2026-11-04 sandbox run recorded this. Earlier tree, not re-run |
| **NOT VERIFIED** | Not code, or needs a person or an external account. Nothing done here |

The 2026-11-07 audit (`audit-2026-11-07.md`, outside the repo) is **historical**. Where current code shows a finding fixed, this file says so. Where the code evidence is unclear, the row stays PARTIAL and names what was not traced.

## Commands

| Purpose | Command | Network / secrets |
|---|---|---|
| Targeted test | `SETTLEWELL_MOCK=1 uv run pytest -q -x <file>` | None |
| Lint changed files | `uv run ruff check <files>` | None |
| Full test | `SETTLEWELL_MOCK=1 uv run pytest` | None |
| Full lint | `uv run ruff check .` | None |
| Incident runner (30) | `SETTLEWELL_MOCK=1 uv run python -m backend.incidents --all` | None (MOCK). Exit 1 while any row is not PASS |
| Eval harness (first 15) | `SETTLEWELL_MOCK=1 uv run python -m backend.scripts.eval_harness` | None |
| Mock hero (canned replay) | `SETTLEWELL_MOCK=1 uv run python -m backend.scripts.hero_run --mock` | None. Only `approved_replacement` and `reconcile_both` can PASS; the other 7 beats are PARTIAL |
| Local console (mock) | `bash scripts/run_console.sh` (or `--mode mock --api-port 8877 --ui-port 15173`) | None. `scripts/run_console.sh` is not executable, so run it with `bash` |
| Local API (mock) | `uv run python -m backend.api --mode mock --db var/settlewell.db --port 8877` | None |
| Local API (sandbox, operator only) | `uv run python -m backend.api --mode sandbox --db var/hero-sandbox.db` | **Live sandbox verified:** HTTP outreach approval/replay refusal; no browser money clicks |
| Sandbox hero (operator only) | `uv run python -m backend.scripts.hero_run --sandbox --confirm-sandbox --manifest var/hero-run-manifest.json --db var/hero-sandbox.db` | **Live sandbox verified on this box** |
| Owned reset (operator only) | `uv run python -m backend.scripts.hero_run --reset --sandbox --confirm-sandbox --manifest var/hero-run-manifest.json` | **Live sandbox.** Touches only transfers in this run's manifest |
| Mock reset | `uv run python -m backend.scripts.reset_demo` | None. Removes only an empty or mock hero manifest. It does **not** reset the ledger |
| Sandbox contract | `SETTLEWELL_SANDBOX=1 uv run python -m backend.scripts.sandbox_contract` | **Live sandbox verified on this box** |

## Credential and live-proof status

Fresh sandbox contract, full hero money path and real Claude parse ran on THIS BOX. No registered PC is involved. Credentials were inherited by SDKs; values never read/printed. See [README live proof](../README.md#live-proof--2026-11-08-ist-this-box) for transfer, conversion, quote and beneficiary IDs. The exact final report/evidence is `/workspace/settlewell-ctx/final-test-report.md`. Historical2026-11-04 evidence remains historical.

## Known limits of the current tree

- SANDBOX HTTP outreach approval200, stale hash409 and replay409 verified. Money approvals executed through hero; browser replacement/FX clicks and visual render not verified.
- Real hero: actual original return/reversal, duplicate cancel, beneficiary, locked FX and replacement PAID. Callback scripted, outreach queued only, no phone/email delivery.
- Proof: genuine PAID replacement bank PDF persisted and served HTTP200; mock seed PDFs remain synthetic. Original cancelled URL refused404/no bytes; hero cancellation policy also refused a new proof.
- Actual payout fee EUR13.50 confirmed by cash history. Conservative pre-send estimate fallback remains; independent provider fee-quote demonstration absent.
- Hosted webhooks unverified; signed loopback simulator is synthetic.30 incident scenarios/property tests remain mock-only. Live races/timeouts/quote expiry/reuse not induced.

## 1. The 45 checklist items (build plan §5)

Paraphrased from the plan's ordered checklist. Final statuses below reflect fresh evidence; see the exhaustive report for scopes and limits.

| # | Item (paraphrase) | Status | Symbols / tests | Gap or note |
|---|---|---|---|---|
| 1 | Claude credits in the Console | PARTIAL | none | Credits dashboard not independently checked; successful real API calls prove access, not remaining credits. |
| 2 | Private repo with `.gitignore` covering `.env` | PASS | `.gitignore` | Actual private repository metadata and gitignored .env verified earlier; no credential files staged. |
| 3 | Airwallex docs MCP in Cursor | PARTIAL | `.cursor/mcp.json` (`airwallex-docs`) | Docs MCP configuration exists; no fresh docs query. |
| 4 | Calendar blocks | PARTIAL | none | No calendar action. |
| 5 | Optional mail filter | PARTIAL | none | Optional mail filter not configured. |
| 6 | Local env (Py 3.12, uv, Node 20, `.env.example`) | PARTIAL | `pyproject.toml`, `.env.example` | uv/Python/Node work on this box; fresh-machine/env-file setup not tested. |
| 7 | First sandbox smoke call | PASS | historical only | Fresh contract PASS: p28b-contract-2.log; first attempt returned fee_unknown and was not bypassed. |
| 8 | Airwallex adapter (token, pinned version, throttle, typed results) | PASS | `backend/airwallex/client.py` `AirwallexClient`; `types.py`; `test_airwallex_client.py`, `test_transport_timeout.py` | Real SDK calls: balance, transfer get/list/create/cancel, beneficiaries, FX, PDF and cash history; live-readback/history JSON. |
| 9 | Sandbox driver and contract | PASS | `backend/sandbox.py` `SandboxDriver`; `scripts/sandbox_contract.py`; `test_sandbox_driver.py`, `test_reset.py` | Contract SENT→90701→CANCELLED, PDF and full reversal; sandbox simulator, not production bank behavior. |
| 10 | Data model, ledger, seed | PASS | `backend/ledger.py` `SCHEMA`; `scripts/seed_ledger.py`; `test_ledger.py` | Persisted live execution/approval/funding/audit/proof rows; p28b-final-ledger-evidence.db (outside git). |
| 11 | Poller, reversal join, account-wide index | PASS | `backend/reconcile.py` `poll_incident`; `test_reconcile.py`, `test_reconcile_durable.py` | Real poll/reversal join and final outcomes; original+replacement accounted for, primary CLOSED. |
| 12 | Policy v1, failure-code table, rule ids | PARTIAL | `backend/policy.py` `decide`, `FAILURE_ROWS`; `test_policy.py` | Hero policy decisions exercised live; full failure-code table only mock-tested. |
| 13 | Claim parser (strict schema) and match | PASS | `backend/claims.py` `parse_email`, `_match`; `test_claims*.py` | p28b-claude-proof.json: live Haiku parse and wrong pinned-supplier rejection. |
| 14 | Account-wide paid-once scan | PASS | `backend/paid_once.py` `scan_paid_once`; `test_paid_once.py` | Live supplier-aware scan found scheduled duplicate; ordinary complete scan exercised. Beyond-page-five/ambiguity regressions remain mock-only. |
| 15 | WAIT, status-gated proof, ETA, trace draft, reply draft | PARTIAL | `backend/proof.py` `issue_proof`, `describe_eta`, `trace_draft`; `backend/api_proof.py`; `test_proof.py` | Genuine PAID bank PDF HTTP200; cancelled original refused. Outbox queued, trace/ETA and delivery not fully live. |
| 16 | Bound single-use approval | PARTIAL | `backend/approvals.py` `binding_hash`, `approve`; `backend/execute.py`; `test_approvals.py` | Money approvals1–4 consumed by execute; HTTP outreach replay409. Money concurrency/replay/terms edge cases mock-only. |
| 17 | Approver ≠ proposer, two identities | PARTIAL | `approvals.py` `AP_CLERK`, `FINANCE_APPROVER`; `test_approvals.py` | ap-clerk/finance-approver are distinct demo labels, not authenticated people. |
| 18 | STOP_DUPLICATE (cancel, €0) | PASS | `backend/stop_duplicate.py`; scenario 3 | Duplicate d5eae955… CANCELLED via approved execute; complete cash history zero duplicate entries. |
| 19 | REPLACE, lock, `stl-` request id | PASS | `backend/replace_payment.py` `replace_payment`; `test_replace_payment.py` | Replacement e64445c5… PAID via execute; stored request stl-inv39391662-1 and owning approval4. |
| 20 | One replacement per failure; unknown stays IN_FLIGHT; six-day rule | PARTIAL | `replace_payment.py` (`_DEDUPE_MARGIN` = 6 days, check at line ~438); scenario 14 | Unknown-send restart, six-day stale snapshot and concurrent replacement tested with mocks, not induced live. |
| 21 | Replacement input = obligation + incident only | PASS | `backend/replacement_tool.py` `propose_replacement`; `test_replacement_tool.py` | Hero replacement proposed from pinned obligation/original provider terms, then gated execute. |
| 22 | Refund-confirmed replacement (N1) | PASS | `replacement_tool.refund_card`; `test_refund_card.py`, `test_funding_refunds.py` | Actual cash PAYOUT_REVERSAL EUR4200 for original; source/principal/currency match before replacement. |
| 23 | Bank-change gate: validate, call-back, new beneficiary, diff | PARTIAL | `backend/bank_change.py` `verify_bank_change`, `propose`, `binding_hash`; `test_bank_change.py`; scenarios 7, 9, 26 | Real new beneficiary created; callback attestation scripted, no phone verification. |
| 24 | FX reserve floor and limit | PASS | `backend/fx_funding.py` `fund_replacement`; `test_fx_funding.py`; scenario 24 | Locked real quote, conversion SETTLED; EUR after payout10005186.25 ≥ configured floor10005172.75. Demo floor deliberately raised to force FX. |
| 25 | FxQuote single-use and expiry | PARTIAL | `backend/fx_quote.py` `approve_fx_quote`, `execute_fx_quote`; `test_fx_quote.py`; scenario 29 | Locked quote used once live; reuse/expiry refusal only mock-tested. |
| 26 | Colleague already re-paid (N2) | PARTIAL | `backend/repaid.py` `colleague_repaid`; `test_repaid.py`; scenario 6 | Colleague already-paid/later-failed scenarios only mock replay. |
| 27 | Webhook receiver | PARTIAL | `backend/webhooks.py` `receive_webhook`, `_verify`, `_apply`; `api.py` POST route; `worker.py`; `test_webhooks.py`, `test_webhook_settle.py` | Signed loopback simulator tested; no Airwallex-hosted webhook subscription/delivery. Ordering/window/race tests use mocks. |
| 28 | Console skeleton | PARTIAL | `backend/console.py`; `frontend/` React console; `test_console.py`, `test_api.py`, `test_api_p17.py` | SANDBOX console HTTP200 and API approval200; wrong hash/replay409. No successful browser visual check or browser money clicks. |
| 29 | Money ledger, results, FX beat | PARTIAL | `console.py` `_ledger`; `frontend/` | Actual cash ledger/fee/FX balances proved; React results rendered in a browser not visually verified. |
| 30 | Visible outbox | PARTIAL | `backend/outbox.py`; `backend/incident_outreach.py`; `test_outbox.py`, `test_incident_outreach.py` | Actual approved outreach queued; no email delivery integration. |
| 31 | Eval harness, first 15 incidents | PARTIAL | `backend/eval_cases.py` `CASES`; `backend/eval_harness.py`; `test_eval_harness.py` | Offline scenario/harness assertions only; not live banking. |
| 32 | Remaining 15 incidents | PARTIAL | `backend/eval_rest.py`; `backend/incident_catalog.py`; `backend/incidents.py` `run_all`; `test_incident_catalog.py`, `test_incidents_runner.py` | 30/30 offline runner,208 checks; all final incident statuses PARTIAL/mock. |
| 33 | Property test (Hypothesis) | PARTIAL | `tests/test_properties.py`, `tests/test_incident_properties.py` | Property tests pass against mocks; not live stochastic banking evidence. |
| 34 | Seed/reset leaves nothing at SENT | PASS | `backend/reset.py` `reset_demo`; `backend/scripts/reset_demo.py`; `tests/test_reset.py` | Owned terminal transfers verified and scoped reset succeeded; custom DB and both manifests removed, backups preserved. Unknown-conversion reset not live-tested. |
| 35 | One-command local run | PARTIAL | `scripts/run_console.sh`; `settlewell` package (`python -m settlewell`) | API/Vite served, but a fresh-machine one-command rehearsal not completed. |
| 36 | Offline mock mode with fidelity table | PARTIAL | `backend/mock_replay.py`; `backend/scripts/hero_run.py --mock`; `test_mock_replay.py`, `test_hero_run.py` | Offline mock fidelity documented; remains mock, not live proof. |
| 37 | README: setup, credit, baseline, limits | PARTIAL | `README.md` (rewritten) | README corrected with fresh sandbox IDs/fee and limits; no clean-machine reader verification. |
| 38 | Code readability pass | PARTIAL | none | Focused live bug reviews/regressions completed; no whole-code independent readability review. |
| 39 | Feature freeze | PARTIAL | `README.md` freeze section | Feature-freeze declaration only; release process not certified. |
| 40 | Two rehearsals on real sandbox | PARTIAL | `docs/rehearsal.md` | One complete fresh hero succeeded; earlier partial attempts and historical run are not two fresh complete rehearsals. |
| 41 | Rough-cut video and side clips | PARTIAL | none | No rough-cut video. |
| 42 | Writeup draft | PARTIAL | `docs/writeup.md` | Draft exists; final editorial review unverified. |
| 43 | Final video and backup | PARTIAL | none | No final video/backup. |
| 44 | Final writeup, clean-machine check, judge access | PARTIAL | none | No clean-machine judge-access/writeup sign-off. |
| 45 | Form dry-run and submission | PARTIAL | none | No submission/dry-run form action. |


## 2. Must-haves (build plan §2.1, 17 items)

| # | Must-have (paraphrase) | Status | Symbols / tests | Gap |
|---|---|---|---|---|
| 1 | Supplier email → schema-validated claim | PASS | `claims.parse_email`; `test_claims*.py` | p28b-claude-proof.json: live Haiku parse and wrong pinned-supplier rejection. |
| 2 | Match claim → obligation → transfer | PASS | `claims._match`; `replacement_tool.py` | Hero replacement proposed from pinned obligation/original provider terms, then gated execute. |
| 3 | Account-wide paid-once guard | PARTIAL | `paid_once.scan_paid_once`; `test_paid_once.py` | Ordinary live scan/duplicate exercised; colleague PAID blocking and ambiguous/beyond-page-five paths mock-only. |
| 4 | Live investigation | PASS | `reconcile.poll_incident`; `backend/airwallex/client.py` | Real SDK calls: balance, transfer get/list/create/cancel, beneficiaries, FX, PDF and cash history; live-readback/history JSON. |
| 5 | Deterministic policy engine | PASS | `policy.decide`; `test_policy.py` | Real provider statuses drove WAIT, duplicate cancel, refund-gated replacement and reconciliation in hero. |
| 6 | Status-gated proof | PARTIAL | `proof.issue_proof`; `api_proof.py`; scenario 22 | Genuine PAID bank PDF HTTP200; cancelled original refused. Outbox queued, trace/ETA and delivery not fully live. |
| 7 | STOP_DUPLICATE | PASS | `stop_duplicate.py`; scenario 3 | Duplicate d5eae955… CANCELLED via approved execute; complete cash history zero duplicate entries. |
| 8 | Bank-change gate | PARTIAL | `bank_change.py`; scenarios 7, 9 | Real new beneficiary created; callback attestation scripted, no phone verification. |
| 9 | Bound single-use approvals | PARTIAL | `approvals.binding_hash`, `approve`; `execute.execute`; `fx_quote.py`; `funding.py` | Money approvals1–4 consumed by execute; HTTP outreach replay409. Money concurrency/replay/terms edge cases mock-only. |
| 10 | No double pay on our side | PARTIAL | `replace_payment.py`; scenarios 13, 14 | Unknown-send restart, six-day stale snapshot and concurrent replacement tested with mocks, not induced live. |
| 11 | Reconciler (poll + webhook, closure rule) | PARTIAL | `reconcile.py`; `webhooks.py`; `worker.py`; `outcomes.decide_set` | Original CANCELLED+reversal and replacement PAID close primary incident live; later-return/race branches only mock-tested. |
| 12 | Console | PARTIAL | `console.py`; `frontend/` | SANDBOX console HTTP200 and API approval200; wrong hash/replay409. No successful browser visual check or browser money clicks. |
| 13 | Balance and FX funding check | PASS | `fx_funding.py`; `fx_quote.py`; scenarios 24, 29 | Locked real quote, conversion SETTLED; EUR after payout10005186.25 ≥ configured floor10005172.75. Demo floor deliberately raised to force FX. |
| 14 | 30-incident harness + property test | PARTIAL | `incidents.py`; `incident_scenarios.py`; `tests/test_properties.py` | 30/30 offline runner,208 checks; all final incident statuses PARTIAL/mock. Property tests pass against mocks; not live stochastic banking evidence. |
| 15 | Runnable demo, video, README | PARTIAL | `README.md`; `scripts/run_console.sh`; `backend.api` | Runnable local console/API observed; video and clean-machine checks missing. |
| 16 | Money back before money out (N1) | PASS | `replacement_tool.refund_card` | Actual cash PAYOUT_REVERSAL EUR4200 for original; source/principal/currency match before replacement. |
| 17 | Colleague already re-paid (N2) | PARTIAL | `repaid.colleague_repaid`; scenario 6 | Colleague already-paid/later-failed scenarios only mock replay. |

Should-haves §2.2 (2 items): visible outbox (PARTIAL, `outbox.py`); no-LLM baseline (STATIC, the `README.md` table is a static expectation, not a measured comparison).


## 3. The 30 incidents (build plan §2.5)

Final status for **all 30 is MOCK-ONLY / PARTIAL**, even where the runner shows PASS. The runner PASS is an offline assertion count against the stateful mock remote. It is not a bank result. Scenario functions are `backend/incident_scenarios.py` `sc_N`, registered in `SCENARIOS`. Catalog: `backend/incident_catalog.py`. Runner: `backend/incidents.py`.

| # | Scenario (safe name) | Plan expected action | Scenario symbol | Notes |
|---|---|---|---|---|
| 1 | Late SENT, supplier asks where | WAIT + reassure + PDF + ETA | `sc_1` | Deviation: ETA cut-off is assumed |
| 2 | Still processing | WAIT, no replace | `sc_2` | |
| 3 | Colleague's scheduled duplicate | STOP_DUPLICATE (cancel before funding) | `sc_3` | Cost 0 is mock |
| 4 | Already paid, supplier impatient | WAIT, reassure, no replace | `sc_4` | |
| 5 | Returned after SENT | REPLACE only after reversal seen | `sc_5` | Refund-confirmed gate |
| 6 | Colleague already re-paid after return | No replacement; close against colleague | `sc_6` | N2 |
| 7 | Return plus new-details email | Block until call-back; separate approvals | `sc_7` | Call-back scripted |
| 8 | Return to same details | REPLACE to existing beneficiary | `sc_8` | |
| 9 | Account closed (90701) plus new details | Full new-beneficiary gate | `sc_9` | |
| 10 | Details changed after approval | Void and re-approve | `sc_10` | |
| 11 | Duplicate approve click; self-approval | Second rejected; self-approval rejected | `sc_11` | |
| 12 | Replayed paid webhook | Dedupe; no second transfer | `sc_12` | |
| 13 | Concurrent replace attempts | One transfer; duplicate request id | `sc_13` | Sequential in scenario |
| 14 | Timeout, unknown outcome | Lookup by request id; no attempt 2 | `sc_14` | Deviation: held-unknown label is `LOOKUP_KEEP_IN_FLIGHT`; recovery follows |
| 15 | Prompt injection | Escalate; no money out | `sc_15` | Identity-level JSON check; not a real model test |
| 16 | Invalid or low-confidence model output | Escalate | `sc_16` | Deviation: confidence 0.8 assumed; no confidence accepted as legacy |
| 17 | Amount or invoice mismatch | Escalate | `sc_17` | |
| 18 | Recall-coded return (91001) | Escalate; never auto-replace | `sc_18` | |
| 19 | Wrong-currency claim | Escalate or clarify draft | `sc_19` | |
| 20 | Trace requested | TRACE draft (labelled) | `sc_20` | Deviation: minimum SENT age 7 days assumed; fixture 14 |
| 21 | Replace while SENT | Policy refuses | `sc_21` | |
| 22 | Proof after CANCELLED | Refuse proof; explain status | `sc_22` | Deviation: cancelled STANDARD fixture is not stored as proof |
| 23 | Fee accounting after return | Replacement sized to original plus new fee | `sc_23` | Fee is an estimate |
| 24 | Reserve floor breach, FX within or over limit | Locked quote, bound approval, convert, replace | `sc_24` | Mock |
| 25 | Out-of-order webhooks, paid then failed | Ordered; paid provisional | `sc_25` | |
| 26 | Beneficiary update without attestation | Rejected | `sc_26` | |
| 27 | Injection via reference field | Untrusted; escalate | `sc_27` | |
| 28 | Unknown supplier | Escalate | `sc_28` | Deviation: refusal before `execute` when no pinned supplier; no incident row |
| 29 | Replacement id reused; FX quote reused or expired | Duplicate id; CONSUMED; re-quote and re-approve | `sc_29` | Deviation: runner does not force a provider `duplicate_request_id` |
| 30 | Full hero composite | Full arc; invariants hold | `sc_30` | Mock composite |

## 4. Hero demo beats (build plan §2.4, 11 rows)

Beat names follow the plan's time table. Fresh actual path evidence is identified; combined beats with mocked/unverified components stay PARTIAL. The mock hero replay exercises only beats 9 and 10 (see the report).

| # | Beat (plan time) | Code and tests | Status | Gap |
|---|---|---|---|---|
| 1 | Cold open, supplier email (0:00) | `claims.parse_email` (offline completer); `mock_replay.py` | PASS | Live Claude Haiku reads synthetic fixture claim and matches pinned supplier (not email identity). |
| 2 | Investigate, colleague's scheduled duplicate (0:12) | `paid_once.scan_paid_once`; `sc_3` | PASS | Real original SENT and live account scan found separate scheduled duplicate. |
| 3 | Stop the duplicate, approver ≠ proposer (0:45) | `stop_duplicate.py`; `approvals.py`; `execute.py`; `sc_3`, `sc_11` | PASS | Duplicate d5eae955… CANCELLED via approved execute; complete cash history zero duplicate entries. |
| 4 | WAIT and proof while SENT, trace draft, outbox (1:08) | `proof.py`; `api_proof.py`; `outbox.py`; `sc_1`, `sc_20` | PARTIAL | Real bank PDF fetched/stored/served. Outreach only queued, ETA estimated, full trace/delivery unverified. |
| 5 | New information: `failed` 90701, resend refused (1:35) | `webhooks._apply`; `proof.py`; `sc_22` | PARTIAL | Actual sandbox 90701→CANCELLED and full cash reversal; cancelled proof refused. Event injection simulated, not provider webhook delivery. |
| 6 | Resolve: validate, call-back, new beneficiary (1:57) | `bank_change.verify_bank_change`; `sc_9` | PARTIAL | Real new beneficiary created; callback attestation scripted, no phone verification. |
| 7 | Money back first, PAYOUT_REVERSAL (2:20) | `replacement_tool.refund_card`; `sc_5` | PASS | Actual cash PAYOUT_REVERSAL EUR4200 for original; source/principal/currency match before replacement. |
| 8 | Funding and FX on a locked quote; reuse rejected (2:30) | `fx_funding.py`; `fx_quote.py`; `sc_24`, `sc_29` | PARTIAL | Actual quote1006e5ba… + conversion103b9ab7… SETTLED; quote reuse/expiry refusal not exercised against live SDK. |
| 9 | One bound approval, one replacement, one per failure (2:48) | `approvals.py`; `replace_payment.py`; `sc_13`, `sc_14` | PARTIAL | Real bound approval4 produced exactly one observed PAID replacement. Concurrent/unknown-send/second-money-approval refusal only mock-tested. |
| 10 | Result: balances, paid once, incident cost (3:08) | `outcomes.decide_set`; `console.py`; `incidents.py` | PARTIAL | Live final bank outcomes, cash fee13.50, reserve and closure proved. 30/30 invariant strip is mock evidence, not 30 bank tests. |
| 11 | Credit and limits end card (3:40) | `README.md` Honest limits | PARTIAL | README limits documented; credit/end-card/video not recorded. |


## 5. Ordering and closure rules (build plan §3.4 and §2.1 #11/#17)

| Rule | Code | Status | Gap |
|---|---|---|---|
| 3.4-1 Verify first: HMAC over timestamp and raw body; stale rejected; dedupe on event id | `webhooks._verify`, `receive_webhook`; `test_webhooks.py` | STATIC | Synthetic signatures only |
| 3.4-2 Order by event time, then status rank; ignore backwards moves; ignore `funding.*` | `webhooks._apply` (`occurred > latest_event_ts_ms`; failure wins a tie; `_RANK` table present) | PARTIAL | Deviation: status-rank sort is not implemented as the plan describes. README states this |
| 3.4-3 `paid` provisional; settle window; later `failed` wins | `webhook_state`; `worker.py` settles windows; `reconcile.py` read | PARTIAL | Window is durable. Worker runs in the local process only |
| 3.4-4 Store failure reason on receipt | `webhooks._apply` calls `remember_failure_reason` | STATIC | |
| 3.4-5 Fast 2xx, async processing; polling as backstop | `api.py` POST route; `worker.py` polls every 5 s | PARTIAL | Apply is synchronous in the request. Retry behaviour untested |
| §2.1 #11 Close only when original and replacement are both accounted for | `outcomes.decide_set`, `reduce_outcome` | PARTIAL | Live poll accounted for original CANCELLED+refund and replacement PAID, then closed primary incident. Webhook/worker/later-failure races remain mock-only |
| §2.1 #17 Colleague re-paid closes against the external transfer | `repaid.colleague_repaid` | PARTIAL | Supplier-aware scan and outcome reduction; live external-payment behavior unverified |

## 6. Non-code items (not verified, nothing performed)

Calendar, credits, video, writeup final, submission form and judge access are **NOT VERIFIED**. No external action was taken. Judge access must not be granted without explicit authorisation.

## 7. Test coverage caveat

- Tests check the money invariants in code: no second live payment, no payment to unverified details, no proof outside SENT or PAID, single-use approval, and webhook dedupe. They run against fakes and the stateful mock remote.
- Passing tests do not prove bank behaviour, webhook delivery from Airwallex, or real Claude output.
- The property test's generated state machine is mostly `REPLACE_SAME`. FX and some other paths are fixed sequences.
- Eval-harness `30/30` counts are not the same as runner `PASS`. The runner is the stricter check.

## 8. Corrections to earlier mapping and README claims

| # | Earlier claim | Current code evidence | Correction |
|---|---|---|---|
| 1 | Audit: webhook apply does not compare timestamps | `webhooks._apply` compares `occurred > latest_event_ts_ms` | Audit is stale here |
| 2 | Audit: no POST webhook route | `api.py` and `README.md` describe one | Audit is stale; async and fast-2xx still open |
| 3 | Audit: FX and payout paths bypass approval | `fx_quote.py` and `funding.py` check the binding hash before execute | Historical. Not every path re-traced |
| 4 | Audit: six-day rule off by default | `replace_payment.py` `_DEDUPE_MARGIN` = 6 days | Not supported by current evidence; enforcement path not fully traced |
| 5 | Audit: PDF gate at send not implemented | `api_proof.py` gates the GET serving path | Gate exists on serving; the send path is outbox-only |
| 6 | Older README: live sandbox proof presented as current | 2026-11-04 run is historical | Historical run relabelled; new current sandbox IDs/evidence now recorded separately |
| 7 | Older README: Airwallex kit labels and a "reference" wording | Removed | Generic Airwallex patterns credited |
| 8 | Older README: outside-repo links and a personal repo slug | Removed | |
| 9 | Older README: baseline described as a comparison | Static expectation table | Reworded |
| 10 | Older README: `reset_demo` resets the hero | `reset_demo` now delegates to the scoped hero reset | Mock reset removes only the manifest; the ledger is not reset |
| 11 | Presence check of three variable names | Earlier presence-check gate is not current evidence | SDKs successfully used inherited credentials; values never read |
