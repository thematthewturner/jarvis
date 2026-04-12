# OpenClaw — Skills & Tools

## Skills
Skills extend the agent with new capabilities.

### Locations
| Type | Path |
|------|------|
| Workspace skills | `~/.openclaw/workspace/skills/<skill>/SKILL.md` |
| Bundled skills | included in OpenClaw install |
| Managed skills | installed via `openclaw skills` |

### ClawHub (Skills Registry)
With ClawHub enabled, the agent auto-searches and pulls in new skills as needed.
Docs: https://docs.openclaw.ai/tools/clawhub.md

### CLI
```bash
openclaw skills          # list/manage skills
```

## Built-in Tools
| Tool | Description |
|------|-------------|
| `browser` | OpenClaw-managed Chrome/Chromium with CDP control |
| `canvas` (A2UI) | Agent-driven visual workspace |
| `nodes` | Camera, screen record, location, notifications |
| `cron` | Scheduled tasks and wakeups |
| `webhooks` | External trigger surface |
| `exec` / `system.run` | Run shell commands on the gateway host |
| `sessions_list` | Discover active sessions/agents |
| `sessions_history` | Fetch transcript for a session |
| `sessions_send` | Message another session (agent-to-agent) |

## Agent-to-Agent (Sub-Agents)
```
sessions_list       → discover active agents
sessions_history    → read another agent's transcript
sessions_send       → send a message to another agent (with reply-back)
```
Details: https://docs.openclaw.ai/concepts/session-tool.md

## Elevated Mode
- Toggles elevated bash access per-session.
- Chat command: `/elevated on|off`
- Must be enabled + allowlisted in config.

## Thinking Levels
`/think off|minimal|low|medium|high|xhigh`
(Opus 4.6 / GPT-5+ models only)

## Workspace Prompt Files
| File | Injected as |
|------|------------|
| `AGENTS.md` | Agent instructions |
| `SOUL.md` | Personality |
| `TOOLS.md` | Tool usage guidance |
| `skills/<skill>/SKILL.md` | Per-skill instructions |
