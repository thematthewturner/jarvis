"""
iCloud Calendar — CalDAV integration for Jarvis.
Uses caldav + recurring-ical-events for proper recurring event expansion.

iCloud CalDAV quirk: RRULE data is returned instead of individual occurrences,
so we must expand manually using recurring_ical_events.
"""
import asyncio
import logging
import uuid
from datetime import datetime, timedelta, date, timezone
from typing import Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger("jarvis")

CALDAV_URL = "https://caldav.icloud.com/"
TZ = ZoneInfo("America/New_York")
MAX_RETRIES = 3


def _get_credentials() -> tuple[str, str]:
    import os
    email = os.getenv("ICLOUD_EMAIL", "")
    password = os.getenv("ICLOUD_APP_PASSWORD", "").replace("-", "")
    if not email or not password:
        raise RuntimeError("ICLOUD_EMAIL or ICLOUD_APP_PASSWORD not set in .env")
    return email, password


def _get_client():
    """Create and return an authenticated caldav DAVClient."""
    import caldav
    email, password = _get_credentials()
    return caldav.DAVClient(url=CALDAV_URL, username=email, password=password)


def _to_aware(dt) -> datetime:
    """Ensure a datetime or date is timezone-aware in US/Eastern."""
    if isinstance(dt, datetime):
        if dt.tzinfo is None:
            return dt.replace(tzinfo=TZ)
        return dt.astimezone(TZ)
    # date → midnight ET
    return datetime(dt.year, dt.month, dt.day, tzinfo=TZ)


def _parse_event(event, calendar_name: str) -> dict:
    """Parse an icalendar event component into a clean dict."""
    try:
        dtstart = event.get("DTSTART")
        dtend   = event.get("DTEND")

        start = dtstart.dt if dtstart else None
        end   = dtend.dt   if dtend   else None

        all_day = isinstance(start, date) and not isinstance(start, datetime)

        return {
            "summary":       str(event.get("SUMMARY", "Untitled")),
            "start":         _to_aware(start).isoformat() if start else None,
            "end":           _to_aware(end).isoformat()   if end   else None,
            "location":      str(event.get("LOCATION", "")) or None,
            "description":   str(event.get("DESCRIPTION", "")) or None,
            "calendar_name": calendar_name,
            "all_day":       all_day,
            "uid":           str(event.get("UID", "")),
        }
    except Exception as e:
        logger.warning(f"Could not parse calendar event: {e}")
        return {}


def _get_calendars_sync() -> list[dict]:
    client = _get_client()
    principal = client.principal()
    cals = []
    for cal in principal.calendars():
        cals.append({
            "name":        cal.name or "Unknown",
            "calendar_id": str(cal.id),
            "url":         str(cal.url),
        })
    return cals


def _fetch_events_sync(start: datetime, end: datetime,
                       calendar_names: Optional[list[str]] = None) -> list[dict]:
    """Core sync fetch: gets events from CalDAV and expands recurring events."""
    import caldav
    import recurring_ical_events
    from icalendar import Calendar as iCal

    client = _get_client()
    principal = client.principal()
    all_calendars = principal.calendars()

    # Filter by name if requested
    if calendar_names:
        lower = {n.lower() for n in calendar_names}
        all_calendars = [c for c in all_calendars if (c.name or "").lower() in lower]

    events = []

    for cal in all_calendars:
        cal_name = cal.name or "Unknown"
        try:
            results = cal.search(start=start, end=end, event=True, expand=False)
            for obj in results:
                try:
                    raw = obj.data
                    if isinstance(raw, str):
                        raw = raw.encode("utf-8")
                    ical = iCal.from_ical(raw)
                    expanded = recurring_ical_events.of(ical).between(start, end)
                    for ev in expanded:
                        parsed = _parse_event(ev, cal_name)
                        if parsed.get("summary"):
                            events.append(parsed)
                except Exception as e:
                    logger.debug(f"Skipping event in {cal_name}: {e}")
        except Exception as e:
            logger.warning(f"Could not fetch from calendar '{cal_name}': {e}")

    return sorted(events, key=lambda e: e.get("start") or "")


async def get_calendars() -> list[dict]:
    """List all iCloud calendars."""
    for attempt in range(MAX_RETRIES):
        try:
            return await asyncio.to_thread(_get_calendars_sync)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"get_calendars failed: {e}")
                return []
            import time; time.sleep(2 ** attempt)
    return []


async def get_events_for_date_range(
    start: datetime,
    end: datetime,
    calendar_names: Optional[list[str]] = None,
) -> list[dict]:
    """Fetch all events in a date range, with recurring events expanded."""
    start = _to_aware(start)
    end   = _to_aware(end)
    for attempt in range(MAX_RETRIES):
        try:
            return await asyncio.to_thread(_fetch_events_sync, start, end, calendar_names)
        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                logger.error(f"get_events_for_date_range failed: {e}")
                return []
            import time; time.sleep(2 ** attempt)
    return []


async def get_todays_events(calendar_names: Optional[list[str]] = None) -> list[dict]:
    """Get all events for today."""
    now   = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = now.replace(hour=23, minute=59, second=59, microsecond=0)
    return await get_events_for_date_range(start, end, calendar_names)


async def get_weeks_events(calendar_names: Optional[list[str]] = None) -> list[dict]:
    """Get all events from today through the next 7 days."""
    now   = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = start + timedelta(days=7)
    return await get_events_for_date_range(start, end, calendar_names)


async def get_upcoming_events(days: int = 14, calendar_names: Optional[list[str]] = None) -> list[dict]:
    """Get upcoming events from today through the next N days."""
    window_days = max(1, min(int(days or 14), 60))
    now = datetime.now(TZ)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=window_days)
    return await get_events_for_date_range(start, end, calendar_names)


def _create_event_sync(
    summary: str,
    start: datetime,
    end: datetime,
    calendar_name: str,
    location: str,
    description: str,
    all_day: bool,
) -> str:
    """Create a CalDAV event and return its UID."""
    from icalendar import Calendar as iCal, Event
    import caldav

    client = _get_client()
    principal = client.principal()
    calendars = principal.calendars()

    target = None
    for cal in calendars:
        if (cal.name or "").lower() == calendar_name.lower():
            target = cal
            break
    if target is None:
        target = calendars[0]  # fallback to first calendar

    uid = str(uuid.uuid4())
    cal = iCal()
    cal.add("prodid", "-//Jarvis//EN")
    cal.add("version", "2.0")

    event = Event()
    event.add("uid",      uid)
    event.add("summary",  summary)
    event.add("dtstamp",  datetime.now(timezone.utc))

    if all_day:
        from icalendar import vDate
        event.add("dtstart", start.date())
        event.add("dtend",   end.date())
    else:
        event.add("dtstart", _to_aware(start))
        event.add("dtend",   _to_aware(end))

    if location:
        event.add("location", location)
    if description:
        event.add("description", description)

    cal.add_component(event)
    target.save_event(cal.to_ical().decode("utf-8"))
    return uid


async def create_event(
    summary: str,
    start: datetime,
    end: datetime,
    calendar_name: str = "Home",
    location: str = "",
    description: str = "",
    all_day: bool = False,
) -> str:
    """Create a new iCloud Calendar event. Returns the event UID."""
    start = _to_aware(start)
    end   = _to_aware(end)
    return await asyncio.to_thread(
        _create_event_sync, summary, start, end, calendar_name, location, description, all_day
    )


def _delete_event_sync(uid: str, calendar_name: str) -> bool:
    client = _get_client()
    principal = client.principal()
    for cal in principal.calendars():
        if (cal.name or "").lower() == calendar_name.lower():
            try:
                results = cal.search(event=True)
                for obj in results:
                    if uid in (obj.data or ""):
                        obj.delete()
                        return True
            except Exception as e:
                logger.error(f"Delete event failed: {e}")
    return False


async def delete_event(uid: str, calendar_name: str) -> bool:
    """Delete a calendar event by UID."""
    return await asyncio.to_thread(_delete_event_sync, uid, calendar_name)


async def find_conflicts(
    start: datetime,
    end: datetime,
    calendar_names: Optional[list[str]] = None,
) -> list[dict]:
    """Return any events that overlap with the proposed time window."""
    events = await get_events_for_date_range(start, end, calendar_names)
    start_iso = _to_aware(start).isoformat()
    end_iso   = _to_aware(end).isoformat()
    return [e for e in events if e.get("start") and e.get("end")
            and e["start"] < end_iso and e["end"] > start_iso]


def format_events_for_claude(events: list[dict]) -> str:
    """Format a list of calendar events into a readable string for Claude."""
    if not events:
        return "No events found."
    lines = []
    for e in events:
        start = e.get("start", "?")
        try:
            dt = datetime.fromisoformat(start)
            if e.get("all_day"):
                start_fmt = dt.strftime("%A, %b %-d")
            else:
                start_fmt = dt.strftime("%A, %b %-d at %-I:%M %p")
        except Exception:
            start_fmt = start

        line = f"- {e['summary']} | {start_fmt}"
        if e.get("location"):
            line += f" @ {e['location']}"
        if e.get("calendar_name"):
            line += f" [{e['calendar_name']}]"
        lines.append(line)
    return "\n".join(lines)
