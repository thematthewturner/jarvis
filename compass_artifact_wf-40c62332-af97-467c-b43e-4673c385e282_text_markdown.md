# Build your AI Jarvis this weekend for under $10/month

**The winning architecture is surprisingly simple: Claude API + plain Python + direct SDK integrations, deployed on a free Oracle Cloud instance.** No framework needed. The AI agent ecosystem has matured enough by early 2026 that a Python-comfortable developer can wire together `python-telegram-bot`, Anthropic's tool-calling API, and first-party Python SDKs for every required service into a robust personal agent in a single weekend — all for **$3–8/month** in API costs. Frameworks like LangGraph and orchestrators like n8n are strong alternatives, but they add complexity that isn't justified for a single-user system with well-defined integrations.

The critical insight driving this recommendation: every integration you need — Monarch Money, Todoist, Notion, Google Calendar — now has a mature async Python library, and Claude's tool-calling API with the new Tool Runner handles the orchestration loop that frameworks used to be required for. MCP (Model Context Protocol) servers exist for all four services if you want plug-and-play later, but direct SDK calls give you more control and fewer moving parts for an MVP.

---

## Every approach ranked for this exact use case

After evaluating 13 tools and frameworks against your specific requirements (Telegram bot, Notion backend, Monarch Money, $50 budget, weekend MVP), the field narrows quickly. Here's how they stack up, grouped by viability.

**Tier 1 — Recommended**

| Approach | Weekend MVP? | Monthly cost | Why it works |
|----------|-------------|-------------|--------------|
| **Claude API + Python scripts + cron** | ✅ Yes | $3–8 | Simplest architecture, full control, tool_runner handles orchestration |
| **n8n self-hosted** | ✅ Yes | $10–25 | Visual workflows, native Telegram/Notion/Calendar nodes, AI Agent node |

**Tier 2 — Viable but slower**

| Approach | Weekend MVP? | Monthly cost | Tradeoff |
|----------|-------------|-------------|----------|
| **LangGraph 1.0** | ⚠️ 1–2 weeks | $5–12 | Most robust agent framework, but graph-paradigm learning curve |
| **Claude + MCP servers** | ⚠️ 1 week | $3–8 | Elegant plug-and-play integrations, but adds architectural complexity |
| **OpenAI Responses API + function calling** | ⚠️ 1 week | $2–6 | Cheapest API (GPT-4o-mini), but newer API with less documentation |

**Tier 3 — Poor fit for this use case**

| Approach | Why it fails |
|----------|-------------|
| **OpenClaw** | Not a Python framework — it's a ready-made Node.js/TS app. Security concerns flagged by Cisco and CrowdStrike. Founder just joined OpenAI, governance uncertain. High token consumption (~50x per interaction). |
| **CrewAI** | Designed for multi-agent batch task execution, awkward fit for a conversational assistant. Not yet at stable 1.0. |
| **AutoGen** | **In maintenance mode** since Oct 2025. Microsoft merged it into "Microsoft Agent Framework." Don't start new projects here. |
| **Semantic Kernel** | Python SDK is secondary to C#. Being absorbed into Microsoft Agent Framework. Enterprise-oriented, overkill for personal use. |
| **Composio** | A tool-integration middleware layer, not a standalone solution. You'd still need to build the bot and agent yourself. Useful as an add-on, not a foundation. |
| **Lindy.ai** | iMessage-focused, not Telegram. Pro tier at $49.99/month eats entire budget with limited credits. |
| **Relevance AI** | Business/sales-focused agent builder. No native Telegram or Notion integration. Wrong tool entirely. |
| **Windmill** | Developer-first script runner, but no pre-built integration nodes. You'd be writing everything from scratch with less ecosystem support than plain Python. |
| **Activepieces** | Solid n8n alternative (MIT license), but smaller community, fewer templates for this exact use case, and AI agent capabilities are newer. |
| **OpenAI Assistants API** | **Deprecated August 2025**, shutdown August 2026. Replaced by Responses API + Conversations API. |

---

## The recommended architecture in detail

The stack is intentionally minimal: **five Python packages, one database file, one cloud instance**. Every component is chosen for stability, async compatibility, and minimal abstraction overhead.

```
┌─────────────┐     ┌──────────────────┐     ┌─────────────────────┐
│  Telegram    │────▶│  python-telegram  │────▶│  Claude API         │
│  (you)       │◀────│  -bot v21+        │◀────│  (Haiku 4.5 routine │
└─────────────┘     │  + APScheduler    │     │   Sonnet 4.5 reports)│
                    └────────┬─────────┘     └──────────┬──────────┘
                             │                          │
                    ┌────────▼─────────┐     ┌──────────▼──────────┐
                    │  SQLite           │     │  Tool Functions      │
                    │  (memory, state)  │     │  ├─ notion-client    │
                    └──────────────────┘     │  ├─ todoist-api-py   │
                                             │  ├─ monarchmoney     │
                                             │  ├─ google-calendar  │
                                             │  └─ custom helpers   │
                                             └─────────────────────┘
```

**Why Claude API over OpenAI for this use case**: Claude's Tool Runner (`client.beta.messages.tool_runner()`) automatically handles the tool-call loop — you decorate Python functions with `@beta_tool`, and the SDK manages the back-and-forth without manual conversation management. This eliminates the main reason developers reach for frameworks. Claude Haiku 4.5 at **$1/$5 per million tokens** (input/output) offers near-frontier quality for routine tasks, while Sonnet 4.5 at $3/$15 handles complex weekly reports. Prompt caching gives **90% savings** on repeated system prompts.

**Why not n8n?** n8n is the strongest alternative and arguably faster for a first prototype — community templates exist for "AI Telegram Assistant + Notion" workflows. But n8n adds a dependency layer between you and your integrations. When Monarch Money's unofficial API changes its GraphQL schema (which happens), you'll debug faster in Python than in n8n's HTTP Request node. For a system you'll maintain for months or years, owning the code directly pays off.

---

## Integration-by-integration feasibility check

**Monarch Money** has no official API, but the `monarchmoney` Python library (328 GitHub stars, MIT license) reverse-engineers their GraphQL endpoint. It's fully async, supports MFA via TOTP secret, and sessions persist for months after initial login. Available data includes transactions, accounts, budgets, cash flow, and net worth. The community fork by bradleyseanf fixes the domain migration to `api.monarch.com`. An MCP server also exists (`robcerda/monarch-mcp-server`) wrapping this library. **Risk**: as an unofficial API, it can break when Monarch updates their frontend. The community typically patches within days.

**Telegram bot framework**: `python-telegram-bot` (PTB) v21.11 is the safest choice — **28,000+ GitHub stars**, fully async since v20, built-in job queue via APScheduler (perfect for scheduled briefings), and the largest English-language community. `aiogram` v3.25 is an excellent async-native alternative favored in the Eastern European developer community. Single-user security is trivial: check `message.from_user.id` against your Telegram user ID and silently drop everything else.

**Notion**: The `notion-client` SDK v2.7.0 (2,100 stars) mirrors the official JavaScript SDK's API surface and supports both sync and async usage. The official API supports full database CRUD — **creating append-only database entries** is a single `pages.create()` call with typed properties. Rate limit is 3 requests/second, which is generous for personal use. Notion also now offers an **official hosted MCP server**, useful if you later want Claude to dynamically discover Notion operations.

**Todoist**: The official `todoist-api-python` SDK v3.2.1 has a killer feature for this use case — the **Quick Add API** accepts natural language strings like "Submit report by Friday 5pm #Work @review p1" and automatically parses them into structured tasks with dates, projects, labels, and priorities. This means your Claude agent can simply pass the user's natural language directly to Todoist's parser instead of doing its own extraction.

**Google Calendar**: Standard `google-api-python-client` with OAuth 2.0. For a headless server deployment, use a **service account** (share your calendar with the service account email) to avoid the browser-based OAuth flow entirely. Reading upcoming events for morning briefings is a single `events().list()` call.

**MCP server ecosystem**: Official MCP servers exist from **Todoist** (hosted at `ai.todoist.net/mcp` with OAuth) and **Notion** (hosted by Notion). Community MCP servers cover Google Calendar (28 tools) and Monarch Money (31 tools). The Python MCP SDK is at v1.26.0 with full support for stdio, SSE, and Streamable HTTP transports. However, for your MVP, **direct SDK calls are simpler** than running MCP servers — save MCP for a future upgrade when you want Claude to dynamically discover tools.

---

## Cost breakdown comes in dramatically under budget

The total system runs for **$3–11/month** depending on hosting choice, leaving massive headroom under the $50 ceiling.

| Component | Option A (cheapest) | Option B (reliable) | Option C (easiest) |
|-----------|-------------------|--------------------|--------------------|
| **Hosting** | Oracle Cloud Free (PAYG) — $0 | Hetzner CX23 — $3.80 | Railway Hobby — $5 |
| **LLM API** | GPT-4o-mini only — $0.61 | Haiku 4.5 + Sonnet 4.5 — $5 | Haiku 4.5 + Sonnet 4.5 — $5 |
| **SaaS** | All free tiers — $0 | All free tiers — $0 | All free tiers — $0 |
| **Total** | **~$1/month** | **~$9/month** | **~$10/month** |

Oracle Cloud's Always Free tier is absurdly generous — **4 ARM OCPUs + 24 GB RAM** for $0. The key gotcha: upgrade to Pay-As-You-Go immediately (you still pay $0 for free-tier resources) to avoid idle-instance reclamation and get actual support. If Oracle proves difficult to provision (capacity issues are common in popular US regions), Hetzner at €3.49/month is rock-solid. Railway at $5/month offers the simplest deployment (git push → auto-deploy) if you want zero infrastructure management.

**LLM API costs are negligible** at personal-use volumes. At 75 messages/day plus daily briefings and weekly reports, total token consumption is roughly 1.2M input + 700K output tokens per month. Even using Claude Haiku 4.5 for everything, that's **~$4.80/month**. Using GPT-4o-mini for routine messages drops it to under $1. The smart routing strategy — Haiku 4.5 for routine, Sonnet 4.5 for reports — lands around **$5/month** and gives you best-in-class quality where it matters.

All required SaaS services (Todoist, Google Calendar, Notion, Telegram Bot API) have **free tiers** that are more than sufficient for single-user personal use.

---

## Weekend build plan: four phases across two days

**Phase 1 — Saturday morning (3 hours): Core bot + deployment**

Set up the project skeleton. Create a Python project with `python-telegram-bot`, `anthropic`, and `aiosqlite`. Implement the Telegram bot with authorized-user middleware (check `from_user.id`). Wire up Claude Haiku 4.5 with a system prompt defining the assistant's personality and capabilities. Define 2–3 simple tool functions (e.g., `get_current_time`, `save_note_to_sqlite`). Test the conversational loop locally. Deploy to your cloud host with Docker and `--restart unless-stopped`. **Milestone**: you can chat with your Jarvis via Telegram and it responds intelligently.

**Phase 2 — Saturday afternoon (4 hours): Calendar + Todoist + daily briefing**

Integrate Google Calendar API (use service account for headless auth). Implement `get_todays_events()` and `get_weeks_events()` as Claude tools. Integrate Todoist SDK — implement `add_task()` and `get_tasks()` as Claude tools. Wire up Todoist's Quick Add for natural language task capture. Set up APScheduler to trigger a daily morning briefing at your preferred time — the briefing pulls calendar events and today's tasks, sends them to Claude for formatting, then pushes the result to Telegram. **Milestone**: "What's on my calendar today?" works, "Remind me to call the dentist Friday" creates a Todoist task, and you get a morning briefing at 7 AM.

**Phase 3 — Sunday morning (3 hours): Notion + passive capture**

Set up Notion databases (Transactions, Notes, Weekly Briefs). Implement `append_to_notion()` tool for structured database writes. Build the passive data capture system: Claude analyzes every incoming message and determines if it contains actionable data (expense, idea, task, note) and routes it to the appropriate Notion database or Todoist. Implement conversation memory in SQLite (store last N messages per conversation, summarize older ones). **Milestone**: sending "Spent $45 at Whole Foods" automatically creates a categorized entry in your Notion Transactions database.

**Phase 4 — Sunday afternoon (3 hours): Financial intelligence + weekly brief**

Integrate `monarchmoney` library — implement `get_recent_transactions()`, `get_account_balances()`, `get_monthly_budget()` as Claude tools. Set up the weekly executive brief: APScheduler triggers Sunday at 6 PM, pulls calendar events for past/upcoming week, Todoist completed/pending tasks, Monarch Money transaction summary, and any Notion entries — sends everything to Claude Sonnet 4.5 for a comprehensive narrative brief, delivered via Telegram. Set up monthly financial summary on the 1st. **Milestone**: a complete working system with all integrations.

---

## What makes this architecture win long-term

**Maintenance burden is minimal** because the architecture has no framework layer to update or debug. When Anthropic releases new models, you change one string. When a Python SDK updates, you bump one dependency. There's no n8n version to upgrade, no LangGraph graph to refactor, no CrewAI crew to reconfigure. The entire system is ~500–800 lines of Python across 4–5 files.

**The upgrade path is clear.** Once the MVP is stable, you can add MCP servers for any integration where you want Claude to dynamically discover capabilities rather than using hard-coded tool functions. LangGraph becomes worth adopting if you need durable multi-step workflows (e.g., a weekly financial review that requires human approval before executing trades). For now, these are premature optimizations.

**Security is straightforward**: no public web endpoints (use Telegram polling mode, not webhooks), OAuth where available (Google Calendar, Todoist), encrypted session storage for Monarch Money, all credentials in environment variables loaded from a chmod-600 `.env` file, and the single-user ID check ensures only you can interact with the bot.

## Conclusion

The AI agent tooling landscape in early 2026 is paradoxically both overwhelming and surprisingly simple. The proliferation of frameworks (LangGraph, CrewAI, AutoGen, Semantic Kernel) and platforms (n8n, Lindy, Relevance AI) has created an illusion that building a personal agent requires sophisticated orchestration. It doesn't. For a single-user Telegram bot with well-defined integrations, **the Claude API's native tool calling plus Python's excellent SDK ecosystem eliminates the need for any framework at all**. The total cost of $5–10/month is roughly what you'd spend on a single coffee — and unlike the coffee, this system compounds in value every week as it learns your patterns and accumulates structured data in Notion. The only real risk is Monarch Money's unofficial API, which the community has proven resilient at maintaining. Start with Phase 1 Saturday morning; by Sunday evening, you'll have a working Jarvis.