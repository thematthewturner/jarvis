# Jarvis Project Rules

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
