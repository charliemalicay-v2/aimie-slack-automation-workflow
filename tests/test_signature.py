import json
import time

from app.slack_verify import verify_slack_signature
from tests.conftest import SECRET, sign


def test_valid_signature():
    body = b'{"a":1}'
    h = sign(body)
    assert verify_slack_signature(SECRET, body, h["X-Slack-Request-Timestamp"], h["X-Slack-Signature"])


def test_tampered_body_rejected():
    h = sign(b'{"a":1}')
    assert not verify_slack_signature(SECRET, b'{"a":2}', h["X-Slack-Request-Timestamp"], h["X-Slack-Signature"])


def test_stale_timestamp_rejected():
    body = b"{}"
    h = sign(body, ts=int(time.time()) - 600)
    assert not verify_slack_signature(SECRET, body, h["X-Slack-Request-Timestamp"], h["X-Slack-Signature"])


def test_endpoint_rejects_unsigned(client):
    r = client.post("/slack/events", content=json.dumps({"type": "url_verification"}))
    assert r.status_code == 401


def test_url_verification(client):
    body = json.dumps({"type": "url_verification", "challenge": "abc"}).encode()
    r = client.post("/slack/events", content=body, headers=sign(body))
    assert r.json() == {"challenge": "abc"}


def test_malformed_json_body_is_400(client):
    body = b"not json"
    r = client.post("/slack/events", content=body, headers=sign(body))
    assert r.status_code == 400
