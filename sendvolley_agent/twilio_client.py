from __future__ import annotations

# Outbound Twilio WhatsApp sender. Two public sends (`send_text`, `send_template`)
# plus `close()` for the FastAPI lifespan. All requests share a module-level
# httpx.AsyncClient (lazy-init, mirroring db._connect). Retry policy per §11.3:
# 3 attempts on 5xx / connection errors with 0.5s/1s/2s backoff; no retry on 4xx.

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from sendvolley_agent.config import settings
from sendvolley_agent.errors import TwilioSendError

logger = logging.getLogger(__name__)

# `whatsapp:+<digits>` — same shape config.py enforces on TWILIO_WHATSAPP_NUMBER.
_WHATSAPP_TO_RE = re.compile(r"^whatsapp:\+\d+$")

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [0.5, 1.0, 2.0]  # gap AFTER attempt N before attempt N+1

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=10.0,
            transport=httpx.AsyncHTTPTransport(retries=3),
        )
    return _client


async def close() -> None:
    """Close the module's httpx client. Called from FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _validate_to(to: str) -> None:
    if not _WHATSAPP_TO_RE.match(to):
        raise ValueError(
            f"'to' must match 'whatsapp:+<digits>' (got {to!r})"
        )


def _messages_url() -> str:
    return (
        f"https://api.twilio.com/2010-04-01/"
        f"Accounts/{settings.TWILIO_ACCOUNT_SID}/Messages.json"
    )


def _log_attempt(
    *, to: str, kind: str, attempt: int, outcome: str, latency_ms: int
) -> None:
    logger.info(
        "twilio_send_attempt",
        extra={
            "tool": "twilio_send",
            "to": to,
            "kind": kind,
            "attempt": attempt,
            "outcome": outcome,
            "latency_ms": latency_ms,
        },
    )


async def _send_with_5xx_retry(
    method: str, url: str, *, kind: str, to: str, **kwargs: Any
) -> httpx.Response:
    """POST/GET wrapper that retries on 5xx and network errors per §11.3.

    Returns the final httpx.Response (which may be a 2xx OR a 4xx — caller decides
    how to handle 4xx). Raises TwilioSendError if all attempts fail with 5xx or
    network errors."""
    client = _get_client()
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            resp = await client.request(method, url, **kwargs)
        except httpx.RequestError as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log_attempt(
                to=to, kind=kind, attempt=attempt,
                outcome=f"transport_error:{type(e).__name__}",
                latency_ms=latency_ms,
            )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt - 1])
                continue
            raise TwilioSendError(
                f"retries_exhausted: {type(e).__name__}: {e}"
            ) from e

        latency_ms = int((time.monotonic() - t0) * 1000)

        if 200 <= resp.status_code < 300:
            _log_attempt(
                to=to, kind=kind, attempt=attempt,
                outcome=f"http_{resp.status_code}",
                latency_ms=latency_ms,
            )
            return resp

        if 400 <= resp.status_code < 500:
            _log_attempt(
                to=to, kind=kind, attempt=attempt,
                outcome=f"http_{resp.status_code}",
                latency_ms=latency_ms,
            )
            return resp  # caller surfaces the 4xx as a TwilioSendError with parsed details

        # 5xx
        _log_attempt(
            to=to, kind=kind, attempt=attempt,
            outcome=f"http_{resp.status_code}",
            latency_ms=latency_ms,
        )
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_SECONDS[attempt - 1])
            continue
        raise TwilioSendError(
            f"retries_exhausted: HTTP {resp.status_code} after {_MAX_ATTEMPTS} attempts"
        )

    # Unreachable — the loop either returns or raises on every path.
    raise TwilioSendError("retries_exhausted: unknown")


def _parse_2xx_or_raise(resp: httpx.Response) -> str:
    """For a response that's already either 2xx or 4xx (5xx exhausted above),
    return the sid on success or raise TwilioSendError on Twilio rejection."""
    try:
        body = resp.json()
    except ValueError:
        body = {}

    if 200 <= resp.status_code < 300:
        sid = body.get("sid")
        if not sid:
            raise TwilioSendError(
                f"Twilio 2xx response missing 'sid' (status={resp.status_code})"
            )
        return sid

    # 4xx — Twilio's error JSON shape: {"code": 21211, "message": "...", ...}
    raise TwilioSendError(
        f"Twilio rejected request: status={resp.status_code} "
        f"code={body.get('code')} message={body.get('message')!r}"
    )


async def send_text(to: str, body: str) -> str:
    """Send a regular (session) WhatsApp message. Returns the Twilio MessageSid."""
    _validate_to(to)
    resp = await _send_with_5xx_retry(
        "POST",
        _messages_url(),
        kind="text",
        to=to,
        data={
            "From": settings.TWILIO_WHATSAPP_NUMBER,
            "To": to,
            "Body": body,
        },
        auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
    )
    return _parse_2xx_or_raise(resp)


async def send_template(to: str, template_sid: str, variables: dict[str, str]) -> str:
    """Send a pre-approved template message (proactive sends outside the 24h
    session window). Returns the Twilio MessageSid."""
    _validate_to(to)
    resp = await _send_with_5xx_retry(
        "POST",
        _messages_url(),
        kind="template",
        to=to,
        data={
            "From": settings.TWILIO_WHATSAPP_NUMBER,
            "To": to,
            "ContentSid": template_sid,
            "ContentVariables": json.dumps(variables),
        },
        auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
    )
    return _parse_2xx_or_raise(resp)
