from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app.models import OAuthToken
from app.retry import RetriesExhausted
from app.slack_client import SlackAPIError, SlackAuthError, SlackClient, TokenStore


def make(session_factory, handler, *, refresh_token="xoxe-1", client_id="cid"):
    store = TokenStore(session_factory, "xoxb-old", refresh_token)
    client = SlackClient(httpx.Client(transport=httpx.MockTransport(handler)), store,
                         client_id=client_id, client_secret="secret", max_attempts=3, sleep=lambda s: None)
    return client, store


def test_expired_token_is_refreshed_and_request_retried(session_factory):
    auth_headers = []

    def handler(request):
        if request.url.path.endswith("oauth.v2.access"):
            assert b"grant_type=refresh_token" in request.read()
            return httpx.Response(200, json={"ok": True, "access_token": "xoxb-new",
                                             "refresh_token": "xoxe-2", "expires_in": 43200})
        auth_headers.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer xoxb-old":
            return httpx.Response(200, json={"ok": False, "error": "token_expired"})
        return httpx.Response(200, json={"ok": True, "ts": "1.2"})

    client, store = make(session_factory, handler)
    assert client.post_message("C1", "hi")["ts"] == "1.2"
    assert auth_headers == ["Bearer xoxb-old", "Bearer xoxb-new"]
    tok = store.get()
    assert (tok.access_token, tok.refresh_token) == ("xoxb-new", "xoxe-2")  # persisted


def test_expired_token_without_refresh_config_raises(session_factory):
    client, _ = make(session_factory, lambda r: httpx.Response(200, json={"ok": False, "error": "token_expired"}),
                     client_id=None)
    with pytest.raises(SlackAuthError):
        client.post_message("C1", "hi")


def test_revoked_token_does_not_refresh(session_factory):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json={"ok": False, "error": "token_revoked"})

    client, _ = make(session_factory, handler)
    with pytest.raises(SlackAuthError):
        client.post_message("C1", "hi")
    assert not any("oauth" in p for p in paths)


def test_proactive_refresh_before_expiry(session_factory):
    with session_factory() as s:
        s.add(OAuthToken(provider="slack", access_token="xoxb-old", refresh_token="xoxe-1",
                         expires_at=datetime.now(timezone.utc) + timedelta(minutes=2)))
        s.commit()
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.url.path.endswith("oauth.v2.access"):
            return httpx.Response(200, json={"ok": True, "access_token": "xoxb-new", "refresh_token": "xoxe-2",
                                             "expires_in": 43200})
        assert request.headers["Authorization"] == "Bearer xoxb-new"
        return httpx.Response(200, json={"ok": True, "ts": "1"})

    client, _ = make(session_factory, handler)
    client.post_message("C1", "hi")
    assert paths[0].endswith("oauth.v2.access")


def test_rate_limit_retry_after(session_factory):
    responses = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={"ok": True})])
    sleeps = []
    client, _ = make(session_factory, lambda r: next(responses))
    client.sleep = sleeps.append
    client.post_message("C1", "hi")
    assert sleeps == [3.0]


def test_rate_limit_exhausted(session_factory):
    client, _ = make(session_factory, lambda r: httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(RetriesExhausted):
        client.post_message("C1", "hi")


def test_other_api_errors_surface(session_factory):
    client, _ = make(session_factory, lambda r: httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))
    with pytest.raises(SlackAPIError):
        client.post_message("C1", "hi")
