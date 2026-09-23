"""Slack request signing: https://api.slack.com/authentication/verifying-requests-from-slack"""
from __future__ import annotations

import hashlib
import hmac
import time


def verify_slack_signature(signing_secret: str, body: bytes, timestamp: str | None,
                           signature: str | None, now: float | None = None,
                           tolerance_seconds: int = 300) -> bool:
    if not signing_secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    now = time.time() if now is None else now
    if abs(now - ts) > tolerance_seconds:  # replay protection
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
