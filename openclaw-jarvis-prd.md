# PRD: Jarvis on OpenClaw on a Separate Droplet

## Executive Recommendation

Short answer:

- **Good idea** if you want OpenClaw to become a **multi-channel gateway, remote control plane, or experimental front end** for Jarvis.
- **Not a good idea** if you mean "spin up a second full Jarvis brain with the same automations, same side effects, and separate local state on another droplet."

Recommended decision:

- Keep the current Python Jarvis as the **system of record**.
- Use OpenClaw on a second droplet only as a **gateway/orchestration layer** or as a **staging/rescue environment**.
- Do **not** duplicate schedulers, SQLite state, or write-capable integrations across two independent deployments until you redesign for shared state and idempotent jobs.

## Why

Current Jarvis is already a specialized personal operations system, not just a chat bot.

Today it includes:

- A Claude-driven tool loop in [brain.py](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/brain.py)
- A Telegram runtime and scheduler in [bot.py](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/bot.py)
- Shared SQLite-backed state in [memory.py](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/memory.py)
- A large tool surface in [tools.py](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/tools.py)
- A web dashboard in [web/app.py](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/web/app.py)
- Domain systems for email triage, finance, research, investor workflow, fitness, pool ops, home maintenance, intentionality, and daily briefings

That matters because a second deployment can create:

- Split memory and conversation context
- Duplicate scheduled jobs
- Duplicate external writes
- Credential sprawl
- Hard-to-debug drift between droplets

This also aligns with OpenClaw's own deployment guidance: most setups should use one gateway, and additional gateways are mainly for isolation or a rescue bot, not for duplicate active ownership of the same assistant workflows.

## Product Goal

Create a better Jarvis deployment that can do more than the current Telegram-first setup, while preserving reliability, auditability, and low operational drag.

## Non-Goals

- Building a multi-tenant assistant for multiple adversarial users
- Running two active copies that both own the same automations
- Rewriting every existing Python service into OpenClaw-native logic on day one
- Chasing channel breadth before core reliability is stable

## Problem Statement

The current Jarvis implementation is effective but tightly coupled:

- Chat, scheduling, business logic, and storage live in one Python deployment
- State is local SQLite
- External integrations are mostly direct library calls and local token files
- Access control is strongly single-user

This makes the current system simple and practical, but it also limits:

- Multi-channel access
- Safer experimentation
- Clean staging vs production separation
- Reusable interfaces for future clients or assistants

## Current Feature Baseline

### Core assistant behavior

- Conversational assistant with tool calling
- Automatic task/note capture
- Read/write actions across personal systems

### Current channels

- Telegram bot
- Web dashboard/chat

### Current scheduled jobs

From [bot.py](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/bot.py):

- 5:00 AM ET daily research
- 6:45 AM ET morning briefing
- 7:00 AM ET inbox triage
- 7:15 AM ET Gmail triage
- 7:30 AM ET investor scan
- 7:35 AM ET action nudges
- 8:05 AM ET home maintenance sync
- 8:20 AM ET pool nudge checks
- 4:40 AM ET financial sync

### Current domain capabilities

- Gmail, Google Calendar, Google Drive
- iCloud Mail and iCloud Calendar
- Todoist
- Notion
- Monarch Money and utility bill extraction
- Fitness/WHOOP
- Pool chemistry and Leslie's/Hayward data
- Investor scanning and tracking
- Home maintenance workflows
- Intentionality and recurring life-rhythm tracking

## What OpenClaw Is Good For

OpenClaw is strongest when you want:

- Multiple messaging surfaces behind one gateway
- Built-in pairing and channel routing
- A workspace/skills model for agent behavior
- A browser/tooling runtime
- Remote control from other devices
- A cleaner "assistant gateway" abstraction than a custom bot process

For this project, that means OpenClaw is best treated as one of these:

1. **Gateway layer**
   One agent front door for Telegram, WebChat, maybe iMessage/WhatsApp later.

2. **Staging environment**
   A safe place to test prompts, skills, or channels without touching production Jarvis.

3. **Rescue bot / backup control plane**
   A second interface when the main runtime is unhealthy.

## What OpenClaw Is Bad For

OpenClaw is a poor fit if the plan is:

- "Copy the repo to another droplet and let both run"
- "Let both instances schedule briefings, nudges, and syncs"
- "Keep separate SQLite files and hope behavior stays aligned"
- "Port everything into prompt files and ignore the Python service layer"

That would increase complexity faster than it increases capability.

## Recommended Product Shape

### Recommended architecture: OpenClaw as front end, Jarvis Python as backend

**Target pattern**

- **Jarvis Core**
  Existing Python code owns integrations, state, and scheduled jobs.

- **Jarvis API/Command Layer**
  A thin interface exposes safe operations such as:
  - `briefing.run`
  - `research.run`
  - `tasks.list`
  - `email.triage`
  - `pool.status`
  - `investor.scan`
  - `home.status`

- **OpenClaw Gateway**
  Handles channels, session routing, and agent experience.

- **OpenClaw Skills**
  Skills call the Jarvis backend through:
  - authenticated HTTP endpoints
  - local CLI wrappers
  - SSH command execution
  - webhooks

### Why this is the best fit

- One place owns state
- One place owns scheduled side effects
- OpenClaw adds channel flexibility without forcing a rewrite
- You can migrate feature-by-feature instead of doing a risky full cutover

## Alternative Options

### Option A: Stay Direct Python Only

Best if you want:

- Lowest ops burden
- Telegram-first workflow
- Maximum control over behavior
- No channel expansion yet

Verdict:

- Still the best production choice if "better" mainly means reliability and polish

### Option B: Full OpenClaw Rewrite

Best if you want:

- OpenClaw-native skills, tools, and workspace conventions
- More generic agent behavior
- More multi-channel surface area

Risks:

- Long migration
- Reimplementation of all Python service logic
- Harder parity testing
- Easier to regress existing workflows

Verdict:

- Not the right first move

### Option C: Two Fully Independent Jarvis Deployments

Best if you want:

- Nothing, unless they are fully isolated and serve different purposes

Risks:

- Split-brain state
- Duplicate automation side effects
- Conflicting task/email/calendar actions
- Higher credential and observability burden

Verdict:

- Bad default architecture

## User Stories

### Personal operator

- As the single owner, I want to talk to Jarvis from more than Telegram without changing the actual business logic every time.
- As the operator, I want one trusted system of record for tasks, notes, syncs, and scheduled digests.
- As the operator, I want a safe staging environment for new prompts, skills, and tools.

### Reliability

- As the operator, I want scheduled automations to run exactly once.
- As the operator, I want write actions to be auditable and easy to disable.
- As the operator, I want failures in one interface not to corrupt core state.

## Functional Requirements

### Phase 1: Safe OpenClaw bridge

- OpenClaw runs on a separate droplet
- OpenClaw authenticates only approved users/channels
- OpenClaw can call read-only Jarvis backend actions
- OpenClaw exposes at least:
  - daily briefing
  - research digest
  - task list
  - calendar snapshot
  - inbox summary
  - pool status
  - investor status

### Phase 2: Controlled write actions

- Add explicit wrappers for:
  - add/update Todoist tasks
  - send/archive email
  - create calendar events
  - save Notion notes
  - log fitness/pool/home updates

- Every write path must support:
  - authentication
  - logging
  - idempotency key or duplicate protection
  - timeout and error reporting

### Phase 3: Channel expansion

- Add WebChat, Telegram, and optionally one new surface
- Keep the same Jarvis capability model across channels
- Preserve single-user trust boundary

### Phase 4: Optional migration of scheduler ownership

- Only after shared state and job locks are redesigned
- Only one system may own each recurring job
- Jobs must be observable and restart-safe

## Technical Requirements

### State

- Keep one primary database for production
- If SQLite remains the store, it should live only on the backend owner
- If you want active-active or failover later, move to Postgres first

### Scheduling

- Exactly one scheduler owns each recurring workflow
- Add a job registry and lock strategy before moving jobs around
- All jobs should emit logs and last-run status

### Security

- Separate secrets by environment: `prod`, `stage`, `openclaw`
- Do not share unrestricted write credentials across experimental assistants
- Use network-level restriction or signed requests between OpenClaw and Jarvis backend
- Keep single-user authorization assumptions explicit

### Observability

- Structured logs per command and per scheduled job
- Simple health endpoints
- A last-success/last-failure dashboard for automations
- Clear audit trail for external writes

## Success Metrics

- OpenClaw can answer 80%+ of daily read-only requests via backend bridge
- No duplicate scheduled deliveries over 30 days
- No conflicting external writes during pilot
- Median response time remains acceptable for chat surfaces
- Staging changes can be tested without touching production state

## Risks

### Highest-risk failure modes

- Duplicate automations creating duplicate tasks/emails/events
- Split SQLite state producing inconsistent answers
- Prompt-level wrappers bypassing business rules embedded in Python services
- New channel exposure expanding attack surface

### Mitigations

- One source of truth
- One owner per scheduled job
- Start read-only
- Add an action audit log before enabling writes
- Keep staging credentials separate from production credentials

## Proposed Rollout

### Step 1

Create `Jarvis Core` boundaries inside the current repo:

- identify read-only commands
- identify write commands
- identify scheduled jobs
- identify shared state tables

### Step 2

Expose a thin backend surface from the current Python app:

- authenticated Flask/FastAPI endpoints or CLI entry points
- one endpoint/command per stable capability

### Step 3

Stand up OpenClaw on the new droplet in staging mode:

- Telegram + WebChat only
- pairing/auth enabled
- no autonomous write tools yet

### Step 4

Implement OpenClaw skills that proxy to Jarvis Core:

- `briefing`
- `research`
- `tasks`
- `calendar`
- `inbox`
- `pool`
- `investor`
- `home`

### Step 5

Enable write actions one category at a time:

- Todoist first
- then Notion
- then calendar
- then email
- then specialty domains

### Step 6

Only consider moving automation ownership after the bridge model is stable.

## Feature Guide: What To Build First

### Tier 1: Highest leverage

- Multi-channel access
- Read-only status skills
- Better operational dashboard
- Health and failure visibility
- Safer staging environment

### Tier 2: Worth building after that

- Unified command vocabulary across channels
- Action audit log
- Approval mode for sensitive writes
- Per-domain skill boundaries

### Tier 3: Nice-to-have

- Browser-powered workflows
- Voice/device nodes
- Rescue bot profile
- Failover routing

## Final Recommendation

If your goal is:

- **"I want Jarvis to do more, from more places"**
  then **yes**, OpenClaw on a separate droplet is a good idea.

- **"I want to clone the whole current Jarvis and let both run"**
  then **no**, that is the wrong architecture.

The right move is:

- **OpenClaw for channels and orchestration**
- **Current Python Jarvis for stateful business logic**
- **Single production source of truth**
- **Staging/prod separation instead of duplicate active brains**

## References

- Local OpenClaw notes in [openclaw-docs/overview.md](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/openclaw-docs/overview.md)
- Local OpenClaw notes in [openclaw-docs/configuration.md](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/openclaw-docs/configuration.md)
- Local OpenClaw notes in [openclaw-docs/channels.md](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/openclaw-docs/channels.md)
- Current direct-deploy opinion in [jarvis-build-plan.md](/Users/toniturner/Library/Mobile%20Documents/com~apple~CloudDocs/projects/jarvis/jarvis-build-plan.md)
- Official OpenClaw docs: [Overview](https://docs.openclaw.ai/), [Chat Channels](https://docs.openclaw.ai/channels/index), [Security](https://docs.openclaw.ai/gateway/security), [Multiple Gateways](https://docs.openclaw.ai/gateway/multiple-gateways)
