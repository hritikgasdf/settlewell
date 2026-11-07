# P1-4 real Claude claim parser

`backend/claims.py` now calls the Anthropic Messages API (SDK `anthropic`) to *propose* a claim.
Code validates it and matches it to a pinned internal record. Nothing here moves money.

## Configuration
- `ANTHROPIC_API_KEY` (required unless `SETTLEWELL_MOCK=1`). Never logged or persisted.
- `ANTHROPIC_MODEL` (optional; empty means `claude-haiku-4-5`, pin `claude-haiku-4-5-20251001`).
- Fixed in code: `max_tokens=300`, 20 s timeout, 1 retry, no tools, email
  truncated at 8000 chars. A claim cannot choose model, key or base URL.
- `SETTLEWELL_MOCK=1` uses a deterministic offline fake (`mock_complete`); no SDK, no network.

## Trust boundary
1. The email is untrusted data, wrapped in `<email>` and described as such in the system prompt.
2. The model only returns JSON. `_claim_from_json` is the final guard: exact key set, types,
   finite positive amount, 3-letter currency, bounded invoice ref. Refusal, `max_tokens`,
   non-text, API/transport error, missing key all escalate (`R-MODEL-ERROR` / `R-BAD-JSON`).
3. Identity is `supplier_id`, a required keyword from our pinned records (e.g. the open incident's
   obligation). Email text, sender and model output are never identity. Absent or unknown ->
   `R-NO-IDENTITY` before any model call. A claim for another supplier's invoice ->
   `R-IDENTITY-MISMATCH`.
4. A matched claim only selects an obligation. It grants no approval or execution terms; money
   still goes through `backend.execute.execute` with an exact-intent claim.
5. Raw responses are not stored or logged; only model name and token counts are surfaced.

## Live check (opt-in, one call)
`SETTLEWELL_LIVE_CLAUDE=1 uv run pytest -q -s -m live_claude`
Needs `ANTHROPIC_API_KEY`. Uses synthetic data, forces `SETTLEWELL_MOCK=0`, prints redacted
parsed result, model and token counts, and writes `/workspace/settlewell-ctx/p14-live-proof.json`.

## Limitations
- No inbound email intake; the caller must supply the pinned `supplier_id`.
- No supplier resolution from sender (deliberately), no multi-transfer or metadata matching.
- No confidence score or semantic injection classifier; defence is strict schema + pinned identity.
- Usage is token counts only; no cost computation.
