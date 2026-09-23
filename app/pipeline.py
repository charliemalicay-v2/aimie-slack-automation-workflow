"""The workflow: ingest -> dedupe -> store -> classify -> approval request. Never acts."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.audit import audit
from app.config import Settings
from app.llm import ClassifierOutcome, LLMAuthError
from app.models import ApprovalRequest, Classification, SlackEvent
from app.retry import RetriesExhausted
from app.slack_client import SlackAPIError, SlackAuthError, approval_blocks

log = logging.getLogger(__name__)

ALLOWED_SUBTYPES: set[str] = set()  # e.g. {"file_share"} to accept file uploads
PROCESSABLE = ("received", "retry_pending")
STALE_CLAIM = timedelta(minutes=10)  # a "processing" row older than this was orphaned by a crash


class Classifier(Protocol):
    model: str

    def classify(self, text: str) -> ClassifierOutcome: ...


class ApprovalPoster(Protocol):
    def post_message(self, channel: str, text: str, blocks: list[dict] | None = None) -> dict: ...


@dataclass
class IngestResult:
    outcome: str  # stored | duplicate | ignored
    event_pk: int | None = None
    reason: str | None = None


class Pipeline:
    def __init__(self, session_factory: sessionmaker, classifier: Classifier,
                 slack: ApprovalPoster, settings: Settings):
        self.sf = session_factory
        self.classifier = classifier
        self.slack = slack
        self.settings = settings

    # ---- 1. ingest ------------------------------------------------------------
    def ingest(self, envelope: dict, retry_num: str | None = None) -> IngestResult:
        event = envelope.get("event") or {}
        skip = self._skip_reason(event)
        if skip:
            return IngestResult("ignored", reason=skip)

        with self.sf() as s:
            row = SlackEvent(
                event_id=envelope["event_id"],
                channel_id=event["channel"],
                message_ts=event["ts"],
                user_id=event.get("user"),
                text=event["text"],
                raw=envelope,
            )
            s.add(row)
            try:
                s.flush()  # hits the unique constraints now
            except IntegrityError:
                s.rollback()
                audit(s, "event.duplicate", "slack_event", envelope["event_id"], slack_retry_num=retry_num)
                s.commit()
                return IngestResult("duplicate", reason="event_id or channel+ts already stored")
            audit(s, "event.received", "slack_event", row.id,
                  event_id=row.event_id, channel=row.channel_id, slack_retry_num=retry_num)
            s.commit()
            return IngestResult("stored", event_pk=row.id)

    def _skip_reason(self, event: dict) -> str | None:
        if event.get("type") != "message":
            return f"unsupported event type {event.get('type')!r}"
        if event.get("bot_id") or event.get("subtype") == "bot_message":
            return "bot message (also prevents loops on our own approval posts)"
        if event.get("subtype") and event["subtype"] not in ALLOWED_SUBTYPES:
            return f"message subtype {event['subtype']!r}"
        if event.get("channel") != self.settings.slack_test_channel_id:
            return "not the test channel"
        if not (event.get("text") or "").strip():
            return "empty text"
        if not event.get("ts"):
            return "missing ts"
        return None

    # ---- 2. process (runs in the background, after Slack got its 200) ---------
    def process(self, event_pk: int) -> None:
        try:
            self._process(event_pk)
        except Exception as exc:  # a background task must never die silently
            log.exception("Processing failed for event %s", event_pk)
            self._set_status(event_pk, "failed", "event.failed", error=repr(exc))

    @staticmethod
    def _claimable():
        stale_before = datetime.now(timezone.utc) - STALE_CLAIM
        return or_(SlackEvent.status.in_(PROCESSABLE),
                   (SlackEvent.status == "processing") & (SlackEvent.claimed_at < stale_before))

    def _process(self, event_pk: int) -> None:
        # Atomic claim: a single UPDATE ... WHERE, so only one worker wins, even across processes.
        with self.sf() as s:
            claimed = s.execute(
                update(SlackEvent)
                .where(SlackEvent.id == event_pk, self._claimable())
                .values(status="processing", claimed_at=datetime.now(timezone.utc))
            ).rowcount
            s.commit()
        if not claimed:
            log.info("Event %s already claimed/processed; skipping", event_pk)
            return

        with self.sf() as s:
            text = s.get(SlackEvent, event_pk).text

        try:
            outcome = self.classifier.classify(text)
        except LLMAuthError as exc:
            self._set_status(event_pk, "failed", "classification.auth_failed", error=str(exc))
            return
        except RetriesExhausted as exc:
            self._set_status(event_pk, "retry_pending", "classification.rate_limited", error=str(exc))
            return

        r = outcome.result
        with self.sf() as s:
            s.add(Classification(event_pk=event_pk, label=r.label, confidence=r.confidence,
                                 reason=r.reason, suggested_action=r.suggested_action,
                                 model=self.classifier.model, is_fallback=outcome.is_fallback,
                                 attempts=outcome.attempts))
            audit(s, "classification.created", "slack_event", event_pk, label=r.label,
                  confidence=r.confidence, is_fallback=outcome.is_fallback,
                  attempts=outcome.attempts, error=outcome.error)

            if r.label == "noise" and not outcome.is_fallback and not self.settings.create_noise_approvals:
                s.get(SlackEvent, event_pk).status = "closed_noise"
                audit(s, "event.closed_noise", "slack_event", event_pk)
                s.commit()
                return

            approval = ApprovalRequest(event_pk=event_pk, label=r.label, proposed_action=r.suggested_action)
            s.add(approval)
            s.get(SlackEvent, event_pk).status = "approval_requested"
            s.flush()
            audit(s, "approval.requested", "approval", approval.id, event_pk=event_pk,
                  label=r.label, proposed_action=r.suggested_action)
            s.commit()
            approval_id = approval.id

        # Slack post is outside the DB transaction: the approval exists even if Slack is down.
        self._post_approval(approval_id, text, r.label, r.reason, r.suggested_action, outcome.is_fallback)

    def _post_approval(self, approval_id: int, text: str, label: str, reason: str,
                       proposed_action: str, is_fallback: bool) -> None:
        try:
            resp = self.slack.post_message(
                self.settings.slack_approval_channel_id,
                text=f"Approval needed ({label}): {proposed_action}",
                blocks=approval_blocks(approval_id, label, text, reason, proposed_action, is_fallback),
            )
        except (SlackAuthError, SlackAPIError, RetriesExhausted) as exc:
            with self.sf() as s:
                audit(s, "approval.post_failed", "approval", approval_id, error=repr(exc))
                s.commit()
            return
        with self.sf() as s:
            s.get(ApprovalRequest, approval_id).slack_message_ts = resp.get("ts")
            audit(s, "approval.posted", "approval", approval_id, slack_ts=resp.get("ts"))
            s.commit()

    def _set_status(self, event_pk: int, status: str, action: str, **details) -> None:
        with self.sf() as s:
            ev = s.get(SlackEvent, event_pk)
            if ev is not None:
                ev.status = status
            audit(s, action, "slack_event", event_pk, **details)
            s.commit()

    # ---- 3. human decision ------------------------------------------------------
    def decide(self, approval_id: int, decision: str, user_id: str) -> str:
        if decision not in ("approve", "reject"):
            raise ValueError(decision)
        new_status = "approved" if decision == "approve" else "rejected"
        with self.sf() as s:
            approval = s.get(ApprovalRequest, approval_id)
            if approval is None:
                return "not_found"
            if approval.status != "pending":  # double-clicks / Slack retries are no-ops
                audit(s, "approval.decision_ignored", "approval", approval_id, actor=user_id,
                      current_status=approval.status, attempted=new_status)
                s.commit()
                return "already_decided"
            approval.status = new_status
            approval.decided_by = user_id
            approval.decided_at = datetime.now(timezone.utc)
            # Deliberately NO side effect here. An executor would be a separate,
            # explicitly-enabled step that reads approved rows.
            audit(s, f"approval.{new_status}", "approval", approval_id, actor=user_id,
                  executed=False, note="approval-only mode: no action taken")
            s.commit()
            return new_status

    # ---- 4. recovery ------------------------------------------------------------
    def reprocess_pending(self) -> int:
        """Re-run events left behind by a crash or rate limit. Safe to run anytime."""
        with self.sf() as s:
            ids = s.scalars(select(SlackEvent.id).where(self._claimable())).all()
        for event_pk in ids:
            self.process(event_pk)
        return len(ids)
