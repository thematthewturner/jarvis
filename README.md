# Jarvis

Jarvis is a private personal assistant stack built around Telegram, a web dashboard, scheduled digests, and a set of homegrown operational skills.

## What It Does

- Runs a Telegram-based assistant with tool calling and scheduled jobs
- Serves a Flask web dashboard for operations, digests, inbox review, finance, and fitness
- Aggregates multiple daily signals into a single daily digest
- Connects to external systems like Notion, Todoist, Gmail, iCloud, Monarch, WHOOP, and pool/home services
- Tracks system health, preferences, inbox-zero queues, and digest quality through a shared ops layer

## Main Files

- `bot.py`: Telegram bot runtime, commands, and scheduler
- `web/app.py`: Flask web app
- `daily_digest.py`: unified digest assembly, caching, persistence, and notifications
- `jarvis_ops.py`: skills registry, preferences, source health, inbox zero, digest quality
- `brain.py`: conversational tool-routing assistant behavior
- `tools.py`: shared tool functions used by Jarvis
- `memory.py`: SQLite-backed persistence and initialization

## Skills

Feature areas are documented in [`JARVIS_SKILLS.md`](./JARVIS_SKILLS.md).

Current areas include:

- Daily Digest
- Inbox Triage
- Action Nudges
- Finance Console
- Finance Inbox
- Fitness
- Pool Ops
- Home Ops
- Intentionality
- Calendar and Mail
- Context Memory

## Local Setup

1. Create and activate a Python virtual environment.
2. Install the project dependencies you need for the bot and web stack.
3. Copy `.env.example` to `.env` and fill in your credentials.
4. Initialize any external integrations you want to use.
5. Start the bot with `python bot.py`.
6. Start the web app with your preferred Flask or Gunicorn entrypoint for `web/app.py`.

## Notes

- This repo is intentionally private and optimized for a single-owner workflow.
- Secrets, tokens, local databases, and credential JSON files are ignored by Git.
- Deployment details and internal planning docs are kept in the repo because this codebase is used as an operational system, not just a library.
