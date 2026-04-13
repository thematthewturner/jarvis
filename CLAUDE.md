# Jarvis Project Rules

## Architecture
Direct Python stack — Claude API + python-telegram-bot + Flask + SQLite, deployed
to a single DigitalOcean droplet. No gateway layer, no agent framework, no
n8n/LangGraph/OpenClaw. The Python service is the system of record for tasks,
notes, finance, fitness, pool, and home ops.

## Model Choices
- **CLAUDE_MODEL_CHAT**: Stay on `claude-haiku-4-5-20251001` (cost). Do NOT upgrade to Sonnet without explicit user approval.
- **CLAUDE_MODEL_REPORT**: `claude-sonnet-4-6` (for scheduled reports/briefings only).

## Deployment
- Server: `138.197.70.215` (NYC3 droplet), user `jarvis`
- Project root: `/home/jarvis/jarvis/`
- Shared venv: `/home/jarvis/jarvis/.venv` (used by both bot and web)
- Web service: `jarvis-web` (gunicorn, port 8080)
- Bot service: `jarvis` (python bot.py)

## Development Workflow
- Edit locally at `/Users/toniturner/Library/Mobile Documents/com~apple~CloudDocs/projects/jarvis/`
- Deploy with `scp` then `systemctl restart jarvis` / `jarvis-web`
- Never push model changes, infra changes, or cost-impacting decisions without asking first

## Reliability Layer (`jarvis_reliability.py`)
- Every write tool is routed through `execute_and_log` in `tools.py:handle_tool_call`.
- When adding a new write tool, also add its name to `WRITE_TOOLS` in `jarvis_reliability.py`
  so it gets audit logging + idempotency dedupe. Tune its dedupe window in `DEDUPE_WINDOWS`
  if the default 10 min isn't right (outbound email/events should be longer; tight loops shorter).
- Scheduled jobs are tracked in `jarvis_job_runs` via `_run_scheduled` in `bot.py`. New
  scheduled runners must `raise` after sending their Telegram error message so the wrapper
  records the failure — do not silently swallow exceptions.
- Surfaces: `/api/ops/overview`, `/api/ops/jobs`, `/api/ops/audit`, and the ops cards on the web dashboard.

## Skills Discipline
New features should extend an existing skill in `JARVIS_SKILLS.md` rather than become
one-off scripts. Each skill needs a clear job, a visible health status, and a low-noise contract.
