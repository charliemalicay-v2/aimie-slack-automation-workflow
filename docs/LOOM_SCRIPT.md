# 5-minute Loom outline

**0:00–0:30 · What it is**
README diagram on screen. "Slack test channel in, one row per message, Claude labels it, a human approves. Nothing executes automatically."

**0:30–2:00 · Live demo**
1. `docker compose up`, then `curl localhost:8000/health`.
2. Post "Checkout is returning 500s for everyone" in the test channel. Show the approval message with its buttons.
3. Post "thanks team!" and show it closed as noise with no approval.
4. Send the same event twice with `scripts/send_test_event.py --event-id Ev1`: first `stored`, then `duplicate`.
5. Click Approve, then show the `audit_log` rows in Supabase (`executed: false`).

**2:00–3:30 · Failure handling (show the code, not slides)**
- `app/retry.py`: Retry-After, backoff with jitter, `retry_pending` + `reprocess`.
- `app/llm.py`: forced tool call, Pydantic validation, repair retry, fallback to human review.
- `app/slack_client.py`: `token_expired` → refresh → persist → retry once.
- `app/pipeline.py`: two unique constraints and the atomic claim.

**3:30–4:30 · Tests**
Run `pytest` (42 pass). Open one test each for a malformed AI response, a 429 with Retry-After, token refresh, and a duplicate event.

**4:30–5:00 · Trade-offs and next steps**
Background task vs a durable queue; auth on `/approvals`; building an executor behind approvals.
