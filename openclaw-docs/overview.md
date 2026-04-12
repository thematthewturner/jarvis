# OpenClaw — Overview

**Repo:** https://github.com/openclaw/openclaw
**Version:** 2026.2.12
**License:** MIT
**Mascot:** Molty the space lobster 🦞
**Created by:** Peter Steinberger + community

## What It Is
OpenClaw is a **personal AI assistant gateway** you self-host on your own devices (or a VPS/droplet).
- Single control plane (the Gateway) that routes messages from many channels to an AI agent.
- Talks back on the same channel the message came in on.
- Runs on macOS, Linux, Windows (WSL2).

## Core Concept
```
WhatsApp / Telegram / Slack / Discord / Signal / iMessage / Teams / Matrix / ...
               │
               ▼
     ┌─────────────────┐
     │    Gateway       │   ws://127.0.0.1:18789
     │  (control plane) │
     └────────┬─────────┘
              ├─ Pi agent (RPC)
              ├─ CLI (openclaw …)
              ├─ WebChat UI
              ├─ macOS app
              └─ iOS / Android nodes
```

## Recommended Model
- **Anthropic Pro/Max (100/200) + Claude Opus 4.6**
- Config: `agent: { model: "anthropic/claude-opus-4-6" }`
- Best for long-context + prompt-injection resistance

## Runtime Requirement
- Node ≥ 22
- Package manager: npm, pnpm, or bun (pnpm preferred for builds from source)
