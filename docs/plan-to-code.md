# Plan to code: final mapping

This maps the build plan (`build-plan.md`, v4 checklist in §5) to the repository. It was rewritten after the P2-8 handoff. Statuses come from reading the current code and from the local verification logs of this round. **No live Airwallex or Claude call was made for this document.** Final test and live results are in `/workspace/settlewell-ctx/final-test-report.md`; this file does not claim a pass.

## Status legend

| Label | Meaning |
|---|---|
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
| Local API (sandbox, operator only) | `uv run python -m backend.api --mode sandbox --db var/hero-sandbox.db` | **Live sandbox: NOT RUN.** SANDBOX writes: guard implemented and targeted MOCK-transport tested (see below) |
| Sandbox hero (operator only) | `uv run python -m backend.scripts.hero_run --sandbox --confirm-sandbox --manifest var/hero-run-manifest.json --db var/hero-sandbox.db` | **Live sandbox.** Not run for this document |
| Owned reset (operator only) | `uv run python -m backend.scripts.hero_run --reset --sandbox --confirm-sandbox --manifest var/hero-run-manifest.json` | **Live sandbox.** Touches only transfers in this run's manifest |
| Mock reset | `uv run python -m backend.scripts.reset_demo` | None. Removes only an empty or mock hero manifest. It does **not** reset the ledger |
| Sandbox contract | `SETTLEWELL_SANDBOX=1 uv run python -m backend.scripts.sandbox_contract` | **Live sandbox.** Not run for this document |

## Credential and live-proof status

| Item | Status |
|---|---|
| Live sandbox run for the current tree | **None.** No sandbox or Claude call was made for this document |
| Registered PC (the operator's machine) | Offline at check time. Part B (live run on that machine) not performed |
| Sandbox credentials | Present on the box only, by name. Values never read or printed. The box is not the registered PC |
| `ANTHROPIC_API_KEY` | The live parse path exists; no fresh real call from this tree |
| Historical proof (2026-11-04, `api-demo`) | Historical. Not current evidence. Details in `README.md` and `docs/rehearsal.md` |
| Final live PASS | **Not claimed** |

## Known limits of the current tree

- **Write guard in SANDBOX:** `backend/api.py` `write_guard` is implemented and targeted MOCK-transport tested. Live sandbox/browser action is still NOT RUN, so do not rely on console writes in SANDBOX mode. MOCK writes are the verified path.
- **Mock hero:** `hero_run --mock` runs the canned `play_hero` replay. It does not drive the real `Hero` pipeline, and it does not exercise FX, duplicate, bank-change or proof beats. Those show as PARTIAL.
- **Real `Hero` pipeline:** supports the full sequence in code. Every live step is unverified on the sandbox. Callback attestation is scripted by the finance approver, not a human call-back. Supplier outreach is a queued outbox row, not human contact.
- **Proof PDF:** the demo PDF is a MOCK / SYNTHETIC document generated locally. It is not the direct bank PDF. Live proof from Airwallex is not verified.
- **Fees:** no fee quote is captured. Fee figures are conservative estimates.
- **Webhooks:** the simulator sends signed synthetic events to loopback. Hosted delivery from Airwallex is not verified.

## 1. The 45 checklist items (build plan §5)

Paraphrased from the plan's ordered checklist. Final statuses are PARTIAL or worse for every item in this round; see the report.

| # | Item (paraphrase) | Status | Symbols / tests | Gap or note |
|---|---|---|---|---|
| 1 | Claude credits in the Console | NOT VERIFIED | none | Console balance not checked |
| 2 | Private repo with `.gitignore` covering `.env` | NOT VERIFIED | `.gitignore` | Repo visibility not checked |
| 3 | Airwallex docs MCP in Cursor | PARTIAL | `.cursor/mcp.json` (`airwallex-docs`) | No docs query recorded |
| 4 | Calendar blocks | NOT VERIFIED | none | Not code |
| 5 | Optional mail filter | NOT VERIFIED | none | Not code; optional |
| 6 | Local env (Py 3.12, uv, Node 20, `.env.example`) | PARTIAL | `pyproject.toml`, `.env.example` | `uv` runs locally; `.env` loading not verified |
| 7 | First sandbox smoke call | LIVE-HIST | historical only | No fresh call |
| 8 | Airwallex adapter (token, pinned version, throttle, typed results) | STATIC | `backend/airwallex/client.py` `AirwallexClient`; `types.py`; `test_airwallex_client.py`, `test_transport_timeout.py` | Live behaviour unverified |
| 9 | Sandbox driver and contract | STATIC | `backend/sandbox.py` `SandboxDriver`; `scripts/sandbox_contract.py`; `test_sandbox_driver.py`, `test_reset.py` | Contract not run for this document |
| 10 | Data model, ledger, seed | STATIC | `backend/ledger.py` `SCHEMA`; `scripts/seed_ledger.py`; `test_ledger.py` | Append-only is a convention, not enforced by the DB |
| 11 | Poller, reversal join, account-wide index | PARTIAL | `backend/reconcile.py` `poll_incident`; `test_reconcile.py`, `test_reconcile_durable.py` | Index lacks supplier matching |
| 12 | Policy v1, failure-code table, rule ids | STATIC | `backend/policy.py` `decide`, `FAILURE_ROWS`; `test_policy.py` | Not every execution path calls it |
| 13 | Claim parser (strict schema) and match | PARTIAL | `backend/claims.py` `parse_email`, `_match`; `test_claims*.py` | Live Claude path not run from this tree |
| 14 | Account-wide paid-once scan | PARTIAL | `backend/paid_once.py` `scan_paid_once`; `test_paid_once.py` | Supplier and version matching and full pagination are gaps |
| 15 | WAIT, status-gated proof, ETA, trace draft, reply draft | PARTIAL | `backend/proof.py` `issue_proof`, `describe_eta`, `trace_draft`; `backend/api_proof.py`; `test_proof.py` | Demo PDF is synthetic. Serving checks status, settle window and hash (`api_proof.py`) |
| 16 | Bound single-use approval | PARTIAL | `backend/approvals.py` `binding_hash`, `approve`; `backend/execute.py`; `test_approvals.py` | Current code binds FX (`fx_quote.py`) and payout (`funding.py`, binding check) paths. Not every path re-traced this pass |
| 17 | Approver ≠ proposer, two identities | STATIC | `approvals.py` `AP_CLERK`, `FINANCE_APPROVER`; `test_approvals.py` | Identities are demo labels; no login |
| 18 | STOP_DUPLICATE (cancel, €0) | PARTIAL | `backend/stop_duplicate.py`; scenario 3 | Mock cost 0. Live cancel only in the historical run |
| 19 | REPLACE, lock, `stl-` request id | PARTIAL | `backend/replace_payment.py` `replace_payment`; `test_replace_payment.py` | Two-thread test exists; mock remote only |
| 20 | One replacement per failure; unknown stays IN_FLIGHT; six-day rule | PARTIAL | `replace_payment.py` (`_DEDUPE_MARGIN` = 6 days, check at line ~438); scenario 14 | The audit's "off by default" claim is not supported by the current code. The enforcement path was not fully traced |
| 21 | Replacement input = obligation + incident only | STATIC | `backend/replacement_tool.py` `propose_replacement`; `test_replacement_tool.py` | |
| 22 | Refund-confirmed replacement (N1) | PARTIAL | `replacement_tool.refund_card`; `test_refund_card.py`, `test_funding_refunds.py` | No full-principal equality check |
| 23 | Bank-change gate: validate, call-back, new beneficiary, diff | PARTIAL | `backend/bank_change.py` `verify_bank_change`, `propose`, `binding_hash`; `test_bank_change.py`; scenarios 7, 9, 26 | Call-back is scripted by the approver, not a human call. Mock remote only |
| 24 | FX reserve floor and limit | PARTIAL | `backend/fx_funding.py` `fund_replacement`; `test_fx_funding.py`; scenario 24 | The audit's pre-approval conversion claim was not re-traced end to end |
| 25 | FxQuote single-use and expiry | PARTIAL | `backend/fx_quote.py` `approve_fx_quote`, `execute_fx_quote`; `test_fx_quote.py`; scenario 29 | Current code checks the binding hash before approve and execute |
| 26 | Colleague already re-paid (N2) | PARTIAL | `backend/repaid.py` `colleague_repaid`; `test_repaid.py`; scenario 6 | Supplier and post-return matching are gaps |
| 27 | Webhook receiver | PARTIAL | `backend/webhooks.py` `receive_webhook`, `_verify`, `_apply`; `api.py` POST route; `worker.py`; `test_webhooks.py`, `test_webhook_settle.py` | Apply runs in the request. Retry behaviour untested. No hosted registration |
| 28 | Console skeleton | PARTIAL | `backend/console.py`; `frontend/` React console; `test_console.py`, `test_api.py`, `test_api_p17.py` | MOCK. SANDBOX write guard implemented and targeted MOCK-transport tested; live sandbox/browser action NOT RUN |
| 29 | Money ledger, results, FX beat | PARTIAL | `console.py` `_ledger`; `frontend/` | Some result data is seeded |
| 30 | Visible outbox | PARTIAL | `backend/outbox.py`; `backend/incident_outreach.py`; `test_outbox.py`, `test_incident_outreach.py` | Queue only. Nothing delivered |
| 31 | Eval harness, first 15 incidents | PARTIAL | `backend/eval_cases.py` `CASES`; `backend/eval_harness.py`; `test_eval_harness.py` | Eval counts are not the runner's PASS |
| 32 | Remaining 15 incidents | PARTIAL | `backend/eval_rest.py`; `backend/incident_catalog.py`; `backend/incidents.py` `run_all`; `test_incident_catalog.py`, `test_incidents_runner.py` | Runner is authoritative. MOCK |
| 33 | Property test (Hypothesis) | PARTIAL | `tests/test_properties.py`, `tests/test_incident_properties.py` | Generated machine is mostly REPLACE_SAME; FX fixed |
| 34 | Seed/reset leaves nothing at SENT | PARTIAL | `backend/reset.py` `reset_demo`; `backend/scripts/reset_demo.py`; `tests/test_reset.py` | Sandbox reset is owned-only (manifest or exact request id) and refuses on uncertainty. Mock reset does not clear the ledger |
| 35 | One-command local run | PARTIAL | `scripts/run_console.sh`; `settlewell` package (`python -m settlewell`) | Launcher refusal cases were not all run in this round |
| 36 | Offline mock mode with fidelity table | MOCK-ONLY | `backend/mock_replay.py`; `backend/scripts/hero_run.py --mock`; `test_mock_replay.py`, `test_hero_run.py` | 2 PASS and 7 PARTIAL beats |
| 37 | README: setup, credit, baseline, limits | PARTIAL | `README.md` (rewritten) | Clean-machine read-through not done |
| 38 | Code readability pass | PARTIAL | none | No review done for this document |
| 39 | Feature freeze | NOT VERIFIED | `README.md` freeze section | A declaration, not proof of completeness |
| 40 | Two rehearsals on real sandbox | LIVE-HIST | `docs/rehearsal.md` | Historical; not re-run |
| 41 | Rough-cut video and side clips | NOT VERIFIED | none | Not code |
| 42 | Writeup draft | STATIC | `docs/writeup.md` | Not reviewed in this pass (out of scope) |
| 43 | Final video and backup | NOT VERIFIED | none | Not code |
| 44 | Final writeup, clean-machine check, judge access | NOT VERIFIED | none | No access granted by this work |
| 45 | Form dry-run and submission | NOT VERIFIED | none | No external action taken |

## 2. Must-haves (build plan §2.1, 17 items)

| # | Must-have (paraphrase) | Status | Symbols / tests | Gap |
|---|---|---|---|---|
| 1 | Supplier email → schema-validated claim | PARTIAL | `claims.parse_email`; `test_claims*.py` | No email intake; live call not run |
| 2 | Match claim → obligation → transfer | PARTIAL | `claims._match`; `replacement_tool.py` | No multi-transfer match |
| 3 | Account-wide paid-once guard | PARTIAL | `paid_once.scan_paid_once`; `test_paid_once.py` | Supplier and version matching |
| 4 | Live investigation | PARTIAL | `reconcile.poll_incident`; `backend/airwallex/client.py` | Live read unverified |
| 5 | Deterministic policy engine | PARTIAL | `policy.decide`; `test_policy.py` | Not consulted by every path |
| 6 | Status-gated proof | PARTIAL | `proof.issue_proof`; `api_proof.py`; scenario 22 | Gate on GET; synthetic PDF |
| 7 | STOP_DUPLICATE | PARTIAL | `stop_duplicate.py`; scenario 3 | MOCK |
| 8 | Bank-change gate | PARTIAL | `bank_change.py`; scenarios 7, 9 | Call-back scripted |
| 9 | Bound single-use approvals | PARTIAL | `approvals.binding_hash`, `approve`; `execute.execute`; `fx_quote.py`; `funding.py` | Not every path re-traced |
| 10 | No double pay on our side | PARTIAL | `replace_payment.py`; scenarios 13, 14 | External race not tested live |
| 11 | Reconciler (poll + webhook, closure rule) | PARTIAL | `reconcile.py`; `webhooks.py`; `worker.py`; `outcomes.decide_set` | Not every path enforces the closure rule |
| 12 | Console | PARTIAL | `console.py`; `frontend/` | MOCK, local, no login |
| 13 | Balance and FX funding check | PARTIAL | `fx_funding.py`; `fx_quote.py`; scenarios 24, 29 | MOCK |
| 14 | 30-incident harness + property test | PARTIAL | `incidents.py`; `incident_scenarios.py`; `tests/test_properties.py` | MOCK-only |
| 15 | Runnable demo, video, README | PARTIAL | `README.md`; `scripts/run_console.sh`; `backend.api` | Video NOT VERIFIED; live flow not run |
| 16 | Money back before money out (N1) | PARTIAL | `replacement_tool.refund_card` | Principal equality not checked |
| 17 | Colleague already re-paid (N2) | PARTIAL | `repaid.colleague_repaid`; scenario 6 | Supplier and date validation gap |

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

Beat names follow the plan's time table. Every beat is MOCK-ONLY, LIVE-HIST, or NOT VERIFIED. The mock hero replay exercises only beats 9 and 10 (see the report).

| # | Beat (plan time) | Code and tests | Status | Gap |
|---|---|---|---|---|
| 1 | Cold open, supplier email (0:00) | `claims.parse_email` (offline completer); `mock_replay.py` | MOCK-ONLY | Email text is a constant; live Claude not run |
| 2 | Investigate, colleague's scheduled duplicate (0:12) | `paid_once.scan_paid_once`; `sc_3` | MOCK-ONLY | Index lacks supplier matching |
| 3 | Stop the duplicate, approver ≠ proposer (0:45) | `stop_duplicate.py`; `approvals.py`; `execute.py`; `sc_3`, `sc_11` | MOCK-ONLY; LIVE-HIST for the cancel | Cost 0 proven only in the historical run |
| 4 | WAIT and proof while SENT, trace draft, outbox (1:08) | `proof.py`; `api_proof.py`; `outbox.py`; `sc_1`, `sc_20` | MOCK-ONLY | PDF synthetic; outbox does not send |
| 5 | New information: `failed` 90701, resend refused (1:35) | `webhooks._apply`; `proof.py`; `sc_22` | MOCK-ONLY | Replay uses a poll; hosted webhook not verified |
| 6 | Resolve: validate, call-back, new beneficiary (1:57) | `bank_change.verify_bank_change`; `sc_9` | MOCK-ONLY | Call-back scripted by approver |
| 7 | Money back first, PAYOUT_REVERSAL (2:20) | `replacement_tool.refund_card`; `sc_5` | MOCK-ONLY | Not full-principal equality |
| 8 | Funding and FX on a locked quote; reuse rejected (2:30) | `fx_funding.py`; `fx_quote.py`; `sc_24`, `sc_29` | MOCK-ONLY; LIVE-HIST for one conversion | Mock hero does not run the FX beat |
| 9 | One bound approval, one replacement, one per failure (2:48) | `approvals.py`; `replace_payment.py`; `sc_13`, `sc_14` | MOCK-ONLY | Mock hero `approved_replacement` PASS is canned replay only |
| 10 | Result: balances, paid once, incident cost (3:08) | `outcomes.decide_set`; `console.py`; `incidents.py` | MOCK-ONLY | Mock hero `reconcile_both` PASS is canned replay only |
| 11 | Credit and limits end card (3:40) | `README.md` Honest limits | NOT VERIFIED | Recording not made; no credit action taken |

## 5. Ordering and closure rules (build plan §3.4 and §2.1 #11/#17)

| Rule | Code | Status | Gap |
|---|---|---|---|
| 3.4-1 Verify first: HMAC over timestamp and raw body; stale rejected; dedupe on event id | `webhooks._verify`, `receive_webhook`; `test_webhooks.py` | STATIC | Synthetic signatures only |
| 3.4-2 Order by event time, then status rank; ignore backwards moves; ignore `funding.*` | `webhooks._apply` (`occurred > latest_event_ts_ms`; failure wins a tie; `_RANK` table present) | PARTIAL | Deviation: status-rank sort is not implemented as the plan describes. README states this |
| 3.4-3 `paid` provisional; settle window; later `failed` wins | `webhook_state`; `worker.py` settles windows; `reconcile.py` read | PARTIAL | Window is durable. Worker runs in the local process only |
| 3.4-4 Store failure reason on receipt | `webhooks._apply` calls `remember_failure_reason` | STATIC | |
| 3.4-5 Fast 2xx, async processing; polling as backstop | `api.py` POST route; `worker.py` polls every 5 s | PARTIAL | Apply is synchronous in the request. Retry behaviour untested |
| §2.1 #11 Close only when original and replacement are both accounted for | `outcomes.decide_set`, `reduce_outcome` | PARTIAL | Not every path calls the reducer |
| §2.1 #17 Colleague re-paid closes against the external transfer | `repaid.colleague_repaid` | PARTIAL | Matching gaps (see §2 #17) |

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
| 6 | Older README: live sandbox proof presented as current | 2026-11-04 run is historical | Relabelled; no final live PASS claimed |
| 7 | Older README: Airwallex kit labels and a "reference" wording | Removed | Generic Airwallex patterns credited |
| 8 | Older README: outside-repo links and a personal repo slug | Removed | |
| 9 | Older README: baseline described as a comparison | Static expectation table | Reworded |
| 10 | Older README: `reset_demo` resets the hero | `reset_demo` now delegates to the scoped hero reset | Mock reset removes only the manifest; the ledger is not reset |
| 11 | Presence check of three variable names | Blocked by the shell permission gate in an earlier pass | Not checked. Values never read |
