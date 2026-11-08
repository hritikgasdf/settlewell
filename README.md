# Settlewell

Settlewell (formerly "Where's My Money?") is Hritik's Airwallex Agentic Banking Hackathon entry: a supplier payment incident commander.

A supplier email says a payment never arrived. Settlewell matches it to an invoice, reads the transfer, and proposes one next step: wait, prove, trace, stop a duplicate, replace, or escalate. A second person approves anything that moves money. Amounts are copied from Airwallex's record.

## Reference

Settlewell's safety core follows Airwallex's kit 3 reference pattern — amounts copied from Airwallex's record, one replacement per failure, approver ≠ proposer, closure with both dispositions — and kit 1's single-use quote lock. What's ours: duplicate cancel, status-gated proof, verified bank-detail versions, refund-confirmed replacement, supplier-facing replies, webhooks.

New bank details become a new beneficiary after validate and a call-back. The previous beneficiary is left as it was.

## Live sandbox proof

One hero run on `api-demo.airwallex.com` on 2026-11-04, with `x-api-version` `2025-11-11` and `SETTLEWELL_RESERVE_FLOOR` set so the EUR shortfall was 100.00. The payment was 20.00 EUR over SWIFT. `reset_demo` ran first. The run took 37088 ms and passed. These are the Airwallex ids and the final status of each. The quote status is Settlewell's row.

| What | Id | Final status |
|---|---|---|
| Original transfer | `856ef116-4754-4bb4-870c-2fd461751da4` | Failed 90701 (account closed); Airwallex status CANCELLED |
| Cancelled duplicate | `78e1501e-0b9b-4544-984a-5111da33e5cb` | CANCELLED |
| Replacement transfer | `a9ec4848-8322-4c3b-b006-d5ac730459e9` | PAID |
| FX quote | `c4bd4ef1-afaf-3314-9269-0937af396e19` | CONSUMED |
| Conversion | `62f19891-3aca-46f2-9640-8d76ad349126` | SETTLED |

The duplicate cancel posted no EUR balance-history movement. A second claim on that quote was rejected. The same ids are in `docs/rehearsal.md`.

## What is live vs mock

| Surface | Which |
|---|---|
| `/replay`, and any screen that uses canned statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL`, or a canned quote rate | MOCK |
| The seeded INV-2291 console beats (4200.00 EUR, synthetic ids) | MOCK |
| Eval harness (`SETTLEWELL_MOCK=1`) | Offline. It does not call Airwallex |
| The 2026-11-04 sandbox run above: create, SENT, proof, scheduled duplicate cancel, `90701`, proof refused after cancel, new beneficiary, reversal, locked USD→EUR quote, one conversion, replacement PAID | Live on `api-demo.airwallex.com` |
| Sandbox console pages | Read the local ledger only. They do not call Airwallex |

## Setup

Python 3.12 and [uv](https://docs.astral.sh/uv/). From the repo root:

```
uv sync --frozen
uv run python -m settlewell
```

The console listens on `http://127.0.0.1:8765/`. Mock mode is the default, so this needs no Airwallex credentials. From the inbox, open an incident, the money ledger, the outbox, and the hero replay at `http://127.0.0.1:8765/replay`.

Sandbox mode is off unless you set `SETTLEWELL_SANDBOX=1`. That console reads the ledger at `SETTLEWELL_DB` (default `var/settlewell.db`) and does not call Airwallex from page handlers. The live contract is `SETTLEWELL_SANDBOX=1 uv run python -m backend.scripts.sandbox_contract`. Reset the local hero with `uv run python -m backend.scripts.reset_demo`. Seed the same ledger with `uv run python -m backend.scripts.seed_ledger`.

Score the 30 incidents offline:

```
SETTLEWELL_MOCK=1 uv run python -m backend.scripts.eval_harness
```

That prints one line per incident and then `30/30`. Without `SETTLEWELL_MOCK=1` it prints `eval skipped`.

`uv run pytest` runs the unit tests with no network and no secrets. `uv run ruff check .` is the lint.

Copy `.env.example` to `.env` if you want a local note of the variable names. The process reads the environment itself. `.env` is gitignored. Put values only in the environment, never in the repo. The mock console and the eval do not need those variables.

## Local demo API (MOCK only)

`uv run python -m backend.api` serves a JSON API on 127.0.0.1:8765 over a persisted SQLite demo DB (`var/settlewell.db`, seeded once). Approve, reject and cancel all run through `backend.execute`. Writes need a loopback Host, an allowed Origin and the CSRF token from `GET /api/session`, and work only in MOCK mode. There is no production authentication. Contract: `settlewell-ctx/p15-api-contract.md`.

### Webhooks and the periodic worker (local MOCK scope)

```bash
export AIRWALLEX_WEBHOOK_SECRET=<your local test secret>   # missing or blank => the endpoint answers 503
SETTLEWELL_MOCK=1 uv run python -m backend.api              # worker is on; add --no-worker to turn it off
uv run python -m backend.scripts.simulate_webhook --status PAID --transfer-id synthetic-inv-2291
```

- `POST /api/webhooks/airwallex` checks `x-signature` (hex HMAC-SHA256 of the `x-timestamp` header bytes plus the exact raw body, `x-timestamp` in Unix milliseconds) before parsing. Missing/bad signature, stale (>5 min) or future (>60 s) timestamps and bodies over 64 KiB are rejected. It is authenticated by signature, not by operator CSRF. Operator routes are unchanged.
- Event ids are deduplicated in SQLite. Ordering uses the payload's `created_at` (event time), not the delivery header. A later failure wins over an earlier PAID; on a timestamp tie the failure wins.
- A webhook never unlocks money. It opens a durable 10-second settle window. After it, the worker reads the remote status through the shared reconciler (`backend.reconcile`), which is the source of truth. If that read fails or is incomplete the window stays open and nothing closes or unlocks. Poll-only paths skip an incident while its window is open.
- The worker (`backend.worker`, every 5 s) settles windows, resumes a proposed intent whose approval is already consumed through `backend.execute` (same intent, no second approval; a refusal stays held and audited), polls every incident (so a later bank return reopens a closed one), and binds a consumed FX quote to its conversion only by stored request id.
- Limits: local MOCK only, no live webhook registration; the simulator sends synthetic events to loopback only. Real-adapter operation is not wired into this API.

## Operator console (frontend, MOCK only)

A React + Vite + TypeScript console in `frontend/`. It shows incidents, payments, bank versions, funding, proposals and audit, and can approve or reject proposals and cancel a scheduled duplicate payment. It talks only to the local demo API.

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

Limits: this is a local demo. The console has no login, and writes are accepted only when the backend runs in MOCK mode with an allowed Origin and the per-process CSRF token. The seeded demo is a `STOP_DUPLICATE` cancel, not a replacement. Proof PDFs are not available in this build. The seeded claim is absent. Only the cancel path is exercised end to end. To reset the demo, stop the backend and delete `var/settlewell.db`. Details: `settlewell-ctx/p15-ui-completion.md`.

## No-LLM baseline

Without the policy engine, every supplier email is handed to a person. That baseline is **ESCALATE** on all 30 incidents. It is a static table, not a second model and not a dashboard. Settlewell's column is what the offline harness expects.

| # | Incident | No-LLM baseline | Settlewell | Rule |
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
| 24 | Reserve floor and the FX limit | ESCALATE | R-FX-FLOOR-CONVERT | R-FX-LIMIT-ESCALATE |
| 25 | Paid then failed out of order | ESCALATE | CANCELLED | R-WEBHOOK-ORDER |
| 26 | Bank change without a call-back | ESCALATE | REJECT | R-BANK-CALLBACK-REQUIRED |
| 27 | Valid JSON smuggles a payment | ESCALATE | ESCALATE | R-BAD-JSON |
| 28 | Unknown supplier invoice | ESCALATE | ESCALATE | R-NO-MATCH |
| 29 | Quote consumed and expired | ESCALATE | R-FX-QUOTE-CONSUMED | R-FX-QUOTE-EXPIRED |
| 30 | Hero checks still hold | ESCALATE | PASS | R-HERO |

Case 24 reports `R-FX-FLOOR-CONVERT` when the EUR top-up is under the limit and `R-FX-LIMIT-ESCALATE` when it is over the limit. Case 29 reports `R-FX-QUOTE-CONSUMED` when a quote is used twice and `R-FX-QUOTE-EXPIRED` when the quote has expired. Case 7 proposes new details and does not pay them until a call-back verifies a new beneficiary.

## Honest limits

Mock replay uses canned transfer statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL`, and a canned quote rate. The fidelity table is on `http://127.0.0.1:8765/replay` and in [`backend/mock_replay.py`](backend/mock_replay.py) (heading "Mock fidelity", `id="fidelity"`). ETA and the cut-off on that page are labels, not Airwallex API fields. A trace is a drafted support request. Settlewell does not retrieve an MT103 or a UETR. Cancelling a `PROCESSING` transfer is not a behavior this demo claims.

Supplier replies sit in the outbox until a person approves them. The outbox does not send them.

The sandbox console reads the local ledger and does not call Airwallex from its pages. A refund card uses the transfer currency. A quote is claimed with one `UPDATE` before a conversion is created. A repeated webhook inserts the event id and stops on the unique constraint. Reset lists remote `SENT` transfers for at most five pages and releases them on the demo host.

## Feature freeze

The payment behaviour is frozen. No new actions, incidents, or console pages. A demo rehearsal can still fix a broken run. The video and the writeup come after this line.

## MCP

Cursor reads `.cursor/mcp.json` for cloud agents and for local Cursor.

| Server | What it is | Auth |
|---|---|---|
| `airwallex-docs` | Airwallex Docs MCP at `https://mcp.sandbox.airwallex.com/docs` | None |
| `airwallex-developer` | Official Developer MCP (sandbox) at `https://mcp.sandbox.airwallex.com/developer` | OAuth in Cursor |
| `airwallex-dev` | Local `@airwallex/developer-mcp` (0.3.0-beta.13) | `AIRWALLEX_SANDBOX_CLIENT_ID` and `AIRWALLEX_SANDBOX_API_KEY` from the environment |

Airwallex's current connector docs use the two remote URLs. The npm package is marked deprecated in favor of the remote Developer MCP. It stays in this config so agents can pass sandbox API credentials with Cursor's `${env:...}` interpolation and no key is stored in git. The package registers its sandbox tools only when both variables are set. `DISABLE_TELEMETRY` is on so the client id is not sent in telemetry.

Node.js 18 or newer is required for `airwallex-dev` (`npx` downloads the package on first use).

## Environment

Sandbox API calls use base URL `https://api-demo.airwallex.com` and header `x-api-version: 2025-11-11`.

Cloud agents read the two variables when they are saved as Runtime Secrets:

1. Open [Cloud Agents](https://cursor.com/dashboard/cloud-agents) and go to Secrets (My Secrets).
2. Add `AIRWALLEX_SANDBOX_CLIENT_ID` and `AIRWALLEX_SANDBOX_API_KEY` as Runtime Secrets.
3. Apply them to repository `hritikgasdf/settlewell`.
4. Start a new Cloud Agent. An agent that is already running does not see secrets added later.

For local Cursor, export the same two variables in the environment Cursor starts with, then reload MCP. `${env:...}` in `.cursor/mcp.json` reads that environment.
