# OpenClaw — Installation & Setup

## Quick Install (recommended)
```bash
npm install -g openclaw@latest
# or
pnpm add -g openclaw@latest

openclaw onboard --install-daemon
```
The wizard sets up: Gateway, workspace, channels, skills.
Installs a **launchd/systemd user service** so Gateway stays running.

## From Source (development)
```bash
git clone https://github.com/openclaw/openclaw.git
cd openclaw

pnpm install
pnpm ui:build        # installs UI deps on first run
pnpm build

pnpm openclaw onboard --install-daemon

# Dev loop (auto-reload on TS changes)
pnpm gateway:watch
```

## Running the Gateway
```bash
openclaw gateway --port 18789 --verbose
```

## Sending a Message
```bash
openclaw message send --to +1234567890 --message "Hello from OpenClaw"
```

## Running the Agent
```bash
openclaw agent --message "Ship checklist" --thinking high
```

## Release Channels
| Channel | Tag | Notes |
|---------|-----|-------|
| stable  | `latest` | Tagged releases `vYYYY.M.D` |
| beta    | `beta` | Prerelease tags |
| dev     | `dev` | Moving head of main |

Switch: `openclaw update --channel stable|beta|dev`

## Useful CLI Commands
| Command | Purpose |
|---------|---------|
| `openclaw onboard` | Setup wizard |
| `openclaw gateway` | Start the gateway |
| `openclaw agent` | Talk to the assistant |
| `openclaw doctor` | Health check / surface config issues |
| `openclaw pairing approve <channel> <code>` | Approve a new sender |
| `openclaw update --channel <name>` | Switch release channel |
| `openclaw channels login` | Link WhatsApp device |
