"""
Morning briefing generator.

Fetches calendar, email, and tasks concurrently, synthesizes with Claude Sonnet,
saves to Notion, and returns the formatted text for Telegram delivery.
"""
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from llm import complete_text

logger = logging.getLogger("jarvis")
ET = ZoneInfo("America/New_York")

BRIEFING_PROMPT = """You are Jarvis preparing Matt's morning executive briefing.

Be direct and actionable. No fluff. Structure it clearly:
- 2-3 sentence day overview
- Calendar: one combined schedule from Google and iCloud, times + event names only
- Email: only items needing action (skip newsletters/automated)
- Tasks: prioritized list
- One sentence on what to tackle first

Use plain text. No markdown headers with #. Use emoji sparingly for section breaks."""


def _parse_google_calendar_for_briefing(raw: str) -> list[dict]:
    if not raw or raw.startswith("No events on") or raw.startswith("(calendar unavailable)"):
        return []

    items: list[dict] = []
    for line in raw.splitlines():
        match = re.match(r"^\s*(All day|\d{1,2}:\d{2}\s[AP]M)\s+—\s+(.+)$", line.strip())
        if not match:
            continue
        time_label = match.group(1)
        summary = match.group(2).split(" @ ", 1)[0].strip()
        if time_label == "All day":
            sort_key = (0, 0)
        else:
            try:
                dt = datetime.strptime(time_label, "%I:%M %p")
                sort_key = (1, dt.hour * 60 + dt.minute)
            except ValueError:
                sort_key = (2, 0)
        items.append({
            "time_label": time_label,
            "summary": summary,
            "source": "Google",
            "sort_key": sort_key,
        })
    return items


def _parse_icloud_calendar_for_briefing(events: list[dict]) -> list[dict]:
    items: list[dict] = []
    for event in events or []:
        summary = (event.get("summary") or "Untitled").strip()
        calendar_name = (event.get("calendar_name") or "").strip()
        if event.get("all_day"):
            time_label = "All day"
            sort_key = (0, 0)
        else:
            try:
                start_dt = datetime.fromisoformat(event.get("start", ""))
                time_label = start_dt.astimezone(ET).strftime("%-I:%M %p")
                sort_key = (1, start_dt.astimezone(ET).hour * 60 + start_dt.astimezone(ET).minute)
            except Exception:
                time_label = "Time TBD"
                sort_key = (2, 0)
        source = f"iCloud/{calendar_name}" if calendar_name else "iCloud"
        items.append({
            "time_label": time_label,
            "summary": summary,
            "source": source,
            "sort_key": sort_key,
        })
    return items


def _build_calendar_context(google_calendar_raw: str, icloud_events: list[dict]) -> str:
    items = _parse_google_calendar_for_briefing(google_calendar_raw)
    items.extend(_parse_icloud_calendar_for_briefing(icloud_events))
    if not items:
        return "No calendar events today."

    items.sort(key=lambda item: (item["sort_key"], item["summary"].lower()))
    lines = []
    for item in items:
        lines.append(f"- {item['time_label']} — {item['summary']} [{item['source']}]")
    return "\n".join(lines)


async def generate_briefing() -> tuple[str, str]:
    """
    Fetch data from all sources concurrently, synthesize with Sonnet.
    Returns (briefing_text, notion_title).
    """
    from google_services import get_calendar_events, get_emails
    from icloud_calendar import get_todays_events
    from intentionality import generate_digest_nudge
    from tools import _get_tasks_sync

    now = datetime.now(ET)
    date_label = now.strftime("%A, %B %d, %Y")

    # Fetch daily context concurrently, including both calendar systems.
    google_calendar_result, icloud_calendar_result, email_result, tasks_result, intentionality_result = await asyncio.gather(
        get_calendar_events({"date": "today"}),
        get_todays_events(),
        get_emails({"query": "is:unread", "max_results": 10}),
        asyncio.to_thread(_get_tasks_sync, "today | overdue"),
        generate_digest_nudge(),
        return_exceptions=True,
    )

    def safe(result, label):
        if isinstance(result, Exception):
            logger.warning(f"Briefing {label} error: {result}")
            return f"({label} unavailable)"
        return result

    google_calendar_data = safe(google_calendar_result, "google calendar")
    icloud_calendar_data = safe(icloud_calendar_result, "icloud calendar")
    email_data = safe(email_result, "email")
    tasks_data = safe(tasks_result, "tasks")
    intentionality_data = safe(intentionality_result, "intentionality")
    calendar_data = _build_calendar_context(
        google_calendar_data if isinstance(google_calendar_data, str) else "",
        icloud_calendar_data if isinstance(icloud_calendar_data, list) else [],
    )

    user_content = f"""Today is {date_label}.

CALENDAR:
{calendar_data}

UNREAD EMAIL:
{email_data}

TASKS (today + overdue):
{tasks_data}

Generate Matt's morning briefing."""

    briefing_text = await complete_text(
        model="report",
        max_tokens=1500,
        system=BRIEFING_PROMPT,
        user_content=user_content,
    )
    if intentionality_data and not str(intentionality_data).startswith("(intentionality unavailable)"):
        briefing_text = f"{briefing_text}\n\n🧠 Intentionality\n{intentionality_data}"
    notion_title = f"Morning Briefing — {now.strftime('%b %d, %Y')}"

    return briefing_text, notion_title


async def save_briefing_to_notion(title: str, content: str):
    """Save the briefing as a digest row in Notion."""
    from notion_services import (
        build_habits_reference_blocks,
        build_plaintext_blocks,
        save_digest_page,
    )
    try:
        today = datetime.now(ET).date().isoformat()
        children = build_plaintext_blocks(content)
        habits_blocks = build_habits_reference_blocks(today)
        if habits_blocks:
            children.append({"object": "block", "type": "divider", "divider": {}})
            children.extend(habits_blocks)
        await save_digest_page({
            "title": title,
            "digest_type": "Morning Briefing",
            "digest_date": today,
            "children": children,
        })
        logger.info(f"Briefing saved to Notion: {title}")
    except Exception as e:
        logger.error(f"Failed to save briefing to Notion: {e}")


async def run_briefing() -> str:
    """
    Full pipeline: generate + save to Notion.
    Returns the briefing text for Telegram delivery.
    """
    briefing_text, notion_title = await generate_briefing()
    await save_briefing_to_notion(notion_title, briefing_text)
    return briefing_text
