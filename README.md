# Aime Slack Triage

Receives Slack messages from one test channel, stores each one exactly once in Supabase/Postgres, has Claude label it `urgent` / `action` / `noise` via structured output, and posts an **approval request** to Slack. It never takes the proposed action; approving or rejecting only records the decision.

```mermaid
flowchart LR
  S[Slack event] -->|signed POST| E[/slack/events/]
  E -->|verify HMAC, filter, insert| DB[(Postgres)]
  E -->|200 within 3s| S
  E -.background.-> C[Claude: forced tool call + Pydantic validation]
  C --> A[approval_requests: pending]
  A --> P[Slack message with Approve / Reject]
  P -->|click| I[/slack/interactions/] -->|status + audit only| DB
```

## Run it

```bash
# Tests (no services needed; 42 tests, SQLite in memory)
pip install -r requirements-dev.txt
pytest

# Same suite against real Postgres
TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@localhost:5432/triage_test pytest

# Local stack (Postgres stands in for Supabase)
cp .env.example .env              # fill in Slack + Anthropic values
docker compose up --build
curl localhost:8000/health

# Fake a signed Slack event (no Slack app needed); send twice to see dedupe
SLACK_SIGNING_SECRET=... python -m scripts.send_test_event --text "Checkout is returning 500s" --event-id Ev1
```

**Supabase:** run `sql/schema.sql` in the SQL editor, set `DATABASE_URL` to the pooler connection string with the `postgresql+psycopg://` prefix, and drop the `db` service from compose.

**Slack:** create the app from `slack-app-manifest.yml`, expose port 8000 with a tunnel (`cloudflared tunnel --url http://localhost:8000` or ngrok), put that URL in the manifest, install the app, and invite the bot to the test channel.

## Endpoints

| Route | Purpose |
|---|---|
| `POST /slack/events` | Event intake: signature check, `url_verification`, dedupe, store, ack |
| `POST /slack/interactions` | Approve/Reject buttons; records the decision only |
| `GET /health` | DB connectivity, required config, backlog count; 503 if degraded |
| `GET /approvals?status=pending` | Review approvals without Slack (add auth before exposing publicly) |

## Where each requirement lives

| Requirement | Implementation |
|---|---|
| Duplicate prevention | Unique `event_id` (Slack retries) + unique `(channel_id, message_ts)` (same message, different envelope). Insert-and-catch-`IntegrityError`, so it is race-safe. Processing uses an atomic `UPDATE ... WHERE status IN (...)` claim, so a message is classified once. |
| Structured AI output | Forced tool call whose schema comes from the `ClassificationResult` Pydantic model (`extra="forbid"`, enum label, 0–1 confidence), then validated again in code. |
| Malformed AI responses | One repair retry that tells the model what was wrong; if that fails too, a fallback labels it `action` with confidence 0 and `is_fallback=true`, so it goes to a human instead of being dropped. |
| Rate limits | Shared `call_with_retry`: honors `Retry-After` on 429, exponential backoff with jitter for 5xx/529/timeouts. If retries run out, the event goes to `retry_pending` and `python -m scripts.reprocess` picks it up. |
| Expired tokens | Slack `token_expired` triggers one refresh via `oauth.v2.access` (token rotation). The new token pair is persisted, and the app also refreshes proactively when a token is within 5 minutes of expiry. Revoked or invalid tokens and Anthropic 401s fail fast and are audited. |
| Approval, not action | `approval_requests` table + Slack buttons. `decide()` only changes status. Double clicks are no-ops. |
| Audit log | Written in the same transaction as the change it describes. A Postgres trigger makes it append-only. |
| Health check | `/health` plus a Docker `HEALTHCHECK`. It does not call Slack or Anthropic, so it cannot burn rate limits. |

## Design notes and next steps

- **Ack then classify.** Slack retries if it doesn't get a 2xx within 3 seconds, so the event is stored and acknowledged first, and classification runs in a FastAPI background task. An event left `processing` for more than 10 minutes after a crash is picked up again by `reprocess`. In production I'd replace the background task with a durable queue (SQS or pg-boss) and run `reprocess` on a schedule.
- **Bot messages are ignored.** This also stops the app from looping on its own approval posts.
- **Prompt injection.** Slack text is treated as untrusted data inside `<message>` tags. The worst an injected message can do is change a label, because nothing executes without a human.
- **Noise.** Noise is stored and audited but does not create an approval. Set `CREATE_NOISE_APPROVALS=true` to change that.
- **Not built:** executing approved actions, auth on `/approvals`, and metrics/alerting on `failed` events.
