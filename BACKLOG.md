Per-client context bundle for copy generation. Today the Worker generates copy with no knowledge of who's asking. Pre v2:

Agent serializes active memory_facts into a markdown block on each generate_copy call
Worker accepts an optional client_context field, weaves it into the system prompt
When Instantly data flows in, also include per-variant reply-rate history
Open question: how much context until token costs become a concern? (Worker call is ~650 tokens in today; adding 2-4KB of client context = ~1000-2000 more)
Open question: do we eventually move to per-client Worker prompts (§5 says no for v1, revisit at client #5)? Or stay with one global prompt + per-call context blocks?
This is the moat. Design carefully.
# Backlog

## Pre-v2 (priority order)
- **Implement the remaining 8 tool stubs.** Apollo (search_people, enrich_person), Instantly (list_campaigns, campaign_stats, add_leads), memory (remember_fact, recall_facts), proposals (propose_prompt_change). Each is a focused session of the same shape as tools/sendvolley.py.
- **Per-client context bundle for the Worker.** Agent serializes active memory_facts into a markdown block, sends with each generate_copy call. Worker accepts optional `client_context` field. This is the moat — accumulated per-client learning that compounds. See conversation 2026-05-15.
- **Variant ranking pass.** Currently generate_copy returns variants in generation order. Worker should rank by likely reply-rate using a second model call. Open question: ranking criteria become per-client once we have campaign reply data — does ranking eventually move to the agent?

## Operational
- Twilio billing auto-recharge enabled (£20 minimum, trigger at £5)
- Auth token rotation: rotate after today's deploy session since the token appeared in a debug response.
- Caddy renewal monitoring: cert expires Aug 13 — auto-renews ~30 days prior; verify when renewal happens.

## Known v1 quirks
- Logs show TWILIO_ACCOUNT_SID uppercased in URLs (config.py validator does v.upper()); confirmed working but inconsistent with Twilio's canonical casing. Not a problem; cosmetic.