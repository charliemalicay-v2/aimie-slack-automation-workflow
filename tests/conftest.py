from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import Base, make_engine, make_session_factory
from app.llm import ClassificationResult, ClassifierOutcome
from app.main import create_app
from app.models import AuditLog

SECRET = "test-signing-secret"
TEST_CHANNEL = "C0TEST"


@pytest.fixture
def settings() -> Settings:
    return Settings(database_url="sqlite://", slack_signing_secret=SECRET,
                    slack_test_channel_id=TEST_CHANNEL, slack_approval_channel_id="C0APPROVE",
                    slack_bot_token="xoxb-test", ollama_base_url="http://localhost:11434")


# Default: in-memory SQLite. Set TEST_DATABASE_URL to run the same suite against Postgres,
# e.g. postgresql+psycopg://postgres:postgres@localhost:5432/triage_test
TEST_DB = os.environ.get("TEST_DATABASE_URL", "sqlite://")


@pytest.fixture
def session_factory():
    engine = make_engine(TEST_DB)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield make_session_factory(engine)
    Base.metadata.drop_all(engine)
    engine.dispose()


class FakeClassifier:
    model = "fake-model"

    def __init__(self, label="urgent", is_fallback=False, exc: Exception | None = None):
        self.label, self.is_fallback, self.exc, self.calls = label, is_fallback, exc, []

    def classify(self, text):
        self.calls.append(text)
        if self.exc:
            raise self.exc
        return ClassifierOutcome(ClassificationResult(label=self.label, confidence=0.9,
                                 reason="test", suggested_action="Page the on-call"),
                                 is_fallback=self.is_fallback, attempts=1)


class FakeSlack:
    def __init__(self, exc: Exception | None = None):
        self.exc, self.posts, self.updates = exc, [], []

    def post_message(self, channel, text, blocks=None):
        if self.exc:
            raise self.exc
        self.posts.append({"channel": channel, "text": text, "blocks": blocks})
        return {"ok": True, "ts": "1700000000.000100"}

    def update_message(self, channel, ts, text, blocks=None):
        if self.exc:
            raise self.exc
        self.updates.append({"channel": channel, "ts": ts, "text": text, "blocks": blocks})
        return {"ok": True, "ts": ts}


@pytest.fixture
def fake_classifier():
    return FakeClassifier()


@pytest.fixture
def fake_slack():
    return FakeSlack()


@pytest.fixture
def client(settings, session_factory, fake_classifier, fake_slack):
    app = create_app(settings, session_factory, fake_classifier, fake_slack)
    with TestClient(app) as c:
        yield c


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    ts = int(time.time()) if ts is None else ts
    sig = "v0=" + hmac.new(secret.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig}


def envelope(event_id="Ev001", text="Prod checkout is down!", channel=TEST_CHANNEL,
             ts="1700000000.000001", **event_overrides) -> dict:
    event = {"type": "message", "channel": channel, "user": "U123", "text": text, "ts": ts}
    event.update(event_overrides)
    return {"type": "event_callback", "event_id": event_id, "event": event}


def post_event(client, payload: dict, extra_headers: dict | None = None):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json", **sign(body), **(extra_headers or {})}
    return client.post("/slack/events", content=body, headers=headers)


def post_interaction(client, approval_id: int, action_id="approve", user="U999"):
    payload = {"type": "block_actions", "user": {"id": user},
               "actions": [{"action_id": action_id, "value": str(approval_id)}]}
    body = urlencode({"payload": json.dumps(payload)}).encode()
    headers = {"Content-Type": "application/x-www-form-urlencoded", **sign(body)}
    return client.post("/slack/interactions", content=body, headers=headers)


def audit_actions(session_factory) -> list[str]:
    with session_factory() as s:
        return list(s.scalars(select(AuditLog.action).order_by(AuditLog.id)))
