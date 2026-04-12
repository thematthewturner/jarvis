# OpenClaw — Channels

## Supported Channels
| Channel | Notes |
|---------|-------|
| WhatsApp | via Baileys |
| Telegram | via grammY |
| Slack | via Bolt |
| Discord | via discord.js |
| Google Chat | via Chat API |
| Signal | via signal-cli |
| iMessage (BlueBubbles) | **Recommended** iMessage integration |
| iMessage (legacy) | macOS-only via imsg |
| Microsoft Teams | via Bot Framework extension |
| Matrix | extension |
| Zalo | extension |
| Zalo Personal | extension |
| WebChat | built into Gateway (no extra config) |
| IRC | extension |
| LINE | extension |
| Mattermost | extension |
| Feishu | extension |

## DM Security (default: pairing mode)
- Unknown senders get a pairing code — bot does NOT process their message until approved.
- Approve: `openclaw pairing approve <channel> <code>`
- To open to all: set `dmPolicy: "open"` and include `"*"` in `allowFrom`.
- Run `openclaw doctor` to surface risky DM policies.

## Telegram Setup
```json
{
  "channels": {
    "telegram": {
      "botToken": "123456:ABCDEF"
    }
  }
}
```
Or set env: `TELEGRAM_BOT_TOKEN`

## Discord Setup
```json
{
  "channels": {
    "discord": {
      "token": "1234abcd"
    }
  }
}
```
Or set env: `DISCORD_BOT_TOKEN`

## WhatsApp Setup
```bash
openclaw channels login   # links device, stores creds in ~/.openclaw/credentials
```
Then set `channels.whatsapp.allowFrom` to control who can message.

## Slack Setup
Set `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN`
(or `channels.slack.botToken` + `channels.slack.appToken`)

## BlueBubbles (iMessage — Recommended)
```json
{
  "channels": {
    "bluebubbles": {
      "serverUrl": "http://your-mac:1234",
      "password": "yourpassword",
      "webhookPath": "/bluebubbles"
    }
  }
}
```
BlueBubbles server runs on macOS; Gateway can run anywhere.

## WebChat
- No separate config — uses the Gateway WebSocket.
- Access via: `openclaw dashboard` → opens `http://127.0.0.1:18789/`
