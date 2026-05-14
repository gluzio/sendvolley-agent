from __future__ import annotations

# init_db() must run once at app startup (will be wired into the FastAPI lifespan
# from main.py) before any other function in this module is called. The lazy
# _connect() opens the SQLite file but assumes the schema already exists.

import json
import logging
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sendvolley_agent.config import settings

logger = logging.getLogger(__name__)

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"

_conn: sqlite3.Connection | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        conn = sqlite3.connect(
            settings.DB_PATH,
            check_same_thread=False,
            isolation_level=None,  # autocommit; short ops don't need explicit transactions
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _conn = conn
    return _conn


# ---------------------------------------------------------------------------
# Return types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Message:
    id: int
    role: str  # 'user' | 'assistant'
    content: str
    created_at: int
    turn_id: str | None


@dataclass(frozen=True, slots=True)
class Fact:
    id: int
    category: str
    fact: str
    created_at: int
    updated_at: int


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Apply schema.sql (idempotent) and ensure the single clients row exists."""
    conn = _connect()
    schema_sql = _SCHEMA_PATH.read_text()
    conn.executescript(schema_sql)
    conn.execute(
        "INSERT OR IGNORE INTO clients (id, name, created_at) VALUES (?, ?, ?)",
        (settings.CLIENT_ID, settings.CLIENT_NAME, _now_ms()),
    )


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def get_client_anthropic_key(client_id: str) -> str | None:
    """Return the per-client Anthropic API key, or None if not set.

    v1 always uses settings.ANTHROPIC_API_KEY (§4.3 ANTHROPIC_KEY_MODE='ours');
    this lookup exists so v2 ('client' mode) is a config flip, not a refactor."""
    row = _connect().execute(
        "SELECT anthropic_api_key FROM clients WHERE id = ?",
        (client_id,),
    ).fetchone()
    return row["anthropic_api_key"] if row else None


def inbound_message_exists(twilio_message_sid: str) -> bool:
    """Return True iff an inbound (`role='user'`) row with this MessageSid is
    already in `conversations`. Drives webhook idempotency per §3."""
    cursor = _connect().execute(
        """
        SELECT 1 FROM conversations
        WHERE twilio_message_sid = ? AND role = 'user'
        LIMIT 1
        """,
        (twilio_message_sid,),
    )
    return cursor.fetchone() is not None


def record_inbound_message(
    client_id: str,
    body: str,
    from_number: str,
    twilio_message_sid: str,
) -> str:
    """Persist an inbound WhatsApp message and return the freshly-minted turn_id."""
    turn_id = uuid.uuid4().hex
    _connect().execute(
        """
        INSERT INTO conversations
            (client_id, role, content, twilio_message_sid, from_number, turn_id, created_at)
        VALUES (?, 'user', ?, ?, ?, ?, ?)
        """,
        (client_id, body, twilio_message_sid, from_number, turn_id, _now_ms()),
    )
    return turn_id


def record_outbound_message(
    client_id: str,
    body: str,
    twilio_message_sid: str,
    turn_id: str,
) -> None:
    """Persist an outbound WhatsApp message under the same turn_id that started
    with the inbound message — this is what lets us reconstruct full turns."""
    _connect().execute(
        """
        INSERT INTO conversations
            (client_id, role, content, twilio_message_sid, turn_id, created_at)
        VALUES (?, 'assistant', ?, ?, ?, ?)
        """,
        (client_id, body, twilio_message_sid, turn_id, _now_ms()),
    )


def load_recent_messages(client_id: str, limit: int) -> list[Message]:
    """Return the last `limit` messages for `client_id` in chronological order.

    Counts individual messages, not turn pairs (§11.4). `id DESC` is the
    deterministic tiebreaker for rows inserted in the same millisecond."""
    cursor = _connect().execute(
        """
        SELECT id, role, content, created_at, turn_id
        FROM conversations
        WHERE client_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (client_id, limit),
    )
    rows = list(cursor.fetchall())
    rows.reverse()
    return [
        Message(
            id=r["id"], role=r["role"], content=r["content"],
            created_at=r["created_at"], turn_id=r["turn_id"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Memory facts (§11.6)
# ---------------------------------------------------------------------------

def load_memory_facts(client_id: str) -> list[Fact]:
    """Return only active (non-superseded) facts for the client."""
    cursor = _connect().execute(
        """
        SELECT id, category, fact, created_at, updated_at
        FROM memory_facts
        WHERE client_id = ? AND superseded_by IS NULL
        ORDER BY category, created_at
        """,
        (client_id,),
    )
    return [
        Fact(
            id=r["id"],
            category=r["category"],
            fact=r["fact"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in cursor.fetchall()
    ]


def save_memory_fact(client_id: str, category: str, fact: str) -> int:
    now = _now_ms()
    cursor = _connect().execute(
        """
        INSERT INTO memory_facts (client_id, category, fact, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (client_id, category, fact, now, now),
    )
    new_id = cursor.lastrowid
    if new_id is None:
        raise RuntimeError("INSERT did not return lastrowid")
    return new_id


def supersede_memory_fact(old_id: int, new_id: int) -> None:
    _connect().execute(
        """
        UPDATE memory_facts
        SET superseded_by = ?, updated_at = ?
        WHERE id = ?
        """,
        (new_id, _now_ms(), old_id),
    )


# ---------------------------------------------------------------------------
# Observability: llm_calls / tool_calls
# ---------------------------------------------------------------------------

def log_llm_call(
    client_id: str,
    turn_id: str,
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    stop_reason: str | None,
    latency_ms: int,
    error: str | None = None,
) -> None:
    _connect().execute(
        """
        INSERT INTO llm_calls
            (client_id, turn_id, model, input_tokens, output_tokens, stop_reason,
             latency_ms, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id, turn_id, model, input_tokens, output_tokens, stop_reason,
            latency_ms, error, _now_ms(),
        ),
    )


# Best-effort redaction so secrets don't land in the DB if Claude ever passes one
# as a tool argument or a tool echoes one back in an error. Not security-critical
# — defense-in-depth. Prefix-only: a generic token-shape heuristic produced false
# positives on legitimate long IDs (e.g. Instantly campaign IDs). If a new secret
# format appears, add its prefix here.
_SECRET_PREFIX_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]+"),
    re.compile(r"sv_live_[A-Za-z0-9_\-]+"),
]


def _redact_string(s: str) -> str:
    for pattern in _SECRET_PREFIX_PATTERNS:
        s = pattern.sub("[REDACTED:prefix]", s)
    return s


def _redact_secrets(value: Any) -> Any:
    """Walk a JSON-ish structure and mask anything that looks like an API key."""
    if isinstance(value, dict):
        return {k: _redact_secrets(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_secrets(v) for v in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def log_tool_call(
    client_id: str,
    turn_id: str,
    tool_name: str,
    tool_input: Any,
    tool_result: Any,
    is_error: bool,
    latency_ms: int,
) -> None:
    redacted_input = _redact_secrets(tool_input)
    redacted_result = _redact_secrets(tool_result)
    result_text = (
        redacted_result if isinstance(redacted_result, str)
        else json.dumps(redacted_result, default=str)
    )
    _connect().execute(
        """
        INSERT INTO tool_calls
            (client_id, turn_id, tool_name, tool_input, tool_result, is_error,
             latency_ms, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            client_id, turn_id, tool_name,
            json.dumps(redacted_input, default=str),
            result_text,
            1 if is_error else 0,
            latency_ms,
            _now_ms(),
        ),
    )


# ---------------------------------------------------------------------------
# Webhook failures / proposals
# ---------------------------------------------------------------------------

def record_webhook_failure(
    source_ip: str,
    reason: str,
    headers_redacted: dict | None,
) -> None:
    _connect().execute(
        """
        INSERT INTO webhook_failures (source_ip, reason, headers_redacted, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            source_ip,
            reason,
            json.dumps(headers_redacted) if headers_redacted is not None else None,
            _now_ms(),
        ),
    )


def record_proposal(
    client_id: str,
    kind: str,
    summary: str,
    body_markdown: str,
) -> int:
    cursor = _connect().execute(
        """
        INSERT INTO proposals (client_id, kind, summary, body_markdown, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (client_id, kind, summary, body_markdown, _now_ms()),
    )
    new_id = cursor.lastrowid
    if new_id is None:
        raise RuntimeError("INSERT did not return lastrowid")
    return new_id
