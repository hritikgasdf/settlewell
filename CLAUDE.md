# Settlewell coding brief

## Safety and workflow
- Every money action goes through backend.execute.execute and an exact-intent claim.
- Fail closed on missing evidence, ambiguity, stale terms, missing approval or unknown status.
- Persist durable attempt before remote send; recover by request_id before any retry.
- Preserve locked FX amount/currency and same-intent single-use approval binding.
- Never read, print, log or commit secrets or .env files. No live API calls in tests.
- Never push to main. Supervisor handles commits/push/PRs after review.
- Keep diffs minimal. Read only files needed for the current narrow task.

## Commands
- Targeted test: `SETTLEWELL_MOCK=1 uv run pytest -q -x <file>`
- Lint: `uv run ruff check <changed-files>`
- Final full check: `SETTLEWELL_MOCK=1 uv run pytest` and `uv run ruff check .`

## Backend module map
- `backend/__init__.py` — Settlewell backend. The Airwallex adapter lives in `backend.airwallex`.
- `backend/airwallex/__init__.py` — Airwallex REST adapter. Auth and the API version stay inside this package.
- `backend/airwallex/client.py` — Airwallex sandbox adapter.
- `backend/airwallex/http.py` — HTTP types for the Airwallex adapter. No credentials are logged.
- `backend/airwallex/types.py` — Typed Airwallex results. Money is Decimal so fees such as 13.47 stay exact.
- `backend/api.py` — Local-demo FastAPI (MOCK only). Writes call execute(); loopback, Origin and CSRF guarded. Start: `uv run python -m backend.api`.
- `backend/hosted.py` — Hosted demo entrypoint `python -m backend.hosted`: one process, API + built UI on 0.0.0.0:$PORT; mock reseed, sandbox hero autorun/rerun.
- `backend/hosting.py` — Hosted-demo settings: allowed hosts/origins, passcode gate (signed cookie, rate limit), static UI dir.
- `backend/api_proof.py` — Gated MOCK/SYNTHETIC proof PDF for the demo API. Status-gated; never Airwallex proof.
- `backend/incident_catalog.py` — The 30-incident plan catalog: expected actions and stage evidence.
- `backend/incident_outreach.py` — Non-money `outreach` intents (reply/TRACE/escalate): bound approval, execute(), outbox only.
- `backend/incident_remote.py` — Stateful mock remote for incident scenarios. No network.
- `backend/incident_scenarios.py` — One real scenario per incident, run through execute.
- `backend/incident_world.py` — Scenario world: ledger, remote, checks and helpers.
- `backend/incidents.py` — Runner `python -m backend.incidents --all`: PASS only with real scenario evidence.
- `backend/approvals.py` — Bound, single-use approvals. A changed detail voids the grant.
- `backend/bank_change.py` — New supplier details become a new beneficiary only after validate and a call-back.
- `backend/claims.py` — Strict claim parser. A model may only propose JSON. Code accepts it or escalates.
- `backend/console.py` — Mock console for the first four hero beats. No Airwallex calls.
- `backend/eval_cases.py` — The first 15 incident fixtures. Synthetic names only. No account data.
- `backend/eval_harness.py` — Run the first 15 incidents offline. No Airwallex calls and no secrets.
- `backend/eval_rest.py` — Incidents 16–30. Each one calls production code. No Airwallex and no secrets.
- `backend/execute.py` — Intent, approval, then one remote action. P0-2 and P0-3 call this module.
- `backend/funding.py` — Mandatory cash/refund gate, durable payout holds, before/after observations and ownership-safe recovery.
- `backend/fx_funding.py` — Buy EUR with USD when a replacement would leave less than the reserve floor.
- `backend/fx_quote.py` — One use per quote. Expiry is checked before approval and again before conversion.
- `backend/ledger.py` — SQLite ledger. Versions, snapshots, and the audit log are append-only.
- `backend/mock_replay.py` — Offline replay of the hero. Statuses, the letter, the reversal, and the quote are canned.
- `backend/outbox.py` — Supplier replies stay here until a person approves them. Nothing is sent.
- `backend/outcomes.py` — One complete-set outcome reducer for the original and every replacement. Unknown evidence never counts as success.
- `backend/paid_once.py` — Account-wide scan: a second live payment for one invoice is its own incident.
- `backend/policy.py` — Deterministic payment-incident policy. Codes come from Airwallex's failure-reasons table.
- `backend/proof.py` — Status-gated proof, a labelled ETA, and a TRACE draft. Nothing is sent.
- `backend/reconcile.py` — Recover our attempts by stored request id, read every payment, decide close or reopen once. No webhooks.
- `backend/repaid.py` — After a return, a colleague's payment for the same invoice is not replaced.
- `backend/replace_payment.py` — One replacement per attempt. The request id is shared by concurrent callers.
- `backend/replacement_tool.py` — The only replacement tool. Callers pass an obligation and an incident, never an amount.
- `backend/reset.py` — Reset the demo ledger and move sandbox transfers off SENT. No secrets are printed.
- `backend/sandbox.py` — Sandbox driver. This is the only place that calls Airwallex simulation.
- `backend/scripts/__init__.py` — Command-line checks. The sandbox contract is the only simulation entrypoint.
- `backend/scripts/eval_harness.py` — Score the first 15 incidents. SETTLEWELL_MOCK=1. No network and no secrets.
- `backend/scripts/reset_demo.py` — Reset the local hero ledger. Sandbox SENT transfers are released only on the demo host.
- `backend/scripts/sandbox_contract.py` — Prove create → SENT → FAILED(90701) → CANCELLED on the sandbox.
- `backend/scripts/seed_ledger.py` — Create Lumen Textiles, INV-2291, and its transfer in SQLite.
- `backend/stop_duplicate.py` — Cancel a scheduled duplicate only after an approval. A refusal is re-read.
- `backend/webhooks.py` — Webhook fast path. Signed, deduped, event-time ordered; a durable 10 s settle window, then the shared reconciler's remote read decides.
- `backend/worker.py` — Periodic worker: settles webhook windows, resumes consumed-approval intents via execute, polls incidents and conversions by stored request id.
- `backend/scripts/simulate_webhook.py` — Send one signed synthetic webhook to the loopback MOCK API. Prints only http status, event id and outcome.
