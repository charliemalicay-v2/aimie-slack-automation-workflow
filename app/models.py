"""Tables. sql/schema.sql is the Postgres/Supabase equivalent (source of truth in prod)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SlackEvent(Base):
    __tablename__ = "slack_events"
    # Two dedupe guards: Slack's event_id (covers Slack retries) and channel+ts
    # (covers the same message arriving under a different event envelope).
    __table_args__ = (UniqueConstraint("channel_id", "message_ts", name="uq_slack_events_channel_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    channel_id: Mapped[str] = mapped_column(String(32), nullable=False)
    message_ts: Mapped[str] = mapped_column(String(32), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text, nullable=False)
    raw: Mapped[dict] = mapped_column(JsonType, nullable=False)
    # received -> processing -> approval_requested | closed_noise | retry_pending | failed
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # set when processing starts


class Classification(Base):
    __tablename__ = "classifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_pk: Mapped[int] = mapped_column(ForeignKey("slack_events.id"), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)  # urgent | action | noise
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApprovalRequest(Base):
    __tablename__ = "approval_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_pk: Mapped[int] = mapped_column(ForeignKey("slack_events.id"), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_action: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")  # pending|approved|rejected
    slack_message_ts: Mapped[str | None] = mapped_column(String(32))
    decided_by: Mapped[str | None] = mapped_column(String(32))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    """Append-only. In Postgres a trigger (sql/schema.sql) blocks UPDATE/DELETE."""
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)       # "system" or a Slack user id
    action: Mapped[str] = mapped_column(String(64), nullable=False)      # e.g. "approval.requested"
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    details: Mapped[dict] = mapped_column(JsonType, nullable=False, default=dict)


class OAuthToken(Base):
    """Persists rotated Slack tokens so a restart doesn't lose the latest refresh token."""
    __tablename__ = "oauth_tokens"

    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
