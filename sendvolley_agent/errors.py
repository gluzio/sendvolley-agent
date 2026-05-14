from __future__ import annotations


class ConfigurationError(RuntimeError):
    """Raised at boot for any unrecoverable misconfiguration: failed Pydantic
    validators, filesystem/permission issues on DB_PATH, or required external
    fetches (e.g. Twilio IP allowlist) failing at startup. Always fatal —
    propagates out of FastAPI lifespan to exit the process."""


class AgentIterationLimitExceeded(RuntimeError):
    """Raised when the agent loop exceeds AGENT_MAX_ITERATIONS for a single turn."""


class ToolExecutionError(RuntimeError):
    """Raised when `execute_tool_safely`'s wrapper itself fails — i.e. a bug in the
    wrapper, not a failure inside the wrapped tool. Tool-internal failures are
    converted to `is_error: True` tool_result blocks instead (§11.2)."""


class WebhookAuthenticationError(RuntimeError):
    """Raised when an inbound Twilio webhook fails HMAC signature or source-IP
    verification (§4.4)."""


class TwilioSendError(RuntimeError):
    """Raised when a Twilio outbound send fails — either Twilio rejected the request
    (4xx with error code) or all transport retries were exhausted (5xx / network)."""
