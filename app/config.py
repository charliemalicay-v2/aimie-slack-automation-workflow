"""Settings loaded from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in ("", None) else default


@dataclass(frozen=True)
class Settings:
    database_url: str
    slack_signing_secret: str
    slack_test_channel_id: str
    slack_approval_channel_id: str
    slack_bot_token: str
    slack_refresh_token: str | None = None
    slack_client_id: str | None = None
    slack_client_secret: str | None = None
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-haiku-4-5-20251001"
    http_max_attempts: int = 4          # per HTTP call, for 429/5xx/timeouts
    llm_validation_attempts: int = 2    # 1 normal try + 1 "repair" try for malformed output
    create_noise_approvals: bool = False
    auto_create_tables: bool = False

    def missing_required(self) -> list[str]:
        required = {
            "DATABASE_URL": self.database_url,
            "SLACK_SIGNING_SECRET": self.slack_signing_secret,
            "SLACK_TEST_CHANNEL_ID": self.slack_test_channel_id,
            "SLACK_BOT_TOKEN": self.slack_bot_token,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
        }
        return [k for k, v in required.items() if not v]


def load_settings() -> Settings:
    test_channel = _env("SLACK_TEST_CHANNEL_ID", "") or ""
    return Settings(
        database_url=_env("DATABASE_URL", "sqlite:///./local.db"),
        slack_signing_secret=_env("SLACK_SIGNING_SECRET", "") or "",
        slack_test_channel_id=test_channel,
        slack_approval_channel_id=_env("SLACK_APPROVAL_CHANNEL_ID", test_channel) or "",
        slack_bot_token=_env("SLACK_BOT_TOKEN", "") or "",
        slack_refresh_token=_env("SLACK_REFRESH_TOKEN"),
        slack_client_id=_env("SLACK_CLIENT_ID"),
        slack_client_secret=_env("SLACK_CLIENT_SECRET"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", "") or "",
        anthropic_model=_env("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
        http_max_attempts=int(_env("HTTP_MAX_ATTEMPTS", "4")),
        llm_validation_attempts=int(_env("LLM_VALIDATION_ATTEMPTS", "2")),
        create_noise_approvals=(_env("CREATE_NOISE_APPROVALS", "false") or "").lower() == "true",
        auto_create_tables=(_env("AUTO_CREATE_TABLES", "false") or "").lower() == "true",
    )
