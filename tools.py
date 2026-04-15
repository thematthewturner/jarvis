import json
import os
import asyncio
from datetime import datetime
import aiosqlite
from memory import DB_PATH, db_connect


def _require_todoist_token() -> str:
    token = os.getenv("TODOIST_API_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TODOIST_API_TOKEN is not configured.")
    return token

_HANDLERS: dict = {}


def _build_handlers() -> dict:
    # Lazy-initialized so that import ordering stays simple (the iCloud/Google
    # sub-modules below are imported at module load).
    if _HANDLERS:
        return _HANDLERS
    _HANDLERS.update({
        "save_note": save_note,
        "get_current_time": get_current_time,
        "add_task": add_task,
        "update_task": update_task,
        "get_tasks": get_tasks,
        "get_calendar_events": get_calendar_events,
        "get_emails": get_emails,
        "send_email": send_email,
        "archive_gmail_email":   archive_gmail_email,
        "star_gmail_email":      star_gmail_email,
        "mark_gmail_email_read": mark_gmail_email_read,
        "search_drive": search_drive,
        "read_drive_file": read_drive_file,
        "save_notion_note": save_notion_note,
        "search_notion": search_notion,
        "read_notion_page": read_notion_page,
        "save_intentionality_signal": save_intentionality_signal_tool,
        "get_intentionality_status": get_intentionality_status_tool,
        "get_account_balances": get_account_balances,
        "get_recent_transactions": get_recent_transactions,
        "get_budget_summary": get_budget_summary,
        "get_utility_bills": get_utility_bills,
        "get_finance_digest": get_finance_digest,
        "get_weekly_financial_digest": get_weekly_financial_digest,
        "get_spending_trend": get_spending_trend,
        # iCloud
        "get_icloud_emails":      get_icloud_emails,
        "search_icloud_emails":   search_icloud_emails,
        "flag_icloud_email":      flag_icloud_email,
        "archive_icloud_email":   archive_icloud_email,
        "move_icloud_email":      move_icloud_email,
        "trash_icloud_email":     trash_icloud_email,
        "mark_icloud_email_read": mark_icloud_email_read,
        "get_icloud_calendar":    get_icloud_calendar,
        "create_icloud_event":    create_icloud_event,
        "send_icloud_email":      send_icloud_email,
        # Pool
        "log_pool_chemistry":   log_pool_chemistry,
        "log_pool_chemical":    log_pool_chemical,
        "get_pool_status":      get_pool_status_tool,
        # Fitness
        "get_fitness_status":   get_fitness_status,
        "log_pr":               log_pr_tool,
        "get_prs":              get_prs_tool,
        "log_workout":          log_workout_tool,
    })
    return _HANDLERS


async def handle_tool_call(tool_name: str, tool_input: dict, *, skill: str = "chat") -> str:
    handlers = _build_handlers()
    handler = handlers.get(tool_name)
    if not handler:
        return f"Unknown tool: {tool_name}"
    # Route through the reliability layer: read tools pass through untouched,
    # write tools get audit logging + idempotency dedupe.
    from jarvis_reliability import execute_and_log

    return await execute_and_log(
        tool=tool_name,
        args=tool_input or {},
        handler=handler,
        skill=skill,
    )

async def save_note(input: dict) -> str:
    category = input["category"]
    content = input["content"]
    metadata = json.dumps(input.get("metadata", {}))
    async with db_connect() as db:
        await db.execute(
            "INSERT INTO notes (category, content, metadata, timestamp) VALUES (?, ?, ?, ?)",
            (category, content, metadata, datetime.utcnow().isoformat())
        )
        await db.commit()
    return f"Saved {category}: {content}"

async def get_current_time(input: dict) -> str:
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.strftime("%A, %B %d, %Y at %I:%M %p ET")

def _add_task_sync(content: str, due_string: str) -> str:
    from todoist_api_python.api import TodoistAPI
    api = TodoistAPI(_require_todoist_token())
    kwargs = {"content": content}
    if due_string:
        kwargs["due_string"] = due_string
    task = api.add_task(**kwargs)
    due_part = f" (due: {task.due.string})" if task.due else ""
    return f"Task added: {task.content}{due_part} [task_id:{task.id}]"

async def add_task(input: dict) -> str:
    return await asyncio.to_thread(_add_task_sync, input["content"], input.get("due_string", ""))

def _update_task_sync(task_id: str, content: str, due_string: str, priority: int) -> str:
    from todoist_api_python.api import TodoistAPI
    api = TodoistAPI(_require_todoist_token())
    kwargs = {}
    if content:    kwargs["content"]    = content
    if due_string: kwargs["due_string"] = due_string
    if priority:   kwargs["priority"]   = priority  # 1=P4(normal), 2=P3, 3=P2, 4=P1(urgent)
    api.update_task(task_id, **kwargs)
    parts = []
    if content:    parts.append(f"content='{content}'")
    if due_string: parts.append(f"due='{due_string}'")
    if priority:   parts.append(f"priority=P{5-priority}")
    return f"Task {task_id} updated: {', '.join(parts)}"

async def update_task(input: dict) -> str:
    return await asyncio.to_thread(
        _update_task_sync,
        input["task_id"],
        input.get("content", ""),
        input.get("due_string", ""),
        input.get("priority", 0),
    )

def _get_tasks_sync(filter_str: str) -> str:
    from todoist_api_python.api import TodoistAPI
    from datetime import date
    api = TodoistAPI(_require_todoist_token())
    all_tasks = []
    for page in api.get_tasks():
        all_tasks.extend(page)
    today = date.today()
    if filter_str in ("today | overdue", "today", "overdue"):
        tasks = [t for t in all_tasks if t.due and t.due.date <= today]
    else:
        tasks = all_tasks
    if not tasks:
        return "No tasks found."
    lines = []
    for t in sorted(tasks, key=lambda x: x.due.date if x.due else "9999"):
        line = f"- {t.content}"
        if t.due:
            line += f" (due: {t.due.string})"
        lines.append(line)
    return "\n".join(lines)

async def get_tasks(input: dict) -> str:
    filter_str = input.get("filter", "today | overdue")
    return await asyncio.to_thread(_get_tasks_sync, filter_str)


async def save_intentionality_signal_tool(input: dict) -> str:
    from intentionality import save_signal_from_chat

    return await save_signal_from_chat(
        signal_type=input["signal_type"],
        content=input["content"],
        metadata=input.get("metadata") or {},
    )


async def get_intentionality_status_tool(input: dict) -> str:
    from intentionality import format_intentionality_status

    return await format_intentionality_status()


# ── Google ────────────────────────────────────────────────────────────────────

from google_services import (
    get_calendar_events,
    get_emails,
    send_email,
    search_drive,
    read_drive_file,
    archive_gmail_email,
    star_gmail_email,
    mark_gmail_email_read,
)


# ── Notion ────────────────────────────────────────────────────────────────────

from notion_services import (
    save_notion_note,
    search_notion,
    read_notion_page,
)


# ── Monarch Money ─────────────────────────────────────────────────────────────

from monarch_services import (
    get_account_balances,
    get_recent_transactions,
    get_budget_summary,
)


async def get_utility_bills(input: dict) -> str:
    from utility_bills import format_monthly_utility_bills

    return await format_monthly_utility_bills(
        year=input.get("year"),
        month=input.get("month"),
        refresh=bool(input.get("refresh", True)),
    )


async def get_finance_digest(input: dict) -> str:
    from finance_inbox import get_finance_digest as _get_finance_digest

    return await _get_finance_digest(
        days_ahead=int(input.get("days_ahead", 10)),
        recent_days=int(input.get("recent_days", 7)),
        limit=int(input.get("limit", 20)),
    )


async def get_weekly_financial_digest(input: dict) -> str:
    from finance_weekly_digest import run_weekly_financial_digest

    return await run_weekly_financial_digest(save_to_notion=bool(input.get("save_to_notion", False)))


async def get_spending_trend(input: dict) -> str:
    from financial_db import get_spending_trend_summary

    return await get_spending_trend_summary(weeks=int(input.get("weeks", 4)))


# ── iCloud ────────────────────────────────────────────────────────────────────

async def get_icloud_emails(input: dict) -> str:
    from icloud_mail import fetch_unread_emails, prepare_email_for_claude
    since_hours = input.get("since_hours", 24)
    emails = await fetch_unread_emails(since_hours=since_hours)
    if not emails:
        return f"No unread emails in the last {since_hours} hours."
    parts = [prepare_email_for_claude(e, max_body_length=800) for e in emails[:8]]
    return f"Found {len(emails)} unread email(s):\n\n" + "\n\n---\n\n".join(parts)


async def search_icloud_emails(input: dict) -> str:
    from icloud_mail import search_emails, prepare_email_for_claude
    query = input.get("query", "")
    since_days = input.get("since_days", 7)
    emails = await search_emails(query=query, since_days=since_days)
    if not emails:
        return f"No emails found matching '{query}' in the last {since_days} days."
    parts = [prepare_email_for_claude(e, max_body_length=800) for e in emails[:6]]
    return f"Found {len(emails)} email(s) matching '{query}':\n\n" + "\n\n---\n\n".join(parts)


async def get_icloud_calendar(input: dict) -> str:
    from icloud_calendar import (
        get_todays_events, get_weeks_events, get_upcoming_events,
        get_events_for_date_range, format_events_for_claude,
    )
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")

    date_arg = input.get("date", "today").lower()
    days = int(input.get("days", 14) or 14)
    cal_names = input.get("calendar_names") or None

    if date_arg == "today":
        events = await get_todays_events(cal_names)
        label = "Today's iCloud calendar"
    elif date_arg == "tomorrow":
        tomorrow = datetime.now(TZ) + timedelta(days=1)
        start = tomorrow.replace(hour=0, minute=0, second=0, microsecond=0)
        end   = tomorrow.replace(hour=23, minute=59, second=59)
        events = await get_events_for_date_range(start, end, cal_names)
        label = "Tomorrow's iCloud calendar"
    elif date_arg == "week":
        events = await get_weeks_events(cal_names)
        label = "This week's iCloud calendar"
    elif date_arg == "upcoming":
        events = await get_upcoming_events(days=days, calendar_names=cal_names)
        label = f"Upcoming iCloud calendar ({days} days)"
    else:
        try:
            day = datetime.fromisoformat(date_arg).replace(tzinfo=TZ)
            start = day.replace(hour=0, minute=0, second=0)
            end   = day.replace(hour=23, minute=59, second=59)
            events = await get_events_for_date_range(start, end, cal_names)
            label = f"iCloud calendar for {date_arg}"
        except ValueError:
            return f"Could not parse date: {date_arg}"

    return f"{label}:\n{format_events_for_claude(events)}"


async def create_icloud_event(input: dict) -> str:
    from icloud_calendar import create_event
    from datetime import datetime
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("America/New_York")

    try:
        start = datetime.fromisoformat(input["start"]).replace(tzinfo=TZ)
        end   = datetime.fromisoformat(input["end"]).replace(tzinfo=TZ)
    except Exception as e:
        return f"Could not parse start/end datetime: {e}"

    uid = await create_event(
        summary=input["summary"],
        start=start,
        end=end,
        calendar_name=input.get("calendar_name", "Home"),
        location=input.get("location", ""),
        description=input.get("description", ""),
    )
    return f"Event created: '{input['summary']}' on {start.strftime('%b %-d at %-I:%M %p')} (UID: {uid})"


# ── Pool ──────────────────────────────────────────────────────────────────────

async def log_pool_chemistry(input: dict) -> str:
    from pool_services import log_chemistry
    allowed = ("fc", "tc", "ph", "ta", "salt", "cya", "ch",
               "water_temp", "iron", "copper", "phosphate", "notes")
    kwargs = {k: v for k, v in input.items() if k in allowed and v is not None}
    return await log_chemistry(**kwargs)


async def log_pool_chemical(input: dict) -> str:
    from pool_services import log_chemical_addition
    chemical = input.get("chemical", "")
    amount = input.get("amount", "")
    notes = input.get("notes")
    return await log_chemical_addition(chemical, amount, notes)


async def get_pool_status_tool(input: dict) -> str:
    from pool_services import get_pool_status
    return await get_pool_status()


# ── Fitness ───────────────────────────────────────────────────────────────────

async def get_fitness_status(input: dict) -> str:
    from fitness_services import get_fitness_status as _get
    return await _get()


async def log_pr_tool(input: dict) -> str:
    from fitness_services import log_pr
    return await log_pr(
        exercise=input["exercise"],
        value=float(input["value"]),
        unit=input.get("unit", "lbs"),
        notes=input.get("notes"),
    )


async def get_prs_tool(input: dict) -> str:
    from fitness_services import get_prs
    return await get_prs(exercise=input.get("exercise"))


async def log_workout_tool(input: dict) -> str:
    from fitness_services import log_workout
    return await log_workout(
        description=input["description"],
        result=input.get("result"),
        rx=input.get("rx", True),
        workout_type=input.get("type", "crossfit"),
        notes=input.get("notes"),
    )


async def send_icloud_email(input: dict) -> str:
    from icloud_mail import send_email
    ok = await send_email(
        to=input["to"],
        subject=input["subject"],
        body=input["body"],
    )
    if ok:
        return f"Email sent to {input['to']}: {input['subject']}"
    return "Failed to send email — check SMTP credentials."


async def flag_icloud_email(input: dict) -> str:
    from icloud_mail import flag_email
    uid     = input["uid"]
    starred = bool(input.get("starred", True))
    result  = await flag_email(uid, flagged=starred)
    return f"Email {uid}: {result}"


async def archive_icloud_email(input: dict) -> str:
    from icloud_mail import archive_email
    uid = input["uid"]
    result = await archive_email(uid)
    return f"Email {uid}: {result}"


async def move_icloud_email(input: dict) -> str:
    from icloud_mail import move_email
    uid    = input["uid"]
    folder = input["folder"]
    result = await move_email(uid, folder)
    return f"Email {uid}: {result}"


async def trash_icloud_email(input: dict) -> str:
    from icloud_mail import trash_email
    uid = input["uid"]
    result = await trash_email(uid)
    return f"Email {uid}: {result}"


async def mark_icloud_email_read(input: dict) -> str:
    from icloud_mail import mark_email_read
    uid  = input["uid"]
    seen = input.get("seen", True)
    result = await mark_email_read(uid, seen)
    return f"Email {uid}: {result}"
