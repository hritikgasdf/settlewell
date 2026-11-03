# Plan status

Build-plan checklist §5, one line each. Item 8 is in an open PR. Items 9–45 are not started.

1. Claim Claude credits — done (Hritik). No PR.
2. Private repo and .gitignore — done. Merged https://github.com/hritikgasdf/settlewell/pull/1
3. Airwallex Docs MCP — done. https://github.com/hritikgasdf/settlewell/pull/1
4. Block calendar through Sun 15 Nov — not started (Hritik's). No PR.
5. Gmail filter for sandbox mail — not started (optional). No PR.
6. Local env — partial. `.env.example` landed in https://github.com/hritikgasdf/settlewell/pull/1. This PR wires Python 3.12 + uv (`uv sync --frozen`, `uv run pytest`, `uv run ruff check`). Still open: loading `.env`, Node 20, and the LLM key. https://github.com/hritikgasdf/settlewell/pull/2
7. Sandbox smoke (login, then balances) — done. Login 201, balances 200. No PR.
8. Airwallex adapter — done. Token cache, re-login on 401, pinned `2025-11-11`, 5 requests/second, 429 backoff, typed calls for transfers, beneficiaries, letters, balances, and FX. https://github.com/hritikgasdf/settlewell/pull/3
9. Sandbox driver and contract check — not started. No PR.
10. Data model and ledger — not started. No PR.
11. Poller, PAYOUT_REVERSAL join, and account-wide transfer index — not started. No PR.
12. Policy engine v1 and failure-code tests — not started. No PR.
13. Claim parser and match to obligation — not started. No PR.
14. Account-wide paid-once scan — not started. No PR.
15. WAIT, status-gated proof, ETA, and TRACE draft — not started. No PR.
16. Approval service (bound hash, single use, void on change) — not started. No PR.
17. Approver ≠ proposer and two demo identities — not started. No PR.
18. STOP_DUPLICATE cancel of a scheduled duplicate — not started. No PR.
19. REPLACE lock, request_id, and duplicate_request_id handling — not started. No PR.
20. One replacement per failure, unknown outcome stays IN_FLIGHT, 6-day rule — not started. No PR.
21. Replacement tool input is obligation_id + incident_ref only — not started. No PR.
22. N1 refund-confirmed replacement — not started. No PR.
23. Bank-change gate and new beneficiary — not started. No PR.
24. FX adapter, reserve floor, and limit — not started. No PR.
25. FxQuote single-use and expiry check — not started. No PR.
26. N2 colleague already re-paid — not started. No PR.
27. Webhook receiver (HMAC, dedupe, ordering, paid settle window) — not started. No PR.
28. Console skeleton — not started. No PR.
29. Money ledger screen — not started. No PR.
30. Visible outbox — not started. No PR.
31. Eval harness, first 15 incidents — not started. No PR.
32. Remaining 15 incidents — not started. No PR.
33. Property test — not started. No PR.
34. Seed/reset script that leaves nothing at SENT — not started. No PR.
35. One-command local run of the app — not started. No PR.
36. Offline mock mode that replays the hero — not started. No PR.
37. README with setup, reference credit, baseline table, and honest limits — not started. No PR.
38. Code readability pass — not started. No PR.
39. Feature freeze — not started. No PR.
40. Rehearse the hero twice on the sandbox — not started. No PR.
41. Rough-cut video and side clips — not started. No PR.
42. Writeup outline and draft — not started. No PR.
43. Final video and backup recording — not started. No PR.
44. Writeup final and judge repo access — not started. No PR.
45. Dry-run the form and submit — not started. No PR.
