"""HTTP layer. Keep it thin: verify, parse, hand off to Pipeline, answer Slack fast."""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker
from starlette.concurrency import run_in_threadpool

from app.config import Settings, load_settings
from app.db import Base, make_engine, make_session_factory
from app.llm import AnthropicClassifier
from app.models import ApprovalRequest, SlackEvent
from app.pipeline import ApprovalPoster, Classifier, Pipeline
from app.slack_client import SlackClient, TokenStore
from app.slack_verify import verify_slack_signature

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("app")
VERSION = "0.1.0"


def create_app(settings: Settings | None = None, session_factory: sessionmaker | None = None,
               classifier: Classifier | None = None, slack: ApprovalPoster | None = None) -> FastAPI:
    settings = settings or load_settings()
    if session_factory is None:
        engine = make_engine(settings.database_url)
        session_factory = make_session_factory(engine)
    engine = session_factory.kw["bind"]

    http = httpx.Client()
    classifier = classifier or AnthropicClassifier(
        settings.anthropic_api_key, settings.anthropic_model, http,
        http_max_attempts=settings.http_max_attempts,
        validation_attempts=settings.llm_validation_attempts)
    slack = slack or SlackClient(
        http, TokenStore(session_factory, settings.slack_bot_token, settings.slack_refresh_token),
        client_id=settings.slack_client_id, client_secret=settings.slack_client_secret,
        max_attempts=settings.http_max_attempts)
    pipeline = Pipeline(session_factory, classifier, slack, settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.auto_create_tables:
            Base.metadata.create_all(engine)
        yield
        http.close()

    app = FastAPI(title="Aime Slack triage", version=VERSION, lifespan=lifespan)
    app.state.pipeline = pipeline

    async def verified_body(request: Request) -> bytes:
        body = await request.body()
        if not verify_slack_signature(settings.slack_signing_secret, body,
                                      request.headers.get("X-Slack-Request-Timestamp"),
                                      request.headers.get("X-Slack-Signature")):
            raise HTTPException(status_code=401, detail="invalid Slack signature")
        return body

    @app.post("/slack/events")
    async def slack_events(request: Request, background: BackgroundTasks):
        body = await verified_body(request)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="body is not JSON")

        if payload.get("type") == "url_verification":
            return {"challenge": payload.get("challenge")}
        if payload.get("type") != "event_callback" or "event_id" not in payload:
            return {"ok": True, "result": "ignored"}

        # Slack wants a 2xx within 3s, so we store and ack now, classify in the background.
        # Deferring the (sync) DB write to the threadpool keeps the event loop free.
        result = await run_in_threadpool(pipeline.ingest, payload, request.headers.get("X-Slack-Retry-Num"))
        if result.outcome == "stored":
            background.add_task(pipeline.process, result.event_pk)
        log.info("slack event %s -> %s (%s)", payload.get("event_id"), result.outcome, result.reason)
        return {"ok": True, "result": result.outcome}

    @app.post("/slack/interactions")
    async def slack_interactions(request: Request):
        body = await verified_body(request)
        try:
            payload = json.loads(parse_qs(body.decode())["payload"][0])
            action = payload["actions"][0]
            approval_id = int(action["value"])
            user_id = payload["user"]["id"]
        except (KeyError, IndexError, ValueError, json.JSONDecodeError):
            raise HTTPException(status_code=400, detail="malformed interaction payload")
        if action.get("action_id") not in ("approve", "reject"):
            return {"ok": True, "result": "ignored"}
        result = await run_in_threadpool(pipeline.decide, approval_id, action["action_id"], user_id)
        return {"ok": True, "result": result}

    @app.get("/health")
    def health():
        checks: dict[str, str] = {}
        try:
            with session_factory() as s:
                s.execute(text("SELECT 1"))
                backlog = s.scalar(select(func.count()).select_from(SlackEvent)
                                   .where(SlackEvent.status.in_(("received", "retry_pending"))))
            checks["database"] = "ok"
        except Exception as exc:
            checks["database"] = f"error: {type(exc).__name__}"
            backlog = None
        missing = settings.missing_required()
        checks["config"] = "ok" if not missing else f"missing: {', '.join(missing)}"
        healthy = all(v == "ok" for v in checks.values())
        # No calls to Slack/Anthropic here: health checks run often and shouldn't burn rate limits.
        return JSONResponse(status_code=200 if healthy else 503, content={
            "status": "ok" if healthy else "degraded", "version": VERSION,
            "checks": checks, "backlog": backlog})

    @app.get("/approvals")
    def list_approvals(status: str = "pending", limit: int = 50):
        with session_factory() as s:
            rows = s.scalars(select(ApprovalRequest).where(ApprovalRequest.status == status)
                             .order_by(ApprovalRequest.id.desc()).limit(min(limit, 200))).all()
            return [{"id": r.id, "event_pk": r.event_pk, "label": r.label, "status": r.status,
                     "proposed_action": r.proposed_action, "created_at": r.created_at.isoformat()}
                    for r in rows]

    return app


def get_app() -> FastAPI:
    """Entry point: `uvicorn app.main:get_app --factory`."""
    return create_app()
