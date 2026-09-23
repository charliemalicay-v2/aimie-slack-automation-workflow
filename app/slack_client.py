"""Slack Web API client: rate limits (429 + Retry-After) and expiring tokens.

With Slack token rotation enabled, bot tokens expire (~12h) and must be refreshed via
oauth.v2.access with grant_type=refresh_token. Slack returns a NEW refresh token each
time, so the latest pair is persisted (oauth_tokens table) instead of living only in memory.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import httpx
from sqlalchemy.orm import sessionmaker

from app.audit import audit
from app.models import OAuthToken
from app.retry import RetryableError, call_with_retry, parse_retry_after

log = logging.getLogger(__name__)
SLACK_API = "https://slack.com/api"
AUTH_ERRORS = {"token_expired", "invalid_auth", "not_authed", "token_revoked", "account_inactive"}


class SlackAuthError(Exception):
    pass


class SlackAPIError(Exception):
    pass


class _TokenProblem(Exception):
    def __init__(self, error: str):
        super().__init__(error)
        self.error = error


class TokenStore:
    PROVIDER = "slack"

    def __init__(self, session_factory: sessionmaker, initial_access: str, initial_refresh: str | None):
        self.session_factory = session_factory
        self.initial_access = initial_access
        self.initial_refresh = initial_refresh

    def get(self) -> OAuthToken:
        with self.session_factory() as s:
            row = s.get(OAuthToken, self.PROVIDER)
            if row is None:  # first boot: seed from env
                row = OAuthToken(provider=self.PROVIDER, access_token=self.initial_access,
                                 refresh_token=self.initial_refresh)
            return row

    def save(self, access: str, refresh: str | None, expires_in: int | None) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in) if expires_in else None
        with self.session_factory() as s:
            row = s.get(OAuthToken, self.PROVIDER) or OAuthToken(provider=self.PROVIDER, access_token=access)
            row.access_token, row.refresh_token, row.expires_at = access, refresh, expires_at
            row.updated_at = datetime.now(timezone.utc)
            s.merge(row)
            audit(s, "slack.token_refreshed", "oauth_token", self.PROVIDER, expires_in=expires_in)
            s.commit()


class SlackClient:
    def __init__(self, http: httpx.Client, tokens: TokenStore, *, client_id: str | None,
                 client_secret: str | None, max_attempts: int = 4,
                 sleep: Callable[[float], None] = time.sleep):
        self.http = http
        self.tokens = tokens
        self.client_id = client_id
        self.client_secret = client_secret
        self.max_attempts = max_attempts
        self.sleep = sleep
        self._refresh_lock = threading.Lock()

    # ---- public -------------------------------------------------------------
    def post_message(self, channel: str, text: str, blocks: list[dict] | None = None) -> dict:
        return self.call("chat.postMessage", {"channel": channel, "text": text, "blocks": blocks or []})

    def call(self, method: str, payload: dict) -> dict:
        self._refresh_if_expiring()
        try:
            return self._call_with_retry(method, payload)
        except _TokenProblem as exc:
            if exc.error == "token_expired" and self._can_refresh():
                log.info("Slack token expired; refreshing and retrying %s once", method)
                self.refresh()
                try:
                    return self._call_with_retry(method, payload)
                except _TokenProblem as exc2:
                    raise SlackAuthError(exc2.error) from exc2
            raise SlackAuthError(exc.error) from exc

    def refresh(self) -> None:
        with self._refresh_lock:
            current = self.tokens.get()
            resp = self.http.post(f"{SLACK_API}/oauth.v2.access", data={
                "grant_type": "refresh_token",
                "refresh_token": current.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }, timeout=15.0)
            data = resp.json()
            if not data.get("ok"):
                raise SlackAuthError(f"token refresh failed: {data.get('error')}")
            self.tokens.save(data["access_token"], data.get("refresh_token"), data.get("expires_in"))

    # ---- internals ----------------------------------------------------------
    def _can_refresh(self) -> bool:
        return bool(self.client_id and self.client_secret and self.tokens.get().refresh_token)

    def _refresh_if_expiring(self) -> None:
        tok = self.tokens.get()
        if tok.expires_at and self._can_refresh():
            expires_at = tok.expires_at if tok.expires_at.tzinfo else tok.expires_at.replace(tzinfo=timezone.utc)
            if expires_at - datetime.now(timezone.utc) < timedelta(minutes=5):
                self.refresh()

    def _call_with_retry(self, method: str, payload: dict) -> dict:
        def once() -> dict:
            token = self.tokens.get().access_token  # re-read: may have been refreshed
            try:
                resp = self.http.post(f"{SLACK_API}/{method}", json=payload, timeout=15.0,
                                      headers={"Authorization": f"Bearer {token}"})
            except httpx.TransportError as exc:
                raise RetryableError(f"transport error: {exc}") from exc
            if resp.status_code == 429:
                raise RetryableError("Slack rate limited",
                                     retry_after=parse_retry_after(resp.headers.get("Retry-After")))
            if resp.status_code >= 500:
                raise RetryableError(f"Slack HTTP {resp.status_code}")
            data = resp.json()
            if data.get("ok"):
                return data
            error = data.get("error", "unknown_error")
            if error in AUTH_ERRORS:
                raise _TokenProblem(error)
            if error == "ratelimited":
                raise RetryableError("Slack rate limited (body)")
            raise SlackAPIError(error)

        return call_with_retry(once, max_attempts=self.max_attempts, label=f"slack.{method}", sleep=self.sleep)


def approval_blocks(approval_id: int, label: str, text: str, reason: str, proposed_action: str,
                    is_fallback: bool) -> list[dict]:
    emoji = {"urgent": ":rotating_light:", "action": ":memo:", "noise": ":zzz:"}.get(label, "")
    note = "\n:warning: AI output was invalid, so this defaulted to human review." if is_fallback else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text":
            f"{emoji} *Approval needed* ({label})\n>{text[:500]}\n*Why:* {reason}\n"
            f"*Proposed action (not executed):* {proposed_action}{note}"}},
        {"type": "actions", "block_id": f"approval_{approval_id}", "elements": [
            {"type": "button", "action_id": "approve", "style": "primary",
             "text": {"type": "plain_text", "text": "Approve"}, "value": str(approval_id)},
            {"type": "button", "action_id": "reject", "style": "danger",
             "text": {"type": "plain_text", "text": "Reject"}, "value": str(approval_id)},
        ]},
    ]
