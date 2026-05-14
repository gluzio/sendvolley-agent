from __future__ import annotations

# Inbound Twilio WhatsApp webhook. Mirrors the §11.1 six-step sequence:
#
#   1. validate HMAC signature against settings.TWILIO_WEBHOOK_URL
#   2. idempotency check on MessageSid (Twilio retries → 200 OK no work)
#   3. persist inbound message to conversations
#   4. asyncio.create_task(_run_agent_turn(...))
#   5. _pending_tasks.add(task) + add_done_callback(_pending_tasks.discard)
#   6. return 200 OK with empty TwiML
#
# Media-only messages (NumMedia > 0) are intercepted here per §5 — the webhook
# itself sends the rejection reply via twilio_client and never enqueues the agent.

import asyncio
import logging

from fastapi import APIRouter, Request, Response
from twilio.request_validator import RequestValidator

from sendvolley_agent import db, twilio_client
from sendvolley_agent.agent import run_agent_turn as _run_agent_turn
from sendvolley_agent.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()

# RequestValidator is pure-Python, no I/O — safe at module-load time.
_validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)

# Keeps in-flight agent tasks alive. asyncio.create_task returns a Task that, if
# not referenced elsewhere, may be garbage-collected before completion. The set +
# add_done_callback pattern is the documented idiom for "fire and forget" tasks.
_pending_tasks: set[asyncio.Task[None]] = set()

_EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?>\n<Response></Response>'

# Never logged or persisted into webhook_failures.headers_redacted.
# WhatsApp webhooks don't carry cookies in practice — `cookie` is here on the
# principle that anything credential-shaped gets redacted by default.
_REDACTED_HEADERS = frozenset({"x-twilio-signature", "authorization", "cookie"})

_MEDIA_REJECTION_MESSAGE = (
    "I can only process text messages right now. Please type your request."
)


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _mask_from(from_number: str) -> str:
    """`whatsapp:+1415555****` — last four digits masked for log lines."""
    if len(from_number) < 4:
        return "[masked]"
    return from_number[:-4] + "****"


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in headers.items() if k.lower() not in _REDACTED_HEADERS}


def _twiml_ok() -> Response:
    return Response(content=_EMPTY_TWIML, media_type="application/xml", status_code=200)


@router.post("/whatsapp")
async def whatsapp_webhook(request: Request) -> Response:
    # ----- header presence check (before form parse) ----------------------
    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        source_ip = _client_host(request)
        db.record_webhook_failure(
            source_ip=source_ip,
            reason="missing_signature",
            headers_redacted=_redact_headers(dict(request.headers)),
        )
        logger.warning(
            "signature_missing",
            extra={"source_ip": source_ip},
        )
        return Response(status_code=403)

    # ----- read the form once; use the same dict for signature + fields ---
    form_data = await request.form()
    # str() is defensive — Twilio's x-www-form-urlencoded payloads are always
    # strings in practice, but starlette's FormData can in principle hold
    # UploadFile values. Keep them strings so the signature math is stable.
    form_dict = {k: str(v) for k, v in form_data.multi_items()}

    # ----- §11.1 step 1: HMAC validation ---------------------------------
    # IMPORTANT: validate against settings.TWILIO_WEBHOOK_URL — NOT
    # str(request.url). Behind Caddy, request.url is the internal
    # http://localhost:8000/whatsapp; Twilio signed the public
    # https://<client>.sendvolley.com/whatsapp. Mismatch → rejects all
    # legitimate traffic. If you find yourself "fixing" this to use
    # request.url: don't.
    is_valid = _validator.validate(
        settings.TWILIO_WEBHOOK_URL, form_dict, signature
    )
    if not is_valid:
        source_ip = _client_host(request)
        db.record_webhook_failure(
            source_ip=source_ip,
            reason="signature_mismatch",
            headers_redacted=_redact_headers(dict(request.headers)),
        )
        logger.warning(
            "signature_invalid",
            extra={
                "source_ip": source_ip,
                "message_sid": form_dict.get("MessageSid", ""),
            },
        )
        return Response(status_code=403)

    # ----- required-field extraction -------------------------------------
    # `Body` may legitimately be the empty string (media-only messages still
    # carry a Body key from Twilio), so we check for key presence rather
    # than truthiness on Body. From and MessageSid must be non-empty.
    from_number = form_dict.get("From", "")
    body = form_dict.get("Body")
    message_sid = form_dict.get("MessageSid", "")

    if not from_number or body is None or not message_sid:
        db.record_webhook_failure(
            source_ip=_client_host(request),
            reason="malformed_request",
            headers_redacted=_redact_headers(dict(request.headers)),
        )
        logger.warning(
            "malformed_request",
            extra={
                "has_from": bool(from_number),
                "has_body": body is not None,
                "has_message_sid": bool(message_sid),
            },
        )
        return Response(status_code=400)

    try:
        num_media = int(form_dict.get("NumMedia", "0") or "0")
    except ValueError:
        num_media = 0
    profile_name = form_dict.get("ProfileName", "")
    from_masked = _mask_from(from_number)

    logger.info(
        "webhook_received",
        extra={
            "message_sid": message_sid,
            "from_masked": from_masked,
            "num_media": num_media,
            "profile_name": profile_name,
        },
    )

    # ----- §11.1 step 2: idempotency -------------------------------------
    if db.inbound_message_exists(message_sid):
        logger.info(
            "duplicate_webhook",
            extra={"message_sid": message_sid, "from_masked": from_masked},
        )
        return _twiml_ok()

    # ----- media-only branch (§5 — webhook handles, agent never sees) ----
    if num_media > 0:
        synth_body = f"[Media: {num_media} attachment(s) — auto-rejected]"
        turn_id = db.record_inbound_message(
            client_id=settings.CLIENT_ID,
            body=synth_body,
            from_number=from_number,
            twilio_message_sid=message_sid,
        )
        logger.info(
            "media_only_message",
            extra={
                "message_sid": message_sid,
                "from_masked": from_masked,
                "num_media": num_media,
            },
        )
        outbound_sid = await twilio_client.send_text(
            from_number, _MEDIA_REJECTION_MESSAGE
        )
        db.record_outbound_message(
            client_id=settings.CLIENT_ID,
            body=_MEDIA_REJECTION_MESSAGE,
            twilio_message_sid=outbound_sid,
            turn_id=turn_id,
        )
        return _twiml_ok()

    # ----- §11.1 step 3: persist inbound ---------------------------------
    turn_id = db.record_inbound_message(
        client_id=settings.CLIENT_ID,
        body=body,
        from_number=from_number,
        twilio_message_sid=message_sid,
    )

    # ----- §11.1 steps 4–5: schedule + track -----------------------------
    task = asyncio.create_task(
        _run_agent_turn(settings.CLIENT_ID, turn_id, from_number, body)
    )
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)

    logger.info(
        "agent_turn_enqueued",
        extra={
            "message_sid": message_sid,
            "turn_id": turn_id,
            "from_masked": from_masked,
        },
    )

    # ----- §11.1 step 6 --------------------------------------------------
    return _twiml_ok()
