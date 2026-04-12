# OpenClaw — Security

## Key Principle
OpenClaw connects to **real messaging surfaces** — treat all inbound DMs as untrusted input.

Full guide: https://docs.openclaw.ai/gateway/security/index.md

## DM Pairing (default)
- `dmPolicy: "pairing"` — unknown senders get a code; bot ignores their message until approved.
- Approve: `openclaw pairing approve <channel> <code>`
- Public inbound: requires `dmPolicy: "open"` AND `"*"` in allowFrom.

## Sandboxing
For group/multi-user sessions, run non-main sessions in Docker:
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
- Sandbox **allowlist**: `bash, process, read, write, edit, sessions_list, sessions_history, sessions_send, sessions_spawn`
- Sandbox **denylist**: `browser, canvas, nodes, cron, discord, gateway`

## Gateway Auth Modes
| Mode | Description |
|------|-------------|
| `password` | Shared password (required for Funnel) |
| Tailscale identity | Default for `serve` mode |

## Tailscale Exposure
```json
{
  "gateway": {
    "tailscale": { "mode": "serve" }
  }
}
```
- `off` — loopback only (default)
- `serve` — tailnet-only HTTPS
- `funnel` — public HTTPS (needs `auth.mode: "password"`)

## Elevated Bash
- Off by default — opt-in per session with `/elevated on`
- Must be enabled + allowlisted in config
- See: https://docs.openclaw.ai/gateway/sandbox-vs-tool-policy-vs-elevated.md

## Health Check
```bash
openclaw doctor   # surfaces risky configs, DM policy issues, etc.
```
