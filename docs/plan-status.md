# Plan status

Build-plan checklist §5, one line each. Items 8–13 are merged. Item 14 is in review. Items 15–45 are not started.

1. Claim Claude credits — done (Hritik). No PR.
2. Private repo and .gitignore — done. Merged https://github.com/hritikgasdf/settlewell/pull/1
3. Airwallex Docs MCP — done. https://github.com/hritikgasdf/settlewell/pull/1
4. Block calendar through Sun 15 Nov — not started (Hritik's). No PR.
5. Gmail filter for sandbox mail — not started (optional). No PR.
6. Local env — partial. `.env.example` landed in https://github.com/hritikgasdf/settlewell/pull/1. Python 3.12 + uv (`uv sync --frozen`, `uv run pytest`, `uv run ruff check`) is merged in https://github.com/hritikgasdf/settlewell/pull/2. Still open: loading `.env`, Node 20, and the LLM key.
7. Sandbox smoke (login, then balances) — done. Login 201, balances 200. No PR.
8. Airwallex adapter — merged. Token cache, re-login on 401, pinned `2025-11-11`, 5 requests/second, 429 backoff, typed calls for transfers, beneficiaries, letters, balances, and FX. https://github.com/hritikgasdf/settlewell/pull/3
9. Sandbox driver and contract check — merged. Create, SENT, FAILED 90701, CANCELLED on GET, letter, and balance-history join. https://github.com/hritikgasdf/settlewell/pull/4
10. Data model and ledger — merged. Seed creates Lumen Textiles GmbH, INV-2291, and its transfer. https://github.com/hritikgasdf/settlewell/pull/5
11. Poller, PAYOUT_REVERSAL join, and account-wide transfer index — merged. Timeline builds from polling, reversal join by source, external look-alikes tagged. https://github.com/hritikgasdf/settlewell/pull/6
12. Policy engine v1 and failure-code tests — merged. One action and rule id per published failure code. No proof after CANCELLED, no replace in flight, stop duplicate only while SCHEDULED. https://github.com/hritikgasdf/settlewell/pull/7
13. Claim parser and match to obligation — merged. Hero email matches INV-2291; bad JSON escalates. https://github.com/hritikgasdf/settlewell/pull/8
14. Account-wide paid-once scan — in review. A colleague's SCHEDULED duplicate is its own incident. https://github.com/hritikgasdf/settlewell/pull/9
15. WAIT, status-gated proof, ETA, and TRACE draft — in review. Proof attached while SENT; refused after CANCELLED. https://github.com/hritikgasdf/settlewell/pull/10
16. Approval service (bound hash, single use, void on change) — in review. Changed details void the approval; a second click is rejected. https://github.com/hritikgasdf/settlewell/pull/11
17. Approver ≠ proposer and two demo identities — in review. Self-approval is rejected. https://github.com/hritikgasdf/settlewell/pull/12
18. STOP_DUPLICATE cancel of a scheduled duplicate — in review. Scheduled duplicate cancels with no ledger impact; update_unsupported is re-read. https://github.com/hritikgasdf/settlewell/pull/13
19. REPLACE lock, request_id, and duplicate_request_id handling — in review. Concurrent replace makes one transfer. https://github.com/hritikgasdf/settlewell/pull/14
20. One replacement per failure, unknown outcome stays IN_FLIGHT, 6-day rule — in review. A second replacement is refused, and a timeout does not create attempt 2. https://github.com/hritikgasdf/settlewell/pull/15
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
