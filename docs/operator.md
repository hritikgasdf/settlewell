# Settlewell operator and reference notes

Secondary detail moved out of the README: operator commands, earlier live-run evidence, the local webhook worker, the 30-incident runner rules, the no-LLM baseline table and the MCP config. The [README](../README.md) is the starting point.

## Historical sandbox run (not current evidence)

A hero run was recorded on `api-demo.airwallex.com` on 2026-11-04, with `x-api-version` `2025-11-11`. The transcript and ids are in `docs/rehearsal.md`. It is listed here only so the history is visible:

- It ran against an earlier tree, before the current commits. It was **not re-run** for this document.
- It does not show that the current code passes a live run.
- Fresh current sandbox evidence follows; the older rehearsal remains historical.

## Live proof — 2026-11-08 (IST), this box

Fresh real requests to the Airwallex **sandbox** completed the hero money path. These are virtual funds, not production or a real bank callback. No credential values or raw API responses are committed.

| Object | Sandbox id | Observed outcome |
|---|---|---|
| Original transfer | `08ffee21-a813-4a10-96d9-8c18edec85cc` | SENT → simulated sandbox 90701 → CANCELLED; full EUR4200 cash reversal joined |
| Scheduled duplicate | `d5eae955-420d-4dd1-b2cc-6c811e77fe90` | CANCELLED through approved execute; fully paginated cash history shows zero ledger entries/charge |
| Replacement | `e64445c5-7942-408f-bcc2-924e0cfe60b4` | PAID; approved execute, request `stl-inv39391662-1`; EUR4200 plus actual EUR13.50 fee |
| Old beneficiary | `a69525a5-bab0-4466-af7c-18cad8c8e2bb` | Pinned original bank record |
| New beneficiary | `0c00da25-7210-42d0-a82c-5cf01f6875c1` | Created through approved execute; callback attestation scripted, not a real phone call |
| Locked quote | `1006e5ba-1813-38c5-ade0-7e5c0b4e7c3a` | Bound to approved conversion |
| Conversion | `103b9ab7-a7bc-468d-a146-ab62cb6843df` | SETTLED; bought EUR4340.50, sold USD4904.04 |

- Final live available EUR10005186.25 stayed above the deliberately stricter demo reserve floor EUR10005172.75. The higher configured floor intentionally exercised FX on an otherwise well-funded sandbox wallet; it did not bypass the gate.
- Original and replacement accounted for: obligation PAID_ONCE, primary incident CLOSED. The original `live_transfer_id` remains its immutable anchor.
- Genuine stored replacement PDF served HTTP200/application/pdf after fresh sandbox status read; cancelled original URL refused HTTP404/no stored bytes. The hero also observed cancellation policy refusing a new proof. PDF bytes/DB remain outside git.
- Separate live Claude parse: EUR4200, asks-where/new-account true; pinned match accepted, wrong pinned supplier refused. Invoice redacted in evidence.
- Console served in SANDBOX; HTTP outreach approval200, wrong hash409, replay409. Outreach remains queued, **not sent**. Replacement/FX browser clicks not tested.
- Mock runner: 30/30, 208 checks (**not** 30 live bank scenarios). Full local mock suite: 612 passed before the last focused PDF/reset fixes; their targeted regressions pass. Final-head CI is authoritative for the combined suite.
- Signed webhook simulator exercised loopback HTTP, not provider-hosted delivery. Scoped reset verified owned terminal transfers and removed only this run's DB/manifests; evidence was preserved outside git.
- One headless screenshot attempt timed out; no screenshot or video is claimed. Callback, email delivery, hosted webhooks, live concurrency/timeouts/quote replay/expiry and production operation remain unverified or simulated.

## Legacy console and helper scripts

Legacy read-only HTML console (Python 3.12 and uv):

```
uv sync --frozen
uv run python -m settlewell
```

The console listens on `http://127.0.0.1:8765/`. Mock mode needs no Airwallex credentials. From the inbox, open an incident, the money ledger, the outbox, or the replay at `http://127.0.0.1:8765/replay`.

The legacy `backend.console` HTML pages are read-only. They read the ledger at `SETTLEWELL_DB` (default `var/settlewell.db`) and do not call Airwallex from page handlers. The current React console uses the HTTP API, which selects its mode with `--mode mock` or `--mode sandbox`; sandbox mode uses the exact host credentials and does not require `SETTLEWELL_SANDBOX=1`. `uv run python -m backend.scripts.reset_demo` removes only the mock hero manifest and does not clear the ledger. Deleting the DB file is MOCK ONLY and only when the demo owns no remote payments. Do not delete a live DB or one with unresolved request IDs by hand; the only live reset is the owned hero `--reset` routine. Seed the same ledger with `uv run python -m backend.scripts.seed_ledger`.

The sandbox contract script (`SETTLEWELL_SANDBOX=1 uv run python -m backend.scripts.sandbox_contract`) calls the sandbox and is not run by the offline checks below.

## 30-incident runner rules

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

## Local demo API (MOCK only)

`uv run python -m backend.api` serves a JSON API on 127.0.0.1:8765 over a persisted SQLite demo DB (`var/settlewell.db`, seeded once). Approve, reject and cancel are routed through `backend.execute` with bound, single-use approvals. Writes need a loopback Host, an allowed Origin and the CSRF token from `GET /api/session`. They are accepted in MOCK mode. SANDBOX HTTP outreach approval, stale-hash refusal and replay refusal were verified live; replacement and FX ran via hero execute, not browser buttons. There is no production authentication.

Seeded demo (new DB only): INV-2291 has a claim parsed offline by `claims.parse_email` with a mock completer. The supplier identity comes from the obligation, not the email. The claim shows as matched only when the parse matched the pinned supplier. The INV-2291 proof is one generated MOCK / SYNTHETIC PDF stored in `proof_blob`. `GET /api/incidents/{id}/proof/{document_id}/pdf` serves it only when the transfer is the original anchor or the latest exactly-owned executed replacement, no webhook settle window is pending, the local and remote statuses allow a proof (SENT or PAID), and the stored hash matches. Otherwise it returns a JSON refusal with `gate_code`. An existing database is not reseeded. For a fresh MOCK demo, use a separate `--db` path; never delete or overwrite a sandbox ledger containing remote attempts.

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
- Limits: local demo only. No hosted webhook registration. The simulator sends synthetic events to loopback; sandbox mode uses the real adapter.

## Launcher and hero run (operator)

One-command console (mock is the default; ports 8765 API and 5173 UI):

```bash
bash scripts/run_console.sh                                      # mock
bash scripts/run_console.sh --mode mock --api-port 8877 --ui-port 15173
```

The script is not executable in the repo, so run it with `bash`. Ports must be 1024–65535, differ, and be free. An occupied port is refused, not killed. Sandbox mode is `bash scripts/run_console.sh --mode sandbox`, which reads the DB from the hero manifest.

Hero run against the sandbox (operator only; verified on this box):

```bash
uv run python -m backend.scripts.hero_run --sandbox --confirm-sandbox \
  --manifest var/hero-run-manifest.json --db var/hero-sandbox.db
uv run python -m backend.scripts.hero_run --reset --sandbox --confirm-sandbox \
  --manifest var/hero-run-manifest.json
```

- The sandbox run needs `--sandbox --confirm-sandbox`. Without it the command is refused.
- The owned reset releases only transfers in the run's manifest (or with an exact stored request id). It refuses to remove the manifest or DB while an owned transfer is unresolved.
- The complete sandbox money path was exercised on this box; see the live-proof IDs and explicit remaining limitations above.
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

Limits: local demo. No login. Writes need an allowed Origin and the per-process CSRF token. They are accepted in MOCK mode. SANDBOX HTTP outreach approval and replay refusal were verified; browser money-button clicks were not. The seeded demo exercises the cancel path end to end. The replacement and FX paths ran through hero execute, not console clicks. To reset this MOCK-only ledger, stop the backend and delete `var/settlewell.db`, but only when the demo owns no remote payments. Do not delete a live sandbox DB or one with unresolved request IDs by hand; the only live reset is the owned hero `--reset` routine.

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

## Feature freeze

The payment behaviour is frozen. No new actions, incidents or console pages. A rehearsal can still fix a broken run. The video and the writeup come after this line. Scoped live sandbox results and remaining partials are documented above; see `docs/plan-to-code.md`.

## MCP

Cursor reads `.cursor/mcp.json`.

| Server | What it is | Auth |
|---|---|---|
| `airwallex-docs` | Airwallex Docs MCP at `https://mcp.sandbox.airwallex.com/docs` | None |
| `airwallex-developer` | Official Developer MCP (sandbox) at `https://mcp.sandbox.airwallex.com/developer` | OAuth in Cursor |
| `airwallex-dev` | Local `@airwallex/developer-mcp` package (deprecated in favour of the remote Developer MCP) | `AIRWALLEX_SANDBOX_CLIENT_ID` and `AIRWALLEX_SANDBOX_API_KEY` from the environment |

The config uses Cursor's `${env:...}` interpolation, so no key is stored in git. Node.js 18 or newer is needed for `airwallex-dev`.
