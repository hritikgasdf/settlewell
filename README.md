# Settlewell

Settlewell is a supplier-payment incident tool built as a local demo against the Airwallex sandbox API. It is not a production payments system.

A supplier email says a payment never arrived. The intended flow is: parse the email into a claim, match it to a synthetic invoice, read the transfer, and propose one next step (wait, prove, trace, stop a duplicate, replace, or escalate). Money-moving actions are meant to need a bound, single-use approval by a second identity. Each part of that flow has a different maturity level; the table in [What is live vs mock](#what-is-live-vs-mock) says which.

Read [Honest limits](#honest-limits) before relying on any claim here. Per-item status against the plan is in [`docs/plan-to-code.md`](docs/plan-to-code.md).

## Credits

The safety design uses documented Airwallex API patterns: amounts taken from the provider's transfer record, one replacement per failure, a separate approver, and a single-use quote lock for FX conversions. Settlewell's own additions are a duplicate-cancel path, status-gated proof, versioned bank details, a refund-confirmed replacement gate, reply drafts held for approval, and webhook handling with a settle window.

A new bank detail set becomes a new beneficiary only after validation and a call-back attestation. The previous beneficiary is not modified.

## What is live vs mock

Read this table before any claim in the README, the plan or a recording.

| Surface | Status | Label in the product |
|---|---|---|
| Proof PDF from the local demo API | **Synthetic.** Generated locally, not issued by Airwallex | `MOCK / SYNTHETIC - not Airwallex proof, no bank or network confirmation` |
| Supplier reply in the outbox | **Local queue only.** Nothing is delivered. No email is sent | Outbox row plus an audit row |
| Webhooks in the local API | **Simulated.** `backend.scripts.simulate_webhook` sends signed synthetic events to loopback. Hosted delivery from Airwallex is **not verified**; no webhook registration is wired | MOCK |
| Fees | **Conservative estimates** from the transfer record and the fee amounts shown in the sandbox. Not a provider fee quote | Labelled as estimated |
| Sandbox money | **Virtual.** Airwallex sandbox simulated funds, no real bank movement | Banner: synthetic supplier, sandbox funds |
| Production use | **Unsupported.** No production credentials, hosting, monitoring or key management exist | Not offered |
| Claim parser | **Code path calls Claude** (`ANTHROPIC_API_KEY`), but no fresh live call has been run from this tree. Tests and the demo use an offline deterministic completer | Offline parser in tests and demo |
| Beneficiary change | A person enters the call-back attestation text. Software does not verify the call-back or the bank | Attestation recorded, not verified |
| User authentication | **None.** The console has no login. Writes need a loopback Host, an allowed Origin and a per-process CSRF token. Approver identities are demo labels | MOCK |
| Replay (`/replay`) | **MOCK.** Canned statuses, letter, `PAYOUT_REVERSAL` and quote rate | MOCK |
| 30-incident eval and runner | **Offline MOCK.** No Airwallex call, no secrets | MOCK |
| Sandbox console pages | Read the local ledger. They do not call Airwallex | Local ledger |

## Historical sandbox run (not current evidence)

A hero run was recorded on `api-demo.airwallex.com` on 2026-11-04, with `x-api-version` `2025-11-11`. The transcript and ids are in `docs/rehearsal.md`. It is listed here only so the history is visible:

- It ran against an earlier tree, before the current commits. It was **not re-run** for this document.
- It does not show that the current code passes a live run.
- No final live PASS is claimed for the current tree. A new sandbox run is pending and would need an operator to start it on a machine that holds the sandbox credentials.

## Current verification status

- Final sandbox and real-Claude checks: **NOT RUN.** The requested local test computer was offline, so this tree has had no live, sandbox or real Claude call.
- A headless browser screenshot attempt failed (one attempt, no `console.png`).
- Offline MOCK checks: the API served in MOCK mode and the console dev server responded. The signed webhook simulator sent a synthetic event to the loopback MOCK API and passed. Ruff, the offline incident runner, the mock hero run and the frontend build also exited 0.
- The full `pytest` run in the same verification pass exited non-zero. No full-suite pass is claimed.

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/). From the repo root:

```
uv sync --frozen
uv run python -m settlewell
```

The console listens on `http://127.0.0.1:8765/`. Mock mode needs no Airwallex credentials. From the inbox, open an incident, the money ledger, the outbox, or the replay at `http://127.0.0.1:8765/replay`.

The legacy `backend.console` HTML pages are read-only. They read the ledger at `SETTLEWELL_DB` (default `var/settlewell.db`) and do not call Airwallex from page handlers. The current React console uses the HTTP API, which selects its mode with `--mode mock` or `--mode sandbox`; sandbox mode uses the exact host credentials and does not require `SETTLEWELL_SANDBOX=1`. `uv run python -m backend.scripts.reset_demo` removes only the mock hero manifest and does not clear the ledger. Deleting the DB file is MOCK ONLY and only when the demo owns no remote payments. Do not delete a live DB or one with unresolved request IDs by hand; the only live reset is the owned hero `--reset` routine. Seed the same ledger with `uv run python -m backend.scripts.seed_ledger`.

The sandbox contract script (`SETTLEWELL_SANDBOX=1 uv run python -m backend.scripts.sandbox_contract`) calls the sandbox and is not run by the offline checks below.

Score the 30 incidents offline:

```
SETTLEWELL_MOCK=1 uv run python -m backend.scripts.eval_harness
```

Run the incident runner over the 30-incident catalog:

```
SETTLEWELL_MOCK=1 uv run python -m backend.incidents --all
```

- Offline MOCK only. It calls no Airwallex endpoint and reads no secrets. A row is `PASS` only when its scenario runs through the claim, match, decision, intent, approval, `execute` and reconcile steps against the stateful mock remote and all its assertions hold. A row on a legacy fixture is `PARTIAL`.
- Exit code: `0` only when every row is `PASS`. `PARTIAL`, `FAIL` or `STUB` rows give exit `1`. `2` means `--all` was missing.
- Output: a markdown table (`#`, Title, Status, Expected, Observed, Transfers out, Notes) and a count line.
- Non-money replies (wait, proof, cancelled, trace, escalation) are `outreach` intents: proposed, approved once, bound to recipient and body hash, then `execute`. The effect is a queued outbox row and an audit row. Nothing is delivered.
- Assumed, not from the plan: LLM confidence threshold 0.8, and a minimum SENT age of 7 days for a trace. A claim with no `confidence` is accepted as legacy and not escalated. Escalation does not change `incident.state`.
- Incident 14 uses the label `LOOKUP_KEEP_IN_FLIGHT` in the plan. The table shows the held-unknown phase first, then `LOOKUP_RECOVER_BY_REQUEST_ID`.
- Incident 28 with no pinned supplier is refused before `execute`. The standard cancelled letter comes from the mock remote and is not stored as a proof document.
- The demo client is cancel-only. Replacement and FX paths are tested through `client_factory` with a stateful fake.

`uv run pytest` runs the unit tests with no network and no secrets. `uv run ruff check .` runs the lint. Tests check the code paths against fakes and the mock remote. They do not prove bank behaviour.

Copy `.env.example` to `.env` if you want a local note of the variable names. The process reads the environment itself. `.env` is gitignored. Put values only in the environment, never in the repo. The mock console and the eval do not need them.

## Local demo API (MOCK only)

`uv run python -m backend.api` serves a JSON API on 127.0.0.1:8765 over a persisted SQLite demo DB (`var/settlewell.db`, seeded once). Approve, reject and cancel are routed through `backend.execute` with bound, single-use approvals. Writes need a loopback Host, an allowed Origin and the CSRF token from `GET /api/session`. They are accepted in MOCK mode. The SANDBOX write guard is implemented and targeted MOCK-transport tested, but live sandbox/browser action is still NOT RUN, so SANDBOX console writes are not verified. There is no production authentication.

Seeded demo (new DB only): INV-2291 has a claim parsed offline by `claims.parse_email` with a mock completer. The supplier identity comes from the obligation, not the email. The claim shows as matched only when the parse matched the pinned supplier. The INV-2291 proof is one generated MOCK / SYNTHETIC PDF stored in `proof_blob`. `GET /api/incidents/{id}/proof/{document_id}/pdf` serves it only when the transfer is the live one, no webhook settle window is pending, the local and remote statuses allow a proof (SENT or PAID), and the stored hash matches. Otherwise it returns a JSON refusal with `gate_code`. An existing `var/settlewell.db` is not reseeded; delete it to get the seeded claim and PDF.

The replacement and FX tests use `client_factory` with a stateful fake remote. The default demo client implements only `get_transfer` and `cancel_transfer`, so a replacement or FX intent against it is refused, not sent.

### Webhooks and the periodic worker (local MOCK scope)

```bash
export AIRWALLEX_WEBHOOK_SECRET=<your local test secret>   # missing or blank => the endpoint answers 503
SETTLEWELL_MOCK=1 uv run python -m backend.api              # worker is on; add --no-worker to turn it off
uv run python -m backend.scripts.simulate_webhook --status PAID --transfer-id synthetic-inv-2291
```

- `POST /api/webhooks/airwallex` checks `x-signature` (hex HMAC-SHA256 of the `x-timestamp` header bytes plus the raw body, `x-timestamp` in Unix milliseconds) before parsing. Missing or bad signatures, stale (>5 min) or future (>60 s) timestamps, and bodies over 64 KiB are rejected. It is authenticated by signature, not by operator CSRF.
- Event ids are deduplicated in SQLite. The ordering key is the payload's `created_at`. A later failure beats an earlier PAID, and a failure wins a timestamp tie. The status-rank ordering in the plan is **not** implemented as a sort.
- A webhook never unlocks money. It opens a durable 10-second settle window. After that the worker reads the remote status through the shared reconciler (`backend.reconcile`). If that read fails or is incomplete, the window stays open and nothing closes or unlocks.
- The worker (`backend.worker`, every 5 s) settles windows, resumes an intent whose approval is already consumed (through `backend.execute`, no second approval), and polls every incident. It is a local process, not a hosted service.
- Limits: local MOCK only. No live webhook registration. The simulator sends synthetic events to loopback only. The real adapter is not wired into this API.

## Launcher and hero run (operator)

One-command console (mock is the default; ports 8765 API and 5173 UI):

```bash
bash scripts/run_console.sh                                      # mock
bash scripts/run_console.sh --mode mock --api-port 8877 --ui-port 15173
```

The script is not executable in the repo, so run it with `bash`. Ports must be 1024–65535, differ, and be free. An occupied port is refused, not killed. Sandbox mode is `bash scripts/run_console.sh --mode sandbox`, which reads the DB from the hero manifest.

Hero run against the sandbox (operator only; not run for this tree):

```bash
uv run python -m backend.scripts.hero_run --sandbox --confirm-sandbox \
  --manifest var/hero-run-manifest.json --db var/hero-sandbox.db
uv run python -m backend.scripts.hero_run --reset --sandbox --confirm-sandbox \
  --manifest var/hero-run-manifest.json
```

- The sandbox run needs `--sandbox --confirm-sandbox`. Without it the command is refused.
- The owned reset releases only transfers in the run's manifest (or with an exact stored request id). It refuses to remove the manifest or DB while an owned transfer is unresolved.
- The sandbox path has been exercised only against offline fakes. Live sandbox behaviour is unverified.
- The mock hero (`SETTLEWELL_MOCK=1 uv run python -m backend.scripts.hero_run --mock`) runs a canned replay. Only `approved_replacement` and `reconcile_both` can PASS. The other seven beats print PARTIAL.

## Operator console (frontend, MOCK only)

A React + Vite + TypeScript console in `frontend/`. It shows incidents, payments, bank versions, funding, proposals and audit. It can approve or reject proposals and cancel a scheduled duplicate. It talks only to the local demo API.

Requirements: Node.js 20 or newer (Vite 6) and npm. The backend must be running.

```
# terminal 1, repo root
uv run python -m backend.api

# terminal 2
cd frontend
npm ci
npm run dev        # http://127.0.0.1:5173 (proxies /api to 127.0.0.1:8765)
```

Build check and production-style preview:

```
cd frontend
npm ci
npm run build      # tsc --noEmit, then vite build into frontend/dist
npm run preview    # serves frontend/dist on 127.0.0.1:5173; the backend must still be running
```

Limits: local demo. No login. Writes need an allowed Origin and the per-process CSRF token. They are accepted in MOCK mode. SANDBOX-mode writes are not verified: the guard is implemented and targeted MOCK-transport tested, but live sandbox/browser action is NOT RUN (see the write-guard note above). The seeded demo exercises the cancel path end to end. The replacement and FX paths are not exercised from the console. To reset this MOCK-only ledger, stop the backend and delete `var/settlewell.db`, but only when the demo owns no remote payments. Do not delete a live sandbox DB or one with unresolved request IDs by hand; the only live reset is the owned hero `--reset` routine.

## No-LLM baseline (static table)

This is a static table of expected outcomes, not a measured experiment. The "baseline" column is what a process with no policy engine would do: hand every email to a person. It is not a comparison against an LLM, because neither path calls one. The Settlewell column is the rule the offline harness expects.

| # | Incident | No-policy baseline | Settlewell (expected) | Rule |
|---|---|---|---|---|
| 1 | Late SENT | ESCALATE | WAIT | R-NO-REPLACE-IN-FLIGHT |
| 2 | Still processing | ESCALATE | WAIT | R-NO-REPLACE-IN-FLIGHT |
| 3 | Scheduled duplicate | ESCALATE | STOP_DUPLICATE | R-STOP-DUPLICATE-SCHEDULED |
| 4 | Already paid | ESCALATE | WAIT | R-WAIT-PAID |
| 5 | Returned after a reversal | ESCALATE | REPLACE_SAME | R-REPLACE_SAME-90802 |
| 6 | Colleague already re-paid | ESCALATE | CLOSE | R-N2-REPAID |
| 7 | New details before a call-back | ESCALATE | REPLACE_NEW_DETAILS | R-NEW-DETAILS-UNVERIFIED |
| 8 | Return to the same beneficiary | ESCALATE | REPLACE_SAME | R-REPLACE_SAME-90802 |
| 9 | Account closed needs new details | ESCALATE | REPLACE_NEW_DETAILS | R-REPLACE_NEW_DETAILS-90701 |
| 10 | Details changed after approval | ESCALATE | VOID | R-APPROVAL-VOID |
| 11 | Second click and self-approval | ESCALATE | REJECT | R-APPROVAL-USED |
| 12 | Replayed paid webhook | ESCALATE | DEDUPE | R-WEBHOOK-REPLAY |
| 13 | One replacement under one request id | ESCALATE | ONE_TRANSFER | R-REPLACE-LOCK |
| 14 | Unknown timeout stays in flight | ESCALATE | IN_FLIGHT | R-REPLACE-UNKNOWN |
| 15 | Prompt injection | ESCALATE | ESCALATE | R-BAD-JSON |
| 16 | Invalid model JSON | ESCALATE | ESCALATE | R-BAD-JSON |
| 17 | Amount does not match the ledger | ESCALATE | ESCALATE | R-AMOUNT-MISMATCH |
| 18 | Recall is not a replacement | ESCALATE | ESCALATE | R-ESCALATE-91001 |
| 19 | Wrong currency claim | ESCALATE | ESCALATE | R-AMOUNT-MISMATCH |
| 20 | Supplier asks for a trace | ESCALATE | TRACE | R-TRACE-SENT |
| 21 | Replace while still sent | ESCALATE | WAIT | R-NO-REPLACE-IN-FLIGHT |
| 22 | Proof after cancel | ESCALATE | REFUSE | R-CANCELLED-NO-CODE |
| 23 | Fee is not refunded | ESCALATE | REPLACE_SAME | R-REPLACE_SAME-90802 |
| 24 | Reserve floor and the FX limit | ESCALATE | FX convert or escalate | R-FX-FLOOR-CONVERT / R-FX-LIMIT-ESCALATE |
| 25 | Paid then failed out of order | ESCALATE | CANCELLED | R-WEBHOOK-ORDER |
| 26 | Bank change without a call-back | ESCALATE | REJECT | R-BANK-CALLBACK-REQUIRED |
| 27 | Valid JSON smuggles a payment | ESCALATE | ESCALATE | R-BAD-JSON |
| 28 | Unknown supplier invoice | ESCALATE | ESCALATE | R-NO-MATCH |
| 29 | Quote consumed and expired | ESCALATE | RE-QUOTE + re-approve | R-FX-QUOTE-CONSUMED / R-FX-QUOTE-EXPIRED |
| 30 | Hero checks still hold | ESCALATE | PASS | R-HERO |

Case 7 proposes new details and pays them only after a call-back verifies a new beneficiary. Case 29 reports `R-FX-QUOTE-CONSUMED` on reuse and `R-FX-QUOTE-EXPIRED` after expiry.

## Honest limits

- **Mock replay** uses canned transfer statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL` and a canned quote rate. The fidelity table is on `/replay` and in `backend/mock_replay.py`. ETA and the cut-off on that page are labels, not Airwallex API fields.
- **Traces** are drafted support requests. Settlewell does not retrieve an MT103 or a UETR.
- **Cancelling a PROCESSING transfer** is not a behaviour this demo claims.
- **Supplier replies** sit in the local outbox until a person approves them. Nothing is sent, and no email is delivered.
- **Claude parsing**: the live call path exists, but this tree has no fresh live run. Offline tests use a deterministic completer.
- **Approvals**: the bound, single-use approval is implemented in `backend/approvals.py` and `backend/execute.py`. The FX quote and payout paths check the binding hash too. Not every path has been traced end to end in the current pass, so `docs/plan-to-code.md` lists the status of each item. The 2026-11-07 audit is historical; several of its findings are fixed in current code.
- **Beneficiary change**: the call-back attestation is entered by a person. Software does not verify it.
- **Webhooks**: simulated locally. Hosted delivery and retry behaviour are not verified.
- **Fees**: estimates. The provider fee quote is not captured.
- **Sandbox money** is virtual. Production use is unsupported.
- **Reset**: the sandbox reset releases only transfers the run owns (manifest id or exact stored request id). It refuses when it cannot verify. The mock reset removes only the manifest and does not clear the ledger. Remote state beyond the owned transfers is not cleaned.
- **Authentication**: none for users. CSRF protects local writes only.

## Feature freeze

The payment behaviour is frozen. No new actions, incidents or console pages. A rehearsal can still fix a broken run. The video and the writeup come after this line. Final test and live results are not asserted here; they are pending, see `docs/plan-to-code.md`.

## MCP

Cursor reads `.cursor/mcp.json`.

| Server | What it is | Auth |
|---|---|---|
| `airwallex-docs` | Airwallex Docs MCP at `https://mcp.sandbox.airwallex.com/docs` | None |
| `airwallex-developer` | Official Developer MCP (sandbox) at `https://mcp.sandbox.airwallex.com/developer` | OAuth in Cursor |
| `airwallex-dev` | Local `@airwallex/developer-mcp` package (deprecated in favour of the remote Developer MCP) | `AIRWALLEX_SANDBOX_CLIENT_ID` and `AIRWALLEX_SANDBOX_API_KEY` from the environment |

The config uses Cursor's `${env:...}` interpolation, so no key is stored in git. Node.js 18 or newer is needed for `airwallex-dev`.

## Environment

Sandbox API calls use base URL `https://api-demo.airwallex.com` and header `x-api-version: 2025-11-11`.

Variable names only (values never go in the repo):

| Name | Used by |
|---|---|
| `AIRWALLEX_SANDBOX_CLIENT_ID`, `AIRWALLEX_SANDBOX_API_KEY` | Sandbox adapter and the `airwallex-dev` MCP server |
| `ANTHROPIC_API_KEY` | Claude claim parser (live path) |
| `AIRWALLEX_WEBHOOK_SECRET` | Webhook signature check (missing or blank returns 503) |
| `SETTLEWELL_MOCK`, `SETTLEWELL_SANDBOX`, `SETTLEWELL_DB` | Mode and ledger path |

For cloud agents, add the sandbox variables as runtime secrets in the agent dashboard, then start a new agent; a running agent does not see secrets added later. For local Cursor, export the same variables in the environment Cursor starts with, then reload MCP.
