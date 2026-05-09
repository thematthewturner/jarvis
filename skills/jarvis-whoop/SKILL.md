---
name: jarvis-whoop
description: Monitor Matt's WHOOP data through the WHOOP API, analyze recovery, sleep, strain, HRV, RHR, and rolling trends, and turn that into source-backed daily recovery guidance.
version: 1.0.0
metadata:
  hermes:
    category: personal-health
---

# Jarvis WHOOP

Use this skill when Matt asks about WHOOP, recovery, HRV, RHR, sleep, strain, workouts, readiness, or daily recovery guidance.

## Data Source

Use `/opt/data/jarvis/whoop_monitor.py`.

Common commands:

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/whoop_monitor.py analyze --days 90 --format markdown
/opt/hermes/.venv/bin/python /opt/data/jarvis/whoop_monitor.py analyze --days 90 --format json
/opt/hermes/.venv/bin/python /opt/data/jarvis/whoop_monitor.py analyze --days 90 --format telegram
```

## Auth Setup

WHOOP uses OAuth 2.0. Access tokens are short-lived; request `offline` scope so a refresh token is available.

```bash
/opt/hermes/.venv/bin/python /opt/data/jarvis/whoop_monitor.py auth-url
/opt/hermes/.venv/bin/python /opt/data/jarvis/whoop_monitor.py exchange-code CODE_FROM_CALLBACK
```

Secrets and tokens must stay out of git:

- `WHOOP_CLIENT_ID`
- `WHOOP_CLIENT_SECRET`
- `WHOOP_REDIRECT_URI`
- `/opt/data/secrets/whoop_token.json`

## Analysis Rules

- Treat WHOOP data as coaching context, not medical advice.
- Separate observations from recommendations.
- Prioritize rolling trends over one-day noise.
- Always check rolling 7-day, 30-day, 60-day, and 90-day patterns when enough history is available.
- Watch for:
  - red recovery streaks
  - yellow/red streaks
  - HRV below 30-day baseline
  - RHR above 30-day baseline
  - low sleep performance or efficiency
  - high strain stacked on low recovery
- Translate metrics into practical guidance:
  - red 2+ days: recovery focus, easy movement, early sleep
  - yellow/red 3+ days: reduce training load
  - green: planned strain is acceptable if calendar and sleep allow

## Daily Brief Voice

Keep it direct and personal:

```text
WHOOP: RED 28% | HRV 31 ms | RHR 64 | strain 14.2
You've been red 2 days. We need to focus on recovery today: hydration, Zone 1/2 only, and protect sleep tonight.
```

Never overstate certainty. Avoid diagnosis. If data is missing or unscored, say that plainly.
