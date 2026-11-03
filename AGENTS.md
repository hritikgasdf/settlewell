# Settlewell

Settlewell (formerly "Where's My Money?") is Hritik Gupta's entry for the Airwallex Agentic Banking Hackathon, starter kit 3.
It is a payment-ops incident commander: a supplier says a payment never arrived, and Settlewell decides to wait, prove, trace, stop a duplicate, or replace, with a person approving every money-out.
Airwallex's kit 3 reference replaces one failed payment. Settlewell handles the incidents that reference hands to a person (duplicate cancel, status-gated proof, verified bank-detail versions, refund-confirmed replacement).

## Folder map

| Path | What |
|---|---|
| `AGENTS.md` | This file |
| `README.md` | Repo intro and MCP setup |
| `.env.example` | Variable names only. `.env` is gitignored |
| `.cursor/mcp.json` | Airwallex docs and developer MCP |
| `.cursor/rules/` | Glob-scoped rules for the adapter, the ledger, and tests |
| `docs/plan-status.md` | Checklist items 1–45 |
| `settlewell/` | Dev entrypoint and runtime flags. Not the adapter, ledger, or poller |
| `tests/` | Fast unit tests. No network |
| `pyproject.toml`, `uv.lock` | Python 3.12 + uv |
| `backend/` | Planned: adapter, sandbox driver, ledger, reconciler, policy, approvals, API |
| `frontend/` | Planned: inbox, timeline, decision card, approval card, money ledger, outbox |
| `fixtures/`, `evals/` | Planned: synthetic cases and the 30-incident harness |

## Commands

From the repo root, Python 3.12:

- Install: `uv sync --frozen`
- Run: `uv run python -m settlewell`
- Test: `uv run pytest`
- Lint: `uv run ruff check .`
- Mock: `SETTLEWELL_MOCK=1 uv run python -m settlewell`

`uv run pytest` does not call Airwallex and does not read secrets. Sockets are blocked unless a test is marked `sandbox` and `SETTLEWELL_SANDBOX=1`. Mock mode prints `MOCK` and makes no API call. It does not replay the hero flow (checklist item 36). Run prints one line and exits; the console is checklist item 35 and is not built yet.

## Coding conventions

- Python 3.12. `uv run ruff check .` stays clean. Type public functions.
- Money decisions live in code: policy, locks, approvals. An LLM may parse supplier email and draft a reply. It never sees keys and never chooses an amount.
- Pin `x-api-version` in the adapter. Do not assemble auth or version headers at call sites.
- Copy amounts and fees from Airwallex responses. Do not hard-code the SWIFT fee.
- New bank details create a new beneficiary. An in-place update overwrites history.
- Simulation calls live in one sandbox driver, behind `SETTLEWELL_SANDBOX=1` or mock mode.
- Default tests pass with no secrets and no network. A live call uses the `sandbox` marker.
- Never log or print secrets: client id, API key, bearer token, webhook secret, or `Authorization`. Transfer ids and statuses are fine.

## Sandbox gotchas

Call `https://api-demo.airwallex.com`. Some docs cURL examples use `https://api.sandbox.airwallex.com`. Both are the sandbox; api-demo is the host that answered.

Login is `POST /api/v1/authentication/login` with headers `x-client-id` and `x-api-key` (token, `expires_at` about 30 minutes out). Send `x-api-version: 2025-11-11` on every call: transfers, simulation, letters, balances, and FX. A smoke call returned login 201 and balances 200.

`failure_reason` on a CANCELLED transfer persists only when that version header is set. With no `x-api-version`, GET returns `failure_type` and `failure_reason` null (the Oct 20 "cleared after cancel" result was this default). With `2025-11-11` the flat fields remain, for example `failure_reason: "90802: Beneficiary bank returned"`. The same is true of `GET /api/v1/transfers`. `failure_type` is `INCORRECT_ROUTING` for both a bank return (90802) and a recall (91001); classify on the code in `failure_reason`. Version `2026-06-30` replaces the two flat fields with `failure: {code, message, details.type}`. Do not unpin. FAILED itself lasts under a second, then the transfer is CANCELLED, so a poller often never sees status FAILED.

The same `request_id` returns 400 `duplicate_request_id` with the original id in `details.id`, including when the amount differs, including under concurrency. A new `request_id` with the same supplier, amount, and reference creates a second transfer. Airwallex does not dedupe by content. `GET /api/v1/transfers?request_id=` returns the one row after a timeout.

Webhooks arrive out of order. On create, `processing` can arrive before `scheduled`. On a bank return the sandbox emits `paid` and then `failed`: the path is SENT → PAID → FAILED → CANCELLED, and `cancelled` can arrive before `failed` even though `failed` has the earlier `x-timestamp`. Do not apply events in arrival order. Order by `x-timestamp`, then status rank (SCHEDULED < PROCESSING < SENT < PAID < FAILED < CANCELLED). Treat `paid` as provisional until a short settle window or a GET confirms PAID; a later `failed` wins. Verify HMAC-SHA256 over `x-timestamp` plus the raw body, compared as hex to `x-signature`. Dedupe on event id. `payout.transfer.failed` carries `failure_reason`. Retries after a non-2xx response were not tested; the poller is the backstop.

`POST /api/v1/confirmation_letters/create` returns a PDF at every status tried, including CANCELLED, and the supplier notice does not show status. Send proof only when live status is SENT or PAID.

India-HQ sandbox: EUR over LOCAL returns `beneficiary_type_unsupported`; EUR SWIFT works; GBP LOCAL works. Quotes come back `MULTI_USE`; the app must enforce single use. An expired quote is `expired_quote` on a conversion and `quote_expired` on a transfer. A return posts `PAYOUT_REVERSAL` for the principal and does not refund the fee. Read the fee from the response.

Simulation: FAILED only from SENT or PAID; CANCELLED only before dispatch; a future-dated SCHEDULED transfer returns 500 `operation_failed` if you try to transition it. Limits are 20 requests per second, 10 per endpoint, 10 concurrent. 429 body code is `too_many_requests`. There is no UETR or MT103 on the transfer; do not label an estimate as an Airwallex field.

Read `AIRWALLEX_SANDBOX_CLIENT_ID` and `AIRWALLEX_SANDBOX_API_KEY` from the environment. Never commit them, print them, or write them into logs.
