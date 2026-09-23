from sqlalchemy import func, select

from app.models import ApprovalRequest, AuditLog, Classification, SlackEvent
from app.retry import RetriesExhausted
from app.llm import LLMAuthError
from app.main import create_app
from app.slack_client import SlackAuthError
from fastapi.testclient import TestClient
from tests.conftest import FakeClassifier, FakeSlack, audit_actions, envelope, post_event, post_interaction


def count(sf, model):
    with sf() as s:
        return s.scalar(select(func.count()).select_from(model))


def make_client(settings, sf, classifier=None, slack=None):
    return TestClient(create_app(settings, sf, classifier or FakeClassifier(), slack or FakeSlack()))


def test_happy_path_creates_approval_and_takes_no_action(client, session_factory, fake_slack):
    r = post_event(client, envelope())
    assert r.status_code == 200 and r.json()["result"] == "stored"

    with session_factory() as s:
        ev = s.scalars(select(SlackEvent)).one()
        approval = s.scalars(select(ApprovalRequest)).one()
        assert ev.status == "approval_requested"
        assert approval.status == "pending" and approval.label == "urgent"
        assert approval.slack_message_ts == "1700000000.000100"
    assert len(fake_slack.posts) == 1  # the approval request itself, nothing else
    assert audit_actions(session_factory) == [
        "event.received", "classification.created", "approval.requested", "approval.posted"]


def test_duplicate_event_id_is_stored_once(client, session_factory, fake_classifier):
    post_event(client, envelope())
    r = post_event(client, envelope(), extra_headers={"X-Slack-Retry-Num": "1"})
    assert r.json()["result"] == "duplicate"
    assert count(session_factory, SlackEvent) == 1
    assert len(fake_classifier.calls) == 1  # AI called once, not twice
    assert "event.duplicate" in audit_actions(session_factory)


def test_same_message_different_event_id_is_duplicate(client, session_factory):
    post_event(client, envelope(event_id="Ev001"))
    r = post_event(client, envelope(event_id="Ev002"))  # same channel + ts
    assert r.json()["result"] == "duplicate"
    assert count(session_factory, SlackEvent) == 1


def test_ignores_other_channels_bots_and_edits(client, session_factory):
    assert post_event(client, envelope(channel="C0OTHER")).json()["result"] == "ignored"
    assert post_event(client, envelope(event_id="E2", bot_id="B1")).json()["result"] == "ignored"
    assert post_event(client, envelope(event_id="E3", subtype="message_changed")).json()["result"] == "ignored"
    assert post_event(client, envelope(event_id="E4", text="   ")).json()["result"] == "ignored"
    assert count(session_factory, SlackEvent) == 0


def test_noise_is_closed_without_approval(settings, session_factory):
    with make_client(settings, session_factory, FakeClassifier(label="noise")) as c:
        post_event(c, envelope(text="thanks all!"))
    with session_factory() as s:
        assert s.scalars(select(SlackEvent.status)).one() == "closed_noise"
    assert count(session_factory, ApprovalRequest) == 0


def test_fallback_classification_still_goes_to_human(settings, session_factory):
    with make_client(settings, session_factory, FakeClassifier(label="action", is_fallback=True)) as c:
        post_event(c, envelope())
    with session_factory() as s:
        assert s.scalars(select(Classification.is_fallback)).one() is True
    assert count(session_factory, ApprovalRequest) == 1


def test_llm_rate_limit_marks_retry_pending_then_reprocess(settings, session_factory):
    flaky = FakeClassifier(exc=RetriesExhausted("429 x4"))
    app = create_app(settings, session_factory, flaky, FakeSlack())
    with TestClient(app) as c:
        post_event(c, envelope())
    with session_factory() as s:
        assert s.scalars(select(SlackEvent.status)).one() == "retry_pending"

    flaky.exc = None  # rate limit is over
    assert app.state.pipeline.reprocess_pending() == 1
    with session_factory() as s:
        assert s.scalars(select(SlackEvent.status)).one() == "approval_requested"


def test_llm_auth_error_marks_failed(settings, session_factory):
    with make_client(settings, session_factory, FakeClassifier(exc=LLMAuthError("401"))) as c:
        post_event(c, envelope())
    with session_factory() as s:
        assert s.scalars(select(SlackEvent.status)).one() == "failed"
    assert "classification.auth_failed" in audit_actions(session_factory)


def test_slack_post_failure_keeps_approval_in_db(settings, session_factory):
    with make_client(settings, session_factory, slack=FakeSlack(exc=SlackAuthError("invalid_auth"))) as c:
        post_event(c, envelope())
    assert count(session_factory, ApprovalRequest) == 1
    assert "approval.post_failed" in audit_actions(session_factory)


def test_unexpected_crash_is_caught_and_audited(settings, session_factory):
    with make_client(settings, session_factory, FakeClassifier(exc=RuntimeError("boom"))) as c:
        assert post_event(c, envelope()).status_code == 200
    with session_factory() as s:
        assert s.scalars(select(SlackEvent.status)).one() == "failed"


def test_process_is_idempotent(client, session_factory, fake_classifier):
    post_event(client, envelope())
    client.app.state.pipeline.process(1)  # second run: already claimed
    assert len(fake_classifier.calls) == 1
    assert count(session_factory, ApprovalRequest) == 1


def test_approve_records_decision_without_executing(client, session_factory, fake_slack):
    post_event(client, envelope())
    r = post_interaction(client, approval_id=1, action_id="approve", user="U42")
    assert r.json()["result"] == "approved"
    with session_factory() as s:
        a = s.get(ApprovalRequest, 1)
        assert (a.status, a.decided_by) == ("approved", "U42")
        log = s.scalars(select(AuditLog).where(AuditLog.action == "approval.approved")).one()
        assert log.actor == "U42" and log.details["executed"] is False
    assert len(fake_slack.updates) == 1  # the original message got edited to show the decision
    update = fake_slack.updates[0]
    assert update["ts"] == "1700000000.000100"
    assert "Approved" in update["blocks"][-1]["elements"][0]["text"]
    assert "U42" in update["blocks"][-1]["elements"][0]["text"]


def test_reject_also_updates_the_message(client, session_factory, fake_slack):
    post_event(client, envelope())
    post_interaction(client, approval_id=1, action_id="reject", user="U42")
    assert len(fake_slack.updates) == 1
    assert "Rejected" in fake_slack.updates[0]["blocks"][-1]["elements"][0]["text"]


def test_double_click_does_not_update_message_twice(client, session_factory, fake_slack):
    post_event(client, envelope())
    post_interaction(client, 1, "approve")
    post_interaction(client, 1, "reject")
    assert len(fake_slack.updates) == 1  # second (ignored) decision doesn't touch Slack again


def test_double_click_is_ignored(client, session_factory):
    post_event(client, envelope())
    post_interaction(client, 1, "approve")
    r = post_interaction(client, 1, "reject")
    assert r.json()["result"] == "already_decided"
    with session_factory() as s:
        assert s.get(ApprovalRequest, 1).status == "approved"


def test_malformed_interaction_is_400(client):
    from tests.conftest import sign
    body = b"payload=%7Bnope"
    r = client.post("/slack/interactions", content=body, headers=sign(body))
    assert r.status_code == 400


def test_health_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "config": "ok"}


def test_health_degraded_when_config_missing(session_factory):
    from app.config import Settings
    bad = Settings(database_url="sqlite://", slack_signing_secret="", slack_test_channel_id="C",
                   slack_approval_channel_id="C", slack_bot_token="")
    with TestClient(create_app(bad, session_factory, FakeClassifier(), FakeSlack())) as c:
        r = c.get("/health")
    assert r.status_code == 503 and "SLACK_SIGNING_SECRET" in r.json()["checks"]["config"]


def test_orphaned_processing_row_is_recovered(settings, session_factory):
    from datetime import datetime, timedelta, timezone
    app = create_app(settings, session_factory, FakeClassifier(), FakeSlack())
    with session_factory() as s:  # simulate a crash mid-processing 30 minutes ago
        s.add(SlackEvent(event_id="Ev9", channel_id="C0TEST", message_ts="9.9", text="db is down",
                         raw={}, status="processing",
                         claimed_at=datetime.now(timezone.utc) - timedelta(minutes=30)))
        s.commit()
    assert app.state.pipeline.reprocess_pending() == 1
    with session_factory() as s:
        assert s.scalars(select(SlackEvent.status)).one() == "approval_requested"


def test_fresh_processing_row_is_left_alone(settings, session_factory):
    from datetime import datetime, timezone
    app = create_app(settings, session_factory, FakeClassifier(), FakeSlack())
    with session_factory() as s:
        s.add(SlackEvent(event_id="Ev9", channel_id="C0TEST", message_ts="9.9", text="x", raw={},
                         status="processing", claimed_at=datetime.now(timezone.utc)))
        s.commit()
    assert app.state.pipeline.reprocess_pending() == 0
