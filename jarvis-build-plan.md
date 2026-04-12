# JARVIS Build Plan — Direct Approach

**Kill OpenClaw. Go direct. Claude API + Python + Telegram + DigitalOcean.**

---

## Stack Summary

| Layer | Choice | Why |
|-------|--------|-----|
| **Compute** | DigitalOcean Droplet ($6/mo, 1GB, Ubuntu 24.04, NYC region) | Simple, US-hosted, no Oracle provisioning lottery |
| **LLM** | Claude API via `anthropic` SDK | Tool Runner handles orchestration loop; Haiku 4.5 for chat ($1/$5 per M tokens), Sonnet 4.5 for reports |
| **Bot Framework** | `python-telegram-bot` v21+ (long polling) | No public ports, no webhooks, battle-tested (28K GitHub stars) |
| **Dev Environment** | VSCode + Claude Code extension | Code locally, deploy via SSH |
| **Chat Interface** | Telegram | Always with you, push notifications, media support |
| **State** | SQLite via `aiosqlite` | Zero config, single-file, good enough for single-user |
| **Scheduler** | APScheduler (bundled with PTB) | Morning briefings, weekly reports, heartbeat |

**Monthly cost: ~$11** ($6 droplet + ~$5 Claude API at 75 msgs/day)

---

## Phase 0: Infrastructure (30 min)

### 0A. Create DigitalOcean Droplet

```
Region:         NYC1 (or NYC3)
Image:          Ubuntu 24.04 LTS
Size:           Basic $6/mo (1 vCPU, 1GB RAM, 25GB SSD)
Auth:           SSH key (add your public key)
Hostname:       jarvis
```

### 0B. Initial Server Setup

```bash
# SSH in
ssh root@YOUR_DROPLET_IP

# Create non-root user
adduser jarvis
usermod -aG sudo jarvis
cp -r ~/.ssh /home/jarvis/.ssh
chown -R jarvis:jarvis /home/jarvis/.ssh

# Firewall — block ALL inbound except SSH
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw enable

# Switch to jarvis user for everything else
su - jarvis
```

### 0C. Install Python & Dependencies

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv python3-pip git

# Create project
mkdir ~/jarvis && cd ~/jarvis
python3.12 -m venv .venv
source .venv/bin/activate

# Core dependencies
pip install \
  python-telegram-bot[job-queue]==21.11 \
  anthropic \
  aiosqlite \
  python-dotenv
```

### 0D. Create Telegram Bot

1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Name: `Jarvis` (or whatever you want)
4. Username: `your_jarvis_bot` (must be unique, end in `bot`)
5. Save the **bot token**
6. Send `/setprivacy` → select your bot → `Disable` (so it can read group messages if needed later)
7. **Get your Telegram user ID**: message @userinfobot or @raw_data_bot — save the numeric ID

### 0E. Get Claude API Key

1. Go to https://console.anthropic.com/
2. Create a new API key
3. Save it

### 0F. Create Environment File

```bash
# On the droplet
nano ~/jarvis/.env
```

```env
TELEGRAM_BOT_TOKEN=your_bot_token_here
TELEGRAM_AUTHORIZED_USER_ID=your_numeric_id_here
ANTHROPIC_API_KEY=sk-ant-xxxxx
CLAUDE_MODEL_CHAT=claude-haiku-4-5-20251001
CLAUDE_MODEL_REPORT=claude-sonnet-4-5-20250929
```

```bash
chmod 600 ~/jarvis/.env
```

---

## Phase 1: Core Bot + Claude (2 hours)

### Project Structure

```
~/jarvis/
├── .env
├── bot.py              # Telegram bot entry point
├── brain.py            # Claude API wrapper + tool definitions
├── tools.py            # Tool implementations
├── memory.py           # SQLite conversation memory
├── config.py           # Config loader
└── data/
    └── jarvis.db       # SQLite database (auto-created)
```

### config.py

```python
import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("TELEGRAM_AUTHORIZED_USER_ID"))
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL_CHAT = os.getenv("CLAUDE_MODEL_CHAT", "claude-haiku-4-5-20251001")
CLAUDE_MODEL_REPORT = os.getenv("CLAUDE_MODEL_REPORT", "claude-sonnet-4-5-20250929")
```

### memory.py

```python
import aiosqlite
import json
from datetime import datetime

DB_PATH = "data/jarvis.db"

async def init_db():
    """Create tables if they don't exist."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,          -- 'user' or 'assistant'
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,      -- 'task', 'note', 'expense', 'idea'
                content TEXT NOT NULL,
                metadata TEXT,               -- JSON blob for structured data
                timestamp TEXT NOT NULL
            )
        """)
        await db.commit()

async def save_message(role: str, content: str):
    """Store a conversation message."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO messages (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, datetime.utcnow().isoformat())
        )
        await db.commit()

async def get_recent_messages(limit: int = 20) -> list[dict]:
    """Retrieve last N messages for context window."""
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT role, content FROM messages ORDER BY id DESC LIMIT ?",
            (limit,)
        )
        rows = await cursor.fetchall()
        # Return in chronological order
        return [{"role": r[0], "content": r[1]} for r in reversed(rows)]
```

### brain.py

```python
import anthropic
from config import ANTHROPIC_API_KEY, CLAUDE_MODEL_CHAT

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

SYSTEM_PROMPT = """You are Jarvis, a personal executive assistant for Matt.
You are direct, efficient, and proactive. You speak concisely.

Your capabilities:
- Conversational AI assistance
- Saving notes, tasks, and ideas to memory
- Answering questions using your knowledge

When Matt mentions a task, expense, idea, or note — save it automatically
without asking for confirmation. Acknowledge briefly and move on.

Current date/time context will be provided with each message.
"""

# Tool definitions for Claude's tool-calling API
TOOLS = [
    {
        "name": "save_note",
        "description": "Save a note, task, idea, or expense to local storage. Use proactively when Matt mentions anything worth remembering.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["task", "note", "expense", "idea"],
                    "description": "Type of item to save"
                },
                "content": {
                    "type": "string",
                    "description": "The item content"
                },
                "metadata": {
                    "type": "object",
                    "description": "Optional structured data (amount, due_date, priority, etc.)"
                }
            },
            "required": ["category", "content"]
        }
    },
    {
        "name": "get_current_time",
        "description": "Get the current date and time in ET",
        "input_schema": {
            "type": "object",
            "properties": {}
        }
    }
]

async def think(user_message: str, conversation_history: list[dict]) -> str:
    """Send message to Claude with tools, handle tool calls, return final text."""
    messages = conversation_history + [{"role": "user", "content": user_message}]

    # Import tool handlers
    from tools import handle_tool_call

    # Agentic loop — keeps going until Claude returns text (not a tool call)
    while True:
        response = client.messages.create(
            model=CLAUDE_MODEL_CHAT,
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages
        )

        # Check if Claude wants to use a tool
        if response.stop_reason == "tool_use":
            # Process all tool calls in the response
            tool_results = []
            assistant_content = response.content  # contains text + tool_use blocks

            for block in response.content:
                if block.type == "tool_use":
                    result = await handle_tool_call(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": str(result)
                    })

            # Append assistant response + tool results, then loop
            messages.append({"role": "assistant", "content": assistant_content})
            messages.append({"role": "user", "content": tool_results})
        else:
            # Claude returned final text
            text_parts = [b.text for b in response.content if hasattr(b, "text")]
            return "\n".join(text_parts)
```

### tools.py

```python
import json
from datetime import datetime
import aiosqlite
from memory import DB_PATH

async def handle_tool_call(tool_name: str, tool_input: dict) -> str:
    """Route tool calls to their implementations."""
    handlers = {
        "save_note": save_note,
        "get_current_time": get_current_time,
    }

    handler = handlers.get(tool_name)
    if handler:
        return await handler(tool_input)
    return f"Unknown tool: {tool_name}"

async def save_note(input: dict) -> str:
    """Save a note/task/expense/idea to SQLite."""
    category = input["category"]
    content = input["content"]
    metadata = json.dumps(input.get("metadata", {}))

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO notes (category, content, metadata, timestamp) VALUES (?, ?, ?, ?)",
            (category, content, metadata, datetime.utcnow().isoformat())
        )
        await db.commit()

    return f"Saved {category}: {content}"

async def get_current_time(input: dict) -> str:
    """Return current time in ET."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.strftime("%A, %B %d, %Y at %I:%M %p ET")
```

### bot.py — The Main Entry Point

```python
import asyncio
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from config import TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_ID
from memory import init_db, save_message, get_recent_messages
from brain import think

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("jarvis")

# --- Auth middleware ---
def authorized_only(func):
    """Decorator: silently drop messages from unauthorized users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != AUTHORIZED_USER_ID:
            logger.warning(f"Unauthorized: {update.effective_user.id}")
            return  # silent drop
        return await func(update, context)
    return wrapper

# --- Handlers ---
@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Jarvis online. How can I help, sir?")

@authorized_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return

    # Show typing indicator
    await update.message.chat.send_action("typing")

    # Save user message
    await save_message("user", user_text)

    # Get conversation context
    history = await get_recent_messages(limit=20)

    # Get Claude's response
    try:
        response = await think(user_text, history)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        response = "I hit a snag processing that. Try again in a moment."

    # Save assistant response
    await save_message("assistant", response)

    # Send response (handle Telegram's 4096 char limit)
    if len(response) <= 4096:
        await update.message.reply_text(response)
    else:
        # Split into chunks
        for i in range(0, len(response), 4096):
            await update.message.reply_text(response[i:i+4096])

# --- Main ---
async def post_init(application):
    """Run after bot starts."""
    await init_db()
    logger.info("Jarvis initialized. Database ready.")

def main():
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Jarvis starting... (long polling)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
```

---

## Phase 2: Deploy & Run as Service (30 min)

### Test Locally First

```bash
cd ~/jarvis
source .venv/bin/activate
python bot.py
```

Open Telegram → message your bot → verify it responds.

### Create systemd Service

```bash
sudo nano /etc/systemd/system/jarvis.service
```

```ini
[Unit]
Description=Jarvis AI Assistant
After=network.target

[Service]
Type=simple
User=jarvis
WorkingDirectory=/home/jarvis/jarvis
Environment=PATH=/home/jarvis/jarvis/.venv/bin:/usr/bin
ExecStart=/home/jarvis/jarvis/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable jarvis
sudo systemctl start jarvis

# Check status
sudo systemctl status jarvis

# View logs
journalctl -u jarvis -f
```

---

## Phase 3: VSCode + Claude Code Dev Workflow

### Local Development Setup

```bash
# On your Mac/PC
# 1. Install Claude Code extension in VSCode
# 2. Configure SSH remote connection to droplet

# In VSCode:
#   Cmd+Shift+P → "Remote-SSH: Connect to Host"
#   Add: jarvis@YOUR_DROPLET_IP
#   Open folder: /home/jarvis/jarvis
```

### Development Workflow

```
1. Edit code in VSCode (connected to droplet via SSH)
2. Use Claude Code in the terminal for AI-assisted coding
3. Test changes:
   sudo systemctl stop jarvis
   python bot.py          # run interactively to see logs
   # Ctrl+C when done
   sudo systemctl start jarvis
```

### Alternatively: Develop Fully Local, Deploy via Git

```bash
# On the droplet — set up bare git repo
mkdir ~/jarvis-repo.git && cd ~/jarvis-repo.git
git init --bare

# Create post-receive hook for auto-deploy
cat > hooks/post-receive << 'EOF'
#!/bin/bash
GIT_WORK_TREE=/home/jarvis/jarvis git checkout -f main
cd /home/jarvis/jarvis
source .venv/bin/activate
pip install -r requirements.txt --quiet
sudo systemctl restart jarvis
echo "Jarvis deployed and restarted."
EOF
chmod +x hooks/post-receive

# On your local machine
cd ~/projects/jarvis
git init
git remote add deploy jarvis@YOUR_DROPLET_IP:jarvis-repo.git

# Deploy with:
git push deploy main
```

---

## Phase 4: Add Integrations (Weekend Afternoon)

Add these one at a time. Each is a new tool definition in `brain.py` + handler in `tools.py`.

### Google Calendar

```bash
pip install google-api-python-client google-auth
```

1. Go to Google Cloud Console → create project → enable Calendar API
2. Create **Service Account** → download JSON key
3. Share your calendar with the service account email
4. Add `GOOGLE_SERVICE_ACCOUNT_FILE=path/to/key.json` to `.env`

**Tool**: `get_todays_calendar` → returns today's events

### Todoist

```bash
pip install todoist-api-python
```

1. Get API token from https://app.todoist.com/app/settings/integrations/developer
2. Add `TODOIST_API_TOKEN=xxx` to `.env`

**Tools**: `add_task` (use Quick Add for NLP parsing), `get_tasks`

### Monarch Money

```bash
pip install monarchmoney
```

1. Add `MONARCH_EMAIL`, `MONARCH_PASSWORD`, `MONARCH_MFA_SECRET` to `.env`
2. If using Google SSO, create a dedicated password first in Monarch settings

**Tools**: `get_account_balances`, `get_recent_transactions`, `get_budget_summary`

⚠️ **Risk**: Unofficial API, technically violates Monarch ToS. Community patches quickly when it breaks. Acceptable for personal use.

### Scheduled Briefings (APScheduler — already included in PTB)

Add to `bot.py`:

```python
from telegram.ext import ApplicationBuilder

async def morning_briefing(context: ContextTypes.DEFAULT_TYPE):
    """Runs daily at 6:45 AM ET — before CrossFit."""
    # Pull calendar, tasks, weather
    # Send to Claude Sonnet for synthesis
    # Push formatted brief to Telegram
    brief = await generate_briefing()  # implement in brain.py
    await context.bot.send_message(
        chat_id=AUTHORIZED_USER_ID,
        text=brief
    )

# In main(), before app.run_polling():
from datetime import time
from zoneinfo import ZoneInfo

job_queue = app.job_queue
job_queue.run_daily(
    morning_briefing,
    time=time(hour=6, minute=45, tzinfo=ZoneInfo("America/New_York")),
    name="morning_briefing"
)
```

---

## Phase 5: Hardening & Production Touches

### Watchdog Heartbeat

Add a daily "I'm alive" ping to yourself:

```python
job_queue.run_daily(
    heartbeat_ping,
    time=time(hour=12, minute=0, tzinfo=ZoneInfo("America/New_York")),
    name="heartbeat"
)
```

### Log Rotation

```bash
sudo nano /etc/logrotate.d/jarvis
```

```
/var/log/jarvis/*.log {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
```

### Auto-Updates for Security

```bash
sudo apt install unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

### Backup SQLite

```python
# Weekly backup job — copy jarvis.db to a timestamped file
import shutil
shutil.copy("data/jarvis.db", f"data/backups/jarvis_{date}.db")
```

---

## Design Points from Your Research

Based on your three uploaded docs, here are the high-signal decisions baked into this plan:

1. **No framework layer** — Your Compass report nailed it: Claude's Tool Runner + Python SDKs eliminates the need for LangGraph, n8n, or any orchestrator for a single-user system. ~500 lines of code total.

2. **Long polling, not webhooks** — Zero public attack surface. UFW blocks everything inbound except SSH. This was the #1 security recommendation across all three docs.

3. **LLM never touches fetch/write primitives** — Per your deep research: compute facts in code, use Claude only for parsing and synthesis. Tool functions do the actual API calls with deterministic logic.

4. **Haiku for chat, Sonnet for reports** — Smart model routing keeps costs at ~$5/mo while using frontier intelligence where it matters (weekly briefs, financial analysis).

5. **SQLite over Postgres** — For single-user, SQLite is simpler and eliminates a whole service to manage. Upgrade to Postgres only if you add multi-device or need concurrent writes.

6. **Monarch Money risk accepted** — Unofficial API, ToS-gray. Community patches fast. Keep sessions alive, don't hammer the endpoint, and you're fine for personal use. Build an export fallback.

7. **Skip MCP for MVP** — Direct SDK calls = fewer moving parts. MCP is the upgrade path once Jarvis is stable and you want Claude to dynamically discover tools.

8. **Telegram is not E2E encrypted for bots** — Never send raw account numbers or passwords through the chat. Summaries and alerts only. Detailed data stays in SQLite/Notion.

---

## Execution Timeline

| Block | What | Duration |
|-------|------|----------|
| **Tonight** | Droplet + SSH + Python + .env + bot token | 30 min |
| **Session 1** | Phase 1 core bot — chat with Claude via Telegram | 2 hours |
| **Session 2** | Phase 2 systemd + Phase 3 VSCode dev workflow | 30 min |
| **Session 3** | Add Calendar + Todoist tools | 2 hours |
| **Session 4** | Add Monarch + morning briefing | 2 hours |
| **Session 5** | Hardening, heartbeat, backups | 1 hour |

**Total: ~8 hours across a few evenings.** Not a weekend sprint — steady, testable progress.

---

## Quick Reference Commands

```bash
# Start/stop/restart
sudo systemctl start jarvis
sudo systemctl stop jarvis
sudo systemctl restart jarvis

# Live logs
journalctl -u jarvis -f

# SSH into droplet
ssh jarvis@YOUR_DROPLET_IP

# Activate venv
cd ~/jarvis && source .venv/bin/activate

# Quick DB check
sqlite3 data/jarvis.db "SELECT * FROM messages ORDER BY id DESC LIMIT 5;"
```
