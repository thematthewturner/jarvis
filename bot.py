import asyncio
import json
import logging
from datetime import datetime, time
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes
)
from config import TELEGRAM_BOT_TOKEN, AUTHORIZED_USER_ID
from memory import init_db, save_message, get_recent_messages
from brain import think
from startup_checks import secure_token_files, validate_required_env

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")
SCHEDULED_JOB_KWARGS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 1800}
SCHEDULED_JOB_LOCKS = {
    "financial_sync": asyncio.Lock(),
    "daily_digest": asyncio.Lock(),
    "inbox_triage": asyncio.Lock(),
    "gmail_triage": asyncio.Lock(),
    "investor_scan": asyncio.Lock(),
    "action_nudges": asyncio.Lock(),
    "pool_nudge": asyncio.Lock(),
    "home_maintenance": asyncio.Lock(),
    "weekly_finance_digest": asyncio.Lock(),
}


async def _run_scheduled(job_name: str, context: ContextTypes.DEFAULT_TYPE, runner):
    lock = SCHEDULED_JOB_LOCKS[job_name]
    if lock.locked():
        logger.warning("Skipping scheduled job %s: previous run still active", job_name)
        await context.bot.send_message(
            chat_id=AUTHORIZED_USER_ID,
            text=f"⏭️ Skipping {job_name}: previous run is still in progress.",
        )
        return
    async with lock:
        await runner()


async def _schedule_startup_catchup(application):
    """If startup happens shortly after morning windows, run one catch-up cycle."""
    now = datetime.now(ET)
    windows = [
        ("financial_sync", 4, 40, 180, scheduled_financial_sync),
        ("daily_digest", 6, 45, 120, scheduled_daily_digest),
        ("inbox_triage", 7, 0, 90, scheduled_triage),
        ("gmail_triage", 7, 15, 90, scheduled_gmail_triage),
        ("investor_scan", 7, 30, 120, scheduled_investor_scan),
        ("action_nudges", 7, 35, 120, scheduled_action_nudges),
        ("home_maintenance", 8, 5, 120, scheduled_home_maintenance),
    ]
    for name, hour, minute, window_mins, callback in windows:
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        delta_minutes = int((now - scheduled).total_seconds() / 60)
        if 0 <= delta_minutes <= window_mins:
            catchup_name = f"{name}_catchup_{now.strftime('%Y%m%d')}"
            application.job_queue.run_once(callback, when=10, name=catchup_name)
            logger.info("Scheduled startup catch-up for %s (%s mins late)", name, delta_minutes)

    if now.weekday() == 4:
        weekly_scheduled = now.replace(hour=7, minute=45, second=0, microsecond=0)
        weekly_delta_minutes = int((now - weekly_scheduled).total_seconds() / 60)
        if 0 <= weekly_delta_minutes <= 180:
            catchup_name = f"weekly_finance_digest_catchup_{now.strftime('%Y%m%d')}"
            application.job_queue.run_once(scheduled_weekly_finance_digest, when=10, name=catchup_name)
            logger.info(
                "Scheduled startup catch-up for weekly_finance_digest (%s mins late)",
                weekly_delta_minutes,
            )


def authorized_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if update.effective_user.id != AUTHORIZED_USER_ID:
            logger.warning(f"Unauthorized: {update.effective_user.id}")
            return
        return await func(update, context)
    return wrapper


@authorized_only
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Jarvis online. How can I help, sir?")


@authorized_only
async def skills_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        from jarvis_ops import get_skills_overview_text

        await update.message.reply_text(get_skills_overview_text())
    except Exception as e:
        logger.error(f"Skills overview error: {e}")
        await update.message.reply_text(f"Skills overview failed: {e}")


@authorized_only
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text
    if not user_text:
        return

    await update.message.chat.send_action("typing")
    history = await get_recent_messages(limit=20)
    await save_message("user", user_text)

    try:
        response = await think(user_text, history)
    except Exception as e:
        logger.error(f"Claude error: {e}")
        response = "I hit a snag processing that. Try again in a moment."

    await save_message("assistant", response)

    if len(response) <= 4096:
        await update.message.reply_text(response)
    else:
        for i in range(0, len(response), 4096):
            await update.message.reply_text(response[i:i+4096])


@authorized_only
async def daily_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Build the combined daily digest and send the Notion link + highlights."""
    await update.message.reply_text("Assembling your daily digest... give me a moment.")
    await update.message.chat.send_action("typing")

    try:
        from daily_digest import run_daily_digest

        payload = await run_daily_digest()
        await update.message.reply_text(payload.get("notification_text", "Daily digest complete."))
    except Exception as e:
        logger.error(f"Daily digest error: {e}")
        await update.message.reply_text(f"Daily digest failed: {e}")


@authorized_only
async def briefing_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /briefing command — generate on demand."""
    await update.message.reply_text("Generating your briefing... give me a moment.")
    await update.message.chat.send_action("typing")

    try:
        from briefing import run_briefing
        text = await run_briefing()
        await update.message.reply_text(f"📋 {text}")
    except Exception as e:
        logger.error(f"Briefing error: {e}")
        await update.message.reply_text(f"Briefing failed: {e}")


@authorized_only
async def family_calendar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show upcoming events from the family iCloud calendar."""
    days = int(context.args[0]) if context.args and context.args[0].isdigit() else 14
    await update.message.chat.send_action("typing")
    try:
        from tools import get_icloud_calendar

        text = await get_icloud_calendar({"date": "upcoming", "days": days})
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Family calendar command error: {e}")
        await update.message.reply_text(f"Family calendar failed: {e}")


@authorized_only
async def research_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /research command — generate on demand.
    Optional: /research <ad hoc topic prompt>
    """
    ad_hoc_prompt = " ".join(context.args or []).strip()
    if ad_hoc_prompt:
        await update.message.reply_text(
            f"Scanning latest intel for: {ad_hoc_prompt}\n"
            "I will only report genuinely new items."
        )
    else:
        await update.message.reply_text("Fetching today's research... give me a moment.")
    await update.message.chat.send_action("typing")

    try:
        from daily_research import run_daily_research
        text = await run_daily_research(
            override_focus_prompt=ad_hoc_prompt or None
        )
        await update.message.reply_text(f"🔬 {text}")
    except Exception as e:
        logger.error(f"Research error: {e}")
        await update.message.reply_text(f"Research failed: {e}")


@authorized_only
async def kids_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run the kids/family digest on demand."""
    await update.message.reply_text("Scanning parent inboxes for kids and school items...")
    await update.message.chat.send_action("typing")

    try:
        from kids_digest import run_kids_digest

        text = await run_kids_digest()
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Kids digest error: {e}")
        await update.message.reply_text(f"Kids digest failed: {e}")


@authorized_only
async def research_topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage persistent daily research focus prompt.
    /research_topic                  -> show current config
    /research_topic clear            -> remove custom focus
    /research_topic <prompt text>    -> set custom focus
    """
    args = context.args or []
    try:
        from research_services import (
            clear_focus_topic_prompt,
            get_focus_topic_prompt,
            get_research_topics,
            set_focus_topic_prompt,
        )

        if not args:
            focus = await get_focus_topic_prompt()
            topics = await get_research_topics()
            lines = []
            if focus:
                lines.append(f"🎯 Current daily focus: {focus}")
            else:
                lines.append("🎯 No custom focus set (using default topic streams).")
            lines.append("")
            lines.append("Active research streams:")
            for t in topics:
                lines.append(f"- {t['emoji']} {t['name']}")
            lines.append("")
            lines.append("Set focus: /research_topic <what you want tracked>")
            lines.append("Clear focus: /research_topic clear")
            await update.message.reply_text("\n".join(lines))
            return

        if args[0].lower() in {"clear", "reset", "off"}:
            await clear_focus_topic_prompt()
            await update.message.reply_text(
                "Cleared custom research focus. Daily digest is back to default streams."
            )
            return

        prompt = " ".join(args).strip()
        saved = await set_focus_topic_prompt(prompt)
        await update.message.reply_text(
            "Saved. Daily research will prioritize this focus and only report new intel:\n"
            f"🎯 {saved}\n\n"
            "Tip: run /research now to test immediately."
        )
    except Exception as e:
        logger.error(f"Research topic command error: {e}")
        await update.message.reply_text(f"Research topic update failed: {e}")


@authorized_only
async def investor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run/manage the daily investor paper-trade scanner."""
    args = context.args or []
    sub = args[0].lower() if args else "scan"
    usage = (
        "Investor commands:\n"
        "/investor — today's investor scan\n"
        "/investor refresh — force a fresh scan\n"
        "/investor status\n"
        "/investor fill <ticker> <entry_price> [qty]\n"
        "/investor close <ticker> <exit_price> [reason]\n"
        "/investor skip\n"
        "/investor review\n"
        "/investor pause [on|off]\n"
        "/investor bankroll <amount>"
    )

    try:
        from investor_bot import (
            format_investor_status,
            get_trade_review,
            log_trade_close,
            log_trade_fill,
            mark_trade_skipped,
            run_investor_scan,
            set_bankroll_amount,
            set_investor_pause,
        )

        await update.message.chat.send_action("typing")

        if sub in ("scan", "today", ""):
            text = await run_investor_scan(force_refresh=False, notify_when_no_play=True)
        elif sub in ("refresh", "rescan", "run"):
            text = await run_investor_scan(force_refresh=True, notify_when_no_play=True)
        elif sub == "status":
            text = await format_investor_status()
        elif sub == "fill":
            if len(args) < 3:
                await update.message.reply_text(usage)
                return
            quantity = int(args[3]) if len(args) > 3 and args[3].isdigit() else 1
            text = await log_trade_fill(args[1], float(args[2]), quantity=quantity)
        elif sub == "close":
            if len(args) < 3:
                await update.message.reply_text(usage)
                return
            reason = " ".join(args[3:]).strip() or "manual"
            text = await log_trade_close(args[1], float(args[2]), exit_reason=reason)
        elif sub == "skip":
            text = await mark_trade_skipped()
        elif sub == "review":
            text = await get_trade_review()
        elif sub == "pause":
            if len(args) < 2:
                await update.message.reply_text(usage)
                return
            text = await set_investor_pause(args[1].lower() in {"on", "true", "1", "yes"})
        elif sub == "bankroll":
            if len(args) < 2:
                await update.message.reply_text(usage)
                return
            text = await set_bankroll_amount(float(args[1]))
        else:
            text = usage

        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except ValueError as e:
        await update.message.reply_text(f"Investor command error: {e}\n\n{usage}")
    except Exception as e:
        logger.error(f"Investor command error: {e}")
        await update.message.reply_text(f"Investor command failed: {e}")


@authorized_only
async def pool_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /pool and subcommands."""
    args = context.args or []
    sub = args[0].lower() if args else "status"

    if sub in ("status", ""):
        await update.message.chat.send_action("typing")
        try:
            from pool_services import get_pool_status
            text = await get_pool_status()
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Pool status error: {e}")

    elif sub == "report":
        await update.message.reply_text("Generating pool report...")
        await update.message.chat.send_action("typing")
        try:
            from pool_services import generate_pool_report
            text = await generate_pool_report()
            await update.message.reply_text(f"Pool Report\n\n{text}")
        except Exception as e:
            await update.message.reply_text(f"Pool report error: {e}")

    elif sub == "history":
        days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
        try:
            from pool_services import get_pool_history
            text = await get_pool_history(days)
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Pool history error: {e}")

    elif sub == "log":
        # /pool log fc=2.5 ph=7.4 ta=90 salt=3100 cya=50 ch=280
        params = {}
        for pair in args[1:]:
            if "=" in pair:
                key, val = pair.split("=", 1)
                try:
                    params[key.lower()] = float(val)
                except ValueError:
                    params[key.lower()] = val
        if not params:
            await update.message.reply_text(
                "Usage: /pool log fc=2.5 ph=7.4 ta=90 salt=3100 cya=50 ch=280\n"
                "Include only the params you tested."
            )
            return
        try:
            from pool_services import log_chemistry
            text = await log_chemistry(**params)
            await update.message.reply_text(f"✓ {text}")
        except Exception as e:
            await update.message.reply_text(f"Log error: {e}")

    elif sub == "add" and len(args) >= 3:
        # /pool add salt 40lbs   /pool add muriatic_acid "1 quart"
        chemical = args[1]
        amount = " ".join(args[2:])
        try:
            from pool_services import log_chemical_addition
            text = await log_chemical_addition(chemical, amount)
            await update.message.reply_text(f"✓ {text}")
        except Exception as e:
            await update.message.reply_text(f"Log error: {e}")

    elif sub == "sync":
        # /pool sync — pull latest tests from Leslie's
        await update.message.reply_text("Syncing Leslie's water test history...")
        await update.message.chat.send_action("typing")
        try:
            from leslies_services import sync_leslies_tests
            text = await sync_leslies_tests()
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Leslie's sync error: {e}")

    elif sub == "orders":
        await update.message.reply_text("Fetching Leslie's order history...")
        await update.message.chat.send_action("typing")
        try:
            from leslies_services import sync_leslies_orders
            text = await sync_leslies_orders()
            await update.message.reply_text(text)
        except Exception as e:
            await update.message.reply_text(f"Leslie's orders error: {e}")

    elif sub == "debug":
        # /pool debug [tests|orders] — dump raw HTML for parsing troubleshooting
        target = args[1] if len(args) > 1 else "tests"
        await update.message.reply_text(f"Fetching Leslie's {target} page (debug)...")
        try:
            from leslies_services import get_leslies_debug
            text = await get_leslies_debug(target)
            if len(text) > 4096:
                text = text[:4093] + "..."
            await update.message.reply_text(text, parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"Debug error: {e}")

    elif sub == "omni_debug":
        # /pool omni_debug — dump raw OmniLogic telemetry JSON
        await update.message.reply_text("Fetching raw OmniLogic telemetry...")
        try:
            from pool_services import fetch_omnilogic_status
            data = await fetch_omnilogic_status()
            raw = data.get("raw", data)
            text = json.dumps(raw, indent=2, default=str)
            if len(text) > 4000:
                text = text[:3997] + "..."
            await update.message.reply_text(f"```\n{text}\n```", parse_mode="Markdown")
        except Exception as e:
            await update.message.reply_text(f"OmniLogic debug error: {e}")

    else:
        await update.message.reply_text(
            "Pool commands:\n"
            "/pool — current status (OmniLogic + last chemistry)\n"
            "/pool report — AI-generated pool report\n"
            "/pool history [days] — recent readings (default 7 days)\n"
            "/pool log fc=2.5 ph=7.4 ta=90 salt=3100 — log manual test\n"
            "/pool add salt '40 lbs' — log a chemical addition\n"
            "/pool sync — import latest tests from Leslie's\n"
            "/pool orders — Leslie's purchase history\n"
            "/pool debug — troubleshoot Leslie's HTML parsing\n"
            "/pool omni_debug — raw Hayward OmniLogic data"
        )


@authorized_only
async def budget_meeting_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /budget-meeting command — weekly budget meeting prep."""
    await update.message.reply_text("Syncing Monarch and prepping your budget meeting... give me a moment.")
    await update.message.chat.send_action("typing")

    try:
        from budget_meeting import run_budget_meeting
        text = await run_budget_meeting()
        msg = f"📊 Budget Meeting\n\n{text}\n\n→ Full breakdown saved to Notion."
        if len(msg) <= 4096:
            await update.message.reply_text(msg)
        else:
            for i in range(0, len(msg), 4096):
                await update.message.reply_text(msg[i:i+4096])
    except Exception as e:
        logger.error(f"Budget meeting error: {e}")
        await update.message.reply_text(f"Budget meeting failed: {e}")


@authorized_only
async def finance_sync_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /finance_sync [backfill] — sync Monarch cache to SQLite."""
    force_backfill = bool(context.args and context.args[0].lower() in {"backfill", "full", "365"})
    await update.message.reply_text(
        "Starting finance sync..."
        + (" running 1-year backfill." if force_backfill else " pulling latest transactions.")
    )
    await update.message.chat.send_action("typing")
    try:
        from financial_db import sync_financial_cache
        result = await sync_financial_cache(force_backfill=force_backfill)
        await update.message.reply_text(
            f"✅ Finance sync complete\n"
            f"mode: {result.get('mode')}\n"
            f"upserted: {result.get('upserted', 0)}\n"
            f"range: {result.get('range_start', '?')} → {result.get('range_end', '?')}"
        )
    except Exception as e:
        logger.error(f"Finance sync command error: {e}")
        await update.message.reply_text(f"Finance sync failed: {e}")


@authorized_only
async def finance_inbox_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process Drive-based finance inbox documents and archive completed files."""
    limit = int(context.args[0]) if context.args and context.args[0].isdigit() else 10
    await update.message.reply_text(
        f"Processing Finance Inbox (up to {limit} files)..."
    )
    await update.message.chat.send_action("typing")
    try:
        from finance_inbox import process_finance_inbox

        text = await process_finance_inbox(limit=limit)
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Finance inbox command error: {e}")
        await update.message.reply_text(f"Finance inbox failed: {e}")


@authorized_only
async def finance_open_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List open financial review items."""
    limit = int(context.args[0]) if context.args and context.args[0].isdigit() else 20
    await update.message.chat.send_action("typing")
    try:
        from finance_inbox import list_open_financial_items

        text = await list_open_financial_items(limit=limit)
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Finance open command error: {e}")
        await update.message.reply_text(f"Finance open failed: {e}")


@authorized_only
async def finance_digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Summarize open financial items into a digest view."""
    await update.message.chat.send_action("typing")
    try:
        from finance_inbox import get_finance_digest

        text = await get_finance_digest()
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Finance digest command error: {e}")
        await update.message.reply_text(f"Finance digest failed: {e}")


@authorized_only
async def finance_trend_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Analyze spending trend for this week versus recent weeks."""
    weeks = int(context.args[0]) if context.args and context.args[0].isdigit() else 4
    await update.message.chat.send_action("typing")
    try:
        from financial_db import get_spending_trend_summary

        text = await get_spending_trend_summary(weeks=weeks)
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Finance trend command error: {e}")
        await update.message.reply_text(f"Finance trend failed: {e}")


@authorized_only
async def finance_weekly_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Write a tight weekly finance digest using rolling 7-day and 30-day windows."""
    await update.message.reply_text("Building your weekly financial digest...")
    await update.message.chat.send_action("typing")
    try:
        from finance_weekly_digest import run_weekly_financial_digest

        text = await run_weekly_financial_digest(save_to_notion=False)
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Finance weekly command error: {e}")
        await update.message.reply_text(f"Finance weekly digest failed: {e}")


@authorized_only
async def finance_done_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mark one open financial item as reviewed/done."""
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Usage: /finance_done <id> [note]")
        return
    item_id = int(context.args[0])
    note = " ".join(context.args[1:]).strip()
    await update.message.chat.send_action("typing")
    try:
        from finance_inbox import mark_financial_item_done

        text = await mark_financial_item_done(item_id=item_id, note=note)
        await update.message.reply_text(text)
    except Exception as e:
        logger.error(f"Finance done command error: {e}")
        await update.message.reply_text(f"Finance done failed: {e}")


@authorized_only
async def triage_gmail_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /triage_gmail [max] — historical Gmail inbox triage.
    Optional: /triage_gmail 200  (default 300)
    """
    args = context.args or []
    max_emails = int(args[0]) if args and args[0].isdigit() else 300

    await update.message.reply_text(
        f"📬 Gmail triage starting — fetching up to {max_emails} inbox messages.\n"
        f"I'll send progress updates as each batch of 50 completes."
    )

    async def progress(text: str):
        await context.bot.send_message(chat_id=update.effective_chat.id, text=text)

    try:
        from inbox_triage import run_gmail_triage
        result = await run_gmail_triage(
            max_emails=max_emails,
            progress_callback=progress,
        )
        if len(result) <= 4096:
            await update.message.reply_text(result)
        else:
            for i in range(0, len(result), 4096):
                await update.message.reply_text(result[i:i+4096])
    except Exception as e:
        logger.error(f"Gmail triage error: {e}")
        await update.message.reply_text(
            f"Gmail triage failed: {e}\n\n"
            f"If you see a scope/permission error, re-run:\n"
            f"  python scripts/authorize_google.py\n"
            f"then scp the new google_token.json to the server."
        )


@authorized_only
async def triage_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /triage command — run inbox triage on demand."""
    await update.message.reply_text("Running inbox triage… scanning both inboxes with Claude. Give me ~20 seconds.")
    await update.message.chat.send_action("typing")

    try:
        from inbox_triage import run_triage
        text = await run_triage()
        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except Exception as e:
        logger.error(f"Triage error: {e}")
        await update.message.reply_text(f"Triage failed: {e}")


@authorized_only
async def action_nudges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run daily email and intentionality Todoist nudges on demand."""
    await update.message.reply_text("Scanning recent signals for clear action items and intentionality follow-through...")
    await update.message.chat.send_action("typing")
    try:
        from nudges import run_daily_email_action_nudges
        text = await run_daily_email_action_nudges(notify_empty=True)
        await update.message.reply_text(text or "No actionable items found.")
    except Exception as e:
        logger.error(f"Action nudges error: {e}")
        await update.message.reply_text(f"Action nudges failed: {e}")


@authorized_only
async def pool_nudge_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run pool chemistry nudge check on demand."""
    await update.message.reply_text("Checking whether a pool chemistry reminder is needed...")
    await update.message.chat.send_action("typing")
    try:
        from nudges import run_weekly_pool_nudge
        text = await run_weekly_pool_nudge(force=True, notify_empty=True)
        await update.message.reply_text(text or "Pool chemistry nudge not needed.")
    except Exception as e:
        logger.error(f"Pool nudge error: {e}")
        await update.message.reply_text(f"Pool nudge failed: {e}")


@authorized_only
async def nudges_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Run both action nudges and pool nudge checks on demand."""
    await update.message.reply_text("Running Jarvis nudge workflow...")
    await update.message.chat.send_action("typing")
    try:
        from nudges import run_daily_email_action_nudges, run_weekly_pool_nudge
        action_text = await run_daily_email_action_nudges(notify_empty=True)
        pool_text = await run_weekly_pool_nudge(force=True, notify_empty=True)
        combined = "\n\n".join([t for t in [action_text, pool_text] if t])
        await update.message.reply_text(combined or "No nudges to create right now.")
    except Exception as e:
        logger.error(f"Nudges workflow error: {e}")
        await update.message.reply_text(f"Nudges workflow failed: {e}")


@authorized_only
async def intentionality_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage long-lived intentionality themes, anchors, and recurring rhythms."""
    args = context.args or []
    sub = args[0].lower() if args else "status"
    usage = (
        "Intentionality commands:\n"
        "/intentionality (or /i) — show current theme, anchors, and rhythms\n"
        "/intentionality set <theme>\n"
        "/intentionality note <context to hold>\n"
        "/intentionality study <daily|weekly|monthly|every 30 days> <scripture reference>\n"
        "/intentionality source scripture|book|other | reference | optional note\n"
        "/intentionality rhythm <description> | weekly|monthly|every 30 days | optional task text | optional due string | optional priority\n"
        "/intentionality done <rhythm_id> [YYYY-MM-DD]\n"
        "/intentionality nudge"
    )

    try:
        from intentionality import (
            add_context_note,
            add_rhythm,
            add_source_anchor,
            format_intentionality_status,
            generate_digest_nudge,
            mark_rhythm_done,
            parse_rhythm_input,
            parse_study_input,
            parse_source_input,
            set_primary_theme,
        )

        await update.message.chat.send_action("typing")

        if sub in ("status", "list", ""):
            text = await format_intentionality_status()
        elif sub in ("set", "theme"):
            raw = " ".join(args[1:]).strip()
            if not raw:
                await update.message.reply_text(usage)
                return
            entry = await set_primary_theme(raw)
            text = f"Saved intentionality theme: {entry['content']}"
        elif sub in ("note", "add", "context"):
            raw = " ".join(args[1:]).strip()
            if not raw:
                await update.message.reply_text(usage)
                return
            entry = await add_context_note(raw)
            text = f"Saved intentionality context #{entry['id']}."
        elif sub == "study":
            parsed = parse_study_input(args[1:])
            source_entry = await add_source_anchor(
                reference=parsed["reference"],
                source_kind="scripture",
                note=parsed["note"],
            )
            rhythm_entry = await add_rhythm(
                description=f"Study {parsed['reference']}",
                cadence_days=parsed["cadence_days"],
                task_text=parsed["task_text"],
            )
            text = (
                f"Saved scripture anchor #{source_entry['id']}: {source_entry['content']}\n"
                f"Saved study rhythm #{rhythm_entry['id']}: every {parsed['cadence_days']} day(s)"
            )
        elif sub == "source":
            raw = " ".join(args[1:]).strip()
            parsed = parse_source_input(raw)
            entry = await add_source_anchor(
                reference=parsed["reference"],
                source_kind=parsed["source_kind"],
                note=parsed["note"],
            )
            kind = entry["metadata"].get("source_kind", "other")
            text = f"Saved {kind} anchor #{entry['id']}: {entry['content']}"
        elif sub == "rhythm":
            raw = " ".join(args[1:]).strip()
            parsed = parse_rhythm_input(raw)
            entry = await add_rhythm(**parsed)
            cadence_days = entry["metadata"].get("cadence_days")
            text = (
                f"Saved rhythm #{entry['id']}: {entry['content']}\n"
                f"Cadence: every {cadence_days} day(s)"
            )
        elif sub == "done":
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text(usage)
                return
            completed_on = args[2] if len(args) > 2 else None
            completed_at = await mark_rhythm_done(int(args[1]), completed_on)
            text = f"Marked rhythm #{args[1]} complete at {completed_at}"
        elif sub == "nudge":
            text = await generate_digest_nudge()
            if not text:
                text = "No intentionality nudge yet. Add a theme, source, or rhythm first."
        else:
            text = usage

        await update.message.reply_text(text)
    except Exception as e:
        logger.error(f"Intentionality command error: {e}")
        await update.message.reply_text(f"Intentionality update failed: {e}")


@authorized_only
async def home_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage recurring home maintenance tasks."""
    args = context.args or []
    sub = args[0].lower() if args else "status"

    usage = (
        "Home commands:\n"
        "/home — status for the next 30 days\n"
        "/home check [days] — create due Todoist tasks\n"
        "/home add Replace Air Filters (3) | every 90 days | starts 2026-03-15 | Sizes: 16x20x1, 20x20x1, 20x25x1\n"
        "/home info <id> — full notes + history\n"
        "/home done <id> [YYYY-MM-DD] — mark complete and roll next due date"
    )

    try:
        from home_ops import (
            add_home_task,
            complete_home_task,
            get_home_status,
            get_home_task_details,
            parse_home_add_input,
            run_home_maintenance_check,
        )

        await update.message.chat.send_action("typing")

        if sub in ("status", "list", ""):
            days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
            text = await get_home_status(days_ahead=days)
        elif sub == "check":
            days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
            text = await run_home_maintenance_check(days_ahead=days, notify_empty=True)
        elif sub == "add":
            raw = " ".join(args[1:]).strip()
            parsed = parse_home_add_input(raw)
            text = await add_home_task(**parsed)
        elif sub in ("done", "complete"):
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text(usage)
                return
            completed_on = args[2] if len(args) > 2 else None
            text = await complete_home_task(int(args[1]), completed_on)
        elif sub == "info":
            if len(args) < 2 or not args[1].isdigit():
                await update.message.reply_text(usage)
                return
            text = await get_home_task_details(int(args[1]))
        else:
            await update.message.reply_text(usage)
            return

        if len(text) <= 4096:
            await update.message.reply_text(text)
        else:
            for i in range(0, len(text), 4096):
                await update.message.reply_text(text[i:i+4096])
    except ValueError as e:
        await update.message.reply_text(f"Home command error: {e}\n\n{usage}")
    except Exception as e:
        logger.error(f"Home command error: {e}")
        await update.message.reply_text(f"Home command failed: {e}")


async def scheduled_triage(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily inbox triage at 7am ET."""
    async def _runner():
        logger.info("Running scheduled inbox triage")
        try:
            from inbox_triage import run_triage
            text = await run_triage()
            await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
        except Exception as e:
            logger.error(f"Scheduled triage error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Morning triage failed: {e}"
            )

    await _run_scheduled("inbox_triage", context, _runner)


async def scheduled_gmail_triage(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily Gmail triage — last 48 hrs, capped at 50 emails."""
    async def _runner():
        logger.info("Running scheduled Gmail triage")
        try:
            from inbox_triage import run_gmail_triage
            text = await run_gmail_triage(
                max_emails=50,
                query="in:inbox newer_than:2d",
            )
            await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
        except Exception as e:
            logger.error(f"Scheduled Gmail triage error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Gmail triage failed: {e}"
            )

    await _run_scheduled("gmail_triage", context, _runner)


async def scheduled_investor_scan(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily investor paper-trade scan."""
    async def _runner():
        logger.info("Running scheduled investor scan")
        try:
            from investor_bot import run_investor_scan
            text = await run_investor_scan(force_refresh=True, notify_when_no_play=False)
            if text:
                await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
            else:
                logger.info("Scheduled investor scan: no new play; skipping Telegram send.")
        except Exception as e:
            logger.error(f"Scheduled investor scan error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Investor scan failed: {e}"
            )

    await _run_scheduled("investor_scan", context, _runner)


async def scheduled_action_nudges(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily email and intentionality Todoist nudges."""
    async def _runner():
        logger.info("Running scheduled action nudges")
        try:
            from nudges import run_daily_email_action_nudges
            text = await run_daily_email_action_nudges(notify_empty=False)
            if text:
                await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
        except Exception as e:
            logger.error(f"Scheduled action nudges error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Action nudges failed: {e}"
            )

    await _run_scheduled("action_nudges", context, _runner)


async def scheduled_pool_nudge(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled pool reminder check (Monday-only logic inside nudges module)."""
    async def _runner():
        logger.info("Running scheduled pool nudge check")
        try:
            from nudges import run_weekly_pool_nudge
            text = await run_weekly_pool_nudge(force=False, notify_empty=False)
            if text:
                await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
        except Exception as e:
            logger.error(f"Scheduled pool nudge error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Pool nudge check failed: {e}"
            )

    await _run_scheduled("pool_nudge", context, _runner)


async def scheduled_home_maintenance(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily home maintenance sync to Todoist."""
    async def _runner():
        logger.info("Running scheduled home maintenance sync")
        try:
            from home_ops import run_home_maintenance_check
            text = await run_home_maintenance_check(days_ahead=7, notify_empty=False)
            if text:
                await context.bot.send_message(chat_id=AUTHORIZED_USER_ID, text=text)
        except Exception as e:
            logger.error(f"Scheduled home maintenance error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Home maintenance sync failed: {e}"
            )

    await _run_scheduled("home_maintenance", context, _runner)


async def scheduled_daily_digest(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled unified morning digest."""
    async def _runner():
        logger.info("Running scheduled daily digest")
        try:
            from daily_digest import run_daily_digest

            payload = await run_daily_digest()
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=payload.get("notification_text", "Daily digest complete."),
            )
        except Exception as e:
            logger.error(f"Scheduled daily digest error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Daily digest failed: {e}",
            )

    await _run_scheduled("daily_digest", context, _runner)


async def scheduled_financial_sync(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled daily Monarch cache sync."""
    async def _runner():
        logger.info("Running scheduled financial sync")
        try:
            from financial_db import sync_financial_cache
            result = await sync_financial_cache(force_backfill=False)
            logger.info(
                "Financial sync result: mode=%s upserted=%s range=%s..%s",
                result.get("mode"),
                result.get("upserted"),
                result.get("range_start"),
                result.get("range_end"),
            )
        except Exception as e:
            logger.error(f"Scheduled financial sync error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Financial sync failed: {e}"
            )

    await _run_scheduled("financial_sync", context, _runner)


async def scheduled_weekly_finance_digest(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled Friday-morning weekly finance digest."""
    async def _runner():
        if datetime.now(ET).weekday() != 4:
            return
        logger.info("Running scheduled weekly finance digest")
        try:
            from finance_weekly_digest import run_weekly_financial_digest

            text = await run_weekly_financial_digest(save_to_notion=True)
            if text:
                await context.bot.send_message(
                    chat_id=AUTHORIZED_USER_ID,
                    text=f"💵 Weekly Financial Digest\n\n{text}",
                )
        except Exception as e:
            logger.error(f"Scheduled weekly finance digest error: {e}")
            await context.bot.send_message(
                chat_id=AUTHORIZED_USER_ID,
                text=f"Weekly finance digest failed: {e}",
            )

    await _run_scheduled("weekly_finance_digest", context, _runner)


async def post_init(application):
    await init_db()
    secure_token_files()
    await _schedule_startup_catchup(application)
    logger.info("Jarvis initialized. Database ready.")


def main():
    validate_required_env(
        ["TELEGRAM_BOT_TOKEN", "TELEGRAM_AUTHORIZED_USER_ID", "ANTHROPIC_API_KEY"],
        scope="bot",
    )
    if AUTHORIZED_USER_ID <= 0:
        raise RuntimeError("TELEGRAM_AUTHORIZED_USER_ID must be a non-zero integer")

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("skills", skills_command))
    app.add_handler(CommandHandler("daily_digest", daily_digest_command))
    app.add_handler(CommandHandler("briefing", briefing_command))
    app.add_handler(CommandHandler("family_calendar", family_calendar_command))
    app.add_handler(CommandHandler("kids_digest", kids_digest_command))
    app.add_handler(CommandHandler("investor", investor_command))
    app.add_handler(CommandHandler("research", research_command))
    app.add_handler(CommandHandler("research_topic", research_topic_command))
    app.add_handler(CommandHandler("finance_sync", finance_sync_command))
    app.add_handler(CommandHandler("finance_inbox", finance_inbox_command))
    app.add_handler(CommandHandler("finance_open", finance_open_command))
    app.add_handler(CommandHandler("finance_digest", finance_digest_command))
    app.add_handler(CommandHandler("finance_trend", finance_trend_command))
    app.add_handler(CommandHandler("finance_weekly", finance_weekly_command))
    app.add_handler(CommandHandler("finance_done", finance_done_command))
    app.add_handler(CommandHandler("budget_meeting", budget_meeting_command))
    app.add_handler(CommandHandler("pool", pool_command))
    app.add_handler(CommandHandler("triage", triage_command))
    app.add_handler(CommandHandler("triage_gmail", triage_gmail_command))
    app.add_handler(CommandHandler("action_nudges", action_nudges_command))
    app.add_handler(CommandHandler("pool_nudge", pool_nudge_command))
    app.add_handler(CommandHandler("nudges", nudges_command))
    app.add_handler(CommandHandler("intentionality", intentionality_command))
    app.add_handler(CommandHandler("i", intentionality_command))
    app.add_handler(CommandHandler("home", home_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Daily financial cache sync at 4:40 AM ET
    app.job_queue.run_daily(
        scheduled_financial_sync,
        time=time(hour=4, minute=40, tzinfo=ET),
        name="financial_sync",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Daily inbox triage at 7:00 AM ET
    app.job_queue.run_daily(
        scheduled_triage,
        time=time(hour=7, minute=0, tzinfo=ET),
        name="inbox_triage",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Daily Gmail triage at 7:15 AM ET (recent 48 hrs, capped at 50)
    app.job_queue.run_daily(
        scheduled_gmail_triage,
        time=time(hour=7, minute=15, tzinfo=ET),
        name="gmail_triage",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Unified morning digest at 6:45 AM ET
    app.job_queue.run_daily(
        scheduled_daily_digest,
        time=time(hour=6, minute=45, tzinfo=ET),
        name="daily_digest",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Daily investor scan at 7:30 AM ET
    app.job_queue.run_daily(
        scheduled_investor_scan,
        time=time(hour=7, minute=30, tzinfo=ET),
        name="investor_scan",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Daily actionable-email nudges at 7:35 AM ET
    app.job_queue.run_daily(
        scheduled_action_nudges,
        time=time(hour=7, minute=35, tzinfo=ET),
        name="action_nudges",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Pool nudge check at 8:20 AM ET (module enforces Monday-only unless forced)
    app.job_queue.run_daily(
        scheduled_pool_nudge,
        time=time(hour=8, minute=20, tzinfo=ET),
        name="pool_nudge",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Daily home maintenance sync at 8:05 AM ET
    app.job_queue.run_daily(
        scheduled_home_maintenance,
        time=time(hour=8, minute=5, tzinfo=ET),
        name="home_maintenance",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    # Friday-morning weekly finance digest at 7:45 AM ET
    app.job_queue.run_daily(
        scheduled_weekly_finance_digest,
        time=time(hour=7, minute=45, tzinfo=ET),
        name="weekly_finance_digest",
        job_kwargs=SCHEDULED_JOB_KWARGS,
    )

    logger.info("Jarvis starting... (long polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
