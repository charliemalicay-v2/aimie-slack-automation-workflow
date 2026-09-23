"""Send a correctly signed fake Slack event to a running instance (no Slack needed).

    python -m scripts.send_test_event --text "Checkout is returning 500s" --event-id Ev1
    python -m scripts.send_test_event --text "Checkout is returning 500s" --event-id Ev1   # -> duplicate
"""
import argparse
import hashlib
import hmac
import json
import os
import time

import httpx

parser = argparse.ArgumentParser()
parser.add_argument("--url", default="http://localhost:8000/slack/events")
parser.add_argument("--text", required=True)
parser.add_argument("--event-id", default=f"EvLocal{int(time.time())}")
parser.add_argument("--ts", default=None, help="message ts; reuse one to test channel+ts dedupe")
parser.add_argument("--channel", default=os.environ.get("SLACK_TEST_CHANNEL_ID", "C0TEST"))
args = parser.parse_args()

secret = os.environ["SLACK_SIGNING_SECRET"]
payload = {"type": "event_callback", "event_id": args.event_id, "event": {
    "type": "message", "channel": args.channel, "user": "ULOCAL", "text": args.text,
    "ts": args.ts or f"{time.time():.6f}"}}
body = json.dumps(payload).encode()
ts = str(int(time.time()))
sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
r = httpx.post(args.url, content=body, headers={"Content-Type": "application/json",
               "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
print(r.status_code, r.text)
