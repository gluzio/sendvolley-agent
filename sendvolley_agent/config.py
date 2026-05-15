from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sendvolley_agent.errors import ConfigurationError

_TWILIO_SID_RE = re.compile(r"^AC[a-f0-9]{32}$", re.IGNORECASE)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="forbid",
        case_sensitive=True,
    )

    # --- Identity -----------------------------------------------------------
    CLIENT_ID: str = Field(pattern=r"^[a-z0-9-]+$")
    CLIENT_NAME: str

    # --- Anthropic ----------------------------------------------------------
    ANTHROPIC_API_KEY: str = Field(repr=False)
    ANTHROPIC_KEY_MODE: Literal["ours", "client"] = "ours"
    ANTHROPIC_MODEL: str = "claude-sonnet-4-6"

    # --- SendVolley MCP Worker ---------------------------------------------
    SENDVOLLEY_WORKER_URL: str
    SENDVOLLEY_WORKER_TOKEN: str = Field(repr=False)

    # --- Twilio -------------------------------------------------------------
    TWILIO_ACCOUNT_SID: str
    TWILIO_AUTH_TOKEN: str = Field(repr=False)
    TWILIO_WHATSAPP_NUMBER: str
    TWILIO_WEBHOOK_URL: str

    # --- Other vendor APIs --------------------------------------------------
    APOLLO_API_KEY: str = Field(repr=False)
    INSTANTLY_API_KEY: str = Field(repr=False)

    # --- Agent loop tunables ------------------------------------------------
    AGENT_MAX_ITERATIONS: int = Field(default=30, ge=1, le=100)
    N_HISTORY_TURNS: int = Field(default=20, ge=1, le=100)

    # --- Infra --------------------------------------------------------------
    DB_PATH: Path = Path("/var/lib/sendvolley/state.db")
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # --- Validators ---------------------------------------------------------
    @field_validator("SENDVOLLEY_WORKER_URL", "TWILIO_WEBHOOK_URL")
    @classmethod
    def _validate_https_url(cls, v: str) -> str:
        parsed = urlparse(v)
        if parsed.scheme != "https":
            raise ValueError("must use the https:// scheme")
        if not parsed.netloc:
            raise ValueError("must include a host (e.g. 'https://example.com/...')")
        return v.rstrip("/")

    @field_validator("TWILIO_WHATSAPP_NUMBER")
    @classmethod
    def _twilio_whatsapp_prefix(cls, v: str) -> str:
        if not v.startswith("whatsapp:+"):
            raise ValueError("must start with 'whatsapp:+' (e.g. 'whatsapp:+14155551234')")
        return v

    @field_validator("ANTHROPIC_API_KEY")
    @classmethod
    def _validate_anthropic_key(cls, v: str) -> str:
        if not v.startswith("sk-ant-"):
            raise ValueError("must start with 'sk-ant-'")
        return v

    @field_validator("SENDVOLLEY_WORKER_TOKEN")
    @classmethod
    def _validate_worker_token(cls, v: str) -> str:
        if not v.startswith("sv_live_"):
            raise ValueError("must start with 'sv_live_'")
        return v

    @field_validator("TWILIO_ACCOUNT_SID")
    @classmethod
    def _validate_twilio_sid(cls, v: str) -> str:
        if not _TWILIO_SID_RE.match(v):
            raise ValueError("must match Twilio account SID format '^AC[a-f0-9]{32}$' (case-insensitive)")
        return v.upper()

    @model_validator(mode="after")
    def _check_db_path_writeable(self) -> Settings:
        parent = self.DB_PATH.parent
        if not parent.exists():
            raise ConfigurationError(
                f"DB_PATH parent directory does not exist: {parent}. "
                f"The install script must create it and chown it to the service user."
            )
        if not parent.is_dir():
            raise ConfigurationError(
                f"DB_PATH parent exists but is not a directory: {parent}."
            )
        if not os.access(parent, os.W_OK):
            raise ConfigurationError(
                f"DB_PATH parent directory is not writeable by the current user: {parent}. "
                f"Check ownership and permissions."
            )
        return self


settings = Settings()
