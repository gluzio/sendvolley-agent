# sendvolley-agent

The per-client agent that runs on a dedicated Hetzner VPS for each SendVolley customer. Receives WhatsApp messages via Twilio, reasons with Claude, calls SendVolley/Apollo/Instantly tools, sends replies back via Twilio.

Custom Python (FastAPI + Anthropic SDK). No agent framework. ~600–900 lines total.

**Read [ARCHITECTURE.md](./ARCHITECTURE.md) before writing or modifying any code.** It is the binding specification for v1. Anything not in ARCHITECTURE.md is out of scope until it's added there.

## Status

- v0.1 (in progress) — first build pass. Target: a single test client (`client_id=sendvolley`, dogfooding own outreach) can have a coherent WhatsApp conversation with the agent and get cold-email variants back from the SendVolley MCP Worker.

## Quick links

- **Companion repo (the IP):** `sendvolley-mcp` — Cloudflare Worker hosting the copy-generation prompt and exposing it as an MCP server. The agent in *this* repo calls that Worker as one of several tools.
- **Live Worker:** `https://sendvolley-mcp.gian-31d.workers.dev`
- **Test VPS:** `77.42.73.61` (Hetzner CX23, Helsinki, Ubuntu 24.04)

## Repo layout

```
sendvolley-agent/
├── README.md                       this file
├── ARCHITECTURE.md                 the binding spec — read first
├── pyproject.toml                  Python project + deps
├── schema.sql                      SQLite schema
├── install/                        provisioning scripts for fresh VPSs
│   ├── 01-bootstrap.sh
│   ├── 02-install-agent.sh
│   ├── 03-configure.sh
│   └── sendvolley-agent.service    systemd unit
└── sendvolley_agent/
    ├── main.py                     FastAPI app + lifespan
    ├── webhook.py                  Twilio signature + IP validation
    ├── agent.py                    the agent loop
    ├── twilio_client.py            outbound message sender
    ├── db.py                       SQLite connection + queries
    ├── config.py                   env vars (Pydantic Settings)
    ├── prompts/system.py           the agent's system prompt
    └── tools/
        ├── sendvolley.py           HTTP client for the Worker (MCP)
        ├── apollo.py               REST wrapper
        ├── instantly.py            REST wrapper
        └── memory.py               SQLite reads/writes
```

## Build order (binding for v1)

Files must be built in dependency order, one focused session each. **Do not start a later file until the earlier ones are working.**

1. `config.py`
2. `db.py` + `schema.sql`
3. `twilio_client.py`
4. `webhook.py` — *deploy to VPS, point Twilio sandbox at it, confirm inbound WhatsApp produces a logged event before moving on*
5. `agent.py` — the agent loop. **Most important file. Pair-write line by line.**
6. `tools/sendvolley.py` — first real tool. End-to-end test: WhatsApp → agent → Worker → reply.
7. `tools/memory.py`
8. `tools/instantly.py`
9. `tools/apollo.py`
10. `install/*` — provisioning scripts, only after everything above is working on the test VPS.

## License

Private. Do not distribute.