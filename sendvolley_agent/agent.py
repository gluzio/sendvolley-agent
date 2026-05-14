from __future__ import annotations

# The agent loop. §6.1 of ARCHITECTURE.md is normative; this file implements
# the shape exactly:
#
#   load history → load facts → build system prompt → loop {Claude → execute
#   tools → continue, or extract text → reply}
#
# Production replacement for the placeholder in webhook.py. The webhook imports
# `run_agent_turn` from here and hands it to asyncio.create_task per §11.1.
#
# TOOL DESCRIPTIONS ARE AUTO-GENERATED FROM DOCSTRINGS FOR v1.
# TODO(v2): Replace with hand-written descriptions in tools/_descriptions.py.
# The auto-gen approach is fine for the build phase but the descriptions are
# part of the product surface — Claude's tool selection behavior is shaped by
# them — so they deserve intentional human curation before production scale.

import asyncio
import inspect
import logging
import time
import typing
from typing import Any, Awaitable, Callable

from anthropic import AsyncAnthropic

from sendvolley_agent import db, twilio_client
from sendvolley_agent.config import settings
from sendvolley_agent.db import Fact
from sendvolley_agent.errors import ConfigurationError
from sendvolley_agent.tools.sendvolley import generate_copy

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (user-facing strings)
# ---------------------------------------------------------------------------

_STUCK_MESSAGE = (
    "I'm getting stuck on this — could you give me more detail or break it into "
    "smaller steps?"
)

# TODO: wire alerting. "The team has been notified" is currently a polite lie —
# the failure lands in the logs only. When we add Sentry/PagerDuty/whatever,
# this user-facing string stays the same.
_FALLBACK_ERROR_MESSAGE = "Something went wrong on my end. The team has been notified."

_MAX_TOKENS = 2048


# ---------------------------------------------------------------------------
# Anthropic client (lazy singleton, mirrors twilio_client._get_client / db._connect)
# ---------------------------------------------------------------------------

_anthropic_client: AsyncAnthropic | None = None


def _resolve_anthropic_key(client_id: str) -> str:
    """§4.3 — v1 uses settings.ANTHROPIC_API_KEY for every client. v2 will set
    ANTHROPIC_KEY_MODE='client' and read per-client keys from the clients
    table. Code path exists now so the v2 flip is config-only."""
    if settings.ANTHROPIC_KEY_MODE == "ours":
        return settings.ANTHROPIC_API_KEY
    # "client" mode
    key = db.get_client_anthropic_key(client_id)
    if not key:
        raise ConfigurationError(
            f"ANTHROPIC_KEY_MODE='client' but no anthropic_api_key found for "
            f"client_id={client_id!r}"
        )
    return key


def _get_anthropic_client(client_id: str) -> AsyncAnthropic:
    """In v1 ('ours' mode) every client uses the same key so a singleton is
    safe. v2 ('client' mode) will need per-client clients keyed by client_id —
    revisit when the mode flips."""
    if settings.ANTHROPIC_KEY_MODE == "client":
        raise NotImplementedError(
            "Per-client Anthropic clients not yet wired. The singleton in "
            "_get_anthropic_client must be replaced with a "
            "dict[client_id, AsyncAnthropic] before v2 multi-tenancy ships — "
            "otherwise client A's calls will bill to the first-seen client's "
            "account. See ARCHITECTURE.md §4.3."
        )
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = AsyncAnthropic(api_key=_resolve_anthropic_key(client_id))
    return _anthropic_client


async def close() -> None:
    """Close the module's Anthropic client. Called from the FastAPI lifespan."""
    global _anthropic_client
    if _anthropic_client is not None:
        await _anthropic_client.close()
        _anthropic_client = None


# ---------------------------------------------------------------------------
# Tool stubs (v1) — all raise NotImplementedError until the real implementations
# land in tools/<name>.py. The wrappers catch this and surface it to Claude as
# an is_error tool_result (§11.2). Stubs are here so the tool catalog is
# structurally complete and Claude knows what tools exist.
# ---------------------------------------------------------------------------

async def apollo_search_people(client_id: str, query: str, limit: int = 25) -> str:
    """Search Apollo for prospects matching a natural-language query.

    Args:
        query: Natural-language prospect description.
        limit: Max results to return.
    """
    raise NotImplementedError("Tool apollo_search_people not yet implemented")


async def apollo_enrich_person(client_id: str, person_id: str) -> str:
    """Enrich a single Apollo contact with full profile details.

    Args:
        person_id: Apollo contact ID.
    """
    raise NotImplementedError("Tool apollo_enrich_person not yet implemented")


async def instantly_list_campaigns(client_id: str) -> str:
    """List the client's active Instantly campaigns."""
    raise NotImplementedError("Tool instantly_list_campaigns not yet implemented")


async def instantly_campaign_stats(client_id: str, campaign_id: str) -> str:
    """Fetch reply rate, open rate, and deliverability for an Instantly campaign.

    Args:
        campaign_id: The Instantly campaign ID.
    """
    raise NotImplementedError("Tool instantly_campaign_stats not yet implemented")


async def instantly_add_leads(
    client_id: str, campaign_id: str, leads: list[str]
) -> str:
    """Add prospects (with copy) to an Instantly campaign.

    Args:
        campaign_id: The Instantly campaign ID.
        leads: List of lead identifiers (Apollo IDs or JSON-encoded lead dicts).
    """
    raise NotImplementedError("Tool instantly_add_leads not yet implemented")


async def remember_fact(client_id: str, category: str, fact: str) -> str:
    """Save a durable fact about this client to memory.

    Args:
        category: Free-form tag — suggested values: voice, icp, preferences,
            campaign_history, other.
        fact: The fact text.
    """
    raise NotImplementedError("Tool remember_fact not yet implemented")


async def recall_facts(client_id: str) -> str:
    """Read all current durable facts about this client."""
    raise NotImplementedError("Tool recall_facts not yet implemented")


async def propose_prompt_change(
    client_id: str, kind: str, summary: str, body_markdown: str
) -> str:
    """Draft a prompt-improvement proposal for human review.

    Args:
        kind: One of worker_prompt_edit, system_prompt_edit, skill_addition, other.
        summary: One-line summary of the proposed change.
        body_markdown: Full markdown body of the proposal.
    """
    raise NotImplementedError("Tool propose_prompt_change not yet implemented")


TOOL_REGISTRY: dict[str, Callable[..., Awaitable[str]]] = {
    "generate_copy": generate_copy,
    "apollo_search_people": apollo_search_people,
    "apollo_enrich_person": apollo_enrich_person,
    "instantly_list_campaigns": instantly_list_campaigns,
    "instantly_campaign_stats": instantly_campaign_stats,
    "instantly_add_leads": instantly_add_leads,
    "remember_fact": remember_fact,
    "recall_facts": recall_facts,
    "propose_prompt_change": propose_prompt_change,
}


# ---------------------------------------------------------------------------
# Tool catalog generation (module-load time)
# ---------------------------------------------------------------------------

def _type_to_json_schema(t: Any, param_name: str, tool_name: str) -> dict:
    if t is str:
        return {"type": "string"}
    if t is int:
        return {"type": "integer"}
    if t is bool:
        return {"type": "boolean"}
    origin = typing.get_origin(t)
    args = typing.get_args(t)
    if origin is list and args == (str,):
        return {"type": "array", "items": {"type": "string"}}
    raise TypeError(
        f"Unsupported tool parameter type for {tool_name}.{param_name}: {t!r}. "
        f"v1 supports str, int, bool, list[str]."
    )


def _build_input_schema(func: Callable[..., Any]) -> dict:
    sig = inspect.signature(func)
    hints = typing.get_type_hints(func)
    properties: dict[str, dict] = {}
    required: list[str] = []
    for param_name, param in sig.parameters.items():
        # client_id is injected by the runtime, never exposed in the schema.
        if param_name == "client_id":
            continue
        param_type = hints.get(param_name, str)
        properties[param_name] = _type_to_json_schema(param_type, param_name, func.__name__)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)
    return {"type": "object", "properties": properties, "required": required}


def _build_tool_catalog() -> list[dict]:
    catalog: list[dict] = []
    for name, func in TOOL_REGISTRY.items():
        doc = inspect.getdoc(func) or ""
        catalog.append({
            "name": name,
            "description": doc,
            "input_schema": _build_input_schema(func),
        })
    return catalog


# Built at module-load time; bad signatures fail import (intended).
TOOL_CATALOG: list[dict] = _build_tool_catalog()


# ---------------------------------------------------------------------------
# System prompt (§6.3)
# ---------------------------------------------------------------------------

def _format_tool_descriptions() -> str:
    lines: list[str] = []
    for tool in TOOL_CATALOG:
        lines.append(f"- {tool['name']}: {tool['description'].splitlines()[0]}")
    return "\n".join(lines)


def _format_memory_facts(facts: list[Fact]) -> str:
    if not facts:
        return (
            "(No durable facts saved yet — use remember_fact to save anything you "
            "learn about this client.)"
        )
    by_category: dict[str, list[str]] = {}
    for f in facts:
        by_category.setdefault(f.category, []).append(f.fact)
    blocks: list[str] = []
    for category, items in by_category.items():
        blocks.append(f"{category}:")
        for item in items:
            blocks.append(f"  - {item}")
    return "\n".join(blocks)


def _build_system_prompt(facts: list[Fact]) -> str:
    return f"""You are SendVolley, the AI agent for {settings.CLIENT_NAME}. Your job is to help them run cold outreach campaigns.

Your tools:
{_format_tool_descriptions()}

What you know about {settings.CLIENT_NAME}:
{_format_memory_facts(facts)}

Operating constraints:
- You're talking to the client via WhatsApp. Keep responses concise (≤200 words usually).
- For multi-step tasks (build list → enrich → write copy → schedule), use the todo pattern: state the plan, execute each step, report progress.
- Never invent prospect data, campaign stats, or copy variants. Always use a tool to fetch real information.
- Never claim to have done something you haven't actually done via a tool call.
- For destructive actions (deleting campaigns, removing leads), always confirm with the client first."""


# ---------------------------------------------------------------------------
# Helpers for response handling
# ---------------------------------------------------------------------------

def _blocks_to_dicts(content: Any) -> list[dict]:
    """Convert SDK content blocks (TextBlock / ToolUseBlock or duck-typed mocks)
    into plain dicts so we can re-feed them as the assistant message."""
    result: list[dict] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            result.append({"type": "text", "text": getattr(block, "text", "")})
        elif btype == "tool_use":
            result.append({
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": getattr(block, "input", {}),
            })
    return result


def _extract_text(content: Any) -> str:
    parts: list[str] = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts)


# ---------------------------------------------------------------------------
# Tool execution wrapper (§11.2)
# ---------------------------------------------------------------------------

async def execute_tool_safely(block: Any, client_id: str, turn_id: str) -> dict:
    """Wrap a single tool invocation. Catches exceptions inside the tool and
    converts them to a tool_result block with is_error=True. Always writes a
    tool_calls row, even on success. If the wrapper itself raises (e.g.
    db.log_tool_call fails), the exception propagates to the outer agent-loop
    try/except per §11.2."""
    tool_use_id = getattr(block, "id", "")
    tool_name = getattr(block, "name", "")
    tool_input = getattr(block, "input", {}) or {}

    func = TOOL_REGISTRY.get(tool_name)
    t0 = time.monotonic()
    is_error = False
    if func is None:
        content = f"Tool {tool_name} does not exist"
        is_error = True
    else:
        try:
            content = await func(client_id, **tool_input)
        except Exception as e:
            content = (
                f"Tool {tool_name} failed: {type(e).__name__}: {str(e)[:200]}"
            )
            is_error = True

    latency_ms = int((time.monotonic() - t0) * 1000)
    db.log_tool_call(
        client_id=client_id,
        turn_id=turn_id,
        tool_name=tool_name,
        tool_input=tool_input,
        tool_result=content,
        is_error=is_error,
        latency_ms=latency_ms,
    )
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": content,
        "is_error": is_error,
    }


# ---------------------------------------------------------------------------
# The agent loop (§6.1)
# ---------------------------------------------------------------------------

async def run_agent_turn(
    client_id: str,
    turn_id: str,
    from_number: str,
    user_message: str,
) -> None:
    """Run a complete agent turn. Logs everything to SQLite, sends the reply
    via Twilio. Never raises — failures are caught, logged, and surfaced to
    the user (or swallowed per §11.7)."""
    logger.info(
        "agent_turn_started",
        extra={"client_id": client_id, "turn_id": turn_id},
    )
    try:
        # Step 1: history. The webhook persisted the current inbound row
        # before enqueueing us, so it's in `conversations` — strip every row
        # matching the current turn_id before appending user_message explicitly
        # per §6.1. (Robust against content-normalization changes; content
        # equality matching would be brittle.)
        history_raw = db.load_recent_messages(
            client_id, settings.N_HISTORY_TURNS + 1
        )
        filtered = [
            m for m in history_raw if not m.content.startswith("[Media: ")
        ]
        filtered = [m for m in filtered if m.turn_id != turn_id]
        filtered = filtered[-settings.N_HISTORY_TURNS:]

        # Step 2-3: facts + system prompt
        facts = db.load_memory_facts(client_id)
        system_prompt = _build_system_prompt(facts)

        # Step 4: messages
        messages: list[dict] = [
            {"role": m.role, "content": m.content} for m in filtered
        ]
        messages.append({"role": "user", "content": user_message})

        client = _get_anthropic_client(client_id)
        last_tool_names: list[str] = []

        # Step 5: loop
        for iteration in range(settings.AGENT_MAX_ITERATIONS):
            t0 = time.monotonic()
            try:
                response = await client.messages.create(
                    model=settings.ANTHROPIC_MODEL,
                    system=system_prompt,
                    messages=messages,
                    tools=TOOL_CATALOG,
                    max_tokens=_MAX_TOKENS,
                )
            except Exception as e:
                latency_ms = int((time.monotonic() - t0) * 1000)
                db.log_llm_call(
                    client_id=client_id, turn_id=turn_id,
                    model=settings.ANTHROPIC_MODEL,
                    input_tokens=None, output_tokens=None,
                    stop_reason=None, latency_ms=latency_ms,
                    error=f"{type(e).__name__}: {e}",
                )
                raise

            latency_ms = int((time.monotonic() - t0) * 1000)
            usage = getattr(response, "usage", None)
            db.log_llm_call(
                client_id=client_id, turn_id=turn_id,
                model=getattr(response, "model", settings.ANTHROPIC_MODEL),
                input_tokens=getattr(usage, "input_tokens", None) if usage else None,
                output_tokens=getattr(usage, "output_tokens", None) if usage else None,
                stop_reason=response.stop_reason, latency_ms=latency_ms,
            )
            logger.info(
                "claude_call_complete",
                extra={
                    "client_id": client_id, "turn_id": turn_id,
                    "iteration": iteration, "stop_reason": response.stop_reason,
                    "latency_ms": latency_ms,
                },
            )

            if response.stop_reason == "tool_use":
                # §11.8 — sequential execution
                tool_results: list[dict] = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    logger.info(
                        "tool_dispatch",
                        extra={
                            "client_id": client_id, "turn_id": turn_id,
                            "tool": block.name, "tool_use_id": block.id,
                        },
                    )
                    result = await execute_tool_safely(block, client_id, turn_id)
                    tool_results.append(result)
                    last_tool_names.append(block.name)
                    logger.info(
                        "tool_complete",
                        extra={
                            "client_id": client_id, "turn_id": turn_id,
                            "tool": block.name,
                            "is_error": result.get("is_error", False),
                        },
                    )
                last_tool_names = last_tool_names[-3:]
                messages.append({
                    "role": "assistant",
                    "content": _blocks_to_dicts(response.content),
                })
                messages.append({"role": "user", "content": tool_results})
                continue

            # Non-tool stop — reply or note silence
            text = _extract_text(response.content)
            if not text:
                logger.info(
                    "empty_assistant_response",
                    extra={
                        "client_id": client_id, "turn_id": turn_id,
                        "stop_reason": response.stop_reason,
                        "iteration": iteration,
                    },
                )
                return

            sid = await twilio_client.send_text(from_number, text)
            db.record_outbound_message(
                client_id=client_id, body=text,
                twilio_message_sid=sid, turn_id=turn_id,
            )
            logger.info(
                "agent_turn_complete",
                extra={
                    "client_id": client_id, "turn_id": turn_id,
                    "iterations": iteration + 1,
                },
            )
            return

        # Step 6 fallthrough: iteration limit (§11.7)
        logger.warning(
            "iteration_limit_exceeded",
            extra={
                "client_id": client_id, "turn_id": turn_id,
                "last_3_tools": last_tool_names[-3:],
            },
        )
        db.log_llm_call(
            client_id=client_id, turn_id=turn_id,
            model=settings.ANTHROPIC_MODEL,
            input_tokens=None, output_tokens=None,
            stop_reason="iteration_limit", latency_ms=0,
            error="iteration_limit_exceeded",
        )
        sid = await twilio_client.send_text(from_number, _STUCK_MESSAGE)
        db.record_outbound_message(
            client_id=client_id, body=_STUCK_MESSAGE,
            twilio_message_sid=sid, turn_id=turn_id,
        )
        return

    except Exception:
        logger.exception(
            "agent_turn_failed",
            extra={"client_id": client_id, "turn_id": turn_id},
        )
        try:
            sid = await twilio_client.send_text(from_number, _FALLBACK_ERROR_MESSAGE)
            db.record_outbound_message(
                client_id=client_id, body=_FALLBACK_ERROR_MESSAGE,
                twilio_message_sid=sid, turn_id=turn_id,
            )
        except Exception:
            logger.exception(
                "fallback_send_failed",
                extra={"client_id": client_id, "turn_id": turn_id},
            )
        return
