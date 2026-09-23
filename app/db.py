"""Engine/session setup. Works with Supabase Postgres in prod and SQLite in tests."""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


def make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        # StaticPool keeps one in-memory DB shared across threads (tests + BackgroundTasks).
        return create_engine(url, connect_args={"check_same_thread": False}, poolclass=StaticPool)
    connect_args = {}
    if url.startswith("postgresql+psycopg"):
        # Supabase's transaction pooler (port 6543) does not support prepared statements.
        connect_args["prepare_threshold"] = None
    return create_engine(url, pool_pre_ping=True, pool_size=5, max_overflow=5, connect_args=connect_args)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
