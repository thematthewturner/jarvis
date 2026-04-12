# Jarvis Skills

Jarvis is organized around a small set of durable skills instead of a pile of one-off scripts.

## Skill Model

Each skill should have:

- A clear name
- A single job to be done well
- Primary commands or triggers
- The surfaces it lives on: Telegram, web, Notion, Todoist, Gmail, iCloud, etc.
- Its owning modules
- A low-noise contract: what it should do automatically vs what it should avoid

## Current Skills

### Daily Digest

- Purpose: Assemble one morning digest from briefing, kids/school email, and research.
- Commands: `/daily_digest`, `/briefing`, `/research`, `/kids_digest`
- Surfaces: Telegram, web, Notion
- Modules: `daily_digest.py`, `briefing.py`, `daily_research.py`, `kids_digest.py`

### Inbox Triage

- Purpose: Clean iCloud and Gmail inboxes by starring real action items and archiving low-value mail.
- Commands: `/triage`, `/triage_gmail`
- Surfaces: Telegram, web, Gmail, iCloud
- Modules: `inbox_triage.py`, `google_services.py`, `icloud_mail.py`

### Action Nudges

- Purpose: Convert email and intentionality signals into a controlled Todoist nudge stream.
- Commands: `/action_nudges`, `/nudges`
- Surfaces: Telegram, Todoist, web
- Modules: `nudges.py`, `intentionality.py`

### Finance Console

- Purpose: Keep Monarch data warm locally and summarize spending, trends, and weekly financial signal.
- Commands: `/finance_sync`, `/finance_digest`, `/finance_weekly`, `/finance_trend`
- Surfaces: Telegram, web, Notion
- Modules: `financial_db.py`, `finance_weekly_digest.py`, `monarch_services.py`

### Finance Inbox

- Purpose: Process finance documents from Drive into an open review queue.
- Commands: `/finance_inbox`, `/finance_open`, `/finance_done`
- Surfaces: Telegram, web, Google Drive, Notion
- Modules: `finance_inbox.py`, `google_services.py`

### Investor

- Purpose: Run the paper-trade scanner and manage the daily play workflow.
- Commands: `/investor`
- Surfaces: Telegram, web
- Modules: `investor_bot.py`

### Fitness

- Purpose: Sync WHOOP readiness and track PRs and workouts.
- Commands: chat tools, WHOOP auth flow
- Surfaces: Telegram, web, WHOOP
- Modules: `fitness_services.py`

### Pool Ops

- Purpose: Track chemistry, OmniLogic status, Leslie's data, and maintenance prompts.
- Commands: `/pool`, `/pool_nudge`
- Surfaces: Telegram, web
- Modules: `pool_services.py`, `leslies_services.py`, `nudges.py`

### Home Ops

- Purpose: Manage recurring household maintenance and queue due items to Todoist.
- Commands: `/home`
- Surfaces: Telegram, web, Todoist
- Modules: `home_ops.py`

### Intentionality

- Purpose: Keep long-lived personal themes, sources, and rhythms alive across digests and nudges.
- Commands: `/intentionality`, `/i`
- Surfaces: Telegram, Todoist, Notion
- Modules: `intentionality.py`

### Calendar and Mail

- Purpose: Read and act across Gmail, iCloud Mail, Google Calendar, and iCloud Calendar.
- Commands: `/family_calendar`, chat tool calls
- Surfaces: Telegram, web, Gmail, iCloud, Google Calendar
- Modules: `google_services.py`, `icloud_calendar.py`, `icloud_mail.py`, `tools.py`

### Context Memory

- Purpose: Preserve recent conversation, notes, and personal profile context.
- Commands: chat, web context view
- Surfaces: Telegram, web
- Modules: `memory.py`, `brain.py`, `tools.py`

## New Ops Layer

Jarvis now has an explicit ops layer for:

- Skills registry
- Preferences
- Source health
- Inbox zero review
- Digest quality scorecard
- Digest cache and freshness policy
- **Action audit log** — every write tool (add_task, send_email, log_pool_chemistry, …) records status, args, result, duration, and errors.
- **Idempotency guard** — identical write calls inside a short per-tool window are deduped automatically before hitting external services.
- **Job registry** — scheduled jobs record start/finish, duration, last success, and last error. Surfaced on the web dashboard.

Primary modules:

- `jarvis_ops.py`
- `jarvis_reliability.py`
- `daily_digest.py`
- `web/app.py`

## Reliability Endpoints

- `GET /api/ops/overview` — rolled-up skills, health, jobs, audit totals
- `GET /api/ops/jobs` — per-job last-run status and recent history
- `GET /api/ops/audit?tool=<tool>&hours=<n>&limit=<n>` — recent write-tool activity with rollup

## Preferences

Current preferences:

- `digest_enabled_sections`
- `digest_notification_verbosity`
- `digest_weekend_mode`
- `digest_cache_window_minutes`
- `todoist_nudge_daily_limit`
- `todoist_nudge_mode`

## Design Direction

The next rule for Jarvis features should be:

1. Add or extend a skill, not a random script path.
2. Give the skill a visible health status.
3. Make the skill discoverable in the web app and in this document.
4. Prefer one durable queue per domain over many narrow automations.
