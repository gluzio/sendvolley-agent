from __future__ import annotations

# FastAPI entry point. `uvicorn sendvolley_agent.main:app` boots the service
# Twilio talks to.
#
# Single-worker model: SQLite is the source of truth, _pending_tasks lives in
# the process, and there's no shared-state layer. Multi-worker is a v2+ change.

import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from sendvolley_agent import agent, db, twilio_client, webhook
from sendvolley_agent.config import settings
from sendvolley_agent.tools import sendvolley as sendvolley_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging — keep journalctl readable while preserving the `extra={...}`
# payloads emitted throughout the codebase. Custom formatter appends any
# non-standard LogRecord fields as space-separated k=v pairs.
# ---------------------------------------------------------------------------

class _ExtraFormatter(logging.Formatter):
    _STANDARD_KEYS = frozenset(
        logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
    ) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = {
            k: v for k, v in record.__dict__.items()
            if k not in self._STANDARD_KEYS and not k.startswith("_")
        }
        if not extras:
            return base
        tail = " ".join(f"{k}={v}" for k, v in extras.items())
        return f"{base} | {tail}"


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(
        _ExtraFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(settings.LOG_LEVEL)
    # Replace existing handlers (uvicorn installs its own) so we own format.
    root.handlers[:] = [handler]


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()
    db.init_db()
    logger.info(
        "app_startup",
        extra={
            "client_id": settings.CLIENT_ID,
            "client_name": settings.CLIENT_NAME,
        },
    )
    try:
        yield
    finally:
        # Each close in its own try so one failure doesn't block the others.
        for name, closer in (
            ("agent", agent.close),
            ("twilio_client", twilio_client.close),
            ("sendvolley_tool", sendvolley_tool.close),
        ):
            try:
                await closer()
            except Exception:
                logger.exception("shutdown_close_failed", extra={"module": name})
        logger.info("app_shutdown")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(lifespan=lifespan)
app.include_router(webhook.router)


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "client_id": settings.CLIENT_ID,
        "ts": int(time.time()),
    }


# ---------------------------------------------------------------------------
# Local dev entry point. The systemd unit on the VPS calls uvicorn directly:
#   uvicorn sendvolley_agent.main:app --host 127.0.0.1 --port 8000 --workers 1
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    # Single worker in v1: SQLite is the source of truth, _pending_tasks set
    # is per-process, and there's no Redis/shared-state layer. Multi-worker
    # is v2+.
    uvicorn.run(
        "sendvolley_agent.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.LOG_LEVEL.lower(),
    )
