# Jarvis personal agent architecture, tool choices, and ideal build approach

## Decision and rationale

Build Jarvis as an **integrations-first automation system** (deterministic workflows + strong data model) with an **LLM used narrowly for parsing and summarization**, and keep the chat interface **Telegram-first via long polling** to satisfy “no public web exposure.” In practice, that means: a lightweight Telegram bot (inbound), a self-hosted workflow/orchestration layer, and Notion as the long-term system of record—while explicitly avoiding “agentic computer control” patterns that tend to be fragile and risky for a personal always-on system. citeturn0search4turn7view2turn5view0turn1search1

This differs from “build it as an autonomous agent with lots of third-party skills.” Recent OpenClaw ecosystem events show why: the “skills marketplace” model has already been exploited by malware campaigns, and agent configs commonly contain high-value tokens (Telegram, calendar, etc.), making “personal agent configs” an attractive target for infostealers. citeturn11view0turn11view1

The highest-leverage design choice is to **treat finance, tasks, and calendar as data pipelines with clear schemas and idempotent writes**, not as free-form agent behavior. OpenAI-style **Structured Outputs** + **function calling** are explicitly designed to make “LLM → schema” extraction reliable (schema adherence vs “valid JSON only”), which fits your “stable, low-maintenance” requirement. citeturn3search1turn3search5

Where your requirements are *internally inconsistent*, the biggest one is **“US-hosted services only” while using Telegram**: Telegram’s own API documentation states its servers are divided into multiple data centers in different parts of the world, so you can’t strictly guarantee “US-only” storage/processing for Telegram traffic. citeturn12search15turn6search2

## Why OpenClaw is not the best default for this use case

OpenClaw is positioned as a **self-hosted multi-channel gateway** (WhatsApp/Telegram/Discord/iMessage) built to connect chat apps to “agent-native” tool-using assistants, with a Web control UI and session/routing features. That’s powerful, but it’s also **more surface area than you need for Telegram-only**, especially under a “no public exposure” and “minimal manual maintenance” mandate. citeturn5view1turn5view2

OpenClaw does have design choices that align with your threat model: its Gateway won’t start unless configured for local mode, and “binding beyond loopback without auth is blocked (safety guardrail)” in the CLI docs—directly matching your “gateway bound to loopback” constraint. citeturn5view2

The downside is current ecosystem risk and operational risk:

- **Supply-chain / marketplace risk:** malware has been reported in hundreds of user-submitted OpenClaw “skills,” with researchers describing the skill hub as an attack surface and documenting waves of malicious skills masquerading as productivity/crypto tools. citeturn11view0turn0news42  
- **Credential-exfiltration risk:** infostealer malware has been observed exfiltrating OpenClaw configuration environments, and those configs can contain tokens/API keys that grant access to linked apps (Telegram, calendars, etc.). citeturn11view1  
- **Agentic unpredictability risk:** enterprise security leaders have publicly described agentic “do things on your machine” tools like OpenClaw as unvetted/high-risk and vulnerable to prompt injection or malicious content that tricks the agent into leaking data. citeturn11view2  

**Bottom line:** If you keep OpenClaw, treat it as a **dumb message gateway only** (no third-party skills, no “computer use,” no broad shell/file permissions). Your “Jarvis” value comes from clean schemas + scheduled ETL + concise reports, not from “agent autonomy.” citeturn11view0turn2search23

## Off-the-shelf stack that fits your constraints

The best “off-the-shelf” move is to adopt a workflow engine for schedules, retries, and integrations—**but only if it can run fully self-hosted and without public inbound webhooks**.

A strong fit is **entity["company","n8n","workflow automation"] self-hosted**, because it already provides off-the-shelf connectors (Notion, Todoist, Google Calendar, etc.), credential handling guidance, and explicit self-hosting security guidance. citeturn2search19turn2search11turn0search13turn0search17turn0search21  
However, n8n’s **Telegram Trigger** is operationally a webhook integration in typical deployments: n8n documentation for common issues references registering a single webhook, requiring an HTTPS public URL, and configuring `WEBHOOK_URL`—which conflicts with “no public web exposure.” citeturn7view2turn0search28

So the off-the-shelf pattern that actually matches your constraints is:

- **Custom inbound Telegram bot using long polling (`getUpdates`)** (no inbound ports). Telegram explicitly documents `getUpdates` vs webhooks as mutually exclusive, and updates are retained up to 24 hours server-side—important for reliability planning. citeturn0search4  
- **Self-hosted workflow engine (n8n) for schedules + API actions** (outbound only), reachable only on loopback; you access the UI via SSH port forwarding. citeturn5view3turn2search23  
- **LLM API selected for US processing + controllable retention**; if you use OpenAI, OpenAI’s “data controls” doc shows US regional domain prefix requirements and explains how Zero Data Retention / “store=false” interacts with server-side retention. citeturn5view0turn4search25turn3search1  

This approach also avoids a key OpenAI caveat: OpenAI documentation notes that using MCP servers can hand customer content to third-party services with their own retention policies—so do not use “remote MCP server” tools if you’re serious about privacy constraints. citeturn5view0

## Ideal reference architecture

**Core principle:** Jarvis should be a **data product** (schemas, idempotency, provenance, audit trail) with a chat UI—not a chat agent that “figures it out live.”

**Components**

- **Compute**: one always-on droplet on entity["company","DigitalOcean","cloud hosting provider"] in a US region (Ubuntu LTS). (This is your single trust boundary.) citeturn5view2turn2search23  
- **Inbound interface**: a Telegram bot using long polling (`getUpdates`) so there is no public HTTP listener. citeturn0search4turn6search2  
- **Orchestration**: self-hosted n8n (loopback bind) to run schedules, retries, and integrations; no public webhooks. n8n’s own docs explicitly warn self-hosting mistakes can cause security issues/downtime, so treat it like production software (health checks, backups). citeturn5view3turn2search23  
- **State & idempotency**: Postgres (local) for operational state: job runs, last successful poll, idempotency keys, message dedupe, retry queues.  
- **System of record / long-term memory**: entity["company","Notion","productivity software"] databases only, append-only records. Notion’s API authenticates via bearer tokens (integration token or OAuth). citeturn1search1turn1search13  
- **Tasks**: entity["company","Todoist","task management app"] via API token (simple) or OAuth (more complex). Todoist’s developer docs describe bearer-token auth, and their help center documents how users retrieve API tokens. citeturn1search6turn1search10  
- **Email ingestion**: Gmail API on a dedicated automation inbox, using minimal scopes; Google documents that scopes define access level and that “sensitive scopes” require additional review in some contexts. citeturn1search3turn1search7  
- **LLM layer**: entity["company","OpenAI","ai research company"] Responses API with Structured Outputs + function calling. OpenAI documentation recommends Structured Outputs over JSON mode for schema adherence. citeturn3search1turn3search5turn3search20  

**Network posture (meets “no public web exposure”)**

- No inbound HTTP/HTTPS at all.
- Outbound only to Telegram API + Notion API + Todoist API + Google APIs + LLM API endpoints.
- n8n UI bound to 127.0.0.1; accessed through SSH port forward.
- If you keep OpenClaw, its Gateway already enforces “don’t bind beyond loopback without auth,” which is consistent with this posture. citeturn5view2  

**Data residency reality check (your stated constraint)**

- Notion: Notion’s data residency help page states that by default data resides in the United States, with an EU migration option for some orgs. citeturn12search0  
- Todoist: Todoist’s security/compliance article states they process data in North Virginia, USA (AWS). Doist’s subprocessors list shows AWS hosting/data processing in the United States. citeturn14search0turn14search1  
- Monarch: Monarch’s help center states data is stored in US-based AWS data centers. citeturn12search2  
- Telegram: multiple global data centers (cannot guarantee US-only). citeturn12search15  
- n8n Cloud: explicitly hosted in Frankfurt, Germany; therefore your constraint implies self-hosting, not n8n Cloud. citeturn2search11  

## Skill implementation blueprint

Your skills should be implemented as **modular “pipelines”** with: (a) a deterministic fetch step, (b) parse/normalize step, (c) validate step, (d) write step (idempotent), (e) summarize step. The LLM should only touch (b) and (e)—never the fetch or write primitives.

**Financial Intelligence: the hard truth and the ideal design**

There’s a major hidden blocker: **programmatically pulling Monarch transaction data is not just unstable—it can violate Monarch’s Terms.** Monarch’s Terms of Use prohibit crawling/scraping/spidering pages/data and also prohibit programmatic access via automated means. citeturn10view0

That makes the “best” approach look like two lanes:

- **Lane A: compliant + stable (recommended)**  
  - Bills & recurring charges: ingest from Gmail (PDFs/HTML emails) and normalize into a Notion “Bills” database. (Email ingestion was already your preference.) citeturn1search3turn1search7turn1search1  
  - Transactions: do not scrape Monarch. Instead, either  
    - use bank notifications/statement emails (less complete), or  
    - integrate a finance data provider with a developer API (more complete; more setup). Monarch itself states it works with data providers including Plaid, Finicity, and MX for account data aggregation, but that doesn’t give you an official Monarch API for your own automation. citeturn12search10turn10view0  
  - Month-end “Monarch export” (manual): Monarch supports CSV downloads for transactions and balances (last updated Oct 19, 2025). Jarvis can process the CSV once you drop it into the automation inbox or upload path, but the export click remains manual. citeturn9view0  

- **Lane B: higher automation, higher risk (only if you knowingly accept ToS + breakage risk)**  
  - Unofficial Monarch APIs exist (Python/JS libraries and MCP wrappers), and their authors explicitly warn they are unofficial and may break at any time. citeturn8search17turn0search3turn8search15  
  - There is evidence of churn/breakage (e.g., domain changes breaking integrations). citeturn0search31turn0search7  
  - Some MCP servers require storing Monarch credentials and even MFA secrets in environment variables—directly conflicting with your “never store banking passwords / avoid credential risk” posture (and still likely ToS-problematic for Monarch). citeturn8search14turn10view0  

**If you want “off-the-shelf” transaction aggregation without Monarch scraping:** entity["company","Plaid","financial data network"] is the most common developer path, with JSON-over-HTTPS APIs and explicit documentation around OAuth-based bank connections; Plaid documents that OAuth support is required for institutions that require OAuth (many major US banks), which fits your “use OAuth where possible” requirement. citeturn15search4turn15search1turn15search24  
This is not “two hours to value,” but it is the cleanest way to get reliable transaction feeds without scraping a consumer app UI. citeturn15search1turn15search4

**Task & Life Operations**

Todoist is straightforward: tasks are created/queried via bearer token (REST). This is one of the simplest parts of the system and should be implemented without an “agent”—just a command parser + API client. citeturn1search6turn1search10

**Calendar Awareness**

Calendar briefings should be computed deterministically:

- Fetch “today” and “next 7 days” events.
- Compute travel time alerts and conflicts in code (rules engine).
- Use the LLM only to produce a ~6–10 sentence executive narrative after the facts are fixed.

For Gmail and Calendar scopes, keep scopes minimal; Google documents scope-based access levels and flags “sensitive scopes” for additional review in some cases. citeturn1search3turn1search7

**Passive Capture (free-form → structured Notion rows)**

Make this a dedicated “ingest + confirm” pipeline:

- Deterministic pre-parser: regex for `$`, dates, keywords (bill, due, shot, played, etc.).
- LLM fallback using Structured Outputs with a schema like `{type, date, amount, vendor, notes, confidence}`. OpenAI documents that Structured Outputs is the evolution of JSON mode and ensures schema adherence. citeturn3search1
- Human confirmation: if confidence < threshold, ask a single clarifying question in Telegram and don’t write yet.

**Weekly Executive Brief**

Treat the Sunday report as a **rendered artifact** written into Notion “Weekly Reports” (append-only), then pushed via Telegram:

- Inputs: last 7 days bills created/updated, tasks due/overdue, next-week calendar conflicts, anomalies.
- Outputs: fixed section headers, max 5 actions, with explicit references (bill IDs, task URLs, calendar event IDs).
- LLM used only after the underlying metrics are computed.

## Security and privacy hardening

**Telegram is convenient, not “private messenger secure,” especially with bots**

Telegram bot chats are not end-to-end encrypted; multiple security writeups warn that bot interactions downgrade security guarantees compared with “Secret Chats,” and bot traffic relies on TLS rather than Telegram’s end-user E2E model. citeturn1news40turn1search4turn1search12  
Practical implication: **do not send raw account numbers, statements, or anything you’d regret leaking** through the bot. Treat Telegram as: commands + alerts + short summaries; keep sensitive detail in Notion. citeturn1news40turn12search12

**Secrets and credential storage**

- n8n recommends using OAuth where possible and documents credential handling expectations, plus self-hosted security steps (TLS, encrypted partitions, audits) and warns about community node risks. citeturn2search11turn2search23  
- If you keep OpenClaw: infostealer reports specifically highlight agent configuration files as a high-value artifact; therefore, ensure config directories are root-owned, not world-readable, and protected by OS hardening. citeturn11view1  
- Avoid off-the-shelf “community nodes” and “skills” registries unless you are prepared to audit code: both n8n and OpenClaw ecosystems have explicit warnings about third-party extension risk. citeturn2search23turn11view0  

**LLM data retention / region controls**

If you use OpenAI:

- OpenAI’s “data controls” doc shows US regional domain prefixes and eligibility; US has regional storage + processing in that table, and the US prefix is listed as “required.” citeturn5view0  
- OpenAI documents that responses can be stored for 30 days by default unless `store=false`, and that conversation objects can persist without the 30-day TTL—so for your privacy constraint you should avoid server-side conversations/threads and set `store=false`. citeturn4search25turn5view0  

**Data model protections**

- Notion bearer tokens are powerful; Notion’s docs show integrations use bearer tokens in the Authorization header, so token leakage is equivalent to workspace compromise for whatever pages/databases the integration can access. citeturn1search1turn1search13  
- Todoist tokens similarly authorize REST API access. citeturn1search6turn1search10  

So your “least privilege” implementation is:

- Create separate Notion integration + dedicated “Jarvis” workspace area; share only the required databases with the integration token.
- Use a dedicated Todoist project namespace; token scope limited to personal workspace.
- Gmail: prefer label-routing via Gmail filters so the bot uses read-only access to a narrow subset of messages (minimize need for modify scopes). citeturn1search3turn1search7  

## Operating model and cost controls

**Reliability model**

Telegram update retention is only 24 hours on Telegram servers (per Bot API docs). That means if your bot is down for >24 hours, you may permanently miss inbound commands. citeturn0search4  
So design for:

- systemd restart policies (bot + orchestrator)
- a tiny “watchdog” heartbeat job that sends a daily “alive” ping to yourself
- idempotent writes everywhere (email message-id, bill-id, etc.)
- retries with backoff for external APIs (Google/Notion/Todoist)

**Cost strategy (LLM)**

OpenAI’s published API pricing (as shown on their pricing pages) supports a clear tiering strategy: use smaller/cheaper models for extraction/classification, bigger models only for weekly/monthly synthesis. citeturn4search2turn4search6

A pragmatic tier aligned to your requirements:

- **Default (high volume)**: extraction + routing + short replies on a low-cost model (e.g., “nano/mini” class). citeturn4search6  
- **Weekly report**: upgrade model for better narrative + action selection (flagship class). citeturn4search2turn4search6  
- **Monthly finance analysis**: flagship class, but keep prompts compact by computing aggregates in code first, then asking for interpretation.

If you want an explicit number to anchor budgeting: OpenAI’s pricing page lists input/output token costs per million tokens by model family (as of the pricing page snapshot used here). citeturn4search6  
Because your architecture computes metrics in code and uses the LLM mainly to **render** summaries, you can keep weekly/monthly token volume low and cost predictable.

## Risks and failure modes

**Risk: Monarch automation can be ToS-violating and brittle**  
Monarch’s Terms explicitly prohibit crawling/scraping and programmatic/automated access, meaning “Jarvis pulls Monarch transactions weekly using unofficial APIs” is a potential account-termination risk. citeturn10view0  
Mitigation: treat Monarch as a human-facing app; build Jarvis finance pipelines from email bills + compliant transaction sources (or accept that Monarch CSV export is manual). citeturn9view0turn10view0

**Risk: Telegram bot channel is not end-to-end encrypted**  
Bots downgrade privacy guarantees; assume summaries could be exposed if Telegram is compelled or compromised. citeturn1news40turn1search12turn12search15  
Mitigation: minimize sensitive content in Telegram; keep detailed records in Notion; use Telegram for alerts + pointers.

**Risk: Extension ecosystems are an attack surface (OpenClaw skills, n8n community nodes)**  
Malware has already been reported in OpenClaw skills; n8n explicitly cautions about installing community nodes. citeturn11view0turn2search23  
Mitigation: no third-party skills/nodes unless audited; pin versions; isolate runtime; monitor outbound connections.

**Risk: OAuth scope creep (especially Gmail)**  
Google documents scopes as permission boundaries; requesting broad scopes increases blast radius and can create operational friction. citeturn1search3turn1search7  
Mitigation: use Gmail filters/labels + read-only; never request “full mailbox” unless absolutely necessary.

**Risk: LLM hallucination causing bad writes**  
Mitigation: compute facts in code; use Structured Outputs to force schema adherence; validate before writing; require confirmation on low-confidence parses. citeturn3search1turn3search5

**Risk: “US-hosted only” cannot be perfectly enforced end-to-end**  
Telegram is inherently multi-DC worldwide. citeturn12search15  
Mitigation: enforce US hosting where you can (your server, Notion default US residency, Todoist processing in North Virginia, Monarch US-based storage), and explicitly classify Telegram as an unavoidable exception in your threat model documentation. citeturn12search0turn14search0turn12search2turn12search15