# SendVolley agent — architecture (v1)

**This document is binding.** Any code in this repo must conform to it. If a proposed change conflicts with this document, the document wins — update the document first, then change the code. Do not silently drift.

Last updated: May 14, 2026.

---

## 1. What this is

A custom Python agent that runs **one instance per SendVolley client**, on a dedicated Hetzner CX23 VPS provisioned by SendVolley's onboarding flow. The agent:

- Receives the client's WhatsApp messages via Twilio webhooks.
- Reasons about what the client is asking using Claude (Anthropic API).
- Calls a small set of tools to do real work: generate cold-email copy (via the SendVolley MCP Worker), search prospects (Apollo), schedule sends and pull stats (Instantly), and read/write per-client memory (local SQLite).
- Sends replies back to the client via Twilio's WhatsApp API.
- Wakes itself on a schedule (systemd timers) to do proactive work: weekly stats summaries, draft prompt-improvement proposals for human review.

It is **not** a general-purpose agent, **not** a Hermes-style framework, **not** multi-channel. It does one job: be a SendVolley client's AI agent for cold outreach operations.

---

## 2. Architecture diagram

```
┌────────────────────┐                  ┌─────────────────────┐
│       Client       │ ───WhatsApp───►  │   Twilio WhatsApp   │
│  WhatsApp on phone │ ◄───────────     │   Business API      │
└────────────────────┘                  └──────────┬──────────┘
                                                   │
                                          webhooks │ outbound
                                                   │ API calls
                                                   ▼
   ┌───────────────────────────────────────────────────────────┐
   │  Per-client Hetzner VPS  (sendvolley-client-<client_id>)  │
   │                                                            │
   │                  ┌─────────────────────────┐               │
   │                  │ FastAPI                 │               │
   │                  │ /whatsapp webhook       │               │
   │                  │ Twilio sig + IP verify  │               │
   │                  └────────────┬────────────┘               │
   │                               │                            │
   │  ┌──────────────┐             ▼              ┌──────────┐  │
   │  │  systemd     │   ┌─────────────────────┐  │  SQLite  │  │
   │  │  timers      │──►│  Agent loop         │◄►│  state   │  │
   │  │  (cron)      │   │  Claude SDK         │  │          │  │
   │  └──────────────┘   │  Tool routing       │  └──────────┘  │
   │                     └──────────┬──────────┘                │
   │                                │                            │
   └────────────────────────────────┼────────────────────────────┘
                                    │ HTTPS (REST + MCP)
                ┌───────────────────┼──────────────────┐
                ▼                   ▼                  ▼
        ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
        │  Anthropic   │    │  SendVolley  │    │   Apollo +   │
        │  Claude API  │    │  MCP Worker  │    │   Instantly  │
        │  Sonnet 4.6  │    │  (THE IP)    │    │   REST APIs  │
        └──────────────┘    └──────────────┘    └──────────────┘
```

**Color/role legend (for the visual version of this diagram):**
- On-VPS code = "your code per client" (FastAPI, agent loop, SQLite, systemd)
- SendVolley MCP Worker = THE MOAT. Lives separately on Cloudflare, hosts the copy-generation prompt. Never deployed per-client.
- External APIs = Anthropic, Apollo, Instantly, Twilio — third-party services we call.

---

## 3. The data flow (canonical message round-trip)

1. Client types into WhatsApp on their phone.
2. Twilio receives the message, POSTs a webhook to `https://<client-id>.sendvolley.com/whatsapp` with the message body, sender info, and an HMAC signature in the `X-Twilio-Signature` header.
3. FastAPI's `/whatsapp` handler validates **both** the Twilio HMAC signature **and** that the source IP is in Twilio's published webhook IP ranges. If either check fails → reject with 403, log the attempt.
4. The validated message is persisted to SQLite's `conversations` table.
5. The webhook handler enqueues the agent turn as a background task (asyncio.create_task) and returns 200 OK with an empty TwiML body immediately. The agent task runs the loop asynchronously and posts the outbound reply via Twilio's REST API when done. Inbound and outbound are fully decoupled — Twilio never waits for Claude. See §11.1 for the task-management pattern. The agent loop itself:
   a. Loads the last N turns of conversation history from SQLite.
   b. Loads the client's durable memory facts from SQLite (`memory_facts` table).
   c. Builds a system prompt that includes the SendVolley agent's role + the user's persistent facts.
   d. Calls Claude via the Anthropic SDK with the message, history, and the **bound tool catalog** (see §6).
   e. If Claude returns a tool-use block: executes the tool, captures the result, re-calls Claude with the tool result appended. Loops until Claude returns text-only (max iterations: 30, hard cap).
   f. Every Claude call and every tool call is logged to SQLite's `tool_calls` table.
6. The final text response is sent to Twilio's WhatsApp API as an outbound message. The outbound message is persisted to `conversations`.
7. Twilio delivers to the client's WhatsApp.
Concurrency note. Multiple agent turns may run concurrently for the same client (e.g. if the client sends two messages in quick succession). This is expected and supported. Each task owns its own SQLite reads/writes; conflicts are handled by SQLite's locking. The agent loop is not synchronized across turns within a client.

**Target latency:** 3–15 seconds end-to-end depending on tool calls. >20s is a bug.

**Systemd timer variant of the same flow:** same agent loop, no inbound webhook. A timer fires (e.g. weekly Monday 9am UTC), passes a system-generated prompt to the agent loop ("summarize last week's Instantly campaign stats and DM the client"), the loop runs, the final reply goes via Twilio as a **template message** (because we're outside the 24-hour session window). Template messages cost ~$0.01–0.05 each and must be pre-registered in Twilio.

---

## 4. Locked-in decisions

These are decided. **Do not relitigate without an explicit ARCHITECTURE.md edit.**

### 4.1 WhatsApp number strategy
**Per-client Twilio WhatsApp number.** Each client gets their own dedicated number (~$1.50/month). Cleaner isolation, simpler debugging, better client trust. Revisit shared-sender architecture only when active clients exceed ~50.

### 4.2 DNS / webhook URL strategy
**Wildcard DNS:** `*.sendvolley.com` resolves to a Hetzner load balancer (or directly to per-client VPS IPs in v1 — load balancer is a v2 optimization). Each client gets `<client-id>.sendvolley.com`. **Caddy** runs on each VPS as a reverse proxy in front of FastAPI, handling automatic Let's Encrypt SSL provisioning and renewal. No manual cert management ever.

### 4.3 Anthropic API key strategy
**v1: SendVolley uses its own single Anthropic API key** for all clients. Cost is absorbed by SendVolley as part of the retainer. This reduces onboarding friction (no "get an Anthropic API key" step for new clients). **v2: client-supplied keys.**

The code must support this future migration cheaply. Specifically:
- `config.py` has `ANTHROPIC_KEY_MODE` env var, values: `"ours"` (v1) or `"client"` (v2).
- SQLite `clients` table has a nullable `anthropic_api_key` column.
- At agent-init time, if mode is `"ours"`, use the env var key; if `"client"`, look up `clients.anthropic_api_key` for the active client_id.
- About 20 lines of code total. Don't over-abstract this — no "LLM provider plugin system." Claude is the only model we support, ever.

### 4.4 Webhook authentication
**Belt and braces.** Every inbound Twilio webhook must pass:
1. **HMAC signature verification** using Twilio's signing-key algorithm against the `X-Twilio-Signature` header.
2. Source IP check — request must come from Twilio's published webhook IP ranges. The list is fetched synchronously at FastAPI startup before the app accepts traffic. If the startup fetch fails, the process exits with a non-zero code (no fail-open). On each subsequent webhook, the cache is checked: if older than 24h, an async refresh is triggered out-of-band; if that refresh fails, the existing (stale) list continues to be used and a twilio_ip_refresh_failures event is logged. See §11.5 for the implementation pattern. There is no systemd timer.

Failed verification: respond 403, log to `webhook_failures` table with timestamp, source IP, header dump (redacted). No retry-friendly responses; this is hostile-traffic territory.

---

## 5. Explicitly OUT of scope for v1

**These exist deliberately. Do not add any of them without a documented change to this file.** This list is the most important section of the document — it is the anti-scope-creep fence.

- ❌ **Autonomous prompt edits.** The agent CAN draft prompt-improvement proposals to `proposals/` for human review. The agent CANNOT edit the SendVolley Worker's `src/prompt.ts` directly. Ratification is always a human pushing a git commit.
- ❌ **Browser automation.** No headless Chrome, no Playwright, no "let the agent click around websites." Prompt-injection vector, memory hog, unnecessary for cold outreach.
- ❌ **Multi-channel messaging.** WhatsApp only in v1. No Telegram, Discord, Slack, email, SMS. Adding a second channel is an architectural change, not a small feature.
- ❌ **Image/voice/file input.** Text WhatsApp messages only. If the client sends a photo or voice note, agent responds: "I can only process text messages right now."
- ❌ **Multiple LLM providers.** Claude is the only model. No OpenRouter, no fallback to GPT/Gemini, no "provider abstraction layer."
- ❌ **Task delegation / sub-agents.** No spawning child agents. The single agent loop handles everything sequentially.
- ❌ **Per-client prompt overrides on the Worker.** One global SendVolley copy-generation prompt for all clients in v1. Per-client variants when client #5 onboards.
- ❌ **Web search.** No `web_search` tool. The agent operates entirely on data from SQLite, Apollo, Instantly, and the SendVolley Worker.
- ❌ **Code execution tool.** Agent does not run shell commands or Python eval.
- ❌ **File operations tool.** Agent does not read or write files on the VPS filesystem outside SQLite.
- ❌ **Cron jobs that *the agent itself* creates.** Systemd timers are configured at install time only, by the human operator (you). The agent cannot create new scheduled jobs at runtime in v1.

---

## 6. The agent contract

### 6.1 The agent loop (sketch)

```python
def run_agent_turn(client_id: str, user_message: str) -> str:
    history = db.load_recent_turns(client_id, limit=N_HISTORY)
    facts = db.load_memory_facts(client_id)
    system_prompt = build_system_prompt(client_id, facts)

    messages = history + [{"role": "user", "content": user_message}]
    for iteration in range(MAX_ITERATIONS):
        response = anthropic.messages.create(
            model="claude-sonnet-4-6",
            system=system_prompt,
            messages=messages,
            tools=TOOL_CATALOG,
            max_tokens=2048,
        )
        if response.stop_reason == "tool_use":
            tool_results = [execute_tool(b, client_id) for b in response.content if b.type == "tool_use"]
            messages.append({"role": "assistant", "content": response.content})
            messages.append({"role": "user", "content": tool_results})
            continue
        else:
            return extract_text(response.content)
    raise AgentIterationLimitExceeded(...)
```

That is *the* shape. The whole product is wrapped around it. Do not outsource this loop to a library. About 80 lines including logging and error handling.

### 6.2 Tool catalog (the full v1 list)

| Tool name | What it does | Where it lives |
|---|---|---|
| `generate_copy` | Call the SendVolley MCP Worker. Returns cold-email variants. | `tools/sendvolley.py` |
| `apollo_search_people` | Search Apollo for prospects matching criteria. | `tools/apollo.py` |
| `apollo_enrich_person` | Enrich a single Apollo contact. | `tools/apollo.py` |
| `instantly_list_campaigns` | List active Instantly campaigns. | `tools/instantly.py` |
| `instantly_campaign_stats` | Fetch reply rate, open rate, deliverability for a campaign. | `tools/instantly.py` |
| `instantly_add_leads` | Add prospects (with copy) to a campaign. | `tools/instantly.py` |
| `remember_fact` | Save a durable fact to `memory_facts`. | `tools/memory.py` |
| `recall_facts` | Read all current facts for the client. | `tools/memory.py` |
| `propose_prompt_change` | Draft a markdown proposal to `proposals/` for human review. | `tools/proposals.py` |

**That is the entire tool catalog.** Nine tools. Adding a tenth requires an ARCHITECTURE.md edit.

### 6.3 The system prompt (shape, not contents)

```
You are SendVolley, the AI agent for {client_name}. Your job is to help them run cold outreach campaigns.

Your tools:
{tool_descriptions_auto_generated_from_TOOL_CATALOG}

What you know about {client_name}:
{memory_facts_formatted}

Operating constraints:
- You're talking to the client via WhatsApp. Keep responses concise (≤200 words usually).
- For multi-step tasks (build list → enrich → write copy → schedule), use the todo pattern: state the plan, execute each step, report progress.
- Never invent prospect data, campaign stats, or copy variants. Always use a tool to fetch real information.
- Never claim to have done something you haven't actually done via a tool call.
- For destructive actions (deleting campaigns, removing leads), always confirm with the client first.
```

The full system prompt lives in `sendvolley_agent/prompts/system.py` and is iterated based on real usage. Like the Worker's `src/prompt.ts`, this prompt is part of the IP — keep it in the private repo, never commit to a public mirror.

---

## 7. State model (SQLite tables)

Defined fully in `schema.sql`. Summary:

- `clients` — one row per client this VPS serves (v1: always exactly one row).
- `conversations` — every inbound and outbound WhatsApp message.
- `tool_calls` — every Claude call and every tool invocation, with latency, tokens, error.
- `memory_facts` — durable facts about the client (their voice, preferences, ICP, what's worked before).
- `proposals` — agent-drafted prompt improvements awaiting human review.
- `webhook_failures` — rejected webhook attempts (auth failures, hostile traffic).

Schema is multi-tenant in shape (`client_id` column on every table) even though v1 always has one client per VPS. Cheap to write now, expensive to refactor later.

---

## 8. Configuration (environment variables)

All config via env vars, read once at startup through Pydantic Settings (`sendvolley_agent/config.py`). The full list:

```
# Required
CLIENT_ID                     # e.g. "sendvolley"
CLIENT_NAME                   # e.g. "SendVolley (internal)"
ANTHROPIC_API_KEY             # SendVolley's key in v1
ANTHROPIC_KEY_MODE=ours       # "ours" or "client" (v2)

SENDVOLLEY_WORKER_URL         # https://sendvolley-mcp.gian-31d.workers.dev
SENDVOLLEY_WORKER_TOKEN       # sv_live_... bearer token

STARTUP_TWILIO_FETCH_TIMEOUT=10
TWILIO_ACCOUNT_SID
TWILIO_AUTH_TOKEN
TWILIO_WHATSAPP_NUMBER        # e.g. "whatsapp:+14155551234"
TWILIO_WEBHOOK_URL            # https://<client-id>.sendvolley.com/whatsapp

APOLLO_API_KEY
INSTANTLY_API_KEY

# Optional / with defaults
ANTHROPIC_MODEL=claude-sonnet-4-6
AGENT_MAX_ITERATIONS=30
N_HISTORY_TURNS=20
DB_PATH=/var/lib/sendvolley/state.db
LOG_LEVEL=INFO
```

Secrets never logged. `LOG_LEVEL=DEBUG` must still redact keys.

---

## 9. Coding conventions

- **Python 3.12.** Type hints everywhere. `from __future__ import annotations` at the top of every module.
- **Async where it matters:** FastAPI handlers, Anthropic SDK calls, Twilio API calls, outbound HTTPS to the Worker. Synchronous SQLite is fine (it's local, fast, single-writer).
- **No magic.** No metaclasses, no `__getattr__` tricks, no decorators that hide control flow. The whole codebase should be readable top-to-bottom by someone debugging at 11pm.
- **No silent failures.** Every exception either: logs structured + re-raises, or logs + returns a documented error response. Never swallow.
- **Tests come later.** v1 is shipped on the strength of careful hand-review and dogfooding. Pytest scaffolding is welcomed but not required for v1.
- **One responsibility per file.** If a file does two things, it should be two files.

---

## 10. What success looks like for v1

A single sentence: **Gianluzio can DM the SendVolley agent on WhatsApp, ask it for 3 cold email variants for a specific PWI campaign brief, and receive 3 variants generated by the SendVolley MCP Worker within 15 seconds.**


## 11. Resolved ambiguities (v1 build pass)
This section records the answers to questions raised during the v1 build review. Each entry is a normative requirement, not commentary. If the implementation diverges from any of these, the document was not updated — update it first, then change the code.
11.1 Webhook → agent task model
The /whatsapp handler is non-blocking. Sequence:

Validate Twilio HMAC signature and source IP (reject 403 on failure).
Persist inbound message to conversations.
Schedule the agent turn: task = asyncio.create_task(run_agent_turn(...)).
Add the task to a module-level set _pending_tasks: set[asyncio.Task] and register task.add_done_callback(_pending_tasks.discard) to prevent garbage collection mid-flight.
Return 200 OK with an empty TwiML body.

Do not use FastAPI's BackgroundTasks — it blocks the worker until completion. Use asyncio.create_task directly.
11.2 Tool error propagation
Every tool invocation is wrapped. Exceptions raised by a tool are caught, logged, and converted to a tool_result content block with is_error: True and a Claude-readable message in the form Tool {name} failed: {ExceptionType}: {message[:200]}. The agent loop continues; Claude decides how to handle the failure (retry, switch tools, explain to user).
If the wrapper itself raises (i.e. a bug in execute_tool_safely, not in a tool), that bubbles to the outer agent-loop try/except and is logged as a wrapper_failed event.
The wrapper signature:
pythonasync def execute_tool_safely(block, client_id: str, db) -> dict:
    # returns {"type": "tool_result", "tool_use_id": ..., "content": ..., "is_error": bool}
11.3 Outbound HTTP retry policy
For all outbound HTTPS calls (SendVolley Worker, Apollo, Instantly, Twilio outbound):

Attempts: 3 maximum.
Backoff: exponential, 0.5s → 1s → 2s between attempts.
Retry on: HTTP 5xx, connection errors, read timeouts.
Do not retry on: HTTP 4xx (client errors are our bug), TLS errors.
Logging: every attempt logged with attempt number, latency, outcome.

Implementation: use httpx.AsyncClient with a custom httpx.AsyncHTTPTransport(retries=...) OR tenacity decorators on the call functions — pick one and use it consistently across all four outbound modules. Do not mix.
Inbound webhook acknowledgement is never retried — Twilio's timeout is short and our response (per §11.1) is fast by design.
No retry compounding: retries only happen at the HTTP transport layer. If Claude chooses to call the same tool again after a failure, that's a fresh tool call, not a retry — and it gets its own 3-attempt budget. The agent loop does not implement automatic "try again" logic on top of transport retries.
11.4 History window definition
N_HISTORY_TURNS counts individual messages (rows in conversations), not user/assistant pairs. The query:
sqlSELECT role, content, created_at
FROM conversations
WHERE client_id = ?
ORDER BY created_at DESC
LIMIT ?
Results are reversed before being passed to the agent loop (chronological order for Claude).
Default value: 20. Tuneable via env var. May need to drop to 10 if observed token bloat from tool-result-heavy turns becomes a problem.
11.5 Twilio IP allowlist refresh
The Twilio IP ranges live in a module-level cache:
python_twilio_ips: dict = {"ranges": [...], "fetched_at": datetime}
Startup: fetch synchronously from Twilio's docs URL before FastAPI accepts traffic. Timeout: STARTUP_TWILIO_FETCH_TIMEOUT env var (default 10s). If the fetch fails or times out, the process exits non-zero. No fail-open.
Steady state: on each inbound webhook, check now - _twilio_ips["fetched_at"]. If older than 24h, schedule an out-of-band async refresh (do not block the request). If the refresh fails, keep the stale cache and log a twilio_ip_refresh_failure event.
No systemd timer. The refresh is request-driven plus self-healing on restart.
11.6 memory_facts schema shape
Lightly categorized free-form prose. Schema:
sqlCREATE TABLE memory_facts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  client_id     TEXT NOT NULL REFERENCES clients(id),
  category      TEXT NOT NULL,
  fact          TEXT NOT NULL,
  created_at    INTEGER NOT NULL,
  updated_at    INTEGER NOT NULL,
  superseded_by INTEGER REFERENCES memory_facts(id)
);
CREATE INDEX idx_memory_facts_client_category
  ON memory_facts(client_id, category)
  WHERE superseded_by IS NULL;
category is free-form text chosen by Claude when calling remember_fact (suggested values in the tool description: voice, icp, preferences, campaign_history, other). No enum constraint. If categories proliferate weirdly in practice, constrain later.
Facts are never hard-deleted. Replacement uses superseded_by to link the old fact to the new one. The partial index excludes superseded facts from active reads.
recall_facts returns only active (non-superseded) facts for the client.
11.7 Iteration-limit handling
When AgentIterationLimitExceeded is raised in the agent loop:

Log structured error event iteration_limit_exceeded with client_id, turn_id, last 3 tool calls.
Insert a row in tool_calls with tool_name='__agent_loop__', error='iteration_limit_exceeded'.
Send a Twilio reply to the client: "I'm getting stuck on this — could you give me more detail or break it into smaller steps?"

Do not write to webhook_failures. That table is reserved for hostile/rejected traffic, not normal-conversation anomalies.
11.8 Parallel tool execution
Sequential in v1. When Claude returns multiple tool_use blocks in a single assistant turn, execute them in order, collect results, then re-call Claude with all results.
Rationale: simpler error story, simpler logging, tools are not independent (later tools often depend on earlier results within the same turn).
Parallel execution via asyncio.gather is a v1.x addition if and when we observe specific cases where it'd help. ~5 lines of code; not pre-built.
11.9 tools/proposals.py (renamed from being inside tools/memory.py)
The propose_prompt_change tool lives in its own file tools/proposals.py, not in tools/memory.py. Cosmetic; reflects that proposals are a distinct concept from durable memory facts. Implementation is a small append to a markdown file in proposals/ plus a row in the proposals SQLite table.
11.10 config.py conventions (Pydantic Settings v2)
The settings module must follow these conventions:

Use the standalone pydantic-settings package (not pydantic.BaseSettings from Pydantic v1, which is deprecated).
Class-level config: model_config = SettingsConfigDict(env_file=".env", extra="forbid", case_sensitive=True). Unknown env vars are a hard error at boot.
All secret-bearing fields use repr=False (Pydantic v2 syntax). print(settings) never leaks keys.

Field typing:

URL fields (SENDVOLLEY_WORKER_URL, TWILIO_WEBHOOK_URL) are typed as plain str, not pydantic.HttpUrl. HttpUrl returns a Url object which creates surprise behavior at downstream use sites; validation is enforced explicitly instead (see below).
DB_PATH is typed as pathlib.Path so the boot-time writeable check and downstream consumers can call .parent / .exists() / etc. directly.

Validators baked in for every field that has a structural constraint:

CLIENT_ID matches ^[a-z0-9-]+$ (lowercase, digits, hyphens only).
SENDVOLLEY_WORKER_URL and TWILIO_WEBHOOK_URL: parsed via urllib.parse.urlparse — scheme must be https, netloc must be present, any trailing / is stripped at validation time for predictable string concatenation downstream.
TWILIO_WHATSAPP_NUMBER starts with whatsapp:+.
ANTHROPIC_API_KEY starts with sk-ant- (catches paste-the-wrong-thing mistakes).
SENDVOLLEY_WORKER_TOKEN starts with sv_live_.
TWILIO_ACCOUNT_SID matches ^AC[a-f0-9]{32}$ (case-insensitive). Twilio account SIDs have a fixed shape; enforcing it catches typos at boot.
ANTHROPIC_KEY_MODE is one of {"ours", "client"}.
LOG_LEVEL is one of {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}.
AGENT_MAX_ITERATIONS is a positive int, 1 ≤ value ≤ 100.
N_HISTORY_TURNS is a positive int, 1 ≤ value ≤ 100.
STARTUP_TWILIO_FETCH_TIMEOUT is a positive int, 1 ≤ value ≤ 60 (seconds).


Boot-time filesystem check on DB_PATH:

Parent directory must exist.
Parent directory must be writeable by the current user.
If not, raise a clear ConfigurationError at startup. This verifies the install script created /var/lib/sendvolley/ and chowned to sendvolley.


Custom exceptions live in sendvolley_agent/errors.py — not in config.py. ConfigurationError, AgentIterationLimitExceeded, ToolExecutionError, and WebhookAuthenticationError are all declared there so other modules can catch them without triggering the settings-singleton load at config import time. config.py imports ConfigurationError from errors.

Settings instance is a module-level singleton (settings = Settings() at the bottom of config.py). Other modules from sendvolley_agent.config import settings.


Once that works end-to-end on the test VPS, v1 is functionally complete. Everything after that is incremental capability (Apollo, Instantly, memory, cron, onboarding script for client #2) on the same skeleton.