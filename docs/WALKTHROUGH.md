# Live walkthrough prep

## Likely questions

- **Why store first and classify in the background?** Slack's 3-second ack deadline. Calling the LLM inline would trigger Slack retries, which is where duplicates come from.
- **Why catch IntegrityError instead of checking whether the row exists?** Check-then-insert has a race when two retries arrive at once. The unique constraint is the only guard that is actually atomic.
- **Why two unique keys?** `event_id` covers Slack retrying the same delivery. `channel+ts` covers the same message arriving under a new event_id.
- **Why does the fallback use `action` and not `noise`?** Failing closed. An unclassifiable message goes to a human rather than disappearing.
- **Why a forced tool call and also Pydantic validation?** The schema guides the model, but only validation guarantees the result.
- **Why write your own retry instead of the SDK's?** So the policy is explicit, testable, shared between Slack and Anthropic, and so exhaustion becomes a state (`retry_pending`) rather than an exception.
- **What happens if the process dies mid-classification?** The row stays `processing` with `claimed_at` set. After 10 minutes, `reprocess` reclaims it.
- **Is the audit log trustworthy?** It commits in the same transaction as the change, and a Postgres trigger blocks UPDATE and DELETE.

## Likely live modifications (where to change)

| Ask | Change |
|---|---|
| Add a 4th label, e.g. `question` | `Literal[...]` in `ClassificationResult` (llm.py), the system prompt, the emoji map in `approval_blocks`, and the CHECK constraint in schema.sql |
| Also send noise to approval | `CREATE_NOISE_APPROVALS=true`, or the `if r.label == "noise"` branch in `pipeline._process` |
| Low confidence goes to a human | In `_process`: `if r.confidence < 0.6` treat as fallback |
| Watch multiple channels | Make `slack_test_channel_id` a set in config and update `_skip_reason` |
| More or fewer retries | `HTTP_MAX_ATTEMPTS`, `LLM_VALIDATION_ATTEMPTS` |
| Execute after approval | New `executor.py` invoked from `decide()` only when status is `approved`, audited with `executed=True`. It should be idempotent. |
| Swap to OpenAI | New class with the same `classify()` signature (the `Classifier` Protocol); use `response_format` with a json_schema and keep the Pydantic validation |
| Rate-limit inbound per user | Count recent `slack_events` by `user_id` in `ingest()` before inserting |

Practice one of these from scratch before the call, running `pytest` after each change.
