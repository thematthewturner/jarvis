# OpenClaw — Configuration

## Config File Location
`~/.openclaw/openclaw.json`

## Minimal Config
```json
{
  "agent": {
    "model": "anthropic/claude-opus-4-6"
  }
}
```

## Key Config Sections

### Agent / Model
```json
{
  "agent": {
    "model": "anthropic/claude-opus-4-6"
  },
  "agents": {
    "defaults": {
      "workspace": "~/.openclaw/workspace"
    }
  }
}
```

### Browser (optional)
```json
{
  "browser": {
    "enabled": true,
    "color": "#FF4500"
  }
}
```

### Sandbox Security (for group/channel sessions)
```json
{
  "agents": {
    "defaults": {
      "sandbox": {
        "mode": "non-main"
      }
    }
  }
}
```
- `non-main` → runs non-main sessions (groups/channels) in Docker sandboxes
- Sandbox allowlist: `bash, process, read, write, edit, sessions_*`
- Sandbox denylist: `browser, canvas, nodes, cron, discord, gateway`

### Tailscale Exposure
```json
{
  "gateway": {
    "tailscale": {
      "mode": "serve"  // "off" | "serve" | "funnel"
    }
  }
}
```
- `off` — no Tailscale (default)
- `serve` — tailnet-only HTTPS
- `funnel` — public HTTPS (requires `gateway.auth.mode: "password"`)

## Workspace Files
Located at `~/.openclaw/workspace/`:
| File | Purpose |
|------|---------|
| `AGENTS.md` | Injected agent prompt |
| `SOUL.md` | Personality/soul prompt |
| `TOOLS.md` | Tool instructions |
| `skills/<skill>/SKILL.md` | Per-skill instructions |

## Chat Commands (in any channel)
| Command | Effect |
|---------|--------|
| `/status` | Session status (model, tokens, cost) |
| `/new` or `/reset` | Reset the session |
| `/compact` | Compact context with summary |
| `/think <level>` | `off\|minimal\|low\|medium\|high\|xhigh` |
| `/verbose on\|off` | Toggle verbose mode |
| `/usage off\|tokens\|full` | Usage footer per response |
| `/restart` | Restart gateway (owner-only in groups) |
| `/activation mention\|always` | Group activation toggle |
| `/elevated on\|off` | Toggle elevated bash access (per-session) |
