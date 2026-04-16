import sys
import os
import asyncio
import math
import random
import re
import secrets
from datetime import timedelta
from functools import wraps

# Add jarvis project root to path and set cwd so relative paths (data/jarvis.db) work
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import anthropic as _anthropic
from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from memory import DB_PATH, get_recent_messages, save_message
from brain import think
from startup_checks import secure_token_files, validate_required_env
from contextlib import asynccontextmanager

try:
    from memory import db_connect
except ImportError:
    import aiosqlite

    @asynccontextmanager
    async def db_connect(path):
        async with aiosqlite.connect(path) as db:
            yield db

validate_required_env(["ANTHROPIC_API_KEY", "WEB_PIN", "FLASK_SECRET_KEY"], scope="web")
secure_token_files(ROOT)

app = Flask(__name__)
WEB_PIN = os.getenv("WEB_PIN", "").strip()
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "").strip()
if not FLASK_SECRET_KEY:
    raise RuntimeError("FLASK_SECRET_KEY must be set")

app.secret_key = FLASK_SECRET_KEY
app.permanent_session_lifetime = timedelta(days=30)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("FLASK_SESSION_COOKIE_SECURE", "true").lower() == "true",
)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "unauthorized"}), 401
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if not WEB_PIN:
            return "WEB_PIN is not configured on this server.", 503
        pin = request.form.get("pin", "").strip()
        if secrets.compare_digest(pin, WEB_PIN):
            session.permanent = True
            session["authenticated"] = True
            return redirect(url_for("index"))
        error = "Invalid PIN"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def index():
    return render_template("index.html")


async def _run_web_slash_command(user_message: str) -> str | None:
    text = (user_message or "").strip()
    if not text.startswith("/"):
        return None

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "/start":
        return "Jarvis online. How can I help, sir?"

    if cmd == "/skills":
        from jarvis_ops import get_skills_overview_text
        return get_skills_overview_text()

    if cmd == "/help":
        return (
            "Commands:\n"
            "/morning_brief\n"
            "/skills\n"
            "/daily_digest\n"
            "/briefing\n"
            "/family_calendar [days]\n"
            "/kids_digest\n"
            "/research [topic]\n"
            "/research_topic [clear|<topic>]\n"
            "/finance_sync [backfill]\n"
            "/finance_inbox [limit]\n"
            "/finance_digest\n"
            "/finance_trend [weeks]\n"
            "/finance_weekly\n"
            "/finance_open [limit]\n"
            "/finance_done <id> [note]\n"
            "/budget_meeting\n"
            "/triage\n"
            "/triage_gmail [max]\n"
            "/action_nudges\n"
            "/pool_nudge\n"
            "/nudges\n"
            "/intentionality (or /i) [set|note|study|source|rhythm|done|nudge]\n"
            "/home [status|check|add|info|done]\n"
            "/pool [status|report|history|log|add|sync|orders|debug|omni_debug]"
        )

    if cmd == "/morning_brief":
        from morning_brief import run_morning_brief
        result = await run_morning_brief()
        return result.get("text", "Morning brief complete.")

    if cmd == "/briefing":
        from briefing import run_briefing
        text = await run_briefing()
        return f"📋 {text}"

    if cmd == "/daily_digest":
        from daily_digest import run_daily_digest
        force_refresh = bool(args and args[0].lower() in {"refresh", "force", "fresh"})
        payload = await run_daily_digest(force_refresh=force_refresh)
        return payload.get("notification_text", "Daily digest complete.")

    if cmd == "/family_calendar":
        from tools import get_icloud_calendar
        days = int(args[0]) if args and args[0].isdigit() else 14
        return await get_icloud_calendar({"date": "upcoming", "days": days})

    if cmd == "/kids_digest":
        from kids_digest import run_kids_digest
        return await run_kids_digest()

    if cmd == "/research":
        from daily_research import run_daily_research
        ad_hoc_prompt = " ".join(args).strip() or None
        text = await run_daily_research(override_focus_prompt=ad_hoc_prompt)
        return f"🔬 {text}"

    if cmd == "/research_topic":
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
            return "\n".join(lines)

        if args[0].lower() in {"clear", "reset", "off"}:
            await clear_focus_topic_prompt()
            return "Cleared custom research focus. Daily digest is back to default streams."

        prompt = " ".join(args).strip()
        saved = await set_focus_topic_prompt(prompt)
        return (
            "Saved. Daily research will prioritize this focus and only report new intel:\n"
            f"🎯 {saved}\n\n"
            "Tip: run /research now to test immediately."
        )

    if cmd == "/budget_meeting":
        from budget_meeting import run_budget_meeting
        text = await run_budget_meeting()
        return f"📊 Budget Meeting\n\n{text}\n\n→ Full breakdown saved to Notion."

    if cmd == "/finance_sync":
        from financial_db import sync_financial_cache
        force_backfill = bool(args and args[0].lower() in {"backfill", "full", "365"})
        result = await sync_financial_cache(force_backfill=force_backfill)
        return (
            f"✅ Finance sync complete\n"
            f"mode: {result.get('mode')}\n"
            f"upserted: {result.get('upserted', 0)}\n"
            f"range: {result.get('range_start', '?')} → {result.get('range_end', '?')}"
        )

    if cmd == "/finance_inbox":
        from finance_inbox import process_finance_inbox
        limit = int(args[0]) if args and args[0].isdigit() else 10
        return await process_finance_inbox(limit=limit)

    if cmd == "/finance_digest":
        from finance_inbox import get_finance_digest
        return await get_finance_digest()

    if cmd == "/finance_trend":
        from financial_db import get_spending_trend_summary
        weeks = int(args[0]) if args and args[0].isdigit() else 4
        return await get_spending_trend_summary(weeks=weeks)

    if cmd == "/finance_weekly":
        from finance_weekly_digest import run_weekly_financial_digest
        return await run_weekly_financial_digest(save_to_notion=False)

    if cmd == "/finance_open":
        from finance_inbox import list_open_financial_items
        limit = int(args[0]) if args and args[0].isdigit() else 20
        return await list_open_financial_items(limit=limit)

    if cmd == "/finance_done":
        from finance_inbox import mark_financial_item_done
        if not args or not args[0].isdigit():
            return "Usage: /finance_done <id> [note]"
        item_id = int(args[0])
        note = " ".join(args[1:]).strip()
        return await mark_financial_item_done(item_id=item_id, note=note)

    if cmd == "/triage":
        from inbox_triage import run_triage
        return await run_triage()

    if cmd == "/triage_gmail":
        from inbox_triage import run_gmail_triage
        max_emails = int(args[0]) if args and args[0].isdigit() else 300
        return await run_gmail_triage(max_emails=max_emails)

    if cmd == "/action_nudges":
        from nudges import run_daily_email_action_nudges
        text = await run_daily_email_action_nudges(notify_empty=True)
        return text or "No actionable items found."

    if cmd == "/pool_nudge":
        from nudges import run_weekly_pool_nudge
        text = await run_weekly_pool_nudge(force=True, notify_empty=True)
        return text or "Pool chemistry nudge not needed."

    if cmd == "/nudges":
        from nudges import run_daily_email_action_nudges, run_weekly_pool_nudge
        action_text = await run_daily_email_action_nudges(notify_empty=True)
        pool_text = await run_weekly_pool_nudge(force=True, notify_empty=True)
        return "\n\n".join([t for t in [action_text, pool_text] if t]) or "No nudges to create right now."

    if cmd in {"/intentionality", "/i"}:
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

        sub = args[0].lower() if args else "status"
        usage = (
            "Intentionality commands:\n"
            "/intentionality (or /i)\n"
            "/intentionality set <theme>\n"
            "/intentionality note <context to hold>\n"
            "/intentionality study <daily|weekly|monthly|every 30 days> <scripture reference>\n"
            "/intentionality source scripture|book|other | reference | optional note\n"
            "/intentionality rhythm <description> | weekly|monthly|every 30 days | optional task text | optional due string | optional priority\n"
            "/intentionality done <rhythm_id> [YYYY-MM-DD]\n"
            "/intentionality nudge"
        )

        try:
            if sub in ("status", "list", ""):
                return await format_intentionality_status()

            if sub in ("set", "theme"):
                raw = " ".join(args[1:]).strip()
                if not raw:
                    return usage
                entry = await set_primary_theme(raw)
                return f"Saved intentionality theme: {entry['content']}"

            if sub in ("note", "add", "context"):
                raw = " ".join(args[1:]).strip()
                if not raw:
                    return usage
                entry = await add_context_note(raw)
                return f"Saved intentionality context #{entry['id']}."

            if sub == "study":
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
                return (
                    f"Saved scripture anchor #{source_entry['id']}: {source_entry['content']}\n"
                    f"Saved study rhythm #{rhythm_entry['id']}: every {parsed['cadence_days']} day(s)"
                )

            if sub == "source":
                raw = " ".join(args[1:]).strip()
                parsed = parse_source_input(raw)
                entry = await add_source_anchor(
                    reference=parsed["reference"],
                    source_kind=parsed["source_kind"],
                    note=parsed["note"],
                )
                kind = entry["metadata"].get("source_kind", "other")
                return f"Saved {kind} anchor #{entry['id']}: {entry['content']}"

            if sub == "rhythm":
                raw = " ".join(args[1:]).strip()
                parsed = parse_rhythm_input(raw)
                entry = await add_rhythm(**parsed)
                cadence_days = entry["metadata"].get("cadence_days")
                return (
                    f"Saved rhythm #{entry['id']}: {entry['content']}\n"
                    f"Cadence: every {cadence_days} day(s)"
                )

            if sub == "done":
                if len(args) < 2 or not args[1].isdigit():
                    return usage
                completed_on = args[2] if len(args) > 2 else None
                completed_at = await mark_rhythm_done(int(args[1]), completed_on)
                return f"Marked rhythm #{args[1]} complete at {completed_at}"

            if sub == "nudge":
                text = await generate_digest_nudge()
                return text or "No intentionality nudge yet. Add a theme, source, or rhythm first."
        except ValueError as e:
            return f"Intentionality command error: {e}\n\n{usage}"

        return usage

    if cmd == "/home":
        from home_ops import (
            add_home_task,
            complete_home_task,
            get_home_status,
            get_home_task_details,
            parse_home_add_input,
            run_home_maintenance_check,
        )

        sub = args[0].lower() if args else "status"
        usage = (
            "Home commands:\n"
            "/home\n"
            "/home check [days]\n"
            "/home add Replace Air Filters (3) | every 90 days | starts 2026-03-15 | Sizes: 16x20x1, 20x20x1, 20x25x1\n"
            "/home info <id>\n"
            "/home done <id> [YYYY-MM-DD]"
        )

        try:
            if sub in ("status", "list", ""):
                days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 30
                return await get_home_status(days_ahead=days)

            if sub == "check":
                days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
                return await run_home_maintenance_check(days_ahead=days, notify_empty=True)

            if sub == "add":
                raw = " ".join(args[1:]).strip()
                parsed = parse_home_add_input(raw)
                return await add_home_task(**parsed)

            if sub in ("done", "complete"):
                if len(args) < 2 or not args[1].isdigit():
                    return usage
                completed_on = args[2] if len(args) > 2 else None
                return await complete_home_task(int(args[1]), completed_on)

            if sub == "info":
                if len(args) < 2 or not args[1].isdigit():
                    return usage
                return await get_home_task_details(int(args[1]))
        except ValueError as e:
            return f"Home command error: {e}\n\n{usage}"

        return usage

    if cmd == "/pool":
        sub = args[0].lower() if args else "status"
        if sub in ("status", ""):
            from pool_services import get_pool_status
            return await get_pool_status()

        if sub == "report":
            from pool_services import generate_pool_report
            text = await generate_pool_report()
            return f"Pool Report\n\n{text}"

        if sub == "history":
            days = int(args[1]) if len(args) > 1 and args[1].isdigit() else 7
            from pool_services import get_pool_history
            return await get_pool_history(days)

        if sub == "log":
            params = {}
            for pair in args[1:]:
                if "=" in pair:
                    key, val = pair.split("=", 1)
                    try:
                        params[key.lower()] = float(val)
                    except ValueError:
                        params[key.lower()] = val
            if not params:
                return (
                    "Usage: /pool log fc=2.5 ph=7.4 ta=90 salt=3100 cya=50 ch=280\n"
                    "Include only the params you tested."
                )
            from pool_services import log_chemistry
            text = await log_chemistry(**params)
            return f"✓ {text}"

        if sub == "add" and len(args) >= 3:
            chemical = args[1]
            amount = " ".join(args[2:])
            from pool_services import log_chemical_addition
            text = await log_chemical_addition(chemical, amount)
            return f"✓ {text}"

        if sub == "sync":
            from leslies_services import sync_leslies_tests
            return await sync_leslies_tests()

        if sub == "orders":
            from leslies_services import sync_leslies_orders
            return await sync_leslies_orders()

        if sub == "debug":
            target = args[1] if len(args) > 1 else "tests"
            from leslies_services import get_leslies_debug
            text = await get_leslies_debug(target)
            return text if len(text) <= 8000 else (text[:7997] + "...")

        if sub == "omni_debug":
            import json
            from pool_services import fetch_omnilogic_status
            data = await fetch_omnilogic_status()
            raw = data.get("raw", data)
            text = json.dumps(raw, indent=2, default=str)
            return text if len(text) <= 8000 else (text[:7997] + "...")

        return (
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

    return f"Unknown command: {cmd}\nUse /help for available commands."


@app.route("/api/chat", methods=["POST"])
@login_required
def chat():
    data = request.get_json()
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "empty message"}), 400

    try:
        history = asyncio.run(get_recent_messages(20))
        asyncio.run(save_message("user", user_message))
        response = asyncio.run(_run_web_slash_command(user_message))
        if response is None:
            response = asyncio.run(think(user_message, history))
        asyncio.run(save_message("assistant", response))
        return jsonify({"response": response})
    except _anthropic.APIStatusError as e:
        if e.status_code == 529:
            return jsonify({"error": "Claude is overloaded right now. Try again in a moment."}), 503
        return jsonify({"error": f"API error {e.status_code}"}), 502
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/tasks")
@login_required
def tiles_tasks():
    try:
        from tools import get_tasks
        result = asyncio.run(get_tasks({"filter": "today | overdue"}))
        if result.strip() == "No tasks found.":
            return jsonify({"count": 0, "items": []})
        items = [l.lstrip("- ") for l in result.strip().splitlines() if l.strip().startswith("-")]
        return jsonify({"count": len(items), "items": items[:6]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/home")
@login_required
def tiles_home():
    """Home maintenance tile: due-soon tasks from the recurring home ops system."""
    try:
        from home_ops import get_home_dashboard
        payload = asyncio.run(get_home_dashboard(days_ahead=30, limit=5))
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/calendar")
@login_required
def tiles_calendar():
    try:
        from icloud_calendar import get_todays_events
        from datetime import datetime
        events = asyncio.run(get_todays_events())
        items = []
        for e in events[:5]:
            summary = e.get("summary", "?")
            start = e.get("start", "")
            try:
                dt = datetime.fromisoformat(start)
                time_str = "All day" if e.get("all_day") else dt.strftime("%-I:%M %p")
                items.append(f"{time_str} — {summary}")
            except Exception:
                items.append(summary)
        return jsonify({"count": len(events), "items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/inbox")
@login_required
def section_inbox():
    async def _fetch():
        from icloud_mail import fetch_unread_emails
        from tools import get_tasks
        return await asyncio.gather(
            fetch_unread_emails(since_hours=48),
            get_tasks({"filter": "today | overdue"}),
            return_exceptions=True,
        )
    try:
        icloud_emails, tasks_raw = asyncio.run(_fetch())
        ic_items = []
        if isinstance(icloud_emails, list):
            for e in icloud_emails[:6]:
                sender = e.get("from", "?")
                m = re.match(r'^"?([^"<,]+)"?\s*<', sender)
                sender_short = m.group(1).strip() if m else sender.split("@")[0].replace(".", " ").replace("_", " ").title()
                ic_items.append({"from": sender_short, "subject": e.get("subject", "(no subject)")})
        task_items = []
        if isinstance(tasks_raw, str) and tasks_raw.strip() != "No tasks found.":
            task_items = [l.lstrip("- ").strip() for l in tasks_raw.strip().splitlines() if l.strip().startswith("-")]
        return jsonify({
            "icloud": {"count": len(ic_items), "items": ic_items},
            "tasks":  {"count": len(task_items), "items": task_items[:6]},
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/kids")
@login_required
def section_kids():
    async def _fetch():
        from icloud_calendar import get_todays_events, get_events_for_date_range
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        TZ = ZoneInfo("America/New_York")
        now = datetime.now(TZ)
        tom_start = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        tom_end   = tom_start.replace(hour=23, minute=59, second=59)
        return await asyncio.gather(
            get_todays_events(),
            get_events_for_date_range(tom_start, tom_end),
            return_exceptions=True,
        )
    try:
        today_evs, tomorrow_evs = asyncio.run(_fetch())
        def fmt(evs):
            if isinstance(evs, Exception) or not evs:
                return []
            from datetime import datetime
            out = []
            for e in evs[:7]:
                try:
                    dt = datetime.fromisoformat(e.get("start", ""))
                    t = "All day" if e.get("all_day") else dt.strftime("%-I:%M %p")
                except Exception:
                    t = ""
                out.append({"summary": e.get("summary", "?"), "time": t, "calendar": e.get("calendar_name", "")})
            return out
        return jsonify({"today": fmt(today_evs), "tomorrow": fmt(tomorrow_evs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/kids-digest")
@login_required
def section_kids_digest():
    """Latest kids email digest from DB."""
    try:
        from kids_digest import get_latest_digest
        digest = asyncio.run(get_latest_digest())
        if not digest:
            return jsonify({"found": False})
        return jsonify({
            "found": True,
            "date":        digest.get("date"),
            "summary":     digest.get("summary"),
            "items":       digest.get("items", []),
            "items_found": digest.get("items_found", 0),
            "notion_url":  digest.get("notion_url"),
            "created_at":  digest.get("created_at"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/daily-digest")
@login_required
def section_daily_digest():
    """Latest unified daily digest from local DB."""
    try:
        from daily_digest import get_latest_daily_digest

        digest = asyncio.run(get_latest_daily_digest())
        if not digest:
            return jsonify({"found": False})
        return jsonify({
            "found": True,
            "digest_date": digest.get("digest_date"),
            "title": digest.get("title"),
            "summary": digest.get("summary"),
            "highlights": digest.get("highlights", []),
            "sections": digest.get("sections", []),
            "notion_url": digest.get("notion_url"),
            "created_at": digest.get("created_at"),
            "quality": digest.get("quality", {}),
            "cached": digest.get("cached", False),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/daily-digest/run", methods=["POST"])
@login_required
def run_daily_digest_section():
    try:
        from daily_digest import run_daily_digest

        digest = asyncio.run(run_daily_digest(force_refresh=True))
        return jsonify({
            "found": True,
            "digest_date": digest.get("digest_date"),
            "title": digest.get("title"),
            "summary": digest.get("summary"),
            "highlights": digest.get("highlights", []),
            "sections": digest.get("sections", []),
            "notion_url": digest.get("notion_url"),
            "created_at": digest.get("created_at"),
            "cached": digest.get("cached", False),
            "quality": digest.get("quality", {}),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/kids-digest/run", methods=["POST"])
@login_required
def run_kids_digest():
    """Trigger a fresh kids digest scan on demand."""
    try:
        from kids_digest import run_kids_digest as _run
        asyncio.run(_run())
        from kids_digest import get_latest_digest
        digest = asyncio.run(get_latest_digest())
        return jsonify({
            "found": True,
            "date":        digest.get("date"),
            "summary":     digest.get("summary"),
            "items":       digest.get("items", []),
            "items_found": digest.get("items_found", 0),
            "notion_url":  digest.get("notion_url"),
            "created_at":  digest.get("created_at"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ops/overview")
@login_required
def ops_overview():
    try:
        from jarvis_ops import get_ops_overview

        live = request.args.get("live", "").strip().lower() in {"1", "true", "yes"}
        payload = asyncio.run(get_ops_overview(live_health=live))
        return jsonify(payload)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ops/jobs")
@login_required
def ops_jobs():
    try:
        from jarvis_ops import KNOWN_SCHEDULED_JOBS
        from jarvis_reliability import get_job_status, get_recent_job_runs

        jobs = asyncio.run(get_job_status(KNOWN_SCHEDULED_JOBS))
        recent = asyncio.run(get_recent_job_runs(limit=30))
        return jsonify({"jobs": jobs, "recent": recent})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ops/audit")
@login_required
def ops_audit():
    try:
        from jarvis_reliability import get_action_rollup, get_recent_actions

        tool = (request.args.get("tool") or "").strip() or None
        try:
            limit = int(request.args.get("limit", "50"))
        except ValueError:
            limit = 50
        try:
            hours = int(request.args.get("hours", "24"))
        except ValueError:
            hours = 24
        rollup = asyncio.run(get_action_rollup(hours=hours))
        actions = asyncio.run(get_recent_actions(limit=limit, tool=tool))
        return jsonify({"rollup": rollup, "actions": actions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/ops/preferences", methods=["GET", "POST"])
@login_required
def ops_preferences():
    try:
        from jarvis_ops import get_preferences, update_preferences

        if request.method == "POST":
            payload = request.get_json(silent=True) or {}
            return jsonify({"ok": True, "preferences": asyncio.run(update_preferences(payload))})
        return jsonify({"ok": True, "preferences": asyncio.run(get_preferences())})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/finances")
@login_required
def tiles_finances():
    """Home tile: recent spending transactions from cached Monarch data."""
    try:
        from financial_db import sync_financial_cache, get_recent_spending_transactions
        asyncio.run(sync_financial_cache(force_backfill=False))
        payload = asyncio.run(get_recent_spending_transactions(days=7, limit=7))
        items = [
            {
                "merchant": (t.get("merchant") or "Unknown")[:28],
                "amount": f"-${abs(float(t.get('amount') or 0)):,.2f}",
            }
            for t in payload.get("items", [])
        ]
        return jsonify({"count": int(payload.get("count", 0)), "items": items})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/research")
@login_required
def tiles_research():
    """Home tile: configured research topics."""
    try:
        from research_services import get_research_topics
        topics = asyncio.run(get_research_topics())
        items = [f"{topic.get('emoji', '•')} {topic.get('name', 'Topic')}" for topic in topics]
        return jsonify({
            "count": len(items),
            "items": items,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _parse_balances(result: str) -> dict:
    """Shared balance parser for finances section."""
    net_worth = None
    accounts  = []
    section = ""
    for raw in result.strip().splitlines():
        line = raw.strip().lstrip("•-– ").strip()
        if not line:
            continue
        if line.endswith(":") and line[:-1].lower() in {"assets", "liabilities"}:
            section = line[:-1].lower()
            continue
        m = re.search(r'\$([0-9,]+\.?\d*)\s*$', line)
        if not m:
            continue
        val = float(m.group(1).replace(",", ""))
        if abs(val) < 1:
            continue
        name = line[: m.start()].rstrip(": ").strip()
        value_str = m.group(0).strip()
        if "net worth" in name.lower():
            net_worth = value_str
            continue
        name = re.sub(r'\s*\(\.{2,}[^)]*\)', '', name).strip()
        name = re.sub(r'\s*\([^)]*\*+[^)]*\)', '', name).strip()
        if re.match(r'^\d+\s+\w', name):
            paren = re.search(r'\(([^)]+)\)\s*$', name)
            name = f"Home ({paren.group(1)})" if paren else "Real Estate"
        accounts.append({
            "name": name,
            "value": value_str,
            "value_num": val,
            "section": section or "assets",
            "classification": _classify_balance_account(name, section or "assets"),
        })

    summary = _summarize_balance_accounts(accounts, net_worth)
    return {
        "net_worth": net_worth,
        "accounts": accounts[:9],
        "all_accounts": accounts,
        "account_summary": summary,
    }


def _format_usd(value: float) -> str:
    return f"${value:,.2f}"


def _classify_balance_account(name: str, section: str) -> str:
    lower = (name or "").lower()

    if section == "liabilities" or any(token in lower for token in (
        "credit card", "mortgage", "loan", "heloc", "line of credit", "student loan",
    )):
        return "debt"

    if re.match(r"^\d+\s", lower) or any(token in lower for token in ("zillow", "home", "real estate")):
        return "real_estate"

    if any(token in lower for token in (
        "401", "403", "457", "ira", "roth", "retirement", "pension", "sep", "simple",
    )):
        return "retirement"

    if any(token in lower for token in ("checking", "savings", "cash management", "money market", "cash reserve")):
        return "cash"

    if any(token in lower for token in (
        "brokerage", "investment", "wealthfront", "betterment", "etrade", "e*trade",
        "robinhood", "vanguard", "schwab", "fidelity", "ameritrade",
    )):
        return "brokerage"

    return "other"


def _summarize_balance_accounts(accounts: list[dict], net_worth: str | None) -> dict:
    totals = {
        "retirement": 0.0,
        "brokerage": 0.0,
        "cash": 0.0,
        "real_estate": 0.0,
        "debt": 0.0,
        "other": 0.0,
    }
    for account in accounts:
        classification = account.get("classification") or "other"
        totals[classification] = totals.get(classification, 0.0) + float(account.get("value_num") or 0.0)

    investment_accounts = [
        account for account in accounts
        if account.get("classification") in {"retirement", "brokerage"}
    ]
    investment_accounts.sort(key=lambda item: float(item.get("value_num") or 0.0), reverse=True)

    invested_total = totals["retirement"] + totals["brokerage"]
    net_worth_num = 0.0
    if net_worth:
        match = re.search(r"\$([0-9,]+\.?\d*)", net_worth)
        if match:
            net_worth_num = float(match.group(1).replace(",", ""))

    return {
        "invested_total": round(invested_total, 2),
        "invested_total_fmt": _format_usd(invested_total),
        "retirement_total": round(totals["retirement"], 2),
        "retirement_total_fmt": _format_usd(totals["retirement"]),
        "brokerage_total": round(totals["brokerage"], 2),
        "brokerage_total_fmt": _format_usd(totals["brokerage"]),
        "cash_total": round(totals["cash"], 2),
        "cash_total_fmt": _format_usd(totals["cash"]),
        "real_estate_total": round(totals["real_estate"], 2),
        "real_estate_total_fmt": _format_usd(totals["real_estate"]),
        "debt_total": round(totals["debt"], 2),
        "debt_total_fmt": _format_usd(totals["debt"]),
        "other_total": round(totals["other"], 2),
        "other_total_fmt": _format_usd(totals["other"]),
        "net_worth": net_worth or "—",
        "net_worth_num": round(net_worth_num, 2),
        "investment_accounts": investment_accounts[:6],
        "retirement_accounts": [a for a in investment_accounts if a.get("classification") == "retirement"][:4],
        "brokerage_accounts": [a for a in investment_accounts if a.get("classification") == "brokerage"][:4],
        "tracked_investment_count": len(investment_accounts),
    }


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(float(v or 0.0) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    idx = (len(ordered) - 1) * pct
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return ordered[lo]
    weight = idx - lo
    return ordered[lo] * (1 - weight) + ordered[hi] * weight


def _build_investment_projection(balance_data: dict, dashboard: dict) -> dict:
    summary = balance_data.get("account_summary") or {}
    monthly_pnl = list((dashboard or {}).get("monthly_pnl") or [])
    history = monthly_pnl[:-1] if len(monthly_pnl) > 1 else monthly_pnl
    trailing = history[-6:] if history else monthly_pnl[-6:]
    positive_nets = [max(0.0, float(row.get("net") or 0.0)) for row in trailing]
    monthly_contribution = round((sum(positive_nets) / len(positive_nets)), 2) if positive_nets else 0.0

    start_balance = float(summary.get("retirement_total") or 0.0)
    if start_balance <= 0:
        start_balance = float(summary.get("invested_total") or 0.0)
    projection_target = "Retirement funds" if float(summary.get("retirement_total") or 0.0) > 0 else "Invested assets"

    annual_return = 0.07
    annual_volatility = 0.15
    simulations = 2000
    horizon_years = 30
    monthly_mean = (1 + annual_return) ** (1 / 12) - 1
    monthly_vol = annual_volatility / math.sqrt(12)
    rng = random.Random(int(start_balance) + int(monthly_contribution) + 2603)

    yearly_paths: dict[int, list[float]] = {year: [] for year in range(1, horizon_years + 1)}
    for _ in range(simulations):
        balance = start_balance
        for month in range(1, (horizon_years * 12) + 1):
            monthly_return = max(-0.95, rng.gauss(monthly_mean, monthly_vol))
            balance = max(0.0, (balance + monthly_contribution) * (1 + monthly_return))
            if month % 12 == 0:
                yearly_paths[month // 12].append(balance)

    chart = []
    for year in range(1, horizon_years + 1):
        values = yearly_paths.get(year, [])
        chart.append({
            "year": year,
            "p10": round(_percentile(values, 0.10), 2),
            "p50": round(_percentile(values, 0.50), 2),
            "p90": round(_percentile(values, 0.90), 2),
        })

    horizons = []
    for years in (10, 20, 30):
        values = yearly_paths.get(years, [])
        p10 = round(_percentile(values, 0.10), 2)
        p50 = round(_percentile(values, 0.50), 2)
        p90 = round(_percentile(values, 0.90), 2)
        horizons.append({
            "years": years,
            "p10": p10,
            "p50": p50,
            "p90": p90,
            "gain_p50": round(p50 - start_balance, 2),
        })

    double_target = start_balance * 2 if start_balance > 0 else 0.0
    year_20 = yearly_paths.get(20, [])
    year_30 = yearly_paths.get(30, [])
    probability_double_20y = (
        round(sum(1 for value in year_20 if value >= double_target) / len(year_20) * 100.0, 1)
        if year_20 and double_target > 0 else 0.0
    )
    probability_two_million_30y = (
        round(sum(1 for value in year_30 if value >= 2_000_000) / len(year_30) * 100.0, 1)
        if year_30 else 0.0
    )

    return {
        "target_label": projection_target,
        "starting_balance": round(start_balance, 2),
        "starting_balance_fmt": _format_usd(start_balance),
        "monthly_contribution": monthly_contribution,
        "monthly_contribution_fmt": _format_usd(monthly_contribution),
        "annual_return_assumption": round(annual_return * 100, 1),
        "annual_volatility_assumption": round(annual_volatility * 100, 1),
        "simulations": simulations,
        "chart": chart,
        "horizons": horizons,
        "probability_double_20y": probability_double_20y,
        "probability_two_million_30y": probability_two_million_30y,
        "note": "Illustrative only. Assumes the trailing 6-month positive net cash flow keeps getting invested each month.",
    }


def _build_finances_payload(force_backfill: bool = False) -> dict:
    from financial_db import sync_financial_cache, build_finance_dashboard

    sync = asyncio.run(sync_financial_cache(force_backfill=force_backfill))
    dashboard = asyncio.run(build_finance_dashboard(months=15, control_window=6))

    balances = {"net_worth": "—", "accounts": []}
    try:
        from tools import get_account_balances
        result = asyncio.run(get_account_balances({}))
        parsed = _parse_balances(result)
        if parsed.get("net_worth"):
            balances = parsed
    except Exception as e:
        balances["balances_error"] = str(e)

    projection = _build_investment_projection(balances, dashboard)

    return {
        **balances,
        "sync": sync,
        "dashboard": dashboard,
        "investment_projection": projection,
    }


@app.route("/api/sections/finances")
@login_required
def section_finances():
    """Finances section page: balances + cached spend analytics."""
    try:
        return jsonify(_build_finances_payload(force_backfill=False))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/morning_brief/run", methods=["POST"])
@login_required
def run_morning_brief_section():
    """Trigger the full morning brief from the web dashboard."""
    try:
        from morning_brief import run_morning_brief
        result = asyncio.run(run_morning_brief())
        return jsonify({
            "text": result.get("text", ""),
            "sections": result.get("sections", {}),
            "generated_at": result.get("generated_at", ""),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/finances/sync", methods=["POST"])
@login_required
def section_finances_sync():
    """Manual finance cache sync (optionally force 1-year backfill)."""
    body = request.get_json(silent=True) or {}
    force_backfill = bool(body.get("force_backfill"))
    try:
        return jsonify(_build_finances_payload(force_backfill=force_backfill))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/pool")
@login_required
def tiles_pool():
    """Home tile: pool health snapshot from DB (fast, no live API calls)."""
    import aiosqlite
    from datetime import date as _date

    async def _fetch():
        async with db_connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM pool_readings ORDER BY logged_at DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            reading = dict(row) if row else {}
            async with db.execute(
                "SELECT * FROM pool_equipment_snapshots ORDER BY logged_at DESC LIMIT 1"
            ) as cur:
                snap = await cur.fetchone()
            equipment = dict(snap) if snap else {}
        return reading, equipment

    try:
        reading, equipment = asyncio.run(_fetch())
        alerts = []
        IDEAL = {"fc": (2.0, 4.0, "FC"), "ph": (7.4, 7.6, "pH"),
                 "ta": (80, 120, "TA"), "salt": (2700, 3400, "Salt")}
        for key, (lo, hi, label) in IDEAL.items():
            val = reading.get(key)
            if val is not None:
                try:
                    v = float(val)
                    if v < lo:
                        alerts.append(f"{label} low ({v})")
                    elif v > hi:
                        alerts.append(f"{label} high ({v})")
                except (ValueError, TypeError):
                    pass

        test_date = (reading.get("logged_at") or "")[:10] or None
        days_ago = None
        if test_date:
            try:
                days_ago = (_date.today() - _date.fromisoformat(test_date)).days
            except Exception:
                pass

        return jsonify({
            "score":           reading.get("overall_score"),
            "days_ago":        days_ago,
            "test_date":       test_date,
            "water_temp":      equipment.get("water_temp") or reading.get("water_temp"),
            "pump":            equipment.get("pump_state"),
            "chlorinator_pct": equipment.get("chlorinator_pct"),
            "alerts":          alerts[:3],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/pool")
@login_required
def section_pool():
    """Pool section: chemistry readings, equipment snapshot, chemical additions."""
    import aiosqlite

    async def _fetch():
        async with db_connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM pool_readings ORDER BY logged_at DESC LIMIT 5"
            ) as cur:
                readings = [dict(r) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT * FROM pool_equipment_snapshots ORDER BY logged_at DESC LIMIT 1"
            ) as cur:
                snap = await cur.fetchone()
            equipment = dict(snap) if snap else {}
            async with db.execute(
                "SELECT * FROM pool_chemical_log ORDER BY logged_at DESC LIMIT 8"
            ) as cur:
                chemicals = [dict(r) for r in await cur.fetchall()]
        return readings, equipment, chemicals

    try:
        readings, equipment, chemicals = asyncio.run(_fetch())
        equipment.pop("raw_json", None)
        return jsonify({
            "readings":  readings,
            "equipment": equipment,
            "chemicals": chemicals,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tiles/fitness")
@login_required
def tiles_fitness():
    """Home tile: today's WHOOP recovery snapshot."""
    import aiosqlite
    async def _fetch():
        async with db_connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM fitness_whoop_daily ORDER BY date DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            return dict(row) if row else {}
    try:
        data = asyncio.run(_fetch())
        return jsonify({
            "recovery_score":     data.get("recovery_score"),
            "hrv":                data.get("hrv"),
            "resting_hr":         data.get("resting_hr"),
            "sleep_performance":  data.get("sleep_performance"),
            "sleep_duration_hrs": data.get("sleep_duration_hrs"),
            "strain":             data.get("strain"),
            "date":               data.get("date"),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sections/fitness")
@login_required
def section_fitness():
    """Fitness section: today's WHOOP data + PRs + recent workouts."""
    import aiosqlite
    async def _fetch():
        async with db_connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM fitness_whoop_daily ORDER BY date DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
            today = dict(row) if row else {}
            async with db.execute(
                """SELECT exercise, MAX(value) as value, unit, MAX(logged_at) as logged_at
                   FROM fitness_prs GROUP BY exercise ORDER BY logged_at DESC"""
            ) as cur:
                prs = [dict(r) for r in await cur.fetchall()]
            async with db.execute(
                "SELECT * FROM fitness_workouts ORDER BY logged_at DESC LIMIT 10"
            ) as cur:
                workouts = [dict(r) for r in await cur.fetchall()]
        return today, prs, workouts
    try:
        today, prs, workouts = asyncio.run(_fetch())
        return jsonify({"today": today, "prs": prs, "workouts": workouts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/whoop/authorize")
@login_required
def whoop_authorize():
    """Redirect to WHOOP OAuth authorization page."""
    import secrets, urllib.parse
    client_id    = os.getenv("WHOOP_CLIENT_ID", "")
    redirect_uri = os.getenv("WHOOP_REDIRECT_URI", "")
    if not client_id:
        return "WHOOP_CLIENT_ID not set in .env", 500
    state = secrets.token_urlsafe(16)
    session["whoop_state"] = state
    scope = "offline read:recovery read:sleep read:workout read:body_measurement read:cycles read:profile"
    params = urllib.parse.urlencode({
        "client_id":     client_id,
        "redirect_uri":  redirect_uri,
        "response_type": "code",
        "scope":         scope,
        "state":         state,
    })
    return redirect(f"https://api.prod.whoop.com/oauth/oauth2/auth?{params}")


@app.route("/whoop/callback")
@login_required
def whoop_callback():
    """Handle WHOOP OAuth callback — exchange code for tokens."""
    import json, time, urllib.parse, urllib.request
    # Validate state
    returned_state = request.args.get("state", "")
    expected_state = session.pop("whoop_state", None)
    if not expected_state or returned_state != expected_state:
        return f"<h2>Auth failed: state mismatch</h2><p>Try <a href='/whoop/authorize'>authorizing again</a>.</p>", 400

    code = request.args.get("code")
    if not code:
        return f"Error: {request.args.get('error', 'no code returned')}", 400

    client_id     = os.getenv("WHOOP_CLIENT_ID", "")
    client_secret = os.getenv("WHOOP_CLIENT_SECRET", "")
    redirect_uri  = os.getenv("WHOOP_REDIRECT_URI", "")

    data = urllib.parse.urlencode({
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  redirect_uri,
        "client_id":     client_id,
        "client_secret": client_secret,
    }).encode()

    try:
        req = urllib.request.Request(
            "https://api.prod.whoop.com/oauth/oauth2/token",
            data=data, method="POST"
        )
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(req) as resp:
            tokens = json.loads(resp.read())
        tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)

        tokens_path = os.path.join(ROOT, "data", "whoop_tokens.json")
        with open(tokens_path, "w") as f:
            json.dump(tokens, f, indent=2)
        if os.name != "nt":
            os.chmod(tokens_path, 0o600)

        return "<h2>✅ WHOOP connected!</h2><p>Tokens saved. You can close this tab and return to Jarvis.</p>"
    except Exception as e:
        return f"<h2>Auth failed</h2><pre>{e}</pre>", 500


@app.route("/fitness")
@login_required
def fitness_page():
    return send_from_directory(os.path.dirname(os.path.abspath(__file__)), "fitness.html")


@app.route("/api/whoop/history")
@login_required
def whoop_history():
    from fitness_services import sync_history, get_history_from_db
    force_sync = request.args.get("sync", "false").lower() == "true"
    days = int(request.args.get("days", 30))
    try:
        if force_sync:
            rows = asyncio.run(sync_history(days))
        else:
            rows = asyncio.run(get_history_from_db(days))
            if not rows:
                rows = asyncio.run(sync_history(days))
        return jsonify({"records": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/inbox/triage")
@login_required
def inbox_triage_latest():
    """Latest inbox triage run from DB."""
    try:
        from inbox_triage import get_latest_triage
        triage = asyncio.run(get_latest_triage())
        if not triage:
            return jsonify({"found": False})
        return jsonify({
            "found":          True,
            "run_at":         triage.get("run_at"),
            "emails_scanned": triage.get("emails_scanned", 0),
            "starred":        triage.get("starred", 0),
            "archived":       triage.get("archived", 0),
            "left_alone":     triage.get("left_alone", 0),
            "summary":        triage.get("summary"),
            "created_at":     triage.get("created_at"),
            "actions":        triage.get("actions", []),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/inbox/triage/run", methods=["POST"])
@login_required
def inbox_triage_run():
    """Trigger a fresh inbox triage scan on demand."""
    try:
        from inbox_triage import run_triage, get_latest_triage
        asyncio.run(run_triage())
        triage = asyncio.run(get_latest_triage())
        return jsonify({
            "found":          True,
            "run_at":         triage.get("run_at"),
            "emails_scanned": triage.get("emails_scanned", 0),
            "starred":        triage.get("starred", 0),
            "archived":       triage.get("archived", 0),
            "left_alone":     triage.get("left_alone", 0),
            "summary":        triage.get("summary"),
            "created_at":     triage.get("created_at"),
            "actions":        triage.get("actions", []),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/context")
@login_required
def get_context():
    import json
    ctx_path = os.path.join(ROOT, "data", "context.json")
    try:
        with open(ctx_path) as f:
            return jsonify(json.load(f))
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080, debug=False)
