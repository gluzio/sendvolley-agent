from __future__ import annotations

# `generate_copy` — the agent's first real tool. POSTs to the SendVolley Worker's
# /v1/generate-copy endpoint (which hosts the cold-email IP) and formats the
# returned variants for Claude to relay over WhatsApp.
#
# Retry/error policy per §11.3:
#   - 3 attempts max, exponential 0.5s / 1s / 2s backoff
#   - Retry on 5xx (except 502 — see below) and on network errors
#   - 502 has special semantics here: the Worker uses it to surface its OWN
#     upstream Anthropic failure. Retrying usually won't help, so we hand 502
#     directly to Claude with a "Retry in a moment" hint — Claude decides
#     whether to invoke the tool again (which gets its own fresh 3-attempt
#     budget per §11.3 "no retry compounding").
#
# The retry helper here mirrors the pattern in twilio_client.py. Not shared
# yet — we'll consolidate into a utils module when a third outbound module
# needs it.

import asyncio
import logging
import time
from typing import Any

import httpx

from sendvolley_agent.config import settings

logger = logging.getLogger(__name__)

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = [0.5, 1.0, 2.0]
_MIN_BRIEF_LEN = 20
_MIN_ICP_LEN = 20
_TIMEOUT_SECONDS = 30.0  # generate_copy can take 5-15s

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=_TIMEOUT_SECONDS,
            transport=httpx.AsyncHTTPTransport(retries=3),
        )
    return _client


async def close() -> None:
    """Close the module's httpx client. Called from FastAPI lifespan shutdown."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


def _endpoint_url() -> str:
    return f"{settings.SENDVOLLEY_WORKER_URL}/v1/generate-copy"


def _log_attempt(*, client_id: str, attempt: int, outcome: str, latency_ms: int) -> None:
    logger.info(
        "sendvolley_attempt",
        extra={
            "tool": "sendvolley_worker",
            "client_id": client_id,
            "attempt": attempt,
            "outcome": outcome,
            "latency_ms": latency_ms,
        },
    )


async def _post_with_retry(
    *, url: str, payload: dict, headers: dict, client_id: str
) -> httpx.Response:
    """3 attempts, 0.5s/1s/2s backoff, retry on 5xx (except 502) and network
    errors. Returns the Response for caller-side handling of 2xx / 4xx / 502.
    Raises RuntimeError on exhausted retries."""
    client = _get_client()
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        t0 = time.monotonic()
        try:
            response = await client.post(url, json=payload, headers=headers)
        except httpx.RequestError as e:
            latency_ms = int((time.monotonic() - t0) * 1000)
            _log_attempt(
                client_id=client_id, attempt=attempt,
                outcome=f"transport_error:{type(e).__name__}",
                latency_ms=latency_ms,
            )
            if attempt < _MAX_ATTEMPTS:
                await asyncio.sleep(_BACKOFF_SECONDS[attempt - 1])
                continue
            raise RuntimeError(
                f"SendVolley Worker timed out after {_MAX_ATTEMPTS} attempts. "
                f"Check connectivity."
            ) from e

        latency_ms = int((time.monotonic() - t0) * 1000)
        _log_attempt(
            client_id=client_id, attempt=attempt,
            outcome=f"http_{response.status_code}",
            latency_ms=latency_ms,
        )

        # 2xx / 4xx / 502 → return to caller for parsing.
        # 502 is a 5xx but the Worker uses it specifically for upstream
        # Anthropic failures — retrying at this layer rarely helps, so we
        # let Claude decide.
        if (
            200 <= response.status_code < 300
            or 400 <= response.status_code < 500
            or response.status_code == 502
        ):
            return response

        # Other 5xx → retry
        if attempt < _MAX_ATTEMPTS:
            await asyncio.sleep(_BACKOFF_SECONDS[attempt - 1])
            continue
        raise RuntimeError(
            f"SendVolley Worker is unreachable after {_MAX_ATTEMPTS} attempts. "
            f"Try again in a moment."
        )

    # Unreachable
    raise RuntimeError("SendVolley Worker retry loop ended unexpectedly")


def _format_variants(body: dict) -> str:
    """Turn the Worker's JSON into WhatsApp-readable text. The trailing
    [tokens: ...] line lets Claude relay cost info if asked, and stays in the
    tool output regardless of whether Claude includes it in the reply."""
    variants = body.get("variants", [])
    n = len(variants)
    plural = "" if n == 1 else "s"
    lines: list[str] = [f"Generated {n} variant{plural} for the campaign:", ""]
    for i, variant in enumerate(variants, start=1):
        angle = variant.get("angle") or ""
        if angle:
            lines.append(f"━━━ Variant {i} ({angle}) ━━━")
        else:
            lines.append(f"━━━ Variant {i} ━━━")
        lines.append(f"Subject: {variant.get('subject', '')}")
        lines.append("Body:")
        lines.append(variant.get("body", ""))
        lines.append("")
    in_tok = body.get("input_tokens", 0)
    out_tok = body.get("output_tokens", 0)
    lines.append(f"[tokens: {in_tok} in / {out_tok} out]")
    return "\n".join(lines)


def _safe_json(response: httpx.Response) -> dict:
    try:
        data = response.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


async def generate_copy(
    client_id: str,
    brief: str,
    icp: str,
    num_variants: int = 3,
) -> str:
    """Generate cold-email variants for a campaign via the SendVolley copy engine.

    Args:
        brief: The campaign brief — the offer, who's sending, what's the goal.
            Be specific; this is what shapes the copy. Min 20 chars.
        icp: Description of the target persona — role, seniority, company shape,
            pain points, what they care about. Min 20 chars.
        num_variants: How many distinct variants to generate (1-10, default 3).
    """
    if len(brief) < _MIN_BRIEF_LEN or len(icp) < _MIN_ICP_LEN:
        raise ValueError(
            f"brief and icp must each be at least {_MIN_BRIEF_LEN} characters "
            f"(got brief={len(brief)}, icp={len(icp)})"
        )

    logger.info(
        "sendvolley_request",
        extra={"client_id": client_id, "num_variants": num_variants},
    )

    response = await _post_with_retry(
        url=_endpoint_url(),
        payload={
            "campaign_brief": brief,
            "icp_description": icp,
            "n_variants": num_variants,
        },
        headers={
            "Authorization": f"Bearer {settings.SENDVOLLEY_WORKER_TOKEN}",
            "Content-Type": "application/json",
        },
        client_id=client_id,
    )

    if 200 <= response.status_code < 300:
        body = _safe_json(response)
        logger.info(
            "sendvolley_response",
            extra={
                "client_id": client_id,
                "num_variants": len(body.get("variants", [])),
                "input_tokens": body.get("input_tokens"),
                "output_tokens": body.get("output_tokens"),
            },
        )
        return _format_variants(body)

    # Non-2xx — log + map to typed exception. The agent's execute_tool_safely
    # converts the raised exception into an is_error tool_result block for Claude.
    body = _safe_json(response)
    logger.warning(
        "sendvolley_error",
        extra={
            "client_id": client_id,
            "status": response.status_code,
            "error": body.get("error"),
        },
    )

    if response.status_code == 401:
        raise ValueError(
            "SendVolley Worker authentication failed (HTTP 401). "
            "Check SENDVOLLEY_WORKER_TOKEN."
        )
    if response.status_code == 502:
        details = body.get("details") or "no details"
        raise RuntimeError(
            f"SendVolley Worker had an upstream failure: {details}. "
            f"Retry in a moment."
        )

    # Other 4xx — typically the zod validation shape with details.fieldErrors
    details = body.get("details")
    field_errors = (
        details.get("fieldErrors") if isinstance(details, dict) else None
    )
    raise ValueError(
        f"SendVolley Worker rejected the request: "
        f"{body.get('error', 'unknown error')}. "
        f"Details: {field_errors}. Adjust the brief or ICP and try again."
    )
