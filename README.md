# Settlewell

Settlewell (formerly "Where's My Money?") is Hritik's Airwallex Agentic Banking Hackathon entry: a supplier payment incident commander.

This repository currently contains project setup only.

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

Copy `.env.example` to `.env` for local notes. `.env` is gitignored. Put values only in the environment, never in the repo.

Cloud agents read the two variables when they are saved as Runtime Secrets:

1. Open [Cloud Agents](https://cursor.com/dashboard/cloud-agents) and go to Secrets (My Secrets).
2. Add `AIRWALLEX_SANDBOX_CLIENT_ID` and `AIRWALLEX_SANDBOX_API_KEY` as Runtime Secrets.
3. Apply them to repository `hritikgasdf/settlewell`.
4. Start a new Cloud Agent. An agent that is already running does not see secrets added later.

For local Cursor, export the same two variables in the environment Cursor starts with, then reload MCP. `${env:...}` in `.cursor/mcp.json` reads that environment.
