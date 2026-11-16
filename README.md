# Settlewell

**An agentic payment-ops incident commander on Airwallex.** Settlewell takes a supplier's "where's my money?" email and runs the incident to closure: it understands the claim, diagnoses the payment, handles the low-risk steps itself, and asks a human only when money is at risk. Submitted to the Airwallex Agentic Banking Hackathon, Starter Kit 03 (Payment Ops Incident Commander), as "Settlewell (formerly Where's My Money?)".

> ## Try it live
> - **Live sandbox** (real Airwallex sandbox + Claude): **https://settlewell-sandbox.onrender.com**
> - **Mock demo** (resettable, no external calls): **https://settlewell-demo.onrender.com**
> - **Passcode:** provided to judges privately (not in this repo).
> - **First load may take ~1 minute** (Render free tier cold start).

## What it does and why

Accounts-payable teams lose hours on "we never got paid" emails. The answer is usually buried across transfer statuses, bank return codes, a colleague's duplicate payment and a request to "just pay our new account", which is the classic invoice-fraud path. A rushed human can double-pay or pay a fraudster.

Settlewell handles that incident end to end:

1. **Understand (AI).** Claude (Haiku) parses the supplier's email into a claim: invoice, amount, currency, new bank details. A strict validator checks it against the pinned supplier record.
2. **Diagnose (rules).** Deterministic rules read the live Airwallex transfers and decide one next step: wait, prove, trace, stop a duplicate, replace, or escalate. Examples: a colleague's still-scheduled **duplicate**, a **90701 "account closed"** bank return, **changed bank details**.
3. **Act on its own when risk is low (agent).** A fixed policy lets the agent re-read statuses, fetch return codes, attach status-gated proof, schedule rechecks, queue supplier updates and **cancel a proven scheduled duplicate** (cancel-only, never moves money).
4. **Ask a human only when money is at risk.** New bank details (with a call-back attestation), the replacement payment, FX conversions, amounts above a limit and anything unrecognised wait for a bound, single-use approval by a second person.
5. **Explain and audit.** Claude writes an advisory explanation, triage suggestion and editable supplier reply draft (never sent automatically). Every step, automatic or human, lands in the audit log with the rule that allowed it.

**Who it is for:** AP clerks and finance approvers at businesses paying suppliers through Airwallex.

**Key value**
- **Fewer human steps.** On the live sandbox hero run the agent handled 5 of 7 steps itself; only the bank change and the replacement payment needed a person (no FX top-up was needed on that run; when one is, the FX conversion is a third human step). The run took about 43 seconds. At roughly 3 minutes per manual step, that is an *estimated* ~15 minutes saved per incident (estimate, not measured).
- **Can't double-pay.** One replacement per failure, amounts copied from Airwallex's record, the obligation closes only when the invoice is paid once.
- **Fraud-safe bank changes.** New details become a new beneficiary only after validation and a call-back attestation; the old beneficiary is never modified.
- **AI for understanding, rules for money.** No model output can approve, execute or send anything (test-enforced).

## Using the hosted demo (step by step)

1. Open a URL above and enter the passcode at `/login` (cookie lasts 12 hours).
2. **Reset demo** (console header) gives a clean run. Mock: reseeds Lumen Textiles / INV-2291 instantly. Sandbox: cancels only this run's own transfers and reruns the live hero in the background (~30–40 s; at most once every 2 minutes; progress at `GET /api/demo/status`).
3. **Open the incident** from the Incidents list (INV-2291 on mock; the current run's invoice on sandbox; ids change on every reset or cold start).
4. Read the **Agent handled it** card: "N of M steps handled automatically", each step with its policy rule, and why the rest needed a human.
5. Read the **AI explanation (advisory)** card: why the rules chose the action, triage suggestion, and an editable supplier reply draft. Each section is labelled with its source: "Claude (Haiku)", "Rules engine" or "Template (no model)".
6. **Approve or Reject** any proposal waiting for a human (e.g. the duplicate cancel on mock when policy leaves it to a person). Approver must differ from proposer; approvals are single-use and bound to the exact terms.
7. Check the tabs: **Evidence** (proof PDF, bank return code), **Status history**, **Decisions** (rule ids), **Audit** (every action, "auto-approved by policy (rule X)" or the human approver). The **Funding check** card in the incident shows the balance vs reserve floor and any locked FX quote.
8. On sandbox, the human-gated steps of the autorun are approved by a scripted `finance-approver`, labelled "hosted autorun simulates this human approval".

## Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| Python | 3.12+ (`pyproject.toml`) | Backend |
| [uv](https://docs.astral.sh/uv/) | recent | Python deps and running |
| Node.js + npm | 20+ (`frontend/package.json`) | Building/running the React console |
| Docker | optional | One-command run |
| Airwallex sandbox client id + API key, Anthropic API key | — | **Sandbox mode only.** Mock mode needs no keys |

## Install

```bash
git clone https://github.com/hritikgasdf/settlewell.git && cd settlewell
uv sync --frozen                     # Python deps
(cd frontend && npm ci)              # console deps
```

## Run

**A. Docker (quickest, mock mode)**

```bash
docker build -t settlewell .
docker run --rm -p 8080:8080 -e SETTLEWELL_DEMO_PASSCODE=pick-one \
  -e SETTLEWELL_PUBLIC_ORIGIN=http://localhost:8080 settlewell
# open http://localhost:8080 and enter your passcode
```

**B. Local development (mock, two terminals)**

```bash
uv run python -m backend.api          # terminal 1: API on 127.0.0.1:8765 (add --no-worker to disable the worker)
cd frontend && npm run dev            # terminal 2: console on http://127.0.0.1:5173 (proxies /api)
```

`bash scripts/run_console.sh` starts both in one command (ports 8765 / 5173).

**C. Hosted-style single process (what Render runs)**

```bash
cd frontend && npm run build && cd ..
PORT=8080 SETTLEWELL_DEMO_PASSCODE=pick-one SETTLEWELL_PUBLIC_ORIGIN=http://localhost:8080 \
  uv run python -m backend.hosted     # API + built console on one origin; health check GET /health
```

**D. Sandbox mode (real Airwallex sandbox + Claude).** Set these in the environment (never in the repo): `AIRWALLEX_SANDBOX_CLIENT_ID`, `AIRWALLEX_SANDBOX_API_KEY`, `ANTHROPIC_API_KEY`, plus `SETTLEWELL_MODE=sandbox`, `SETTLEWELL_DEMO_PASSCODE` (required in sandbox), and optionally `SETTLEWELL_SANDBOX_AUTORUN=1`, `SETTLEWELL_AUTONOMY=1`, `SETTLEWELL_AUTO_MAX_AMOUNT=5000`. Then run option A or C. Operator hero-run commands are in [`docs/operator.md`](docs/operator.md#launcher-and-hero-run-operator).

### Hosted deployment details

Two Render free-plan services built from `Dockerfile` (`railway.json` is a Railway alternative). Start command `python -m backend.hosted`, listening on `0.0.0.0:$PORT` (default 8080).

- **Passcode gate.** With `SETTLEWELL_DEMO_PASSCODE` set, every page and API route except `/health` (and the HMAC-signed `/api/webhooks/airwallex`) needs the passcode. `/login` sets an HttpOnly, Secure, SameSite=Strict signed cookie for 12 hours. Failed attempts are rate limited per client and globally. Sandbox mode refuses to start without a passcode.
- **Same safety model.** Writes still need an allowed Host, an allowed Origin and the per-process CSRF token, and every money action still goes through `execute()` with a bound, single-use approval. `SETTLEWELL_PUBLIC_ORIGIN` (or Railway's `RAILWAY_PUBLIC_DOMAIN`, picked up automatically if you deploy there) adds the public host and origin; `SETTLEWELL_ALLOWED_HOSTS` adds extra exact hosts. With none of these set, the API is loopback-only as before.
- **Mock mode** (`SETTLEWELL_MODE=mock`, default): seeds Lumen Textiles / INV-2291 with a pending duplicate-stop proposal on every boot. **Reset demo** in the console header (or `POST /api/demo/reset`, CSRF-guarded) wipes and reseeds the synthetic ledger so judges can rerun it.
- **Sandbox mode** (`SETTLEWELL_MODE=sandbox`): needs `AIRWALLEX_SANDBOX_CLIENT_ID`, `AIRWALLEX_SANDBOX_API_KEY` and `ANTHROPIC_API_KEY`. With `SETTLEWELL_SANDBOX_AUTORUN=1`, the live sandbox hero runs once on first boot, and **Reset demo** releases or cancels only this run's own transfers (cancels go through `execute()`) and then reruns the hero in the background (at most once every 2 minutes; progress at `GET /api/demo/status`). Without it, the console shows the run recorded in the data directory, read from the hero manifest.
- **Data.** SQLite at `SETTLEWELL_DB` (default `/data/settlewell.db` when a `/data` volume is mounted, else `/tmp/settlewell/settlewell.db`). Sandbox runs keep `hero-run-manifest.json` and `hero-sandbox.db` beside it.

## Tests and checks

```bash
uv run pytest                                             # 727 tests, no network, no secrets
uv run ruff check .                                       # lint
SETTLEWELL_MOCK=1 uv run python -m backend.scripts.eval_harness   # 30/30 offline incident eval
SETTLEWELL_MOCK=1 uv run python -m backend.incidents --all        # 30-incident runner (exit 0 only if all PASS)
(cd frontend && npm run build)                            # type-check + production build
```

Tests run against fakes and a stateful mock remote; they do not prove bank behaviour. Runner rules are in [`docs/operator.md`](docs/operator.md#30-incident-runner-rules).

## Configuration

Sandbox API calls use base URL `https://api-demo.airwallex.com` and header `x-api-version: 2025-11-11`.

Variable names only (values never go in the repo):

| Name | Used by |
|---|---|
| `AIRWALLEX_SANDBOX_CLIENT_ID`, `AIRWALLEX_SANDBOX_API_KEY` | Sandbox adapter and the `airwallex-dev` MCP server |
| `ANTHROPIC_API_KEY` | Claude claim parser (live path) |
| `AIRWALLEX_WEBHOOK_SECRET` | Webhook signature check (missing or blank returns 503) |
| `SETTLEWELL_MOCK`, `SETTLEWELL_SANDBOX`, `SETTLEWELL_DB` | Mode and ledger path |
| `ANTHROPIC_MODEL` | Optional claim-parser model override (defaults to the Haiku model in `backend/claims.py`) |
| `PORT`, `SETTLEWELL_MODE` | Hosted demo: listen port and `mock` or `sandbox` |
| `SETTLEWELL_DEMO_PASSCODE`, `SETTLEWELL_SESSION_SECRET` | Hosted demo: passcode gate and optional cookie-signing secret |
| `SETTLEWELL_PUBLIC_ORIGIN`, `SETTLEWELL_ALLOWED_HOSTS` | Hosted demo: public origin and extra exact Host names |
| `SETTLEWELL_STATIC_DIR`, `SETTLEWELL_RESET_ON_BOOT`, `SETTLEWELL_SANDBOX_AUTORUN` | Hosted demo: built UI path, mock reseed on boot (default on), sandbox hero autorun and rerun (default off) |
| `SETTLEWELL_AUTONOMY`, `SETTLEWELL_AUTO_MAX_AMOUNT` | Risk-tiered autonomy kill switch (code default off; hosted default on) and auto limit in EUR (code default 1000; hosted default 5000) |

For cloud agents, add the sandbox variables as runtime secrets in the agent dashboard, then start a new agent; a running agent does not see secrets added later. For local Cursor, export the same variables in the environment Cursor starts with, then reload MCP.

Copy `.env.example` to `.env` for a local list of names; `.env` is gitignored.

## Architecture

- **Backend** (`backend/`, FastAPI JSON API over SQLite): `claims.py` (Claude parse + validation), rules/decisions, `autonomy.py` (fixed policy table), `approvals.py` + `execute.py` (the only path to a money action), `reconcile.py`, `worker.py` (rechecks, settle windows), `advisory.py` (read-only Claude explainer/triage/reply draft), Airwallex adapter pinned to `x-api-version: 2025-11-11` on `https://api-demo.airwallex.com`.
- **Console** (`frontend/`, React + Vite + TypeScript): incidents, payments, bank versions, funding, proposals, audit.
- **Hosted** (`backend/hosted.py`): one container serving API and console, passcode gate, CSRF + Origin checks on every write.

### Risk-tiered autonomy policy (`backend/autonomy.py`)

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

Auto steps still go through `execute()` with a single-use approval consumed as `policy:<rule id>`; `execute()` re-checks the table before any remote call, so a forged policy approval on a payout, FX or beneficiary change is refused. `SETTLEWELL_AUTONOMY=0` (code default) puts a human on every step. The hosted demos run with autonomy on and a EUR 5,000 auto limit so the EUR 4,200 scheduled duplicate qualifies.

### What is live vs mock

| Surface | Status | Label in the product |
|---|---|---|
| Proof PDF | **Sandbox: genuine bank PDF**, fetched, persisted and served only after live status/ownership/hash checks. **Mock seed: synthetic**, never bank proof | Origin label; mock bytes explicitly labelled synthetic |
| Supplier reply in the outbox | **Local queue only.** Nothing is delivered. No email is sent | Outbox row plus an audit row |
| Webhooks in the local API | **Simulated.** `backend.scripts.simulate_webhook` sends signed synthetic events to loopback. Hosted delivery from Airwallex is **not verified**; no webhook registration is wired | MOCK |
| Fees | Use provider fee when available; otherwise a **conservative estimated upper bound**, not a provider quote. 2026-11-08 live run: actual EUR13.50 payout fee confirmed in cash history | Estimated fallback remains labelled |
| Sandbox money | **Virtual.** Airwallex sandbox simulated funds, no real bank movement | Banner: synthetic supplier, sandbox funds |
| Production use | **Unsupported.** No production credentials, hosting, monitoring or key management exist | Not offered |
| Claim parser | **Live Claude verified** on this box with `claude-haiku-4-5-20251001`; strict validation and pinned-supplier matching retained. Offline tests use a deterministic completer | Model proposes only; no money tools |
| Advisory AI (explainer, supplier reply draft, unknown-incident triage) | **Advisory only.** Claude Haiku on the sandbox host when `ANTHROPIC_API_KEY` is set; deterministic template in MOCK, without a key, or on error. Read-only GET; no money module imports it (test-enforced); reply draft is never sent | Each section labelled Claude or template |
| Beneficiary change | A person enters the call-back attestation text. Software does not verify the call-back or the bank | Attestation recorded, not verified |
| User authentication | **None locally; a shared demo passcode when hosted** (see [Hosted deployment details](#hosted-deployment-details)). No per-user login. Writes need a loopback Host, an allowed Origin and a per-process CSRF token. Approver identities are demo labels | MOCK |
| Replay (`/replay`) | **MOCK.** Canned statuses, letter, `PAYOUT_REVERSAL` and quote rate | MOCK |
| 30-incident eval and runner | **Offline MOCK.** No Airwallex call, no secrets | MOCK |
| Hero scenario world setup | **Sandbox-only seed** (`Hero.seed_scenario_world`): only the pre-existing world is created directly: prerequisite original beneficiary, original payment, colleague duplicate. It refuses outside the confirmed sandbox before any remote write. Every Settlewell action (duplicate cancel, new-bank beneficiary, replacement, FX, and any cancel done by `--reset`) runs through `execute()` with a bound single-use approval. Simulated bank outcomes (`SandboxDriver` SENT/return) are scenario stimuli, not app actions | SANDBOX only; never MOCK or production |
| Sandbox React console/API | **Live adapter wired**. HTTP outreach approval verified; replacement and FX executed by hero through the same execution boundary, not clicked in the browser | SANDBOX banner; local-only demo identities |

## Honest limits

- **Mock replay** uses canned transfer statuses, a canned confirmation letter, a canned `PAYOUT_REVERSAL` and a canned quote rate. The fidelity table is on `/replay` and in `backend/mock_replay.py`. ETA and the cut-off on that page are labels, not Airwallex API fields.
- **Traces** are drafted support requests. Settlewell does not retrieve an MT103 or a UETR.
- **Cancelling a PROCESSING transfer** is not a behaviour this demo claims.
- **Supplier replies** sit in the local outbox until a person approves them. Nothing is sent, and no email is delivered.
- **Claude parsing**: live Haiku parse and pinned-identity rejection verified; offline tests use a deterministic completer.
- **Approvals**: the bound, single-use approval is implemented in `backend/approvals.py` and `backend/execute.py`. The FX quote and payout paths check the binding hash too. Not every path has been traced end to end in the current pass, so `docs/plan-to-code.md` lists the status of each item. The 2026-11-07 audit is historical; several of its findings are fixed in current code.
- **Beneficiary change**: the call-back attestation is entered by a person. Software does not verify it.
- **Webhooks**: simulated locally. Hosted delivery and retry behaviour are not verified.
- **Fees**: provider fee used where available, otherwise conservative estimate. The actual replacement fee in the 2026-11-08 live run was EUR13.50, confirmed by cash history; a pre-send provider quote was not independently demonstrated.
- **Sandbox money** is virtual. Production use is unsupported.
- **Reset**: the sandbox reset releases only transfers the run owns (manifest id or exact stored request id). It refuses when it cannot verify. The mock reset removes only the manifest and does not clear the ledger. Remote state beyond the owned transfers is not cleaned.
- **Authentication**: none for users. CSRF protects local writes only.
- **Sandbox ids drift**: the hosted sandbox reruns the hero on reset and on cold start, so transfer ids change.

## Credits

The safety design uses documented Airwallex API patterns: amounts from the provider's transfer record, one replacement per failure, a separate approver, and a single-use quote lock for FX. Settlewell adds the duplicate-cancel path, status-gated proof, versioned bank details with call-back, a refund-confirmed replacement gate, approval-gated reply drafts, webhook handling with a settle window, advisory Claude output and risk-tiered autonomy. Built with Airwallex sandbox APIs and Anthropic Claude.

## More docs

- [`docs/writeup.md`](docs/writeup.md) — submission writeup
- [`docs/operator.md`](docs/operator.md) — operator commands, earlier live-run evidence, webhooks, no-LLM baseline, MCP
- [`docs/plan-to-code.md`](docs/plan-to-code.md) — per-item status against the plan
- [`docs/rehearsal.md`](docs/rehearsal.md) — earlier rehearsal transcript
